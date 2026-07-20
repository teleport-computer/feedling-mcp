from __future__ import annotations

import io
import json
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from qa import provision_profiles as provisioner
from tools.provider_smoke.client import Session, SmokeError


VALID_MODELS = {
    "official-deepseek": "deepseek-v4-flash",
    "official-anthropic": "claude-sonnet-4-5",
    "official-openai": "gpt-5.4",
    "official-gemini": "gemini-2.5-flash",
    "openrouter-claude": "anthropic/claude-sonnet-4.5",
    "openrouter-openai": "openai/gpt-4.1-mini",
    "openrouter-glm": "z-ai/glm-4.5-air:free",
    "openrouter-kimi": "moonshotai/kimi-k3",
    "relay-kongbeiqie": "[特价纯血]claude-opus-4-6",
}


PROFILE_ROWS = [
    {
        "profile_id": profile_id,
        "provider": spec.provider,
        "route_family": spec.route_family,
        "model_family": spec.model_family,
        "credential_slot": spec.credential_env,
        "model_env": spec.model_env,
        "allowed_model_regex": spec.allowed_model_regex,
        **(
            {
                "base_url_env": spec.base_url_env,
                "allowed_base_url": spec.allowed_base_url,
            }
            if spec.base_url_env
            else {}
        ),
        "reasoning_expected": True,
        "reasoning_effort": provisioner.EXPECTED_REASONING_EFFORT,
    }
    for profile_id, spec in provisioner.PROFILE_SPECS.items()
]


def _synthetic_lease(index: int) -> dict[str, object]:
    return {
        "registered": True,
        "lease_id": f"lease_{index:032x}",
        "absence_token": f"{index:064x}",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "expires_at_epoch": 4_070_908_800,
        "ttl_seconds": provisioner.MAX_SYNTHETIC_TTL_SECONDS,
    }


def _cleanup_run_receipt(
    run_id: str, *, remaining: int = 0, failures: int = 0
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": provisioner.SYNTHETIC_CLEANUP_RUN_KIND,
        "run_id_sha256": provisioner.hashlib.sha256(run_id.encode()).hexdigest(),
        "label_prefix_sha256": provisioner.hashlib.sha256(
            f"{provisioner.SYNTHETIC_LABEL_PREFIX}{run_id}-".encode()
        ).hexdigest(),
        "database_authoritative": True,
        "matched_count": max(remaining, failures),
        "deleted_count": 0,
        "already_absent_count": 0,
        "operation_failure_count": failures,
        "remaining_count": remaining,
        "complete": remaining == 0 and failures == 0,
    }


def _write_coverage(tmp_path: Path, rows=None) -> Path:
    path = tmp_path / "coverage-lock.json"
    path.write_text(json.dumps({"profiles": PROFILE_ROWS if rows is None else rows}))
    return path


def _env() -> dict[str, str]:
    env = {
        "IO_E2E_BASE_URL": provisioner.ALLOWED_BASE_URL,
        "IO_E2E_ADMIN_TOKEN": "admin-sensitive-value",
        "QA_DEEPSEEK_API_KEY": "deepseek-sensitive-value",
        "QA_ANTHROPIC_API_KEY": "anthropic-sensitive-value",
        "QA_OPENAI_PROVIDER_API_KEY": "openai-sensitive-value",
        "QA_GEMINI_API_KEY": "gemini-sensitive-value",
        "QA_OPENROUTER_API_KEY": "openrouter-sensitive-value",
        "QA_KONGBEIQIE_API_KEY": "kongbeiqie-sensitive-value",
        "QA_KONGBEIQIE_BASE_URL": provisioner.ALLOWED_KONGBEIQIE_BASE_URL,
        "QA_RUN_ID": "unit/42",
    }
    for profile_id, spec in provisioner.PROFILE_SPECS.items():
        env[spec.model_env] = VALID_MODELS[profile_id]
    return env


class FakeSmokeClient:
    def __init__(self):
        self.registered: list[tuple[str, Session]] = []
        self.setup_calls: list[tuple[str, str, str, str, str, str | None]] = []
        self.trace_calls: list[str] = []
        self.runtime_calls: list[str] = []
        self.reset_calls: list[tuple[str, dict]] = []
        self.accept_invalid = False
        self.echo_invalid_secret = False
        self.echo_valid_secret = False
        self.invalid_http_status = 400
        self.invalid_provider_status = 401
        self.fail_valid_for: str | None = None
        self.reject_valid_for: str | None = None
        self.fail_registration_at: int | None = None
        self.trace_deploy_enabled = True
        self.runtime_mode = provisioner.DIAGNOSTIC_RUNTIME_MODE
        self.runtime_version = provisioner.DIAGNOSTIC_RUNTIME_VERSION
        self.runtime_configured = True
        self.reset_fail_for: set[str] = set()
        self.already_reset_for: set[str] = set()
        self.revoked_keys: set[str] = set()
        self.deleted_config_keys: set[str] = set()

    def register(self, label: str) -> Session:
        index = len(self.registered)
        if self.fail_registration_at == index:
            raise SmokeError("register", "synthetic registration failure")
        session = Session(
            user_id=f"user-{index}",
            api_key=f"feedling-account-key-{index}",
            sk=bytes([index + 1]) * 32,
            pk=bytes([index + 11]) * 32,
        )
        self.registered.append((label, session))
        return session

    def setup(
        self, session, provider, model, base_url, api_key, *, reasoning_effort=None
    ):
        self.setup_calls.append(
            (session.user_id, provider, model, base_url, api_key, reasoning_effort)
        )
        if self.fail_valid_for == session.user_id:
            raise SmokeError("setup", f"provider echoed secret={api_key}")
        return {
            "provider": provider,
            "model": model,
            "base_url": self._configured_base_url(provider, base_url),
            "reasoning_effort": reasoning_effort,
        }

    @staticmethod
    def _configured_base_url(provider: str, requested_base_url: str) -> str:
        if requested_base_url:
            return requested_base_url.rstrip("/")
        return {
            "deepseek": "https://api.deepseek.com",
            "anthropic": "https://api.anthropic.com/v1",
            "openai": "https://api.openai.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta",
            "openrouter": "https://openrouter.ai/api/v1",
        }[provider]

    def setup_raw(
        self, session, provider, model, base_url, api_key, *, reasoning_effort=None
    ):
        self.setup_calls.append(
            (session.user_id, provider, model, base_url, api_key, reasoning_effort)
        )
        if api_key != provisioner.INVALID_PROVIDER_KEY:
            if self.fail_valid_for == session.user_id:
                raise SmokeError("setup", f"provider echoed secret={api_key}")
            if self.reject_valid_for == session.user_id:
                return 400, {
                    "error": "provider_test_failed",
                    "detail": "provider authentication rejected",
                    "status_code": 401,
                }
            config = {
                "provider": provider,
                "model": model,
                "base_url": self._configured_base_url(provider, base_url),
                "reasoning_effort": reasoning_effort,
            }
            if self.echo_valid_secret:
                config["nested"] = {"credential": api_key}
            return 200, {"status": "configured", "config": config}
        if self.accept_invalid:
            return 200, {
                "status": "configured",
                "config": {"provider": provider, "model": model},
            }
        body = {
            "error": "provider_test_failed",
            "detail": "provider authentication rejected",
            "status_code": self.invalid_provider_status,
        }
        if self.echo_invalid_secret:
            body["nested"] = {"detail": f"rejected secret={api_key}"}
        return self.invalid_http_status, body

    def runtime_status(self, session):
        status, body = self._req(
            "GET", "/v1/model_api/runtime", api_key=session.api_key
        )
        if status != 200:
            raise SmokeError("runtime", f"status={status}")
        return body

    def _req(self, method, path, *, api_key=None, body=None, **_kwargs):
        if path == "/v1/users/whoami":
            if api_key in self.revoked_keys or api_key in self.already_reset_for:
                return 401, {"error": "unauthorized"}
            session = next(s for _label, s in self.registered if s.api_key == api_key)
            return 200, {"user_id": session.user_id, "active_route": "model_api"}
        if path.startswith("/v1/model_api/") and api_key in self.revoked_keys:
            return 401, {"error": "unauthorized"}
        if path == "/v1/model_api/get":
            return 200, {
                "config": {"configured": api_key not in self.deleted_config_keys}
            }
        if path == "/v1/model_api/delete":
            assert method == "DELETE"
            self.deleted_config_keys.add(api_key)
            return 200, {"deleted": True}
        if path == "/v1/model_api/key_envelope":
            if api_key in self.deleted_config_keys:
                return 404, {"error": "model_api_key_envelope_missing"}
            return 200, {"api_key_envelope": {"ciphertext": "not-public"}}
        if path == "/v1/chat/history?limit=1":
            return 200, {"messages": []}
        if path == "/v1/memory/list?limit=1":
            return 200, {"moments": []}
        if path == "/v1/debug/trace/enable":
            self.trace_calls.append(api_key)
            assert method == "POST"
            assert body == {"enabled": True}
            return 200, {"enabled": True, "deploy_enabled": self.trace_deploy_enabled}
        if path == "/v1/model_api/runtime":
            self.runtime_calls.append(api_key)
            return 200, {
                "configured": self.runtime_configured,
                "runtime_mode": self.runtime_mode,
                "runtime_version": self.runtime_version,
            }
        if path == "/v1/account/reset":
            self.reset_calls.append((api_key, body))
            if api_key in self.already_reset_for or api_key in self.revoked_keys:
                return 401, {"error": "unauthorized"}
            if api_key in self.reset_fail_for:
                return 503, {"error": "unavailable"}
            self.revoked_keys.add(api_key)
            return 200, {"deleted": True}
        raise AssertionError(f"unexpected request: {method} {path}")


