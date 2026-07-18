from __future__ import annotations

import json
import stat
import threading
import time
from pathlib import Path

import pytest

from qa import request_cot_delivery_probe as request


PROFILE_ID = "official-gemini"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path.resolve()


def _facts(status: str, digest: str = "a" * 64) -> dict:
    return {
        "schema_version": 1,
        "profile_id": PROFILE_ID,
        "receipt_sha256": digest,
        "terminal_sha256": request.cot_terminal_sha256(
            PROFILE_ID,
            receipt_sha256=digest,
            status="RECEIPT",
            failure_code="NONE",
        ),
        "receipt": {
            "status": status,
            "failure_code": "NONE" if status == "PASS" else "CHAT_TIMEOUT",
        },
    }


def _write_private_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    path.chmod(0o600)


@pytest.mark.parametrize("status", ("PASS", "FAIL", "UNVERIFIED"))
def test_one_shot_helper_accepts_all_completed_receipt_statuses(
    tmp_path, monkeypatch, status
):
    work = _private_dir(tmp_path / "work")
    marker = request.request_path(work)
    facts = request.facts_path(work)
    payload = _facts(status)
    monkeypatch.setattr(
        request,
        "validate_cot_receipt_document",
        lambda receipt, profile_id: (receipt, "a" * 64),
    )
    result: dict[str, object] = {}

    def run() -> None:
        try:
            result["payload"] = request.request_and_wait(
                request=marker,
                facts=facts,
                environment={
                    "QA_PROFILE_ID": PROFILE_ID,
                    "QA_WORK_ROOT": str(work),
                },
                wait_seconds=2.0,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1.0
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert marker.read_text(encoding="utf-8") == f"{PROFILE_ID}\n"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    _write_private_json(facts, payload)
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert "error" not in result
    assert result["payload"] == payload


def test_helper_rejects_explicit_unavailable_facts(tmp_path):
    facts = tmp_path.resolve() / "facts.json"
    _write_private_json(
        facts,
        {
            "schema_version": 1,
            "profile_id": PROFILE_ID,
            "receipt_sha256": None,
            "terminal_sha256": request.cot_terminal_sha256(
                PROFILE_ID,
                receipt_sha256=None,
                status="UNAVAILABLE",
                failure_code="TRUSTED_PROBE_ERROR",
            ),
            "status": "UNAVAILABLE",
            "failure_code": "TRUSTED_PROBE_ERROR",
        },
    )

    with pytest.raises(request.CotProbeUnavailableError, match="unavailable"):
        request._load_facts(facts, PROFILE_ID)


def test_helper_rejects_digest_mismatch(tmp_path, monkeypatch):
    facts = tmp_path.resolve() / "facts.json"
    _write_private_json(facts, _facts("PASS", digest="b" * 64))
    monkeypatch.setattr(
        request,
        "validate_cot_receipt_document",
        lambda receipt, profile_id: (receipt, "a" * 64),
    )

    with pytest.raises(request.CotProbeRequestError, match="binding"):
        request._load_facts(facts, PROFILE_ID)


def test_helper_rejects_preexisting_marker_without_reusing_it(tmp_path):
    work = _private_dir(tmp_path / "work")
    marker = request.request_path(work)
    marker.write_text(f"{PROFILE_ID}\n", encoding="utf-8")
    marker.chmod(0o600)

    with pytest.raises(request.CotProbeRequestError, match="paths"):
        request.request_and_wait(
            request=marker,
            facts=request.facts_path(work),
            environment={
                "QA_PROFILE_ID": PROFILE_ID,
                "QA_WORK_ROOT": str(work),
            },
            wait_seconds=0.01,
        )


def test_cli_environment_holds_profile_gate_for_p0_12(
    tmp_path, monkeypatch, capsys
):
    work = _private_dir(tmp_path / "work")
    seen: dict[str, object] = {}
    monkeypatch.setenv("QA_RUN_ID", "run-123")
    monkeypatch.setenv("QA_PROFILE_ID", PROFILE_ID)
    monkeypatch.setenv("QA_WORK_ROOT", str(work))

    def acquire(work_root, **identity):
        seen.update(work_root=work_root, **identity)
        return 92

    monkeypatch.setattr(request, "acquire_sequence_phase_gate", acquire)
    monkeypatch.setattr(
        request,
        "request_and_wait",
        lambda **_kwargs: {
            "receipt": {"status": "PASS", "failure_code": "NONE"}
        },
    )

    assert request.main(
        [
            "--request",
            str(request.request_path(work)),
            "--facts",
            str(request.facts_path(work)),
        ]
    ) == 0
    assert seen == {
        "work_root": work,
        "run_id": "run-123",
        "profile_id": PROFILE_ID,
        "phase": "P0-12",
    }
    assert json.loads(capsys.readouterr().out)["scenario_id"] == "P0-12"
