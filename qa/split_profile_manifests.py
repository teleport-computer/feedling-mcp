#!/usr/bin/env python3
"""Split private provisioning credentials into isolated one-account files."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from qa.orchestration_contract import MEMORY_CONTRACT_PROFILE_ID, PROFILE_IDS
except ModuleNotFoundError:  # Direct `python qa/...py` execution.
    from orchestration_contract import MEMORY_CONTRACT_PROFILE_ID, PROFILE_IDS


class ManifestSplitError(RuntimeError):
    """Sanitized manifest-split failure."""


def _private_file(path: Path, label: str) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ManifestSplitError(f"{label} is unsafe")
    try:
        metadata = path.lstat()
    except OSError:
        raise ManifestSplitError(f"{label} is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
    ):
        raise ManifestSplitError(f"{label} is unsafe")


def _private_empty_directory(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ManifestSplitError("profile manifest directory is unsafe")
    try:
        metadata = path.lstat()
        entries = list(path.iterdir())
    except OSError:
        raise ManifestSplitError("profile manifest directory is unavailable") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
        or entries
    ):
        raise ManifestSplitError("profile manifest directory is unsafe or nonempty")


def _load_manifest(path: Path) -> dict[str, Any]:
    _private_file(path, "provisioning manifest")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ManifestSplitError("provisioning manifest is unreadable") from None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ManifestSplitError("provisioning manifest schema is unsupported")
    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        raise ManifestSplitError("provisioning manifest profile matrix is invalid")
    ids = [row.get("profile_id") if isinstance(row, dict) else None for row in profiles]
    if ids != list(PROFILE_IDS):
        raise ManifestSplitError("provisioning manifest profile matrix is invalid")
    return payload


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise ManifestSplitError("unable to create isolated profile manifest") from None
    _private_file(path, "isolated profile manifest")


def _future_private_file(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or path.exists():
        raise ManifestSplitError("memory contract manifest path is unsafe")
    try:
        metadata = path.parent.lstat()
    except OSError:
        raise ManifestSplitError(
            "memory contract manifest parent is unavailable"
        ) from None
    if (
        path.parent.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise ManifestSplitError("memory contract manifest parent is unsafe")


def split_manifest(
    manifest_path: Path,
    output_dir: Path,
    memory_output: Path | None = None,
) -> tuple[Path, ...]:
    payload = _load_manifest(manifest_path)
    _private_empty_directory(output_dir)
    if memory_output is not None:
        if memory_output.parent.resolve() != output_dir.resolve():
            raise ManifestSplitError(
                "memory contract manifest must share the isolated manifest directory"
            )
        _future_private_file(memory_output)
    profiles = payload["profiles"]
    created: list[Path] = []
    try:
        for profile_id, profile in zip(PROFILE_IDS, profiles, strict=True):
            isolated = dict(payload)
            isolated["profiles"] = [profile]
            isolated.pop("auxiliary_accounts", None)
            destination = output_dir / f"{profile_id}.json"
            _write_private_json(destination, isolated)
            created.append(destination)
        if memory_output is not None:
            auxiliary = payload.get("auxiliary_accounts")
            if (
                not isinstance(auxiliary, list)
                or len(auxiliary) != 1
                or not isinstance(auxiliary[0], dict)
                or auxiliary[0].get("profile_id") != MEMORY_CONTRACT_PROFILE_ID
                or auxiliary[0].get("provision_status") != "ready"
            ):
                raise ManifestSplitError(
                    "provisioning manifest memory contract account is invalid"
                )
            isolated_memory = dict(payload)
            isolated_memory["profiles"] = [auxiliary[0]]
            isolated_memory.pop("auxiliary_accounts", None)
            _write_private_json(memory_output, isolated_memory)
            created.append(memory_output)
        expected_names = {f"{profile_id}.json" for profile_id in PROFILE_IDS}
        if memory_output is not None:
            expected_names.add(f"{MEMORY_CONTRACT_PROFILE_ID}.json")
        if {path.name for path in output_dir.iterdir()} != expected_names:
            raise ManifestSplitError("isolated profile manifest set is incomplete")
        directory_fd = os.open(output_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return tuple(created)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Split private API-key QA manifests")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--memory-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        split_manifest(args.manifest, args.output_dir, args.memory_output)
    except ManifestSplitError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "ERROR: profile manifest split encountered an internal error",
            file=sys.stderr,
        )
        return 1
    print(
        "nine isolated profile manifests created"
        + (" with dedicated memory contract manifest" if args.memory_output else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
