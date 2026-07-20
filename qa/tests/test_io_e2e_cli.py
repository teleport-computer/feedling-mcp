from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from tools.io_e2e import cli
from tools.io_e2e.artifacts import project_downloaded_results
from tools.io_e2e.contracts import IoE2EError, RunPlan, WORKFLOW_PATH, validate_ref
from tools.io_e2e.github import CommandResult, GitHubClient


SHA = "a" * 40
CONTROLLER_SHA = "b" * 40
REQUEST_ID = "12345678-1234-4123-8123-123456789abc"
REPOSITORY = "teleport-computer/feedling-mcp"


def _run_payload(
    *,
    run_id: int = 12345,
    status: str = "in_progress",
    conclusion: str | None = None,
    path: str = WORKFLOW_PATH,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "display_title": (
            f"IO E2E · {REQUEST_ID} · deployed_test · hosted_resident "
            "· persona x1"
        ),
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        "head_sha": CONTROLLER_SHA,
        "head_branch": "main",
        "created_at": "2026-07-20T12:00:00Z",
        "updated_at": "2026-07-20T12:01:00Z",
        "run_attempt": 1,
        "path": path,
        "event": "workflow_dispatch",
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
    }


def _populate_downloaded_artifacts(
    destination: Path,
    *,
    request_id: str = REQUEST_ID,
    second_manifest: bool = False,
) -> None:
    request_dir = destination / f"io-e2e-request-{request_id}"
    team_dir = destination / "io-e2e-team-report-api-key-e2e-12345-1"
    request_dir.mkdir(parents=True)
    team_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "request_id": request_id,
        "repository": REPOSITORY,
        "controller_sha": CONTROLLER_SHA,
        "target_ref": "test",
        "target_sha": SHA,
        "deployed_sha": "c" * 40,
        "lane": "deployed_test",
        "suite": "full",
        "runtime_target": "hosted_resident",
        "persona_repetitions": 1,
    }
    (request_dir / "request-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    if second_manifest:
        duplicate = destination / "duplicate-request"
        duplicate.mkdir()
        (duplicate / "request-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    redaction = {
        "synthetic_users_only": True,
        "credentials_omitted": True,
        "user_identifiers_omitted": True,
        "raw_correlation_identifiers_omitted": True,
        "raw_chat_omitted": True,
        "raw_persona_omitted": True,
        "raw_trace_omitted": True,
        "raw_reasoning_omitted": True,
    }
    failure_index = {
        "schema_version": 1,
        "kind": "io_e2e_team_failure_index",
        "run_id": "api-key-e2e-12345-1",
        "failure_count": 1,
        "api_key_failure_count": 1,
        "persona_memory_failure_count": 0,
        "exact_id_failure_count": 1,
        "failures": [{"sanitized": True}],
        "redaction": redaction,
    }
    (team_dir / "failure-index.json").write_text(
        json.dumps(failure_index), encoding="utf-8"
    )
    (team_dir / "team-summary.md").write_text(
        "# Team summary\n\n- Overall: `PRODUCT_FAIL`\n", encoding="utf-8"
    )
    (team_dir / "matrix.md").write_text(
        "# Matrix\n\n| Profile | Status |\n| --- | --- |\n| official-openai | PRODUCT_FAIL |\n",
        encoding="utf-8",
    )


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.run = _run_payload()
        self.watch_result = CommandResult(0, "", "")
        self.download_builder = _populate_downloaded_artifacts

    def infer_repository(self) -> str:
        self.calls.append(("infer_repository",))
        return REPOSITORY

    def require_write_permission(self, repository: str) -> dict[str, Any]:
        self.calls.append(("require_write_permission", repository))
        return {"default_branch": "main", "permissions": {"push": True}}

    def require_protected_trust_branches(self, repository: str) -> None:
        self.calls.append(("require_protected_trust_branches", repository))

    def require_scoped_qa_environments(self, repository: str) -> None:
        self.calls.append(("require_scoped_qa_environments", repository))

    def require_trusted_workflow(self, repository: str, default_branch: str) -> None:
        self.calls.append(("require_trusted_workflow", repository, default_branch))

    def resolve_commit(self, repository: str, target_ref: str) -> str:
        self.calls.append(("resolve_commit", repository, target_ref))
        return SHA

    def dispatch(self, plan: RunPlan) -> None:
        self.calls.append(("dispatch", plan))

    def await_dispatch(self, repository: str, request_id: str) -> dict[str, Any] | None:
        self.calls.append(("await_dispatch", repository, request_id))
        return self.run

    def resolve_run(self, repository: str, identifier: int | str) -> dict[str, Any]:
        self.calls.append(("resolve_run", repository, identifier))
        return self.run

    def watch(
        self, repository: str, run_id: int, interval_seconds: int
    ) -> CommandResult:
        self.calls.append(("watch", repository, run_id, interval_seconds))
        return self.watch_result

    def download(
        self,
        repository: str,
        run_id: int,
        run_attempt: int,
        request_id: str,
        destination: Path,
    ) -> None:
        self.calls.append(
            ("download", repository, run_id, run_attempt, request_id, destination)
        )
        self.download_builder(destination)

    def open(self, repository: str, run_id: int) -> None:
        self.calls.append(("open", repository, run_id))

    def cancel(self, repository: str, run_id: int) -> None:
        self.calls.append(("cancel", repository, run_id))


def _parse(*args: str):
    return cli.build_parser().parse_args(list(args))


def test_plan_resolves_target_but_keeps_controller_on_default_branch(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: REQUEST_ID)

    plan = cli.create_plan(
        _parse(
            "plan",
            "--ref",
            "test",
            "--runtime-target",
            "hosted_resident",
            "--persona-repetitions",
            "3",
        ),
        client,
    )

    assert plan.controller_ref == "main"
    assert plan.controller_workflow == WORKFLOW_PATH
    assert plan.target_ref == "test"
    assert plan.target_sha == SHA
    assert plan.workflow_inputs() == {
        "request_id": REQUEST_ID,
        "target_ref": "test",
        "target_sha": SHA,
        "lane": "deployed_test",
        "suite": "full",
        "persona_repetitions": "3",
        "runtime_target": "hosted_resident",
    }
    assert ("require_write_permission", REPOSITORY) in client.calls
    assert ("require_trusted_workflow", REPOSITORY, "main") in client.calls
    assert ("require_protected_trust_branches", REPOSITORY) in client.calls
    assert ("require_scoped_qa_environments", REPOSITORY) in client.calls


def test_human_plan_distinguishes_branch_head_from_live_deployed_image(
    monkeypatch, capsys
):
    client = FakeClient()
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: REQUEST_ID)

    exit_code = cli.main(["plan", "--ref", "test"], client=client)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert f"Requested branch head: {REPOSITORY}@test ({SHA})" in output
    assert "Live backend image: resolved and bound separately" in output