class FakeAdminClient:
    def __init__(self, smoke: FakeSmokeClient | None = None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.modes: dict[str, str] = {}
        self.missing_users: set[str] = set()
        self.user_lookup_status: int | None = None
        self.smoke = smoke

    def register_synthetic(self, label: str, *, run_id: str, ttl_seconds: int):
        if self.smoke is None:
            raise AssertionError(
                "synthetic registration requires the fake smoke client"
            )
        session = self.smoke.register(label)
        self.calls.append(
            (
                "POST",
                provisioner.SYNTHETIC_REGISTRATION_PATH,
                {"label": label, "run_id": run_id, "ttl_seconds": ttl_seconds},
            )
        )
        return session, {
            **_synthetic_lease(len(self.smoke.registered)),
            "ttl_seconds": ttl_seconds,
        }

    def cleanup_synthetic_run(self, run_id: str):
        self.calls.append(
            (
                "POST",
                provisioner.SYNTHETIC_CLEANUP_RUN_PATH,
                {"run_id": run_id},
            )
        )
        return _cleanup_run_receipt(run_id)

    def request(self, method: str, path: str, body=None):
        self.calls.append((method, path, body))
        if path == provisioner.SYNTHETIC_REAPER_PATH:
            return 200, {
                "enabled": True,
                "ready": True,
                "heartbeat_fresh": True,
                "label_prefix": provisioner.SYNTHETIC_LABEL_PREFIX,
                "max_ttl_seconds": provisioner.MAX_SYNTHETIC_TTL_SECONDS,
            }
        if path == provisioner.SYNTHETIC_ABSENCE_PATH:
            if self.user_lookup_status is not None:
                return self.user_lookup_status, {"error": "lookup_unavailable"}
            user_id = str((body or {}).get("user_id") or "")
            lease_id = str((body or {}).get("lease_id") or "")
            absent = user_id in self.missing_users
            if self.smoke is not None:
                absent = absent or any(
                    session.user_id == user_id
                    and session.api_key in self.smoke.revoked_keys
                    for _label, session in self.smoke.registered
                )
            return 200, {
                "schema_version": 1,
                "status": "absent" if absent else "present",
                "user_id": user_id,
                "lease_id": lease_id,
                "lease_attested": True,
                "database_authoritative": True,
            }
        if path.startswith("/v1/admin/data-track/users/"):
            user_id = path.rsplit("/", 1)[1]
            if self.user_lookup_status is not None:
                return self.user_lookup_status, {"error": "lookup_unavailable"}
            if user_id in self.missing_users:
                return 404, {"error": "user_not_found"}
            if self.smoke is not None and any(
                session.user_id == user_id
                and session.api_key in self.smoke.revoked_keys
                for _label, session in self.smoke.registered
            ):
                return 404, {"error": "user_not_found"}
            return 200, {"user": {"user_id": user_id}}
        if method == "POST":
            self.modes[body["user_id"]] = body["mode"]
            return 200, {
                "user_id": body["user_id"],
                "hosted_runtime_mode": body["mode"],
            }
        user_id = path.split("user_id=", 1)[1]
        return 200, {
            "user_id": user_id,
            "hosted_runtime_mode": self.modes[user_id],
        }


def test_provision_creates_all_profiles_without_persisting_provider_secrets(tmp_path):
    coverage = _write_coverage(tmp_path)
    manifest_path = tmp_path / "private" / "profiles.json"
    env = _env()
    smoke = FakeSmokeClient()
    admin = FakeAdminClient(smoke)

    result = provisioner.provision(
        coverage, manifest_path, env=env, client=smoke, admin_client=admin
    )

    assert len(result["profiles"]) == len(provisioner.PROFILE_SPECS) == 9
    assert len(smoke.registered) == 10
    assert len(smoke.setup_calls) == 18
    assert len(smoke.trace_calls) == 9
    assert len(admin.calls) == 11
    assert admin.calls[0] == ("GET", provisioner.SYNTHETIC_REAPER_PATH, None)
    for index in range(0, len(smoke.setup_calls), 2):
        assert smoke.setup_calls[index][4] == provisioner.INVALID_PROVIDER_KEY
        assert smoke.setup_calls[index + 1][4] != provisioner.INVALID_PROVIDER_KEY
        assert smoke.setup_calls[index][5] == provisioner.EXPECTED_REASONING_EFFORT
        assert smoke.setup_calls[index + 1][5] == provisioner.EXPECTED_REASONING_EFFORT
    for call in smoke.setup_calls:
        profile_id = next(
            row["profile_id"] for row in result["profiles"] if row["user_id"] == call[0]
        )
        expected_request_base_url = (
            provisioner.ALLOWED_KONGBEIQIE_BASE_URL
            if profile_id == "relay-kongbeiqie"
            else ""
        )
        assert call[3] == expected_request_base_url
    assert all(row["invalid_key_rejected"] for row in result["profiles"])
    assert all(row["valid_key_configured"] for row in result["profiles"])
    assert all(row["registration_verified"] for row in result["profiles"])
    assert all(row["fresh_state_verified"] for row in result["profiles"])
    assert all(row["trace_enabled"] for row in result["profiles"])
    assert all(row["runtime_mode"] == "hosted_resident" for row in result["profiles"])
    assert all(
        row["runtime_mode_set_required"] is False
        and row["runtime_mode_set_verified"] is False
        for row in result["profiles"]
    )
    assert all(row["runtime_mode_readback_verified"] for row in result["profiles"])
    assert [row["profile_id"] for row in result["profiles"]] == list(
        provisioner.PROFILE_SPECS
    )
    assert all(
        row["provision_status"] == provisioner.PROVISION_STATUS_READY
        and row["provision_failure_code"] == provisioner.PROVISION_FAILURE_NONE
        for row in result["profiles"]
    )
    assert all(
        row["reasoning_effort"] == provisioner.EXPECTED_REASONING_EFFORT
        for row in result["profiles"]
    )
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600

    raw = manifest_path.read_text()
    for name, value in env.items():
        if name.endswith("API_KEY") or name == "IO_E2E_ADMIN_TOKEN":
            assert value not in raw
    persisted = json.loads(raw)
    assert {p["profile_id"] for p in persisted["profiles"]} == set(
        provisioner.PROFILE_SPECS
    )
    for profile in persisted["profiles"]:
        spec = provisioner.PROFILE_SPECS[profile["profile_id"]]
        assert profile["configured_model"] == VALID_MODELS[profile["profile_id"]]
        assert profile["configured_base_url"] == spec.expected_configured_base_url
        assert profile["invalid_key_receipt"] == {
            "http_status": 400,
            "error": "provider_test_failed",
            "provider_status_code": 401,
        }
        assert profile["valid_key_receipt"] == {
            "status": "configured",
            "provider": profile["provider"],
            "model": profile["configured_model"],
            "base_url": spec.expected_configured_base_url,
            "reasoning_effort": provisioner.EXPECTED_REASONING_EFFORT,
        }
        assert profile["synthetic_account_lease"] == _synthetic_lease(
            list(provisioner.PROFILE_SPECS).index(profile["profile_id"]) + 1
        )
    assert persisted["auxiliary_accounts"] == [
        {
            "profile_id": provisioner.MEMORY_CONTRACT_PROFILE_ID,
            "purpose": "deterministic_memory_contract",
            "label": "agent-e2e-unit-42-memory-contract",
            "user_id": "user-9",
            "api_key": "feedling-account-key-9",
            "secret_key_b64": provisioner.base64.b64encode(bytes([10]) * 32).decode(),
            "public_key_b64": provisioner.base64.b64encode(bytes([20]) * 32).decode(),
            "provision_status": provisioner.PROVISION_STATUS_READY,
            "provision_failure_code": provisioner.PROVISION_FAILURE_NONE,
            "synthetic_account_lease": _synthetic_lease(10),
        }
    ]


def test_adminless_diagnostic_subset_uses_user_runtime_readback(tmp_path):
    env = _env()
    env.pop("IO_E2E_ADMIN_TOKEN")
    for profile_id, spec in provisioner.PROFILE_SPECS.items():
        if profile_id == "official-gemini":
            continue
        env.pop(spec.credential_env, None)
        env.pop(spec.model_env, None)
    smoke = FakeSmokeClient()
    admin = FakeAdminClient()
    manifest_path = tmp_path / "diagnostic.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest_path,
        env=env,
        client=smoke,
        admin_client=admin,
        diagnostic=True,
        profile_ids=["official-gemini"],
    )

    assert admin.calls == []
    assert result["qualification_mode"] == "diagnostic"
    assert result["selected_profile_ids"] == ["official-gemini"]
    assert result["runtime_mode"] == "deployed_current"
    assert result["runtime_requirement"] == "deployed_current"
    assert result["synthetic_account_reaper"] == {
        "required": False,
        "verified": False,
        "reason": "adminless_diagnostic",
    }
    assert smoke.runtime_calls == ["feedling-account-key-0"]
    assert len(result["profiles"]) == 1
    profile = result["profiles"][0]
    assert profile["profile_id"] == "official-gemini"
    assert profile["runtime_mode"] == "hosted_resident"
    assert profile["runtime_version"] == 2
    assert profile["runtime_mode_set_required"] is False
    assert profile["runtime_mode_set_verified"] is False
    assert profile["runtime_mode_readback_verified"] is True
    assert profile["runtime_readback_receipt"] == {
        "configured": True,
        "runtime_mode": "hosted_resident",
        "runtime_version": 2,
    }
    assert profile["provision_status"] == provisioner.PROVISION_STATUS_READY
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600


def test_baseline_diagnostic_records_non_v2_runtime_without_selecting_it(tmp_path):
    env = _env()
    env.pop("IO_E2E_ADMIN_TOKEN")
    smoke = FakeSmokeClient()
    smoke.runtime_mode = "resident_cli"
    smoke.runtime_version = 1

    result = provisioner.provision(
        _write_coverage(tmp_path),
        tmp_path / "diagnostic.json",
        env=env,
        client=smoke,
        diagnostic=True,
        profile_ids=["official-gemini"],
    )

    profile = result["profiles"][0]
    assert result["runtime_requirement"] == "deployed_current"
    assert profile["runtime_mode"] == "resident_cli"
    assert profile["runtime_version"] == 1
    assert profile["runtime_mode_set_verified"] is False
    assert profile["runtime_mode_readback_verified"] is True
    assert profile["provision_status"] == provisioner.PROVISION_STATUS_READY


@pytest.mark.parametrize(
    "runtime_field,runtime_value",
    [
        ("runtime_configured", False),
        ("runtime_mode", ""),
        ("runtime_version", 0),
        ("runtime_version", 2.0),
    ],
)
def test_adminless_diagnostic_blocks_runtime_readback_mismatch(
    tmp_path, runtime_field, runtime_value
):
    env = _env()
    env.pop("IO_E2E_ADMIN_TOKEN")
    smoke = FakeSmokeClient()
    setattr(smoke, runtime_field, runtime_value)

    result = provisioner.provision(
        _write_coverage(tmp_path),
        tmp_path / "diagnostic.json",
        env=env,
        client=smoke,
        diagnostic=True,
        profile_ids=["official-gemini"],
    )

    profile = result["profiles"][0]
    assert profile["provision_status"] == provisioner.PROVISION_STATUS_BLOCKED
    assert profile["provision_failure_code"] == "RUNTIME_MODE_VERIFICATION_FAILED"
    assert profile["runtime_mode_readback_verified"] is False


@pytest.mark.parametrize(
    "runtime_field,runtime_value",
    [("runtime_mode", "resident_cli"), ("runtime_version", 1)],
)
def test_adminless_strict_v2_blocks_non_v2_runtime(
    tmp_path, runtime_field, runtime_value
):
    env = _env()
    env.pop("IO_E2E_ADMIN_TOKEN")
    smoke = FakeSmokeClient()
    setattr(smoke, runtime_field, runtime_value)

    result = provisioner.provision(
        _write_coverage(tmp_path),
        tmp_path / "diagnostic.json",
        env=env,
        client=smoke,
        diagnostic=True,
        profile_ids=["official-gemini"],
        runtime_requirement=provisioner.RUNTIME_V2_REQUIREMENT,
    )

    profile = result["profiles"][0]
    assert profile["provision_status"] == provisioner.PROVISION_STATUS_BLOCKED
    assert profile["provision_failure_code"] == "RUNTIME_MODE_VERIFICATION_FAILED"


def test_profile_subset_is_rejected_outside_diagnostic_mode(tmp_path):
    smoke = FakeSmokeClient()
    admin = FakeAdminClient()

    with pytest.raises(provisioner.ProvisionError, match="require diagnostic mode"):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=_env(),
            client=smoke,
            admin_client=admin,
            profile_ids=["official-gemini"],
        )

    assert smoke.registered == []
    assert admin.calls == []


@pytest.mark.parametrize(
    "profile_ids,error",
    [
        ([], "must not be empty"),
        (["official-gemini", "official-gemini"], "duplicates"),
        (["not-locked"], "outside the locked"),
        ("official-gemini", "must be a sequence"),
    ],
)
def test_invalid_diagnostic_profile_selection_fails_before_external_state(
    tmp_path, profile_ids, error
):
    smoke = FakeSmokeClient()

    with pytest.raises(provisioner.ProvisionError, match=error):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "diagnostic.json",
            env={
                "IO_E2E_BASE_URL": provisioner.ALLOWED_BASE_URL,
            },
            client=smoke,
            diagnostic=True,
            profile_ids=profile_ids,
        )

    assert smoke.registered == []


