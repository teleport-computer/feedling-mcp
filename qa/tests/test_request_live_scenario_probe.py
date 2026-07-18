from __future__ import annotations

import json
import stat
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from qa import request_cot_delivery_probe as cot_request
from qa import request_live_scenario_probe as request


RUN_ID = "run-sequence-123"
PROFILE_ID = "official-anthropic"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path.resolve()


def _environment(work: Path) -> dict[str, str]:
    return {
        "QA_RUN_ID": RUN_ID,
        "QA_PROFILE_ID": PROFILE_ID,
        "QA_WORK_ROOT": str(work),
    }


def _facts_payload(
    scenario_id: str,
    attempt: int,
    *,
    status: str = "PASS",
    failure_code: str = "NONE",
) -> dict:
    private_facts = {"bounded": True}
    receipt = {
        "run_id": RUN_ID,
        "profile_id": PROFILE_ID,
        "scenario_id": scenario_id,
        "attempt": attempt,
        "status": status,
        "failure_code": failure_code,
        "private_facts_sha256": request._canonical_sha256(private_facts),
    }
    return {
        "schema_version": 1,
        "profile_id": PROFILE_ID,
        "scenario_id": scenario_id,
        "attempt": attempt,
        "receipt_sha256": request._canonical_sha256(receipt),
        "receipt": receipt,
        "private_facts": private_facts,
    }