def test_deployed_lane_refuses_to_claim_it_tests_an_arbitrary_branch():
    client = FakeClient()
    with pytest.raises(IoE2EError) as exc_info:
        cli.create_plan(_parse("plan", "--ref", "feat/runtime-change"), client)

    assert exc_info.value.code == "NOT_IMPLEMENTED"
    assert exc_info.value.details["required_lane"] == "branch_preview"
    assert client.calls == []


@pytest.mark.parametrize(
    "repository",
    [
        "attacker/feedling-mcp",
        "teleport-computer/feedling-mcp-fork",
        "Teleport-Computer/feedling-mcp",
    ],
)
def test_every_command_rejects_noncanonical_repositories_before_github(
    repository: str,
):
    client = FakeClient()
    with pytest.raises(IoE2EError) as exc_info:
        cli.create_plan(_parse("plan", "--ref", "test", "--repo", repository), client)
    assert exc_info.value.code == "UNTRUSTED_REPOSITORY"
    assert client.calls == []


def test_inferred_fork_repository_is_rejected_before_permission_lookup():
    client = FakeClient()

    def infer_fork() -> str:
        client.calls.append(("infer_repository",))
        return "sxysun/feedling-mcp"

    client.infer_repository = infer_fork
    with pytest.raises(IoE2EError) as exc_info:
        cli.execute(_parse("status", "12345"), client=client)
    assert exc_info.value.code == "UNTRUSTED_REPOSITORY"
    assert client.calls == [("infer_repository",)]


def test_branch_preview_is_explicitly_not_implemented():
    with pytest.raises(IoE2EError) as exc_info:
        cli.create_plan(
            _parse("plan", "--ref", "feat/runtime-change", "--lane", "branch_preview"),
            FakeClient(),
        )
    assert exc_info.value.code == "NOT_IMPLEMENTED"
    assert exc_info.value.details["requested_lane"] == "branch_preview"


def test_request_identifier_requires_the_same_uuid_v4_contract_as_controller():
    with pytest.raises(IoE2EError, match="UUIDv4"):
        RunPlan(
            request_id="12345678-1234-1123-8123-123456789abc",
            repository=REPOSITORY,
            controller_ref="main",
            controller_workflow=WORKFLOW_PATH,
            target_ref="test",
            target_sha=SHA,
            lane="deployed_test",
            suite="full",
            persona_repetitions=1,
            runtime_target="hosted_resident",
        )