def test_invalid_and_valid_setup_calls_use_profile_locked_base_urls(tmp_path):
    smoke = FakeSmokeClient()
    result = provisioner.provision(
        _write_coverage(tmp_path),
        tmp_path / "manifest.json",
        env=_env(),
        client=smoke,
        admin_client=FakeAdminClient(smoke),
    )
    profile_by_user = {row["user_id"]: row for row in result["profiles"]}

    for index in range(0, len(smoke.setup_calls), 2):
        invalid_call = smoke.setup_calls[index]
        valid_call = smoke.setup_calls[index + 1]
        assert invalid_call[0] == valid_call[0]
        profile = profile_by_user[invalid_call[0]]
        expected_request_base_url = (
            provisioner.ALLOWED_KONGBEIQIE_BASE_URL
            if profile["profile_id"] == "relay-kongbeiqie"
            else ""
        )
        assert invalid_call[3] == expected_request_base_url
        assert valid_call[3] == expected_request_base_url
        assert profile["configured_base_url"] == (
            provisioner.PROFILE_SPECS[
                profile["profile_id"]
            ].expected_configured_base_url
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://test-api.feedling.app",
        "https://test-api.feedling.app.evil.example",
        "https://test-api.feedling.app/collect",
        "https://test-api.feedling.app:not-a-port",
        "https://user@test-api.feedling.app",
        "https://test-api.feedling.app?next=https://evil.example",
    ],
)
def test_base_url_allowlist_rejects_variants(url):
    with pytest.raises(provisioner.ProvisionError, match="approved test endpoint"):
        provisioner.validate_base_url(url)


def test_admin_redirect_handler_rejects_without_constructing_forward_request():
    token = "admin-sensitive-value"
    request = urllib.request.Request(
        provisioner.ALLOWED_BASE_URL + "/v1/admin/data-track/users/u1",
        headers={"X-Admin-Token": token},
    )
    handler = provisioner._RejectRedirects()

    with pytest.raises(urllib.error.HTTPError) as caught:
        handler.redirect_request(
            request,
            io.BytesIO(b""),
            302,
            "Found",
            {"Location": "https://attacker.example/collect"},
            "https://attacker.example/collect",
        )

    assert caught.value.code == 302
    assert caught.value.url == request.full_url
    assert token not in str(caught.value)


def test_admin_client_installs_reject_redirect_handler():
    client = provisioner.AdminClient(
        provisioner.ALLOWED_BASE_URL,
        "admin-sensitive-value",
    )
    assert any(
        isinstance(handler, provisioner._RejectRedirects)
        for handler in client._opener.handlers
    )


def test_admin_client_registers_server_marked_account_without_exporting_private_key(
    monkeypatch,
):
    client = provisioner.AdminClient(
        provisioner.ALLOWED_BASE_URL,
        "admin-sensitive-value",
    )
    observed = {}

    def request(method, path, body=None, **kwargs):
        observed.update(
            {"method": method, "path": path, "body": body, "kwargs": kwargs}
        )
        return 201, {
            "user_id": "usr_unit",
            "api_key": "account-key",
            "label": body["label"],
            "lease_id": "lease_" + "a" * 32,
            "absence_token": "b" * 64,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "expires_at_epoch": int(time.time()) + 600,
        }

    monkeypatch.setattr(client, "request", request)
    session, receipt = client.register_synthetic(
        "agent-e2e-run-official-gemini", run_id="run", ttl_seconds=600
    )

    assert observed["method"] == "POST"
    assert observed["path"] == provisioner.SYNTHETIC_REGISTRATION_PATH
    assert observed["kwargs"] == {"attempts": 1}
    assert observed["body"]["access_mode"] == "model_api"
    assert observed["body"]["run_id"] == "run"
    assert observed["body"]["ttl_seconds"] == 600
    assert observed["body"]["public_key"] == provisioner.base64.b64encode(
        session.pk
    ).decode("ascii")
    assert "private_key" not in observed["body"]
    assert session.user_id == "usr_unit"
    assert receipt["registered"] is True
    assert receipt["absence_token"] == "b" * 64
    assert receipt["ttl_seconds"] == 600


def test_admin_client_never_retries_non_idempotent_synthetic_registration():
    client = provisioner.AdminClient(
        provisioner.ALLOWED_BASE_URL,
        "admin-sensitive-value",
    )

    class LostResponseOpener:
        def __init__(self):
            self.calls = 0

        def open(self, _request, timeout):
            assert timeout == 45
            self.calls += 1
            if self.calls > 1:
                pytest.fail("non-idempotent synthetic registration was retried")
            raise TimeoutError("response lost after server commit")

    opener = LostResponseOpener()
    client._opener = opener

    with pytest.raises(
        provisioner.ProvisionError, match="synthetic account registration failed"
    ):
        client.register_synthetic(
            "agent-e2e-run-official-gemini", run_id="run", ttl_seconds=600
        )

    assert opener.calls == 1


def test_admin_client_rejects_unbound_synthetic_registration_receipt(monkeypatch):
    client = provisioner.AdminClient(
        provisioner.ALLOWED_BASE_URL,
        "admin-sensitive-value",
    )
    monkeypatch.setattr(
        client,
        "request",
        lambda *_args, **_kwargs: (
            201,
            {
                "user_id": "usr_unit",
                "api_key": "account-key",
                "label": "agent-e2e-different-account",
                "lease_id": "lease_" + "a" * 32,
                "expires_at": "2099-01-01T00:00:00+00:00",
                "expires_at_epoch": int(time.time()) + 600,
            },
        ),
    )

    with pytest.raises(provisioner.ProvisionError, match="receipt is invalid"):
        client.register_synthetic(
            "agent-e2e-run-official-gemini", run_id="run", ttl_seconds=600
        )


def test_admin_client_requires_server_absence_attestation(monkeypatch):
    client = provisioner.AdminClient(
        provisioner.ALLOWED_BASE_URL,
        "admin-sensitive-value",
    )
    label = "agent-e2e-run-official-gemini"
    monkeypatch.setattr(
        client,
        "request",
        lambda *_args, **_kwargs: (
            201,
            {
                "user_id": "usr_unit",
                "api_key": "account-key",
                "label": label,
                "lease_id": "lease_" + "a" * 32,
                "expires_at": "2099-01-01T00:00:00+00:00",
                "expires_at_epoch": int(time.time()) + 600,
            },
        ),
    )

    with pytest.raises(provisioner.ProvisionError, match="receipt is invalid"):
        client.register_synthetic(label, run_id="run", ttl_seconds=600)


def test_admin_client_validates_aggregate_authoritative_cleanup_run_receipt(
    monkeypatch,
):
    client = provisioner.AdminClient(
        provisioner.ALLOWED_BASE_URL,
        "admin-sensitive-value",
    )
    run_id = "api-key-e2e-123-1"
    observed = {}

    def request(method, path, body=None, **kwargs):
        observed.update(
            {"method": method, "path": path, "body": body, "kwargs": kwargs}
        )
        return 200, _cleanup_run_receipt(run_id)

    monkeypatch.setattr(client, "request", request)
    receipt = client.cleanup_synthetic_run(run_id)

    assert observed == {
        "method": "POST",
        "path": provisioner.SYNTHETIC_CLEANUP_RUN_PATH,
        "body": {"run_id": run_id},
        "kwargs": {"attempts": 1, "timeout_seconds": 180},
    }
    assert receipt["database_authoritative"] is True
    assert receipt["remaining_count"] == 0
    assert receipt["complete"] is True
    assert run_id not in json.dumps(receipt)


@pytest.mark.parametrize(
    "mutation",
    [
        {"run_id_sha256": "0" * 64},
        {"database_authoritative": False},
        {"remaining_count": -1},
        {"matched_count": 1},
        {"complete": True, "remaining_count": 1},
        {"unexpected": True},
    ],
)
def test_admin_client_rejects_unbound_or_invalid_cleanup_run_receipt(
    monkeypatch, mutation
):
    client = provisioner.AdminClient(
        provisioner.ALLOWED_BASE_URL,
        "admin-sensitive-value",
    )
    run_id = "api-key-e2e-123-1"
    receipt = _cleanup_run_receipt(run_id)
    receipt.update(mutation)
    monkeypatch.setattr(
        client, "request", lambda *_args, **_kwargs: (200, receipt)
    )

    with pytest.raises(provisioner.ProvisionError, match="receipt is invalid"):
        client.cleanup_synthetic_run(run_id)


def test_cleanup_run_needs_no_manifest_and_writes_private_receipt(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    receipt_path = (private / "run-cleanup.json").resolve()
    admin = FakeAdminClient()
    run_id = "api-key-e2e-123-1"

    receipt = provisioner.cleanup_run(
        run_id,
        receipt_path,
        env=_env(),
        admin_client=admin,
    )

    assert receipt["complete"] is True
    assert receipt_path.is_file()
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert json.loads(receipt_path.read_text()) == receipt
    assert admin.calls == [
        (
            "POST",
            provisioner.SYNTHETIC_CLEANUP_RUN_PATH,
            {"run_id": run_id},
        )
    ]


def test_cleanup_run_rejects_ambiguous_raw_run_id_before_network_or_file(
    tmp_path,
):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    admin = FakeAdminClient()
    with pytest.raises(provisioner.ProvisionError, match="already be normalized"):
        provisioner.cleanup_run(
            "api/key",
            (private / "receipt.json").resolve(),
            env=_env(),
            admin_client=admin,
        )
    assert admin.calls == []
    assert not (private / "receipt.json").exists()


def test_long_run_id_normalization_is_collision_resistant_and_preserves_workflow_ids():
    base = "api-key-e2e-1234567890-1"
    persona = f"{base}-persona-memory"
    assert provisioner.normalize_synthetic_run_id(base) == base
    assert provisioner.normalize_synthetic_run_id(persona) == persona

    shared = "x" * provisioner.MAX_SYNTHETIC_RUN_ID_LENGTH
    first = provisioner.normalize_synthetic_run_id(shared + "-first")
    second = provisioner.normalize_synthetic_run_id(shared + "-second")
    assert first != second
    assert len(first) == provisioner.MAX_SYNTHETIC_RUN_ID_LENGTH
    assert len(second) == provisioner.MAX_SYNTHETIC_RUN_ID_LENGTH
    assert provisioner._SYNTHETIC_RUN_ID_RE.fullmatch(first)
    assert provisioner._SYNTHETIC_RUN_ID_RE.fullmatch(second)


def test_coverage_must_contain_exact_locked_profiles(tmp_path):
    coverage = _write_coverage(tmp_path, PROFILE_ROWS[:-1])
    with pytest.raises(
        provisioner.ProvisionError, match="coverage profiles do not match"
    ):
        provisioner.provision(
            coverage,
            tmp_path / "manifest.json",
            env=_env(),
            client=FakeSmokeClient(),
            admin_client=FakeAdminClient(),
        )


def test_provisioning_refuses_to_register_without_safe_server_reaper(tmp_path):
    admin = FakeAdminClient()

    def unsafe_reaper(method, path, body=None):
        admin.calls.append((method, path, body))
        return 200, {
            "enabled": False,
            "label_prefix": provisioner.SYNTHETIC_LABEL_PREFIX,
            "max_ttl_seconds": provisioner.MAX_SYNTHETIC_TTL_SECONDS,
        }

    admin.request = unsafe_reaper
    smoke = FakeSmokeClient()
    with pytest.raises(
        provisioner.ProvisionError, match="reaper is not safely configured"
    ):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=_env(),
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []


@pytest.mark.parametrize(
    "readiness",
    [
        {"ready": False, "heartbeat_fresh": False},
        {"ready": True, "heartbeat_fresh": False},
        {},
    ],
    ids=("missing-heartbeat", "stale-heartbeat", "missing-readiness-fields"),
)
def test_provisioning_refuses_unready_or_stale_reaper(tmp_path, readiness):
    admin = FakeAdminClient()

    def unhealthy_reaper(method, path, body=None):
        admin.calls.append((method, path, body))
        return 200, {
            "enabled": True,
            "label_prefix": provisioner.SYNTHETIC_LABEL_PREFIX,
            "max_ttl_seconds": provisioner.MAX_SYNTHETIC_TTL_SECONDS,
            **readiness,
        }

    admin.request = unhealthy_reaper
    smoke = FakeSmokeClient()
    with pytest.raises(
        provisioner.ProvisionError, match="reaper is not safely configured"
    ):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=_env(),
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []


def test_all_static_credentials_are_validated_before_reaper_or_registration(tmp_path):
    env = _env()
    del env["QA_ANTHROPIC_API_KEY"]
    smoke = FakeSmokeClient()
    admin = FakeAdminClient()

    with pytest.raises(
        provisioner.ProvisionError,
        match="missing required environment variable: QA_ANTHROPIC_API_KEY",
    ):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=env,
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []
    assert admin.calls == []


def test_missing_relay_base_url_fails_before_external_state(tmp_path):
    env = _env()
    del env["QA_KONGBEIQIE_BASE_URL"]
    smoke = FakeSmokeClient()
    admin = FakeAdminClient()

    with pytest.raises(
        provisioner.ProvisionError,
        match="missing required environment variable: QA_KONGBEIQIE_BASE_URL",
    ):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=env,
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []
    assert admin.calls == []


@pytest.mark.parametrize(
    "unapproved_url",
    [
        "http://xn--vduyey89e.com/v1",
        "https://relay.example/v1",
        f"{provisioner.ALLOWED_KONGBEIQIE_BASE_URL}/",
        "https://user@xn--vduyey89e.com/v1",
        "https://xn--vduyey89e.com/v1?forward=https://attacker.example",
    ],
)
def test_unapproved_relay_base_url_is_rejected_without_echo_or_external_state(
    tmp_path, unapproved_url
):
    env = _env()
    env["QA_KONGBEIQIE_BASE_URL"] = unapproved_url
    smoke = FakeSmokeClient()
    admin = FakeAdminClient()

    with pytest.raises(provisioner.ProvisionError, match="locked endpoint") as caught:
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=env,
            client=smoke,
            admin_client=admin,
        )

    assert unapproved_url not in str(caught.value)
    assert smoke.registered == []
    assert admin.calls == []


