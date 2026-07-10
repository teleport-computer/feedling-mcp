"""V2 worker 进程入口 + 生产依赖装配（子项目 B，Task 8 扩到全流程）。

部署目标（已钉死，见 spec §2.1 与 merge-conditions-backlog 条件 5）：这是**同一 backend/
镜像的兄弟入口**，运行在 **runner CVM（agent-runner supervisor）** 内，与常驻 consumer、
genesis import worker 并肩——**不是**独立 HTTP 服务、**不是**独立 repo、**不**贴着主 app
CVM 的 FastAPI backend 跑。HTTP 化会把 backend→enclave→backend 的 reentrant 502 根因请回来；
贴主 app 跑则与 backend 争 CPU/内存。实际进程启动（manifest/compose）属子项目 D 的 rollout，
本文件只钉死「跑在哪」这一决定。

装配层：这里（且只有这里）可同时 import hosted/agent_runtime/core/model_api_runtime，把
需要上层的实现注入进 worker.TurnDeps，令 worker.py 保持不逆依赖（CONTRIBUTING §2）。
worker.py 明确不 import `hosted` 或 `agent_runtime.spawners`——official/非官方判定（用户是
否在跑受信任模型）需要 `agent_runtime.spawners._is_official_identity`，那是 hosted-adjacent
的东西，所以只在这里包一层，经 `TurnDeps.is_official` 注入进去。

生产 turn 依赖：
- resolve_provider：mint 一个 user-scoped runtime token → hosted.config_store 用它 JIT
  解密 provider key（单次；只留内存，不落库）。enclave-bound（受 worker.ENCLAVE_SEMAPHORE 框住）。
  BYOK-only：`_load_runtime_provider_config` 只从该用户自己的 `model_api_config` 信封解出
  provider key，从不读取/回退任何平台系统 key。
- is_official：包一层 `agent_runtime.spawners._is_official_identity`（driver/endpoint 是否
  官方原生）——这是 worker.py 不能自己算的唯一原因，就是这个函数活在 hosted-adjacent 的
  agent_runtime 包里。
- mint_enclave_token：签发 enclave-auth runtime_token（scope=envelope_decrypt）。只是 HMAC
  签名，不是解密，回合内可按需多签（executor 的 capability 调用 + read_messages 的逐条
  chat 解密都要用它）。
- read_messages：读该用户 chat_messages 中自上一条 assistant 之后的 user 行，逐条经 enclave
  解密取明文（服务器永不本地解密）；带上 id/ts 供 v2.coalesce 按 since_ts 过滤/去重。enclave-bound。

respond/append_reply 不再是 TurnDeps 的字段：worker.process_job 现在直接调用同层的
`model_api_runtime.v2.responder.respond`（本就 hosted-free）和自己的
`_write_encrypted_reply`（只 import core.envelope/core.store，同样 hosted-free），不需要
再经这层装配转发一次。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
import types
from pathlib import Path

# Put the backend dir on sys.path BEFORE importing backend modules. When this is
# run as a script (`python backend/model_api_runtime/v2/serve_worker.py` — how the
# runner compose starts it), sys.path[0] is the script's own dir (…/v2), NOT
# backend/ — so `from accounts import …` below would ImportError. Mirrors the same
# bootstrap in agent_runtime/supervisor.py. Importing this module normally (tests,
# which already put backend/ on the path) is unaffected: the insert is a no-op.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from accounts import registry as accounts_registry  # noqa: E402
from admin import admin_core
from agent_runtime import spawners as agent_spawners
from capabilities import registry as cap_registry
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import runtime_token
from core import store as core_store
from core import wake_bus as core_wake_bus
from genesis import daemon as genesis_daemon
from hosted import config_store as hosted_config_store
from identity import identity_core
from memory import memory_core
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import scheduler
from model_api_runtime.v2 import screen_watch
from model_api_runtime.v2 import worker as v2_worker
from proactive import capture_scheduler
from proactive import dream_scheduler
from proactive import gate as proactive_gate
import db
import memory_readside_core

log = logging.getLogger("feedling.runtime_v2.serve_worker")

_ASSISTANT_ROLES = ("openclaw", "assistant", "agent")

# Scope name matches the existing host-all/genesis-worker convention
# (agent_runtime/supervisor.py mints "envelope_decrypt", not a colon-form) so a
# future scope-enforcement change (currently the enclave's local HMAC check
# only verifies signature+expiry+user_id, not scope — see enclave/auth.py
# local_user_id_from_token) doesn't silently start rejecting this worker.
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
        caption = core_enclave._decrypt_envelope_via_enclave(
            cap_env, None, purpose="v2_caption_read", runtime_token=token
        ).decode("utf-8", errors="replace").strip()
        return caption or fallback
    except Exception as e:  # noqa: BLE001 — 静默降级，绝不拖垮回合
        log.warning("[v2.serve_worker] caption decrypt failed msg=%s: %s", mid, e)
        return fallback


def _image_row(m, *, mid, ts, role, token, caption_budget: list[int]) -> dict:
    """图片行 -> **纯文本** tail 行 + 两个非敏感标记。绝不放 b64——compaction 共用这条读路径。"""
    text = _caption_text(m, mid=mid, token=token, caption_budget=caption_budget,
                         fallback=_IMAGE_MARKER)
    return {"id": mid, "ts": ts, "role": role, "content": text,
            "has_image": True, "image_mime": m.get("image_mime") or "image/jpeg"}


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
    text = _caption_text(m, mid=mid, token=token, caption_budget=caption_budget,
                         fallback=f"[file: {name}]")
    return {"id": mid, "ts": ts, "role": role, "content": text,
            "has_file": True, "file_name": name,
            "file_mime": m.get("file_mime") or "application/octet-stream"}


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
    runtime = hosted_config_store._load_runtime_provider_config(store, None, runtime_token=token)
    if isinstance(runtime, tuple):
        return None, runtime[1]
    return runtime, {}


def _read_messages(user_id: str) -> list[dict]:
    """读该用户自上一条 assistant 之后的 user 消息，逐条经 enclave 解密成明文文本。
    服务器永不本地解密——每条信封走 enclave /v1/envelope/decrypt。

    The full stored message dict is forwarded to ``_decrypt_envelope_via_enclave``
    (not a hand-picked subset) — mirrors ``content.content_core._build_rewrapped_envelope``,
    which passes the whole ``record``. This matters: the enclave's AEAD
    additional-data is ``owner_user_id||v||id`` (see ``enclave/envelope.py
    decrypt_envelope`` / ``build_aead_aad``), so dropping the message's ``id``
    would make every real (non-synthetic) chat envelope fail AEAD verification.

    Each returned dict carries ``id``/``ts`` alongside ``role``/``content`` (Task 8):
    ``model_api_runtime.v2.coalesce.coalesce_pending`` filters by ``ts > since_ts`` and
    dedupes by ``id`` — Plan B's shape (``role``/``content`` only) predates the v2
    coalesce/planner/executor pipeline and didn't need either field.
    """
    store = core_store.get_store(user_id)
    rows = list(getattr(store, "chat_messages", []) or [])
    # 找到最后一条 assistant 回复的下标；只回放其后的 user 消息（未答的那批）。
    last_assistant = -1
    for idx, m in enumerate(rows):
        if str(m.get("role") or "") in _ASSISTANT_ROLES:
            last_assistant = idx
    pending = rows[last_assistant + 1:]
    token = _mint_runtime_token(user_id)
    out: list[dict] = []
    caption_budget = [_CAPTION_DECRYPT_LIMIT]
    for m in pending:
        if str(m.get("role") or "") != "user":
            continue
        mid, ts = m.get("id"), m.get("ts")
        if m.get("content_type") == "image":
            out.append(_image_row(m, mid=mid, ts=ts, role="user",
                                   token=token, caption_budget=caption_budget))
            continue
        if m.get("content_type") == "file":
            out.append(_file_row(m, mid=mid, ts=ts, role="user",
                                  token=token, caption_budget=caption_budget))
            continue
        if not m.get("body_ct") or m.get("K_enclave") is None:
            continue  # 无 enclave 钥的合成/本地-only 消息跳过
        plaintext = core_enclave._decrypt_envelope_via_enclave(
            m, None, purpose="v2_chat_read", runtime_token=token
        ).decode("utf-8")
        if plaintext.strip():
            out.append({"id": mid, "ts": ts, "role": "user", "content": plaintext})
    return out


def _read_tail(user_id: str, after_ts: float, limit: int) -> list[dict]:
    """读该用户最近一个窗口内的消息（BOTH roles），逐条经 enclave 解密取明文（D1）。

    镜像 `_read_messages` 的解密/过滤规则，但服务于不同目的：`_read_messages` 只回放
    "自上一条 assistant 回复之后"的未答 user 消息（喂 coalesce/planner 判断本回合要不要
    起新 job）；`_read_tail` 给 turn 看一段**真实的、双角色的**近期对话尾巴（喂 responder
    的上下文窗口），所以：
    - 不按 last_assistant 下标切片、不跳过非 user 行；
    - 只保留 ts > after_ts 的行；
    - assistant 角色（`_ASSISTANT_ROLES`：openclaw/assistant/agent）规整为 "assistant"，
      其余规整为 "user"；
    - 过滤后只留最近 limit 条（`result[-limit:]`），保持时间序。

    Skip 规则与 `_read_messages` 一致：无 `body_ct` 或 `K_enclave is None` 的合成/
    本地-only 行跳过；`content_type == "image"` 走 "[image]" 简写，不经 enclave。"""
    store = core_store.get_store(user_id)
    rows = list(getattr(store, "chat_messages", []) or [])
    rows = sorted(rows, key=lambda m: m.get("ts") or 0.0)
    token = _mint_runtime_token(user_id)
    out: list[dict] = []
    caption_budget = [_CAPTION_DECRYPT_LIMIT]
    for m in rows:
        ts = m.get("ts")
        if ts is None or not (ts > after_ts):
            continue
        mid = m.get("id")
        role = "assistant" if str(m.get("role") or "") in _ASSISTANT_ROLES else "user"
        if m.get("content_type") == "image":
            out.append(_image_row(m, mid=mid, ts=ts, role=role,
                                   token=token, caption_budget=caption_budget))
            continue
        if m.get("content_type") == "file":
            out.append(_file_row(m, mid=mid, ts=ts, role=role,
                                  token=token, caption_budget=caption_budget))
            continue
        if not m.get("body_ct") or m.get("K_enclave") is None:
            continue  # 无 enclave 钥的合成/本地-only 消息跳过
        plaintext = core_enclave._decrypt_envelope_via_enclave(
            m, None, purpose="v2_chat_read", runtime_token=token
        ).decode("utf-8")
        if plaintext.strip():
            out.append({"id": mid, "ts": ts, "role": role, "content": plaintext})
    if limit <= 0:
        return []
    return out[-limit:]


def _read_summary(user_id: str) -> tuple[str, float, int]:
    """读取该用户当前的会话摘要（Task 2 storage：v2_conversation_summary），逐条走 enclave
    解密取明文（服务器永不本地解密）。从未压缩过（无行）时返回 ("", 0.0, 0)；行存在但
    summary_envelope 为空（Task 2 首建行、尚未真正压缩过一次）时返回 ("", watermark_ts,
    version)，不触发 enclave 往返——没有密文可解。"""
    row = jobs_store.get_summary_row(user_id)
    if row is None:
        return "", 0.0, 0
    env = row["summary_envelope"]
    if not env:
        return "", row["watermark_ts"], row["version"]
    token = _mint_runtime_token(user_id)
    plaintext = core_enclave._decrypt_envelope_via_enclave(
        env, None, purpose="v2_summary_read", runtime_token=token
    ).decode("utf-8")
    return plaintext, row["watermark_ts"], row["version"]


def _write_summary(user_id: str, summary: str, watermark_ts: float, expected_version: int) -> bool:
    """把新压缩出的摘要**本地**加密（core_envelope，非 enclave 往返——跟 worker._write_encrypted_reply
    同一套写法）后 CAS 写回 v2_conversation_summary。信封构建失败（用户从未 onboard 过加密
    身份）时直接返回 False、不调用 CAS——调用方应当把本次压缩当作丢弃处理，不重试。"""
    store = core_store.get_store(user_id)
    env, err = core_envelope._build_shared_envelope_for_store(store, summary.encode("utf-8"))
    if env is None:
        log.warning("[v2.serve_worker] _write_summary build envelope failed for %s: %s", user_id, err)
        return False
    return jobs_store.upsert_summary_row_cas(
        user_id, summary_envelope=env, watermark_ts=watermark_ts, expected_version=expected_version)


def _wake_decision_for_user(user_id: str) -> dict:
    """Read-only heartbeat wake decision via the real proactive gate (assembly
    layer — reuses gate._build_proactive_v2_wake_decision so activation gate /
    broadcast suppression / all landmines hold with zero drift). No enqueue here;
    the scheduler decides what to do with should_wake."""
    store = core_store.get_store(user_id)
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
    from proactive.controls_v2 import WakeControlDecisionV2
    from proactive.scheduled_wake_v2 import DBScheduledWakeStoreV2, ScheduledWakeServiceV2
    from proactive.store_v2 import DBProactiveSettingsStoreV2

    settings = DBProactiveSettingsStoreV2().load(user_id)
    service = ScheduledWakeServiceV2(DBScheduledWakeStoreV2(), owner_id=_V2_WAKE_OWNER_ID)
    fired = 0

    def _submit(event):
        nonlocal fired
        jobs_store.enqueue_job(user_id, "scheduled", reason="scheduled_wake")
        core_wake_bus.notify("v2_jobs", user_id)
        fired += 1
        return WakeControlDecisionV2(True, "queued_v2", settings)

    service.fire_due_timers(user_id, settings=settings, submit_wake=_submit,
                            owner_id=_V2_WAKE_OWNER_ID)
    return fired


def _is_official(provider_config) -> bool:
    """包一层 `agent_runtime.spawners._is_official_identity`——worker.py 不能自己 import
    agent_runtime（hosted-adjacent），所以这个判定只能在装配层做好、经 TurnDeps 注入。
    provider_config 为 None 时按官方处理（跟 `_is_official_identity` 对空 provider 的
    缺省一致；实际上 resolve_provider 失败时回合根本不会走到 is_official 这一步）。"""
    if provider_config is None:
        return True
    return agent_spawners._is_official_identity(
        str(getattr(provider_config, "provider", "") or ""),
        str(getattr(provider_config, "base_url", "") or ""))


def _record_terminal_error(user_id: str, message: str) -> None:
    """Task 3: patch hosted.config_store's `last_runtime_error` (the field
    `hosted/setup_core.py:265` reads for iOS's error chip) when a v2 turn fails
    terminally. worker.py only has `user_id` at its early-failure call site (no
    `store` binding there), so this re-fetches the store itself."""
    hosted_config_store.set_last_runtime_error(core_store.get_store(user_id), message)


def _read_images(user_id: str, message_ids: list[str]) -> dict[str, dict]:
    """按 id 取图片字节。复用**仍然注册、但 planner 词表够不到**的 chat_image_read
    capability（见 multimodal spec §4.0）—— 能力还在，只是不再由模型选择、不再流经
    responder 的文本 grounding context（那是 BUG-1 的成因）。

    单条失败只跳过那一条，绝不抛——调用方 `worker._inject_tail_images` 会把缺失的图片
    行原样留成文本。
    """
    store = core_store.get_store(user_id)
    token = _mint_runtime_token(user_id)
    out: dict[str, dict] = {}
    for mid in message_ids:
        try:
            res = cap_registry.run_capability(
                "chat_image_read", store, api_key=None, runtime_token=token,
                params={"message_id": mid})
            data = (res.to_dict() or {}).get("data") or {}
        except Exception as e:  # noqa: BLE001
            log.warning("[v2.serve_worker] image read failed msg=%s: %s", mid, e)
            continue
        if data.get("image_b64"):
            out[str(mid)] = {"image_mime": data.get("image_mime") or "image/jpeg",
                             "image_b64": data["image_b64"]}
    return out


# 记忆抽取上下文一次拉多少张卡喂 dream prompt。dream 只需要一份粗粒度的现有卡片清单
# 供模型判断哪些能合并；不是全量导出，控制 prompt 体积 + 一次 enclave 往返的候选数。
_MEMORY_CARDS_LIMIT = int(os.environ.get("FEEDLING_V2_MEMORY_CARDS_LIMIT", "60"))


def _render_card_line(item: dict) -> str:
    """一张记忆卡渲染成一行（title/summary/content 里第一个非空的），仅供 dream prompt 参考。
    卡内容经 enclave readside 解出明文（见 memory_index_core），服务器本地不再解密。"""
    if not isinstance(item, dict):
        return ""
    text = str(item.get("title") or item.get("summary") or item.get("content") or "").strip()
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
        log.warning("[v2.serve_worker] memory context token mint failed for %s: %s", user_id, e)
        token = ""

    def _post(api_key, candidates, *, operation, payload=None):
        return memory_readside_core.post_enclave_readside(
            api_key, candidates, operation=operation, payload=payload, runtime_token=token)

    ctx = {"ai_name": "", "user_name": "", "buckets": "", "threads": "",
           "identity": "", "cards": ""}
    try:
        body, status = memory_core.buckets(store, None, post_enclave=_post)
        if status == 200:
            ctx["buckets"] = ", ".join(str(b) for b in (body.get("buckets") or []))
    except Exception as e:  # noqa: BLE001 — 单项降级
        log.warning("[v2.serve_worker] memory buckets unavailable for %s: %s", user_id, e)
    try:
        body, status = memory_core.threads(store, None, post_enclave=_post)
        if status == 200:
            ctx["threads"] = ", ".join(str(t) for t in (body.get("threads") or []))
    except Exception as e:  # noqa: BLE001 — 单项降级
        log.warning("[v2.serve_worker] memory threads unavailable for %s: %s", user_id, e)
    try:
        body, status = memory_core.index(store, None, {"limit": _MEMORY_CARDS_LIMIT}, post_enclave=_post)
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
            ctx["identity"] = str(ident.get("summary") or ident.get("title") or "").strip()
    except Exception as e:  # noqa: BLE001 — 单项降级
        log.warning("[v2.serve_worker] identity unavailable for %s: %s", user_id, e)
    return ctx


def _apply_memory_actions(user_id: str, actions: list[dict]) -> dict:
    """把抽取产出的 memory.add / memory.supersede action 落库（复用 /v1/memory/actions 的
    服务端实现 memory_core.actions）。api_key=None：capture/dream 产出的 action 都自带完整
    E2E 信封（extraction._to_actions 的 build_envelope），memory.add 走 _memory_add_action、
    memory.supersede 走 envelope 分支（memory/actions.py:698），两条都不需要服务器解密旧卡，
    因此 memory_core.actions 也无 runtime_token 形参（其签名只有 store/api_key/payload）。

    顶层 status>=400（整批被拒）时抛 —— 让 worker._run_extraction 的自有 try/except 把它
    转成静默 mark_failed（背景 job：不写气泡、不弹 error chip），而不是把一次失败的持久化
    伪装成成功。"""
    store = core_store.get_store(user_id)
    body, status = memory_core.actions(store, None, {"actions": actions})
    if status >= 400:
        raise RuntimeError(f"memory_actions_failed: status={status} body={str(body)[:160]}")
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
            user_id, next_screen_watch_at=next_at,
            last_screen_watch_frame_id=latest_frame_id)
        return 1

    # 被 gate（未 fresh/未变/chatting）或 oracle（未激活/免打扰）挡下：只推进到期时间，
    # **不**动 last_screen_watch_frame_id——那一帧仍是「新」的，用户停手后仍能被看到。
    jobs_store.upsert_wake_schedule(user_id, next_screen_watch_at=next_at)
    return 0


def build_production_deps() -> v2_worker.TurnDeps:
    return v2_worker.TurnDeps(
        read_messages=_read_messages,
        resolve_provider=_resolve_provider,
        is_official=_is_official,
        mint_enclave_token=_mint_runtime_token,
        record_terminal_error=_record_terminal_error,
        record_turn_metric=jobs_store.record_turn_metric,
        read_tail=_read_tail,
        read_summary=_read_summary,
        write_summary=_write_summary,
        read_images=_read_images,
        read_memory_context=_read_memory_context,
        apply_memory_actions=_apply_memory_actions,
        build_memory_envelope=_build_memory_envelope,
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
        due_users=lambda: jobs_store.due_heartbeat_users(),
        wake_decision=_wake_decision_for_user,
        enqueue_heartbeat=lambda uid: jobs_store.enqueue_job(uid, "heartbeat"),
        advance_heartbeat=lambda uid, next_at: jobs_store.upsert_wake_schedule(
            uid, next_heartbeat_at=next_at),
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
        extraction_users=lambda: admin_core.list_runtime_modes().get(
            hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2, []),
        tick_extraction=_tick_extraction_for_user,
        # screen_watch lane 生产者（D-screen_watch Task 4）：screen_watch_users 列出
        # next_screen_watch_at 已到期的用户（Task 2 的 due_screen_watch_users，与
        # heartbeat/scheduled 同套 payment-cooldown 排除）；_tick_screen_watch_for_user
        # 逐用户跑纯 gate + 只读 oracle，命中才 enqueue 一个 screen_watch job。
        # scheduler.py 同样用 getattr 探测这两个属性（缺一即整段跳过，既有 FakeDeps 零改动）。
        screen_watch_users=lambda: jobs_store.due_screen_watch_users(),
        tick_screen_watch=_tick_screen_watch_for_user,
    )


def wire_assembly() -> None:
    """复刻 asgi/lifespan.py 的关键接线（本进程无 lifespan）：注入 envelope pubkey getter、
    载入内存 registry、起 wake-bus listener、接上 "v2_jobs" 即时唤醒（FIX 3）。幂等
    （load_users/register_handler/start_listener 均自身幂等；重复调用安全）。

    "v2_jobs"：chat_send_core 入队新 job 后 NOTIFY 这个 channel；`v2_worker.on_v2_job_notify`
    只是把它桥到本进程 event loop 的一个 asyncio.Event（context 由 `_serve` 在 loop 起来后
    经 `v2_worker.set_job_wake_context` 设置——这里只负责注册 handler，不依赖 running loop）。"""
    core_envelope.get_user_public_key = accounts_registry._get_user_public_key
    accounts_registry.load_users()
    core_wake_bus.register_handler("users", lambda _uid: accounts_registry.load_users())
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


_REAP_INTERVAL_SEC = float(os.environ.get("FEEDLING_V2_REAP_INTERVAL_SEC", "30"))


async def _reaper_loop(stop_event: asyncio.Event, *, interval: float = _REAP_INTERVAL_SEC) -> None:
    """周期性回收卡死的 claimed/running job（FIX 1）：若某个 worker 在 claim_next_job 和
    终态 mark_* 之间死掉（进程崩溃/被杀），该 job 会永远卡在 claimed/running——single-flight
    的 partial unique index 会让这个用户之后所有 chat/send 都合并进这个死 job，用户从此
    再也收不到回复、也没有自愈路径。`jobs_store.reap_stuck_jobs()` 早就实现好且有测试
    覆盖，只是没人调用它——这里把它接成一个跟 run_worker_loop 并发跑的周期任务。

    每 ~interval 秒跑一次，interruptible（stop_event 置位时不必等满这个周期就退出，
    drain 更快）；单次 DB 错误只记日志、不杀进程——reaper 本身故障绝不能拖垮整个 worker
    进程（那样反而制造更多卡死 job）。"""
    while not stop_event.is_set():
        try:
            reaped = await asyncio.to_thread(jobs_store.reap_stuck_jobs)
            if reaped:
                log.info("[v2.serve_worker] reaper expired %d stuck job(s)", reaped)
        except Exception as e:  # noqa: BLE001 — 瞬时 DB 错误绝不能杀掉 reaper/worker 进程
            log.warning("[v2.serve_worker] reap_stuck_jobs failed: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


_HEARTBEAT_INTERVAL_SEC = float(os.environ.get("FEEDLING_V2_HEARTBEAT_INTERVAL_SEC", "10"))


async def _heartbeat_loop(
    worker_id: str, stop_event: asyncio.Event, *, interval: float = _HEARTBEAT_INTERVAL_SEC
) -> None:
    """UPSERT this process's liveness row every ~interval seconds (Task 2: the
    db_action_v2 chat/send guard needs something to check — without this, a
    pool where every serve_worker process has died would queue jobs forever
    with no error). Reuses the same ``worker_id`` this process passes to
    ``claim_next_job``/``run_worker_loop`` — one row per live process.

    Emits one heartbeat immediately on startup (before the first sleep) so a
    just-started pool is visible right away rather than only after the first
    full interval. Sleeps in a single ``wait_for(stop_event.wait(), timeout=...)``
    (mirrors ``_reaper_loop``) so ``stop_event.set()`` wakes it immediately
    instead of waiting out the rest of the interval — shutdown must not be
    delayed by a stale/soon-to-expire heartbeat row.
    """
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(jobs_store.record_worker_heartbeat, worker_id)
        except Exception as e:  # noqa: BLE001 — a heartbeat write failure must not kill the worker
            log.warning("[v2.serve_worker] record_worker_heartbeat failed: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


_SCHEDULER_INTERVAL_SEC = float(os.environ.get("FEEDLING_V2_SCHEDULER_INTERVAL_SEC", "30"))


async def _scheduler_loop(stop_event: asyncio.Event, *, interval: float = _SCHEDULER_INTERVAL_SEC) -> None:
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
            result = await asyncio.to_thread(scheduler.run_scheduler_tick, deps, now=time.time())
            if result.get("considered"):
                log.info(
                    "[v2.serve_worker] scheduler tick considered=%s enqueued=%s skipped=%s",
                    result.get("considered"), result.get("enqueued"), result.get("skipped"))
        except Exception as e:  # noqa: BLE001 — 瞬时故障绝不能杀掉 scheduler/worker 进程
            log.warning("[v2.serve_worker] scheduler tick failed: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def _start_genesis_thread(worker_id: str):
    """Start the genesis import worker on a DEDICATED thread, or return None if dormant.

    Rehomed here from `agent_runtime.supervisor` (2026-07-10): genesis never depended
    on the resident CLI runtime — it needed an enclave URL, a token secret, and a
    long-running loop, and supervisor merely happened to be the only process in the
    CVM that had one. Deleting agent-runner would have silently stopped draining
    `genesis_import_jobs`, stalling every new user's onboarding distillation with no
    error surfaced anywhere.

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

    if not genesis_daemon.should_start(enabled=enabled, secret=secret, enclave_url=enclave_url):
        if enabled:
            log.warning("[v2.serve_worker] FEEDLING_GENESIS_WORKER_ENABLED set but "
                        "prerequisites missing (need FEEDLING_RUNTIME_TOKEN_SECRET + "
                        "FEEDLING_ENCLAVE_URL) — genesis worker dormant")
        return None

    genesis_worker_id = f"{worker_id}:genesis"
    stop_event = threading.Event()
    # Read at CALL time (not a module-level constant) — see the comment beside
    # _GENESIS_TOKEN_SCOPE above for why.
    interval = float(os.environ.get("FEEDLING_GENESIS_WORKER_INTERVAL_SEC", "10"))

    def _beat() -> None:
        jobs_store.record_worker_heartbeat(genesis_worker_id, kind="genesis")

    thread = threading.Thread(
        target=genesis_daemon.run_loop, daemon=True, name="v2-genesis",
        kwargs={"api_url": api_url, "enclave_url": enclave_url,
                "mint_genesis": _mint_genesis_token, "interval": interval,
                "stop_event": stop_event, "on_beat": _beat},
    )
    thread.start()
    log.info("[v2.serve_worker] genesis worker enabled — interval=%.0fs worker_id=%s",
             interval, genesis_worker_id)
    return thread, stop_event


