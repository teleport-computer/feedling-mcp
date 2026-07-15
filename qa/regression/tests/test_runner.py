from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from qa.regression.runner import (
    BLOCKED_EVIDENCE,
    COMPLETED,
    INFRA_ERROR,
    RegressionRunner,
    run_scenario,
)
from qa.regression.target import (
    BOUNDARY_CLEAR_HISTORY,
    BOUNDARY_NONE,
    BOUNDARY_ROTATE_RUNTIME_SESSION,
    BoundaryResult,
    FeedlingTarget,
    TargetCapabilities,
    TargetContext,
    TargetError,
    TargetResponse,
    TargetSession,
)


@dataclass(frozen=True)
class _Transition:
    target_turn_id: str | None
    condition: str = "always"
    value: Any = ""
    case_sensitive: bool = False


@dataclass(frozen=True)
class _Turn:
    turn_id: str
    content: str
    role: str = "user"
    session_key: str = "default"
    boundary_before: str = BOUNDARY_NONE
    requirements: tuple[str, ...] = ()
    transitions: tuple[_Transition, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Scenario:
    scenario_id: str
    turns: tuple[_Turn, ...]
    version: str = "1"
    requirements: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def fingerprint_sha256(self) -> str:
        return "a" * 64


class _FakeTarget:
    def __init__(
        self,
        responses: Mapping[str, str | Exception],
        *,
        clear_history: bool = True,
        runtime_rotation: bool = False,
        delay_seconds: float = 0.0,
        fail_close: bool = False,
        response_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._responses = dict(responses)
        self._capabilities = TargetCapabilities(
            clear_history=clear_history,
            runtime_session_rotation=runtime_rotation,
        )
        self.delay_seconds = delay_seconds
        self.fail_close = fail_close
        self.response_metadata = dict(response_metadata or {})
        self.contexts: list[TargetContext] = []
        self.sent: list[tuple[str, str, str]] = []
        self.boundaries: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    @property
    def target_id(self) -> str:
        return "fake-candidate"

    @property
    def capabilities(self) -> TargetCapabilities:
        return self._capabilities

    def open_session(self, context: TargetContext) -> TargetSession:
        with self._lock:
            self.contexts.append(context)
            sequence = len(self.contexts)
        return TargetSession(
            target_id=self.target_id,
            session_key=context.session_key,
            session_id=f"session-{sequence}",
        )

    def send(
        self,
        session: TargetSession,
        *,
        turn_id: str,
        prompt: str,
        timeout_seconds: float,
    ) -> TargetResponse:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.sent.append((session.session_key, turn_id, prompt))
        try:
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            scripted = self._responses[turn_id]
            if isinstance(scripted, Exception):
                raise scripted
            return TargetResponse(
                text=scripted,
                request_id=f"request-{turn_id}",
                response_id=f"response-{turn_id}",
                trace_id=f"trace-{turn_id}",
                latency_ms=1.5,
                metadata={"reply_correlation": "exact", **self.response_metadata},
            )
        finally:
            with self._lock:
                self._active -= 1

    def apply_boundary(
        self,
        session: TargetSession,
        *,
        action: str,
    ) -> BoundaryResult:
        with self._lock:
            self.boundaries.append((session.session_key, action))
        if action == BOUNDARY_CLEAR_HISTORY:
            return BoundaryResult(
                session=replace(session, generation=session.generation + 1),
                action=action,
                boundary_kind="transcript",
                runtime_session_rotated=False,
                evidence={
                    "transcript_cleared": True,
                    "runtime_session_rotation_claimed": False,
                },
            )
        if action == BOUNDARY_ROTATE_RUNTIME_SESSION:
            return BoundaryResult(
                session=replace(session, generation=session.generation + 1),
                action=action,
                boundary_kind="runtime_session",
                runtime_session_rotated=True,
                evidence={
                    "rotated": True,
                    "before_runtime_session_id": "runtime-before",
                    "after_runtime_session_id": "runtime-after",
                },
            )
        raise AssertionError(action)

    def close_session(self, session: TargetSession) -> None:
        with self._lock:
            self.closed.append(session.session_id)
        if self.fail_close:
            raise TargetError("SESSION_CLOSE_FAILED")


def _turns(trajectory: Any) -> list[Any]:
    value = getattr(trajectory, "turns", None)
    if value is None:
        value = getattr(trajectory, "turn_evidence", ())
    return list(value)


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _trajectory_dict(trajectory: Any) -> dict[str, Any]:
    method = getattr(trajectory, "to_dict", None)
    if callable(method):
        return method()
    return dict(vars(trajectory))


def test_runner_collects_correlated_multiturn_trajectory_and_closes_session():
    scenario = _Scenario(
        scenario_id="persona-basic",
        turns=(
            _Turn(turn_id="identity", content="Who are you?"),
            _Turn(turn_id="style", content="Help after a hard day."),
        ),
    )
    target = _FakeTarget(
        {"identity": "I am Mira.", "style": "Let's take one small step."}
    )

    trajectory = run_scenario(scenario, target, repeat_index=2)

    assert trajectory.status == COMPLETED
    assert trajectory.failure_code == "NONE"
    assert trajectory.repeat_index == 2
    turns = _turns(trajectory)
    assert [_value(turn, "turn_id") for turn in turns] == ["identity", "style"]
    assert [_value(turn, "response") for turn in turns] == [
        "I am Mira.",
        "Let's take one small step.",
    ]
    assert _value(turns[0], "request_id") == "request-identity"
    assert _value(turns[0], "response_id") == "response-identity"
    assert len(target.closed) == 1


def test_state_machine_takes_first_matching_branch_and_skips_other_turns():
    scenario = _Scenario(
        scenario_id="memory-branch",
        turns=(
            _Turn(
                turn_id="probe",
                content="What is the ritual?",
                transitions=(
                    _Transition(
                        target_turn_id="confirm",
                        condition="contains",
                        value="jasmine",
                    ),
                    _Transition(target_turn_id="repair"),
                ),
            ),
            _Turn(
                turn_id="repair",
                content="Try once more.",
                metadata={"terminal": True},
            ),
            _Turn(
                turn_id="confirm",
                content="And then?",
                metadata={"terminal": True},
            ),
        ),
    )
    target = _FakeTarget(
        {
            "probe": "Jasmine tea comes first.",
            "repair": "unused",
            "confirm": "A ten-minute walk.",
        }
    )

    trajectory = run_scenario(scenario, target)

    assert trajectory.status == COMPLETED
    assert [sent[1] for sent in target.sent] == ["probe", "confirm"]
    assert [_value(turn, "turn_id") for turn in _turns(trajectory)] == [
        "probe",
        "confirm",
    ]


def test_transition_failure_retains_completed_response_and_projects_metadata():
    secret = "never-publish-api-key"
    scenario = _Scenario(
        scenario_id="transition-no-match",
        turns=(
            _Turn(
                turn_id="probe",
                content="What is the ritual?",
                transitions=(
                    _Transition(
                        target_turn_id=None,
                        condition="contains",
                        value="not present",
                    ),
                ),
            ),
        ),
    )
    target = _FakeTarget(
        {"probe": "Jasmine tea."},
        response_metadata={
            "provider": "fake-provider",
            "api_key": secret,
            "raw_body": secret,
        },
    )

    trajectory = run_scenario(scenario, target)

    assert trajectory.status == INFRA_ERROR
    assert trajectory.failure_code == "NO_SCENARIO_TRANSITION_MATCHED"
    assert _value(_turns(trajectory)[0], "response") == "Jasmine tea."
    serialized = str(_trajectory_dict(trajectory))
    assert "fake-provider" in serialized
    assert secret not in serialized


def test_clear_history_is_recorded_only_as_transcript_boundary():
    scenario = _Scenario(
        scenario_id="memory-transcript-boundary",
        turns=(
            _Turn(turn_id="teach", content="Remember jasmine tea."),
            _Turn(
                turn_id="recall",
                content="What should I make?",
                boundary_before=BOUNDARY_CLEAR_HISTORY,
            ),
        ),
    )
    target = _FakeTarget({"teach": "Okay.", "recall": "Jasmine tea."})

    trajectory = run_scenario(scenario, target)

    assert trajectory.status == COMPLETED
    payload = _trajectory_dict(trajectory)
    boundaries = payload.get("boundary_evidence") or [
        row
        for row in payload.get("turns", payload.get("turn_evidence", []))
        if row.get("boundary_requested") == BOUNDARY_CLEAR_HISTORY
    ]
    assert len(boundaries) == 1
    boundary = boundaries[0]
    assert boundary.get("boundary_kind", "transcript") == "transcript"
    assert boundary.get("runtime_session_rotated") is not True
    assert target.boundaries == [("default", BOUNDARY_CLEAR_HISTORY)]


def test_boundary_result_cannot_substitute_action_or_account():
    scenario = _Scenario(
        scenario_id="boundary-substitution",
        turns=(
            _Turn(turn_id="teach", content="Remember tea."),
            _Turn(
                turn_id="recall",
                content="What was it?",
                boundary_before=BOUNDARY_CLEAR_HISTORY,
            ),
        ),
    )
    action_target = _FakeTarget({"teach": "Okay.", "recall": "Tea."})

    def wrong_action(session: TargetSession, *, action: str) -> BoundaryResult:
        return BoundaryResult(
            session=replace(session, generation=session.generation + 1),
            action=BOUNDARY_NONE,
            boundary_kind="none",
            runtime_session_rotated=False,
        )

    action_target.apply_boundary = wrong_action  # type: ignore[method-assign]
    action_result = run_scenario(scenario, action_target)

    account_target = _FakeTarget({"teach": "Okay.", "recall": "Tea."})

    def wrong_account(session: TargetSession, *, action: str) -> BoundaryResult:
        return BoundaryResult(
            session=replace(
                session,
                generation=session.generation + 1,
                account_fingerprint="different-account",
            ),
            action=action,
            boundary_kind="transcript",
            runtime_session_rotated=False,
            evidence={"transcript_cleared": True},
        )

    account_target.apply_boundary = wrong_account  # type: ignore[method-assign]
    account_result = run_scenario(scenario, account_target)

    assert (action_result.status, action_result.failure_code) == (
        BLOCKED_EVIDENCE,
        "INVALID_BOUNDARY_EVIDENCE",
    )
    assert (account_result.status, account_result.failure_code) == (
        BLOCKED_EVIDENCE,
        "INVALID_BOUNDARY_EVIDENCE",
    )


def test_strong_persistent_memory_blocks_without_runtime_rotation_evidence():
    scenario = _Scenario(
        scenario_id="memory-strong",
        requirements=("persistent_memory_strong",),
        turns=(
            _Turn(turn_id="teach", content="Remember jasmine tea."),
            _Turn(
                turn_id="recall",
                content="What was it?",
                boundary_before=BOUNDARY_CLEAR_HISTORY,
            ),
        ),
    )
    target = _FakeTarget({"teach": "Okay.", "recall": "Jasmine tea."})

    trajectory = run_scenario(scenario, target)

    assert trajectory.status == BLOCKED_EVIDENCE
    assert trajectory.failure_code == "SESSION_BOUNDARY_UNPROVEN"
    assert target.contexts == []
    assert target.sent == []


def test_strong_persistent_memory_accepts_distinct_runtime_rotation_evidence():
    scenario = _Scenario(
        scenario_id="memory-strong",
        requirements=("persistent_memory_strong",),
        turns=(
            _Turn(turn_id="teach", content="Remember jasmine tea."),
            _Turn(
                turn_id="recall",
                content="What was it?",
                boundary_before=BOUNDARY_ROTATE_RUNTIME_SESSION,
            ),
        ),
    )
    target = _FakeTarget(
        {"teach": "Okay.", "recall": "Jasmine tea."},
        runtime_rotation=True,
    )

    trajectory = run_scenario(scenario, target)

    assert trajectory.status == COMPLETED
    assert target.boundaries == [("default", BOUNDARY_ROTATE_RUNTIME_SESSION)]
    serialized = str(_trajectory_dict(trajectory))
    assert "runtime-before" not in serialized
    assert "runtime-after" not in serialized
    assert "before_runtime_session_sha256" in serialized


def test_strong_memory_branch_cannot_skip_runtime_rotation_and_still_complete():
    scenario = _Scenario(
        scenario_id="memory-strong-branch",
        requirements=("persistent_memory_strong",),
        turns=(
            _Turn(
                turn_id="teach",
                content="Remember jasmine tea.",
                transitions=(_Transition(target_turn_id=None),),
            ),
            _Turn(
                turn_id="recall",
                content="What was it?",
                boundary_before=BOUNDARY_ROTATE_RUNTIME_SESSION,
            ),
        ),
    )
    target = _FakeTarget(
        {"teach": "Okay.", "recall": "Jasmine tea."},
        runtime_rotation=True,
    )

    trajectory = run_scenario(scenario, target)

    assert trajectory.status == BLOCKED_EVIDENCE
    assert trajectory.failure_code == "SESSION_BOUNDARY_UNPROVEN"
    assert [sent[1] for sent in target.sent] == ["teach"]


def test_other_session_rotation_cannot_back_strong_memory_probe():
    scenario = _Scenario(
        scenario_id="memory-cross-session-proof",
        requirements=("persistent_memory_strong",),
        turns=(
            _Turn(
                turn_id="alice-teach",
                content="Remember A.",
                session_key="alice",
            ),
            _Turn(
                turn_id="alice-probe",
                content="What was A?",
                session_key="alice",
                requirements=("persistent_memory_strong",),
            ),
            _Turn(
                turn_id="bob-teach",
                content="Remember B.",
                session_key="bob",
            ),
            _Turn(
                turn_id="bob-probe",
                content="What was B?",
                session_key="bob",
                boundary_before=BOUNDARY_ROTATE_RUNTIME_SESSION,
            ),
        ),
    )
    target = _FakeTarget(
        {
            "alice-teach": "Okay A.",
            "alice-probe": "A.",
            "bob-teach": "Okay B.",
            "bob-probe": "B.",
        },
        runtime_rotation=True,
    )

    trajectory = run_scenario(scenario, target)

    assert trajectory.status == BLOCKED_EVIDENCE
    assert trajectory.failure_code == "SESSION_BOUNDARY_UNPROVEN"


def test_target_error_and_unexpected_error_are_explicit_infra_trajectories():
    scenario = _Scenario(
        scenario_id="infra",
        turns=(_Turn(turn_id="probe", content="Hello"),),
    )
    classified = run_scenario(
        scenario,
        _FakeTarget({"probe": TargetError("TARGET_DOWN")}),
    )
    unexpected = run_scenario(
        scenario,
        _FakeTarget({"probe": RuntimeError("private upstream body")}),
    )

    assert (classified.status, classified.failure_code) == (
        INFRA_ERROR,
        "TARGET_DOWN",
    )
    assert (unexpected.status, unexpected.failure_code) == (
        INFRA_ERROR,
        "UNEXPECTED_RUNNER_ERROR",
    )
    assert "private upstream body" not in str(_trajectory_dict(unexpected))


def test_missing_response_correlation_fails_closed_as_a_trajectory():
    scenario = _Scenario(
        scenario_id="uncorrelated",
        turns=(_Turn(turn_id="probe", content="Hello"),),
    )
    target = _FakeTarget({"probe": "response text"})

    def uncorrelated_send(*_args, **_kwargs):
        return TargetResponse(text="response text")

    target.send = uncorrelated_send
    trajectory = run_scenario(scenario, target)

    assert trajectory.status == BLOCKED_EVIDENCE
    assert trajectory.failure_code == "RESPONSE_CORRELATION_MISSING"
    assert _turns(trajectory) == []


def test_cycles_fail_closed_at_max_turns_instead_of_hanging():
    scenario = _Scenario(
        scenario_id="cycle",
        turns=(
            _Turn(
                turn_id="loop",
                content="Again",
                transitions=(_Transition(target_turn_id="loop"),),
            ),
        ),
    )
    target = _FakeTarget({"loop": "Again"})

    trajectory = run_scenario(scenario, target, max_turns=3)

    assert trajectory.status == INFRA_ERROR
    assert trajectory.failure_code == "SCENARIO_MAX_TURNS_EXCEEDED"
    assert len(_turns(trajectory)) == 3


def test_multiple_session_keys_are_isolated_and_closed():
    scenario = _Scenario(
        scenario_id="isolation",
        turns=(
            _Turn(turn_id="alice", content="Remember A", session_key="alice"),
            _Turn(turn_id="bob", content="Remember B", session_key="bob"),
        ),
    )
    target = _FakeTarget({"alice": "A", "bob": "B"})

    trajectory = run_scenario(scenario, target)

    assert trajectory.status == COMPLETED
    assert [context.session_key for context in target.contexts] == ["alice", "bob"]
    assert [row[0] for row in target.sent] == ["alice", "bob"]
    assert len(target.closed) == 2


def test_repeats_run_concurrently_but_results_keep_scenario_repeat_order():
    scenarios = [
        _Scenario(
            scenario_id=name,
            turns=(_Turn(turn_id=f"{name}-turn", content="Hello"),),
        )
        for name in ("one", "two")
    ]
    target = _FakeTarget(
        {"one-turn": "one", "two-turn": "two"},
        delay_seconds=0.03,
    )

    results = RegressionRunner(target, max_concurrency=3).run(
        scenarios,
        repeats=3,
        experiment_id="experiment-fixed",
    )

    assert [(row.scenario_id, row.repeat_index) for row in results] == [
        ("one", 0),
        ("one", 1),
        ("one", 2),
        ("two", 0),
        ("two", 1),
        ("two", 2),
    ]
    assert {row.experiment_id for row in results} == {"experiment-fixed"}
    assert target.max_active >= 2
    assert len(target.closed) == 6


def test_close_failure_changes_completed_collection_to_infra_error():
    scenario = _Scenario(
        scenario_id="close",
        turns=(_Turn(turn_id="probe", content="Hello"),),
    )

    trajectory = run_scenario(
        scenario,
        _FakeTarget({"probe": "Hi"}, fail_close=True),
    )

    assert trajectory.status == INFRA_ERROR
    assert trajectory.failure_code == "SESSION_CLOSE_FAILED"


class _FeedlingClient:
    def __init__(self, _base_url: str) -> None:
        self.requests: list[tuple[str, str]] = []

    def send(
        self, _credentials: Any, prompt: str, *, read_timeout: float = 45
    ) -> dict[str, Any]:
        assert 0 < read_timeout <= 45
        self.requests.append(("send", prompt))
        return {"user_message": {"id": "user-1", "ts": 100.0}}

    def poll_reply_record(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"message": {"id": "assistant-1"}, "reply": "Mira reply"}

    def _req(self, method: str, path: str, **_kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.requests.append((method, path))
        return 200, {"cleared": True, "deleted": 2}


def _feedling_credentials(user_id: str = "user-1") -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        api_key="private-key",
        sk=b"s" * 32,
        pk=b"p" * 32,
    )


def test_feedling_adapter_uses_exact_reply_correlation_and_labels_history_clear():
    target = FeedlingTarget(
        target_id="candidate",
        base_url="https://test-api.feedling.app",
        session_factory=lambda _context: _feedling_credentials(),
        client_factory=_FeedlingClient,
        session_closer=lambda _client, _credentials: None,
    )
    session = target.open_session(
        TargetContext(
            run_id="run-1",
            scenario_id="scenario-1",
            repeat_index=0,
        )
    )

    response = target.send(
        session,
        turn_id="turn-1",
        prompt="Hello",
        timeout_seconds=1,
    )
    boundary = target.apply_boundary(session, action=BOUNDARY_CLEAR_HISTORY)

    assert response.text == "Mira reply"
    assert response.request_id == "user-1"
    assert response.response_id == "assistant-1"
    assert response.trace_id == ""
    assert response.metadata["reply_correlation"] == "exact_message_id"
    assert response.metadata["trace_correlation"] == "not_collected"
    assert boundary.boundary_kind == "transcript"
    assert boundary.runtime_session_rotated is False
    assert boundary.evidence["runtime_session_rotation_claimed"] is False


def test_feedling_adapter_rejects_unproven_runtime_rotation():
    target = FeedlingTarget(
        target_id="candidate",
        base_url="https://test-api.feedling.app",
        session_factory=lambda _context: _feedling_credentials(),
        client_factory=_FeedlingClient,
        runtime_session_rotator=lambda _client, _credentials: {
            "rotated": True,
            "before_runtime_session_id": "same",
            "after_runtime_session_id": "same",
        },
        session_closer=lambda _client, _credentials: None,
    )
    session = target.open_session(
        TargetContext(
            run_id="run-1",
            scenario_id="scenario-1",
            repeat_index=0,
        )
    )

    with pytest.raises(TargetError) as raised:
        target.apply_boundary(session, action=BOUNDARY_ROTATE_RUNTIME_SESSION)

    assert raised.value.status == BLOCKED_EVIDENCE
    assert raised.value.code == "SESSION_BOUNDARY_UNPROVEN"


def test_feedling_target_rejects_untrusted_origin_and_reused_account():
    with pytest.raises(ValueError, match="not explicitly allowed"):
        FeedlingTarget(
            target_id="candidate",
            base_url="https://credential-sink.example",
            session_factory=lambda _context: _feedling_credentials(),
            client_factory=_FeedlingClient,
            session_closer=lambda _client, _credentials: None,
        )

    target = FeedlingTarget(
        target_id="candidate",
        base_url="https://test-api.feedling.app",
        session_factory=lambda _context: _feedling_credentials("same-user"),
        client_factory=_FeedlingClient,
        session_closer=lambda _client, _credentials: None,
    )
    context = TargetContext(
        run_id="run-1",
        scenario_id="scenario-1",
        repeat_index=0,
    )
    first = target.open_session(context)
    target.close_session(first)

    with pytest.raises(TargetError) as raised:
        target.open_session(replace(context, run_id="run-2", repeat_index=1))

    assert raised.value.status == BLOCKED_EVIDENCE
    assert raised.value.code == "SESSION_ISOLATION_FAILED"


def test_feedling_target_runs_through_trajectory_contract_without_fake_trace():
    cleaned: list[str] = []
    target = FeedlingTarget(
        target_id="candidate",
        base_url="https://test-api.feedling.app",
        session_factory=lambda _context: _feedling_credentials("full-run-user"),
        client_factory=_FeedlingClient,
        session_closer=lambda _client, credentials: cleaned.append(credentials.user_id),
    )
    scenario = _Scenario(
        scenario_id="feedling-full-run",
        turns=(_Turn(turn_id="probe", content="Hello"),),
    )

    trajectory = run_scenario(scenario, target)

    assert trajectory.status == COMPLETED
    assert _value(_turns(trajectory)[0], "trace_id") == ""
    assert cleaned == ["full-run-user"]
