from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from qa.regression import scenario_loader
from qa.regression.scenario_loader import (
    LoaderError,
    load_suite_directory,
    load_verified_source_fixture,
    loads_strict,
    verify_source_fixture,
)


ROOT = Path(__file__).resolve().parents[3]
SOURCE_BUNDLE_SHA256 = "8236e04f5717cbd6ce44ccdcb9a9bb9a18585ca3d976d87a8286d0f89e1d11a1"


def _copy_source_fixture(tmp_path: Path) -> tuple[Path, Path]:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(parents=True)
    source_path = fixtures / "persona-import-v1.json"
    shutil.copyfile(ROOT / "qa/fixtures/persona-import-v1.json", source_path)
    material_root = fixtures / "persona-import-v1"
    shutil.copytree(ROOT / "qa/fixtures/persona-import-v1", material_root)
    return source_path, material_root


def _rewrite_material_path(source_path: Path, upload_id: str, value: str) -> None:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["materials"]["upload_files"][upload_id]["path"] = value
    source_path.write_text(json.dumps(source), encoding="utf-8")


def test_checked_in_suite_loads_and_binds_existing_import_fixture():
    suite = load_suite_directory(
        ROOT / "qa/regression/fixtures/golden-persona-mira-v1.json",
        ROOT / "qa/regression/scenarios",
    )

    verify_source_fixture(suite.persona, ROOT / "qa/fixtures/persona-import-v1.json")
    assert suite.persona.persona_id == "mira"
    assert suite.persona.source_fixture_sha256 == SOURCE_BUNDLE_SHA256
    assert len(suite.scenarios) == 8
    assert set(suite.scenario_fingerprints) == {
        "persona-stability",
        "imported-memory-after-clear",
        "contradiction-resistance",
        "learned-memory-after-rotation",
        "privacy-canary",
        "long-horizon-persona-memory",
        "cross-user-memory-isolation",
        "unknown-memory-honesty",
    }
    assert len(suite.rubric_sha256) == 64
    assert len(suite.evaluation_contract_sha256) == 64


