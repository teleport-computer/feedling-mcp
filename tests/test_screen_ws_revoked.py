"""WS 帧上传的存活鉴权（screen/ws.py）。

连接握手时鉴权一次是不够的：账号删除（/v1/account/reset → users 行 CASCADE 删净）
后，iOS 广播扩展的已建立 WebSocket 还活着、每 ~10s 继续推帧——每帧驱动
frames._save_frame 对已不存在的用户写库，set_blob 撞 FK（被吞、只刷日志），
_broadcast_store_change 仍广播 → 其余 worker evict/reload 该幽灵 store →
「rebuilt index n=0 + set_blob FK failed」每帧一轮（2026-07-14 prod 实测
usr_25ce…，53 次/10min 持续刷）。

约定：消息循环内每帧用连接时的 key 重解析用户；key 已失效（账号删除 / key 吊销
→ registry._key_to_user 经 users 广播已摘除）时立即 close(4401) 停止 ingest。
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from screen import ws as screen_ws  # noqa: E402


class _FakeWS:
    """最小 websocket 桩：headers 鉴权 + 异步消息迭代 + close 记录。"""

    def __init__(self, messages, on_yield=None):
        self._messages = messages
        self._on_yield = on_yield  # 每 yield 一条后回调（用于中途吊销 key）
        self.request_headers = {"Authorization": "Bearer flk_test_key"}
        self.remote_address = ("test", 0)
        self.closed: list[tuple] = []

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for i, m in enumerate(self._messages):
            yield m
            if self._on_yield:
                self._on_yield(i)

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))


def _run_handler(monkeypatch, messages, valid, on_yield=None):
    saved: list = []

    monkeypatch.setattr(screen_ws.registry, "_resolve_user",
                        lambda key: valid.get(key))
    monkeypatch.setattr(screen_ws.core_store, "get_store",
                        lambda uid, **_kwargs: SimpleNamespace(
                            user_id=uid, last_seen_api_key=""))
    monkeypatch.setattr(screen_ws.frames, "_save_frame",
                        lambda store, data: saved.append(data))
    # _save_frame 在线程里跑——同步执行以便断言。
    monkeypatch.setattr(screen_ws.threading, "Thread",
                        lambda target, args=(), daemon=None: SimpleNamespace(
                            start=lambda: target(*args)))

    fake = _FakeWS(messages, on_yield=on_yield or (lambda i: None))
    asyncio.run(screen_ws._ws_handler(fake))
    return fake, saved


def test_frames_keep_flowing_while_key_valid(monkeypatch):
    valid = {"flk_test_key": "usr_ws_test"}
    fake, saved = _run_handler(
        monkeypatch,
        ['{"type": "frame", "n": 1}', '{"type": "frame", "n": 2}'],
        valid)
    assert len(saved) == 2
    assert not any(code == 4401 for code, _ in fake.closed)


def test_revoked_key_closes_connection_and_stops_ingest(monkeypatch):
    """第一帧后吊销 key（模拟账号删除的 users 重载摘 key）——第二帧不得入库，
    连接必须以 4401 关闭。"""
    valid = {"flk_test_key": "usr_ws_test"}
    fake, saved = _run_handler(
        monkeypatch,
        ['{"type": "frame", "n": 1}', '{"type": "frame", "n": 2}'],
        valid,
        on_yield=lambda i: valid.clear() if i == 0 else None)
    assert len(saved) == 1, f"吊销后仍入库: {saved}"
    assert any(code == 4401 for code, _ in fake.closed)
