"""in-CVM LiteLLM gateway — per-user config + env generation (codex non-openai).

Codex speaks OpenAI Responses only, so non-openai providers (gemini/openrouter/
openai_compatible) are bridged through a LiteLLM proxy the supervisor runs inside
the CVM. This module builds, per gateway user:
  - a LiteLLM ``model_list`` entry named ``gw-<user_id>`` (the model codex
    requests) routing to the real provider, with the upstream key referenced by
    env var (``os.environ/...``) — NEVER inlined into the on-disk config;
  - the {env_var: upstream_key} map the supervisor injects into the LiteLLM
    subprocess environment (keys stay in memory, never persisted).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent_runtime import litellm_gateway as gw


def test_gateway_model_id_and_env_var_are_per_user_and_sanitized():
    assert gw.gateway_model_id("usr_abc") == "gw-usr_abc"
    # env var names must be shell/env-safe (user ids are usr_<base32>, already safe,
    # but be defensive about any stray chars)
    assert gw.upstream_env_var("usr_abc") == "FEEDLING_UPKEY_usr_abc"
    assert gw.upstream_env_var("usr-a.b/c") == "FEEDLING_UPKEY_usr_a_b_c"


def test_model_entry_references_key_by_env_not_inline():
    e = gw.build_model_entry(user_id="usr_1", provider="gemini", model="gemini-2.0-flash")
    assert e["model_name"] == "gw-usr_1"
    assert e["litellm_params"]["model"] == "gemini/gemini-2.0-flash"
    # the upstream key is an env reference, never the plaintext
    assert e["litellm_params"]["api_key"] == "os.environ/FEEDLING_UPKEY_usr_1"


def test_model_entry_openrouter_prefix():
    e = gw.build_model_entry(user_id="u", provider="openrouter", model="anthropic/claude-3.5-sonnet")
    assert e["litellm_params"]["model"] == "openrouter/anthropic/claude-3.5-sonnet"


def test_model_entry_openrouter_no_reasoning_by_default():
    # Reasoning is OFF by default (end-to-end path not yet verified); the gateway
    # sends no reasoning extra_body until explicitly enabled via env/per-user.
    e = gw.build_model_entry(user_id="u", provider="openrouter", model="deepseek/deepseek-v4-flash")

    assert "extra_body" not in e["litellm_params"]


def test_model_entry_openrouter_non_anthropic_uses_effort_when_on():
    # Non-Anthropic OpenRouter model families still emit reasoning with effort.
    e = gw.build_model_entry(
        user_id="u", provider="openrouter", model="z-ai/glm-5.2", reasoning_effort="medium",
    )
    assert e["litellm_params"]["extra_body"]["reasoning"] == {
        "effort": "medium",
        "summary": "auto",
    }


def test_model_entry_openrouter_anthropic_uses_effort_on_responses_wire():
    # codex speaks the Responses wire and OpenRouter serves it natively; the
    # Responses `reasoning` object takes {effort, summary}; summary:auto asks
    # OpenRouter Anthropic for visible reasoning summary text instead of only
    # encrypted_content (probed 2026-07-11 against /responses). So Anthropic-family
    # models emit the same request shape as every other OpenRouter family.
    e = gw.build_model_entry(
        user_id="u", provider="openrouter", model="anthropic/claude-sonnet-4.6",
        reasoning_effort="medium",
    )
    assert e["litellm_params"]["extra_body"]["reasoning"] == {
        "effort": "medium",
        "summary": "auto",
    }
    assert "max_tokens" not in e["litellm_params"]["extra_body"]["reasoning"]


def test_model_entry_openrouter_non_enum_effort_falls_back_to_medium():
    # The Responses `reasoning.effort` only accepts low/medium/high; a legacy
    # numeric value (or anything else) falls back to medium rather than emitting
    # an invalid effort the wire would reject.
    e = gw.build_model_entry(
        user_id="u", provider="openrouter", model="anthropic/claude-sonnet-4.6",
        reasoning_effort="9999",
    )
    assert e["litellm_params"]["extra_body"]["reasoning"] == {
        "effort": "medium",
        "summary": "auto",
    }


def test_model_entry_openrouter_per_user_effort_override():
    # Per-user override (future iOS reasoning toggle) wins over the default.
    e = gw.build_model_entry(
        user_id="u", provider="openrouter", model="m", reasoning_effort="medium",
    )
    assert e["litellm_params"]["extra_body"]["reasoning"]["effort"] == "medium"


def test_model_entry_openrouter_reasoning_off_sends_no_reasoning():
    # A per-user "off" disables reasoning entirely (no extra_body sent).
    e = gw.build_model_entry(
        user_id="u", provider="openrouter", model="m", reasoning_effort="off",
    )
    assert "extra_body" not in e["litellm_params"]


def test_build_config_threads_per_user_reasoning_effort():
    cfg = gw.build_config([
        {"user_id": "u1", "provider": "openrouter", "model": "anthropic/claude-sonnet-4.6", "reasoning_effort": "low"},
        {"user_id": "u2", "provider": "openrouter", "model": "m", "reasoning_effort": "off"},
    ])
    entries = {e["model_name"]: e["litellm_params"] for e in cfg["model_list"]}
    assert entries["gw-u1"]["extra_body"]["reasoning"]["effort"] == "low"
    assert entries["gw-u1"]["extra_body"]["reasoning"]["summary"] == "auto"
    assert "extra_body" not in entries["gw-u2"]


def test_model_entry_openai_compatible_carries_api_base():
    e = gw.build_model_entry(
        user_id="u", provider="openai_compatible", model="my-model",
        base_url="https://my.host/v1",
    )
    assert e["litellm_params"]["model"] == "openai/my-model"
    assert e["litellm_params"]["api_base"] == "https://my.host/v1"


def test_model_entry_openai_compatible_bridges_responses_to_chat():
    # codex only speaks the Responses wire (POST /v1/responses), but the
    # openai_compatible relays only implement /chat/completions. LiteLLM treats
    # provider=openai as natively Responses-capable and would passthrough → 500.
    # The first-class use_chat_completions_api flag forces LiteLLM's
    # responses→chat-completions bridge.
    e = gw.build_model_entry(
        user_id="u", provider="openai_compatible", model="my-model",
        base_url="https://my.host/v1",
    )
    assert e["litellm_params"]["use_chat_completions_api"] is True


def test_model_entry_non_openai_compatible_has_no_chat_bridge_flag():
    # gemini/openrouter are already correct (bridge or native); must NOT carry
    # the flag, so we don't regress them.
    for prov, model in [("gemini", "gemini-2.0-flash"),
                        ("openrouter", "anthropic/claude-3.5-sonnet")]:
        e = gw.build_model_entry(user_id="u", provider=prov, model=model)
        assert "use_chat_completions_api" not in e["litellm_params"]


def test_model_entry_openai_compatible_native_when_relay_supports_responses():
    # A relay that natively implements /v1/responses (e.g. gemai.cc) must NOT be
    # forced through the chat-completions bridge — the bridge breaks codex's tool
    # loop. Pass it through to its native /responses (no flag), keeping api_base.
    e = gw.build_model_entry(
        user_id="u", provider="openai_compatible", model="gpt-5.4",
        base_url="https://my.host/v1", supports_responses=True,
    )
    assert "use_chat_completions_api" not in e["litellm_params"]
    assert e["litellm_params"]["api_base"] == "https://my.host/v1"


def test_model_entry_openai_compatible_bridges_when_relay_lacks_responses():
    # A chat-only relay (no /v1/responses) is forced through the bridge.
    e = gw.build_model_entry(
        user_id="u", provider="openai_compatible", model="m",
        base_url="https://my.host/v1", supports_responses=False,
    )
    assert e["litellm_params"]["use_chat_completions_api"] is True


def test_build_config_threads_supports_responses_per_entry():
    cfg = gw.build_config([
        {"user_id": "native", "provider": "openai_compatible", "model": "gpt-5.4",
         "base_url": "https://a.host/v1", "supports_responses": True},
        {"user_id": "bridge", "provider": "openai_compatible", "model": "m",
         "base_url": "https://b.host/v1", "supports_responses": False},
    ])
    by_id = {e["model_name"]: e["litellm_params"] for e in cfg["model_list"]}
    assert "use_chat_completions_api" not in by_id["gw-native"]
    assert by_id["gw-bridge"]["use_chat_completions_api"] is True


def test_config_signature_changes_with_supports_responses():
    base = {"user_id": "u", "provider": "openai_compatible", "model": "m",
            "base_url": "https://h/v1"}
    sig_bridge = gw.config_signature([{**base, "supports_responses": False}])
    sig_native = gw.config_signature([{**base, "supports_responses": True}])
    assert sig_bridge != sig_native


def test_config_signature_changes_with_reasoning_effort():
    base = {"user_id": "u", "provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"}
    sig_default = gw.config_signature([base])
    sig_medium = gw.config_signature([{**base, "reasoning_effort": "medium"}])
    assert sig_default != sig_medium


def test_build_config_preserves_reasoning_params_and_sets_master_key_env():
    cfg = gw.build_config([
        {"user_id": "u1", "provider": "gemini", "model": "gemini-2.0-flash"},
    ])
    settings = cfg["litellm_settings"]
    assert settings["drop_params"] is True
    # Codex uses OpenAI Responses reasoning params; if the gateway strips them,
    # OpenRouter/Gemini/openai-compatible models never get a chance to emit
    # provider-native thinking summaries for the iOS disclosure.
    for p in ("reasoning", "reasoning_effort"):
        assert p not in settings["additional_drop_params"]
    # Keep dropping Anthropic-only thinking blocks for non-Anthropic gateway
    # backends; native Anthropic/DeepSeek thinking goes through the claude driver.
    assert "thinking" in settings["additional_drop_params"]
    # codex authenticates with the gateway key; master_key is an env reference
    assert cfg["general_settings"]["master_key"] == "os.environ/FEEDLING_LITELLM_API_KEY"
    assert [m["model_name"] for m in cfg["model_list"]] == ["gw-u1"]


def test_openai_compatible_entry_drops_web_search_options_per_model():
    """codex `exec` always ships a `web_search` tool and offers no switch to turn
    it off (checked on codex-cli 0.142.4: `[tools] web_search = false`,
    `-c tools.web_search=false` and `--disable web_search_request` all leave it on
    the wire). The responses→chat bridge lifts that tool out of `tools` into a
    top-level `web_search_options` with an empty `user_location`; relays then
    re-materialize it as Anthropic's `web_search_20250305` and the upstream 400s
    (`tools.8.web_search_20250305.user_location: At least one field must be
    specified.`), killing the turn before the model runs.

    The drop MUST live in litellm_params, not litellm_settings: verified against
    litellm 1.89.4 that `get_optional_params` reads `additional_drop_params` from
    the per-call kwargs only and never falls back to the module global, so a
    `litellm_settings` entry is a silent no-op."""
    entry = gw.build_model_entry(
        user_id="u1", provider="openai_compatible", model="claude-sonnet-4-6",
        base_url="https://relay/v1")
    assert entry["litellm_params"]["additional_drop_params"] == ["web_search_options"]


def test_web_search_drop_is_scoped_to_openai_compatible():
    """Other gateway providers keep web search: only the openai_compatible relays
    have been observed 400ing on it."""
    for provider in ("openrouter", "gemini"):
        entry = gw.build_model_entry(user_id="u1", provider=provider, model="m")
        assert "additional_drop_params" not in entry["litellm_params"], provider


def test_build_config_never_inlines_upstream_keys():
    # the on-disk config must not contain any plaintext provider key
    cfg = gw.build_config([
        {"user_id": "u1", "provider": "gemini", "model": "m", "provider_key": "SECRET_KEY_123"},
    ])
    assert "SECRET_KEY_123" not in gw.render_config_yaml(cfg)


def test_render_config_is_json_valid_yaml_subset_round_trips():
    # Rendered as JSON (a strict YAML subset) so the module needs NO PyYAML
    # dependency at import time; LiteLLM's yaml.safe_load parses it the same.
    cfg = gw.build_config([{"user_id": "u1", "provider": "gemini", "model": "m"}])
    text = gw.render_config_yaml(cfg)
    assert json.loads(text) == cfg


def test_upstream_env_maps_env_var_to_decrypted_key():
    env = gw.upstream_env([
        {"user_id": "u1", "provider": "gemini", "model": "m", "provider_key": "k1"},
        {"user_id": "u2", "provider": "openrouter", "model": "m2", "provider_key": "k2"},
    ])
    assert env == {"FEEDLING_UPKEY_u1": "k1", "FEEDLING_UPKEY_u2": "k2"}
    # entries without a resolved key are skipped (can't route them)
    assert gw.upstream_env([{"user_id": "u3", "provider": "gemini", "model": "m"}]) == {}


def test_config_signature_changes_with_model_or_base_url():
    a = gw.config_signature([{"user_id": "u1", "provider": "gemini", "model": "m"}])
    b = gw.config_signature([{"user_id": "u1", "provider": "gemini", "model": "m2"}])
    assert a != b


# ---- GatewayManager (subprocess lifecycle, injected launcher/stopper) ----


class _FakeProc:
    """A launcher handle with a Popen-like ``poll`` so the manager can detect a
    crashed proxy. ``poll()`` returns None while alive, an exit code once dead."""
    def __init__(self, name):
        self.name = name
        self._rc = None

    def crash(self, rc=1):
        self._rc = rc

    def poll(self):
        return self._rc


class _FakeLauncher:
    def __init__(self):
        self.calls = []
        self.stopped = []
        self._n = 0

    def launch(self, config_path, env, port):
        self._n += 1
        h = _FakeProc(f"h{self._n}")
        self.calls.append({"config_path": config_path, "env": dict(env), "port": port, "handle": h})
        return h

    def stop(self, handle):
        self.stopped.append(handle.name)


def _mgr(tmp_path, fake, writes):
    return gw.GatewayManager(
        config_path=str(tmp_path / "litellm.yaml"), port=4123,
        launcher=fake.launch, stopper=fake.stop,
        writer=lambda p, c: writes.append((p, c)),
    )


def test_manager_writes_config_and_launches_with_key_env(tmp_path):
    fake = _FakeLauncher(); writes = []
    mgr = _mgr(tmp_path, fake, writes)
    mgr.reconcile([{"user_id": "u1", "provider": "gemini", "model": "m", "provider_key": "k1"}])
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["port"] == 4123
    # the decrypted upstream key is injected into the subprocess env (not on disk)
    assert call["env"]["FEEDLING_UPKEY_u1"] == "k1"
    # config written, and it references the key by env (no plaintext)
    assert writes and "k1" not in writes[-1][1]
    assert "gw-u1" in writes[-1][1]


def test_manager_no_relaunch_when_nothing_changed(tmp_path):
    fake = _FakeLauncher(); writes = []
    mgr = _mgr(tmp_path, fake, writes)
    entries = [{"user_id": "u1", "provider": "gemini", "model": "m", "provider_key": "k1"}]
    mgr.reconcile(entries)
    mgr.reconcile([dict(entries[0])])  # identical routing AND key, proxy alive
    assert len(fake.calls) == 1  # nothing changed → no bounce


def test_manager_relaunches_on_upstream_key_rotation(tmp_path):
    # The routing signature excludes keys (to avoid bouncing on rotation), but the
    # key is only injected at launch — so a rotation MUST relaunch, else the proxy
    # keeps using the stale key.
    fake = _FakeLauncher(); writes = []
    mgr = _mgr(tmp_path, fake, writes)
    mgr.reconcile([{"user_id": "u1", "provider": "gemini", "model": "m", "provider_key": "k1"}])
    mgr.reconcile([{"user_id": "u1", "provider": "gemini", "model": "m", "provider_key": "rotated"}])
    assert len(fake.calls) == 2
    assert fake.calls[-1]["env"]["FEEDLING_UPKEY_u1"] == "rotated"  # fresh key injected
    assert fake.stopped == ["h1"]


def test_manager_relaunches_when_crashed_even_if_unchanged(tmp_path):
    # If the proxy crashed, a same-signature reconcile must NOT no-op — it has to
    # bring the proxy back, or all gateway users stay broken until supervisor restart.
    fake = _FakeLauncher(); writes = []
    mgr = _mgr(tmp_path, fake, writes)
    entries = [{"user_id": "u1", "provider": "gemini", "model": "m", "provider_key": "k1"}]
    mgr.reconcile(entries)
    fake.calls[0]["handle"].crash()  # proxy dies
    mgr.reconcile([dict(entries[0])])
    assert len(fake.calls) == 2  # relaunched the dead proxy
    assert fake.stopped == ["h1"]


def test_manager_relaunches_when_user_set_changes(tmp_path):
    fake = _FakeLauncher(); writes = []
    mgr = _mgr(tmp_path, fake, writes)
    mgr.reconcile([{"user_id": "u1", "provider": "gemini", "model": "m", "provider_key": "k1"}])
    mgr.reconcile([{"user_id": "u1", "provider": "gemini", "model": "m", "provider_key": "k1"},
                   {"user_id": "u2", "provider": "openrouter", "model": "m2", "provider_key": "k2"}])
    assert len(fake.calls) == 2
    assert fake.stopped == ["h1"]  # old proxy stopped before relaunch


def test_default_launch_uses_isolated_litellm_console_script(monkeypatch):
    # LiteLLM lives in its own venv (so its dep tree never disturbs the hash-locked
    # backend env). _default_launch must invoke the ``litellm`` console script from
    # THAT venv's bin dir — not ``python -m litellm`` (litellm has no __main__, so
    # -m aborts at startup and the proxy never binds its port).
    captured = {}
    monkeypatch.setattr(gw.subprocess, "Popen",
                        lambda argv, env: captured.update(argv=argv, env=env) or "proc")
    monkeypatch.setenv("FEEDLING_LITELLM_PYTHON", "/opt/litellm-venv/bin/python")
    gw._default_launch("/cfg.yaml", {"FEEDLING_UPKEY_u1": "k1"}, 4123)
    assert captured["argv"][0] == "/opt/litellm-venv/bin/litellm"
    assert captured["argv"][1:3] == ["--config", "/cfg.yaml"]
    assert "-m" not in captured["argv"]  # regression guard: never `python -m litellm`
    assert "--port" in captured["argv"] and "4123" in captured["argv"]
    # the upstream key is injected into the proxy env
    assert captured["env"]["FEEDLING_UPKEY_u1"] == "k1"


def test_default_launch_scrubs_database_url_from_litellm_env(monkeypatch):
    # LiteLLM proxy auto-enables a Prisma/Postgres-backed store the moment it sees
    # DATABASE_URL in its env, then crashes at startup ("No module named 'prisma'")
    # — the proxy venv ships no prisma and this gateway is a stateless router. The
    # supervisor's own DATABASE_URL (RDS, for leases) must NOT leak into the child,
    # or every gateway turn dies in a litellm crash-loop.
    captured = {}
    monkeypatch.setattr(gw.subprocess, "Popen",
                        lambda argv, env: captured.update(argv=argv, env=env) or "proc")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host:5432/db?sslmode=require")
    monkeypatch.setenv("LITELLM_DATABASE_URL", "postgresql://u:p@host:5432/db")
    gw._default_launch("/cfg.yaml", {"FEEDLING_UPKEY_u1": "k1"}, 4123)
    assert "DATABASE_URL" not in captured["env"]
    assert "LITELLM_DATABASE_URL" not in captured["env"]
    # upstream keys (and other inherited env) still flow through
    assert captured["env"]["FEEDLING_UPKEY_u1"] == "k1"


def test_manager_stops_proxy_when_no_gateway_users(tmp_path):
    fake = _FakeLauncher(); writes = []
    mgr = _mgr(tmp_path, fake, writes)
    mgr.reconcile([{"user_id": "u1", "provider": "gemini", "model": "m", "provider_key": "k1"}])
    mgr.reconcile([])
    assert fake.stopped == ["h1"]
    assert len(fake.calls) == 1


def test_config_signature_changes_with_user_set():
    a = gw.config_signature([{"user_id": "u1", "provider": "gemini", "model": "m"}])
    b = gw.config_signature([{"user_id": "u1", "provider": "gemini", "model": "m"},
                             {"user_id": "u2", "provider": "openrouter", "model": "m2"}])
    same = gw.config_signature([{"user_id": "u1", "provider": "gemini", "model": "m"}])
    assert a == same
    assert a != b
    # signature must NOT depend on the secret key value (keys live in env)
    c = gw.config_signature([{"user_id": "u1", "provider": "gemini", "model": "m", "provider_key": "x"}])
    assert a == c