def test_repository_coverage_lock_matches_provisioner_contract():
    coverage = Path(__file__).resolve().parents[1] / "coverage-lock.json"
    profiles = provisioner._load_coverage(coverage)
    assert [profile["profile_id"] for profile in profiles] == list(
        provisioner.PROFILE_SPECS
    )
    for profile in profiles:
        spec = provisioner.PROFILE_SPECS[profile["profile_id"]]
        assert profile["model_family"] == spec.model_family
        assert profile["model_env"] == spec.model_env
        assert profile["allowed_model_regex"] == spec.allowed_model_regex
        assert str(profile.get("base_url_env") or "") == spec.base_url_env
        assert str(profile.get("allowed_base_url") or "") == spec.allowed_base_url


@pytest.mark.parametrize("field", ["base_url_env", "allowed_base_url"])
def test_coverage_cannot_redirect_relay_key(tmp_path, field):
    rows = [dict(row) for row in PROFILE_ROWS]
    relay = next(row for row in rows if row["profile_id"] == "relay-kongbeiqie")
    relay[field] = (
        "QA_ATTACKER_URL" if field == "base_url_env" else "https://attacker.example/v1"
    )

    with pytest.raises(provisioner.ProvisionError, match="base URL") as caught:
        provisioner._load_coverage(_write_coverage(tmp_path, rows))

    assert "attacker.example" not in str(caught.value)


@pytest.mark.parametrize("profile_id", list(provisioner.PROFILE_SPECS))
def test_coverage_cannot_weaken_hard_coded_model_constraint(tmp_path, profile_id):
    rows = [dict(row) for row in PROFILE_ROWS]
    row = next(item for item in rows if item["profile_id"] == profile_id)
    row["allowed_model_regex"] = r"^.*$"

    with pytest.raises(provisioner.ProvisionError, match="model constraint mismatch"):
        provisioner._load_coverage(_write_coverage(tmp_path, rows))


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("model_family", "wrong-family", "model family mismatch"),
        ("model_env", "QA_WRONG_MODEL", "model environment mismatch"),
    ],
)
def test_coverage_model_route_fields_are_hard_locked(tmp_path, field, value, error):
    rows = [dict(row) for row in PROFILE_ROWS]
    rows[0][field] = value

    with pytest.raises(provisioner.ProvisionError, match=error):
        provisioner._load_coverage(_write_coverage(tmp_path, rows))


@pytest.mark.parametrize(
    "profile_id,model",
    [
        ("official-deepseek", "deepseek-chat"),
        ("official-deepseek", "deepseek-v4-flash"),
        ("official-anthropic", "claude-3-5-sonnet-latest"),
        ("official-anthropic", "claude-sonnet-4-5"),
        ("official-openai", "gpt-4o-mini"),
        ("official-openai", "gpt-5.4"),
        ("official-openai", "o1"),
        ("official-openai", "o3-mini"),
        ("official-gemini", "gemini-2.5-flash"),
        ("official-gemini", "gemini-2.5-pro"),
        ("official-gemini", "gemini-3.5-flash"),
        ("openrouter-claude", "anthropic/claude-sonnet-4.5"),
        ("openrouter-openai", "openai/gpt-4.1-mini"),
        ("openrouter-openai", "openai/o3-mini"),
        ("openrouter-openai", "openai/o-series-preview"),
        ("openrouter-glm", "z-ai/glm-4.5-air:free"),
        ("openrouter-glm", "thudm/glm-4-32b"),
        ("openrouter-kimi", "moonshotai/kimi-k3"),
        ("relay-kongbeiqie", "claude-sonnet-4-6"),
        ("relay-kongbeiqie", "[特价纯血]claude-opus-4-6"),
    ],
)
def test_locked_model_families_accept_realistic_ids(profile_id, model):
    spec = provisioner.PROFILE_SPECS[profile_id]
    profile = next(row for row in PROFILE_ROWS if row["profile_id"] == profile_id)

    assert provisioner._model_for(profile, spec, {spec.model_env: model}) == model


@pytest.mark.parametrize(
    "profile_id,bad_model",
    [
        ("official-deepseek", "claude-sonnet-4-5"),
        ("official-anthropic", "deepseek-chat"),
        ("official-openai", "anthropic/claude-sonnet-4.5"),
        ("official-gemini", "gemini-2.0-flash"),
        ("official-gemini", "gemini-3.0-pro"),
        ("official-gemini", "gemini-4.0-pro"),
        ("openrouter-claude", "openai/gpt-4.1-mini"),
        ("openrouter-openai", "z-ai/glm-4.5-air:free"),
        ("openrouter-glm", "anthropic/claude-sonnet-4.5"),
        ("openrouter-kimi", "z-ai/glm-4.5-air:free"),
        ("relay-kongbeiqie", "openai/gpt-5.4"),
        ("relay-kongbeiqie", "[too-long-label-123456789012345678]claude-opus-4-6"),
    ],
)
def test_wrong_model_family_is_rejected_with_sanitized_error(profile_id, bad_model):
    spec = provisioner.PROFILE_SPECS[profile_id]
    profile = next(row for row in PROFILE_ROWS if row["profile_id"] == profile_id)

    with pytest.raises(provisioner.ProvisionError, match="locked family") as caught:
        provisioner._model_for(profile, spec, {spec.model_env: bad_model})

    assert bad_model not in str(caught.value)


@pytest.mark.parametrize(
    "bad_model",
    [
        "[line\nbreak]claude-opus-4-6",
        "[tab\tlabel]claude-opus-4-6",
        "[bidi\u202elabel]claude-opus-4-6",
        "[line\u2028break]claude-opus-4-6",
        "[pipe|label]claude-opus-4-6",
        "[back`tick]claude-opus-4-6",
    ],
)
def test_relay_model_rejects_controls_and_newlines_without_echo(bad_model):
    profile_id = "relay-kongbeiqie"
    spec = provisioner.PROFILE_SPECS[profile_id]
    profile = next(row for row in PROFILE_ROWS if row["profile_id"] == profile_id)

    with pytest.raises(provisioner.ProvisionError, match="locked family") as caught:
        provisioner._model_for(profile, spec, {spec.model_env: bad_model})

    assert bad_model not in str(caught.value)


@pytest.mark.parametrize(
    "left_profile,right_profile",
    [
        ("openrouter-claude", "openrouter-openai"),
        ("openrouter-claude", "openrouter-glm"),
        ("openrouter-claude", "openrouter-kimi"),
        ("openrouter-openai", "openrouter-glm"),
        ("openrouter-openai", "openrouter-kimi"),
        ("openrouter-glm", "openrouter-kimi"),
    ],
)
def test_swapped_openrouter_models_fail_before_external_state(
    tmp_path, left_profile, right_profile
):
    env = _env()
    left = provisioner.PROFILE_SPECS[left_profile]
    right = provisioner.PROFILE_SPECS[right_profile]
    env[left.model_env], env[right.model_env] = (
        env[right.model_env],
        env[left.model_env],
    )
    smoke = FakeSmokeClient()
    admin = FakeAdminClient()

    with pytest.raises(provisioner.ProvisionError, match="locked family"):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=env,
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []
    assert admin.calls == []


def test_model_must_come_from_its_locked_environment_variable():
    profile_id = "official-deepseek"
    spec = provisioner.PROFILE_SPECS[profile_id]
    profile = next(row for row in PROFILE_ROWS if row["profile_id"] == profile_id)
    profile = {**profile, "configured_model": VALID_MODELS[profile_id]}

    with pytest.raises(provisioner.ProvisionError, match=spec.model_env):
        provisioner._model_for(profile, spec, {})


@pytest.mark.parametrize(
    "field,value",
    [
        ("reasoning_expected", False),
        ("reasoning_effort", ""),
        ("reasoning_effort", "high"),
    ],
)
def test_every_locked_profile_must_explicitly_enable_medium_reasoning(
    tmp_path, field, value
):
    rows = [dict(row) for row in PROFILE_ROWS]
    rows[0][field] = value
    with pytest.raises(provisioner.ProvisionError, match="reasoning"):
        provisioner.provision(
            _write_coverage(tmp_path, rows),
            tmp_path / "manifest.json",
            env=_env(),
            client=FakeSmokeClient(),
            admin_client=FakeAdminClient(),
        )


def test_invalid_key_acceptance_blocks_profiles_without_collapsing_matrix(tmp_path):
    smoke = FakeSmokeClient()
    smoke.accept_invalid = True
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=_env(),
        client=smoke,
        admin_client=FakeAdminClient(smoke),
    )

    assert len(smoke.registered) == 10
    assert [row["profile_id"] for row in result["profiles"]] == list(
        provisioner.PROFILE_SPECS
    )
    assert {row["provision_failure_code"] for row in result["profiles"]} == {
        "INVALID_KEY_ACCEPTED"
    }
    assert all(row["provision_status"] == "blocked" for row in result["profiles"])
    assert smoke.reset_calls == []
    assert manifest.exists()


def test_invalid_key_server_error_has_fixed_diagnostic_code(tmp_path):
    smoke = FakeSmokeClient()
    smoke.invalid_http_status = 503
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=_env(),
        client=smoke,
        admin_client=FakeAdminClient(smoke),
    )

    assert {row["provision_failure_code"] for row in result["profiles"]} == {
        "INVALID_KEY_REJECTION_FAILED"
    }
    assert smoke.reset_calls == []
    assert manifest.exists()


def test_invalid_key_response_must_not_echo_submitted_secret(tmp_path):
    smoke = FakeSmokeClient()
    smoke.echo_invalid_secret = True
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=_env(),
        client=smoke,
        admin_client=FakeAdminClient(smoke),
    )

    assert {row["provision_failure_code"] for row in result["profiles"]} == {
        "INVALID_KEY_ECHOED"
    }
    assert provisioner.INVALID_PROVIDER_KEY not in manifest.read_text()
    assert smoke.reset_calls == []