@pytest.mark.parametrize(
    "target_ref",
    [
        "-evil",
        "../main",
        "heads//main",
        "refs/heads/main.lock",
        "main@{1}",
        "main\nnext",
    ],
)
def test_ref_contract_rejects_unsafe_or_ambiguous_refs(target_ref: str):
    with pytest.raises(IoE2EError, match="invalid format"):
        validate_ref(target_ref)


def test_asserted_sha_must_still_match_resolved_test_head():
    with pytest.raises(IoE2EError) as exc_info:
        cli.create_plan(
            _parse("plan", "--ref", "test", "--sha", "c" * 40), FakeClient()
        )
    assert exc_info.value.code == "TARGET_SHA_MISMATCH"
    assert exc_info.value.details == {"asserted_sha": "c" * 40, "resolved_sha": SHA}


def test_run_dispatches_the_plan_and_returns_the_correlated_run(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: REQUEST_ID)
    payload, exit_code = cli.execute(
        _parse("run", "--ref", "test", "--json"), client=client
    )

    assert exit_code == 0
    assert payload["request_id"] == REQUEST_ID
    assert payload["target_sha"] == SHA
    assert payload["run"]["run_id"] == 12345
    assert payload["wait_requested"] is False
    dispatched = next(call[1] for call in client.calls if call[0] == "dispatch")
    assert dispatched.controller_ref == "main"
    assert dispatched.target_sha == SHA


def test_dispatch_success_is_not_retried_when_github_has_not_indexed_the_run(
    monkeypatch,
):
    client = FakeClient()
    client.await_dispatch = lambda repository, request_id: None
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: REQUEST_ID)

    payload, exit_code = cli.execute(_parse("run", "--ref", "test"), client=client)

    assert exit_code == 0
    assert payload["run"] is None
    assert payload["request_id"] == REQUEST_ID


def test_run_wait_index_timeout_is_nonzero_and_never_redispatches(monkeypatch, capsys):
    client = FakeClient()
    client.await_dispatch = lambda repository, request_id: None
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: REQUEST_ID)

    exit_code = cli.main(["run", "--ref", "test", "--wait", "--json"], client=client)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 6
    assert payload["error"]["code"] == "RUN_INDEX_TIMEOUT"
    assert payload["error"]["details"] == {
        "dispatch_accepted": True,
        "request_id": REQUEST_ID,
        "next_command": (
            f"python3 -m tools.io_e2e status {REQUEST_ID} --repo {REPOSITORY}"
        ),
    }
    assert sum(call[0] == "dispatch" for call in client.calls) == 1


