"""V2 worker 进程入口 + 生产依赖装配（子项目 B，Task 8 扩到全流程）。

部署目标（已钉死，见 spec §2.1）：这是 backend 代码的 worker 镜像入口，运行在独立
runner CVM 的唯一 `serve-worker` service；hosted resident supervisor 已退役。
Genesis 已在 2026-07-10 rehome 到本进程的 dedicated thread。
它**不是**独立 repo，也**不**贴着主 app CVM 的 FastAPI backend 跑。HTTP 化会把
backend→enclave→backend 的 reentrant 502 根因请回来；贴主 app 跑则与 backend 争 CPU/内存。

装配层：这里（且只有这里）可同时 import hosted/core/model_api_runtime，把
需要上层的实现注入进 worker.TurnDeps，令 worker.py 保持不逆依赖（CONTRIBUTING §2）。
模型不再按 provider 身份预分类：所有 provider 都进入同一个 native tool loop，由实际
tool-call 行为自然降级。

生产 turn 依赖：
- resolve_provider：mint 一个 user-scoped runtime token → hosted.config_store 用它 JIT
  解密 provider key（单次；只留内存，不落库）。enclave-bound（受 worker.ENCLAVE_SEMAPHORE 框住）。
  BYOK-only：`_load_runtime_provider_config` 只从该用户自己的 `model_api_config` 信封解出
  provider key，从不读取/回退任何平台系统 key。
- mint_enclave_token：签发 enclave-auth runtime_token（scope=envelope_decrypt）。只是 HMAC
  签名，不是解密，回合内可按需多签（executor 的 capability 调用 + read_messages 的逐条
  chat 解密都要用它）。
- read_messages_after_seq：按 durable `v2_reply_cursor_seq` 读取 snapshot 上界内的 user
  行，逐条经 enclave 解密取明文（服务器永不本地解密）；绝不以 ts 或“最后一条
  assistant”切片，因为同 ts / 回复与 follow-up 交错都会吞消息。带上 id/ts/seq 供
  v2.coalesce 去重和精确推进。enclave-bound。

Model calls and reply construction are hosted-free and stay directly in the
worker/tool-loop layer; only hosted configuration, enclave access, and effect
sinks are assembled here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import signal
import socket
import sys
import threading
import time
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

# Put the backend dir on sys.path BEFORE importing backend modules. When this is
# run as a script (`python backend/model_api_runtime/v2/serve_worker.py` — how the
# runner compose starts it), sys.path[0] is the script's own dir (…/v2), NOT
# backend/ — so `from accounts import …` below would ImportError. Importing this
# module normally (tests, which already put backend/ on the path) is unaffected:
# the insert is a no-op.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from accounts import registry as accounts_registry  # noqa: E402
from admin import admin_core
from capabilities import registry as cap_registry
from capabilities import tool_schema as cap_tool_schema
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import runtime_token
from core import store as core_store
from core import wake_bus as core_wake_bus
from genesis import daemon as genesis_daemon
from hosted import config_store as hosted_config_store
from hosted import mcp_tools
from identity import identity_core
from memory import memory_core
from model_api_runtime.v2 import cursor as v2_cursor
from model_api_runtime.v2 import child_supervisor as v2_child_supervisor
from model_api_runtime.v2 import effect_id as v2_effect_id
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import reaper as v2_reaper
from model_api_runtime.v2 import scheduler
from model_api_runtime.v2 import screen_watch
from model_api_runtime.v2 import watchdog as v2_watchdog
from model_api_runtime.v2 import worker as v2_worker

# `turn_child` is NOT imported at module scope here: turn_child.py imports THIS module
# (to reuse wire_assembly/build_production_deps — see turn_child.py's docstring for why
# that's the correct direction of reuse) at ITS top level. If serve_worker also imported
# turn_child at top level, whichever module loads first would hand the other a
# still-initializing (partial) module object. `_serve` imports it lazily, by which point
# this module has long finished loading — see the import inside `_serve` below.
from proactive import capture_scheduler
from proactive import dream_scheduler
from proactive import gate as proactive_gate
from workspace.artifacts import ArtifactWorkspace, artifact_text_view_path
from workspace.backends import WorkspaceNotFound, model_writable_path
from workspace.prompt import render_trusted_prefix_blocks
from workspace.sandbox import (
    DisabledSandboxProvider,
    LazySandbox,
    SandboxRequiredOperation,
    SandboxUnavailable,
    SandboxUsageUnavailable,
    configured_sandbox_provider,
)
from workspace.service import production_backend as production_workspace_backend
import db
import memory_readside_core
import provider_client

log = logging.getLogger("feedling.runtime_v2.serve_worker")


def _load_workspace_prompt(store, *, runtime_token: str) -> dict:
    """Render one authoritative workspace prompt snapshot for a turn.

    Skill entries come only from the backend's read-only ``/skills`` namespace
    and therefore retain system authority. Agent-editable ``WORKING.md`` stays
    encrypted at rest and is available through ``workspace_read``; it is not
    eagerly injected, because persistent untrusted text must not be able to
    choose a future outbound web/MCP/subagent call. Any malformed/unknown skill
    block fails the turn instead of silently dropping policy.
    """
    backend = production_workspace_backend(
        store, runtime_token=str(runtime_token or "")
    )
    trusted_system_blocks: list[str] = []
    for block in render_trusted_prefix_blocks(
        backend,
        include_working_memory=False,
    ):
        if block.name.startswith("skill:/skills/"):
            trusted_system_blocks.append(block.content)
            continue
        raise RuntimeError("invalid workspace prompt block")
    return {
        "trusted_system_blocks": tuple(trusted_system_blocks),
        "working_memory": "",
    }


def _positive_float_env(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be finite and > 0") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be finite and > 0")
    return value


def _positive_int_env(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer > 0") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be an integer > 0")
    return value


def _workspace_write_parallelism() -> int:
    value = _positive_int_env(
        "FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM",
        "4",
    )
    if value > 8:
        raise RuntimeError(
            "FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM must be <= 8"
        )
    return value


_workspace_executor_lock = threading.Lock()
_workspace_executor: ThreadPoolExecutor | None = None
_workspace_executor_size = 0


def _workspace_write_executor() -> ThreadPoolExecutor:
    """Process-wide admission ceiling for durable workspace mutations.

    A per-batch pool would let every live turn fan out independently and move
    the old hidden concurrency cliff from provider threads to PostgreSQL
    connections.  One shared executor still overlaps disjoint operations while
    bounding nested sink connections across the whole worker process.
    """
    global _workspace_executor, _workspace_executor_size
    parallelism = _workspace_write_parallelism()
    with _workspace_executor_lock:
        if (
            _workspace_executor is None
            or _workspace_executor_size != parallelism
        ):
            previous = _workspace_executor
            _workspace_executor = ThreadPoolExecutor(
                max_workers=parallelism,
                thread_name_prefix="v2-workspace-write",
            )
            _workspace_executor_size = parallelism
            # Environment values are immutable in production.  This branch is
            # primarily useful to isolated tests that deliberately exercise
            # more than one configured ceiling in a single interpreter.
            if previous is not None:
                previous.shutdown(wait=True)
        return _workspace_executor


def _default_worker_id() -> str:
    """Replica-unique identity even when every container runs as PID 1."""
    raw_commit = os.environ.get("FEEDLING_GIT_COMMIT", "dev")
    # 7-char prefix to match the image tag + the CI liveness gate's
    # ``GITHUB_SHA[:7]`` build identity (CI #906 — the mainline fixed-width slice
    # the deploy waiters/pin steps use). A wider slice here reports a build id the
    # gate can't match, false-failing the post-deploy liveness check.
    build_commit = "".join(ch for ch in raw_commit if ch.isalnum())[:7] or "dev"
    return (
        f"v2-worker-{socket.gethostname()}-{os.getpid()}-"
        f"{uuid.uuid4().hex[:8]}-{build_commit}"
    )


_ASSISTANT_ROLES = ("openclaw", "assistant", "agent")
_EXTRACTION_ENABLED = os.environ.get(
    "FEEDLING_V2_EXTRACTION_ENABLED", "0"
).strip().lower() in {"1", "true", "yes", "on"}
_FILE_INLINE_MAX_CHARS = _positive_int_env(
    "FEEDLING_V2_FILE_INLINE_MAX_CHARS",
    "30000",
)


def _configure_db_pool_capacity(max_workers: int) -> int:
    """Guarantee enough pooled connections for every live V2 turn slot.

    ``effect_outbox.apply_pending_effects`` deliberately keeps one transaction
    and generation lock open while its sink performs the durable write.  Most
    sinks therefore need nested pooled connections.  A workspace batch holds
    the outer generation transaction while up to
    ``FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM`` durable CAS operations run on
    separate connections behind one process-wide admission ceiling. At the
    same time, every other active turn can hold its own outer generation
    transaction and one ordinary nested sink connection. With an unscaled
    pool, those mixed drains can deadlock waiting for nested writes (the exact
    silent concurrency cliff V2 is meant to remove). Conservatively reserve
    two connections per turn slot, the process-wide workspace fan-out, and
    four for queue, cursor, and reconciliation work. The historical backend
    floor of 16 is retained for smaller pools.

    An explicit operator override may raise the ceiling but may not undercut
    the invariant.  This runs before ``db.init_schema()`` opens the lazy pool;
    the spawned turn child inherits the resulting environment and revalidates
    it at its own startup.
    """
    try:
        slots = int(max_workers)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("FEEDLING_V2_MAX_WORKERS must be an integer > 0") from exc
    if slots <= 0:
        raise RuntimeError("FEEDLING_V2_MAX_WORKERS must be an integer > 0")

    workspace_parallelism = _workspace_write_parallelism()
    required = max(16, (2 * slots) + workspace_parallelism + 4)
    raw = os.environ.get("FEEDLING_DB_POOL_MAX_SIZE", "").strip()
    if raw:
        try:
            configured = int(raw)
        except ValueError as exc:
            raise RuntimeError(
                "FEEDLING_DB_POOL_MAX_SIZE must be an integer >= " + str(required)
            ) from exc
        if configured < required:
            raise RuntimeError(
                "FEEDLING_DB_POOL_MAX_SIZE="
                + str(configured)
                + " is too small for FEEDLING_V2_MAX_WORKERS="
                + str(slots)
                + "; require >= "
                + str(required)
            )
    else:
        configured = required
        os.environ["FEEDLING_DB_POOL_MAX_SIZE"] = str(configured)
    return configured


# Keep the existing underscore-form scope stable so a future scope-enforcement
# change (currently the enclave's local HMAC check only verifies
# signature+expiry+user_id, not scope — see enclave/auth.py) does not silently
# start rejecting the pooled worker.
_RUNTIME_TOKEN_SCOPE = ["envelope_decrypt"]

# Genesis mints its own token: a wider scope (it decrypts chunk envelopes AND calls
# the genesis apply route) and a much longer TTL — a history import routinely
# outlives the 900s chat token. Deliberately NOT folded into _RUNTIME_TOKEN_SCOPE:
# the per-turn chat path must keep the narrower scope.
#
# TTL and the poll interval are deliberately NOT hoisted into module-level
# constants (unlike _REAP_INTERVAL_SEC etc. below) — both are read from os.environ
# at CALL time, inside `_mint_genesis_token`/`_start_genesis_thread`. Those two
# functions are invoked lazily (once per process, well after import), and tests
# drive them via `monkeypatch.setenv` right before calling in; a module-level
# constant would freeze the value at import time and the monkeypatch would
# silently do nothing.
_GENESIS_TOKEN_SCOPE = ["envelope_decrypt", "genesis"]

# 本 V2 worker 触发 self-wake timer 时用的 owner_id。**必须**与 proactive_core 的
# RESIDENT_RUNTIME_OWNER_ID_V2（"resident_runtime_v2"）不同 —— fire_due_timers 的
# claim_due CAS 用 owner_id 标记谁抢到了 timer，若两者同名，一个 V2 worker 的 claim
# 会与常驻 runtime 的 claim 相互混淆/相互认作对方。
_V2_WAKE_OWNER_ID = "hosted_runtime_v2"

_IMAGE_MARKER = "[image]"
_UNAVAILABLE_CHAT_MARKER = "[message unavailable]"
_SUMMARY_INTEGRITY_ERROR = "v2_summary_integrity_error"
# 只为最近 N 个图片行解 caption。compaction 用 limit=10_000 调 _read_tail，没有这个上限
# 它会把该用户历史上每一张图的 caption 都发起一次 enclave 往返（enclave 是单线程瓶颈）。
_CAPTION_DECRYPT_LIMIT = 8


def _caption_envelope(m: dict) -> dict | None:
    """从 `caption_*` 前缀字段重建 caption 信封；无密文时 None。

    镜像 `enclave/routes/chat.py:79-92`。**必须**用 `caption_id`（不是消息自己的 id）——
    enclave 的 AEAD additional-data 是 `owner_user_id||v||id`，用错 id 会 AEAD 校验失败。
    """
    ct = str(m.get("caption_body_ct") or "").strip()
    if not ct:
        return None
    v = m.get("caption_v", m.get("v", 1))
    return {
        "id": m.get("caption_id") or m.get("id"),
        "v": int(v or 1),
        "body_ct": ct,
        "nonce": m.get("caption_nonce"),
        "K_enclave": m.get("caption_K_enclave"),
        "owner_user_id": m.get("caption_owner_user_id") or m.get("owner_user_id"),
    }


def _caption_text(m, *, mid, token, caption_budget: list[int], fallback: str) -> str:
    """附件行（image / file）的可见文本：随附件发的那句话，取不到就退化成 `fallback`。

    `caption_budget` 是一个单元素列表（可变计数器）：预算耗尽后不再解 caption。
    caption 解密失败同样静默退化——看不到那句话，好过整个回合失败。
    """
    cap_env = _caption_envelope(m)
    if cap_env is None or caption_budget[0] <= 0:
        return fallback
    caption_budget[0] -= 1
    try:
        caption = (
            core_enclave._decrypt_envelope_via_enclave(
                cap_env, None, purpose="v2_caption_read", runtime_token=token
            )
            .decode("utf-8", errors="replace")
            .strip()
        )
        return caption or fallback
    except Exception as e:  # noqa: BLE001 — 静默降级，绝不拖垮回合
        log.warning("[v2.serve_worker] caption decrypt failed msg=%s: %s", mid, e)
        return fallback


def _image_row(m, *, mid, ts, role, token, caption_budget: list[int]) -> dict:
    """图片行 -> **纯文本** tail 行 + 两个非敏感标记。绝不放 b64——compaction 共用这条读路径。"""
    text = _caption_text(
        m, mid=mid, token=token, caption_budget=caption_budget, fallback=_IMAGE_MARKER
    )
    return {
        "id": mid,
        "ts": ts,
        "role": role,
        "content": text,
        "has_image": True,
        "image_mime": m.get("image_mime") or "image/jpeg",
    }


def _file_row(m, *, mid, ts, role, token, caption_budget: list[int]) -> dict:
    """文件行 -> **纯文本** tail 行。**绝不解密 body_ct。**

    文件消息的明文是**原始文件字节**（`chat_send_core`: `user_plaintext = file_parse["bytes"]`，
    `content_type="file"`）。没有这个分支，这一行会掉进下面通用的
    `_decrypt_envelope_via_enclave(...).decode("utf-8")`，一个 PDF 立刻 `UnicodeDecodeError`
    —— 而它会**抛出整个 `_read_tail`**，连带打死该用户的 chat / wake / extraction /
    compaction 四条路径（compaction 用 limit=10_000 调同一个函数）。

    这个洞是两个各自正确的特性撞出来的：上游加文件上传时 V2 还不存在，V2 写这条读路径时
    `content_type == "file"` 还不存在。两边的测试都覆盖不到。

    `file_name` / `file_mime` 是**明文** extra 字段（`core/store.py:406-407`），所以渲染
    这一行连 enclave 都不用打——只有 caption 需要（和图片同一套 `caption_*` 信封，见
    `enclave/routes/chat.py:104-112`）。
    """
    name = str(m.get("file_name") or "file")
    text = _caption_text(
        m,
        mid=mid,
        token=token,
        caption_budget=caption_budget,
        fallback=f"[file: {name}]",
    )
    return {
        "id": mid,
        "ts": ts,
        "role": role,
        "content": text,
        "has_file": True,
        "file_name": name,
        "file_mime": m.get("file_mime") or "application/octet-stream",
    }


def _mint_runtime_token(user_id: str) -> str:
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip().encode("utf-8")
    if not secret:
        raise RuntimeError("FEEDLING_RUNTIME_TOKEN_SECRET not set")
    return runtime_token.mint(
        secret,
        user_id=user_id,
        runtime_instance_id="v2-worker",
        scope=_RUNTIME_TOKEN_SCOPE,
        ttl=900.0,
    )


def _mint_genesis_token(user_id: str, scopes: list[str] | None = None) -> str:
    """Genesis-scoped runtime token: wider scope + longer TTL than
    ``_mint_runtime_token`` (see ``_GENESIS_TOKEN_SCOPE`` docstring above).

    ``runtime_instance_id="v2-genesis"`` is a free-form label — `core.runtime_token
    .authorize` checks only `user_id` + `scope`, never `sub` — this mirrors
    `_mint_runtime_token`'s made-up `"v2-worker"` above.
    """
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip().encode("utf-8")
    if not secret:
        raise RuntimeError("FEEDLING_RUNTIME_TOKEN_SECRET not set")
    return runtime_token.mint(
        secret,
        user_id=user_id,
        runtime_instance_id="v2-genesis",
        scope=list(scopes or _GENESIS_TOKEN_SCOPE),
        ttl=float(os.environ.get("FEEDLING_GENESIS_RUNTIME_TOKEN_TTL_SEC", "7200")),
    )


def _prompt_cache_route_scope(runtime, *, secret: bytes) -> str:
    """Return an unambiguous provider/model/destination/credential namespace.

    The user HMAC must not be linkable across unrelated relays. ProviderConfig
    permits a custom HTTPS base URL even for ``provider='openai'``, so provider
    name alone is not enough: include the normalized destination (without
    credentials, query, or fragment) and rotate the resulting pseudonym when a
    user changes routes.
    """
    provider = provider_client.normalize_provider(
        str(getattr(runtime, "provider", "") or "")
    )
    model = str(getattr(runtime, "model", "") or "").strip().lower()
    api_key = str(getattr(runtime, "api_key", "") or "").strip().encode("utf-8")
    credential_fingerprint = hmac.new(
        secret,
        b"feedling:v2:prompt-cache-credential:v1\0" + api_key,
        hashlib.sha256,
    ).hexdigest()
    base_url = str(getattr(runtime, "base_url", "") or "").strip()
    if not base_url:
        base_url = provider_client.default_base_url(provider)
    parsed = urlsplit(base_url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    default_port = (scheme == "https" and port == 443) or (
        scheme == "http" and port == 80
    )
    normalized_port = None if port is None or default_port else int(port)
    path = parsed.path.rstrip("/")
    # Structured serialization avoids delimiter/IPv6 collisions (for example,
    # an IPv6 host ending in ``:8000`` versus the same prefix plus port 8000).
    return json.dumps(
        [
            provider,
            model,
            scheme,
            host,
            normalized_port,
            path,
            credential_fingerprint,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _resolve_provider(user_id: str):
    """单次解密该用户 provider key（enclave-bound，BYOK-only）。返回 (ProviderConfig|None, meta)。

    ``hosted_config_store._load_runtime_provider_config`` reads only this user's
    own ``model_api_config`` envelope (``api_key_envelope``) — there is no
    platform/system LLM key anywhere on this path. On any failure (unconfigured,
    untested, envelope missing, decrypt failure, invalid config) it returns a
    ``(None, {"error": ...})`` tuple; this function forwards that shape as-is so
    ``worker._run_turn`` can mark the job failed without a placeholder reply.
    """
    store = core_store.get_store(user_id)
    try:
        token = _mint_runtime_token(user_id)
    except Exception as e:  # noqa: BLE001
        return None, {"error": "runtime_token_mint_failed", "detail": str(e)[:160]}
    # api_key=None: hosted/host-all turns never hold the user's long-term
    # Feedling API key — only the runtime token authenticates to the enclave.
    runtime = hosted_config_store._load_runtime_provider_config(
        store, None, runtime_token=token
    )
    if isinstance(runtime, tuple):
        return None, runtime[1]
    # Per-user opaque affinity: provider caches must never receive the raw user
    # id, and the key must rotate with the runtime secret.  Exact prompt/model
    # matching remains the provider's correctness guard; this HMAC only improves
    # routing/stickiness for repeated prefixes.
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip().encode("utf-8")
    route_scope = _prompt_cache_route_scope(runtime, secret=secret)
    scope_bytes = route_scope.encode("utf-8")
    cache_key = hmac.new(
        secret,
        b"feedling:v2:prompt-cache:v3\0"
        + scope_bytes
        + b"\0"
        + str(user_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    route_fingerprint = hmac.new(
        secret,
        b"feedling:v2:prompt-cache-route:v1\0" + scope_bytes,
        hashlib.sha256,
    ).hexdigest()
    return replace(
        runtime,
        prompt_cache_key=f"feedling-v2-{cache_key}",
        prompt_cache_route_fingerprint=f"feedling-v2-route-{route_fingerprint}",
    ), {}


def _decrypt_chat_rows(
    user_id: str,
    rows: list[dict],
    *,
    user_only: bool,
    preserve_unreadable: bool = False,
) -> list[dict]:
    """Decrypt already-selected chat rows and preserve their exact seq IDs.

    Selection/bounding happens before this helper so enclave work stays
    proportional to the actual prompt window. Rows loaded through the new DB
    seq helpers carry ``seq``; legacy test/cache rows may omit it, in which
    case this compatibility helper leaves it absent. Seq-native readers set
    ``preserve_unreadable``: a local-only/missing-key/empty row becomes an
    explicit safe placeholder instead of disappearing before the durable
    cursor advances past its identity.
    """
    token = _mint_runtime_token(user_id)
    out: list[dict] = []
    caption_budget = [_CAPTION_DECRYPT_LIMIT]
    for m in rows:
        raw_role = str(m.get("role") or "")
        if user_only and raw_role != "user":
            continue
        mid, ts = m.get("id"), m.get("ts")
        role = "assistant" if raw_role in _ASSISTANT_ROLES else "user"
        if m.get("content_type") == "image":
            item = _image_row(
                m,
                mid=mid,
                ts=ts,
                role=role,
                token=token,
                caption_budget=caption_budget,
            )
        elif m.get("content_type") == "file":
            item = _file_row(
                m,
                mid=mid,
                ts=ts,
                role=role,
                token=token,
                caption_budget=caption_budget,
            )
        else:
            if not m.get("body_ct") or m.get("K_enclave") is None:
                if not preserve_unreadable:
                    continue  # legacy ts path keeps the historical skip rule
                item = {
                    "id": mid,
                    "ts": ts,
                    "role": role,
                    "content": _UNAVAILABLE_CHAT_MARKER,
                }
            else:
                plaintext = core_enclave._decrypt_envelope_via_enclave(
                    m, None, purpose="v2_chat_read", runtime_token=token
                ).decode("utf-8")
                if not plaintext.strip():
                    if not preserve_unreadable:
                        continue
                    plaintext = _UNAVAILABLE_CHAT_MARKER
                item = {"id": mid, "ts": ts, "role": role, "content": plaintext}
        if m.get("seq") is not None:
            item["seq"] = int(m["seq"])
        out.append(item)
    return out


def _read_messages_after_seq(
    user_id: str,
    after_seq: int = 0,
    *,
    through_seq: int | None = None,
) -> list[dict]:
    """Strict production catch-up reader keyed only by ``chat_messages.seq``.

    This intentionally bypasses the per-process ``UserStore`` cache. The DB
    helper returns every row after the durable cursor in exact seq order and
    raises on database failure, so same-timestamp user turns cannot disappear
    and a stale cache cannot manufacture an empty catch-up.

    The legacy ts reader remains available only for compatibility fixtures;
    ``build_production_deps`` wires this function as the production boundary.
    """
    rows = db.chat_messages_after_seq(
        user_id,
        int(after_seq),
        limit=None,
        through_seq=through_seq,
    )
    return _decrypt_chat_rows(
        user_id,
        rows,
        user_only=True,
        preserve_unreadable=True,
    )


def _read_messages(user_id: str, after_seq: int = 0) -> list[dict]:
    """Read user messages strictly after the durable **seq** reply cursor and
    decrypt them.

    Keyed on ``chat_messages.seq`` (a monotonic identity column), NEVER on
    wall-clock ``ts`` (D5 / spec A1): two messages appended in the same instant
    can share an identical ``ts``, and a late-delivered message can carry an
    earlier client-assigned ``ts`` than an already-answered one — a ``ts <=
    cursor`` boundary silently skips such a message FOREVER, stranding it in a
    reconcile no-op loop with no reply. ``seq`` has no such gap, so reading
    ``seq > after_seq`` never loses or re-answers a message. ``after_seq`` is
    the user's committed ``cursor.CURSOR_KEY`` (see
    ``model_api_runtime.v2.cursor.load_seq`` and the migration-owned legacy
    boundary conversion).

    Never infer unanswered state from the latest assistant position: a user
    message can arrive while a turn is running and be stored immediately
    before that turn's assistant row; assistant slicing would then hide it
    forever. The durable seq cursor is the source of truth.

    Rows come from ``db.chat_messages_after_seq`` (source of truth, ``seq``-
    ordered, seq merged into each doc) rather than the process-local store ring,
    precisely so each returned row can carry its ``seq`` without polluting the
    shared ``core.store`` cache with a seq key.

    服务器永不本地解密——每条信封走 enclave /v1/envelope/decrypt。 The full stored
    message dict is forwarded to ``_decrypt_envelope_via_enclave`` (not a
    hand-picked subset) — the enclave's AEAD additional-data is
    ``owner_user_id||v||id``, so dropping ``id`` would fail AEAD verification.

    Each returned dict carries ``id``/``ts``/``seq`` alongside ``role``/
    ``content``: ``coalesce.coalesce_pending`` dedupes by ``id`` and forwards
    ``seq`` so the turn advances the durable cursor to the max answered seq.

    This public compatibility name delegates to the unbounded strict reader
    used by production.  In particular, it must not cap an unseen backlog to
    the process-local chat ring size: every row after the durable cursor has to
    reach coalescing before that cursor can move.
    """
    return _read_messages_after_seq(user_id, int(after_seq))


def _read_tail_window(
    user_id: str, after_ts: float, limit: int, *, oldest_first: bool
) -> list[dict]:
    """读该用户最近一个窗口内的消息（BOTH roles），逐条经 enclave 解密取明文（D1）。

    镜像 `_read_messages` 的解密/过滤规则，但服务于不同目的：`_read_messages`
    只回放 durable cursor 之后的未答 user 消息；`_read_tail` 给 turn 看一段
    **真实的、双角色的**近期对话尾巴，所以：
    - 不按 last_assistant 下标切片、不跳过非 user 行；
    - 只保留 ts > after_ts 的行；
    - assistant 角色（`_ASSISTANT_ROLES`：openclaw/assistant/agent）规整为 "assistant"，
      其余规整为 "user"；
    - 过滤后只留最近 limit 条（`result[-limit:]`），保持时间序。

    Skip 规则与 `_read_messages` 一致：无 `body_ct` 或 `K_enclave is None` 的合成/
    本地-only 行跳过；`content_type == "image"` 走 "[image]" 简写，不经 enclave。"""
    store = core_store.get_store(user_id)
    reload_chat = getattr(store, "reload_chat_strict", None)
    if callable(reload_chat):
        rows = reload_chat()
    else:  # lightweight test doubles retain the older reload seam
        reload_store = getattr(store, "reload", None)
        if callable(reload_store):
            reload_store()
        rows = list(getattr(store, "chat_messages", []) or [])
    rows = sorted(rows, key=lambda m: m.get("ts") or 0.0)
    if limit <= 0:
        return []
    candidates = [m for m in rows if m.get("ts") is not None and m.get("ts") > after_ts]
    # Bound enclave work before decrypting. This also allocates the caption
    # budget to the messages that will actually reach the prompt.
    rows = candidates[:limit] if oldest_first else candidates[-limit:]
    return _decrypt_chat_rows(user_id, rows, user_only=False)


def _read_tail_window_after_seq(
    user_id: str,
    after_seq: int,
    limit: int,
    *,
    oldest_first: bool,
    through_seq: int | None = None,
) -> list[dict]:
    """Strict bounded two-role prompt window selected by exact seq identity."""
    rows = db.chat_messages_after_seq(
        user_id,
        int(after_seq),
        limit=int(limit),
        oldest_first=oldest_first,
        through_seq=through_seq,
    )
    return _decrypt_chat_rows(
        user_id,
        rows,
        user_only=False,
        preserve_unreadable=True,
    )


def _read_tail_after_seq(
    user_id: str,
    after_seq: int,
    limit: int,
    *,
    through_seq: int | None = None,
) -> list[dict]:
    """Newest bounded verbatim window after a summary seq watermark."""
    return _read_tail_window_after_seq(
        user_id,
        after_seq,
        limit,
        oldest_first=False,
        through_seq=through_seq,
    )


def _read_compaction_tail_after_seq(
    user_id: str,
    after_seq: int,
    limit: int,
    *,
    through_seq: int | None = None,
) -> list[dict]:
    """Oldest contiguous compaction batch after a summary seq watermark."""
    return _read_tail_window_after_seq(
        user_id,
        after_seq,
        limit,
        oldest_first=True,
        through_seq=through_seq,
    )


def _read_tail(user_id: str, after_ts: float, limit: int) -> list[dict]:
    """Newest bounded verbatim window for chat/wake context."""
    return _read_tail_window(user_id, after_ts, limit, oldest_first=False)


def _read_compaction_tail(user_id: str, after_ts: float, limit: int) -> list[dict]:
    """Oldest contiguous batch so summary watermarks never skip backlog rows."""
    return _read_tail_window(user_id, after_ts, limit, oldest_first=True)


def _read_summary_with_seq(user_id: str) -> tuple[str, float, int, int]:
    """Read/decrypt summary plus its exact seq coverage watermark.

    Returns ``(summary, watermark_ts, version, watermark_seq)``. The timestamp
    remains during migration/observability, but prompt selection and retention
    use only ``watermark_seq``. A missing row returns all-zero metadata.
    """
    row = jobs_store.get_summary_row(user_id)
    if row is None:
        return "", 0.0, 0, 0
    watermark_ts = float(row.get("watermark_ts") or 0.0)
    watermark_seq = int(row.get("watermark_seq") or 0)
    version = int(row.get("version") or 0)
    has_durable_coverage = watermark_seq > 0 or watermark_ts > 0
    env = row.get("summary_envelope")
    if not env:
        if has_durable_coverage:
            raise RuntimeError(
                f"{_SUMMARY_INTEGRITY_ERROR}: nonzero watermark without summary envelope"
            )
        return "", watermark_ts, version, watermark_seq
    token = _mint_runtime_token(user_id)
    plaintext = core_enclave._decrypt_envelope_via_enclave(
        env, None, purpose="v2_summary_read", runtime_token=token
    ).decode("utf-8")
    if has_durable_coverage and not plaintext.strip():
        raise RuntimeError(
            f"{_SUMMARY_INTEGRITY_ERROR}: nonzero watermark with empty summary plaintext"
        )
    return plaintext, watermark_ts, version, watermark_seq


def _read_summary(user_id: str) -> tuple[str, float, int]:
    """Backward-compatible three-field summary reader for pre-seq worker code.

    Worker integration should switch to :func:`_read_summary_with_seq`; keeping
    this wrapper prevents a half-landed infrastructure commit from breaking
    the currently wired ``TurnDeps`` tuple unpacking.
    """
    summary, watermark_ts, version, _watermark_seq = _read_summary_with_seq(user_id)
    return summary, watermark_ts, version


def _write_summary(
    user_id: str,
    summary: str,
    watermark_ts: float,
    expected_version: int,
    watermark_seq: int | None = None,
) -> bool:
    """把新压缩出的摘要**本地**加密（core_envelope，非 enclave 往返——跟 worker._write_encrypted_reply
    同一套写法）后 CAS 写回 v2_conversation_summary。信封构建失败（用户从未 onboard 过加密
    身份）时直接返回 False、不调用 CAS——调用方应当把本次压缩当作丢弃处理，不重试。

    ``watermark_seq``（D5/Task 9，可选第 5 参）跟 ``watermark_ts`` 在同一次 CAS 里原子
    推进——见 ``jobs_store.upsert_summary_row_cas``。``worker._run_compaction`` 算不出精确
    seq 时（罕见：折叠批次最后一行连 id 都没有）省略这个参数，CAS 只推进 ts，watermark_seq
    保持不变（COALESCE 在 jobs_store 那一层兜底，不在这里假造一个值）。"""
    store = core_store.get_store(user_id)
    env, err = core_envelope._build_shared_envelope_for_store(
        store, summary.encode("utf-8")
    )
    if env is None:
        log.warning(
            "[v2.serve_worker] _write_summary build envelope failed for %s: %s",
            user_id,
            err,
        )
        return False
    # Only pass watermark_seq through when the caller actually has one — keeps
    # the call shape identical to pre-D5 callers (incl. narrow-signature test
    # doubles) when it's omitted, rather than always sending an extra kwarg.
    if watermark_seq is not None:
        return jobs_store.upsert_summary_row_cas(
            user_id,
            summary_envelope=env,
            watermark_ts=watermark_ts,
            expected_version=expected_version,
            watermark_seq=watermark_seq,
            require_source_row=True,
        )
    return jobs_store.upsert_summary_row_cas(
        user_id,
        summary_envelope=env,
        watermark_ts=watermark_ts,
        expected_version=expected_version,
    )


def _wake_decision_for_user(user_id: str) -> dict:
    """Read-only heartbeat wake decision via the real proactive gate (assembly
    layer — reuses gate._build_proactive_v2_wake_decision so activation gate /
    broadcast suppression / all landmines hold with zero drift). No enqueue here;
    the scheduler decides what to do with should_wake."""
    store = core_store.get_store(user_id)
    if not hosted_config_store.hosted_runtime_v2_enabled_strict(store):
        return {
            "should_wake": False,
            "wake_interval_sec": 7200,
            "block_reason": "runtime_mode",
        }
    payload = {"trigger": "heartbeat"}
    d = proactive_gate._build_proactive_v2_wake_decision(store, payload)
    return {
        "should_wake": bool(d.get("should_wake_agent")),
        "wake_interval_sec": int(d.get("wake_interval_sec") or 7200),
        "block_reason": str(d.get("reason") or ""),
    }


def _fire_scheduled_for_user(user_id: str) -> int:
    """触发该用户所有到期的 self-wake timer，每个转成一个 `scheduled` lane 的 agent_job。

    复用 `ScheduledWakeServiceV2.fire_due_timers` 的注入缝：把 legacy 的
    `submit_wake`（塞进 proactive_jobs 流，V2 下无人排空 = BUG-3）换成 enqueue_job。
    claim/mark_fired 的原子性由 fire_due_timers 内部的 claim_due CAS 保证，多个
    scheduler 实例并发跑也不会重复触发。gate（未激活/免打扰）原样复用，零漂移。
    镜像 `proactive_core.scheduled_fire` 的 settings/service 构造，只换 owner_id 与
    submit_wake 的落点。"""
    if not hosted_config_store.hosted_runtime_v2_enabled_strict(
        core_store.get_store(user_id)
    ):
        return 0

    from proactive.controls_v2 import WakeControlDecisionV2
    from proactive.scheduled_wake_v2 import (
        DBScheduledWakeStoreV2,
        ScheduledWakeServiceV2,
    )
    from proactive.store_v2 import DBProactiveSettingsStoreV2

    settings = DBProactiveSettingsStoreV2().load(user_id)
    service = ScheduledWakeServiceV2(
        DBScheduledWakeStoreV2(), owner_id=_V2_WAKE_OWNER_ID
    )
    fired = 0

    def _submit(event):
        nonlocal fired
        jobs_store.enqueue_job(user_id, "scheduled", reason="scheduled_wake")
        core_wake_bus.notify("v2_jobs", user_id)
        fired += 1
        return WakeControlDecisionV2(True, "queued_v2", settings)

    service.fire_due_timers(
        user_id, settings=settings, submit_wake=_submit, owner_id=_V2_WAKE_OWNER_ID
    )
    return fired


def _read_images(user_id: str, message_ids: list[str]) -> dict[str, dict]:
    """按 id 取图片字节。复用内部 `chat_image_read` capability（见
    multimodal spec §4.0），但它不暴露给模型；worker 仅把最近图片作为
    native multimodal content block 注入 prompt，不经文本 grounding context。

    单条失败只跳过那一条，绝不抛——调用方 `worker._inject_tail_images` 会把缺失的图片
    行原样留成文本。
    """
    store = core_store.get_store(user_id)
    token = _mint_runtime_token(user_id)
    out: dict[str, dict] = {}
    for mid in message_ids:
        try:
            res = cap_registry.run_capability(
                "chat_image_read",
                store,
                api_key=None,
                runtime_token=token,
                params={"message_id": mid},
            )
            data = (res.to_dict() or {}).get("data") or {}
        except Exception as e:  # noqa: BLE001
            log.warning("[v2.serve_worker] image read failed msg=%s: %s", mid, e)
            continue
        if data.get("image_b64"):
            out[str(mid)] = {
                "image_mime": data.get("image_mime") or "image/jpeg",
                "image_b64": data["image_b64"],
            }
    return out


def _read_files(user_id: str, message_ids: list[str]) -> dict[str, dict]:
    """Read cached artifact text views or materialize through a real sandbox.

    Upload/storage remains unchanged. A cache miss acquires the configured
    sandbox *before* asking the enclave for physical bytes. No provider means a
    stable ``sandbox_unavailable`` result; there is no fallback to the old
    in-process PDF/DOCX/XLSX parser.
    """
    store = core_store.get_store(user_id)
    token = _mint_runtime_token(user_id)
    backend = production_workspace_backend(store, runtime_token=token)
    try:
        provider = configured_sandbox_provider()
    except SandboxUnavailable:
        # Stored encrypted text views remain readable even when a configured
        # provider adapter is absent; only a physical cache miss fails closed.
        provider = DisabledSandboxProvider()

    def _record_sandbox_acquire(event):
        return jobs_store.record_sandbox_acquisition(
            event.user_id,
            provider=event.provider,
            purpose=event.purpose,
        )

    def _record_sandbox_release(event) -> None:
        if type(
            event.usage_ref
        ) is not int or not jobs_store.finish_sandbox_acquisition(
            event.usage_ref,
            event.user_id,
            duration_ms=event.duration_ms,
            outcome=event.outcome,
        ):
            raise RuntimeError("sandbox usage row was not finalized")

    lazy = LazySandbox(
        provider,
        user_id=user_id,
        on_acquire=_record_sandbox_acquire,
        on_release=_record_sandbox_release,
    )
    artifacts = ArtifactWorkspace(backend, lazy)
    out: dict[str, dict] = {}
    try:
        rows_by_id = {
            str(row.get("id") or ""): row
            for row in (getattr(store, "chat_messages", None) or [])
            if isinstance(row, dict) and row.get("id")
        }
        for mid in message_ids:
            meta = rows_by_id.get(str(mid)) or {}
            name = str(meta.get("file_name") or "file")
            mime = str(meta.get("file_mime") or "application/octet-stream")
            view_path = artifact_text_view_path(str(mid), name)
            try:
                entry = artifacts.read_text_view(view_path)
            except WorkspaceNotFound:
                try:
                    # Acquire/bill before decrypted physical bytes enter this path.
                    lazy.ensure(SandboxRequiredOperation.MATERIALIZE_ARTIFACT)
                except SandboxUsageUnavailable as exc:
                    # Usage persistence is part of acquisition. Do not ask the
                    # enclave for bytes unless provider use is billable.
                    log.warning(
                        "[v2.serve_worker] sandbox acquisition audit failed "
                        "msg=%s type=%s",
                        mid,
                        type(exc).__name__,
                    )
                    out[str(mid)] = {
                        "file_name": name,
                        "file_mime": mime,
                        "error": "sandbox_usage_unavailable",
                    }
                    continue
                except SandboxUnavailable:
                    out[str(mid)] = {
                        "file_name": name,
                        "file_mime": mime,
                        "error": "sandbox_unavailable",
                    }
                    continue
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "[v2.serve_worker] sandbox acquisition failed msg=%s type=%s",
                        mid,
                        type(exc).__name__,
                    )
                    out[str(mid)] = {
                        "file_name": name,
                        "file_mime": mime,
                        "error": "sandbox_unavailable",
                    }
                    continue
                try:
                    res = cap_registry.run_capability(
                        "chat_file_read",
                        store,
                        api_key=None,
                        runtime_token=token,
                        params={"message_id": mid},
                    )
                    data = (res.to_dict() or {}).get("data") or {}
                    b64 = data.get("file_b64")
                    if not b64:
                        raise RuntimeError("artifact body unavailable")
                    name = str(data.get("file_name") or name)
                    mime = str(data.get("file_mime") or mime)
                    raw = base64.b64decode(b64, validate=True)
                    entry = artifacts.ingest(
                        source_ref=str(mid),
                        filename=name,
                        mime_type=mime,
                        data=raw,
                    )
                    view_path = entry.path
                except Exception as exc:  # noqa: BLE001 - one bad artifact is isolated
                    log.warning(
                        "[v2.serve_worker] sandbox artifact read failed msg=%s type=%s",
                        mid,
                        type(exc).__name__,
                    )
                    out[str(mid)] = {
                        "file_name": name,
                        "file_mime": mime,
                        "error": "artifact_read_unavailable",
                    }
                    continue
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[v2.serve_worker] artifact text-view read failed msg=%s type=%s",
                    mid,
                    type(exc).__name__,
                )
                out[str(mid)] = {
                    "file_name": name,
                    "file_mime": mime,
                    "error": "artifact_read_unavailable",
                }
                continue

            text = str(entry.content or "")
            if text:
                truncated = len(text) > _FILE_INLINE_MAX_CHARS
                out[str(mid)] = {
                    "file_name": name,
                    "file_mime": mime,
                    "text": text[:_FILE_INLINE_MAX_CHARS],
                    "truncated": truncated,
                    "workspace_path": view_path,
                }
    finally:
        lazy.close()
    return out


# 记忆抽取上下文一次拉多少张卡喂 dream prompt。dream 只需要一份粗粒度的现有卡片清单
# 供模型判断哪些能合并；不是全量导出，控制 prompt 体积 + 一次 enclave 往返的候选数。
_MEMORY_CARDS_LIMIT = int(os.environ.get("FEEDLING_V2_MEMORY_CARDS_LIMIT", "60"))


def _render_card_line(item: dict) -> str:
    """一张记忆卡渲染成一行（title/summary/content 里第一个非空的），仅供 dream prompt 参考。
    卡内容经 enclave readside 解出明文（见 memory_index_core），服务器本地不再解密。"""
    if not isinstance(item, dict):
        return ""
    text = str(
        item.get("title") or item.get("summary") or item.get("content") or ""
    ).strip()
    mid = str(item.get("id") or "").strip()
    if not text:
        return ""
    return f"- [{mid}] {text}" if mid else f"- {text}"


def _read_memory_context(user_id: str) -> dict:
    """capture/dream prompt 要的记忆上下文（buckets/threads/identity/cards 明文串）。

    **每一项独立 try/except 降级为 ""**（spec §3.5）：任一子取数失败绝不清空其它项、绝不
    抛——两个 prompt builder 对空串都会 fallback 到 "（暂无）"。buckets/threads/cards 走
    enclave readside（用 runtime token 认证，服务器不本地解密）；runtime token 铸造失败
    也只让这三项降级（token=""，post_enclave 会 raise 被各自 try 吞掉），不影响 identity。

    identity 是 E2E 信封，服务器侧拿不到明文人格文本 —— 只取 get_identity 暴露的顶层
    非敏感明文（如有），否则降级为 ""。ai_name/user_name 同样无服务器侧明文来源，保持 ""，
    由 prompt builder fallback（"我"/"TA"）。"""
    store = core_store.get_store(user_id)
    try:
        token = _mint_runtime_token(user_id)
    except Exception as e:  # noqa: BLE001 — token 铸造失败只让读侧降级，不抛
        log.warning(
            "[v2.serve_worker] memory context token mint failed for %s: %s", user_id, e
        )
        token = ""

    def _post(api_key, candidates, *, operation, payload=None):
        return memory_readside_core.post_enclave_readside(
            api_key,
            candidates,
            operation=operation,
            payload=payload,
            runtime_token=token,
        )

    ctx = {
        "ai_name": "",
        "user_name": "",
        "buckets": "",
        "threads": "",
        "identity": "",
        "cards": "",
    }
    try:
        body, status = memory_core.buckets(store, None, post_enclave=_post)
        if status == 200:
            ctx["buckets"] = ", ".join(str(b) for b in (body.get("buckets") or []))
    except Exception as e:  # noqa: BLE001 — 单项降级
        log.warning(
            "[v2.serve_worker] memory buckets unavailable for %s: %s", user_id, e
        )
    try:
        body, status = memory_core.threads(store, None, post_enclave=_post)
        if status == 200:
            ctx["threads"] = ", ".join(str(t) for t in (body.get("threads") or []))
    except Exception as e:  # noqa: BLE001 — 单项降级
        log.warning(
            "[v2.serve_worker] memory threads unavailable for %s: %s", user_id, e
        )
    try:
        body, status = memory_core.index(
            store, None, {"limit": _MEMORY_CARDS_LIMIT}, post_enclave=_post
        )
        if status == 200:
            lines = [_render_card_line(it) for it in (body.get("items") or [])]
            ctx["cards"] = "\n".join(ln for ln in lines if ln)
    except Exception as e:  # noqa: BLE001 — 单项降级
        log.warning("[v2.serve_worker] memory index unavailable for %s: %s", user_id, e)
    try:
        body, status = identity_core.get_identity(store)
        ident = body.get("identity") if isinstance(body, dict) else None
        if status == 200 and isinstance(ident, dict):
            # E2E 信封：服务器侧只有顶层非敏感明文（summary/title 若曾以明文写入），否则 ""。
            ctx["identity"] = str(
                ident.get("summary") or ident.get("title") or ""
            ).strip()
    except Exception as e:  # noqa: BLE001 — 单项降级
        log.warning("[v2.serve_worker] identity unavailable for %s: %s", user_id, e)
    return ctx


def _apply_memory_actions(
    user_id: str,
    actions: list[dict],
    *,
    runtime_token: str = "",
) -> dict:
    """把抽取产出的 memory.add / memory.supersede action 落库（复用 /v1/memory/actions 的
    服务端实现 memory_core.actions）。api_key=None：capture/dream 产出的 action 都自带完整
    E2E 信封（extraction._to_actions 的 build_envelope），memory.add 走 _memory_add_action。
    Tool-loop 的 plaintext memory.supersede 需要解密旧卡以继承 bucket/threads；因此这里
    也接受并向 memory_core/actions 转发短期 runtime_token。Extraction 的预建信封路径
    仍可用默认空 token，因为它不解旧卡。

    失败分两类（一个后台/工具发起的记忆写，绝不能因此拖垮用户的聊天回合）：

    - **5xx（瞬时：DB/enclave 抖动）** → 抛。让 outbox 重放重试，让
      `worker._run_extraction` 的自有 try/except 转成静默 mark_failed。
    - **4xx（永久：客户端侧被拒）** → 记 warning 后**丢弃、不抛**。典型是模型在
      一个多轮回合里对**已删/已改的卡重复操作**（`not_found` 404）、或产出一条校验
      不过的 action。这类错重放永远还是错，若抛出会经 `_sink_memory` →
      `on_reply` 的 `apply_pending_effects`（awaited）冒上去，把整个聊天回合打成
      `turn_failed:runtimeerror`——即使真正想做的写已经提交（实测：显式 id 删除，
      卡确实删了、turn 却失败）。丢弃+记日志：用户照常拿到回复，被拒的 action 在
      runner 日志里可见（body 打进 warning，供事后定位）。"""
    store = core_store.get_store(user_id)
    body, status = memory_core.actions(
        store,
        None,
        {"actions": actions},
        runtime_token=runtime_token,
    )
    if status >= 500:
        raise RuntimeError(
            f"memory_actions_failed: status={status} body={str(body)[:160]}"
        )
    if status >= 400:
        log.warning(
            "[v2.serve_worker] memory action rejected (4xx, dropped) user=%s status=%s body=%s",
            user_id,
            status,
            str(body)[:300],
        )
    return body


def _build_memory_envelope(user_id: str, inner: dict) -> dict:
    """把一张记忆卡的明文草稿 inner 封成 shared 客户端加密信封（E2E，本地加密，非 enclave
    往返 —— 同 worker._write_encrypted_reply / _write_summary 的写法）。

    **失败必抛**（不返回 None）：一张我们加密不了的记忆卡绝不能被静默丢弃。
    extraction._to_actions 在 build_envelope 抛出时会把整批 to_actions 一并冒出去，交给
    worker._run_extraction 的 try/except mark_failed —— 这正是我们要的：宁可整批失败重来，
    也不要把半张卡/无信封的卡塞进 memory.actions。"""
    store = core_store.get_store(user_id)
    payload = json.dumps(inner, ensure_ascii=False).encode("utf-8")
    env, err = core_envelope._build_shared_envelope_for_store(store, payload)
    if env is None:
        raise RuntimeError(f"memory_envelope_build_failed: {err}")
    return env


def _tick_capture_for_user(user_id: str) -> int:
    """跑一遍 capture 触发闸（`capture_scheduler.tick_quiet_capture`），注入把 job 塞进
    agent_jobs 的 submitter —— gate 的五道早退（capture_disabled/no_new_messages/
    already_captured/quiet_not_due/min_interval）原样复用，零漂移（spec §3.1）。enqueue 了
    返回 1，否则 0。"""
    store = core_store.get_store(user_id)

    def _submit(store, *, trigger, now):
        job_id, _coalesced = jobs_store.enqueue_job(user_id, "capture", reason=trigger)
        core_wake_bus.notify("v2_jobs", user_id)
        return {"enqueued": True, "reason": "v2", "job": {"id": job_id}}

    result = capture_scheduler.tick_quiet_capture(store, submit=_submit) or {}
    return 1 if result.get("enqueued") else 0


def _tick_dream_for_user(user_id: str) -> int:
    """跑一遍 dream 触发闸（`dream_scheduler.tick_memory_dream`），注入把 job 塞进 agent_jobs
    的 submitter —— gate 的全部早退（dream_disabled/no_memory_cards/dream_already_pending/
    night_not_due/failure_backoff/already_dreamed/min_interval/not_enough_new_cards）原样复用，
    零漂移。enqueue 了返回 1，否则 0。"""
    store = core_store.get_store(user_id)

    def _submit(store, *, trigger, now):
        job_id, _coalesced = jobs_store.enqueue_job(user_id, "dream", reason=trigger)
        core_wake_bus.notify("v2_jobs", user_id)
        return {"enqueued": True, "reason": "v2", "job": {"id": job_id}}

    result = dream_scheduler.tick_memory_dream(store, submit=_submit) or {}
    return 1 if result.get("enqueued") else 0


def _tick_extraction_for_user(user_id: str) -> int:
    """一个 scheduler dep 驱动两条抽取 lane：先 capture 再 dream，返回两者 enqueue 计数之和。
    两个触发闸各自独立（capture 看安静窗口，dream 看夜间+新卡阈值），互不影响。"""
    return _tick_capture_for_user(user_id) + _tick_dream_for_user(user_id)


def _latest_frame_meta(user_id: str) -> tuple[str, float]:
    """该用户最新一帧的 (frame_id, ts) —— 纯读 `db.frame_list_meta`（id/ts/app，绝不解密）。

    `frame_list_meta` 按 ts 升序返回（newest last，见 screen/caption.py:73/135），所以取
    `meta[-1]`。frame_id 从 `filename`（"<frame_id>.env.json"）派生，退回 `id`——与
    `screen/caption.py:_frame_id_from_entry` 同一套推导，但**内联复制**以免让本模块耦合
    进 screen 包的私有函数（且 screen/* 在本任务里是只读的）。无帧时返回 ("", 0.0)。"""
    meta = db.frame_list_meta(user_id)
    if not meta:
        return "", 0.0
    entry = meta[-1]
    filename = str(entry.get("filename") or "")
    if filename:
        # frame_id 是无点十六进制串，split(".")[0] 安全（镜像 _frame_id_from_entry）。
        frame_id = filename.removesuffix(".env.json").split(".")[0]
    else:
        frame_id = str(entry.get("id") or "")
    ts = entry.get("ts")
    return frame_id, float(ts) if ts is not None else 0.0


def _last_user_msg_ts(user_id: str) -> float | None:
    """该用户最后一条 `role == "user"` 消息的 ts —— 直接读 `store.chat_messages` 的明文
    `role`/`ts` 列（**绝不 enclave**：只有 `body_ct` 是密文，role/ts 是明文）。没有任何
    user 行时返回 None（gate 视作「未在对话」，不触发 chatting 让路）。"""
    store = core_store.get_store(user_id)
    rows = getattr(store, "chat_messages", []) or []
    last_ts: float | None = None
    for m in rows:
        if str(m.get("role") or "") == "user":
            ts = m.get("ts")
            if ts is not None:
                last_ts = float(ts)
    return last_ts


def _tick_screen_watch_for_user(user_id: str) -> int:
    """屏幕监看生产者（D-screen_watch Task 4）：替掉 resident 的 per-user 120s 循环。

    每 scheduler tick 对一个到期用户跑一遍**纯** gate `screen_watch.should_watch`，命中才
    再问只读 proactive oracle `_wake_decision_for_user`（未激活/Ambient-off/免打扰闸 + 零
    预激活烧钱不变量）。两关都过才 enqueue 一个 `screen_watch` lane 的 background job。

    两个 gate 输入都在服务端明文可得，**绝不碰 enclave**（这条每 120s 对每个 db_action_v2
    用户跑一次，一次 enclave 往返会锤死本子项目要保护的单线程瓶颈）：最新帧 id/ts 走
    `db.frame_list_meta`（不解密），last_screen_watch_frame_id 走 `get_wake_schedule`，
    last-user-msg ts 直接读 `store.chat_messages` 的明文 role/ts。

    **无论结果如何都推进** `next_screen_watch_at = now + INTERVAL_SEC`——否则一个被 gate
    挡下的用户会在每个 30s scheduler tick 都被重新考量。

    **只有真的 enqueue 时才落 `last_screen_watch_frame_id`**——这是**相对 resident 有意的
    修正**：resident 一旦 `fresh and changed` 就把 frame id 写进内存，即便这一 tick 随后被
    `chatting` 抑制，那一帧也永久被「消费」掉、用户停手后再也看不到。我们只在真醒时消费它。

    enqueue 了返回 1，否则 0。"""
    if not hosted_config_store.hosted_runtime_v2_enabled_strict(
        core_store.get_store(user_id)
    ):
        return 0
    now = time.time()
    latest_frame_id, latest_ts = _latest_frame_meta(user_id)
    sched = jobs_store.get_wake_schedule(user_id) or {}
    last_frame_id = str(sched.get("last_screen_watch_frame_id") or "")
    last_user_msg_ts = _last_user_msg_ts(user_id)

    next_at = now + screen_watch.INTERVAL_SEC
    should, _reason = screen_watch.should_watch(
        latest_frame_id=latest_frame_id,
        latest_ts=latest_ts,
        last_frame_id=last_frame_id,
        last_user_msg_ts=last_user_msg_ts,
        now=now,
    )

    if should and bool(_wake_decision_for_user(user_id).get("should_wake")):
        jobs_store.enqueue_job(user_id, "screen_watch", reason="screen_watch")
        core_wake_bus.notify("v2_jobs", user_id)
        # 真醒：推进到期时间 **且** 消费这一帧（记 last_screen_watch_frame_id）。
        jobs_store.upsert_wake_schedule(
            user_id,
            next_screen_watch_at=next_at,
            last_screen_watch_frame_id=latest_frame_id,
        )
        return 1

    # 被 gate（未 fresh/未变/chatting）或 oracle（未激活/免打扰）挡下：只推进到期时间，
    # **不**动 last_screen_watch_frame_id——那一帧仍是「新」的，用户停手后仍能被看到。
    jobs_store.upsert_wake_schedule(user_id, next_screen_watch_at=next_at)
    return 0


@dataclass
class EffectSinkDeps:
    """Injectable sink callables for `build_effect_dispatch` — one per outbox
    `effect_type` (spec A6). Each callable takes the payload dict (already
    annotated with `effect_id` by `effect_outbox.apply_pending_effects` — see
    that module) and performs the real durable write, or no-ops on replay.

    Deliberately `Callable[[dict], None]`, NOT `Callable[[str, dict], None]`:
    `effect_outbox.apply_pending_effects` calls `dispatch(effect_type, payload)`
    with no separate `user_id` argument (the outbox row's `user_id` never
    reaches the payload — see `db.effect_pending`'s SELECT). Production sinks
    that need `user_id` (all of them) get it from a closure built once per
    turn by `build_production_effect_dispatch(user_id)` below; tests just pass
    plain `lambda p: ...` fakes."""

    reply: Callable[[dict], None]
    status: Callable[[dict], None]
    cursor: Callable[[dict], None]
    job: Callable[[dict], None]
    memory: Callable[[dict], None]
    identity: Callable[[dict], None]
    schedule: Callable[[dict], None]
    workspace: Callable[[dict], None]


def build_effect_dispatch(deps: EffectSinkDeps) -> Callable[[str, dict], None]:
    """Pure router (spec A6): `dispatch(effect_type, payload) -> None`. Side-
    effect-free at construction — building the dispatch closure touches no DB;
    effects happen only when `dispatch(...)` is actually called. Unknown
    `effect_type` raises `ValueError` — a new effect kind shipping without a
    wired sink must fail loudly, not silently drop the effect."""
    routes: dict[str, Callable[[dict], None]] = {
        "reply": deps.reply,
        "status": deps.status,
        "cursor": deps.cursor,
        "job": deps.job,
        "memory": deps.memory,
        "identity": deps.identity,
        "schedule": deps.schedule,
        "workspace": deps.workspace,
    }

    def dispatch(effect_type: str, payload: dict) -> None:
        sink = routes.get(effect_type)
        if sink is None:
            raise ValueError(f"unknown effect_type: {effect_type!r}")
        sink(payload)

    return dispatch


def _sink_reply(user_id: str, payload: dict) -> None:
    """Persist a naturally-idempotent encrypted reply effect.

    New payloads contain ``envelope`` and optional ``reply_through_seq``; the
    deterministic envelope id and cursor-aware Store path atomically commit the
    final reply with its consumed input cursor. No generic pre-write claim is
    used on this path, eliminating the hard-crash claim-before-write loss gap.

    ``{"text": ...}`` remains a compatibility adapter for effects already
    pending during deployment of this change.

    `_write_encrypted_reply` returns ``None`` (does NOT raise) when the shared-
    envelope build fails (e.g. the user never onboarded encryption) — see its
    docstring. Treat that the same as a raised exception: release the claim
    and raise, so the effect stays pending / the turn surfaces the failure,
    instead of silently marking a never-written reply as applied (no-filler /
    reply-is-terminal invariant; restores the deleted
    `test_reply_envelope_failure_is_terminal_not_success` guarantee on the
    new PR A effect-sink path)."""
    envelope = payload.get("envelope")
    if isinstance(envelope, dict):
        store = core_store.get_store(user_id)
        v2_worker._write_encrypted_reply_effect(
            store,
            envelope,
            reply_through_seq=int(payload.get("reply_through_seq") or 0),
        )
        return

    eid = payload["effect_id"]
    if not db.effect_sink_claim(eid):
        return  # replay after a crash between sink-write and status=applied -> no-op
    try:
        store = core_store.get_store(user_id)
        row = v2_worker._write_encrypted_reply(store, str(payload.get("text") or ""))
        if row is None:
            raise RuntimeError("reply envelope build failed")
    except Exception:
        db.effect_sink_release(
            eid
        )  # write failed -> undo the claim so replay re-does it
        raise
    db.effect_sink_complete(eid)


def _sink_reply_in_transaction(user_id: str, payload: dict, connection):
    """Persist an encrypted V2 reply on the outbox transaction's connection.

    Only SQL runs before the caller's outer commit.  The returned thunk refreshes
    process-local cache and performs best-effort mirror/R2/capture work after
    commit, so none of those auxiliary systems can turn a durably published
    reply/job/effect into a reported failure.
    """
    envelope = payload.get("envelope")
    if not isinstance(envelope, dict):
        raise RuntimeError("transactional reply requires encrypted envelope")
    store = core_store.get_store(user_id)
    msg = store._build_chat_message("openclaw", "model_api", envelope)
    msg_id = str(msg["id"])
    seq, inserted, finish_db_post_commit = db.chat_append_effect_with_cursor(
        user_id,
        msg_id,
        float(msg["ts"]),
        msg,
        core_store.MAX_CHAT_MESSAGES,
        payload.get("reply_through_seq"),
        connection=connection,
        defer_post_commit=True,
    )
    msg["seq"] = int(seq)

    # PostgreSQL delivers NOTIFY only if the surrounding outbox transaction
    # commits.  This closes the commit->process-crash wake gap for other backend
    # workers without opening a second connection.
    wake_payload = json.dumps(
        {"u": user_id, "c": "chat", "o": core_wake_bus.WORKER_ID},
        separators=(",", ":"),
    )
    connection.execute(
        "SELECT pg_notify(%s, %s)",
        (core_wake_bus.PG_CHANNEL, wake_payload),
    )

    def post_commit() -> None:
        try:
            finish_db_post_commit()
        except Exception as exc:  # noqa: BLE001 — committed primary is authoritative
            log.warning(
                "[v2.reply] post-commit mirror/offload failed user=%s code=%s",
                user_id,
                type(exc).__name__.lower(),
            )
        try:
            store.reload_chat_strict()
        except Exception as exc:  # noqa: BLE001 — cross-worker reload/poll is fallback
            log.warning(
                "[v2.reply] local chat cache refresh failed user=%s code=%s",
                user_id,
                type(exc).__name__.lower(),
            )
        if inserted:
            try:
                capture_scheduler.record_chat_append(store, msg)
            except Exception as exc:  # noqa: BLE001 — auxiliary capture only
                log.warning(
                    "[v2.reply] capture scheduling failed user=%s code=%s",
                    user_id,
                    type(exc).__name__.lower(),
                )
        try:
            store.notify_chat_waiters()
        except Exception as exc:  # noqa: BLE001 — long-poll timeout remains fallback
            log.warning(
                "[v2.reply] local waiter wake failed user=%s code=%s",
                user_id,
                type(exc).__name__.lower(),
            )

    return post_commit


def _sink_status(user_id: str, payload: dict) -> None:
    """`status` sink. Payload keys: ``{"kind": str, "job_id"?: any, "label"?: str,
    "detail"?: dict}``. Reuses `jobs_store.append_status_event` — the same
    writer `worker._emit_status` calls."""
    eid = payload["effect_id"]
    if not db.effect_sink_claim(eid):
        return
    try:
        jobs_store.append_status_event(
            user_id,
            str(payload.get("kind") or ""),
            job_id=payload.get("job_id"),
            label=payload.get("label"),
            detail=payload.get("detail"),
        )
    except Exception:
        db.effect_sink_release(
            eid
        )  # write failed -> undo the claim so replay re-does it
        raise
    db.effect_sink_complete(eid)


def _sink_cursor(user_id: str, payload: dict) -> None:
    """`cursor` sink. Payload keys: ``{"new_seq": int}`` (see
    `model_api_runtime.v2.cursor.advance_effect`). Atomically advances
    `cursor.CURSOR_KEY` in the `model_api_runtime` profile via
    `db.advance_blob_int_strict`: sibling rollout/config keys are preserved and
    an older/replayed effect can never regress a newer cursor. The strict
    writer propagates DB/corrupt-state errors into the claim-release wrapper
    below instead of silently resetting or no-oping."""
    eid = payload["effect_id"]
    if not db.effect_sink_claim(eid):
        return
    try:
        db.advance_blob_int_strict(
            user_id,
            "model_api_runtime",
            v2_cursor.CURSOR_KEY,
            int(payload["new_seq"]),
        )
    except Exception:
        db.effect_sink_release(
            eid
        )  # write failed -> undo the claim so replay re-does it
        raise
    db.effect_sink_complete(eid)


def _sink_job(user_id: str, payload: dict) -> None:
    """`job` sink — generation-fenced follow-up enqueue. Payload keys:
    ``{"lane": str, "reason"?: str, "expected_generation"?: int}``.

    Defence in depth (A6 table note "fenced: only if generation still
    current"): `effect_outbox.apply_pending_effects` already fenced
    *application* of this effect under the row's `FOR UPDATE` lock before
    ever calling dispatch; this is a second, independent check specifically
    for the enqueue side-effect. Missing `expected_generation` (no fence data
    supplied) defaults to reading the CURRENT generation as "expected" — i.e.
    no additional fence beyond what the outbox already did.

    NOTE on claim-release: the generation-fence early-return below is NOT a
    write failure -- it means the effect is intentionally skipped, having
    legitimately consumed its claim, and must NOT be re-driven on replay. The
    claim is released only when the actual enqueue write raises."""
    eid = payload["effect_id"]
    if not db.effect_sink_claim(eid):
        return
    try:
        expected_generation = payload.get("expected_generation")
        if expected_generation is None:
            expected_generation = db.get_runtime_generation(user_id)
        current = db.get_runtime_generation(user_id)
        generation_matches = int(expected_generation) == int(current)
    except Exception:
        # No enqueue was attempted yet, so releasing this claim is safe.
        db.effect_sink_release(eid)
        raise
    if not generation_matches:
        log.debug(
            "[v2.serve_worker] job effect skipped: generation advanced user=%s "
            "expected=%s current=%s",
            user_id,
            expected_generation,
            current,
        )
        db.effect_sink_complete(eid)
        return  # intentional fence skip is a completed no-op, not a write failure
    try:
        # The outbox applier already holds this user's runtime-state row lock
        # across dispatch. Pass the generation it validated so enqueue_job
        # does not need to open a second connection and re-lock the same row
        # (which would self-deadlock until the outer effect transaction ends).
        jobs_store.enqueue_job(
            user_id,
            str(payload.get("lane") or "chat"),
            reason=payload.get("reason"),
            expected_generation=int(expected_generation),
        )
    except Exception:
        db.effect_sink_release(
            eid
        )  # write failed -> undo the claim so replay re-does it
        raise
    db.effect_sink_complete(eid)


def _sink_memory(user_id: str, payload: dict, *, runtime_token: str) -> None:
    """`memory` sink. Payload keys: ``{"actions": list[dict]}`` (memory.add /
    memory.supersede action dicts — see `_apply_memory_actions` docstring
    above). Reuses `_apply_memory_actions`, the same writer capture/dream
    extraction already calls."""
    eid = payload["effect_id"]
    if not db.effect_sink_claim(eid):
        return
    try:
        _apply_memory_actions(
            user_id,
            list(payload.get("actions") or []),
            runtime_token=runtime_token,
        )
    except Exception:
        db.effect_sink_release(
            eid
        )  # write failed -> undo the claim so replay re-does it
        raise
    db.effect_sink_complete(eid)


def _capability_effect_error(result, code: str) -> Exception:
    """Map a failed capability result to the right effect-outbox failure signal.

    A RETRYABLE failure (5xx/429 — transient) keeps the plain-``RuntimeError``
    retry path. A NON-RETRYABLE failure (4xx — deterministic; the write cannot
    succeed on replay, e.g. 409 ``identity_not_initialized``) raises
    ``EffectTerminalError`` so the outbox discards the effect instead of retrying
    forever and wedging the user's conversation.
    """
    retryable = bool((getattr(result, "error", None) or {}).get("retryable"))
    return RuntimeError(code) if retryable else db.EffectTerminalError(code)


def _sink_identity(user_id: str, payload: dict, *, runtime_token: str) -> None:
    """`identity` sink. Payload keys: ``{"patch": dict}`` OR top-level
    ``self_introduction``/``signature`` keys — mirrors
    `capabilities.identity.patch`'s own `params` contract exactly. No
    assembly-tier identity writer exists yet, so (per the brief) this calls
    the capability directly — a thin adapter, not a reimplementation."""
    eid = payload["effect_id"]
    if not db.effect_sink_claim(eid):
        return
    try:
        store = core_store.get_store(user_id)
        params = {k: v for k, v in payload.items() if k != "effect_id"}
        result = cap_registry.run_capability(
            "identity_patch",
            store,
            api_key=None,
            runtime_token=runtime_token,
            params=params,
        )
        if not result.ok:
            # Capability error bodies may contain user/provider details.  Raise
            # only a stable code.  Honor retryable: a deterministic 4xx (e.g. 409
            # identity_not_initialized on a fresh-start user) must terminal-discard
            # so the sweeper stops looping and the reply is not wedged; a retryable
            # 5xx keeps the claim-release retry path below.
            raise _capability_effect_error(result, "identity_patch_failed")
    except Exception:
        db.effect_sink_release(
            eid
        )  # write failed -> undo the claim so replay re-does it
        raise
    db.effect_sink_complete(eid)


_SCHEDULE_PAYLOAD_KEYS = (
    "next_heartbeat_at",
    "next_capture_at",
    "payment_cooldown_until",
    "next_screen_watch_at",
    "last_screen_watch_frame_id",
)


_SCHEDULE_CAPABILITY_OPS = frozenset({"schedule_wake", "cancel_wake"})


def _sink_schedule(user_id: str, payload: dict) -> None:
    """`schedule` sink — two distinct producers share this one effect_type:

    1. Worker's write-tool-call mapping (`worker.process_job`'s
       `_enqueue_write_effect`, spec C6): a `schedule_wake`/`cancel_wake` tool
       call becomes ``{"op": tc.name, **tc.args}`` — the user CAPABILITY params
       (``at``/``tz``/``reason`` for schedule_wake, ``wake_id``/``reason`` for
       cancel_wake; see `capabilities/wake.py`'s `schedule`/`cancel`). When
       ``payload["op"]`` names one of these, route through
       `cap_registry.run_capability` — the same dispatcher
       `_sink_identity`/executor already use for capability writes — so the
       capability actually runs, instead of falling through to
       `upsert_wake_schedule` with none of its kwargs present (a silent no-op:
       none of ``at``/``tz``/``reason``/``wake_id`` are in
       `_SCHEDULE_PAYLOAD_KEYS`, which are PR A/D's own wake-TIMING-TABLE
       fields, an unrelated shape).
    2. PR A/D producers (`_fire_scheduled_for_user`/`_tick_screen_watch_for_user`
       and future maintenance-lane wake-timing writers): payloads keyed by
       `_SCHEDULE_PAYLOAD_KEYS` (`next_heartbeat_at`/`next_capture_at`/
       `payment_cooldown_until`/`next_screen_watch_at`/
       `last_screen_watch_frame_id`) — no ``op`` key. Keep routing those
       through `jobs_store.upsert_wake_schedule`, unchanged.
    """
    eid = payload["effect_id"]
    if not db.effect_sink_claim(eid):
        return
    try:
        op = payload.get("op")
        if op in _SCHEDULE_CAPABILITY_OPS:
            store = core_store.get_store(user_id)
            params = {k: v for k, v in payload.items() if k not in ("effect_id", "op")}
            result = cap_registry.run_capability(op, store, api_key=None, params=params)
            if not result.ok:
                # Same retryable-honoring contract as _sink_identity: a
                # deterministic 4xx terminal-discards instead of wedging.
                raise _capability_effect_error(result, f"{op}_failed")
        else:
            kwargs = {k: v for k, v in payload.items() if k in _SCHEDULE_PAYLOAD_KEYS}
            jobs_store.upsert_wake_schedule(user_id, **kwargs)
    except Exception:
        db.effect_sink_release(
            eid
        )  # write failed -> undo the claim so replay re-does it
        raise
    db.effect_sink_complete(eid)


def _sink_workspace(user_id: str, payload: dict, *, runtime_token: str) -> None:
    """Apply one encrypted, revision-fenced virtual workspace mutation."""
    eid = payload["effect_id"]
    if not db.effect_sink_claim(eid):
        return
    try:
        op = str(payload.get("op") or "")
        if op not in {"workspace_write", "workspace_delete"}:
            raise db.EffectTerminalError("workspace_operation_invalid")
        store = core_store.get_store(user_id)
        params = {k: v for k, v in payload.items() if k not in ("effect_id", "op")}
        result = cap_registry.run_capability(
            op,
            store,
            api_key=None,
            runtime_token=runtime_token,
            params=params,
        )
        if not result.ok:
            raise _capability_effect_error(result, f"{op}_failed")
    except Exception:
        db.effect_sink_release(eid)
        raise
    db.effect_sink_complete(eid)


def _workspace_paths_conflict(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


def _workspace_mutation_waves(operations: list[dict]) -> list[list[dict]]:
    """Partition provider-ordered mutations into conflict-free waves.

    Same-path and ancestor/descendant operations stay ordered.  Disjoint paths
    in the same wave may reach their actual durable backend CAS concurrently.
    All paths are validated before the first wave starts, preventing one bad
    path from causing a surprising partial batch.
    """
    prepared: list[dict] = []
    try:
        for operation in operations:
            prepared.append(
                {
                    **operation,
                    "path": model_writable_path(operation.get("path")),
                }
            )
    except Exception as exc:
        raise db.EffectTerminalError(
            "workspace_operation_invalid"
        ) from exc

    waves: list[list[dict]] = []
    current: list[dict] = []
    for operation in prepared:
        path = str(operation["path"])
        if any(
            _workspace_paths_conflict(path, str(existing["path"]))
            for existing in current
        ):
            waves.append(current)
            current = []
        current.append(operation)
    if current:
        waves.append(current)
    return waves


def _apply_workspace_batch_operation(
    user_id: str,
    operation: dict,
    *,
    runtime_token: str,
) -> None:
    """Apply one child with its own crash-safe idempotency claim."""
    child_effect_id = str(operation["sub_effect_id"])
    if not db.effect_sink_claim(child_effect_id):
        return
    try:
        op = str(operation["op"])
        store = core_store.get_store(user_id)
        params = {
            key: value
            for key, value in operation.items()
            if key not in {"op", "sub_effect_id"}
        }
        result = cap_registry.run_capability(
            op,
            store,
            api_key=None,
            runtime_token=runtime_token,
            params=params,
        )
        if not result.ok:
            raise _capability_effect_error(result, f"{op}_failed")
    except Exception:
        # Only a write that raised is safe to retry.  A hard process death after
        # the backend commit leaves the child claim unresolved, and the parent
        # moves to explicit reconciliation instead of guessing.
        db.effect_sink_release(child_effect_id)
        raise
    db.effect_sink_complete(child_effect_id)


def _sink_workspace_batch(
    user_id: str,
    payload: dict,
    *,
    runtime_token: str,
) -> None:
    """Apply one encrypted workspace batch with real durable concurrency."""
    operations = list(payload.get("operations") or [])
    waves = _workspace_mutation_waves(operations)
    executor = _workspace_write_executor()
    for wave in waves:
        futures = [
            executor.submit(
                _apply_workspace_batch_operation,
                user_id,
                operation,
                runtime_token=runtime_token,
            )
            for operation in wave
        ]
        errors: list[Exception] = []
        # Read results in provider order. All futures are already running, so
        # this preserves deterministic error selection without serializing the
        # actual backend work.
        for future in futures:
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - re-raised after siblings settle
                errors.append(exc)
        if errors:
            # Uncertain delivery dominates deterministic conflicts and ordinary
            # retryable failures. Discarding the parent because an earlier
            # sibling hit a terminal CAS conflict would otherwise orphan a
            # later child whose durable outcome is genuinely unknown.
            for error in errors:
                if isinstance(error, db.EffectDeliveryUncertainError):
                    raise error
            raise errors[0]


def build_production_effect_dispatch(
    user_id: str,
    *,
    runtime_token_provider: Callable[[], str] | None = None,
) -> Callable[[str, dict], None]:
    """Wire the real sinks bound to a single user (see `EffectSinkDeps`
    docstring for why the binding happens here and not inside
    `build_effect_dispatch`). Called once per `apply_pending_effects(user_id,
    ...)` at end-of-turn (see `build_production_deps`'s `apply_pending_effects`
    field and its call site in `worker.process_job`)."""
    if runtime_token_provider is None:
        cached_runtime_token = ""

        def runtime_token_provider() -> str:
            nonlocal cached_runtime_token
            if not cached_runtime_token:
                cached_runtime_token = _mint_runtime_token(user_id)
            return cached_runtime_token

    deps = EffectSinkDeps(
        reply=lambda p: _sink_reply(user_id, p),
        status=lambda p: _sink_status(user_id, p),
        cursor=lambda p: _sink_cursor(user_id, p),
        job=lambda p: _sink_job(user_id, p),
        memory=lambda p: _sink_memory(
            user_id,
            p,
            runtime_token=runtime_token_provider(),
        ),
        identity=lambda p: _sink_identity(
            user_id,
            p,
            runtime_token=runtime_token_provider(),
        ),
        schedule=lambda p: _sink_schedule(user_id, p),
        workspace=lambda p: _sink_workspace(
            user_id,
            p,
            runtime_token=runtime_token_provider(),
        ),
    )
    return build_effect_dispatch(deps)


_ENCRYPTED_TOOL_EFFECT_TYPES = {
    stored: logical for logical, stored in v2_worker.ENCRYPTED_TOOL_EFFECT_TYPES.items()
}


def _decrypt_tool_effect_payload(
    user_id: str,
    payload: dict,
    *,
    runtime_token: str,
) -> dict:
    """Decrypt one model-authored write payload at the sink boundary.

    The outbox applier adds the trusted ``effect_id`` after loading the stored
    JSON.  Encrypted tool rows must therefore contain exactly that id plus one
    envelope: accepting plaintext siblings would accidentally re-introduce a
    second, unauthenticated source of write arguments.  Legacy plaintext rows
    (which have no ``effect_envelope`` key) remain deploy-safe.
    """
    if "effect_envelope" not in payload:
        return payload
    if set(payload) != {"effect_envelope", "effect_id"}:
        raise RuntimeError("invalid encrypted effect payload shape")
    effect_id = str(payload.get("effect_id") or "")
    envelope = payload.get("effect_envelope")
    if not effect_id or not isinstance(envelope, dict):
        raise RuntimeError("invalid encrypted effect payload")
    if str(envelope.get("id") or "") != v2_worker._tool_effect_item_id(effect_id):
        raise RuntimeError("encrypted effect id mismatch")
    owner_user_id = str(envelope.get("owner_user_id") or "")
    if owner_user_id != user_id:
        raise RuntimeError("encrypted effect owner mismatch")
    try:
        plaintext = core_enclave._decrypt_envelope_via_enclave(
            envelope,
            None,
            purpose="v2_effect_apply",
            runtime_token=runtime_token,
        )
        decoded = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("encrypted effect decrypt failed") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("encrypted effect plaintext must be an object")
    # Never trust an inner id even if old/future producers happen to include
    # one.  Sink idempotency is keyed only by the row-derived outer id.
    decoded["effect_id"] = effect_id
    return decoded


def _validate_decrypted_tool_effect(effect_type: str, payload: dict) -> None:
    """Re-validate plaintext immediately before any encrypted tool sink runs."""
    if effect_type == "memory":
        # The model-facing memory_write vocabulary is translated before it is
        # encrypted.  Re-running that model schema here would reject (or even
        # index into) the translated server action shape. Validate the actual
        # sink contract instead, after enclave decryption and before dispatch.
        if set(payload) != {"actions", "effect_id"}:
            raise RuntimeError("invalid encrypted memory effect shape")
        actions = payload.get("actions")
        if not isinstance(actions, list) or not actions:
            raise RuntimeError("invalid encrypted memory actions")
        for action in actions:
            if not isinstance(action, dict):
                raise RuntimeError("invalid encrypted memory action")
            action_type = str(action.get("type") or "")
            if action_type == "memory.delete":
                if (
                    set(action) != {"type", "memory_id"}
                    or not str(action.get("memory_id") or "").strip()
                ):
                    raise RuntimeError("invalid encrypted memory delete")
                continue
            if action_type not in {"memory.add", "memory.supersede"}:
                raise RuntimeError("invalid encrypted memory action type")
            allowed = {"type", "memory", "reason", "capture_mode"}
            if action_type == "memory.supersede":
                allowed.add("supersedes")
                if not str(action.get("supersedes") or "").strip():
                    raise RuntimeError("invalid encrypted memory supersede")
            if set(action) - allowed:
                raise RuntimeError("invalid encrypted memory action shape")
            memory = action.get("memory")
            if not isinstance(memory, dict):
                raise RuntimeError("invalid encrypted memory payload")
            if set(memory) == {"summary", "content", "bucket", "threads"}:
                if (
                    not str(memory.get("summary") or "").strip()
                    or not str(memory.get("content") or "").strip()
                ):
                    raise RuntimeError("invalid encrypted memory content")
                if not isinstance(memory.get("bucket"), str):
                    raise RuntimeError("invalid encrypted memory bucket")
                threads = memory.get("threads")
                if not isinstance(threads, list) or not all(
                    isinstance(thread, str) for thread in threads
                ):
                    raise RuntimeError("invalid encrypted memory threads")
            else:
                # Deploy compatibility for encrypted_v1 rows produced before
                # the model-facing op/summary/content vocabulary landed.
                legacy_fields = {
                    "type",
                    "title",
                    "description",
                    "summary",
                    "content",
                    "bucket",
                    "threads",
                }
                if set(memory) - legacy_fields:
                    raise RuntimeError("invalid legacy encrypted memory payload")
                if not str(memory.get("title") or memory.get("summary") or "").strip():
                    raise RuntimeError("invalid legacy encrypted memory title")
                if not str(
                    memory.get("description")
                    or memory.get("content")
                    or memory.get("summary")
                    or ""
                ).strip():
                    raise RuntimeError("invalid legacy encrypted memory content")
            if not isinstance(action.get("reason", ""), str) or not isinstance(
                action.get("capture_mode", ""), str
            ):
                raise RuntimeError("invalid encrypted memory metadata")
        return
    elif effect_type == "identity":
        tool_name = "identity_patch"
        args = {k: v for k, v in payload.items() if k != "effect_id"}
    elif effect_type == "schedule":
        tool_name = str(payload.get("op") or "")
        if tool_name not in _SCHEDULE_CAPABILITY_OPS:
            raise RuntimeError("invalid encrypted schedule operation")
        args = {k: v for k, v in payload.items() if k not in ("effect_id", "op")}
    elif effect_type == "workspace":
        tool_name = str(payload.get("op") or "")
        if tool_name not in {"workspace_write", "workspace_delete"}:
            raise RuntimeError("invalid encrypted workspace operation")
        args = {
            k: v
            for k, v in payload.items()
            if k not in ("effect_id", "op")
        }
    elif effect_type == "workspace_batch":
        if set(payload) != {"operations", "effect_id"}:
            raise RuntimeError("invalid encrypted workspace batch shape")
        parent_effect_id = str(payload.get("effect_id") or "")
        operations = payload.get("operations")
        if (
            not parent_effect_id
            or not isinstance(operations, list)
            or not operations
            or len(operations) > v2_worker.MAX_WORKSPACE_BATCH_OPERATIONS
        ):
            raise RuntimeError("invalid encrypted workspace batch")
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise RuntimeError("invalid encrypted workspace batch operation")
            tool_name = str(operation.get("op") or "")
            expected_fields = (
                {"op", "path", "content", "expected_revision", "sub_effect_id"}
                if tool_name == "workspace_write"
                else {"op", "path", "expected_revision", "sub_effect_id"}
                if tool_name == "workspace_delete"
                else set()
            )
            if not expected_fields or set(operation) != expected_fields:
                raise RuntimeError("invalid encrypted workspace batch operation")
            expected_child_id = v2_effect_id.derive_batch_item(
                parent_effect_id=parent_effect_id,
                ordinal=index,
            )
            if str(operation.get("sub_effect_id") or "") != expected_child_id:
                raise RuntimeError("invalid workspace batch child identity")
            args = {
                key: value
                for key, value in operation.items()
                if key not in {"op", "sub_effect_id"}
            }
            if cap_tool_schema.validate_tool_args(tool_name, args):
                raise RuntimeError("invalid encrypted workspace batch arguments")
        return
    else:
        raise RuntimeError("unsupported encrypted effect type")
    error = cap_tool_schema.validate_tool_args(tool_name, args)
    if error:
        raise RuntimeError("invalid encrypted effect arguments")


def _apply_pending_effects_for_user(user_id: str) -> dict:
    """`TurnDeps.apply_pending_effects` production wiring: run the generation-
    fenced outbox applier (spec A4) with this turn's real sinks (spec A6) at
    end-of-turn. Returns `{"applied": n, "discarded": m}` (see
    `effect_outbox.apply_pending_effects`)."""
    runtime_token = ""

    def get_runtime_token() -> str:
        nonlocal runtime_token
        if not runtime_token:
            runtime_token = _mint_runtime_token(user_id)
        return runtime_token

    production_dispatch = build_production_effect_dispatch(
        user_id,
        runtime_token_provider=get_runtime_token,
    )

    def dispatch(effect_type: str, payload: dict) -> None:
        if "effect_envelope" in payload:
            logical_effect_type = _ENCRYPTED_TOOL_EFFECT_TYPES.get(effect_type)
            if logical_effect_type is None:
                raise RuntimeError("encrypted payload on unsupported effect type")
            payload = _decrypt_tool_effect_payload(
                user_id,
                payload,
                runtime_token=get_runtime_token(),
            )
            _validate_decrypted_tool_effect(logical_effect_type, payload)
            if logical_effect_type == "workspace_batch":
                _sink_workspace_batch(
                    user_id,
                    payload,
                    runtime_token=get_runtime_token(),
                )
                return
            effect_type = logical_effect_type
        production_dispatch(effect_type, payload)

    return v2_effect_outbox.apply_pending_effects(
        user_id,
        dispatch=dispatch,
        dispatch_reply_in_transaction=(
            lambda _effect_type, payload, connection: _sink_reply_in_transaction(
                user_id, payload, connection
            )
        ),
    )


def _seal_trajectory_payload(
    user_id: str,
    plaintext: bytes,
    item_id: str,
) -> dict:
    """Seal flight-recorder content before any database write."""
    store = core_store.get_store(str(user_id))
    envelope, error = core_envelope._build_shared_envelope_for_store(
        store,
        bytes(plaintext),
        item_id=str(item_id),
    )
    if envelope is None:
        raise RuntimeError("trajectory_encryption_failed:" + str(error or "unknown"))
    return envelope


def _open_trajectory_payload(
    user_id: str,
    envelope: dict,
    runtime_token: str,
) -> bytes:
    """Open one event only inside the trusted worker for offline review."""
    if str(envelope.get("owner_user_id") or "") != str(user_id):
        raise RuntimeError("trajectory_owner_mismatch")
    return core_enclave._decrypt_envelope_via_enclave(
        envelope,
        None,
        purpose="runtime_v2_trajectory_review",
        runtime_token=str(runtime_token),
    )


def build_production_deps() -> v2_worker.TurnDeps:
    return v2_worker.TurnDeps(
        read_messages=_read_messages,
        read_messages_since=_read_messages,
        read_messages_after_seq=_read_messages_after_seq,
        runtime_mode_enabled=lambda user_id: (
            hosted_config_store.hosted_runtime_v2_enabled_strict(
                core_store.get_store(user_id)
            )
        ),
        resolve_provider=_resolve_provider,
        mint_enclave_token=_mint_runtime_token,
        read_tail=_read_tail,
        read_compaction_tail=_read_compaction_tail,
        read_tail_after_seq=_read_tail_after_seq,
        read_compaction_tail_after_seq=_read_compaction_tail_after_seq,
        read_summary=_read_summary,
        read_summary_with_seq=_read_summary_with_seq,
        write_summary=_write_summary,
        read_images=_read_images,
        read_files=_read_files,
        read_memory_context=_read_memory_context,
        apply_memory_actions=_apply_memory_actions,
        build_memory_envelope=_build_memory_envelope,
        apply_pending_effects=_apply_pending_effects_for_user,
        load_mcp_turn=mcp_tools.load_turn_mcp,
        load_workspace_prompt=_load_workspace_prompt,
        seal_trajectory_payload=_seal_trajectory_payload,
        open_trajectory_payload=_open_trajectory_payload,
    )


def _build_scheduler_deps():
    """装配 `model_api_runtime.v2.scheduler.run_scheduler_tick` 要的 deps（D3 Task 5）。
    scheduler.py 是纯模块（不 import hosted/agent_runtime/proactive）——这里把它接到真实
    实现：due_heartbeat_users（Task 2 落的 v2_wake_schedule 表）、_wake_decision_for_user
    （Task 3 适配器，包一层 proactive_gate，读专用，本身不 enqueue）、enqueue_job("heartbeat")
    /upsert_wake_schedule(next_heartbeat_at=...)。

    leader-election 有意跳过（见 D3 plan Task 5 说明）：`enqueue_job` 走
    ux_agent_jobs_singleflight 分区唯一索引，多个 serve_worker 进程的 scheduler tick
    并发对同一用户各自判定 should_wake 也只会各自 INSERT 一次、第二个撞唯一索引 coalesce
    成同一行——重复调度天然无害。prod 只跑一个 serve-worker 容器，这条不变量目前甚至用
    不上，但即使将来横向扩容也不需要另起一套选主。"""
    return types.SimpleNamespace(
        eligible_users=lambda: admin_core.list_runtime_modes().get(
            hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2, []
        ),
        runtime_mode_enabled=lambda uid: (
            hosted_config_store.hosted_runtime_v2_enabled_strict(
                core_store.get_store(uid)
            )
        ),
        due_users=lambda: jobs_store.due_heartbeat_users(),
        wake_decision=_wake_decision_for_user,
        enqueue_heartbeat=lambda uid: jobs_store.enqueue_job(uid, "heartbeat"),
        advance_heartbeat=lambda uid, next_at: jobs_store.upsert_wake_schedule(
            uid, next_heartbeat_at=next_at
        ),
        # scheduled lane 生产者（BUG-3）：due_scheduled_users（Task 2）列出到期 self-wake
        # timer 的用户，_fire_scheduled_for_user 逐用户走 fire_due_timers 把每个到期 timer
        # 转成一个 `scheduled` lane 的 agent_job。scheduler.py 用 getattr 探测这两个属性。
        due_scheduled_users=lambda: jobs_store.due_scheduled_users(),
        fire_scheduled=_fire_scheduled_for_user,
        # capture/dream 抽取 lane 生产者（Task 4）：extraction_users 列出当前处于 db_action_v2
        # 模式的用户（admin_core.list_runtime_modes 已按模式分组，取 db_action_v2 那组即
        # list_agent_runtime_enabled_users 的反集，无需另起一条查询）；_tick_extraction_for_user
        # 逐用户跑 capture+dream 触发闸，各自命中安静窗口/夜间阈值时 enqueue 一个抽取 job。
        # scheduler.py 同样用 getattr 探测这两个属性（缺一即整段跳过，既有 FakeDeps 零改动）。
        extraction_users=lambda: (
            admin_core.list_runtime_modes().get(
                hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2, []
            )
            if _EXTRACTION_ENABLED
            else []
        ),
        tick_extraction=_tick_extraction_for_user,
        # screen_watch lane 生产者（D-screen_watch Task 4）：screen_watch_users 列出
        # next_screen_watch_at 已到期的用户（Task 2 的 due_screen_watch_users，与
        # heartbeat/scheduled 同套 payment-cooldown 排除）；_tick_screen_watch_for_user
        # 逐用户跑纯 gate + 只读 oracle，命中才 enqueue 一个 screen_watch job。
        # scheduler.py 同样用 getattr 探测这两个属性（缺一即整段跳过，既有 FakeDeps 零改动）。
        screen_watch_users=lambda: jobs_store.due_screen_watch_users(),
        tick_screen_watch=_tick_screen_watch_for_user,
    )


def _seed_existing_v2_wake_schedules(*, now: float | None = None) -> int:
    """Idempotent startup backfill for users flipped before seeding existed."""
    due_at = time.time() if now is None else float(now)
    users = admin_core.list_runtime_modes().get(
        hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2, []
    )
    seeded = 0
    for user_id in users:
        if jobs_store.get_wake_schedule(user_id) is not None:
            continue
        jobs_store.upsert_wake_schedule(user_id, next_heartbeat_at=due_at)
        seeded += 1
    return seeded


def _reload_accounts_registry(_user_id: str) -> None:
    accounts_registry.load_users()


def wire_assembly() -> None:
    """复刻 asgi/lifespan.py 的关键接线（本进程无 lifespan）：注入 envelope pubkey getter、
    载入内存 registry、起 wake-bus listener、接上 "v2_jobs" 即时唤醒（FIX 3）。幂等
    （load_users/register_handler/start_listener 均自身幂等；重复调用安全）。

    "v2_jobs"：chat_send_core 入队新 job 后 NOTIFY 这个 channel；`v2_worker.on_v2_job_notify`
    只是把它桥到本进程 event loop 的一个 asyncio.Event（context 由 `_serve` 在 loop 起来后
    经 `v2_worker.set_job_wake_context` 设置——这里只负责注册 handler，不依赖 running loop）。"""
    core_envelope.get_user_public_key = accounts_registry._get_user_public_key
    accounts_registry.load_users()
    core_wake_bus.register_handler("users", _reload_accounts_registry)
    core_wake_bus.register_handler("v2_jobs", v2_worker.on_v2_job_notify)
    core_wake_bus.start_listener()


def build_health_app():
    """极薄 FastAPI，仅暴露 /healthz 供部署平台存活探针（spec §2.1）。"""
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "db": db.healthcheck()}

    return app


_REAP_INTERVAL_SEC = _positive_float_env("FEEDLING_V2_REAP_INTERVAL_SEC", "30")
_R2_CLEANUP_SETTINGS = v2_reaper.cleanup_settings_from_env()


async def _reaper_loop(
    stop_event: asyncio.Event, *, interval: float = _REAP_INTERVAL_SEC
) -> None:
    """Mirror the backend watchdog inside the worker process.

    It expires both queue-deadline and execution-lease timeouts. The ASGI
    lifespan runs the same DB-atomic reaper, so accepted pending rows still
    terminate visibly when the entire worker fleet is down; this copy reduces
    detection latency while a worker is healthy.

    每 ~interval 秒跑一次，interruptible（stop_event 置位时不必等满这个周期就退出，
    drain 更快）；单次 DB 错误只记日志、不杀进程——reaper 本身故障绝不能拖垮整个 worker
    进程（那样反而制造更多卡死 job）。"""
    await v2_reaper.run_loop(stop_event, interval=interval)


async def _r2_cleanup_loop(
    stop_event: asyncio.Event,
    *,
    interval: float = float(_R2_CLEANUP_SETTINGS["interval"]),
    limit: int = int(_R2_CLEANUP_SETTINGS["limit"]),
    inventory_limit: int = int(_R2_CLEANUP_SETTINGS["inventory_limit"]),
) -> None:
    """Run object-store cleanup independently from the deadline watchdog."""
    await v2_reaper.run_cleanup_loop(
        stop_event,
        interval=interval,
        limit=limit,
        inventory_limit=inventory_limit,
    )


_HEARTBEAT_INTERVAL_SEC = _positive_float_env(
    "FEEDLING_V2_HEARTBEAT_INTERVAL_SEC", "10"
)

# D3 (Task 4, PR-D plan): capacity must reflect the turn-child's ACTUAL health, not
# the constant `v2_worker.MAX_WORKERS` — otherwise a heartbeat tick ~10s after the
# watchdog (Task 3) writes capacity=0 on a kill would silently re-advertise full
# capacity for a child that is mid-SIGKILL/respawn, letting admission race new turns
# onto a pool slot that is not there yet. Independently env-configured (own var, own
# default) rather than importing `_CHILD_LIVENESS_TIMEOUT_SEC` (defined further below,
# next to `_watchdog_loop`) — the two constants intentionally share the same literal
# default ("45") so a heartbeat tick and a watchdog tick agree on "stuck" out of the
# box; an operator who deliberately diverges them via env var is doing so on purpose.
_CAPACITY_STALE_SEC = _positive_float_env("FEEDLING_V2_CAPACITY_STALE_SEC", "45")


async def _heartbeat_loop(
    worker_id: str,
    stop_event: asyncio.Event,
    *,
    supervisor: v2_child_supervisor.ChildSupervisor,
    interval: float = _HEARTBEAT_INTERVAL_SEC,
    capacity_stale_sec: float = _CAPACITY_STALE_SEC,
) -> None:
    """UPSERT this process's liveness row every ~interval seconds (Task 2: the
    db_action_v2 chat/send guard needs something to check — without this, a
    pool where every serve_worker process has died would queue jobs forever
    with no error). Reuses the same ``worker_id`` this process passes to
    ``claim_next_job``/``run_worker_loop`` — one row per live process.

    D3 (Task 4): ``capacity`` is DERIVED from ``supervisor.poll_liveness()`` each
    tick, not the constant ``v2_worker.MAX_WORKERS`` — 0 if the turn-child is dead
    OR its progress is older than ``capacity_stale_sec``, else ``MAX_WORKERS``. This
    is deliberately the same shape as (and agrees with) the watchdog's own kill
    threshold: whichever of the two loops ticks next while the child is down keeps
    writing capacity=0, so they reinforce rather than race each other back to a
    stale "full capacity" row (see the watchdog module docstring's "capacity=0 must
    be written before kill_and_respawn" note for the other half of this contract).

    Emits one heartbeat immediately on startup (before the first sleep) so a
    just-started pool is visible right away rather than only after the first
    full interval. Sleeps in a single ``wait_for(stop_event.wait(), timeout=...)``
    (mirrors ``_reaper_loop``) so ``stop_event.set()`` wakes it immediately
    instead of waiting out the rest of the interval — shutdown must not be
    delayed by a stale/soon-to-expire heartbeat row.
    """
    try:
        while not stop_event.is_set():
            try:
                liveness = supervisor.poll_liveness()
                age = liveness.get("last_progress_age_sec", math.inf)
                capacity = (
                    0
                    if (not liveness.get("alive", False) or age > capacity_stale_sec)
                    else v2_worker.MAX_WORKERS
                )
                await asyncio.to_thread(
                    jobs_store.record_worker_heartbeat,
                    worker_id,
                    capacity=capacity,
                    kind="turn",
                )
            except Exception as e:  # noqa: BLE001 — a heartbeat write failure must not kill the worker
                log.warning("[v2.serve_worker] record_worker_heartbeat failed: %s", e)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        # Do not leave a fresh full-capacity row behind during graceful drain.
        # A crash still ages out through the normal heartbeat TTL.
        try:
            await asyncio.to_thread(
                jobs_store.record_worker_heartbeat,
                worker_id,
                capacity=0,
                kind="turn",
            )
        except Exception as e:  # noqa: BLE001 — shutdown must remain bounded
            log.warning("[v2.serve_worker] clear worker capacity failed: %s", e)


_SCHEDULER_INTERVAL_SEC = _positive_float_env(
    "FEEDLING_V2_SCHEDULER_INTERVAL_SEC", "30"
)

# D2 (Task 3, watchdog.py) is the module that actually ACTS on this — comparing it
# against `child_supervisor.ChildSupervisor.poll_liveness()["last_progress_age_sec"]`
# to decide `should_kill`. It's defined here (not in watchdog.py) because the
# ChildSupervisor this constant configures is constructed in `_serve`, and this same
# value is threaded through to `_watchdog_loop`'s `child_liveness_timeout_sec` below
# so the supervisor's own liveness_timeout_sec and the watchdog's kill threshold never
# drift apart under a single env var.
_CHILD_LIVENESS_TIMEOUT_SEC = _positive_float_env(
    "FEEDLING_V2_CHILD_LIVENESS_TIMEOUT_SEC", "45"
)

# A turn has two different clocks and they must never be conflated:
#
# * stall timeout — no real in-turn boundary was crossed.  240s is above the
#   longest single adapter attempt (up to two 90s wires for extraction's
#   compatibility fallback). Reliable retries report a fresh boundary between
#   attempts, so their total envelope does not consume one continuous stall
#   budget.
# * absolute timeout — the turn keeps reporting progress but never terminates.
#   This must fit the configured 600s prompt catch-up plus up to two bounded
#   60s provider wires per round and setup/write margin.  The old single 180s
#   age ceiling was incompatible with both of those legitimate paths.
#
# Treat the old FEEDLING_V2_TURN_HARD_TIMEOUT_SEC as a compatibility alias for
# the *stall* timeout so an existing deployment does not silently turn 180s
# back into an unsafe absolute-age kill.
if os.environ.get("FEEDLING_V2_TURN_STALL_TIMEOUT_SEC", "").strip():
    _TURN_STALL_TIMEOUT_SEC = _positive_float_env(
        "FEEDLING_V2_TURN_STALL_TIMEOUT_SEC", "240"
    )
elif os.environ.get("FEEDLING_V2_TURN_HARD_TIMEOUT_SEC", "").strip():
    _TURN_STALL_TIMEOUT_SEC = _positive_float_env(
        "FEEDLING_V2_TURN_HARD_TIMEOUT_SEC", "240"
    )
else:
    _TURN_STALL_TIMEOUT_SEC = 240.0

# The longest single adapter attempt is extraction's 90s request; a provider
# wire may make one bounded compatibility fallback before returning.  Require a
# margin above 2 x 90s so a valid attempt can never be classified as a stall.
_MIN_TURN_STALL_TIMEOUT_SEC = 210.0
if _TURN_STALL_TIMEOUT_SEC < _MIN_TURN_STALL_TIMEOUT_SEC:
    raise RuntimeError(
        "FEEDLING_V2_TURN_STALL_TIMEOUT_SEC (or legacy "
        "FEEDLING_V2_TURN_HARD_TIMEOUT_SEC) must be at least 210s"
    )


# An MCP call only refreshes the per-turn stall clock after its bounded async
# dispatch settles. Leave explicit room for cancellation, child->parent progress
# propagation, and watchdog polling instead of accepting a call deadline right
# on the kill threshold.
_MCP_STALL_SAFETY_MARGIN_SEC = 30.0


def _validate_mcp_timeout_below_stall(
    *,
    mcp_call_timeout_sec: float,
    turn_stall_timeout_sec: float,
    safety_margin_sec: float = _MCP_STALL_SAFETY_MARGIN_SEC,
) -> None:
    if mcp_call_timeout_sec + safety_margin_sec >= turn_stall_timeout_sec:
        raise RuntimeError(
            "FEEDLING_V2_MCP_TOOL_CALL_TIMEOUT_SEC must be at least "
            f"{safety_margin_sec:.0f}s below "
            "FEEDLING_V2_TURN_STALL_TIMEOUT_SEC"
        )


_validate_mcp_timeout_below_stall(
    mcp_call_timeout_sec=v2_worker.MCP_TOOL_CALL_TIMEOUT_SEC,
    turn_stall_timeout_sec=_TURN_STALL_TIMEOUT_SEC,
)

_TURN_ABSOLUTE_TIMEOUT_SEC = _positive_float_env(
    "FEEDLING_V2_TURN_ABSOLUTE_TIMEOUT_SEC", "1800"
)
_CHAT_TURN_BUDGET_SEC = (
    float(v2_worker._PROMPT_CATCHUP_DEADLINE_SEC)
    # One OpenAI-compatible provider attempt may make a second full-timeout
    # wire request for a bounded reasoning/temperature compatibility fallback.
    + float(v2_worker._TURN_MAX_LLM_CALLS) * (2.0 * 60.0)
    # One cumulative allowance shared by every serial user-MCP call across all
    # provider rounds. The worker enforces this budget, so this is a real upper
    # bound rather than MAX_TOOL_CALLS_PER_TURN * the per-call timeout.
    + float(v2_worker.MCP_TURN_WALL_BUDGET_SEC)
    + 120.0
)
# capture/dream use three 90s reliable attempts plus bounded retry backoff;
# this is lower than the default chat budget but remains explicit so future
# tuning cannot accidentally make a background lane the unaccounted maximum.
_EXTRACTION_TURN_BUDGET_SEC = 3.0 * (2.0 * 90.0) + 6.0 + 120.0
_MIN_TURN_ABSOLUTE_TIMEOUT_SEC = max(_CHAT_TURN_BUDGET_SEC, _EXTRACTION_TURN_BUDGET_SEC)
if _TURN_ABSOLUTE_TIMEOUT_SEC < _MIN_TURN_ABSOLUTE_TIMEOUT_SEC:
    raise RuntimeError(
        "FEEDLING_V2_TURN_ABSOLUTE_TIMEOUT_SEC must cover prompt catch-up, "
        "all provider rounds, the MCP turn wall budget, and 120s "
        "setup/write margin "
        f"(minimum {_MIN_TURN_ABSOLUTE_TIMEOUT_SEC:.0f}s)"
    )
if _TURN_ABSOLUTE_TIMEOUT_SEC <= _TURN_STALL_TIMEOUT_SEC:
    raise RuntimeError(
        "FEEDLING_V2_TURN_ABSOLUTE_TIMEOUT_SEC must exceed "
        "FEEDLING_V2_TURN_STALL_TIMEOUT_SEC"
    )

_WATCHDOG_INTERVAL_SEC = _positive_float_env("FEEDLING_V2_WATCHDOG_INTERVAL_SEC", "10")
_WATCHDOG_DB_TIMEOUT_SEC = _positive_float_env(
    "FEEDLING_V2_WATCHDOG_DB_TIMEOUT_SEC", "5"
)


def _jobs_claimable() -> bool:
    """Watchdog's "is there work waiting" predicate (`watchdog.should_kill`'s
    `jobs_claimable` guard) — `pending_job_count()` counts only rows still
    `status='pending'` (queued, not yet claimed by ANY worker slot), which is
    exactly "all slots stuck while work waits": a wedged child claims nothing, so
    genuinely queued work sits at `pending` instead of draining into `claimed`/
    `running`. Deliberately NOT `inflight_job_count()` (pending+claimed+running) —
    that would stay truthy even while a healthy child is mid-turn on already-claimed
    work, which is not the "stuck and unable to make progress" signal this gates."""
    return jobs_store.pending_job_count() > 0


async def _watchdog_loop(
    supervisor: v2_child_supervisor.ChildSupervisor,
    worker_id: str,
    stop_event: asyncio.Event,
    *,
    interval: float = _WATCHDOG_INTERVAL_SEC,
) -> None:
    """Thin wrapper around `watchdog._watchdog_loop` (mirrors `_reaper_loop`'s
    delegation to `v2_reaper.run_loop`) — supplies the production
    `jobs_claimable_fn` + the two env-configured timeouts. Runs in the PARENT
    process/event loop (never the turn-child): it is the one piece of code with
    both a handle on `supervisor` and the authority to SIGKILL it."""
    await v2_watchdog._watchdog_loop(
        supervisor,
        worker_id,
        stop_event,
        jobs_claimable_fn=_jobs_claimable,
        interval=interval,
        turn_stall_timeout_sec=_TURN_STALL_TIMEOUT_SEC,
        turn_absolute_timeout_sec=_TURN_ABSOLUTE_TIMEOUT_SEC,
        child_liveness_timeout_sec=_CHILD_LIVENESS_TIMEOUT_SEC,
        jobs_claimable_timeout_sec=_WATCHDOG_DB_TIMEOUT_SEC,
        capacity_write_timeout_sec=_WATCHDOG_DB_TIMEOUT_SEC,
    )


async def _scheduler_loop(
    stop_event: asyncio.Event, *, interval: float = _SCHEDULER_INTERVAL_SEC
) -> None:
    """周期性跑一遍纯调度器（D3 Task 4 `scheduler.run_scheduler_tick`，Task 5 接线）：
    对每个到期用户判定是否唤醒 heartbeat（经 `_wake_decision_for_user` 复用真实
    proactive gate），should_wake 就 enqueue_job("heartbeat")（single-flight 去重、
    走 Task 2 的 lane 优先级），无论如何都 advance_heartbeat 推进下次到期时间——
    不会同一批用户每个 tick 都重新判一遍。

    镜像 `_reaper_loop`/`_heartbeat_loop` 的结构：interruptible 的
    `wait_for(stop_event.wait(), timeout=interval)`（stop_event 置位立刻醒，drain 不被
    卡满一个周期）；单次异常只记日志，绝不允许冒出去杀掉这个循环或拖垮跟它并发跑的
    run_worker_loop/reaper/heartbeat（scheduler.run_scheduler_tick 本身逐用户已经吞异常，
    这里再兜一层，防的是 deps 装配/DB 连接层面的意外）。

    leader-election 有意跳过——见 `_build_scheduler_deps` 的说明：single-flight 唯一索引
    让重复调度天然无害，prod 只跑一个 serve-worker 容器，不需要另起选主。

    `run_scheduler_tick` 是同步函数（内部只做 dict/list 操作 + 通过 deps 调用同步 DB
    函数），过 `asyncio.to_thread` 挪出 event loop，跟其它 loop 里所有同步 jobs_store
    调用的桥法一致。`now=time.time()`：装配层读墙钟，scheduler.py 自己不摸时钟（保持
    纯/可测）。"""
    while not stop_event.is_set():
        try:
            deps = _build_scheduler_deps()
            result = await asyncio.to_thread(
                scheduler.run_scheduler_tick, deps, now=time.time()
            )
            if result.get("considered"):
                log.info(
                    "[v2.serve_worker] scheduler tick considered=%s enqueued=%s skipped=%s",
                    result.get("considered"),
                    result.get("enqueued"),
                    result.get("skipped"),
                )
        except Exception as e:  # noqa: BLE001 — 瞬时故障绝不能杀掉 scheduler/worker 进程
            log.warning("[v2.serve_worker] scheduler tick failed: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


_RECONCILE_INTERVAL_SEC = _positive_float_env(
    "FEEDLING_V2_RECONCILE_INTERVAL_SEC", "60"
)


async def _reconcile_loop(
    stop_event: asyncio.Event, *, interval: float = _RECONCILE_INTERVAL_SEC
) -> None:
    """Periodic orphan-message and durable-effect reconciliation.

    D9 (Task 7, PR-D plan): periodic wiring for `db.reconcile_unenqueued_v2_messages`
    — the A7 orphan-message sweeper that was built but never invoked anywhere (its own
    docstring deferred the periodic call to "PR D's sweeper"). Without this loop, a
    `db_action_v2` user whose newest chat message never got a matching `agent_jobs`
    row (a bug, a manual data fix, or a message written before A7 existed) stays
    silently unanswered forever — nothing else in the pool re-derives "has an
    unanswered message" from `chat_messages` state; everything else keys off jobs
    that already exist.

    Runs in the PARENT process/event loop (never the turn-child) — sibling to
    `_reaper_loop`/`_heartbeat_loop`/`_scheduler_loop` in `_serve`'s task list, so a
    turn-child kill (watchdog SIGKILL, crash, respawn) never interrupts it; it must
    keep sweeping across turn-child churn.

    镜像 `_reaper_loop`/`_heartbeat_loop`/`_scheduler_loop` 的结构：interruptible 的
    `wait_for(stop_event.wait(), timeout=interval)`（stop_event 置位立刻醒，drain 不被
    卡满一个周期）；单次异常只记日志、绝不允许冒出去杀掉这个循环或拖垮跟它并发跑的
    其它 parent loop——sweeper 本身故障绝不能拖垮整个 worker 进程。

    `db.reconcile_unenqueued_v2_messages` is synchronous (plain psycopg calls), so it
    is bridged via `asyncio.to_thread` — same pattern as every other sync DB call in
    these parent loops.

    The same parent loop also recreates missing opt-in failure-review runners
    after a review kill-switch off→on transition, then drains due outbox rows
    independently of future turns. Transient failures receive exponential
    backoff; poison rows stop at ``needs_reconciliation`` and are surfaced by
    ``/v1/admin/v2-metrics``.
    """
    while not stop_event.is_set():
        try:
            enqueued = await asyncio.to_thread(db.reconcile_unenqueued_v2_messages)
            if enqueued:
                log.info(
                    "[v2.serve_worker] reconcile sweeper enqueued %d catch-up job(s)",
                    enqueued,
                )
        except Exception as e:  # noqa: BLE001 — 瞬时故障绝不能杀掉 sweeper/worker 进程
            log.warning("[v2.serve_worker] reconcile sweep failed: %s", e)
        try:
            review_runners = await asyncio.to_thread(
                jobs_store.reconcile_failure_review_runners
            )
            if review_runners:
                log.info(
                    "[v2.serve_worker] reconciled %d failure-review runner(s)",
                    review_runners,
                )
        except Exception as e:  # noqa: BLE001 — optional review cannot kill parent loop
            log.warning(
                "[v2.serve_worker] failure-review reconcile failed: %s",
                type(e).__name__,
            )
        try:
            pending_users = await asyncio.to_thread(db.effect_pending_users)
        except Exception as e:  # noqa: BLE001 — one DB failure must not kill the parent loop
            log.warning("[v2.serve_worker] effect reconcile listing failed: %s", e)
            pending_users = []
        for user_id in pending_users:
            try:
                result = await asyncio.to_thread(
                    _apply_pending_effects_for_user,
                    user_id,
                )
                if result.get("applied") or result.get("discarded"):
                    log.info(
                        "[v2.serve_worker] effect reconcile user=%s applied=%s discarded=%s",
                        user_id,
                        result.get("applied"),
                        result.get("discarded"),
                    )
            except Exception as e:  # noqa: BLE001 — isolate poison/uncertain rows per user
                # apply_pending_effects records a sanitized row-level last_error
                # and attempt_count before re-raising.  Keep sweeping other users;
                # this row remains visible and retryable/reconcilable.
                log.warning(
                    "[v2.serve_worker] effect reconcile failed user=%s type=%s",
                    user_id,
                    type(e).__name__,
                )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def _start_genesis_thread(worker_id: str):
    """Start the genesis import worker on a DEDICATED thread, or return None if dormant.

    Rehomed here from `agent_runtime.supervisor` (2026-07-10): genesis never depended
    on the resident CLI runtime — it needed an enclave URL, a token secret, and a
    long-running loop, and the former supervisor merely happened to be the only
    process in the CVM that had one. Keeping it here ensures resident retirement
    cannot stop draining `genesis_import_jobs`.

    NOT `asyncio.to_thread`: a tick blocks for the whole LLM reduce (minutes), and
    `to_thread` would park that in the loop's default executor — `min(32, cpu+4)`, i.e.
    6 threads on a 2-core CVM — which every turn coroutine's sync DB call also bridges
    through. Its own thread cannot starve them.

    The heartbeat (kind='genesis', invisible to `workers_alive`/`live_worker_count`)
    exists because this thread can die while the process lives on: `run_loop` imports
    `genesis.worker` lazily, so an ImportError kills only the thread and the turn loops
    keep beating happily. Today that failure is completely silent.
    """
    enabled = os.environ.get("FEEDLING_GENESIS_WORKER_ENABLED", "")
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip()
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").strip()
    api_url = os.environ.get("FEEDLING_API_URL", "").strip()

    if not genesis_daemon.should_start(
        enabled=enabled, secret=secret, enclave_url=enclave_url
    ):
        if enabled:
            log.warning(
                "[v2.serve_worker] FEEDLING_GENESIS_WORKER_ENABLED set but "
                "prerequisites missing (need FEEDLING_RUNTIME_TOKEN_SECRET + "
                "FEEDLING_ENCLAVE_URL) — genesis worker dormant"
            )
        return None

    genesis_worker_id = f"{worker_id}:genesis"
    stop_event = threading.Event()
    # Read at CALL time (not a module-level constant) — see the comment beside
    # _GENESIS_TOKEN_SCOPE above for why.
    interval = _positive_float_env("FEEDLING_GENESIS_WORKER_INTERVAL_SEC", "10")
    hb_interval = _positive_float_env("FEEDLING_GENESIS_HEARTBEAT_SEC", "15")
    # Live-worker liveness window for the orphan reclaim. >= dead cutoff so a
    # heartbeating worker is reliably "live"; a worker dead longer than this drops
    # out and its claims become reclaimable.
    dead_sec = max(
        60, int(os.environ.get("FEEDLING_GENESIS_WORKER_DEAD_SEC", "120") or "120")
    )

    def _beat() -> None:
        jobs_store.record_worker_heartbeat(
            genesis_worker_id, kind="genesis", capacity=0
        )

    thread = threading.Thread(
        target=genesis_daemon.run_loop,
        daemon=True,
        name="v2-genesis",
        kwargs={
            "api_url": api_url,
            "enclave_url": enclave_url,
            "mint_genesis": _mint_genesis_token,
            "interval": interval,
            "stop_event": stop_event,
            "worker_id": genesis_worker_id,
            "list_live_workers": lambda: jobs_store.live_genesis_worker_ids(
                within_sec=dead_sec
            ),
            "on_beat": _beat,
        },
    )
    thread.start()

    # Independent heartbeat: a tick blocks for the whole LLM reduce (minutes), so
    # the before/after-tick beats above freeze during a distill — making a LIVE
    # worker look dead to the orphan reclaim and risking a false reclaim of its own
    # in-flight job. This timer beats every hb_interval regardless of the tick, so
    # a mid-distill worker stays truthfully alive. It gates on the run_loop thread
    # being alive: if that thread dies (e.g. a lazy-import failure — the very
    # "thread dies while the process lives" mode the heartbeat exists to expose),
    # the timer stops beating so the worker correctly reads as dead and its jobs
    # get reclaimed instead of being masked as alive.
    def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            if not thread.is_alive():
                break
            _beat()
            stop_event.wait(hb_interval)

    threading.Thread(target=_heartbeat_loop, daemon=True, name="v2-genesis-hb").start()

    log.info(
        "[v2.serve_worker] genesis worker enabled — interval=%.0fs hb=%.0fs "
        "dead=%ds worker_id=%s",
        interval,
        hb_interval,
        dead_sec,
        genesis_worker_id,
    )
    return thread, stop_event


async def _serve(worker_id: str, *, poll_interval: float) -> None:
    """PARENT process loop (D1 结构拆分后，见
    `docs/superpowers/plans/2026-07-13-hosted-runtime-v2-PR-D-pool-history-safety.md`
    Task 2). turn slot 不再是这里的 `asyncio.create_task`——它们跑在一个由
    `child_supervisor.ChildSupervisor` spawn/监督的独立子进程里（`turn_child.main`）,
    这样一个 slot 里卡死的同步调用不会拖死本进程的事件循环，本进程才能保住
    SIGKILL 那个子进程的权力。`_reaper_loop`/`_heartbeat_loop`/`_scheduler_loop`/
    Genesis 线程未改动，仍在本（父）进程里跑。

    D2（Task 3）在 `tasks` 里加了 `_watchdog_loop`，读 `supervisor.poll_liveness()` 判定
    卡死并调 `supervisor.kill_and_respawn()`——子进程崩溃/卡死不会让 `asyncio.gather(*tasks)`
    跟着报错退出（父进程能在没有存活子进程的情况下继续跑并把它救回来，这正是 Task 2 要求的
    "子进程崩溃不能带崩父进程"，Task 3 在此基础上补上"卡死了要主动救"）。
    """
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    seeded = await asyncio.to_thread(_seed_existing_v2_wake_schedules)
    if seeded:
        log.info("[v2.serve_worker] seeded %d existing V2 wake schedule(s)", seeded)
    log.info(
        "[v2.serve_worker] starting worker=%s max_workers=%s",
        worker_id,
        v2_worker.MAX_WORKERS,
    )

    # Local import — see the comment beside the module-level import block for why
    # `turn_child` cannot be imported at this module's top level (circular import:
    # turn_child.py imports `serve_worker` at ITS top level to reuse wire_assembly/
    # build_production_deps).
    from model_api_runtime.v2 import turn_child

    # Genesis runs on its own dedicated thread (NOT part of this asyncio.gather —
    # see _start_genesis_thread docstring for why it can't share the to_thread
    # executor with the reaper/heartbeat/scheduler coroutines below). Unaffected by
    # the turn-child split: it never lived in the turn slots' event loop.
    genesis = _start_genesis_thread(worker_id)

    supervisor = v2_child_supervisor.ChildSupervisor(
        turn_child.main,
        liveness_timeout_sec=_CHILD_LIVENESS_TIMEOUT_SEC,
        spawn_args=(worker_id, poll_interval),
    )
    # Synchronous: spawns the child process + starts the parent-side progress-pipe
    # reader thread. Not an asyncio task — the child runs in its own OS process, so
    # there is nothing here for `asyncio.gather` to await; its liveness is polled
    # (Task 3) rather than joined.
    await asyncio.to_thread(supervisor.start)

    tasks = [
        asyncio.create_task(_reaper_loop(stop_event)),
        asyncio.create_task(_r2_cleanup_loop(stop_event)),
        asyncio.create_task(
            _heartbeat_loop(worker_id, stop_event, supervisor=supervisor)
        ),
        asyncio.create_task(_scheduler_loop(stop_event)),
        asyncio.create_task(_watchdog_loop(supervisor, worker_id, stop_event)),
        asyncio.create_task(_reconcile_loop(stop_event)),
    ]
    try:
        # Each loop handles recoverable iteration failures internally.  An
        # escaping exception means the worker is degraded; propagate it to
        # `_run_forever`, which relaunches this service loop instead of leaving
        # a live heartbeat with dead work.
        await asyncio.gather(*tasks)
    finally:
        stop_event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # SIGTERM the child + join (falls back to SIGKILL past the drain timeout);
        # mirrors the graceful-drain contract the parent's own loops follow above.
        await asyncio.to_thread(supervisor.stop)
        if genesis is not None:
            genesis_thread, genesis_stop = genesis
            genesis_stop.set()
            # Bounded: a tick in flight finishes its current LLM call. daemon=True means
            # a wedged tick can never block process exit.
            await asyncio.to_thread(genesis_thread.join, 10.0)
    log.info("[v2.serve_worker] drained; exiting worker=%s", worker_id)


# In-process relaunch backoff bounds (see _run_forever). Bounded exponential:
# a transient crash recovers within a second; a persistent crash loop is throttled
# to one relaunch per 30s so it cannot hot-spin the CPU or hammer the DB/provider.
_RELAUNCH_BACKOFF_MIN_SEC = 1.0
_RELAUNCH_BACKOFF_MAX_SEC = 30.0


def _run_forever(worker_id: str, poll_interval: float) -> None:
    """Run ``_serve``, relaunching it in-process after a fatal Python crash.

    Relaunching here recovers faster than tearing down the whole container when
    an exception escapes ``_serve``. A clean SIGTERM/SIGINT drain returns
    normally and exits without relaunching. This cannot recover a hard
    interpreter death such as OOM-kill/SIGKILL; the rollout must separately
    prove that the container restart policy handles that process-level failure.
    """
    backoff = _RELAUNCH_BACKOFF_MIN_SEC
    while True:
        try:
            asyncio.run(_serve(worker_id, poll_interval=poll_interval))
            return
        except Exception:  # noqa: BLE001 — fatal loop escapes must relaunch
            log.exception(
                "[v2.serve_worker] _serve crashed; relaunching in %.0fs "
                "(in-process recovery)",
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, _RELAUNCH_BACKOFF_MAX_SEC)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    db_pool_max = _configure_db_pool_capacity(v2_worker.MAX_WORKERS)
    # Schema single-point for this standalone process (idempotent — see
    # db.init_schema docstring). The ASGI backend's gunicorn on_starting also
    # runs this once per deploy; this worker is its own entrypoint in the runner
    # CVM (see module docstring) with no shared master migration hook, so it must
    # not assume the schema is already at head.
    db.init_schema()
    wire_assembly()
    worker_id = (
        os.environ.get("FEEDLING_V2_WORKER_ID", "").strip() or _default_worker_id()
    )
    poll_interval = _positive_float_env("FEEDLING_V2_POLL_INTERVAL_SEC", "1.0")
    log.info(
        "[v2.serve_worker] configured db_pool_max=%s for max_workers=%s",
        db_pool_max,
        v2_worker.MAX_WORKERS,
    )
    _run_forever(worker_id, poll_interval)


if __name__ == "__main__":
    main()
