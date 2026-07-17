"""Pure unit tests for tools/user_mcp_materialize.py.

No DB, no I/O — pure function surface. See tests/conftest.py _PURE_UNIT for
why this module is collectable even without a reachable Postgres.
"""

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from tools import user_mcp_materialize as m  # noqa: E402

SRV = [
    {"name": "jira", "enabled": True, "url": "https://a.example.com/mcp",
     "headers": {"Authorization": 'Bearer "quoted"\\x'}},
    {"name": "off", "enabled": False, "url": "https://b.example.com", "headers": {}},
]


def test_claude_mcp_json():
    doc = json.loads(m.claude_mcp_json(SRV))
    assert set(doc["mcpServers"]) == {"jira"}
    assert doc["mcpServers"]["jira"] == {
        "type": "http", "url": "https://a.example.com/mcp",
        "headers": {"Authorization": 'Bearer "quoted"\\x'}}


def test_claude_mcp_json_empty():
    # Implementation pretty-prints (indent=2); lock the real output shape
    # rather than the compact one-liner from the interface prose — both are
    # valid JSON and json.loads() round-trips to the same {"mcpServers": {}}.
    assert json.loads(m.claude_mcp_json([])) == {"mcpServers": {}}
    assert json.loads(m.claude_mcp_json([SRV[1]])) == {"mcpServers": {}}


def test_allow_rules():
    assert m.claude_allow_rules(SRV) == ["mcp__jira__*"]


def test_merge_settings_allow_idempotent():
    # "stale" is itself a previously-managed name (e.g. renamed/re-materialized
    # in an earlier round), so it's included in managed_names here — this is
    # what _maybe_apply_user_mcp does via prev_names | new_names.
    base = json.dumps({"permissions": {"defaultMode": "acceptEdits",
                                       "allow": ["Bash(python io_cli.py perception:*)",
                                                 "mcp__stale__*"]}})
    out = json.loads(m.merge_settings_allow(base, ["mcp__jira__*"], {"jira", "stale"}))
    allow = out["permissions"]["allow"]
    assert "Bash(python io_cli.py perception:*)" in allow
    assert "mcp__jira__*" in allow and "mcp__stale__*" not in allow
    again = m.merge_settings_allow(
        m.merge_settings_allow(base, ["mcp__jira__*"], {"jira", "stale"}),
        ["mcp__jira__*"], {"jira", "stale"})
    assert json.loads(again) == out


def test_merge_settings_allow_none_base():
    out = json.loads(m.merge_settings_allow(None, ["mcp__jira__*"], {"jira"}))
    assert out["permissions"]["allow"] == ["mcp__jira__*"]


def test_merge_settings_allow_preserves_unmanaged_mcp_rules():
    # 修复 2 回归：只 prune managed_names 里的 server 对应规则，其余 mcp__ 规则
    # （用户自己在 .mcp.json 里配的其它 server）必须原样保留，不能被本功能误删。
    base = json.dumps({"permissions": {"allow": [
        "Bash(git status)",
        "mcp__othersrv__*",   # unrelated MCP server — not managed by this feature
        "mcp__jira__*",       # stale rule from a prior materialization of jira
    ]}})
    out = json.loads(m.merge_settings_allow(base, ["mcp__jira__*"], {"jira"}))
    allow = out["permissions"]["allow"]
    assert "Bash(git status)" in allow
    assert allow.count("mcp__othersrv__*") == 1
    assert allow.count("mcp__jira__*") == 1  # replaced, not duplicated
    assert "mcp__othersrv__*" in allow


def test_codex_config_merge_preserves_and_is_idempotent():
    gateway = '[model_providers.feedling_gateway]\nbase_url = "http://127.0.0.1:4000/v1"\n'
    merged = m.codex_config_merged(gateway, SRV)
    assert merged.startswith(gateway)
    assert '[mcp_servers.jira]' in merged and 'url = "https://a.example.com/mcp"' in merged
    assert "off" not in merged.split(m.MARKER_BEGIN)[1]
    assert m.codex_config_merged(merged, SRV) == merged        # 幂等
    assert m.codex_config_merged(merged, []) == gateway         # 清空剥块
    # 真回归：产出必须是可被 tomllib 解析的合法 TOML（不只是字符串匹配）
    parsed = tomllib.loads(merged)
    assert parsed["mcp_servers"]["jira"]["url"] == "https://a.example.com/mcp"
    assert parsed["mcp_servers"]["jira"]["http_headers"] == {
        "Authorization": 'Bearer "quoted"\\x'}


def test_codex_config_merge_no_existing():
    merged = m.codex_config_merged(None, SRV)
    assert merged.startswith(m.MARKER_BEGIN)
    assert merged.endswith(m.MARKER_END + "\n")


def test_codex_config_merge_empty_no_existing():
    assert m.codex_config_merged(None, []) == ""
    assert m.codex_config_merged("", []) == ""