def test_run_wait_index_timeout_renders_request_and_safe_next_step(monkeypatch, capsys):
    client = FakeClient()
    client.await_dispatch = lambda repository, request_id: None
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: REQUEST_ID)

    exit_code = cli.main(["run", "--ref", "test", "--wait"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 6
    assert captured.out == ""
    assert "RUN_INDEX_TIMEOUT" in captured.err
    assert f"Request: {REQUEST_ID}" in captured.err
    assert (
        f"Next: python3 -m tools.io_e2e status {REQUEST_ID} --repo {REPOSITORY}"
        in captured.err
    )
    assert sum(call[0] == "dispatch" for call in client.calls) == 1


def test_json_mode_wraps_argument_errors_in_stable_error_document(capsys):
    exit_code = cli.main(["run", "--json"], client=FakeClient())

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload == {
        "error": {
            "code": "INVALID_ARGUMENTS",
            "message": "the following arguments are required: --ref",
        },
        "ok": False,
        "schema_version": "io-e2e-control.v1",
    }


def test_run_waits_for_terminal_status_without_dispatching_twice(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: REQUEST_ID)
    completed = _run_payload(status="completed", conclusion="success")
    resolve_count = 0

    def resolve_run(repository: str, identifier: int | str) -> dict[str, Any]:
        nonlocal resolve_count
        resolve_count += 1
        return completed

    client.resolve_run = resolve_run
    payload, exit_code = cli.execute(
        _parse("run", "--ref", "test", "--wait", "--interval", "3"),
        client=client,
    )

    assert exit_code == 0
    assert payload["wait_requested"] is True
    assert payload["run"]["status"] == "completed"
    assert resolve_count == 1
    assert sum(call[0] == "dispatch" for call in client.calls) == 1
    assert ("watch", REPOSITORY, 12345, 3) in client.calls


def test_status_accepts_request_uuid_and_checks_write_access():
    client = FakeClient()
    payload, exit_code = cli.execute(
        _parse("status", REQUEST_ID, "--repo", REPOSITORY), client=client
    )

    assert exit_code == 0
    assert payload["run"]["run_id"] == 12345
    assert ("require_write_permission", REPOSITORY) in client.calls
    assert ("resolve_run", REPOSITORY, REQUEST_ID) in client.calls


def test_watch_preserves_failed_run_as_machine_readable_nonzero_result():
    client = FakeClient()
    client.watch_result = CommandResult(1, "", "qualification failed")
    client.run = _run_payload(status="completed", conclusion="failure")

    payload, exit_code = cli.execute(
        _parse("watch", "12345", "--interval", "3", "--json"), client=client
    )

    assert exit_code == 5
    assert payload["run"]["status"] == "completed"
    assert payload["run"]["conclusion"] == "failure"


def test_results_requires_completion_and_a_fresh_destination(tmp_path: Path):
    client = FakeClient()
    destination = tmp_path / "artifacts"
    with pytest.raises(IoE2EError) as exc_info:
        cli.execute(
            _parse("results", "12345", "--dir", str(destination)), client=client
        )
    assert exc_info.value.code == "RUN_NOT_COMPLETE"

    client.run = _run_payload(status="completed", conclusion="failure")
    payload, exit_code = cli.execute(
        _parse("results", "12345", "--dir", str(destination)), client=client
    )
    assert exit_code == 0
    assert destination.is_dir()
    assert payload["directory"] == str(destination.resolve())
    assert payload["result"]["request"]["request_id"] == REQUEST_ID
    assert payload["result"]["request"]["deployed_sha"] == "c" * 40
    assert payload["result"]["failure_counts"]["failure_count"] == 1
    assert payload["result"]["team_summary_markdown"].startswith("# Team summary")
    assert payload["result"]["matrix_markdown"].startswith("# Matrix")
    assert all(Path(path).is_file() for path in payload["result"]["artifacts"].values())
    assert (
        "download",
        REPOSITORY,
        12345,
        1,
        REQUEST_ID,
        destination,
    ) in client.calls

    with pytest.raises(IoE2EError) as exc_info:
        cli.execute(
            _parse("results", "12345", "--dir", str(destination)), client=client
        )
    assert exc_info.value.code == "RESULTS_DIRECTORY_EXISTS"


def test_results_refuses_a_broken_symlink_destination(tmp_path: Path):
    client = FakeClient()
    client.run = _run_payload(status="completed", conclusion="success")
    destination = tmp_path / "artifacts"
    destination.symlink_to(tmp_path / "missing")

    with pytest.raises(IoE2EError) as exc_info:
        cli.execute(
            _parse("results", "12345", "--dir", str(destination)), client=client
        )
    assert exc_info.value.code == "RESULTS_DIRECTORY_EXISTS"


def test_results_fails_closed_on_missing_or_ambiguous_request_manifest(tmp_path: Path):
    client = FakeClient()
    client.run = _run_payload(status="completed", conclusion="success")

    def without_manifest(destination: Path) -> None:
        _populate_downloaded_artifacts(destination)
        next(destination.rglob("request-manifest.json")).unlink()

    client.download_builder = without_manifest
    with pytest.raises(IoE2EError) as missing:
        missing_destination = tmp_path / "missing"
        cli.execute(
            _parse("results", "12345", "--dir", str(missing_destination)),
            client=client,
        )
    assert missing.value.code == "REQUEST_MANIFEST_MISSING"
    assert not missing_destination.exists()

    client.download_builder = lambda destination: _populate_downloaded_artifacts(
        destination, second_manifest=True
    )
    with pytest.raises(IoE2EError) as ambiguous:
        ambiguous_destination = tmp_path / "ambiguous"
        cli.execute(
            _parse("results", "12345", "--dir", str(ambiguous_destination)),
            client=client,
        )
    assert ambiguous.value.code == "REQUEST_MANIFEST_AMBIGUOUS"
    assert not ambiguous_destination.exists()


def test_results_never_follows_an_artifact_symlink(tmp_path: Path):
    client = FakeClient()
    client.run = _run_payload(status="completed", conclusion="success")
    outside = tmp_path / "outside.md"
    outside.write_text("private outside content", encoding="utf-8")

    def with_symlink(destination: Path) -> None:
        _populate_downloaded_artifacts(destination)
        summary = next(destination.rglob("team-summary.md"))
        summary.unlink()
        summary.symlink_to(outside)

    client.download_builder = with_symlink
    with pytest.raises(IoE2EError) as exc_info:
        cli.execute(
            _parse("results", "12345", "--dir", str(tmp_path / "symlink")),
            client=client,
        )
    assert exc_info.value.code == "UNSAFE_RESULT_ARTIFACTS"


def test_results_rejects_a_team_report_from_another_run_attempt(tmp_path: Path):
    client = FakeClient()
    client.run = _run_payload(status="completed", conclusion="success")

    def wrong_run(destination: Path) -> None:
        _populate_downloaded_artifacts(destination)
        index = next(destination.rglob("failure-index.json"))
        value = json.loads(index.read_text(encoding="utf-8"))
        value["run_id"] = "api-key-e2e-99999-1"
        index.write_text(json.dumps(value), encoding="utf-8")

    client.download_builder = wrong_run
    with pytest.raises(IoE2EError) as exc_info:
        cli.execute(
            _parse("results", "12345", "--dir", str(tmp_path / "wrong-run")),
            client=client,
        )
    assert exc_info.value.code == "TEAM_REPORT_RUN_MISMATCH"


def test_results_rejects_a_request_manifest_from_another_controller(tmp_path: Path):
    client = FakeClient()
    client.run = _run_payload(status="completed", conclusion="success")

    def wrong_controller(destination: Path) -> None:
        _populate_downloaded_artifacts(destination)
        manifest = next(destination.rglob("request-manifest.json"))
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["controller_sha"] = "d" * 40
        manifest.write_text(json.dumps(value), encoding="utf-8")

    client.download_builder = wrong_controller
    with pytest.raises(IoE2EError) as exc_info:
        cli.execute(
            _parse("results", "12345", "--dir", str(tmp_path / "wrong-controller")),
            client=client,
        )
    assert exc_info.value.code == "REQUEST_MANIFEST_RUN_MISMATCH"


def test_results_rejects_unbounded_team_markdown(tmp_path: Path):
    client = FakeClient()
    client.run = _run_payload(status="completed", conclusion="success")

    def oversized(destination: Path) -> None:
        _populate_downloaded_artifacts(destination)
        summary = next(destination.rglob("team-summary.md"))
        summary.write_bytes(b"x" * (512 * 1024 + 1))

    client.download_builder = oversized
    with pytest.raises(IoE2EError) as exc_info:
        cli.execute(
            _parse("results", "12345", "--dir", str(tmp_path / "oversized")),
            client=client,
        )
    assert exc_info.value.code == "UNSAFE_RESULT_ARTIFACTS"


def test_results_rejects_terminal_control_bytes_in_human_markdown(tmp_path: Path):
    client = FakeClient()
    client.run = _run_payload(status="completed", conclusion="success")

    def terminal_escape(destination: Path) -> None:
        _populate_downloaded_artifacts(destination)
        summary = next(destination.rglob("team-summary.md"))
        summary.write_text("# Team summary\n\x1b[2J", encoding="utf-8")

    client.download_builder = terminal_escape
    with pytest.raises(IoE2EError) as exc_info:
        cli.execute(
            _parse("results", "12345", "--dir", str(tmp_path / "escape")),
            client=client,
        )
    assert exc_info.value.code == "INVALID_TEAM_REPORT"


def test_human_results_render_the_sanitized_summary_and_matrix(tmp_path: Path, capsys):
    client = FakeClient()
    client.run = _run_payload(status="completed", conclusion="failure")
    payload, _ = cli.execute(
        _parse("results", "12345", "--dir", str(tmp_path / "human")),
        client=client,
    )

    cli._emit(payload, as_json=False)
    output = capsys.readouterr().out
    assert "Downloaded and verified run 12345" in output
    assert "Failures/evidence gaps: 1" in output
    assert "--- team-summary.md ---" in output
    assert "Overall: `PRODUCT_FAIL`" in output
    assert "--- matrix.md ---" in output
    assert "official-openai | PRODUCT_FAIL" in output


def test_json_open_returns_url_without_opening_a_browser():
    client = FakeClient()
    payload, exit_code = cli.execute(_parse("open", "12345", "--json"), client=client)

    assert exit_code == 0
    assert payload["opened"] is False
    assert all(call[0] != "open" for call in client.calls)


def test_cancel_refuses_completed_run():
    client = FakeClient()
    client.run = _run_payload(status="completed", conclusion="success")
    with pytest.raises(IoE2EError) as exc_info:
        cli.execute(_parse("cancel", "12345"), client=client)
    assert exc_info.value.code == "RUN_ALREADY_COMPLETE"


def test_main_emits_stable_json_error_for_unsupported_preview(capsys):
    exit_code = cli.main(
        [
            "plan",
            "--ref",
            "feat/change",
            "--lane",
            "branch_preview",
            "--json",
        ],
        client=FakeClient(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NOT_IMPLEMENTED"


def test_github_dispatch_uses_only_fixed_workflow_default_ref_and_public_inputs():
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str]) -> CommandResult:
        commands.append(tuple(command))
        return CommandResult(0, "", "")

    client = GitHubClient(runner=runner)
    plan = RunPlan(
        request_id=REQUEST_ID,
        repository=REPOSITORY,
        controller_ref="main",
        controller_workflow=WORKFLOW_PATH,
        target_ref="test",
        target_sha=SHA,
        lane="deployed_test",
        suite="full",
        persona_repetitions=1,
        runtime_target="deployed_current",
    )
    client.dispatch(plan)

    assert commands == [
        (
            "gh",
            "workflow",
            "run",
            "io-e2e-control.yml",
            "--repo",
            REPOSITORY,
            "--ref",
            "main",
            "-f",
            f"request_id={REQUEST_ID}",
            "-f",
            "target_ref=test",
            "-f",
            f"target_sha={SHA}",
            "-f",
            "lane=deployed_test",
            "-f",
            "suite=full",
            "-f",
            "persona_repetitions=1",
            "-f",
            "runtime_target=deployed_current",
        )
    ]
    flattened = " ".join(commands[0]).lower()
    assert "token" not in flattened
    assert "secret" not in flattened
    assert "api_key" not in flattened


def test_github_results_download_excludes_protected_debug_artifact(tmp_path: Path):
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str]) -> CommandResult:
        commands.append(tuple(command))
        return CommandResult(0, "", "")

    GitHubClient(runner=runner).download(
        REPOSITORY, 12345, 1, REQUEST_ID, tmp_path
    )

    assert [command[command.index("--name") + 1] for command in commands] == [
        f"io-e2e-request-{REQUEST_ID}-12345-1",
        "io-e2e-team-report-api-key-e2e-12345-1",
    ]
    assert all("protected-debug" not in " ".join(command) for command in commands)


