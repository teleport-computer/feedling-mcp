"""io_cli identity-write — full field set (spec 3.1).

Pure parser/payload tests: 9 string fields, 4 list fields (add/remove/replace
+ --signature's legacy whole-replace), and 七维 --nudge-dimension. No network,
no DB — see conftest.py's _PURE_UNIT whitelist (this module is listed there so
a no-Postgres dev machine still collects and runs it).

The hosted agent uses `io_cli.py identity-write` to patch its own identity
card via /v1/identity/actions (identity.profile_patch [+ identity.dimension_nudge]).
The server does the crypto (decrypt existing -> merge -> re-encrypt); the CLI's
job is (a) shape the action body and (b) front-run the local pre-checks the
server itself enforces or silently swallows (rename pairing / list-op conflict
/ nudge format+cap / I4: total action count <=10 — the server slices
actions[:10] WITHOUT erroring, so a would-be-11th nudge silently never runs
while the CLI still sees a 200; we reject >10 locally instead), so a malformed
call fails fast instead of round-tripping for nothing or worse, "succeeding"
with a silently dropped action.
"""
import argparse
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import io_cli  # noqa: E402

IO_CLI = str(TOOLS / "io_cli.py")


def _ns(**overrides) -> argparse.Namespace:
    """A full identity-write Namespace with every field at its argparse
    default, overridden per-test. Mirrors the real parser 1:1 so a typo here
    would also be a typo in the real --dest names."""
    base = dict(
        agent_name=None, self_introduction=None, category=None,
        user_preferred_name=None, agent_role=None, tone_style=None,
        custom_persona_prompt=None, language_preference=None,
        relationship_anchor=None,
        signature=[], add_signature=[], remove_signature=[], replace_signatures=[],
        add_boundary=[], remove_boundary=[], replace_boundaries=[],
        add_do_not_say=[], remove_do_not_say=[], replace_do_not_say=[],
        add_stable_definition=[], remove_stable_definition=[], replace_stable_definitions=[],
        nudge_dimension=[],
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# _identity_write_payload_v2 — pure builder, happy paths
# ---------------------------------------------------------------------------

def test_full_string_fields_land_in_patch():
    payload = io_cli._identity_write_payload_v2(_ns(user_preferred_name="老张"))
    assert payload["actions"][0]["patch"] == {"user_preferred_name": "老张"}


def test_every_string_field_lands_under_its_own_key():
    """Loop coverage for all 9 string fields (spec 3.1) — each one alone must
    land in the patch under its own name, nothing dropped, nothing renamed."""
    for field in io_cli._STRING_FIELDS:
        if field == "agent_name":
            # agent_name alone trips the D4 rename-pairing pre-check; covered
            # separately below, so give it a paired self_introduction here.
            payload = io_cli._identity_write_payload_v2(
                _ns(agent_name="老6", self_introduction="我是老6"))
            assert payload["actions"][0]["patch"] == {
                "agent_name": "老6", "self_introduction": "我是老6",
            }
            continue
        payload = io_cli._identity_write_payload_v2(_ns(**{field: "值"}))
        assert payload["actions"][0]["patch"] == {field: "值"}, field


def test_add_signature_key_shape():
    payload = io_cli._identity_write_payload_v2(_ns(add_signature=["always direct"]))
    assert payload["actions"][0]["patch"] == {"add_signature": ["always direct"]}


def test_legacy_signature_flag_still_whole_replaces():
    """--signature is the pre-existing flag (action='append'); it must keep
    mapping to the legacy whole-replace `signature` key, unchanged, alongside
    the new add/remove/replace-signature* flags."""
    payload = io_cli._identity_write_payload_v2(
        _ns(signature=["always direct", "never coddles"]))
    assert payload["actions"][0]["patch"] == {
        "signature": ["always direct", "never coddles"],
    }


@pytest.mark.parametrize("field,add_dest,remove_dest,replace_dest,add_key,remove_key,replace_key", [
    ("boundaries", "add_boundary", "remove_boundary", "replace_boundaries",
     "add_boundaries", "remove_boundaries", "replace_boundaries"),
    ("do_not_say", "add_do_not_say", "remove_do_not_say", "replace_do_not_say",
     "add_do_not_say", "remove_do_not_say", "replace_do_not_say"),
    ("stable_definitions", "add_stable_definition", "remove_stable_definition",
     "replace_stable_definitions", "add_stable_definitions",
     "remove_stable_definitions", "replace_stable_definitions"),
])
def test_list_field_three_ops_key_shape(field, add_dest, remove_dest, replace_dest,
                                         add_key, remove_key, replace_key):
    """Loop coverage matching backend/identity/actions.py::_LIST_OP_FIELDS
    exactly — a drift here would silently misroute the patch server-side."""
    add_payload = io_cli._identity_write_payload_v2(_ns(**{add_dest: ["x"]}))
    assert add_payload["actions"][0]["patch"] == {add_key: ["x"]}, field

    remove_payload = io_cli._identity_write_payload_v2(_ns(**{remove_dest: ["x"]}))
    assert remove_payload["actions"][0]["patch"] == {remove_key: ["x"]}, field

    replace_payload = io_cli._identity_write_payload_v2(_ns(**{replace_dest: ["x", "y"]}))
    assert replace_payload["actions"][0]["patch"] == {replace_key: ["x", "y"]}, field


def test_nothing_to_write_is_none():
    assert io_cli._identity_write_payload_v2(_ns()) is None


def test_nudge_action_appended_after_profile_patch():
    payload = io_cli._identity_write_payload_v2(_ns(
        user_preferred_name="老张", nudge_dimension=["幽默:+5"]))
    assert payload["actions"] == [
        {"type": "identity.profile_patch", "patch": {"user_preferred_name": "老张"}},
        {"type": "identity.dimension_nudge", "dimension": "幽默", "delta": 5},
    ]


def test_nudge_only_no_profile_patch_action():
    payload = io_cli._identity_write_payload_v2(_ns(nudge_dimension=["幽默:+5", "耐心:-3"]))
    assert payload["actions"] == [
        {"type": "identity.dimension_nudge", "dimension": "幽默", "delta": 5},
        {"type": "identity.dimension_nudge", "dimension": "耐心", "delta": -3},
    ]


# ---------------------------------------------------------------------------
# _parse_nudge_dimension — pure parser
# ---------------------------------------------------------------------------

def test_parse_nudge_dimension_ok():
    assert io_cli._parse_nudge_dimension("幽默:+5") == ("幽默", 5)
    assert io_cli._parse_nudge_dimension("耐心:-3") == ("耐心", -3)


def test_parse_nudge_dimension_missing_colon_raises():
    with pytest.raises(ValueError):
        io_cli._parse_nudge_dimension("幽默5")


def test_parse_nudge_dimension_non_integer_delta_raises():
    with pytest.raises(ValueError):
        io_cli._parse_nudge_dimension("幽默:abc")


# ---------------------------------------------------------------------------
# Local pre-checks — _identity_write_payload_v2 raises, cmd_identity_write
# turns that into _emit(obj, 2). These run BEFORE any network call, so no env
# / no backend config is needed to observe the exit(2).
# ---------------------------------------------------------------------------

def test_rename_without_intro_raises_precheck_error():
    with pytest.raises(io_cli._IdentityWritePrecheckError) as exc_info:
        io_cli._identity_write_payload_v2(_ns(agent_name="老6"))
    assert exc_info.value.obj["error"] == "rename_requires_self_introduction"
    assert exc_info.value.obj["hint"] == "介绍无需变化时读旧卡原样带回 --self-introduction"


def test_rename_without_intro_exits_2(capsys):
    with pytest.raises(SystemExit) as exc_info:
        io_cli.cmd_identity_write(_ns(agent_name="老6"))
    assert exc_info.value.code == 2
    out = capsys.readouterr().out
    assert '"error": "rename_requires_self_introduction"' in out
    assert "--self-introduction" in out


def test_rename_with_intro_does_not_trip_precheck():
    """Paired rename must NOT raise — build the payload normally."""
    payload = io_cli._identity_write_payload_v2(
        _ns(agent_name="老6", self_introduction="我是老6"))
    assert payload["actions"][0]["patch"]["agent_name"] == "老6"


def test_nudge_parse_and_cap():
    # ok: single nudge within cap builds normally.
    payload = io_cli._identity_write_payload_v2(_ns(nudge_dimension=["幽默:+5"]))
    assert payload["actions"] == [
        {"type": "identity.dimension_nudge", "dimension": "幽默", "delta": 5},
    ]

    # "幽默:+11" -> single delta exceeds the ±10 cap -> exit 2.
    with pytest.raises(SystemExit) as exc_info:
        io_cli.cmd_identity_write(_ns(nudge_dimension=["幽默:+11"]))
    assert exc_info.value.code == 2

    # "幽默5" -> malformed (no colon) -> exit 2.
    with pytest.raises(SystemExit) as exc_info:
        io_cli.cmd_identity_write(_ns(nudge_dimension=["幽默5"]))
    assert exc_info.value.code == 2


def test_nudge_single_delta_cap_error_shape(capsys):
    with pytest.raises(SystemExit) as exc_info:
        io_cli.cmd_identity_write(_ns(nudge_dimension=["幽默:+11"]))
    assert exc_info.value.code == 2
    out = capsys.readouterr().out
    assert '"error": "nudge_delta_exceeds_cap"' in out


def test_nudge_format_invalid_error_shape(capsys):
    with pytest.raises(SystemExit) as exc_info:
        io_cli.cmd_identity_write(_ns(nudge_dimension=["幽默5"]))
    assert exc_info.value.code == 2
    out = capsys.readouterr().out
    assert '"error": "nudge_dimension_format_invalid"' in out


def test_nudge_batch_sum_cap_exceeded():
    """Two individually-legal nudges on the SAME dimension whose sum exceeds
    ±10 must still be rejected — mirrors the server's per-request同维度归一求和
    batch gate (card_policy.validate_nudge_sum)."""
    with pytest.raises(io_cli._IdentityWritePrecheckError) as exc_info:
        io_cli._identity_write_payload_v2(_ns(nudge_dimension=["幽默:+6", "幽默:+6"]))
    assert exc_info.value.obj["error"] == "nudge_delta_exceeds_cap"


def test_nudge_batch_sum_different_dimensions_independent():
    """Two different dimensions each within cap must NOT interact."""
    payload = io_cli._identity_write_payload_v2(
        _ns(nudge_dimension=["幽默:+8", "耐心:+8"]))
    assert len(payload["actions"]) == 2


# ---------------------------------------------------------------------------
# I4: total action count (profile_patch + nudges) must not exceed 10 — the
# server (backend/identity/actions.py::_execute_identity_actions) slices
# actions[:10] WITHOUT erroring, so an 11th action (e.g. the last of 10
# nudges alongside a profile patch) is silently never executed while the CLI
# still gets a 200. We decided NOT to change the server (shared entry point,
# App also depends on the current shape) — so the CLI must reject the >10
# case itself, before ever building the request.
# ---------------------------------------------------------------------------

def _n_nudge_specs(n: int, delta: int = 1) -> list:
    # Distinct dimension names so the per-dimension batch-sum cap (<=10) never
    # interferes with what this block is actually testing (total action count).
    return [f"维度{i}:+{delta}" for i in range(n)]


def test_nine_nudges_with_patch_passes_total_count_precheck():
    payload = io_cli._identity_write_payload_v2(
        _ns(user_preferred_name="老张", nudge_dimension=_n_nudge_specs(9)))
    assert len(payload["actions"]) == 10  # 1 profile_patch + 9 nudges


def test_ten_nudges_with_patch_exceeds_total_count_and_raises():
    with pytest.raises(io_cli._IdentityWritePrecheckError) as exc_info:
        io_cli._identity_write_payload_v2(
            _ns(user_preferred_name="老张", nudge_dimension=_n_nudge_specs(10)))
    assert exc_info.value.obj["error"] == "too_many_actions"
    assert "9 条 nudge" in exc_info.value.obj["hint"]


def test_ten_nudges_with_patch_exits_2_via_cmd(capsys):
    with pytest.raises(SystemExit) as exc_info:
        io_cli.cmd_identity_write(
            _ns(user_preferred_name="老张", nudge_dimension=_n_nudge_specs(10)))
    assert exc_info.value.code == 2
    out = capsys.readouterr().out
    assert '"error": "too_many_actions"' in out


def test_ten_nudges_alone_passes_total_count_precheck():
    payload = io_cli._identity_write_payload_v2(_ns(nudge_dimension=_n_nudge_specs(10)))
    assert len(payload["actions"]) == 10


def test_eleven_nudges_alone_exceeds_total_count_and_raises():
    with pytest.raises(io_cli._IdentityWritePrecheckError) as exc_info:
        io_cli._identity_write_payload_v2(_ns(nudge_dimension=_n_nudge_specs(11)))
    assert exc_info.value.obj["error"] == "too_many_actions"
    assert "10 条 nudge" in exc_info.value.obj["hint"]


@pytest.mark.parametrize("kwargs", [
    {"signature": ["a"], "add_signature": ["b"]},
    {"add_signature": ["a"], "remove_signature": ["b"]},
    {"add_boundary": ["a"], "replace_boundaries": ["b"]},
])
def test_list_op_conflict_raises_precheck_error(kwargs):
    with pytest.raises(io_cli._IdentityWritePrecheckError) as exc_info:
        io_cli._identity_write_payload_v2(_ns(**kwargs))
    assert exc_info.value.obj["error"] == "list_op_conflict"


def test_list_op_conflict_exits_2_via_cmd(capsys):
    with pytest.raises(SystemExit) as exc_info:
        io_cli.cmd_identity_write(_ns(signature=["a"], add_signature=["b"]))
    assert exc_info.value.code == 2
    out = capsys.readouterr().out
    assert '"error": "list_op_conflict"' in out
    assert '"field": "signature"' in out


def test_nothing_to_write_exits_2_via_cmd():
    with pytest.raises(SystemExit) as exc_info:
        io_cli.cmd_identity_write(_ns())
    assert exc_info.value.code == 2


def test_paired_rename_proceeds_past_precheck_to_backend_check(monkeypatch):
    """A VALID paired rename must not be caught by the rename precheck — the
    exit(2) it still hits comes from the (unrelated) missing-backend-config
    check, proving the precheck itself did not fire."""
    for var in ("FEEDLING_API_URL", "FEEDLING_API_KEY", "FEEDLING_RUNTIME_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as exc_info:
        io_cli.cmd_identity_write(_ns(agent_name="老6", self_introduction="我是老6"))
    assert exc_info.value.code == 2
    # Confirm it's the backend-config error, not the rename precheck's.
    import io
    import contextlib
    # (re-run to capture output this time)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        io_cli.cmd_identity_write(_ns(agent_name="老6", self_introduction="我是老6"))
    assert "rename_requires_self_introduction" not in buf.getvalue()
    assert "FEEDLING_API_URL" in buf.getvalue() or "auth" in buf.getvalue()


# ---------------------------------------------------------------------------
# Parser wiring smoke tests (subprocess; no DB, no FEEDLING_* env — mirrors
# tests/test_io_cli_parser.py's "reaches handler, not an argparse crash" style)
# ---------------------------------------------------------------------------

def _run(*argv):
    return subprocess.run(
        [sys.executable, IO_CLI, *argv],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )


def test_new_string_flags_are_registered_not_unrecognized():
    r = _run("identity-write", "--category", "伙伴", "--tone-style", "温柔")
    assert "unrecognized arguments" not in r.stderr
    assert "conflicting subparser" not in r.stderr


def test_new_list_op_flags_are_registered_not_unrecognized():
    r = _run("identity-write", "--add-boundary", "不代人做决定",
              "--remove-do-not-say", "别叫我宝宝",
              "--replace-stable-definitions", "老板=张三")
    assert "unrecognized arguments" not in r.stderr


def test_nudge_dimension_flag_is_registered_not_unrecognized():
    r = _run("identity-write", "--nudge-dimension", "幽默:+5")
    assert "unrecognized arguments" not in r.stderr


def test_identity_write_help_epilog_covers_spec_rules():
    r = _run("identity-write", "--help")
    assert r.returncode == 0
    # D3 来源规则 + D4 改名成对 + list 三操作 + 七维只微调 — spec 3.1's four rules.
    assert "D3" in r.stdout
    assert "D4" in r.stdout
    assert "改名成对" in r.stdout
    assert "七维" in r.stdout
