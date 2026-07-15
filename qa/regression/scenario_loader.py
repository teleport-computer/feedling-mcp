"""Strict loaders and cross-fixture checks for regression inputs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from qa.regression.contracts import (
    ContractError,
    Experiment,
    ExperimentResult,
    GoldenPersona,
    Scenario,
    canonical_json_sha256,
)
from qa.regression.versions import evaluation_versions


MAX_CONTRACT_BYTES = 2 * 1024 * 1024
MAX_SOURCE_MATERIAL_BYTES = 1024 * 1024
MAX_SOURCE_BUNDLE_BYTES = 4 * 1024 * 1024


class LoaderError(ContractError):
    """A contract file could not be read or bound safely."""


def _reject_constant(value: str) -> None:
    raise LoaderError(f"non-finite JSON constant is not allowed: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise LoaderError("non-finite JSON number is not allowed")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LoaderError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def loads_strict(raw: bytes | str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except LoaderError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LoaderError("contract file is not valid UTF-8 JSON") from None


def read_contract_json(path: Path, *, max_bytes: int = MAX_CONTRACT_BYTES) -> Any:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    candidate = Path(path)
    if candidate.is_symlink():
        raise LoaderError("contract path must not be a symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError:
        raise LoaderError("contract file is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > max_bytes
        ):
            raise LoaderError("contract file size or type is invalid")
        raw = os.read(descriptor, metadata.st_size + 1)
        if len(raw) != metadata.st_size:
            raise LoaderError("contract file changed while reading")
    finally:
        os.close(descriptor)
    return loads_strict(raw)


def load_golden_persona(path: Path) -> GoldenPersona:
    value = read_contract_json(path)
    if not isinstance(value, dict):
        raise LoaderError("golden persona must be a JSON object")
    return GoldenPersona.from_dict(value)


def load_scenario(path: Path) -> Scenario:
    value = read_contract_json(path)
    if not isinstance(value, dict):
        raise LoaderError("scenario must be a JSON object")
    return Scenario.from_dict(value)


def load_experiment(path: Path) -> Experiment:
    value = read_contract_json(path)
    if not isinstance(value, dict):
        raise LoaderError("experiment must be a JSON object")
    return Experiment.from_dict(value)


def load_experiment_result(path: Path) -> ExperimentResult:
    value = read_contract_json(path, max_bytes=64 * 1024 * 1024)
    if not isinstance(value, dict):
        raise LoaderError("experiment result must be a JSON object")
    return ExperimentResult.from_dict(value)


def _portable_material_path(value: Any) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or not value:
        raise LoaderError("source material path must be a non-empty string")
    if "\\" in value or "\x00" in value:
        raise LoaderError("source material path is not a safe portable relative path")
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or posix_path.as_posix() != value
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise LoaderError("source material path is not a safe portable relative path")
    return value, posix_path.parts


def _source_material_paths(
    source: Any,
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    if not isinstance(source, dict) or source.get("version") != 1 or isinstance(
        source.get("version"), bool
    ):
        raise LoaderError("source fixture manifest version is invalid")
    materials = source.get("materials")
    if not isinstance(materials, dict):
        raise LoaderError("source fixture materials are invalid")
    upload_files = materials.get("upload_files")
    if not isinstance(upload_files, dict) or not upload_files:
        raise LoaderError("source fixture upload files are invalid")

    paths: list[tuple[str, str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for upload_id in sorted(upload_files):
        if not isinstance(upload_id, str) or not upload_id:
            raise LoaderError("source fixture upload id is invalid")
        upload = upload_files[upload_id]
        if not isinstance(upload, dict):
            raise LoaderError("source fixture upload entry is invalid")
        path, parts = _portable_material_path(upload.get("path"))
        if path in seen:
            raise LoaderError("source fixture references a duplicate material path")
        seen.add(path)
        paths.append((upload_id, path, parts))
    return tuple(sorted(paths, key=lambda item: item[1]))


def _read_regular_material(
    root: Path,
    parts: tuple[str, ...],
    *,
    max_bytes: int,
    aggregate_remaining_bytes: int,
) -> tuple[bytes, str, int]:
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        file_flags |= os.O_CLOEXEC

    descriptors: list[int] = []
    try:
        current = os.open(root, directory_flags)
        descriptors.append(current)
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise LoaderError("source fixture directory is invalid")
        for part in parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise LoaderError("source material parent is invalid")
        material = os.open(parts[-1], file_flags, dir_fd=current)
        descriptors.append(material)
        before = os.fstat(material)
        if not stat.S_ISREG(before.st_mode):
            raise LoaderError("source material must be a regular non-symlink file")
        if before.st_size > max_bytes:
            raise LoaderError("source material exceeds the per-file byte limit")
        if before.st_size > aggregate_remaining_bytes:
            raise LoaderError("source fixture materials exceed the aggregate byte limit")

        digest = hashlib.sha256()
        chunks: list[bytes] = []
        bytes_read = 0
        while True:
            read_limit = min(
                64 * 1024,
                max_bytes - bytes_read + 1,
                aggregate_remaining_bytes - bytes_read + 1,
            )
            chunk = os.read(material, read_limit)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > max_bytes:
                raise LoaderError("source material exceeds the per-file byte limit")
            if bytes_read > aggregate_remaining_bytes:
                raise LoaderError(
                    "source fixture materials exceed the aggregate byte limit"
                )
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(material)
        before_state = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_state = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_state != after_state or bytes_read != before.st_size:
            raise LoaderError("source material changed while reading")
        return b"".join(chunks), digest.hexdigest(), bytes_read
    except OSError:
        raise LoaderError("source material is unavailable or unsafe") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _hash_regular_material(
    root: Path,
    parts: tuple[str, ...],
    *,
    max_bytes: int,
    aggregate_remaining_bytes: int,
) -> tuple[str, int]:
    _raw, digest, size = _read_regular_material(
        root,
        parts,
        max_bytes=max_bytes,
        aggregate_remaining_bytes=aggregate_remaining_bytes,
    )
    return digest, size


def _fixture_sha256(fixture: dict[str, Any]) -> str:
    """Match the canonical fixture digest stored in Genesis evidence receipts."""

    try:
        raw = (
            json.dumps(
                fixture,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise LoaderError("source fixture cannot be canonically encoded") from None
    return hashlib.sha256(raw).hexdigest()


def _source_fixture_snapshot(
    source_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    source = read_contract_json(source_path)
    if not isinstance(source, dict):
        raise LoaderError("source fixture manifest must be a JSON object")
    paths = _source_material_paths(source)
    hydrated = deepcopy(source)
    hydrated_materials = hydrated["materials"]
    manifest_sha256 = canonical_json_sha256(source)
    material_fingerprints = []
    total_bytes = 0
    for upload_id, path, parts in paths:
        raw, digest, size = _read_regular_material(
            Path(source_path).parent,
            parts,
            max_bytes=MAX_SOURCE_MATERIAL_BYTES,
            aggregate_remaining_bytes=MAX_SOURCE_BUNDLE_BYTES - total_bytes,
        )
        total_bytes += size
        if total_bytes > MAX_SOURCE_BUNDLE_BYTES:
            raise LoaderError("source fixture materials exceed the aggregate byte limit")
        try:
            hydrated_materials[upload_id] = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise LoaderError("source material is not valid UTF-8") from None
        material_fingerprints.append(
            {"path": path, "sha256": digest, "size_bytes": size}
        )
    bundle = {
        "schema_version": 1,
        "kind": "source_fixture_bundle",
        "manifest_sha256": manifest_sha256,
        "materials": material_fingerprints,
    }
    return (
        source,
        hydrated,
        canonical_json_sha256(bundle),
        _fixture_sha256(hydrated),
    )


def _source_fixture_bundle(source_path: Path) -> tuple[dict[str, Any], str]:
    source, _fixture, observed, _fixture_sha256_value = _source_fixture_snapshot(
        source_path
    )
    return source, observed


def load_verified_source_fixture(
    persona: GoldenPersona, source_path: Path
) -> tuple[dict[str, Any], str]:
    """Load, bind, and hydrate an import fixture from one verified disk snapshot.

    The returned digest uses the same canonical encoding as Genesis evidence receipts.
    Referenced material files are each opened and read exactly once.
    """

    source, fixture, observed, fixture_sha256 = _source_fixture_snapshot(source_path)
    if source.get("fixture_id") != persona.source_fixture_id:
        raise LoaderError("golden persona source fixture id does not match")
    if observed != persona.source_fixture_sha256:
        raise LoaderError("golden persona source fixture bundle fingerprint does not match")
    return fixture, fixture_sha256


def verify_source_fixture(persona: GoldenPersona, source_path: Path) -> None:
    load_verified_source_fixture(persona, source_path)


@dataclass(frozen=True, kw_only=True)
class RegressionSuite:
    persona: GoldenPersona
    scenarios: tuple[Scenario, ...]

    @property
    def scenario_fingerprints(self) -> dict[str, str]:
        return {
            scenario.scenario_id: scenario.fingerprint_sha256()
            for scenario in self.scenarios
        }

    @property
    def rubric_sha256(self) -> str:
        return canonical_json_sha256(
            {
                "persona_rubric_sha256": self.persona.rubric_sha256,
                "scenario_rubric_sha256": {
                    scenario.scenario_id: scenario.rubric_sha256
                    for scenario in self.scenarios
                },
            }
        )

    @property
    def evaluation_contract_sha256(self) -> str:
        return canonical_json_sha256(
            {
                "persona_fixture_sha256": self.persona.fixture_sha256,
                "rubric_sha256": self.rubric_sha256,
                "scenario_fingerprints": self.scenario_fingerprints,
                "evaluation_versions": evaluation_versions(),
            }
        )


def load_suite(persona_path: Path, scenario_paths: list[Path]) -> RegressionSuite:
    persona = load_golden_persona(persona_path)
    if not scenario_paths:
        raise LoaderError("at least one scenario is required")
    scenarios = tuple(load_scenario(path) for path in scenario_paths)
    ids = [scenario.scenario_id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise LoaderError("scenario ids must be unique")
    for scenario in scenarios:
        if (
            scenario.persona_id != persona.persona_id
            or scenario.persona_version != persona.persona_version
        ):
            raise LoaderError("scenario persona version does not match the golden persona")
    return RegressionSuite(persona=persona, scenarios=scenarios)


def load_suite_directory(persona_path: Path, scenario_root: Path) -> RegressionSuite:
    root = Path(scenario_root)
    if root.is_symlink() or not root.is_dir():
        raise LoaderError("scenario directory is invalid")
    paths = sorted(root.glob("*.json"), key=lambda item: item.name)
    return load_suite(persona_path, paths)