def test_missing_default_branch_workflow_has_stable_unavailable_error():
    def runner(command: Sequence[str]) -> CommandResult:
        assert command == (
            "gh",
            "api",
            f"repos/{REPOSITORY}/actions/workflows/io-e2e-control.yml",
        )
        return CommandResult(1, "", "gh: Not Found (HTTP 404)")

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(runner=runner).require_trusted_workflow(REPOSITORY, "main")

    assert exc_info.value.code == "TRUSTED_WORKFLOW_UNAVAILABLE"
    assert "main" in exc_info.value.message


def test_workflow_lookup_preserves_non_404_github_failure():
    def runner(command: Sequence[str]) -> CommandResult:
        return CommandResult(1, "", "gh: Resource not accessible (HTTP 403)")

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(runner=runner).require_trusted_workflow(REPOSITORY, "main")

    assert exc_info.value.code == "GH_COMMAND_FAILED"
    assert exc_info.value.details == {"exit_code": 1, "http_status": 403}


def _strict_effective_rules() -> list[dict[str, Any]]:
    return [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 1,
                "dismiss_stale_reviews_on_push": True,
                "require_last_push_approval": True,
                "required_review_thread_resolution": True,
            },
        },
    ]


def test_github_trust_branch_check_rejects_unprotected_main():
    def runner(command: Sequence[str]) -> CommandResult:
        assert command == ("gh", "api", f"repos/{REPOSITORY}/branches/main")
        return CommandResult(
            0,
            json.dumps({"name": "main", "protected": False}),
            "",
        )

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(runner=runner).require_protected_trust_branches(REPOSITORY)

    assert exc_info.value.code == "UNPROTECTED_TRUST_BRANCH"
    assert exc_info.value.details == {"branch": "main"}
    assert exc_info.value.exit_code == 3