def test_codex_header_toml_escaping():
    merged = m.codex_config_merged(None, SRV)
    # json.dumps 转义得到合法 TOML basic string
    assert '"Authorization" = "Bearer \\"quoted\\"\\\\x"' in merged


def test_codex_header_u007f_produces_valid_toml():
    # 修复 2 回归：json.dumps 不转义 U+007F(DEL)，但 TOML basic-string 要求转义。
    # header 值同时含引号、反斜杠、U+007F —— 修复前 tomllib.loads 会抛 TOMLDecodeError。
    servers = [{"name": "jira", "enabled": True, "url": "https://a.example.com/mcp",
                "headers": {"X-K": "a\x7fb\"c\\d"}}]
    merged = m.codex_config_merged(None, servers)
    parsed = tomllib.loads(merged)  # 不应抛异常
    assert parsed["mcp_servers"]["jira"]["http_headers"]["X-K"] == "a\x7fb\"c\\d"


def test_enabled_filters_unsafe_names():
    # 修复 1 回归：畸形/注入性 name 必须被 _enabled 过滤掉，单个坏 name 不能破坏整份
    # 配置，也不能借 TOML 表头拼接逃逸出 [mcp_servers.*] 段。
    malicious = [
        {"name": "ok-name", "enabled": True, "url": "https://a.example.com", "headers": {}},
        {"name": "bad name", "enabled": True, "url": "https://b.example.com", "headers": {}},
        {"name": "UPPER", "enabled": True, "url": "https://c.example.com", "headers": {}},
        {"name": "a" * 33, "enabled": True, "url": "https://d.example.com", "headers": {}},
        {"name": "jira\"]\n[malicious_section", "enabled": True,
         "url": "https://e.example.com", "headers": {}},
    ]
    doc = json.loads(m.claude_mcp_json(malicious))
    assert set(doc["mcpServers"]) == {"ok-name"}
    assert m.claude_allow_rules(malicious) == ["mcp__ok-name__*"]

    merged = m.codex_config_merged(None, malicious)
    assert "[mcp_servers.ok-name]" in merged
    assert "bad name" not in merged
    assert "malicious_section" not in merged
    parsed = tomllib.loads(merged)  # 注入性 name 必须没能逃逸出合法 TOML 结构
    assert set(parsed["mcp_servers"]) == {"ok-name"}


def test_merge_settings_allow_non_dict_json():
    # 修复 4 回归：合法 JSON 但非 dict（如 "[]"）不应让 settings.setdefault 崩溃。
    out = json.loads(m.merge_settings_allow("[]", ["mcp__jira__*"], {"jira"}))
    assert out["permissions"]["allow"] == ["mcp__jira__*"]

    out_null = json.loads(m.merge_settings_allow("null", ["mcp__jira__*"], {"jira"}))
    assert out_null["permissions"]["allow"] == ["mcp__jira__*"]


def test_merge_settings_allow_allow_field_not_a_list():
    # 修复 4 回归：allow 字段若是字符串会被逐字符遍历，必须先判类型再过滤。
    base = json.dumps({"permissions": {"allow": "not-a-list"}})
    out = json.loads(m.merge_settings_allow(base, ["mcp__jira__*"], {"jira"}))
    assert out["permissions"]["allow"] == ["mcp__jira__*"]


def test_ca_bundle_pem_concatenates_enabled_only():
    servers = [
        {"name": "a", "enabled": True, "url": "https://a/", "ca_pem": "PEM-A\n"},
        {"name": "b", "enabled": False, "url": "https://b/", "ca_pem": "PEM-B\n"},
        {"name": "c", "enabled": True, "url": "https://c/"},           # 无 CA
    ]
    out = m.ca_bundle_pem(servers)
    assert "PEM-A" in out
    assert "PEM-B" not in out        # disabled 的不进包


def test_ca_bundle_pem_none_when_no_ca():
    servers = [{"name": "a", "enabled": True, "url": "https://a/"}]
    assert m.ca_bundle_pem(servers) is None


def test_ca_bundle_pem_none_when_empty():
    assert m.ca_bundle_pem([]) is None


def test_ca_bundle_pem_newline_separated():
    # PEM 块之间必须有换行，否则 -----END-----BEGIN----- 粘连导致整包解析失败
    servers = [
        {"name": "a", "enabled": True, "url": "https://a/", "ca_pem": "PEM-A"},
        {"name": "b", "enabled": True, "url": "https://b/", "ca_pem": "PEM-B"},
    ]
    out = m.ca_bundle_pem(servers)
    assert out == "PEM-A\nPEM-B\n"


def test_ca_bundle_pem_skips_unsafe_names():
    # _enabled 的 name 白名单同样适用（防御一致性）
    servers = [{"name": "BAD NAME", "enabled": True, "url": "https://a/",
                "ca_pem": "PEM-X"}]
    assert m.ca_bundle_pem(servers) is None