def test_expired_first_provider_key_does_not_abort_remaining_profiles(tmp_path):
    smoke = FakeSmokeClient()
    smoke.reject_valid_for = "user-0"
    env = _env()
    secret = env["QA_DEEPSEEK_API_KEY"]
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=env,
        client=smoke,
        admin_client=FakeAdminClient(smoke),
    )

    rows = result["profiles"]
    assert [row["profile_id"] for row in rows] == list(provisioner.PROFILE_SPECS)
    assert len(smoke.registered) == 10
    assert rows[0]["provision_status"] == provisioner.PROVISION_STATUS_BLOCKED
    assert rows[0]["provision_failure_code"] == "VALID_KEY_REJECTED"
    assert rows[0]["api_key"] == "feedling-account-key-0"
    assert rows[0]["secret_key_b64"]
    assert all(
        row["provision_status"] == provisioner.PROVISION_STATUS_READY
        and row["provision_failure_code"] == provisioner.PROVISION_FAILURE_NONE
        for row in rows[1:]
    )
    raw = manifest.read_text()
    assert secret not in raw
    assert "provider authentication rejected" not in raw
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert smoke.reset_calls == []


def test_valid_key_transport_failure_is_sanitized_and_isolated(tmp_path):
    smoke = FakeSmokeClient()
    smoke.fail_valid_for = "user-0"
    env = _env()
    secret = env["QA_DEEPSEEK_API_KEY"]
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=env,
        client=smoke,
        admin_client=FakeAdminClient(smoke),
    )

    assert result["profiles"][0]["provision_failure_code"] == "VALID_KEY_SETUP_FAILED"
    assert all(
        row["provision_status"] == provisioner.PROVISION_STATUS_READY
        for row in result["profiles"][1:]
    )
    raw = manifest.read_text()
    assert secret not in raw
    assert "provider echoed" not in raw


def test_valid_key_response_must_not_echo_submitted_secret(tmp_path):
    smoke = FakeSmokeClient()
    smoke.echo_valid_secret = True
    env = _env()
    manifest = tmp_path / "manifest.json"

    result = provisioner.provision(
        _write_coverage(tmp_path),
        manifest,
        env=env,
        client=smoke,
        admin_client=FakeAdminClient(smoke),
    )

    assert {row["provision_failure_code"] for row in result["profiles"]} == {
        "VALID_KEY_ECHOED"
    }
    raw = manifest.read_text()
    for name, secret in env.items():
        if name.endswith("API_KEY") or name == "IO_E2E_ADMIN_TOKEN":
            assert secret not in raw
    assert smoke.reset_calls == []


def test_trace_must_be_deploy_enabled(tmp_path):
    smoke = FakeSmokeClient()
    smoke.trace_deploy_enabled = False
    result = provisioner.provision(
        _write_coverage(tmp_path),
        tmp_path / "manifest.json",
        env=_env(),
        client=smoke,
        admin_client=FakeAdminClient(smoke),
    )
    assert {row["provision_failure_code"] for row in result["profiles"]} == {
        "TRACE_UNAVAILABLE"
    }
    assert smoke.reset_calls == []


def test_manifest_is_checkpointed_after_each_successful_profile_stage(
    tmp_path, monkeypatch
):
    snapshots = []
    original_write = provisioner._atomic_write_manifest

    def record_write(path, payload):
        snapshots.append(json.loads(json.dumps(payload)))
        original_write(path, payload)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    monkeypatch.setattr(provisioner, "_atomic_write_manifest", record_write)
    smoke = FakeSmokeClient()
    provisioner.provision(
        _write_coverage(tmp_path),
        tmp_path / "manifest.json",
        env=_env(),
        client=smoke,
        admin_client=FakeAdminClient(smoke),
    )

    assert len(snapshots) == 6 * len(provisioner.PROFILE_SPECS) + 1
    first_profile_stages = [snapshot["profiles"][0] for snapshot in snapshots[:6]]
    assert first_profile_stages[0]["provision_failure_code"] == (
        provisioner.PROVISION_FAILURE_INCOMPLETE
    )
    assert first_profile_stages[1]["fresh_state_verified"] is True
    assert first_profile_stages[2]["invalid_key_rejected"] is True
    assert first_profile_stages[3]["valid_key_configured"] is True
    assert first_profile_stages[4]["trace_enabled"] is True
    assert first_profile_stages[5]["runtime_mode_set_required"] is False
    assert first_profile_stages[5]["runtime_mode_set_verified"] is False
    assert first_profile_stages[5]["runtime_mode_readback_verified"] is True
    assert first_profile_stages[5]["runtime_readback_receipt"] == {
        "configured": True,
        "runtime_mode": "hosted_resident",
        "runtime_version": 2,
    }
    assert first_profile_stages[5]["provision_status"] == (
        provisioner.PROVISION_STATUS_READY
    )


def test_registration_failure_remains_global_and_cleans_prior_accounts(tmp_path):
    smoke = FakeSmokeClient()
    smoke.fail_registration_at = 1
    manifest = tmp_path / "manifest.json"

    with pytest.raises(
        provisioner.ProvisionError,
        match="account registration failed for profile: official-anthropic",
    ):
        provisioner.provision(
            _write_coverage(tmp_path),
            manifest,
            env=_env(),
            client=smoke,
            admin_client=FakeAdminClient(smoke),
        )

    assert len(smoke.registered) == 1
    assert smoke.reset_calls == [
        ("feedling-account-key-0", {"confirm": "delete-all-data"})
    ]
    assert not manifest.exists()


def test_manifest_write_failure_still_cleans_registered_account(tmp_path, monkeypatch):
    smoke = FakeSmokeClient()

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(provisioner, "_atomic_write_manifest", fail_write)
    with pytest.raises(OSError, match="disk full"):
        provisioner.provision(
            _write_coverage(tmp_path),
            tmp_path / "manifest.json",
            env=_env(),
            client=smoke,
            admin_client=FakeAdminClient(smoke),
        )
    assert smoke.reset_calls == [
        ("feedling-account-key-0", {"confirm": "delete-all-data"})
    ]


def test_cleanup_resets_every_account_and_removes_manifest(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {"profile_id": "p1", "user_id": "u1", "api_key": "account-1"},
                    {"profile_id": "p2", "user_id": "u2", "api_key": "account-2"},
                ],
            }
        )
    )
    smoke = FakeSmokeClient()

    result = provisioner.cleanup(manifest_path, client=smoke)

    assert result == {
        "attempted": 2,
        "cleaned": 2,
        "failed_profile_ids": [],
        "manifest_deleted": True,
        "manifest_missing": False,
    }
    assert [call[0] for call in smoke.reset_calls] == ["account-1", "account-2"]
    assert all(call[1] == {"confirm": "delete-all-data"} for call in smoke.reset_calls)
    assert not manifest_path.exists()


def test_release_cleanup_writes_sanitized_deterministic_receipt_and_retains_manifest(
    tmp_path,
):
    smoke = FakeSmokeClient()
    entries = []
    for profile_id in [*provisioner.PROFILE_SPECS, "memory-contract"]:
        session = smoke.register(f"label-{profile_id}")
        entries.append(
            {
                "profile_id": profile_id,
                "user_id": session.user_id,
                "api_key": session.api_key,
                "synthetic_account_lease": _synthetic_lease(len(entries) + 1),
                **(
                    {
                        "valid_key_configured": True,
                        "valid_key_receipt": {"status": "configured"},
                    }
                    if profile_id != "memory-contract"
                    else {}
                ),
            }
        )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": entries[:-1],
                "auxiliary_accounts": entries[-1:],
            }
        )
    )
    receipt_path = tmp_path / "cleanup-receipt.json"

    result = provisioner.cleanup(
        manifest_path,
        client=smoke,
        admin_client=FakeAdminClient(smoke),
        receipt_path=receipt_path,
        run_id="unit-run-0001",
        retain_manifest=True,
    )

    assert result == {
        "attempted": 10,
        "cleaned": 10,
        "failed_profile_ids": [],
        "manifest_deleted": False,
        "manifest_missing": False,
        "receipt_written": True,
    }
    assert manifest_path.exists()
    receipt = json.loads(receipt_path.read_text())
    assert receipt["kind"] == "deterministic_cleanup_receipt"
    assert receipt["run_id"] == "unit-run-0001"
    assert receipt["manifest_retained_for_scan"] is True
    assert [row["profile_id"] for row in receipt["profiles"]] == list(
        provisioner.PROFILE_SPECS
    )
    assert receipt["auxiliary_accounts"][0]["profile_id"] == "memory-contract"
    assert all(
        row["status"] == "PASS"
        for row in [
            *receipt["profiles"],
            *receipt["auxiliary_accounts"],
        ]
    )
    serialized = receipt_path.read_text()
    assert "api_key" not in serialized
    assert "user_id" not in serialized
    assert "absence_token" not in serialized
    assert "feedling-account-key" not in serialized


def test_release_cleanup_receipt_fails_closed_when_revocation_is_not_observable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_ATTEMPTS", 1)
    smoke = FakeSmokeClient()
    entries = []
    for profile_id in [*provisioner.PROFILE_SPECS, "memory-contract"]:
        session = smoke.register(f"label-{profile_id}")
        entries.append(
            {
                "profile_id": profile_id,
                "user_id": session.user_id,
                "api_key": session.api_key,
                "synthetic_account_lease": _synthetic_lease(len(entries) + 1),
                **(
                    {
                        "valid_key_configured": True,
                        "valid_key_receipt": {"status": "configured"},
                    }
                    if profile_id != "memory-contract"
                    else {}
                ),
            }
        )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": entries[:-1],
                "auxiliary_accounts": entries[-1:],
            }
        )
    )
    admin = FakeAdminClient(smoke)
    admin.user_lookup_status = 503
    receipt_path = tmp_path / "cleanup-receipt.json"

    result = provisioner.cleanup(
        manifest_path,
        client=smoke,
        admin_client=admin,
        receipt_path=receipt_path,
        run_id="unit-run-0001",
        retain_manifest=True,
    )

    assert result["cleaned"] == 0
    assert result["failed_profile_ids"] == [
        *provisioner.PROFILE_SPECS,
        "memory-contract",
    ]
    assert manifest_path.exists()
    assert all(
        row["old_credential_rejected"] is True
        and row["user_absence_verified"] is False
        and row["status"] == "FAIL"
        for row in [
            *json.loads(receipt_path.read_text())["profiles"],
            *json.loads(receipt_path.read_text())["auxiliary_accounts"],
        ]
    )


def test_release_cleanup_retries_a_transient_reset_request(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_DELAY_SECONDS", 0)
    smoke = FakeSmokeClient()
    entries = []
    for profile_id in [*provisioner.PROFILE_SPECS, "memory-contract"]:
        session = smoke.register(f"label-{profile_id}")
        entries.append(
            {
                "profile_id": profile_id,
                "user_id": session.user_id,
                "api_key": session.api_key,
                "synthetic_account_lease": _synthetic_lease(len(entries) + 1),
                **(
                    {
                        "valid_key_configured": True,
                        "valid_key_receipt": {"status": "configured"},
                    }
                    if profile_id != "memory-contract"
                    else {}
                ),
            }
        )
    first_api_key = entries[0]["api_key"]
    original_request = smoke._req
    transient_failures = {first_api_key: 2}

    def transient_request(method, path, *, api_key=None, body=None, **kwargs):
        if path == "/v1/account/reset" and transient_failures.get(api_key, 0) > 0:
            transient_failures[api_key] -= 1
            smoke.reset_calls.append((api_key, body))
            return 503, {"error": "unavailable"}
        return original_request(method, path, api_key=api_key, body=body, **kwargs)

    monkeypatch.setattr(smoke, "_req", transient_request)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": entries[:-1],
                "auxiliary_accounts": entries[-1:],
            }
        )
    )

    result = provisioner.cleanup(
        manifest_path,
        client=smoke,
        admin_client=FakeAdminClient(smoke),
        receipt_path=tmp_path / "cleanup-receipt.json",
        run_id="unit-run-transient-reset",
        retain_manifest=True,
    )

    assert result["cleaned"] == 10
    assert result["failed_profile_ids"] == []
    assert transient_failures[first_api_key] == 0


