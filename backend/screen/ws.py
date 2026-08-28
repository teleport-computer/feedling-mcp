"""WebSocket frame-ingest server (port {WS_PORT} — see FEEDLING_WS_PORT)."""

import asyncio
import errno
import json
import os
import threading
from urllib.parse import parse_qs, urlparse

import websockets

from accounts import registry
from core import store as core_store
from core.store_sections import StoreSection
from screen import frames

WS_PORT = int(os.environ.get("FEEDLING_WS_PORT", 9998))


def _resolve_ws_user(websocket) -> tuple[str, str] | None:
    """Resolve user from WS connection. Returns (user_id, key), or None on auth failure.

    Reads ?key=... from the path, or "Bearer ..." from the Authorization
    header (whichever arrives first)."""
    # websockets lib v12+ uses websocket.request.path and .headers
    path = getattr(getattr(websocket, "request", None), "path", "") or ""
    key = None
    if "?" in path:
        try:
            q = parse_qs(urlparse(path).query)
            k = q.get("key", [""])[0].strip()
            if k:
                key = k
        except Exception:
            pass

    if not key:
        # websockets>=10 exposes headers via .request_headers or .request.headers
        headers = getattr(websocket, "request_headers", None) or getattr(
            getattr(websocket, "request", None), "headers", {}
        )
        auth = ""
        try:
            auth = headers.get("Authorization", "")
        except Exception:
            try:
                auth = headers["Authorization"]
            except Exception:
                auth = ""
        if auth and auth.lower().startswith("bearer "):
            key = auth[7:].strip()

    if not key:
        return None
    user_id = registry._resolve_user(key)
    if not user_id:
        return None
    return (user_id, key)


async def _ws_handler(websocket):
    try:
        resolved = _resolve_ws_user(websocket)
    except Exception as e:
        print(f"[ws] auth error: {e}")
        await websocket.close(code=4401, reason="unauthorized")
        return
    if not resolved:
        print("[ws] rejected: no valid key")
        await websocket.close(code=4401, reason="unauthorized")
        return
    user_id, ws_key = resolved

    store = core_store.get_store(user_id, require={StoreSection.FRAMES})
    store.last_seen_api_key = ws_key
    print(f"[ws] client connected user={user_id} peer={websocket.remote_address}")
    try:
        async for message in websocket:
            # 连接握手时鉴权一次不够：账号删除（users 行 CASCADE 删净）/ key 吊销后
            # 这条已建立的广播扩展 WS 还活着、每 ~10s 继续推帧——每帧对已不存在的
            # 用户写库（set_blob 撞 FK 被吞）、仍广播 store 变更，让其余 worker 反复
            # evict/reload 幽灵 store（2026-07-14 prod 实测 usr_25ce… 53 次/10min）。
            # _resolve_user 是内存 hash→uid 查表（账号删除的 users 广播已把 key 摘
            # 除），每帧重验几乎零成本；失效即 4401 关闭，扩展重连会在握手处被拒。
            if registry._resolve_user(ws_key) != user_id:
                print(f"[ws:{user_id}] key no longer valid (account deleted / key revoked) — closing")
                await websocket.close(code=4401, reason="unauthorized")
                return
            try:
                data = json.loads(message)
                if data.get("type") == "frame":
                    threading.Thread(target=frames._save_frame, args=(store, data), daemon=True).start()
            except Exception as e:
                print(f"[ws:{user_id}] parse error: {e}")
    except websockets.exceptions.ConnectionClosed:
        pass
    print(f"[ws:{user_id}] client disconnected")


async def _ws_main():
    try:
        async with websockets.serve(_ws_handler, "0.0.0.0", WS_PORT):
            print(f"[ws] WebSocket ingest server running on ws://0.0.0.0:{WS_PORT}/ingest")
            await asyncio.Future()
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"[ws] WARNING: port {WS_PORT} already in use — WebSocket ingest disabled, HTTP continues")
        else:
            raise


def _run_ws_server():
    asyncio.run(_ws_main())


def start():
    """Spawn the WS ingest thread. Called by the assembly layer via
    core.leader.run_singleton("ws", ...) in asgi/lifespan.py, so exactly one
    worker binds the port."""
    threading.Thread(target=_run_ws_server, daemon=True).start()