def test_github_trust_branch_check_requires_both_main_and_test():
    responses = iter(
        [
            {"name": "main", "protected": True},
            _strict_effective_rules(),
            {"name": "test", "protected": True},
            _strict_effective_rules(),
        ]
    )

    def runner(command: Sequence[str]) -> CommandResult:
        return CommandResult(0, json.dumps(next(responses)), "")

    GitHubClient(runner=runner).require_protected_trust_branches(REPOSITORY)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda rules: [row for row in rules if row["type"] != "deletion"],
        lambda rules: [row for row in rules if row["type"] != "non_fast_forward"],
        lambda rules: [
            {
                **row,
                "parameters": {
                    **row["parameters"],
                    "required_approving_review_count": 0,
                },
            }
            if row["type"] == "pull_request"
            else row
            for row in rules
        ],
        lambda rules: [
            {
                **row,
                "parameters": {
                    **row["parameters"],
                    "dismiss_stale_reviews_on_push": False,
                },
            }
            if row["type"] == "pull_request"
            else row
            for row in rules
        ],
    ],
)
def test_github_trust_branch_check_rejects_weak_effective_rules(mutator):
    responses = iter(
        [
            {"name": "main", "protected": True},
            mutator(_strict_effective_rules()),
        ]
    )

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(
            runner=lambda command: CommandResult(
                0, json.dumps(next(responses)), ""
            )
        ).require_protected_trust_branches(REPOSITORY)

    assert exc_info.value.code == "INSUFFICIENT_TRUST_RULES"
    assert exc_info.value.details == {"branch": "main"}