def test_release_cleanup_still_removes_partial_provisioning_accounts(tmp_path):
    smoke = FakeSmokeClient()
    session = smoke.register("partial")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {
                        "profile_id": "official-deepseek",
                        "user_id": session.user_id,
                        "api_key": session.api_key,
                        "synthetic_account_lease": _synthetic_lease(1),
                        "valid_key_configured": True,
                        "valid_key_receipt": {"status": "configured"},
                    }
                ],
                "auxiliary_accounts": [],
            }
        )
    )
    receipt_path = tmp_path / "cleanup-receipt.json"

    result = provisioner.cleanup(
        manifest_path,
        client=smoke,
        admin_client=FakeAdminClient(smoke),
        receipt_path=receipt_path,
        run_id="unit-run-partial",
        retain_manifest=True,
    )

    assert result["attempted"] == result["cleaned"] == 1
    assert result["failed_profile_ids"] == []
    receipt = json.loads(receipt_path.read_text())
    assert [row["profile_id"] for row in receipt["profiles"]] == ["official-deepseek"]
    assert receipt["profiles"][0]["status"] == "PASS"
    assert session.api_key in smoke.revoked_keys


def test_release_cleanup_verifies_accounts_already_reset_by_profile_workers(tmp_path):
    smoke = FakeSmokeClient()
    entries = []
    for profile_id in [*provisioner.PROFILE_SPECS, "memory-contract"]:
        session = smoke.register(f"label-{profile_id}")
        smoke.revoked_keys.add(session.api_key)
        entries.append(
            {
                "profile_id": profile_id,
                "user_id": session.user_id,
                "api_key": session.api_key,
                "synthetic_account_lease": _synthetic_lease(len(entries) + 1),
                **(
                    {
                        "valid_key_configured": True,
                        "valid_key_receipt": {"status": "configured"},
                    }
                    if profile_id != "memory-contract"
                    else {}
                ),
            }
        )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": entries[:-1],
                "auxiliary_accounts": entries[-1:],
            }
        )
    )
    admin = FakeAdminClient(smoke)
    admin.missing_users.update(entry["user_id"] for entry in entries)
    receipt_path = tmp_path / "cleanup-receipt.json"

    result = provisioner.cleanup(
        manifest_path,
        client=smoke,
        admin_client=admin,
        receipt_path=receipt_path,
        run_id="unit-run-precleaned",
        retain_manifest=True,
    )

    assert result["cleaned"] == 10
    assert result["failed_profile_ids"] == []
    receipt = json.loads(receipt_path.read_text())
    assert all(
        row["status"] == "PASS"
        and row["provider_config_live_predelete_observed"] is False
        and row["provider_config_deletion_source"] == "account_cascade"
        for row in receipt["profiles"]
    )


def test_cleanup_never_unlinks_manifest_replaced_after_snapshot(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {"profile_id": "p1", "user_id": "u1", "api_key": "account-1"}
                ],
            }
        )
    )
    replacement = tmp_path / "replacement.json"
    replacement.write_text("replacement-must-survive", encoding="utf-8")

    def reset_and_replace(_client, _entry, _admin_client=None):
        replacement.replace(manifest_path)
        return True

    monkeypatch.setattr(provisioner, "_reset_one", reset_and_replace)

    result = provisioner.cleanup(manifest_path, client=FakeSmokeClient())

    assert result["attempted"] == 1
    assert result["cleaned"] == 1
    assert result["failed_profile_ids"] == [
        provisioner.MANIFEST_CLEANUP_FAILURE_ID
    ]
    assert result["manifest_deleted"] is False
    assert result["manifest_delete_failure"] == "manifest_path_identity_changed"
    assert manifest_path.read_text(encoding="utf-8") == "replacement-must-survive"


@pytest.mark.parametrize(
    "raw",
    [
        (
            '{"schema_version":1,"base_url":"https://test-api.feedling.app",'
            '"profiles":[],"profiles":[]}'
        ),
        (
            '{"schema_version":1,"base_url":"https://test-api.feedling.app",'
            '"profiles":[],"unexpected":NaN}'
        ),
    ],
    ids=["duplicate-key", "nan"],
)
def test_cleanup_rejects_non_strict_manifest_json_before_reset(tmp_path, raw):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(raw, encoding="utf-8")
    smoke = FakeSmokeClient()

    with pytest.raises(provisioner.ProvisionError, match="manifest is unreadable"):
        provisioner.cleanup(manifest_path, client=smoke)

    assert smoke.reset_calls == []
    assert manifest_path.read_text(encoding="utf-8") == raw


def test_cleanup_manifest_snapshot_keeps_manifest_after_partial_cleanup(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_DELAY_SECONDS", 0)
    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "schema_version": 1,
        "base_url": provisioner.ALLOWED_BASE_URL,
        "profiles": [
            {"profile_id": "p1", "user_id": "u1", "api_key": "account-1"},
            {"profile_id": "p2", "user_id": "u2", "api_key": "account-2"},
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    metadata = manifest_path.stat()
    smoke = FakeSmokeClient()
    smoke.reset_fail_for.add("account-2")

    result = provisioner.cleanup_manifest_snapshot(
        manifest,
        manifest_path=manifest_path,
        manifest_identity=(metadata.st_dev, metadata.st_ino),
        client=smoke,
    )

    assert result == {
        "attempted": 2,
        "cleaned": 1,
        "failed_profile_ids": ["p2"],
        "manifest_deleted": False,
        "manifest_missing": False,
    }
    assert manifest_path.exists()


def test_cleanup_manifest_snapshot_can_checkpoint_success_before_unlink(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "schema_version": 1,
        "base_url": provisioner.ALLOWED_BASE_URL,
        "profiles": [
            {"profile_id": "p1", "user_id": "u1", "api_key": "account-1"},
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    metadata = manifest_path.stat()

    result = provisioner.cleanup_manifest_snapshot(
        manifest,
        manifest_path=manifest_path,
        manifest_identity=(metadata.st_dev, metadata.st_ino),
        client=FakeSmokeClient(),
        delete_manifest=False,
    )

    assert result == {
        "attempted": 1,
        "cleaned": 1,
        "failed_profile_ids": [],
        "manifest_deleted": False,
        "manifest_missing": False,
        "manifest_retained": True,
    }
    assert manifest_path.exists()
    assert (
        provisioner.unlink_manifest_snapshot(
            manifest_path, (metadata.st_dev, metadata.st_ino)
        )
        is None
    )
    assert manifest_path.exists() is False


def test_diagnostic_cleanup_retries_all_pending_accounts_after_transient_rollover(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_ATTEMPTS", 3)
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_DELAY_SECONDS", 0)
    smoke = FakeSmokeClient()
    profiles = [
        {
            "profile_id": profile_id,
            "user_id": f"user-{index}",
            "api_key": f"account-{index}",
        }
        for index, profile_id in enumerate(provisioner.PROFILE_SPECS, start=1)
    ]
    first_round_pending = {row["api_key"] for row in profiles}
    original_request = smoke._req

    def rollover_request(method, path, *, api_key=None, body=None, **kwargs):
        if path == "/v1/account/reset" and api_key in first_round_pending:
            first_round_pending.remove(api_key)
            smoke.reset_calls.append((api_key, body))
            return 503, {"error": "deployment_rollover"}
        return original_request(method, path, api_key=api_key, body=body, **kwargs)

    monkeypatch.setattr(smoke, "_req", rollover_request)
    manifest_path = tmp_path / "diagnostic.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "qualification_mode": "diagnostic",
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": profiles,
            }
        ),
        encoding="utf-8",
    )

    result = provisioner.cleanup(manifest_path, env={}, client=smoke)

    assert result == {
        "attempted": len(profiles),
        "cleaned": len(profiles),
        "failed_profile_ids": [],
        "manifest_deleted": True,
        "manifest_missing": False,
    }
    assert first_round_pending == set()
    assert [api_key for api_key, _body in smoke.reset_calls] == [
        *[row["api_key"] for row in profiles],
        *[row["api_key"] for row in profiles],
    ]
    assert manifest_path.exists() is False


def test_diagnostic_cleanup_retries_only_pending_and_retains_partial_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_ATTEMPTS", 3)
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_DELAY_SECONDS", 0)
    smoke = FakeSmokeClient()
    smoke.reset_fail_for.add("account-2")
    transient_failures = {"account-1": 1}
    original_request = smoke._req

    def partial_failure_request(method, path, *, api_key=None, body=None, **kwargs):
        if path == "/v1/account/reset" and transient_failures.get(api_key, 0) > 0:
            transient_failures[api_key] -= 1
            smoke.reset_calls.append((api_key, body))
            return 503, {"error": "deployment_rollover"}
        return original_request(method, path, api_key=api_key, body=body, **kwargs)

    monkeypatch.setattr(smoke, "_req", partial_failure_request)
    profiles = [
        {"profile_id": "p1", "user_id": "u1", "api_key": "account-1"},
        {"profile_id": "p2", "user_id": "u2", "api_key": "account-2"},
        {"profile_id": "p3", "user_id": "u3", "api_key": "account-3"},
    ]
    manifest_path = tmp_path / "diagnostic.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "qualification_mode": "diagnostic",
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": profiles,
            }
        ),
        encoding="utf-8",
    )

    result = provisioner.cleanup(manifest_path, env={}, client=smoke)

    assert result == {
        "attempted": 3,
        "cleaned": 2,
        "failed_profile_ids": ["p2"],
        "manifest_deleted": False,
        "manifest_missing": False,
    }
    reset_keys = [api_key for api_key, _body in smoke.reset_calls]
    assert reset_keys.count("account-1") == 2
    assert reset_keys.count("account-2") == 3
    assert reset_keys.count("account-3") == 1
    assert manifest_path.exists()


def test_adminless_diagnostic_cleanup_needs_no_admin_token(tmp_path):
    manifest_path = tmp_path / "diagnostic.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "qualification_mode": "diagnostic",
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {
                        "profile_id": "official-gemini",
                        "user_id": "u1",
                        "api_key": "account-1",
                    }
                ],
            }
        )
    )
    smoke = FakeSmokeClient()

    result = provisioner.cleanup(manifest_path, env={}, client=smoke)

    assert result["cleaned"] == 1
    assert result["failed_profile_ids"] == []
    assert result["manifest_deleted"] is True
    assert not manifest_path.exists()


def test_cleanup_failure_keeps_manifest_for_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_DELAY_SECONDS", 0)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {
                        "profile_id": "p1",
                        "user_id": "u1",
                        "api_key": "account-1",
                        "synthetic_account_lease": _synthetic_lease(1),
                    },
                ],
            }
        )
    )
    smoke = FakeSmokeClient()
    smoke.reset_fail_for.add("account-1")

    result = provisioner.cleanup(manifest_path, client=smoke)

    assert result["failed_profile_ids"] == ["p1"]
    assert result["manifest_deleted"] is False
    assert manifest_path.exists()


