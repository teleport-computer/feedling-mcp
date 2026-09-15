"""历史导入的记忆卡：io 这边只切窗、调模型、写库，**判断全部交给 memgarden**。

## 之前 vs 之后

    之前  窗口 → io 的 fact_map（抽候选）→ io 的 fact_write（写卡 + 身份卡）→ 一次性落库
          「什么值得记 / 怎么归桶 / 怎么去重」在 genesis/prompts.py 里有一份，
          和日常落卡（memgarden 的 capture 提示词）各写各的，判断标准会漂。
    之后  窗口 → ``GardenComponent.import_session``（切批次、提示词、解析、重问、
          跨批去重、张数上限、进度推进都在包里）→ io 每批写库 → ``commit(record_ids)``
          身份卡另走一次 identity 推导（``foreground_identity``），不再从写卡那一步顺带产出。

## 边界

    包负责   每批问什么 · 回复怎么解析、要不要重问 · 跨批去重（靠已有记忆索引）· 进度
    io 负责  解析上传文件、按来源分组、切窗 · 调模型（key、并发槽、心跳）· 写库 · 加密存进度

## 进度为什么要带「写到一半」的记录

``memory.add`` 没有幂等键。一批写库写到一半进程死了，续跑时再问一次模型、再写一次，
就是两份卡。所以写库**之前**先把这批的指令（``pending``）存进进度，写完一小段就把拿到的
id 记进去；续跑时发现 ``pending`` 就是当前这批，直接接着写剩下的，不再问模型。
崩溃窗口缩小到「一小段（``WRITE_CHUNK`` 张）写完、id 还没存下」那一瞬。

⚠️ 进度里有用户内容（两段式的候选、``pending`` 里的卡、``written`` 摘要）。宿主必须按
记忆正文的等级保存 —— 托管侧存进已加密的 genesis checkpoint，VPS 侧只放内存。

## 老 checkpoint

``ENGINE`` 写进 checkpoint。没有这个标记、但已经有旧流水线进度（``map_outputs`` /
``tasks``）的 job 是升级前开始的，**在旧流水线上跑完**，不在半路换引擎。
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from memory import garden_component

# ``ImportProgress`` / ``ImportRequest`` / ``ImportBatchResult`` 的批次字段来自 memgarden 的
# 宿主驱动导入（0.20.1 之后的版本）。在函数里取：pin 升级之前，只 import 本模块的地方
# （plaintext 调度、consumer）不会因为包版本旧而整体加载失败。

#: 写进 checkpoint 的引擎标记。改导入语义（换提示词形状之外的东西）时换一个新值。
ENGINE = "memgarden_import_session_v1"
STATE_VERSION = 1

#: 默认策略。取舍依据见 docs/HISTORY_IMPORT_GARDEN_SESSION.md（同批材料、同一模型、
#: 每种 3 次的对比）。环境变量只是回滚闸，不是灰度开关。
STRATEGY_ENV = "FEEDLING_GARDEN_IMPORT_STRATEGY"
DEFAULT_STRATEGY = "two_pass"
_STRATEGIES = ("single_pass", "two_pass")

#: 一次写库最多几条 —— 也是崩溃后可能重复的上限（见模块说明）。和执行器的批大小一致。
WRITE_CHUNK = 20

#: 同一批判断失败（解析失败、重问后仍失败）时再整批重来几次，之后跳过这批继续。
#: 模型网络错误不在这里算 —— 那种直接抛给调用方，job 以可重试失败结束、下次续跑。
BATCH_ATTEMPTS = 2

#: 进度里保留多少张「这次写进去的卡」的摘要。给身份推导、问候、画像用，不是全量。
WRITTEN_KEEP = 80

#: 来源 → (判断尺子, 材料类型)。长期记忆档案是用户手工整理过的，「几乎全收」；
#: 其余都是「用户主动交出来的材料」那一档。
FAMILY_POLICY: dict[str, tuple[str, str]] = {
    "history": ("history_import", "chat_history"),
    "user_profile": ("history_import", "user_profile"),
    "ai_persona": ("history_import", "ai_persona"),
    "memory_summary": ("curated_archive", "memory_summary"),
}


def default_strategy() -> str:
    raw = str(os.environ.get(STRATEGY_ENV, "") or "").strip().lower()
    return raw if raw in _STRATEGIES else DEFAULT_STRATEGY


@dataclass
class ImportSource:
    """一组同来源的窗口 —— 对应一次 ``import_session``。"""

    key: str
    family: str
    windows: list[str]
    #: 这组用哪个日期兜底 ``occurred_at``（空 = 不兜底，内核不猜日期）。
    fallback_occurred_at: str = ""
    #: 整组最多写多少张（None = 不限）。
    max_total_cards: int | None = None

    @property
    def policy(self) -> str:
        return FAMILY_POLICY.get(self.family, FAMILY_POLICY["history"])[0]

    @property
    def material_kind(self) -> str:
        return FAMILY_POLICY.get(self.family, FAMILY_POLICY["history"])[1]


def sources_from_groups(
    source_groups: Sequence[Mapping[str, Any]],
    *,
    prefix: str = "",
    fallback_occurred_at: str = "",
    fallback_families: frozenset[str] | set[str] | None = None,
    window_indices: Mapping[int, Sequence[int]] | None = None,
) -> list[ImportSource]:
    """把 genesis 的 ``source_groups`` 变成导入来源。

    ``window_indices``：``{组序号(从 1 开始): [要跑的窗口下标]}``，用来把前台/后台切成
    两批不重叠的窗口；不给就整组。``fallback_families``：只有这些来源用兜底日期。
    """
    out: list[ImportSource] = []
    for idx, group in enumerate(source_groups, start=1):
        family = str(group.get("source_family") or "history")
        chunks = [str(t) for t in (group.get("chunk_texts") or [])]
        picked = window_indices.get(idx) if window_indices is not None else None
        if picked is not None:
            chunks = [chunks[i] for i in picked if 0 <= i < len(chunks)]
        windows = [c for c in chunks if c.strip()]
        if not windows:
            continue
        use_fallback = fallback_families is None or family in fallback_families
        out.append(ImportSource(
            key=f"{prefix}{idx}:{family}",
            family=family,
            windows=windows,
            fallback_occurred_at=fallback_occurred_at if use_fallback else "",
        ))
    return out


# --------------------------------------------------------------------------- #
# 状态（JSON 可序列化，宿主负责加密保存）
# --------------------------------------------------------------------------- #

def new_state(*, locale: str, user_name: str = "", strategy: str | None = None) -> dict:
    """一次导入的进度。**导入语义参数在第一次就定下来**，续跑沿用 ——
    否则中途档案语言或称呼变了，续传指纹对不上，整个导入只能从头来。"""
    chosen = strategy if strategy in _STRATEGIES else default_strategy()
    return {
        "engine": ENGINE,
        "v": STATE_VERSION,
        "params": {"locale": str(locale or ""), "user_name": str(user_name or ""),
                   "strategy": chosen},
        "sessions": {},
        "pending": None,
        "written": [],
        "totals": {"cards_written": 0, "dropped": 0, "batches_skipped": 0},
    }


def is_state(value: Any) -> bool:
    return isinstance(value, dict) and value.get("engine") == ENGINE


def _progress_from(doc: Mapping[str, Any] | None):
    from memgarden import ImportProgress

    if not isinstance(doc, Mapping) or not doc:
        return None
    names = {f.name for f in dataclasses.fields(ImportProgress)}
    return ImportProgress(**{k: v for k, v in doc.items() if k in names})


def _window_ends(source: ImportSource) -> list[int]:
    ends, offset = [], 0
    for text in source.windows:
        offset += len(text)
        ends.append(offset)
    return ends


def entry_windows_done(entry: Mapping[str, Any] | None) -> tuple[int, int]:
    """``(读完的窗口数, 窗口总数)`` —— 只看进度里的偏移和计数，不含内容。

    两段式读完材料之后还有写卡阶段：会话没真正结束之前最多报「差一个」，
    进度条不会在卡还没写进去时显示「完成」。"""
    entry = entry or {}
    ends = [int(x) for x in (entry.get("window_ends") or [])]
    total = len(ends)
    if entry.get("done"):
        return total, total
    cursor = int(((entry.get("progress") or {}).get("cursor")) or 0)
    done = sum(1 for end in ends if end <= cursor)
    return (min(done, total - 1) if total else 0), total


def session_windows_done(state: Mapping[str, Any], source: ImportSource) -> int:
    """这组窗口里读完了几个（进度条用，不含内容）。"""
    entry = (state.get("sessions") or {}).get(source.key)
    if entry is None:
        return 0
    return entry_windows_done({**entry, "window_ends": _window_ends(source)})[0]


def skip_sources(state: dict, sources: Sequence[ImportSource], *, reason: str) -> None:
    """把这些来源标成「不需要跑」（比如全新开始、没有真实材料）。进度条照样算完成。"""
    entries = state.setdefault("sessions", {})
    for source in sources:
        entries[source.key] = {"done": True, "skipped": str(reason), "cards_written": 0,
                               "dropped": 0, "progress": None,
                               "window_ends": _window_ends(source)}


def session_cards(state: Mapping[str, Any], key: str) -> int:
    entry = (state.get("sessions") or {}).get(key) or {}
    return int(entry.get("cards_written") or 0)


def sessions_done(state: Mapping[str, Any], sources: Sequence[ImportSource]) -> bool:
    entries = state.get("sessions") or {}
    return all((entries.get(s.key) or {}).get("done") for s in sources)


# --------------------------------------------------------------------------- #
# 卡 → io 的记忆写入形状
# --------------------------------------------------------------------------- #

def mutation_item(mutation: Mapping[str, Any]) -> dict:
    """一条写卡指令 → genesis 既有的「记忆条目」形状（``service._memory_action_from_output``
    和 VPS 的 ``_capture_build_envelope`` 都吃这个）。"""
    card = dict(mutation.get("card") or {})
    item: dict[str, Any] = {
        "type": str(card.get("type") or "fact"),
        "summary": str(card.get("summary") or ""),
        "content": str(card.get("content") or ""),
        "bucket": str(card.get("bucket") or ""),
        "threads": list(card.get("threads") or []),
        "occurred_at": str(card.get("occurred_at") or ""),
        "importance": card.get("importance", 0.5),
        "pulse": card.get("pulse", 0.3),
    }
    cues = card.get("retrieval_cues")
    if isinstance(cues, list) and cues:
        item["retrieval_cues"] = [str(c) for c in cues if str(c or "").strip()]
    return item


def supersede_target(mutation: Mapping[str, Any]) -> str:
    if str(mutation.get("op") or "") != "supersede":
        return ""
    return str(mutation.get("target_id") or "").strip()


def index_cards(items: Sequence[Mapping[str, Any]] | None) -> list[dict]:
    """读侧索引条目 → 会话要的「已有卡」。只要可见、有 id 和摘要的。"""
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("status") or "active").strip().lower() != "active":
            continue
        rid = str(item.get("id") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not rid or not summary:
            continue
        out.append({
            "id": rid, "summary": summary,
            "bucket": str(item.get("bucket") or ""),
            "threads": list(item.get("threads") or []),
            "importance": item.get("importance", 0.5),
        })
    return out


#: 这些是「这张卡本身不合格」—— 丢这一张、别的照写。其余错误（信封、存储、鉴权）
#: 说明写库这件事本身坏了：整段抛出去，进度里的 pending 还在，下次续跑补写。
CARD_LEVEL_ERRORS = frozenset({
    "title_required",
    "description_required",
    "memory_card_polluted",
    "memory_card_tombstone",
})

#: supersede 的目标已经不在了（被删、被别的写入先取代）—— 新卡内容仍然有效，改成新增，
#: 不然这张卡就平白丢了。
STALE_TARGET_ERRORS = frozenset({"not_found", "supersede_targets_unavailable"})


def row_memory_id(row: Any) -> str:
    if not isinstance(row, Mapping) or int(row.get("http_status") or 0) >= 400:
        return ""
    if str(row.get("status") or "").lower() in {"error", "failed"}:
        return ""
    memory = row.get("memory") if isinstance(row.get("memory"), Mapping) else {}
    return str(memory.get("id") or "")


def _row_error(row: Any) -> str:
    return str(row.get("error") or "") if isinstance(row, Mapping) else ""


def write_with_executor(
    mutations: Sequence[Mapping[str, Any]],
    *,
    build_action: Callable[[Mapping[str, Any]], dict | None],
    execute: Callable[[list[dict]], list[Any]],
) -> list[str]:
    """把一段写卡指令交给 io 的 memory action 执行器，拿回和指令一一对应的 id。

    托管（明文 action，服务端封信封）和 VPS（客户端封信封，HTTP 写）共用这一份：
    哪些错误算「这张卡不合格」、supersede 目标不在了怎么办、什么情况整段算失败 ——
    两边各写一份就会漂。``build_action`` 返回 None = 这条指令本身不合格。
    """
    ids = [""] * len(mutations)
    planned: list[tuple[int, dict]] = []
    for idx, mutation in enumerate(mutations):
        action = build_action(mutation)
        if action is not None:
            planned.append((idx, action))
    if not planned:
        return ids
    hard: list[str] = []
    retry: list[tuple[int, dict]] = []
    rows = list(execute([a for _i, a in planned]) or [])
    if len(rows) < len(planned):
        hard.append("memory_action_results_missing")
    for (idx, action), row in zip(planned, rows):
        rid = row_memory_id(row)
        if rid:
            ids[idx] = rid
            continue
        err = _row_error(row)
        if str(action.get("type") or "") == "memory.supersede" and err in STALE_TARGET_ERRORS:
            retry.append((idx, {**{k: v for k, v in action.items() if k != "supersedes"},
                                "type": "memory.add"}))
        elif err not in CARD_LEVEL_ERRORS:
            hard.append(err or "memory_action_failed")
    if retry:
        rows2 = list(execute([a for _i, a in retry]) or [])
        for (idx, _action), row in zip(retry, rows2):
            rid = row_memory_id(row)
            if rid:
                ids[idx] = rid
            elif _row_error(row) not in CARD_LEVEL_ERRORS:
                hard.append(_row_error(row) or "memory_action_failed")
    if hard and not any(ids):
        raise RuntimeError(f"memory_actions_failed:{hard[0]}")
    return ids


# --------------------------------------------------------------------------- #
# 跑
# --------------------------------------------------------------------------- #

#: ``complete(prompt, purpose_key) -> (reply_text, truncated)``；网络/provider 错误直接抛。
Complete = Callable[[str, str], tuple[str, bool]]
#: ``write(mutations, idempotency_key) -> ids``，与 mutations 一一对应，没写进去的给 ""。
Write = Callable[[list[dict], str], list[str]]


class GardenImportFailed(RuntimeError):
    """一组材料的每一批都没判出来（不是「没什么可记」）。调用方按可重试失败处理。"""


@dataclass
class ImportRunResult:
    done: bool
    yielded: bool = False
    cards_written: int = 0
    dropped: int = 0
    batches_skipped: int = 0
    batches_total: int = 0
    known: list[dict] = field(default_factory=list)


def _request(source: ImportSource, params: Mapping[str, Any], *, job_key: str):
    from memgarden import ImportRequest

    from identity.user_naming import _naming_rule, sanitize_user_name

    locale = str(params.get("locale") or "")
    name = sanitize_user_name(str(params.get("user_name") or ""))
    return ImportRequest(
        material="",
        batches=tuple({"text": w} for w in source.windows),
        locale=locale,
        policy=source.policy,
        material_kind=source.material_kind,
        user_name="" if name == "TA" else name,
        naming_rule=_naming_rule(name, locale=locale),
        strategy=str(params.get("strategy") or DEFAULT_STRATEGY),
        fallback_occurred_at=source.fallback_occurred_at,
        max_total_cards=source.max_total_cards,
        idempotency_key=f"{job_key}:{source.key}",
    )


def _register_known(known: list[dict], mutations: Sequence[Mapping], ids: Sequence[str]) -> None:
    for mutation, rid in zip(mutations, ids):
        target = supersede_target(mutation)
        if target:
            known[:] = [c for c in known if str(c.get("id")) != target]
        if rid:
            card = dict(mutation.get("card") or {})
            known.append({"id": rid, "summary": str(card.get("summary") or ""),
                          "bucket": str(card.get("bucket") or ""),
                          "threads": list(card.get("threads") or []),
                          "importance": card.get("importance", 0.5)})


def _remember_written(state: dict, mutations: Sequence[Mapping], ids: Sequence[str],
                      family: str) -> None:
    kept = state.setdefault("written", [])
    for mutation, rid in zip(mutations, ids):
        if not rid:
            continue
        if len(kept) >= WRITTEN_KEEP:
            break
        kept.append({**mutation_item(mutation), "id": rid, "_source_family": family})


def run_import(
    *,
    sources: Sequence[ImportSource],
    state: dict,
    job_key: str,
    owner_key: str,
    existing_cards: Sequence[Mapping[str, Any]] | None,
    complete: Complete,
    write: Write,
    save: Callable[[dict], None],
    should_yield: Callable[[], bool] | None = None,
    on_batch: Callable[[ImportSource], None] | None = None,
) -> ImportRunResult:
    """按顺序跑每个来源的导入会话，直到全部跑完（或调用方要求让路）。

    ``save(state)`` 在每次进度推进后调用 —— 宿主把它加密存下。``should_yield()``
    在每次问模型之前检查（VPS 让用户消息先走）；返回 True 时本函数带着 ``yielded`` 返回，
    ``state`` 已经存好，下次用同一个 ``state`` 再调就接着跑。
    """
    from memgarden import ImportBatchResult

    if not is_state(state):
        raise ValueError("garden_import_state_invalid")
    params = state.get("params") or {}
    if not str(params.get("locale") or "").strip():
        # 两段式没有 locale 会整批拒绝（locale_required），被下面的「跳过坏批」吞成
        # 「没什么可记」—— 用户看到导入成功、一张卡没有。宁可当场炸。
        raise ValueError("garden_import_locale_required")
    known = [dict(c) for c in (existing_cards or [])]
    garden = garden_component.build_garden(garden_component.CallableModel(lambda _p: ""))
    totals = state.setdefault("totals", {"cards_written": 0, "dropped": 0, "batches_skipped": 0})
    totals.setdefault("batches_ok", 0)
    batches_total = sum(len(s.windows) for s in sources)

    def _result(*, done: bool, yielded: bool = False) -> ImportRunResult:
        return ImportRunResult(
            done=done, yielded=yielded,
            cards_written=int(totals.get("cards_written") or 0),
            dropped=int(totals.get("dropped") or 0),
            batches_skipped=int(totals.get("batches_skipped") or 0),
            batches_total=batches_total, known=known)

    for source in sources:
        entries = state.setdefault("sessions", {})
        entry = entries.setdefault(source.key, {"done": False, "cards_written": 0,
                                                "dropped": 0, "progress": None})
        entry["window_ends"] = _window_ends(source)
        if entry.get("done"):
            continue
        session = garden.import_session(
            _request(source, params, job_key=job_key),
            progress=_progress_from(entry.get("progress")),
            existing_cards=known, owner_key=owner_key)
        attempts = 0
        while True:
            if should_yield is not None and should_yield():
                entry["progress"] = dataclasses.asdict(session.progress)
                save(state)
                return _result(done=False, yielded=True)
            batch = session.next_batch()
            if batch is None:
                break
            pending = state.get("pending")
            replay = (isinstance(pending, dict) and pending.get("session") == source.key
                      and pending.get("idempotency_key") == batch.idempotency_key
                      and pending.get("stage") == batch.stage
                      and int(pending.get("offset", -1)) == batch.offset)
            if replay:
                outcome = ImportBatchResult(
                    stage=batch.stage, offset=batch.offset, end=int(pending["end"]),
                    idempotency_key=batch.idempotency_key,
                    mutations=list(pending.get("mutations") or []),
                    cards=list(pending.get("cards") or []))
            else:
                if pending:
                    # 属于别的批次的残留（比如续传前材料被判成另一份）—— 不能拿来写。
                    state["pending"] = None
                purpose = f"{source.key}:{batch.stage}:{batch.offset}:{attempts}"
                turn = 0
                while (prompt := batch.next_prompt()) is not None:
                    reply, truncated = complete(prompt, f"{purpose}:{turn}")
                    batch.feed(reply, truncated=truncated)
                    turn += 1
                outcome = batch.result()
            if outcome.error:
                attempts += 1
                if attempts < BATCH_ATTEMPTS:
                    session.commit(outcome)  # 记下失败、游标不动，同一批再来一次
                    continue
                # 同一批反复判不出来：跳过它（记数、不写），别让一段坏材料卡死整个导入。
                empty = ImportBatchResult(stage=outcome.stage, offset=outcome.offset,
                                          end=outcome.end,
                                          idempotency_key=outcome.idempotency_key)
                session.commit(empty)
                totals["batches_skipped"] = int(totals.get("batches_skipped") or 0) + 1
                entry["batches_skipped"] = int(entry.get("batches_skipped") or 0) + 1
                entry["progress"] = dataclasses.asdict(session.progress)
                attempts = 0
                save(state)
                if on_batch is not None:
                    on_batch(source)
                continue
            attempts = 0
            totals["batches_ok"] = int(totals.get("batches_ok") or 0) + 1
            entry["batches_ok"] = int(entry.get("batches_ok") or 0) + 1
            if outcome.stage == "candidates":
                session.commit(outcome)
                entry["progress"] = dataclasses.asdict(session.progress)
                save(state)
                if on_batch is not None:
                    on_batch(source)
                continue

            mutations = list(outcome.mutations)
            ids: list[str] = list((pending or {}).get("ids") or []) if replay else []
            state["pending"] = {
                "session": source.key, "stage": outcome.stage, "offset": outcome.offset,
                "end": outcome.end, "idempotency_key": outcome.idempotency_key,
                "mutations": mutations, "cards": list(outcome.cards), "ids": ids,
            }
            save(state)
            while len(ids) < len(mutations):
                chunk = mutations[len(ids):len(ids) + WRITE_CHUNK]
                got = [str(x or "") for x in write(chunk, outcome.idempotency_key)]
                if len(got) != len(chunk):
                    raise RuntimeError("garden_import_write_ids_mismatch")
                ids.extend(got)
                state["pending"]["ids"] = ids
                save(state)
            session.commit(outcome, record_ids=ids)
            _register_known(known, mutations, ids)
            written = sum(1 for rid in ids if rid)
            dropped = len(ids) - written
            totals["cards_written"] = int(totals.get("cards_written") or 0) + written
            totals["dropped"] = int(totals.get("dropped") or 0) + dropped
            entry["cards_written"] = int(entry.get("cards_written") or 0) + written
            entry["dropped"] = int(entry.get("dropped") or 0) + dropped
            _remember_written(state, mutations, ids, source.family)
            entry["progress"] = dataclasses.asdict(session.progress)
            state["pending"] = None
            save(state)
            if on_batch is not None:
                on_batch(source)
        if int(entry.get("batches_skipped") or 0) and not int(entry.get("batches_ok") or 0):
            # 这组**每一批**都判不出来 —— 不是「材料里没东西」，是模型/回复格式出了问题。
            # 清掉这组的进度再抛：job 以可重试失败结束，重试时这组从头来，而不是
            # 因为游标已经走到头而把「全失败」当成「做完了」。
            entry.update({"progress": None, "batches_skipped": 0, "done": False})
            save(state)
            raise GardenImportFailed(f"garden_import_all_batches_failed:{source.key}")
        entry["progress"] = dataclasses.asdict(session.progress)
        entry["done"] = True
        save(state)
    return _result(done=True)


__all__ = [
    "BATCH_ATTEMPTS",
    "CARD_LEVEL_ERRORS",
    "STALE_TARGET_ERRORS",
    "DEFAULT_STRATEGY",
    "ENGINE",
    "FAMILY_POLICY",
    "GardenImportFailed",
    "ImportRunResult",
    "ImportSource",
    "STRATEGY_ENV",
    "WRITE_CHUNK",
    "default_strategy",
    "entry_windows_done",
    "index_cards",
    "is_state",
    "mutation_item",
    "new_state",
    "row_memory_id",
    "run_import",
    "session_cards",
    "session_windows_done",
    "sessions_done",
    "skip_sources",
    "sources_from_groups",
    "supersede_target",
    "write_with_executor",
]