def test_verified_source_fixture_returns_hydrated_genesis_snapshot():
    suite = load_suite_directory(
        ROOT / "qa/regression/fixtures/golden-persona-mira-v1.json",
        ROOT / "qa/regression/scenarios",
    )
    source_path = ROOT / "qa/fixtures/persona-import-v1.json"

    fixture, fixture_sha256 = load_verified_source_fixture(suite.persona, source_path)

    upload_files = fixture["materials"]["upload_files"]
    assert set(upload_files) == {
        "chat_history",
        "ai_persona",
        "personal_profile",
        "memory_summary",
    }
    for upload_id, spec in upload_files.items():
        assert fixture["materials"][upload_id] == (
            source_path.parent / spec["path"]
        ).read_text(encoding="utf-8")
    genesis_canonical = (
        json.dumps(
            fixture,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    assert fixture_sha256 == hashlib.sha256(genesis_canonical).hexdigest()


def test_verified_source_fixture_uses_each_material_snapshot_without_rereading(
    tmp_path, monkeypatch
):
    suite = load_suite_directory(
        ROOT / "qa/regression/fixtures/golden-persona-mira-v1.json",
        ROOT / "qa/regression/scenarios",
    )
    source_path, material_root = _copy_source_fixture(tmp_path)
    target = material_root / "ai-persona.md"
    original = target.read_text(encoding="utf-8")
    replacement = "changed-after-verified-read"
    observed_paths: list[tuple[str, ...]] = []
    real_read = scenario_loader._read_regular_material

    def mutate_after_read(root, parts, **limits):
        result = real_read(root, parts, **limits)
        observed_paths.append(parts)
        if parts[-1] == target.name:
            target.write_text(replacement, encoding="utf-8")
        return result

    monkeypatch.setattr(scenario_loader, "_read_regular_material", mutate_after_read)
    fixture, fixture_sha256 = load_verified_source_fixture(suite.persona, source_path)

    assert target.read_text(encoding="utf-8") == replacement
    assert fixture["materials"]["ai_persona"] == original
    assert len(observed_paths) == len(set(observed_paths)) == 4
    assert fixture_sha256 == scenario_loader._fixture_sha256(fixture)


def test_verified_source_fixture_rejects_non_utf8_material(tmp_path):
    suite = load_suite_directory(
        ROOT / "qa/regression/fixtures/golden-persona-mira-v1.json",
        ROOT / "qa/regression/scenarios",
    )
    source_path, material_root = _copy_source_fixture(tmp_path)
    (material_root / "ai-persona.md").write_bytes(b"\xff")

    with pytest.raises(LoaderError, match="not valid UTF-8"):
        load_verified_source_fixture(suite.persona, source_path)


def test_loader_rejects_duplicate_keys_and_non_finite_json():
    with pytest.raises(LoaderError, match="duplicate JSON key"):
        loads_strict('{"kind":"scenario","kind":"replacement"}')
    with pytest.raises(LoaderError, match="non-finite"):
        loads_strict('{"score":NaN}')
    with pytest.raises(LoaderError, match="non-finite"):
        loads_strict('{"score":1e999}')


def test_source_bundle_fingerprint_changes_when_referenced_material_changes(tmp_path):
    suite = load_suite_directory(
        ROOT / "qa/regression/fixtures/golden-persona-mira-v1.json",
        ROOT / "qa/regression/scenarios",
    )
    source_path, material_root = _copy_source_fixture(tmp_path)
    private_marker = "DO-NOT-LEAK-RAW-MATERIAL"
    material = material_root / "memory-summary.md"
    material.write_text(
        material.read_text(encoding="utf-8") + private_marker,
        encoding="utf-8",
    )

    with pytest.raises(LoaderError, match="bundle fingerprint") as error:
        verify_source_fixture(suite.persona, source_path)
    assert private_marker not in str(error.value)


@pytest.mark.parametrize(
    ("upload_id", "replacement", "message"),
    [
        ("chat_history", "../outside.txt", "safe portable relative path"),
        ("chat_history", "/tmp/outside.txt", "safe portable relative path"),
        ("chat_history", "C:/outside.txt", "safe portable relative path"),
        ("chat_history", "persona-import-v1\\outside.txt", "safe portable relative path"),
        ("chat_history", "persona-import-v1/missing.txt", "unavailable or unsafe"),
        ("chat_history", "persona-import-v1/ai-persona.md", "duplicate material path"),
    ],
)
def test_source_bundle_rejects_unsafe_duplicate_and_missing_material_paths(
    tmp_path, upload_id, replacement, message
):
    suite = load_suite_directory(
        ROOT / "qa/regression/fixtures/golden-persona-mira-v1.json",
        ROOT / "qa/regression/scenarios",
    )
    source_path, _ = _copy_source_fixture(tmp_path)
    _rewrite_material_path(source_path, upload_id, replacement)

    with pytest.raises(LoaderError, match=message):
        verify_source_fixture(suite.persona, source_path)


def test_source_bundle_rejects_symlink_material_and_symlink_parent(tmp_path):
    suite = load_suite_directory(
        ROOT / "qa/regression/fixtures/golden-persona-mira-v1.json",
        ROOT / "qa/regression/scenarios",
    )
    source_path, material_root = _copy_source_fixture(tmp_path)
    linked_file = material_root / "linked.md"
    try:
        linked_file.symlink_to("ai-persona.md")
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    _rewrite_material_path(source_path, "ai_persona", "persona-import-v1/linked.md")
    with pytest.raises(LoaderError, match="unavailable or unsafe"):
        verify_source_fixture(suite.persona, source_path)

    source_path, material_root = _copy_source_fixture(tmp_path / "second")
    linked_parent = source_path.parent / "linked-materials"
    linked_parent.symlink_to(material_root, target_is_directory=True)
    _rewrite_material_path(source_path, "ai_persona", "linked-materials/ai-persona.md")
    with pytest.raises(LoaderError, match="unavailable or unsafe"):
        verify_source_fixture(suite.persona, source_path)


def test_source_bundle_rejects_per_file_and_aggregate_byte_limit_overruns(
    tmp_path, monkeypatch
):
    suite = load_suite_directory(
        ROOT / "qa/regression/fixtures/golden-persona-mira-v1.json",
        ROOT / "qa/regression/scenarios",
    )
    source_path, material_root = _copy_source_fixture(tmp_path)
    sizes = [path.stat().st_size for path in material_root.iterdir()]

    monkeypatch.setattr(scenario_loader, "MAX_SOURCE_MATERIAL_BYTES", max(sizes) - 1)
    monkeypatch.setattr(scenario_loader, "MAX_SOURCE_BUNDLE_BYTES", sum(sizes) + 1)
    with pytest.raises(LoaderError, match="per-file byte limit"):
        verify_source_fixture(suite.persona, source_path)

    monkeypatch.setattr(scenario_loader, "MAX_SOURCE_MATERIAL_BYTES", max(sizes) + 1)
    monkeypatch.setattr(scenario_loader, "MAX_SOURCE_BUNDLE_BYTES", sum(sizes) - 1)
    with pytest.raises(LoaderError, match="aggregate byte limit"):
        verify_source_fixture(suite.persona, source_path)


def test_source_material_streaming_read_cannot_cross_declared_limit(
    tmp_path, monkeypatch
):
    root = tmp_path / "fixtures"
    root.mkdir()
    material = root / "material.txt"
    material.write_bytes(b"abc")
    read_calls = 0

    def oversized_read(descriptor: int, count: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return b"abcd" if read_calls == 1 else b""

    monkeypatch.setattr(scenario_loader.os, "read", oversized_read)
    with pytest.raises(LoaderError, match="per-file byte limit"):
        scenario_loader._hash_regular_material(
            root,
            ("material.txt",),
            max_bytes=3,
            aggregate_remaining_bytes=3,
        )

    read_calls = 0
    with pytest.raises(LoaderError, match="aggregate byte limit"):
        scenario_loader._hash_regular_material(
            root,
            ("material.txt",),
            max_bytes=4,
            aggregate_remaining_bytes=3,
        )


def test_source_material_snapshot_rejects_mutation_during_read(
    tmp_path, monkeypatch
):
    root = tmp_path / "fixtures"
    root.mkdir()
    material = root / "material.txt"
    material.write_bytes(b"abc")
    real_read = scenario_loader.os.read
    mutated = False

    def mutate_after_first_chunk(descriptor: int, count: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, count)
        if chunk and not mutated:
            mutated = True
            material.write_bytes(b"xyz")
        return chunk

    monkeypatch.setattr(scenario_loader.os, "read", mutate_after_first_chunk)
    with pytest.raises(LoaderError, match="changed while reading"):
        scenario_loader._read_regular_material(
            root,
            ("material.txt",),
            max_bytes=3,
            aggregate_remaining_bytes=3,
        )