def test_cleanup_treats_already_reset_401_as_success(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {
                        "profile_id": "p1",
                        "user_id": "u1",
                        "api_key": "account-1",
                        "synthetic_account_lease": _synthetic_lease(1),
                    },
                ],
            }
        )
    )
    smoke = FakeSmokeClient()
    smoke.already_reset_for.add("account-1")
    admin = FakeAdminClient()
    admin.missing_users.add("u1")

    result = provisioner.cleanup(manifest_path, client=smoke, admin_client=admin)

    assert result["cleaned"] == 1
    assert result["failed_profile_ids"] == []
    assert result["manifest_deleted"] is True
    assert not manifest_path.exists()
    assert smoke.reset_calls == [("account-1", {"confirm": "delete-all-data"})]
    assert admin.calls == [
        (
            "POST",
            provisioner.SYNTHETIC_ABSENCE_PATH,
            {
                "user_id": "u1",
                "lease_id": _synthetic_lease(1)["lease_id"],
                "absence_token": _synthetic_lease(1)["absence_token"],
            },
        )
    ]


def test_cleanup_401_without_admin_proof_keeps_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_ATTEMPTS", 3)
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_DELAY_SECONDS", 0)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {
                        "profile_id": "p1",
                        "user_id": "u1",
                        "api_key": "account-1",
                        "synthetic_account_lease": _synthetic_lease(1),
                    },
                ],
            }
        )
    )
    smoke = FakeSmokeClient()
    smoke.already_reset_for.add("account-1")

    result = provisioner.cleanup(manifest_path, env={}, client=smoke)

    assert result["cleaned"] == 0
    assert result["failed_profile_ids"] == ["p1"]
    assert result["manifest_deleted"] is False
    assert manifest_path.exists()
    assert len(smoke.reset_calls) == 3


def test_adminless_diagnostic_cleanup_retains_ambiguous_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_DELAY_SECONDS", 0)
    manifest_path = tmp_path / "diagnostic.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "qualification_mode": "diagnostic",
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {
                        "profile_id": "official-gemini",
                        "user_id": "u1",
                        "api_key": "account-1",
                    }
                ],
            }
        )
    )
    smoke = FakeSmokeClient()
    smoke.already_reset_for.add("account-1")

    result = provisioner.cleanup(manifest_path, env={}, client=smoke)

    assert result["cleaned"] == 0
    assert result["failed_profile_ids"] == ["official-gemini"]
    assert result["manifest_deleted"] is False
    assert manifest_path.exists()


def test_cleanup_401_with_existing_admin_user_keeps_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_DELAY_SECONDS", 0)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": provisioner.ALLOWED_BASE_URL,
                "profiles": [
                    {
                        "profile_id": "p1",
                        "user_id": "u1",
                        "api_key": "account-1",
                        "synthetic_account_lease": _synthetic_lease(1),
                    },
                ],
            }
        )
    )
    smoke = FakeSmokeClient()
    smoke.already_reset_for.add("account-1")
    admin = FakeAdminClient()

    result = provisioner.cleanup(manifest_path, client=smoke, admin_client=admin)

    assert result["cleaned"] == 0
    assert result["failed_profile_ids"] == ["p1"]
    assert result["manifest_deleted"] is False
    assert manifest_path.exists()


def test_cleanup_missing_manifest_is_idempotent(tmp_path):
    result = provisioner.cleanup(tmp_path / "absent.json", client=FakeSmokeClient())
    assert result["manifest_missing"] is True
    assert result["attempted"] == 0


def test_provision_cli_succeeds_for_complete_matrix_with_blocked_profile(
    tmp_path, monkeypatch, capsys
):
    coverage = _write_coverage(tmp_path)
    manifest = tmp_path / "manifest.json"
    smoke = FakeSmokeClient()
    smoke.reject_valid_for = "user-0"
    env = _env()
    original_provision = provisioner.provision

    def injected_provision(coverage_path, manifest_path, **kwargs):
        return original_provision(
            coverage_path,
            manifest_path,
            env=env,
            client=smoke,
            admin_client=FakeAdminClient(smoke),
            runtime_requirement=kwargs.get("runtime_requirement"),
        )

    monkeypatch.setattr(provisioner, "provision", injected_provision)
    exit_code = provisioner.main(
        ["provision", "--coverage", str(coverage), "--manifest", str(manifest)]
    )
    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert output == {
        "ok": True,
        "profile_count": 9,
        "ready_profile_count": 8,
        "blocked_profile_count": 1,
        "blocked_profile_ids": ["official-deepseek"],
        "manifest": str(manifest),
    }
    raw = manifest.read_text()
    assert env["QA_DEEPSEEK_API_KEY"] not in raw
    assert [row["profile_id"] for row in json.loads(raw)["profiles"]] == list(
        provisioner.PROFILE_SPECS
    )