def _publish(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _wait_for(path: Path, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert path.exists()


def _load_marker(
    marker: Path,
    scenario_id: str,
    attempt: int,
    previous_receipt_sha256: str | None,
    cot_terminal_sha256: str | None = None,
) -> dict:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            return request.load_request_marker(
                marker,
                run_id=RUN_ID,
                profile_id=PROFILE_ID,
                scenario_id=scenario_id,
                attempt=attempt,
                previous_receipt_sha256=previous_receipt_sha256,
                cot_terminal_sha256=cot_terminal_sha256,
            )
        except request.LiveProbeRequestError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def _complete(
    work: Path,
    scenario_id: str,
    attempt: int = 1,
    *,
    previous_receipt_sha256: str | None,
    status: str = "PASS",
    failure_code: str = "NONE",
    before_publish: Callable[[], None] | None = None,
) -> dict:
    marker = request.request_path(work, scenario_id, attempt)
    facts = request.facts_path(work, scenario_id, attempt)
    result: dict[str, object] = {}

    def run() -> None:
        try:
            result["payload"] = request.request_and_wait(
                scenario_id=scenario_id,
                attempt=attempt,
                request=marker,
                facts=facts,
                environment=_environment(work),
                wait_seconds=2.0,
                sequence_wait_seconds=2.0,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    _wait_for(marker)
    loaded = _load_marker(
        marker,
        scenario_id,
        attempt,
        previous_receipt_sha256,
    )
    assert loaded["previous_receipt_sha256"] == previous_receipt_sha256
    if before_publish is not None:
        before_publish()
    payload = _facts_payload(
        scenario_id,
        attempt,
        status=status,
        failure_code=failure_code,
    )
    _publish(facts, payload)
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert "error" not in result
    assert result["payload"] == payload
    return payload


def _complete_through(
    work: Path,
    final_scenario: str,
    *,
    persona_evidence: bool = False,
) -> dict:
    previous: str | None = None
    last: dict = {}
    for scenario_id in request.LIVE_SCENARIO_IDS:
        if scenario_id == "P0-13":
            break
        hook = None
        if scenario_id == "P0-06" and persona_evidence:
            def publish_persona_evidence() -> None:
                _publish(
                    work / "p0-06-private-evidence.json", {"private": True}
                )

            hook = publish_persona_evidence
        last = _complete(
            work,
            scenario_id,
            previous_receipt_sha256=previous,
            before_publish=hook,
        )
        previous = last["receipt_sha256"]
        if scenario_id == "P0-06" and not persona_evidence:
            _publish(work / "p0-06-semantic-judgment.json", {"reviewed": True})
        if scenario_id == final_scenario:
            return last
    raise AssertionError(f"unknown final scenario: {final_scenario}")


def test_concurrent_future_helpers_serialize_and_chain_receipt_hashes(tmp_path):
    work = _private_dir(tmp_path / "work")
    scenarios = ("P0-02", "P0-03", "P0-04")
    barrier = threading.Barrier(len(scenarios))
    results: dict[str, object] = {}

    def run(scenario_id: str) -> None:
        try:
            barrier.wait(timeout=2.0)
            results[scenario_id] = request.request_and_wait(
                scenario_id=scenario_id,
                attempt=1,
                request=request.request_path(work, scenario_id, 1),
                facts=request.facts_path(work, scenario_id, 1),
                environment=_environment(work),
                wait_seconds=2.0,
                sequence_wait_seconds=2.0,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            results[f"{scenario_id}-error"] = exc

    threads = [threading.Thread(target=run, args=(scenario,)) for scenario in reversed(scenarios)]
    for thread in threads:
        thread.start()

    previous: str | None = None
    for index, scenario_id in enumerate(scenarios):
        marker = request.request_path(work, scenario_id, 1)
        _wait_for(marker)
        for future in scenarios[index + 1 :]:
            assert not request.request_path(work, future, 1).exists()
        _load_marker(
            marker,
            scenario_id,
            1,
            previous,
        )
        payload = _facts_payload(scenario_id, 1)
        _publish(request.facts_path(work, scenario_id, 1), payload)
        previous = payload["receipt_sha256"]

    for thread in threads:
        thread.join(timeout=3.0)
        assert not thread.is_alive()
    assert set(results) == set(scenarios)
    state_path = request.sequence_gate_path(work)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert [entry["scenario_id"] for entry in state["completed"]] == list(scenarios)
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_p0_07_waits_for_passed_persona_capture_finalization(tmp_path):
    work = _private_dir(tmp_path / "work")
    capture = _complete_through(work, "P0-06", persona_evidence=True)
    _publish(work / "p0-06-semantic-judgment.json", {"reviewed": True})
    phase_descriptor = request.acquire_sequence_phase_gate(
        work,
        run_id=RUN_ID,
        profile_id=PROFILE_ID,
        phase="P0-06-FINALIZE",
        wait_seconds=2.0,
    )
    marker = request.request_path(work, "P0-07", 1)
    result: dict[str, object] = {}

    def run() -> None:
        try:
            result["payload"] = request.request_and_wait(
                scenario_id="P0-07",
                attempt=1,
                request=marker,
                facts=request.facts_path(work, "P0-07", 1),
                environment=_environment(work),
                wait_seconds=2.0,
                sequence_wait_seconds=2.0,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.05)
    assert not marker.exists()
    (work / "p0-06-private-evidence.json").unlink()
    time.sleep(0.05)
    assert not marker.exists()
    request._release_sequence_turn(phase_descriptor)
    _wait_for(marker)
    _load_marker(
        marker,
        "P0-07",
        1,
        capture["receipt_sha256"],
    )
    payload = _facts_payload("P0-07", 1)
    _publish(request.facts_path(work, "P0-07", 1), payload)
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert result == {"payload": payload}


def test_p0_13_waits_for_terminal_p0_12_facts(tmp_path, monkeypatch):
    work = _private_dir(tmp_path / "work")
    previous = _complete_through(work, "P0-11")["receipt_sha256"]
    phase_descriptor = request.acquire_sequence_phase_gate(
        work,
        run_id=RUN_ID,
        profile_id=PROFILE_ID,
        phase="P0-12",
        wait_seconds=2.0,
    )
    marker = request.request_path(work, "P0-13", 1)
    result: dict[str, object] = {}
    terminal_sha256 = "c" * 64
    monkeypatch.setattr(
        cot_request,
        "_load_facts",
        lambda path, profile_id, *, allow_unavailable: {
            "path": str(path),
            "profile_id": profile_id,
            "terminal_sha256": terminal_sha256,
            "allow_unavailable": allow_unavailable,
        },
    )

    def run() -> None:
        try:
            result["payload"] = request.request_and_wait(
                scenario_id="P0-13",
                attempt=1,
                request=marker,
                facts=request.facts_path(work, "P0-13", 1),
                environment=_environment(work),
                wait_seconds=2.0,
                sequence_wait_seconds=2.0,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.05)
    assert not marker.exists()
    _publish(work / "cot-delivery-facts.json", {"terminal": True})
    time.sleep(0.05)
    assert not marker.exists()
    request._release_sequence_turn(phase_descriptor)
    _wait_for(marker)
    _load_marker(
        marker,
        "P0-13",
        1,
        previous,
        terminal_sha256,
    )
    payload = _facts_payload("P0-13", 1)
    _publish(request.facts_path(work, "P0-13", 1), payload)
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert result == {"payload": payload}


def test_future_helper_fails_closed_on_impossible_predecessor(tmp_path):
    work = _private_dir(tmp_path / "work")
    request.write_request_marker(
        request.request_path(work, "P0-02", 1),
        run_id=RUN_ID,
        profile_id=PROFILE_ID,
        scenario_id="P0-02",
        attempt=1,
        previous_receipt_sha256=None,
    )

    with pytest.raises(request.LiveProbeRequestError, match="predecessor is incomplete"):
        request.request_and_wait(
            scenario_id="P0-03",
            attempt=1,
            request=request.request_path(work, "P0-03", 1),
            facts=request.facts_path(work, "P0-03", 1),
            environment=_environment(work),
            wait_seconds=0.1,
            sequence_wait_seconds=0.1,
        )


def test_retry_chain_uses_attempt_one_receipt_as_causal_head(tmp_path):
    work = _private_dir(tmp_path / "work")
    previous: str | None = None
    for scenario_id in request.LIVE_SCENARIO_IDS:
        if scenario_id == "P0-08":
            break
        payload = _complete(
            work,
            scenario_id,
            previous_receipt_sha256=previous,
        )
        previous = payload["receipt_sha256"]
        if scenario_id == "P0-06":
            _publish(work / "p0-06-semantic-judgment.json", {"reviewed": True})
    attempt_one = _complete(
        work,
        "P0-08",
        previous_receipt_sha256=previous,
        status="AGENT_ERROR",
        failure_code="CHAT_TIMEOUT",
    )

    with pytest.raises(request.LiveProbeRequestError, match="sequence wait timed out"):
        request.request_and_wait(
            scenario_id="P0-09",
            attempt=1,
            request=request.request_path(work, "P0-09", 1),
            facts=request.facts_path(work, "P0-09", 1),
            environment=_environment(work),
            wait_seconds=0.05,
            sequence_wait_seconds=0.05,
        )

    attempt_two = _complete(
        work,
        "P0-08",
        2,
        previous_receipt_sha256=attempt_one["receipt_sha256"],
    )
    _complete(
        work,
        "P0-09",
        previous_receipt_sha256=attempt_two["receipt_sha256"],
    )


def test_request_marker_rejects_wrong_predecessor_digest(tmp_path):
    work = _private_dir(tmp_path / "work")
    marker = request.request_path(work, "P0-03", 1)
    request.write_request_marker(
        marker,
        run_id=RUN_ID,
        profile_id=PROFILE_ID,
        scenario_id="P0-03",
        attempt=1,
        previous_receipt_sha256="a" * 64,
    )

    with pytest.raises(request.LiveProbeRequestError, match="marker is invalid"):
        request.load_request_marker(
            marker,
            run_id=RUN_ID,
            profile_id=PROFILE_ID,
            scenario_id="P0-03",
            attempt=1,
            previous_receipt_sha256="b" * 64,
        )