async def _serve(worker_id: str, *, poll_interval: float) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    deps = build_production_deps()
    # "v2_jobs" 即时唤醒（FIX 3）：event 必须在 running loop 里创建/绑定——wire_assembly
    # 本身在 asyncio.run 之前跑，那时还没有 loop 可绑。
    wake_event = asyncio.Event()
    v2_worker.set_job_wake_context(loop, wake_event)
    log.info("[v2.serve_worker] starting worker=%s max_workers=%s", worker_id, v2_worker.MAX_WORKERS)
    # Genesis runs on its own dedicated thread (NOT part of this asyncio.gather —
    # see _start_genesis_thread docstring for why it can't share the to_thread
    # executor with the turn/reaper/heartbeat/scheduler coroutines below).
    genesis = _start_genesis_thread(worker_id)
    await asyncio.gather(
        v2_worker.run_worker_loop(
            worker_id,
            max_workers=v2_worker.MAX_WORKERS,
            poll_interval=poll_interval,
            stop_event=stop_event,
            deps=deps,
            wake_event=wake_event,
        ),
        _reaper_loop(stop_event),
        _heartbeat_loop(worker_id, stop_event),
        _scheduler_loop(stop_event),
        # 防御性纵深（对齐 run_worker_loop 内层 gather）：四个协程内部都已吞尽异常，
        # 但万一某个漏网抛出，return_exceptions 保证不牵连拖垮另一个 loop。
        return_exceptions=True,
    )
    if genesis is not None:
        genesis_thread, genesis_stop = genesis
        genesis_stop.set()
        # Bounded: a tick in flight finishes its current LLM call. daemon=True means
        # a wedged tick can never block process exit.
        await asyncio.to_thread(genesis_thread.join, 10.0)
    log.info("[v2.serve_worker] drained; exiting worker=%s", worker_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Schema single-point for this standalone process (idempotent — see
    # db.init_schema docstring). The ASGI backend's gunicorn on_starting also
    # runs this once per deploy; this worker is its own entrypoint in the runner
    # CVM (see module docstring) with no shared master migration hook, so it must
    # not assume the schema is already at head.
    db.init_schema()
    wire_assembly()
    worker_id = os.environ.get("FEEDLING_V2_WORKER_ID", f"v2-worker-{os.getpid()}")
    poll_interval = float(os.environ.get("FEEDLING_V2_POLL_INTERVAL_SEC", "1.0"))
    asyncio.run(_serve(worker_id, poll_interval=poll_interval))


if __name__ == "__main__":
    main()