def _scoped_environment(name: str, branch: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "name": name,
            "protection_rules": [
                {
                    "id": 42,
                    "node_id": "ENP_realistic_fixture",
                    "type": "branch_policy",
                }
            ],
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            },
        },
        {
            "total_count": 1,
            "branch_policies": [{"id": 1, "name": branch, "type": "branch"}],
        },
    )


def test_github_environment_check_requires_exact_main_and_test_scopes():
    responses = iter(
        [
            *_scoped_environment("io-e2e-agent-driven-test", "main"),
            *_scoped_environment("io-test-deploy", "test"),
        ]
    )

    GitHubClient(
        runner=lambda command: CommandResult(0, json.dumps(next(responses)), "")
    ).require_scoped_qa_environments(REPOSITORY)


@pytest.mark.parametrize(
    ("metadata", "policies"),
    [
        (
            {
                "name": "io-e2e-agent-driven-test",
                "protection_rules": [],
                "deployment_branch_policy": None,
            },
            {"total_count": 0, "branch_policies": []},
        ),
        (
            {
                "name": "io-e2e-agent-driven-test",
                "protection_rules": [{"type": "required_reviewers"}],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            },
            {
                "total_count": 1,
                "branch_policies": [{"name": "main", "type": "branch"}],
            },
        ),
        (
            {
                "name": "io-e2e-agent-driven-test",
                "protection_rules": [
                    {"type": "branch_policy"},
                    {"type": "wait_timer", "wait_timer": 5},
                ],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            },
            {
                "total_count": 1,
                "branch_policies": [{"name": "main", "type": "branch"}],
            },
        ),
        (
            {
                "name": "io-e2e-agent-driven-test",
                "protection_rules": [{"type": "branch_policy"}],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            },
            {
                "total_count": 1,
                "branch_policies": [{"name": "test", "type": "branch"}],
            },
        ),
    ],
)
def test_github_environment_check_rejects_unscoped_or_review_gated_environment(
    metadata, policies
):
    responses = iter([metadata, policies])

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(
            runner=lambda command: CommandResult(
                0, json.dumps(next(responses)), ""
            )
        ).require_scoped_qa_environments(REPOSITORY)

    assert exc_info.value.code == "UNSCOPED_QA_ENVIRONMENT"
    assert exc_info.value.details == {
        "environment": "io-e2e-agent-driven-test",
        "branch": "main",
    }


def test_missing_qa_environment_has_stable_scoping_error():
    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(
            runner=lambda command: CommandResult(
                1, "", "gh: Not Found (HTTP 404)"
            )
        ).require_scoped_qa_environments(REPOSITORY)

    assert exc_info.value.code == "UNSCOPED_QA_ENVIRONMENT"
    assert exc_info.value.details == {
        "environment": "io-e2e-agent-driven-test",
        "branch": "main",
    }


def test_github_permission_check_fails_closed_without_push_permission():
    def runner(command: Sequence[str]) -> CommandResult:
        assert command == ("gh", "api", f"repos/{REPOSITORY}")
        return CommandResult(
            0,
            json.dumps({"default_branch": "main", "permissions": {"push": False}}),
            "",
        )

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(runner=runner).require_write_permission(REPOSITORY)
    assert exc_info.value.code == "WRITE_PERMISSION_REQUIRED"
    assert exc_info.value.exit_code == 3


def test_github_run_resolution_rejects_runs_from_another_workflow():
    def runner(command: Sequence[str]) -> CommandResult:
        return CommandResult(
            0, json.dumps(_run_payload(path=".github/workflows/ci.yml")), ""
        )

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(runner=runner).resolve_run(REPOSITORY, 12345)
    assert exc_info.value.code == "UNTRUSTED_RUN"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_branch", "feat/collision"),
        ("event", "pull_request"),
        ("event", "push"),
        ("head_sha", "A" * 40),
        ("head_sha", "a" * 39),
    ],
)
def test_github_run_resolution_rejects_untrusted_controller_provenance(
    field: str, value: str
):
    run = _run_payload()
    run[field] = value

    def runner(command: Sequence[str]) -> CommandResult:
        return CommandResult(0, json.dumps(run), "")

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(runner=runner).resolve_run(REPOSITORY, 12345)
    assert exc_info.value.code == "UNTRUSTED_RUN"