def test_provision_cli_supports_adminless_diagnostic_canary(
    tmp_path, monkeypatch, capsys
):
    coverage = _write_coverage(tmp_path)
    manifest = tmp_path / "diagnostic.json"
    env = _env()
    env.pop("IO_E2E_ADMIN_TOKEN")
    smoke = FakeSmokeClient()
    original_provision = provisioner.provision

    def injected_provision(coverage_path, manifest_path, **kwargs):
        return original_provision(
            coverage_path,
            manifest_path,
            env=env,
            client=smoke,
            **kwargs,
        )

    monkeypatch.setattr(provisioner, "provision", injected_provision)
    exit_code = provisioner.main(
        [
            "provision",
            "--coverage",
            str(coverage),
            "--manifest",
            str(manifest),
            "--diagnostic",
            "--profile",
            "official-gemini",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": True,
        "profile_count": 1,
        "ready_profile_count": 1,
        "blocked_profile_count": 0,
        "blocked_profile_ids": [],
        "manifest": str(manifest),
        "qualification_mode": "diagnostic",
    }


def test_provision_cli_rejects_profile_without_diagnostic_mode(
    tmp_path, monkeypatch, capsys
):
    def must_not_provision(*args, **kwargs):
        raise AssertionError("provision should not be called")

    monkeypatch.setattr(provisioner, "provision", must_not_provision)
    exit_code = provisioner.main(
        [
            "provision",
            "--coverage",
            str(_write_coverage(tmp_path)),
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--profile",
            "official-gemini",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert (
        captured.err == "provisioning error: profile subsets require diagnostic mode\n"
    )


def test_provision_cli_is_nonzero_when_registration_prevents_complete_manifest(
    tmp_path, monkeypatch, capsys
):
    coverage = _write_coverage(tmp_path)
    manifest = tmp_path / "manifest.json"
    smoke = FakeSmokeClient()
    smoke.fail_registration_at = 1
    env = _env()
    original_provision = provisioner.provision

    def injected_provision(coverage_path, manifest_path, **kwargs):
        return original_provision(
            coverage_path,
            manifest_path,
            env=env,
            client=smoke,
            admin_client=FakeAdminClient(smoke),
            runtime_requirement=kwargs.get("runtime_requirement"),
        )

    monkeypatch.setattr(provisioner, "provision", injected_provision)
    exit_code = provisioner.main(
        ["provision", "--coverage", str(coverage), "--manifest", str(manifest)]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == (
        "provisioning error: account registration failed for profile: "
        "official-anthropic\n"
    )
    assert all(secret not in captured.err for secret in env.values())
    assert not manifest.exists()


def test_cleanup_cli_emits_machine_readable_sanitized_summary(tmp_path, capsys):
    exit_code = provisioner.main(
        ["cleanup", "--manifest", str(tmp_path / "absent.json")]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output == {
        "ok": True,
        "attempted": 0,
        "cleaned": 0,
        "failed_profile_ids": [],
        "manifest_deleted": False,
    }


@pytest.mark.parametrize(
    ("remaining", "failures", "expected_code"),
    [(0, 0, 0), (1, 0, 1), (0, 1, 1)],
)
def test_cleanup_run_cli_is_manifest_independent_and_fails_closed(
    tmp_path, monkeypatch, capsys, remaining, failures, expected_code
):
    run_id = "api-key-e2e-123-1"
    receipt_path = tmp_path / "run-cleanup.json"
    observed = {}

    def injected_cleanup_run(actual_run_id, actual_receipt):
        observed.update({"run_id": actual_run_id, "receipt": actual_receipt})
        return _cleanup_run_receipt(
            actual_run_id, remaining=remaining, failures=failures
        )

    monkeypatch.setattr(provisioner, "cleanup_run", injected_cleanup_run)
    exit_code = provisioner.main(
        [
            "cleanup-run",
            "--run-id",
            run_id,
            "--receipt",
            str(receipt_path),
        ]
    )

    assert exit_code == expected_code
    assert observed == {"run_id": run_id, "receipt": receipt_path}
    assert json.loads(capsys.readouterr().out) == {
        "ok": expected_code == 0,
        "matched_count": max(remaining, failures),
        "deleted_count": 0,
        "operation_failure_count": failures,
        "remaining_count": remaining,
        "receipt": str(receipt_path),
    }


@pytest.mark.parametrize("count", [1, 8, 24, 27])
def test_provision_pool_creates_strict_same_route_accounts(tmp_path, count):
    from qa.regression.live_accounts import load_account_pool

    coverage = _write_coverage(tmp_path)
    manifest_path = tmp_path / "pool.json"
    profile_id = "official-openai"
    selected_spec = provisioner.PROFILE_SPECS[profile_id]
    env = _env()
    for other_id, other_spec in provisioner.PROFILE_SPECS.items():
        if other_id == profile_id:
            continue
        env.pop(other_spec.model_env, None)
        if other_spec.credential_env != selected_spec.credential_env:
            env.pop(other_spec.credential_env, None)
        if other_spec.base_url_env:
            env.pop(other_spec.base_url_env, None)
    smoke = FakeSmokeClient()
    admin = FakeAdminClient(smoke)

    result = provisioner.provision_pool(
        coverage,
        manifest_path,
        profile_id=profile_id,
        count=count,
        env=env,
        client=smoke,
        admin_client=admin,
    )

    assert provisioner._complete_pool_manifest(result) is True
    assert result["manifest_kind"] == "persona_memory_account_pool"
    assert result["pool_profile_id"] == profile_id
    assert result["pool_count"] == count
    assert result["auxiliary_accounts"] == []
    assert len(result["profiles"]) == count
    assert len(smoke.registered) == count
    assert len(smoke.setup_calls) == count * 2
    assert len(smoke.trace_calls) == count
    assert len(smoke.runtime_calls) == count
    assert len(admin.calls) == 1 + count
    assert not any("hosted-runtime-mode" in path for _method, path, _body in admin.calls)
    assert [row["pool_index"] for row in result["profiles"]] == list(
        range(1, count + 1)
    )
    assert {row["profile_id"] for row in result["profiles"]} == {profile_id}
    assert len({row["label"] for row in result["profiles"]}) == count
    assert len({row["user_id"] for row in result["profiles"]}) == count
    assert len({row["api_key"] for row in result["profiles"]}) == count
    assert len(
        {
            row["synthetic_account_lease"]["lease_id"]
            for row in result["profiles"]
        }
    ) == count
    assert all(
        row["provider"] == selected_spec.provider
        and row["configured_model"] == VALID_MODELS[profile_id]
        and row["configured_base_url"] == selected_spec.expected_configured_base_url
        and row["runtime_mode"] == provisioner.RUNTIME_V2_REQUIREMENT
        and row["runtime_version"] == provisioner.RUNTIME_V2_VERSION
        and row["runtime_mode_set_required"] is False
        and row["runtime_mode_set_verified"] is False
        and provisioner._synthetic_absence_attestation_valid(row)
        and row["provision_status"] == provisioner.PROVISION_STATUS_READY
        for row in result["profiles"]
    )
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    live_pool = load_account_pool(manifest_path)
    assert len(live_pool.rows) == count
    assert live_pool.profile_id == profile_id
    raw = manifest_path.read_text()
    assert env[selected_spec.credential_env] not in raw
    assert env["IO_E2E_ADMIN_TOKEN"] not in raw


def test_pool_cleanup_emits_per_account_authoritative_sanitized_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_DELAY_SECONDS", 0)
    smoke = FakeSmokeClient()
    admin = FakeAdminClient(smoke)
    manifest_path = tmp_path / "pool-cleanup.json"
    manifest = provisioner.provision_pool(
        _write_coverage(tmp_path),
        manifest_path,
        profile_id="official-openai",
        count=2,
        env=_env(),
        client=smoke,
        admin_client=admin,
    )

    result = provisioner.cleanup(
        manifest_path,
        client=smoke,
        admin_client=admin,
        delete_manifest=False,
    )

    assert result["attempted"] == 2
    assert result["cleaned"] == 2
    assert result["failed_profile_ids"] == []
    assert result["manifest_retained"] is True
    assert [row["pool_index"] for row in result["cleanup_accounts"]] == [1, 2]
    assert all(
        row["status"] == "PASS"
        and row["provider_config_deleted"] is True
        and row["key_envelope_deleted"] is True
        and row["account_reset"] is True
        and row["old_credential_rejected"] is True
        and row["user_absence_verified"] is True
        for row in result["cleanup_accounts"]
    )
    serialized = json.dumps(result, sort_keys=True)
    for entry in manifest["profiles"]:
        assert entry["user_id"] not in serialized
        assert entry["api_key"] not in serialized
        assert entry["synthetic_account_lease"]["absence_token"] not in serialized
    assert not any("data-track" in path for _method, path, _body in admin.calls)


def test_pool_cleanup_retry_uses_db_absence_and_keeps_recovery_manifest(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_ATTEMPTS", 1)
    monkeypatch.setattr(provisioner, "CLEANUP_EVIDENCE_DELAY_SECONDS", 0)
    smoke = FakeSmokeClient()
    admin = FakeAdminClient(smoke)
    manifest_path = tmp_path / "pool-cleanup-retry.json"
    provisioner.provision_pool(
        _write_coverage(tmp_path),
        manifest_path,
        profile_id="official-openai",
        count=2,
        env=_env(),
        client=smoke,
        admin_client=admin,
    )
    admin.user_lookup_status = 503

    first = provisioner.cleanup(
        manifest_path,
        client=smoke,
        admin_client=admin,
    )

    assert first["cleaned"] == 0
    assert first["manifest_deleted"] is False
    assert manifest_path.is_file()
    assert all(row["status"] == "FAIL" for row in first["cleanup_accounts"])

    admin.user_lookup_status = None
    second = provisioner.cleanup(
        manifest_path,
        client=smoke,
        admin_client=admin,
    )

    assert second["cleaned"] == 2
    assert second["failed_profile_ids"] == []
    assert second["manifest_deleted"] is True
    assert manifest_path.exists() is False
    assert all(
        row["status"] == "PASS"
        and row["provider_config_deletion_source"] == "account_cascade"
        for row in second["cleanup_accounts"]
    )


def test_provision_pool_supports_strict_baseline_runtime(tmp_path):
    from qa.regression.live_accounts import load_account_pool

    smoke = FakeSmokeClient()
    admin = FakeAdminClient(smoke)
    manifest_path = tmp_path / "pool.json"

    result = provisioner.provision_pool(
        _write_coverage(tmp_path),
        manifest_path,
        profile_id="official-gemini",
        count=2,
        env=_env(),
        client=smoke,
        admin_client=admin,
        runtime_requirement=provisioner.BASELINE_RUNTIME_REQUIREMENT,
    )

    assert provisioner._complete_pool_manifest(result) is True
    assert result["runtime_mode"] == provisioner.BASELINE_RUNTIME_REQUIREMENT
    assert admin.modes == {}
    assert all(
        row["runtime_mode"] == provisioner.DIAGNOSTIC_RUNTIME_MODE
        and row["runtime_version"] == provisioner.DIAGNOSTIC_RUNTIME_VERSION
        and row["runtime_mode_set_required"] is False
        and row["runtime_mode_set_verified"] is False
        for row in result["profiles"]
    )
    live_pool = load_account_pool(manifest_path)
    assert live_pool.deployment_runtime == provisioner.BASELINE_RUNTIME_REQUIREMENT


@pytest.mark.parametrize("count", [True, 0, -1, 28])
def test_provision_pool_rejects_invalid_count_before_external_state(tmp_path, count):
    smoke = FakeSmokeClient()
    admin = FakeAdminClient(smoke)

    with pytest.raises(provisioner.ProvisionError, match="pool count"):
        provisioner.provision_pool(
            _write_coverage(tmp_path),
            tmp_path / "pool.json",
            profile_id="official-openai",
            count=count,
            env=_env(),
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []
    assert admin.calls == []


def test_provision_pool_rejects_unknown_profile_before_external_state(tmp_path):
    smoke = FakeSmokeClient()
    admin = FakeAdminClient(smoke)

    with pytest.raises(provisioner.ProvisionError, match="outside the locked"):
        provisioner.provision_pool(
            _write_coverage(tmp_path),
            tmp_path / "pool.json",
            profile_id="unknown-route",
            count=8,
            env=_env(),
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []
    assert admin.calls == []


@pytest.mark.parametrize("unsafe", ["existing", "public-parent", "relative"])
def test_provision_pool_rejects_unsafe_manifest_path_before_external_state(
    tmp_path, unsafe
):
    smoke = FakeSmokeClient()
    admin = FakeAdminClient(smoke)
    if unsafe == "existing":
        manifest = tmp_path / "pool.json"
        manifest.write_text("occupied", encoding="utf-8")
    elif unsafe == "public-parent":
        parent = tmp_path / "public"
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
        manifest = parent / "pool.json"
    else:
        manifest = Path("relative-pool.json")

    with pytest.raises(provisioner.ProvisionError, match="pool manifest"):
        provisioner.provision_pool(
            _write_coverage(tmp_path),
            manifest,
            profile_id="official-openai",
            count=2,
            env=_env(),
            client=smoke,
            admin_client=admin,
        )

    assert smoke.registered == []
    assert admin.calls == []


def test_provision_pool_blocked_account_cleans_entire_pool(tmp_path):
    smoke = FakeSmokeClient()
    smoke.reject_valid_for = "user-1"
    admin = FakeAdminClient(smoke)
    manifest = tmp_path / "pool.json"

    with pytest.raises(
        provisioner.ProvisionError,
        match="pool account provisioning blocked: VALID_KEY_REJECTED",
    ):
        provisioner.provision_pool(
            _write_coverage(tmp_path),
            manifest,
            profile_id="official-openai",
            count=3,
            env=_env(),
            client=smoke,
            admin_client=admin,
        )

    assert len(smoke.registered) == 2
    assert [call[0] for call in smoke.reset_calls] == [
        "feedling-account-key-0",
        "feedling-account-key-1",
    ]
    assert not manifest.exists()


def test_provision_pool_cleanup_failure_retains_aggregate_manifest(tmp_path):
    smoke = FakeSmokeClient()
    smoke.reject_valid_for = "user-1"
    smoke.reset_fail_for.add("feedling-account-key-0")
    manifest = tmp_path / "pool.json"

    with pytest.raises(provisioner.ProvisionError, match="VALID_KEY_REJECTED"):
        provisioner.provision_pool(
            _write_coverage(tmp_path),
            manifest,
            profile_id="official-openai",
            count=3,
            env=_env(),
            client=smoke,
            admin_client=FakeAdminClient(smoke),
        )

    assert manifest.exists()
    persisted = json.loads(manifest.read_text())
    assert persisted["manifest_kind"] == "persona_memory_account_pool"
    assert [row["pool_index"] for row in persisted["profiles"]] == [1, 2]
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


def test_provision_pool_registration_failure_cleans_prior_accounts(tmp_path):
    smoke = FakeSmokeClient()
    smoke.fail_registration_at = 1
    manifest = tmp_path / "pool.json"

    with pytest.raises(
        provisioner.ProvisionError,
        match="pool account registration failed at index: 2",
    ):
        provisioner.provision_pool(
            _write_coverage(tmp_path),
            manifest,
            profile_id="official-openai",
            count=3,
            env=_env(),
            client=smoke,
            admin_client=FakeAdminClient(smoke),
        )

    assert [call[0] for call in smoke.reset_calls] == ["feedling-account-key-0"]
    assert not manifest.exists()


def test_provision_pool_checkpoint_failure_cleans_unpersisted_account(
    tmp_path, monkeypatch
):
    smoke = FakeSmokeClient()
    manifest_path = tmp_path / "pool.json"
    original_write = provisioner._atomic_write_manifest
    injected = False

    def fail_second_account_first_checkpoint(path, manifest):
        nonlocal injected
        if len(manifest.get("profiles", [])) == 2 and not injected:
            injected = True
            raise OSError("injected checkpoint failure")
        return original_write(path, manifest)

    monkeypatch.setattr(
        provisioner, "_atomic_write_manifest", fail_second_account_first_checkpoint
    )

    with pytest.raises(OSError, match="injected checkpoint failure"):
        provisioner.provision_pool(
            _write_coverage(tmp_path),
            manifest_path,
            profile_id="official-openai",
            count=3,
            env=_env(),
            client=smoke,
            admin_client=FakeAdminClient(smoke),
        )

    assert injected is True
    assert len(smoke.registered) == 2
    assert [call[0] for call in smoke.reset_calls] == [
        "feedling-account-key-0",
        "feedling-account-key-1",
    ]
    assert not manifest_path.exists()


def test_provision_pool_retains_unpersisted_account_when_reset_fails(
    tmp_path, monkeypatch
):
    smoke = FakeSmokeClient()
    smoke.reset_fail_for.add("feedling-account-key-1")
    manifest_path = tmp_path / "pool.json"
    original_write = provisioner._atomic_write_manifest
    injected = False

    def fail_second_account_first_checkpoint(path, manifest):
        nonlocal injected
        if len(manifest.get("profiles", [])) == 2 and not injected:
            injected = True
            raise OSError("injected checkpoint failure")
        return original_write(path, manifest)

    monkeypatch.setattr(
        provisioner, "_atomic_write_manifest", fail_second_account_first_checkpoint
    )

    with pytest.raises(OSError, match="injected checkpoint failure"):
        provisioner.provision_pool(
            _write_coverage(tmp_path),
            manifest_path,
            profile_id="official-openai",
            count=3,
            env=_env(),
            client=smoke,
            admin_client=FakeAdminClient(smoke),
        )

    assert manifest_path.is_file()
    retained = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [row["pool_index"] for row in retained["profiles"]] == [1, 2]
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600


def test_provision_pool_cli_emits_sanitized_summary(tmp_path, monkeypatch, capsys):
    coverage = _write_coverage(tmp_path)
    manifest = tmp_path / "pool.json"
    env = _env()
    smoke = FakeSmokeClient()
    original = provisioner.provision_pool

    def injected_pool(coverage_path, manifest_path, **kwargs):
        return original(
            coverage_path,
            manifest_path,
            env=env,
            client=smoke,
            admin_client=FakeAdminClient(smoke),
            **kwargs,
        )

    monkeypatch.setattr(provisioner, "provision_pool", injected_pool)
    exit_code = provisioner.main(
        [
            "provision-pool",
            "--coverage",
            str(coverage),
            "--manifest",
            str(manifest),
            "--profile",
            "official-openai",
            "--count",
            "2",
            "--baseline-runtime",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": True,
        "manifest_kind": "persona_memory_account_pool",
        "pool_profile_id": "official-openai",
        "pool_count": 2,
        "manifest": str(manifest),
    }
    assert env["QA_OPENAI_PROVIDER_API_KEY"] not in captured.out
    assert env["IO_E2E_ADMIN_TOKEN"] not in captured.out
