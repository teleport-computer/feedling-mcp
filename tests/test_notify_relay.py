"""Notify Relay（自部署推送中继）契约测试。

Run:  python -m pytest tests/test_notify_relay.py -q

覆盖：接口A（匿名 enroll，幂等键 device_token/auth_token）、接口B（X-Relay-Token
鉴权 + 4 种 push type 的 payload 形态 + log 落库/双写 + 限流 + APNs key 缺失
503）。APNs 出网全部 monkeypatch ``apns._send_apns``（spy 捕获参数），永不真连
Apple；TEE 双写 monkeypatch ``tee_shadow.mirror`` 捕获镜像语句。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from asgi import middleware  # noqa: E402
from notify_relay import relay_core  # noqa: E402
from notify_relay import routes_asgi as relay_routes  # noqa: E402
from push import apns  # noqa: E402
from tee_shadow import mirror  # noqa: E402

DEVICE = "ab" * 32
DEVICE2 = "cd" * 32


@pytest.fixture()
def app(monkeypatch):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    relay_routes.register_asgi(app)
    # 限流器是模块级 per-worker 内存 —— 每个测试从干净状态开始。
    relay_routes._register_limiter.reset()
    relay_routes._push_limiter.reset()
    relay_routes._bad_auth_limiter.reset()
    # push 路径先过 APNS_KEY 门禁（无 key → 503）；这里给个假 key，出网已被
    # 各测试的 _send_apns spy 挡住。
    monkeypatch.setattr(apns, "APNS_KEY", "-----FAKE-----")
    yield app
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM notify_relay_logs")
        conn.execute("DELETE FROM notify_relay_configs")


def _post(app, path, json_body=None, headers=None):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(path, json=json_body, headers=headers or {})
            try:
                return resp.status_code, resp.json(), resp.headers
            except Exception:
                return resp.status_code, resp.text, resp.headers

    return asyncio.run(go())


def _enroll(app, device=DEVICE, **extra):
    status, body, _ = _post(app, "/v1/notify-relay/register",
                            {"device_token": device, "apns_env": "sandbox", **extra})
    assert status == 200, body
    return body


@pytest.fixture()
def spy(monkeypatch):
    """_send_apns spy：记录 (token, payload, push_type, topic, preferred_env)，
    返回可编程的结果。"""
    calls: list[dict] = []
    result = {"status": "delivered", "apns_env": "sandbox"}

    def fake_send(token, payload, push_type, topic, *, preferred_env=None):
        calls.append({"token": token, "payload": payload, "push_type": push_type,
                      "topic": topic, "preferred_env": preferred_env})
        return dict(result)

    monkeypatch.setattr(apns, "_send_apns", fake_send)
    # relay_core 通过 `from push import apns` 引模块再取属性，patch 模块函数即可全覆盖。
    return {"calls": calls, "result": result}


@pytest.fixture()
def mirror_log(monkeypatch):
    """捕获 TEE 双写语句（enabled 与否无关——直接替换执行函数）。"""
    captured: list[tuple[str, tuple]] = []
    monkeypatch.setattr(mirror, "execute", lambda sql, params=(): captured.append((sql, params)))
    monkeypatch.setattr(
        mirror, "execute_many",
        lambda statements: captured.extend(statements))
    return captured


# --------------------------------------------------------------------------- #
# 接口A：enroll
# --------------------------------------------------------------------------- #

def test_register_creates_token(app):
    body = _enroll(app)
    assert body["created"] is True
    assert body["auth_token"].startswith("nrt_") and len(body["auth_token"]) == 52
    assert body["device_token"] == DEVICE
    assert body["apns_env"] == "sandbox"


def test_register_same_device_does_not_disclose_token(app):
    """匿名 register 命中已注册设备时不回显 auth_token：device_token 非秘密，
    不能凭它换回别人的中继 token（Codex review P1）。"""
    first = _enroll(app)
    assert first["created"] is True and first["auth_token"].startswith("nrt_")
    second = _enroll(app)  # 纯 device_token，无 auth_token
    assert second["created"] is False
    assert second.get("already_enrolled") is True
    assert "auth_token" not in second


def test_register_with_existing_token_rotates_device(app):
    first = _enroll(app)
    body = _enroll(app, device=DEVICE2, auth_token=first["auth_token"])
    assert body["auth_token"] == first["auth_token"]
    assert body["device_token"] == DEVICE2
    assert body["created"] is False
    with db.get_pool().connection() as conn:
        rows = conn.execute("SELECT device_token FROM notify_relay_configs").fetchall()
    assert [r[0] for r in rows] == [DEVICE2]


def test_register_refuses_to_hijack_others_device(app):
    """持有自己 token 的调用者不能把绑定改到别人已注册的 device_token：返回
    409，受害者行原封不动（Codex review R4 [1] —— 否则任意 token 持有者可
    DoS/劫持任意已知 device_token）。"""
    a = _enroll(app, device=DEVICE)
    b = _enroll(app, device=DEVICE2)
    assert a["auth_token"] != b["auth_token"]
    status, body, _ = _post(app, "/v1/notify-relay/register",
                            {"device_token": DEVICE2, "auth_token": a["auth_token"]})
    assert status == 409, body
    assert body["error"] == "device_already_enrolled"
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT auth_token, device_token FROM notify_relay_configs "
            "ORDER BY device_token").fetchall()
    assert rows == [(a["auth_token"], DEVICE), (b["auth_token"], DEVICE2)]


def test_register_disabled_token_cannot_reenroll(app):
    """被禁用（撤销）的 token 不能再经 register 重绑/顶替（Codex review R4 [2]）。"""
    a = _enroll(app, device=DEVICE)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE notify_relay_configs SET disabled = TRUE "
                     "WHERE auth_token = %s", (a["auth_token"],))
    # 带被禁 token 想绑到一个全新设备 → owns 检查（含 disabled=FALSE）不通过 →
    # fall through 到匿名路径，铸新 token（旧的仍禁用、设备行不受影响）。
    body = _enroll(app, device=DEVICE2, auth_token=a["auth_token"])
    assert body["created"] is True
    assert body["auth_token"] != a["auth_token"]
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT disabled FROM notify_relay_configs "
                           "WHERE auth_token = %s", (a["auth_token"],)).fetchone()
    assert row[0] is True  # 原禁用行没被复活


def test_register_reenroll_preserves_apns_env_when_omitted(app):
    """re-enroll 省略 apns_env 时保留原值，不被默认 production 悄悄翻转
    （Codex review R4 [6]）。"""
    first = _enroll(app)  # apns_env=sandbox（_enroll 默认带）
    status, body, _ = _post(app, "/v1/notify-relay/register",
                            {"device_token": DEVICE2, "auth_token": first["auth_token"]})
    assert status == 200, body
    with db.get_pool().connection() as conn:
        env = conn.execute("SELECT apns_env FROM notify_relay_configs "
                           "WHERE auth_token = %s", (first["auth_token"],)).fetchone()[0]
    assert env == "sandbox"  # 未随省略翻成 production


def test_register_unknown_token_does_not_disclose_existing(app):
    """带未知 auth_token 命中已注册设备 → fall through 到匿名路径，仍不回显
    token（不能用错误 token 探测/换回受害者 token）。"""
    _enroll(app)
    body = _enroll(app, auth_token="nrt_" + "0" * 48)
    assert body["created"] is False
    assert body.get("already_enrolled") is True
    assert "auth_token" not in body


def test_register_unknown_token_new_device_mints_token(app):
    """带未知 auth_token 的全新设备照常铸新 token（fall through 不崩，Codex P3
    的同一 fall-through 路径）。"""
    body = _enroll(app, device=DEVICE2, auth_token="nrt_" + "0" * 48)
    assert body["created"] is True
    assert body["auth_token"].startswith("nrt_")
    assert body["device_token"] == DEVICE2


def test_register_rejects_overlong_user_id(app):
    status, body, _ = _post(app, "/v1/notify-relay/register",
                            {"device_token": DEVICE, "user_id": "x" * 200})
    assert status == 400
    assert body["error"] == "invalid_payload"


def test_register_rejects_bad_device_token(app):
    status, body, _ = _post(app, "/v1/notify-relay/register",
                            {"device_token": "not-hex!!"})
    assert status == 400
    assert body["error"] == "invalid_payload"


def test_register_rejects_bad_apns_env(app):
    status, body, _ = _post(app, "/v1/notify-relay/register",
                            {"device_token": DEVICE, "apns_env": "prod"})
    assert status == 400
    assert body["error"] == "invalid_payload"


def test_register_rejects_non_dict_body(app):
    status, body, _ = _post(app, "/v1/notify-relay/register", ["not", "a", "dict"])
    assert status == 400
    assert body["error"] == "invalid_payload"


def test_register_rate_limited_per_ip(app, monkeypatch):
    monkeypatch.setenv("NOTIFY_RELAY_REGISTER_RATE", "1/3600")
    _enroll(app)
    status, body, headers = _post(app, "/v1/notify-relay/register",
                                  {"device_token": DEVICE})
    assert status == 429
    assert body["error"] == "rate_limited"
    assert int(headers["Retry-After"]) >= 1


# --------------------------------------------------------------------------- #
# 接口B：鉴权
# --------------------------------------------------------------------------- #

def test_push_missing_header_is_401(app):
    status, body, _ = _post(app, "/v1/notify-relay/push", {"type": 1, "content": {}})
    assert (status, body) == (401, {"error": "unauthorized"})


def test_push_unknown_token_is_401(app):
    status, body, _ = _post(app, "/v1/notify-relay/push", {"type": 1, "content": {}},
                            {"X-Relay-Token": "nrt_" + "f" * 48})
    assert (status, body) == (401, {"error": "unauthorized"})


def test_push_disabled_config_is_403(app):
    tok = _enroll(app)["auth_token"]
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE notify_relay_configs SET disabled = TRUE "
                     "WHERE auth_token = %s", (tok,))
    status, body, _ = _post(app, "/v1/notify-relay/push", {"type": 1, "content": {}},
                            {"X-Relay-Token": tok})
    assert (status, body) == (403, {"error": "forbidden"})


def test_push_disabled_token_is_rate_limited(app, monkeypatch):
    """disabled token 不再无限触库：per-token push 限流对它同样生效（Codex
    review 本轮 P2）——否则被撤销的 token 仍能每请求打一次 get_config。"""
    monkeypatch.setenv("NOTIFY_RELAY_PUSH_RATE", "1/60")
    tok = _enroll(app)["auth_token"]
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE notify_relay_configs SET disabled = TRUE "
                     "WHERE auth_token = %s", (tok,))
    hdr = {"X-Relay-Token": tok}
    status, _, _ = _post(app, "/v1/notify-relay/push", {"type": 1, "content": {}}, hdr)
    assert status == 403
    status, body, _ = _post(app, "/v1/notify-relay/push", {"type": 1, "content": {}}, hdr)
    assert status == 429
    assert body["error"] == "rate_limited"


def test_push_known_token_flood_throttled_before_db(app, spy, monkeypatch):
    """已知有效 token 的 DB 查询也受 per-token 限流保护（peek 前置于
    get_config），不只成功发送才限。"""
    monkeypatch.setenv("NOTIFY_RELAY_PUSH_RATE", "1/60")
    tok = _enroll(app)["auth_token"]
    lookups = []
    real = relay_core.get_config
    monkeypatch.setattr(relay_core, "get_config",
                        lambda t: (lookups.append(t), real(t))[1])
    hdr = {"X-Relay-Token": tok}
    s1, _, _ = _post(app, "/v1/notify-relay/push",
                     {"type": 1, "content": {"title": "t"}}, hdr)
    assert s1 == 200
    s2, body, _ = _post(app, "/v1/notify-relay/push",
                        {"type": 1, "content": {"title": "t"}}, hdr)
    assert s2 == 429
    assert len(lookups) == 1  # 第二次在查库前就被 push-limiter peek 挡住


def test_push_bad_auth_rate_limited(app, monkeypatch):
    monkeypatch.setenv("NOTIFY_RELAY_BAD_AUTH_RATE", "2/60")
    hdr = {"X-Relay-Token": "nrt_" + "e" * 48}
    for _ in range(2):
        status, _, _ = _post(app, "/v1/notify-relay/push", {"type": 1}, hdr)
        assert status == 401
    status, body, _ = _post(app, "/v1/notify-relay/push", {"type": 1}, hdr)
    assert status == 429
    assert body["error"] == "rate_limited"


def test_push_valid_token_not_blocked_by_ip_bad_auth(app, spy, monkeypatch):
    """同一 IP 的坏认证预算耗尽后，携带有效 token 的推送仍放行——不因邻居
    （共用 NAT/egress IP）的 stale-token 洪泛被误 429（Codex review R4 [3]）。"""
    monkeypatch.setenv("NOTIFY_RELAY_BAD_AUTH_RATE", "1/60")
    tok = _enroll(app)["auth_token"]
    # 先用坏 token 打爆该 IP 的 bad-auth 预算
    s1, _, _ = _post(app, "/v1/notify-relay/push", {"type": 1},
                     {"X-Relay-Token": "nrt_" + "e" * 48})
    assert s1 == 401
    s2, _, _ = _post(app, "/v1/notify-relay/push", {"type": 1},
                     {"X-Relay-Token": "nrt_" + "e" * 48})
    assert s2 == 429  # IP bad-auth 已超限
    # 有效 token 从同一 IP 仍应成功
    s3, body, _ = _post(app, "/v1/notify-relay/push",
                        {"type": 1, "content": {"title": "t"}}, {"X-Relay-Token": tok})
    assert s3 == 200, body
    assert body["status"] == "delivered"


def test_push_unknown_token_flood_is_throttled(app, monkeypatch):
    """未知 token 洪泛被 per-IP bad-auth 限流挡住（记账后 429）。DB 保护由此
    + 已知 token 的 push-limiter peek（另测）共同提供；不再在查库前短路未知
    token，以免误伤共用 IP 的有效 token（Codex review R4 [3] 的取舍）。"""
    monkeypatch.setenv("NOTIFY_RELAY_BAD_AUTH_RATE", "1/60")
    hdr = {"X-Relay-Token": "nrt_" + "d" * 48}
    status, _, _ = _post(app, "/v1/notify-relay/push", {"type": 1}, hdr)
    assert status == 401
    status, body, _ = _post(app, "/v1/notify-relay/push", {"type": 1}, hdr)
    assert status == 429
    assert body["error"] == "rate_limited"


def test_push_valid_token_unaffected_by_bad_auth_budget(app, spy, monkeypatch):
    """合法 token 的正常调用完全不碰 bad-auth 预算（即便设成 1/60 也能连发）。"""
    monkeypatch.setenv("NOTIFY_RELAY_BAD_AUTH_RATE", "1/60")
    tok = _enroll(app)["auth_token"]
    hdr = {"X-Relay-Token": tok}
    for _ in range(3):
        status, body, _ = _post(app, "/v1/notify-relay/push",
                                {"type": 1, "content": {"title": "t"}}, hdr)
        assert status == 200, body


# --------------------------------------------------------------------------- #
# 接口B：参数校验
# --------------------------------------------------------------------------- #

def test_push_rejects_bad_type(app, spy):
    tok = _enroll(app)["auth_token"]
    for bad in (None, 0, 5, "1"):
        status, body, _ = _post(app, "/v1/notify-relay/push",
                                {"type": bad, "content": {}}, {"X-Relay-Token": tok})
        assert status == 400, (bad, body)
        assert body["error"] == "invalid_payload"
    assert spy["calls"] == []


def test_push_live_types_require_token(app, spy):
    tok = _enroll(app)["auth_token"]
    for t in (2, 3, 4):
        status, body, _ = _post(app, "/v1/notify-relay/push",
                                {"type": t, "content": {}}, {"X-Relay-Token": tok})
        assert status == 400, (t, body)
        assert body["error"] == "invalid_payload"
    assert spy["calls"] == []


def test_push_rejects_non_dict_content(app, spy):
    tok = _enroll(app)["auth_token"]
    status, body, _ = _post(app, "/v1/notify-relay/push",
                            {"type": 1, "content": "hi"}, {"X-Relay-Token": tok})
    assert status == 400
    assert body["error"] == "invalid_payload"
    assert spy["calls"] == []


def test_push_no_apns_key_is_503(app, spy, monkeypatch):
    tok = _enroll(app)["auth_token"]
    monkeypatch.setattr(apns, "APNS_KEY", None)
    status, body, _ = _post(app, "/v1/notify-relay/push",
                            {"type": 1, "content": {"title": "t", "body": "b"}},
                            {"X-Relay-Token": tok})
    assert status == 503
    assert body["error"] == "service_unavailable"
    assert spy["calls"] == []
    with db.get_pool().connection() as conn:
        count = conn.execute("SELECT count(*) FROM notify_relay_logs").fetchone()[0]
    assert count == 0  # 官方侧故障不写 pending log


def test_push_apns_sign_error_is_503_not_500(app, spy, monkeypatch):
    """APNS_KEY 存在但无效（_make_apns_jwt 抛异常）→ 503 而非 500，且 pending
    log 被收尾成 fail 不留幽灵行（Codex review 本轮 P4）。"""
    tok = _enroll(app)["auth_token"]

    def boom(*a, **k):
        raise ValueError("not a valid ES256 key")

    monkeypatch.setattr(apns, "_send_apns", boom)
    status, body, _ = _post(app, "/v1/notify-relay/push",
                            {"type": 1, "content": {"title": "t"}},
                            {"X-Relay-Token": tok})
    assert status == 503
    assert body["error"] == "service_unavailable"
    rows = _log_rows()
    assert len(rows) == 1
    assert rows[0][3] == relay_core.STATUS_FAIL  # 不停在 pending
    assert "apns_sign_error" in (rows[0][4] or "")


def test_prune_logs_removes_rows_past_retention(app, spy, monkeypatch):
    """prune_logs 删除超保留期的日志行（Codex review 本轮 P10）。"""
    monkeypatch.setenv("NOTIFY_RELAY_LOG_RETENTION_DAYS", "30")
    tok = _enroll(app)["auth_token"]
    _post(app, "/v1/notify-relay/push",
          {"type": 1, "content": {"title": "recent"}}, {"X-Relay-Token": tok})
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO notify_relay_logs (auth_token, push_type, target_token, "
            "status, created_at, updated_at) VALUES (%s, 1, %s, 2, "
            "now() - interval '40 days', now() - interval '40 days')",
            (tok, DEVICE))
        before = conn.execute("SELECT count(*) FROM notify_relay_logs").fetchone()[0]
    assert before == 2
    deleted = relay_core.prune_logs()
    assert deleted == 1
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT content FROM notify_relay_logs").fetchall()
    assert len(rows) == 1  # 只留下近期那条


def test_prune_logs_disabled_by_zero(app, monkeypatch):
    monkeypatch.setenv("NOTIFY_RELAY_LOG_RETENTION_DAYS", "0")
    tok = _enroll(app)["auth_token"]
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO notify_relay_logs (auth_token, push_type, target_token, "
            "status, created_at, updated_at) VALUES (%s, 1, %s, 2, "
            "now() - interval '400 days', now() - interval '400 days')",
            (tok, DEVICE))
    assert relay_core.prune_logs() == 0
    with db.get_pool().connection() as conn:
        count = conn.execute("SELECT count(*) FROM notify_relay_logs").fetchone()[0]
    assert count == 1


# --------------------------------------------------------------------------- #
# 接口B：4 种 type 的投递形态
# --------------------------------------------------------------------------- #

def test_push_alert_defaults_to_enrolled_device_token(app, spy):
    tok = _enroll(app)["auth_token"]
    status, body, _ = _post(
        app, "/v1/notify-relay/push",
        {"type": 1, "content": {"title": "Hi", "body": "there", "badge": 3,
                                "data": {"feedling": {"k": 1}}}},
        {"X-Relay-Token": tok})
    assert status == 200, body
    assert body["status"] == "delivered"
    call = spy["calls"][0]
    assert call["token"] == DEVICE
    assert call["push_type"] == "alert"
    assert call["topic"] == apns.BUNDLE_ID
    assert call["preferred_env"] == "sandbox"  # 注册时的 config.apns_env
    assert call["payload"]["aps"]["alert"] == {"title": "Hi", "body": "there"}
    assert call["payload"]["aps"]["badge"] == 3
    assert call["payload"]["feedling"] == {"k": 1}


def test_push_alert_rejects_foreign_token(app, spy):
    """alert 只许推注册设备：中继 token + 任意 APNs token 不得借官方钥
    给别人设备推送（Codex review P1）。"""
    tok = _enroll(app)["auth_token"]
    status, body, _ = _post(
        app, "/v1/notify-relay/push",
        {"type": 1, "token": DEVICE2, "content": {"title": "t", "body": "b"}},
        {"X-Relay-Token": tok})
    assert status == 400, body
    assert body["error"] == "invalid_payload"
    assert spy["calls"] == []


def test_push_alert_matching_token_and_env_override(app, spy):
    tok = _enroll(app)["auth_token"]
    status, body, _ = _post(
        app, "/v1/notify-relay/push",
        {"type": 1, "token": DEVICE.upper(), "apns_env": "production",
         "content": {"title": "t", "body": "b"}},
        {"X-Relay-Token": tok})
    assert status == 200, body
    call = spy["calls"][0]
    assert call["token"] == DEVICE  # 与注册值一致（大小写不敏感比对）
    assert call["preferred_env"] == "production"


def test_push_live_update_payload_shape(app, spy):
    tok = _enroll(app)["auth_token"]
    la_token = "12" * 40
    status, body, _ = _post(
        app, "/v1/notify-relay/push",
        {"type": 2, "token": la_token,
         "content": {"visualState": "sharing", "name": "IO", "desc": "分享中",
                     "aiStart": "2026-01-01", "alert_title": "IO",
                     "alert_body": "更新了", "stale_date": 1800000000}},
        {"X-Relay-Token": tok})
    assert status == 200, body
    call = spy["calls"][0]
    aps = call["payload"]["aps"]
    assert call["token"] == la_token
    assert call["push_type"] == "liveactivity"
    assert call["topic"] == f"{apns.BUNDLE_ID}.push-type.liveactivity"
    assert aps["event"] == "update"
    assert aps["alert"] == {"title": "IO", "body": "更新了"}
    assert aps["stale-date"] == 1800000000
    cs = aps["content-state"]
    assert cs["visualState"] == "sharing"
    assert cs["name"] == "IO"
    assert cs["desc"] == "分享中"
    assert cs["aiStart"] == "2026-01-01"


def test_push_live_start_payload_shape(app, spy):
    tok = _enroll(app)["auth_token"]
    pts_token = "34" * 40
    status, body, _ = _post(
        app, "/v1/notify-relay/push",
        {"type": 3, "token": pts_token,
         "content": {"name": "IO", "desc": "开始", "alert_body": "启动灵动岛"}},
        {"X-Relay-Token": tok})
    assert status == 200, body
    aps = spy["calls"][0]["payload"]["aps"]
    assert aps["event"] == "start"
    assert aps["attributes-type"] == "ScreenActivityAttributes"
    assert set(aps["attributes"]) == {"activityId"}
    assert aps["attributes"]["activityId"].startswith("la_")
    assert aps["alert"]["body"] == "启动灵动岛"


def test_push_live_start_custom_attributes(app, spy):
    tok = _enroll(app)["auth_token"]
    _post(app, "/v1/notify-relay/push",
          {"type": 3, "token": "34" * 40,
           "content": {"attributes": {"activityId": "la_custom"},
                       "attributes_type": "ScreenActivityAttributes"}},
          {"X-Relay-Token": tok})
    aps = spy["calls"][0]["payload"]["aps"]
    assert aps["attributes"] == {"activityId": "la_custom"}


def test_push_live_end_payload_shape(app, spy):
    tok = _enroll(app)["auth_token"]
    status, body, _ = _post(
        app, "/v1/notify-relay/push",
        {"type": 4, "token": "56" * 40,
         "content": {"dismissal_date": 1900000000}},
        {"X-Relay-Token": tok})
    assert status == 200, body
    aps = spy["calls"][0]["payload"]["aps"]
    assert aps["event"] == "end"
    assert aps["dismissal-date"] == 1900000000
    assert aps["content-state"]["visualState"] == "default"


# --------------------------------------------------------------------------- #
# log 落库 + 双写 + last_used_at
# --------------------------------------------------------------------------- #

def _log_rows():
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT push_type, target_token, apns_env, status, err_msg, content "
            "FROM notify_relay_logs ORDER BY id").fetchall()


def test_push_delivered_writes_success_log_and_touches_config(app, spy):
    tok = _enroll(app)["auth_token"]
    status, body, _ = _post(app, "/v1/notify-relay/push",
                            {"type": 1, "content": {"title": "t", "body": "b"}},
                            {"X-Relay-Token": tok})
    assert body["status"] == "delivered"
    assert isinstance(body["log_id"], int)
    rows = _log_rows()
    assert len(rows) == 1
    push_type, target, env, log_status, err, content = rows[0]
    assert (push_type, target, log_status, err) == (1, DEVICE, relay_core.STATUS_SUCCESS, None)
    assert '"title"' in content
    with db.get_pool().connection() as conn:
        last_used = conn.execute(
            "SELECT last_used_at FROM notify_relay_configs WHERE auth_token = %s",
            (tok,)).fetchone()[0]
    assert last_used is not None


def test_push_error_writes_fail_log_with_reason(app, spy):
    spy["result"].update({"status": "error", "reason": "BadDeviceToken",
                          "code": 400, "apns_env": "production"})
    tok = _enroll(app)["auth_token"]
    status, body, _ = _post(app, "/v1/notify-relay/push",
                            {"type": 1, "content": {"title": "t"}},
                            {"X-Relay-Token": tok})
    assert status == 200
    assert body["status"] == "error"
    assert body["reason"] == "BadDeviceToken"
    assert body["error_code"] == 400
    assert body["apns_env"] == "production"  # fallback 后的实际投递环境
    rows = _log_rows()
    assert rows[0][3] == relay_core.STATUS_FAIL
    assert rows[0][4] == "BadDeviceToken"
    with db.get_pool().connection() as conn:
        last_used = conn.execute(
            "SELECT last_used_at FROM notify_relay_configs WHERE auth_token = %s",
            (tok,)).fetchone()[0]
    assert last_used is None  # 失败不刷活跃度


def test_push_content_truncated_by_env(app, spy, monkeypatch):
    monkeypatch.setenv("NOTIFY_RELAY_LOG_CONTENT_MAX", "10")
    tok = _enroll(app)["auth_token"]
    _post(app, "/v1/notify-relay/push",
          {"type": 1, "content": {"title": "x" * 100}}, {"X-Relay-Token": tok})
    assert len(_log_rows()[0][5]) == 10


def test_push_content_retention_disabled(app, spy, monkeypatch):
    monkeypatch.setenv("NOTIFY_RELAY_LOG_CONTENT_MAX", "0")
    tok = _enroll(app)["auth_token"]
    _post(app, "/v1/notify-relay/push",
          {"type": 1, "content": {"title": "secret"}}, {"X-Relay-Token": tok})
    assert _log_rows()[0][5] is None


def test_push_rate_limited_per_token(app, spy, monkeypatch):
    monkeypatch.setenv("NOTIFY_RELAY_PUSH_RATE", "1/60")
    tok = _enroll(app)["auth_token"]
    hdr = {"X-Relay-Token": tok}
    status, _, _ = _post(app, "/v1/notify-relay/push",
                         {"type": 1, "content": {}}, hdr)
    assert status == 200
    status, body, headers = _post(app, "/v1/notify-relay/push",
                                  {"type": 1, "content": {}}, hdr)
    assert status == 429
    assert body["error"] == "rate_limited"
    assert "Retry-After" in headers


def test_mirror_statements_for_register_and_push(app, spy, mirror_log):
    tok = _enroll(app)["auth_token"]
    _post(app, "/v1/notify-relay/push",
          {"type": 1, "content": {"title": "t"}}, {"X-Relay-Token": tok})
    sqls = [sql for sql, _ in mirror_log]
    # enroll → configs 全行 upsert
    assert any("INSERT INTO notify_relay_configs" in s and "ON CONFLICT (auth_token)" in s
               for s in sqls)
    # pending log → 显式 id + OVERRIDING SYSTEM VALUE（IDENTITY 不变量）
    log_inserts = [(s, p) for s, p in mirror_log
                   if "INSERT INTO notify_relay_logs" in s]
    assert len(log_inserts) == 1
    assert "OVERRIDING SYSTEM VALUE" in log_inserts[0][0]
    assert isinstance(log_inserts[0][1][0], int)  # 主库 RETURNING 的 id 被钉进镜像
    # 完成态 UPDATE + last_used_at touch 也各有镜像
    assert any("UPDATE notify_relay_logs SET status" in s for s in sqls)
    assert any("UPDATE notify_relay_configs SET last_used_at" in s for s in sqls)


def test_reconciler_registration():
    from tee_shadow import reconciler

    assert reconciler.TABLES["notify_relay_configs"][0] == ("auth_token",)
    assert reconciler.TABLES["notify_relay_logs"][0] == ("id",)
    assert reconciler._IDENTITY_TABLES["notify_relay_logs"] == "id"


def test_reconciler_converges_after_missed_displacement_delete(app):
    """换机顶替 + 镜像漏删 DELETE 的自愈（Codex review P2）：TEE 里陈旧行仍占着
    同一 device_token 时，reconcile 的 copy 阶段不得撞约束，prune 后两侧收敛。
    TEE 进入 primary-capable 状态后也带 device_token UNIQUE，因此 reconciler
    必须在同一事务内先清掉旧 binding，再写入 RDS 的当前 binding。"""
    from tee_shadow import reconciler

    new_token = _enroll(app)["auth_token"]  # RDS 存活行（dual-write 未开，TEE 没有）
    stale_token = "nrt_" + "a" * 48
    with mirror.get_tee_pool().connection() as conn:
        conn.execute("DELETE FROM notify_relay_configs")
        # 模拟漏删：TEE 里的旧行还占着同一个 device_token
        conn.execute(
            "INSERT INTO notify_relay_configs (auth_token, device_token, apns_env) "
            "VALUES (%s, %s, 'sandbox')",
            (stale_token, DEVICE),
        )
    try:
        result = reconciler.reconcile_table("notify_relay_configs", prune=True)
        with mirror.get_tee_pool().connection() as conn:
            rows = conn.execute(
                "SELECT auth_token, device_token FROM notify_relay_configs").fetchall()
        assert rows == [(new_token, DEVICE)], (result, rows)
    finally:
        with mirror.get_tee_pool().connection() as conn:
            conn.execute("DELETE FROM notify_relay_configs")


# --------------------------------------------------------------------------- #
# 限流身份（X-Forwarded-For）
# --------------------------------------------------------------------------- #

def test_register_rate_limit_ignores_spoofed_first_xff_hop(app, monkeypatch):
    """限流 key 取 XFF 末跳（可信代理追加的那项）：每请求伪造一个新首跳
    不能绕过 per-IP 限流（Codex review P1）。"""
    monkeypatch.setenv("NOTIFY_RELAY_REGISTER_RATE", "1/3600")
    status, _, _ = _post(app, "/v1/notify-relay/register", {"device_token": DEVICE},
                         {"X-Forwarded-For": "6.6.6.1, 9.9.9.9"})
    assert status == 200
    status, body, _ = _post(app, "/v1/notify-relay/register", {"device_token": DEVICE},
                            {"X-Forwarded-For": "6.6.6.2, 9.9.9.9"})
    assert status == 429, body  # 首跳换了，末跳（真实来源）没换 → 仍被限
    assert body["error"] == "rate_limited"


def test_register_rate_limit_xff_hops_zero_uses_socket_peer(app, monkeypatch):
    """NOTIFY_RELAY_XFF_HOPS=0（直连部署）：完全忽略 XFF，用 socket 对端。"""
    monkeypatch.setenv("NOTIFY_RELAY_XFF_HOPS", "0")
    monkeypatch.setenv("NOTIFY_RELAY_REGISTER_RATE", "1/3600")
    status, _, _ = _post(app, "/v1/notify-relay/register", {"device_token": DEVICE},
                         {"X-Forwarded-For": "6.6.6.1"})
    assert status == 200
    status, body, _ = _post(app, "/v1/notify-relay/register", {"device_token": DEVICE},
                            {"X-Forwarded-For": "6.6.6.2"})
    assert status == 429, body  # XFF 全然无效，socket 对端相同 → 限住