@pytest.mark.parametrize("repository_field", ["repository", "head_repository"])
def test_github_run_resolution_rejects_fork_repository_payload(
    repository_field: str,
):
    run = _run_payload()
    run[repository_field] = {"full_name": "attacker/feedling-mcp"}

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(
            runner=lambda command: CommandResult(0, json.dumps(run), "")
        ).resolve_run(REPOSITORY, 12345)
    assert exc_info.value.code == "UNTRUSTED_RUN"


@pytest.mark.parametrize("repository_field", ["repository", "head_repository"])
def test_github_run_resolution_requires_canonical_repository_payload(
    repository_field: str,
):
    run = _run_payload()
    del run[repository_field]

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(
            runner=lambda command: CommandResult(0, json.dumps(run), "")
        ).resolve_run(REPOSITORY, 12345)
    assert exc_info.value.code == "UNTRUSTED_RUN"


@pytest.mark.parametrize(
    "title",
    [
        f"prefix {REQUEST_ID} suffix",
        f"IO E2E · {REQUEST_ID}",
        f"IO E2E · {REQUEST_ID} · deployed_test · hosted_resident · persona x1 extra",
    ],
)
def test_github_run_resolution_requires_exact_structured_title(title: str):
    run = _run_payload()
    run["display_title"] = title

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(
            runner=lambda command: CommandResult(0, json.dumps(run), "")
        ).resolve_run(REPOSITORY, 12345)
    assert exc_info.value.code == "UNTRUSTED_RUN"


def test_request_correlation_filters_untrusted_uuid_collision_first():
    malicious = _run_payload(run_id=99999)
    malicious.update(
        head_branch="feat/collision",
        created_at="2026-07-20T12:02:00Z",
    )
    trusted = _run_payload(run_id=12345)
    trusted["created_at"] = "2026-07-20T12:00:00Z"

    def runner(command: Sequence[str]) -> CommandResult:
        return CommandResult(0, json.dumps({"workflow_runs": [malicious, trusted]}), "")

    found = GitHubClient(runner=runner).find_request(REPOSITORY, REQUEST_ID)
    assert found is not None
    assert found["id"] == 12345


def test_request_correlation_fails_closed_on_two_trusted_uuid_claimants():
    first = _run_payload(run_id=12345)
    second = _run_payload(run_id=12346)

    def runner(command: Sequence[str]) -> CommandResult:
        return CommandResult(0, json.dumps({"workflow_runs": [first, second]}), "")

    with pytest.raises(IoE2EError) as exc_info:
        GitHubClient(runner=runner).find_request(REPOSITORY, REQUEST_ID)
    assert exc_info.value.code == "RUN_REQUEST_AMBIGUOUS"


def test_artifact_projection_requires_inherited_run_provenance(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    projection = {
        "run_id": 12345,
        "run_attempt": 1,
        "request_id": REQUEST_ID,
        "request_title": (
            f"IO E2E · {REQUEST_ID} · deployed_test · hosted_resident "
            "· persona x1"
        ),
        "controller_sha": CONTROLLER_SHA,
        "controller_branch": "feat/collision",
        "event": "workflow_dispatch",
        "workflow_path": WORKFLOW_PATH,
        "repository": REPOSITORY,
    }
    with pytest.raises(IoE2EError) as exc_info:
        project_downloaded_results(root, repository=REPOSITORY, run=projection)
    assert exc_info.value.code == "UNTRUSTED_RUN"


def test_artifact_projection_rejects_noncanonical_controller_sha(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    projection = {
        "run_id": 12345,
        "run_attempt": 1,
        "request_id": REQUEST_ID,
        "request_title": (
            f"IO E2E · {REQUEST_ID} · deployed_test · hosted_resident "
            "· persona x1"
        ),
        "controller_sha": CONTROLLER_SHA.upper(),
        "controller_branch": "main",
        "event": "workflow_dispatch",
        "workflow_path": WORKFLOW_PATH,
        "repository": REPOSITORY,
    }
    with pytest.raises(IoE2EError) as exc_info:
        project_downloaded_results(root, repository=REPOSITORY, run=projection)
    assert exc_info.value.code == "UNTRUSTED_RUN"


def test_github_watch_polls_without_a_global_five_minute_command_timeout(monkeypatch):
    client = GitHubClient(runner=lambda command: CommandResult(0, "", ""))
    runs = iter(
        [
            _run_payload(status="queued"),
            _run_payload(status="in_progress"),
            _run_payload(status="completed", conclusion="failure"),
        ]
    )
    sleeps: list[int] = []
    monkeypatch.setattr(client, "resolve_run", lambda repository, run_id: next(runs))
    monkeypatch.setattr("tools.io_e2e.github.time.sleep", sleeps.append)

    result = client.watch(REPOSITORY, 12345, 17)

    assert result.returncode == 1
    assert sleeps == [17, 17]
