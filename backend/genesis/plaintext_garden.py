"""托管 plaintext genesis：记忆卡换到 memgarden 导入会话（``memory.garden_import``）。

## 之前 vs 之后（记忆卡这一段）

    之前  每个窗口 fact_map 抽候选 → 全部候选 fact_write 一次写卡（顺带吐身份卡）→ 落库
    之后  窗口按来源交给导入会话，一批一批判断、一批一批写库，进度加密存进 genesis
          checkpoint；身份卡另走 ``foreground_identity`` 那一次推导

**其余不变**：上传解析、来源分组、切窗、前台/后台分工（前台采样窗口 → 身份卡 + 问候 →
可以进聊天；后台补全剩下的窗口 + 人设/语气 + 画像）、失败码、通知、完成条件。

## 老 job

升级前就开始、checkpoint 里已经有 fact_map 进度的 job 不走这里 —— 调度处
（``plaintext._run_plaintext_genesis_job``）按 ``progress.legacy`` 分流，让它在旧流水线
上跑完。新 job 的 checkpoint 带引擎标记，重试/重启时一直走这里。
"""
from __future__ import annotations

import distillation_ledger
from genesis import foreground_identity, import_engine, lightweight_identity, service, worker
from genesis import plaintext as pt
from hosted import history_import
from identity.user_naming import sanitize_user_name
from memory import garden_import
from notices import catalog
from notices import core as notices_core

import db

#: 只有长期记忆档案用关系开始日兜底卡片日期 —— 与切换前 ``apply_memory_outputs`` 的
#: ``preserve_dates`` 口径一致（聊天记录的卡没有日期就按写入时间，不拿关系开始日硬填）。
_FALLBACK_DATE_FAMILIES = frozenset({"memory_summary"})


class HostedImport:
    """一个 job 内所有导入会话共用：同一份进度、同一个已有记忆索引。"""

    def __init__(self, store, api_key: str | None, job_id: str, *, runtime, llm,
                 progress, language: str, user_name: str) -> None:
        self.store = store
        self.api_key = api_key
        self.job_id = job_id
        self.progress = progress
        self.state = progress.garden_state(locale=language, user_name=user_name)
        self._complete = import_engine.llm_complete(
            llm, user_id=store.user_id, job_id=job_id, runtime=runtime)
        self._write = import_engine.store_writer(store, api_key)
        self._known: list[dict] | None = None

    def run(self, sources: list[garden_import.ImportSource], *, stage: str) -> garden_import.ImportRunResult:
        if self._known is None:
            self._known = import_engine.existing_cards(self.store, self.api_key, job_id=self.job_id)

        def on_batch(source: garden_import.ImportSource) -> None:
            self.progress.publish(stage=stage, source_family=source.family,
                                  source_pass=_source_pass(source), status="processing")

        result = garden_import.run_import(
            sources=sources, state=self.state, job_key=self.job_id,
            owner_key=str(self.store.user_id), existing_cards=self._known,
            complete=self._complete, write=self._write, save=self.progress.save_garden,
            on_batch=on_batch)
        self._known = result.known
        return result

    @property
    def params(self) -> dict:
        return dict(self.state.get("params") or {})

    def written_cards(self) -> list[dict]:
        """这次导入写进去的卡（最多 ``WRITTEN_KEEP`` 张），给身份推导/问候/画像用。"""
        return [_prompt_card(c) for c in (self.state.get("written") or [])]

    def totals(self) -> dict:
        return dict(self.state.get("totals") or {})


def _source_pass(source: garden_import.ImportSource) -> int:
    try:
        return int(source.key.split(":")[-2])
    except (IndexError, ValueError):
        return 0


def _prompt_card(card: dict) -> dict:
    """进提示词/画像的卡：不带宿主内部键（id、来源标记）。"""
    return {k: v for k, v in dict(card).items() if k not in {"id", "_source_family"}}


def _memory_outcome(result: garden_import.ImportRunResult) -> str:
    raw = result.cards_written + result.dropped
    if raw == 0:
        return "not_provided"
    return "partial" if result.dropped else "written"


def _emit_partial(store, job_id: str, dropped: int) -> None:
    if dropped > 0:
        notices_core.emit(store, source="genesis", error_class="genesis_partial",
                          blame="system", severity="warning",
                          user_text=catalog.user_text_for("genesis_partial"),
                          detail=f"dropped {dropped} card(s)",
                          dedupe_key=f"genesis:{job_id}:partial")


def _fresh_start_only(msgs: list) -> bool:
    return bool(msgs) and all(
        isinstance(m, dict) and str(m.get("source") or "") == history_import._FRESH_START_SOURCE
        for m in msgs
    )


def _sources(source_groups: list[dict], *, prefix: str, relationship_anchor: dict | None,
             window_indices: dict[int, list[int]] | None = None) -> list[garden_import.ImportSource]:
    fallback = str((relationship_anchor or {}).get("relationship_started_at") or "").strip()
    return garden_import.sources_from_groups(
        source_groups, prefix=prefix, fallback_occurred_at=fallback,
        fallback_families=_FALLBACK_DATE_FAMILIES, window_indices=window_indices)


# --------------------------------------------------------------------------- #
# add_memory（花园页「补充材料」）
# --------------------------------------------------------------------------- #

def run_add_memory(store, api_key: str | None, job_id: str, *, runtime, source_groups: list[dict],
                   relationship_anchor: dict | None, analysis_messages: list[dict] | None,
                   user_name: str, llm, progress) -> None:
    notices_core.resolve(store, "genesis:")
    msgs = analysis_messages if isinstance(analysis_messages, list) else []
    language = history_import._import_language_for_store(store, msgs)
    runner = HostedImport(store, api_key, job_id, runtime=runtime, llm=llm, progress=progress,
                          language=language, user_name=user_name)
    sources = _sources(source_groups, prefix="am:", relationship_anchor=relationship_anchor)
    progress.publish(stage="plaintext_add_memory", status="processing")
    with distillation_ledger.ArtifactAttempt(store, job_id, "memory") as attempt:
        result = runner.run(sources, stage="plaintext_add_memory")
        attempt.finish(_memory_outcome(result))
    keep_all_job = any(s.family == "memory_summary" for s in sources)
    if keep_all_job and result.cards_written == 0:
        # 与切换前同一个失败码：长期记忆档案一张卡都没落，不能以「完成」收尾。
        progress.publish(stage="plaintext_add_memory_failed", status="processing", extra={
            "distill_diagnostics": {
                "reason": "keep_all_zero_cards",
                "raw_memory_count": result.cards_written + result.dropped,
                "batches_skipped": result.batches_skipped,
            }})
        raise worker.GenesisWorkerError("distill_empty_output:keep_all_nonempty:zero_memory_cards")
    _emit_partial(store, job_id, result.dropped)
    completed = db.genesis_complete_job(
        store.user_id, job_id, output={"stage": "plaintext_add_memory_done"},
        memory_action_count=result.cards_written, identity_status="skipped",
        persona_ref="", persona_sha256="")
    if completed:
        service.write_genesis_state(store, completed, status=service.DONE_JOB_STATUS)
        progress.mark_identity_ready()
        progress.publish(stage="plaintext_add_memory_done", status=service.DONE_JOB_STATUS)
    pt._write_back_plaintext_user_name(store, api_key, user_name, job_id=job_id)


# --------------------------------------------------------------------------- #
# onboarding
# --------------------------------------------------------------------------- #

def _persona_voice_outputs(store, job_id: str, *, runtime, groups: list[dict], llm, progress,
                           user_name: str) -> list[dict]:
    """人设 + 语气，和切换前后台那一段同一套调用（只是不再顺带写记忆卡）。"""
    outputs: list[dict] = []
    existing_persona: dict = {}
    existing_voice: dict = {}
    for idx, group in enumerate(groups, start=1):
        kind = str(group.get("source_kind") or history_import._HISTORY_SOURCE)
        family = str(group.get("source_family") or worker._source_family(kind))
        chunks = [str(t) for t in (group.get("chunk_texts") or []) if str(t or "").strip()]
        if not chunks or family in {"user_profile", "memory_summary"}:
            continue
        full_idx = group.get("_checkpoint_chunk_indices")
        if not isinstance(full_idx, list) or len(full_idx) != len(chunks):
            full_idx = list(range(len(chunks)))
        resumed = progress.resume_voice_outputs(idx, family)
        output = worker.build_reducer_output_from_texts(
            user_id=store.user_id, job_id=job_id,
            key_prefix=f"{job_id}:source_pass:{idx}:{family}",
            runtime=runtime, chunk_texts=chunks, source_kind=kind,
            existing_persona=existing_persona, existing_voice=existing_voice,
            include_memory=False, include_persona_voice=True, user_name=user_name, llm=llm,
            resume_voice_outputs={i: resumed[f] for i, f in enumerate(full_idx) if f in resumed},
            on_voice_completed=(
                lambda local, mapped, source_pass=idx, fam=family, fi=tuple(full_idx):
                progress.record_voice(source_pass, fam, fi[local], mapped)),
        )
        outputs.append(output)
        next_persona = pt._plaintext_existing_persona_from_output(output)
        if next_persona:
            existing_persona = next_persona
        next_voice = pt._plaintext_existing_voice_from_output(output)
        if next_voice:
            existing_voice = next_voice
    return outputs


def _apply_non_memory(store, api_key: str | None, job_id: str, output: dict, *,
                      memory_action_count: int) -> dict:
    """``service.apply_reducer_output`` 去掉记忆那一段（卡已经由导入会话写过了）。"""
    output = {**output, "job_id": job_id}
    with distillation_ledger.ArtifactAttempt(store, job_id, "identity") as attempt:
        identity_status = service.init_identity_if_absent(store, output, api_key)
        attempt.finish(identity_status)
    persona_ref, persona_sha = service.write_persona_artifact(store, job_id, output)
    voice_ref, voice_sha = service.write_voice_artifact(store, job_id, output)
    profile_ref, profile_sha, profile_status = service.write_profile_artifact(
        store, job_id, output, api_key)
    result_doc = {
        "memory_action_count": memory_action_count, "identity_status": identity_status,
        "persona_ref": persona_ref, "persona_sha256": persona_sha,
        "voice_ref": voice_ref, "voice_sha256": voice_sha,
        "profile_ref": profile_ref, "profile_sha256": profile_sha, "profile_status": profile_status,
    }
    # 与 apply_reducer_output 同样留两份内容无关的产出摘要（后台排查读它们）。
    db.genesis_upsert_output(store.user_id, job_id, "reducer", doc=service._safe_reducer_doc(job_id, output),
                             status="applied", ref="sanitized")
    db.genesis_upsert_output(store.user_id, job_id, "apply", doc=result_doc, status="done", ref="inline")
    return result_doc


def _derive_identity(runtime, msgs: list[dict], cards: list[dict], *, days: int, language: str,
                     max_attempts: int = 3) -> tuple[dict, list[str]]:
    return foreground_identity.derive_foreground_identity(
        runtime=runtime, analysis_messages=msgs, core_memories=cards,
        days_with_user=days, language=language, max_attempts=max_attempts)


def run_onboarding(store, api_key: str | None, job_id: str, *, runtime, source_groups: list[dict],
                   relationship_anchor: dict | None, analysis_messages: list[dict] | None,
                   user_name: str, llm, progress) -> str:
    """返回走了哪条：``genesis_v2``（前台/后台）或 ``full``（一次做完）。"""
    msgs = analysis_messages if isinstance(analysis_messages, list) else []
    language = history_import._import_language_for_store(store, msgs)
    runner = HostedImport(store, api_key, job_id, runtime=runtime, llm=llm, progress=progress,
                          language=language, user_name=user_name)
    if worker.genesis_v2_enabled() and _run_v2(
            store, api_key, job_id, runtime=runtime, source_groups=source_groups,
            relationship_anchor=relationship_anchor, msgs=msgs, user_name=user_name,
            llm=llm, progress=progress, runner=runner, language=language):
        return "genesis_v2"
    _run_full(store, api_key, job_id, runtime=runtime, source_groups=source_groups,
              relationship_anchor=relationship_anchor, msgs=msgs, user_name=user_name,
              llm=llm, progress=progress, runner=runner, language=language)
    return "full"


def _foreground_split(source_groups: list[dict]) -> tuple[list[dict], dict[int, list[int]], dict[int, list[int]]]:
    fg_groups = pt._cap_foreground_history_chunks(source_groups)
    fg_idx: dict[int, list[int]] = {}
    bg_idx: dict[int, list[int]] = {}
    for idx, (group, fg) in enumerate(zip(source_groups, fg_groups), start=1):
        total = len(group.get("chunk_texts") or [])
        picked = [int(i) for i in (fg.get("_checkpoint_chunk_indices") or range(total))]
        fg_idx[idx] = picked
        bg_idx[idx] = [i for i in range(total) if i not in set(picked)]
    return fg_groups, fg_idx, bg_idx


def _run_v2(store, api_key, job_id, *, runtime, source_groups, relationship_anchor, msgs,
            user_name, llm, progress, runner: HostedImport, language: str) -> bool:
    fg_groups, fg_idx, bg_idx = _foreground_split(source_groups)
    fg_group = next((g for g in fg_groups if str(g.get("source_family") or "") == "history"),
                    fg_groups[0])
    fg_pass = fg_groups.index(fg_group) + 1
    fg_kind = str(fg_group.get("source_kind") or history_import._HISTORY_SOURCE)
    fg_family = str(fg_group.get("source_family") or worker._source_family(fg_kind))
    progress.publish(stage="genesis_v2_foreground", source_family=fg_family,
                     source_pass=fg_pass, status="processing")
    fresh_start_only = _fresh_start_only(msgs)
    combined_map = worker.genesis_combined_map_enabled() and not fresh_start_only
    fg_sources = _sources(source_groups, prefix="fg:", relationship_anchor=relationship_anchor,
                          window_indices=fg_idx)
    bg_sources = _sources(source_groups, prefix="bg:", relationship_anchor=relationship_anchor,
                          window_indices=bg_idx)

    if fresh_start_only:
        # 没有真实材料：一张卡都不该从占位文本里蒸出来。窗口照样记成读完，完成条件不卡住。
        garden_import.skip_sources(runner.state, [*fg_sources, *bg_sources], reason="fresh_start")
        progress.save_garden(runner.state)
        fg_result = garden_import.ImportRunResult(done=True)
    else:
        with distillation_ledger.ArtifactAttempt(store, job_id, "memory") as attempt:
            fg_result = runner.run(fg_sources, stage="genesis_v2_foreground")
            attempt.finish(_memory_outcome(fg_result))
        if fg_result.cards_written == 0:
            return False  # 前台窗口里什么都没有：交给一次做完的那条路（同切换前）

    full_memories = runner.written_cards()
    fg_merged = pt._plaintext_merge_reducer_outputs(
        [{"memories": full_memories, "source_kind": fg_kind, "source_family": fg_family}],
        relationship_anchor=relationship_anchor)
    if combined_map:
        voice_persona = _persona_voice_outputs(
            store, job_id, runtime=runtime, groups=fg_groups, llm=llm, progress=progress,
            user_name=user_name)
        fg_merged = pt._plaintext_merge_reducer_outputs(
            [fg_merged, *voice_persona], relationship_anchor=relationship_anchor)
        fg_merged["memories"] = full_memories
    pt._attach_plaintext_user_name(fg_merged, user_name)
    pt._attach_plaintext_profile(store, api_key, job_id, runtime=runtime, output=fg_merged,
                                 key_prefix=f"{job_id}:foreground_profile", llm=llm)
    days = int((relationship_anchor or {}).get("days_with_user") or 0)
    explicit_started_at = str((relationship_anchor or {}).get("relationship_started_at") or "").strip()

    if fresh_start_only:
        identity_payload, id_warnings = {"agent_name": "", "dimensions": []}, []
    else:
        identity_payload, id_warnings = _derive_identity(
            runtime, msgs, full_memories, days=days, language=language)
    provider_failure = pt._provider_identity_failure(id_warnings)
    if provider_failure or not foreground_identity.has_identity_signal(identity_payload):
        support_texts = [str(m.get("content") or "") for m in msgs
                         if history_import._is_import_support_message(m)]
        lite = lightweight_identity.derive_from_support(
            support_texts, days_with_user=days, language=language)
        if lightweight_identity.has_signal(lite):
            identity_payload = lite
        elif provider_failure:
            service.mark_failed(store, job_id, "onboarding_no_identity:provider_unstable")
            return True
    if sanitize_user_name(user_name) != "TA":
        identity_payload["user_preferred_name"] = sanitize_user_name(user_name)
    identity_first = bool(msgs) and foreground_identity.has_identity_signal(identity_payload)
    persona_ref = persona_sha = ""
    mem_count = fg_result.cards_written

    if identity_first:
        if combined_map:
            persona_ref, persona_sha = service.write_persona_artifact(store, job_id, fg_merged)
            service.write_voice_artifact(store, job_id, fg_merged)
        service.write_profile_artifact(store, job_id, fg_merged, api_key)
        with distillation_ledger.ArtifactAttempt(store, job_id, "identity") as attempt:
            identity_row = history_import._store_identity_payload(
                store, identity_payload, days_with_user=days,
                evidence=f"genesis_foreground:{job_id}", language=language,
                relationship_started_at=explicit_started_at)
            attempt.finish("written" if identity_row else "not_provided")
        pt._append_plaintext_onboarding_greeting(
            store, job_id=job_id, runtime=runtime, analysis_messages=msgs,
            memories=full_memories, identity_payload=identity_payload, days=days,
            language=language, fresh_start=fresh_start_only)
        identity_status = "initialized"
    else:
        pt._append_plaintext_onboarding_greeting(
            store, job_id=job_id, runtime=runtime, analysis_messages=msgs,
            memories=full_memories, identity_payload=identity_payload, days=days,
            language=language, fresh_start=fresh_start_only)
        applied = _apply_non_memory(store, api_key, job_id, fg_merged, memory_action_count=mem_count)
        identity_status = str(applied.get("identity_status") or "")
        persona_ref = str(applied.get("persona_ref") or persona_ref)
        persona_sha = str(applied.get("persona_sha256") or persona_sha)

    notices_core.resolve(store, "genesis:")
    progress.mark_identity_ready()
    progress.publish(stage="genesis_v2_foreground_ready", status="processing")
    completion = {"memory_action_count": mem_count, "identity_status": identity_status,
                  "persona_ref": persona_ref, "persona_sha256": persona_sha}
    if fresh_start_only:
        pt._complete_plaintext_v2_job(store, job_id, progress=progress, **completion)
        return True
    try:
        _run_background(store, api_key, job_id, runtime=runtime, source_groups=source_groups,
                        bg_sources=bg_sources, relationship_anchor=relationship_anchor, msgs=msgs,
                        user_name=user_name, llm=llm, progress=progress, runner=runner,
                        language=language, write_identity=not identity_first,
                        include_persona_voice=not combined_map, completion=completion)
    except Exception as e:  # noqa: BLE001
        service.mark_failed(
            store, job_id, f"genesis_v2_background_failed:{type(e).__name__}:{str(e)[:220]}", exc=e)
    return True


def _finish_output(store, api_key, job_id, *, runtime, source_groups, relationship_anchor, msgs,
                   user_name, llm, progress, runner: HostedImport, language: str,
                   write_identity: bool, include_persona_voice: bool, profile_key: str) -> dict:
    """记忆写完之后：人设/语气（需要时）、身份卡推导（需要时）、画像。返回合并好的输出。"""
    cards = runner.written_cards()
    outputs: list[dict] = []
    if include_persona_voice:
        outputs = _persona_voice_outputs(store, job_id, runtime=runtime, groups=source_groups,
                                         llm=llm, progress=progress, user_name=user_name)
    family = str((source_groups[0] if source_groups else {}).get("source_family") or "history")
    merged = pt._plaintext_merge_reducer_outputs(
        [*outputs, {"memories": cards, "source_family": family}],
        relationship_anchor=relationship_anchor)
    merged["memories"] = cards
    if write_identity:
        days = int((relationship_anchor or {}).get("days_with_user") or 0)
        identity, warnings = _derive_identity(runtime, msgs, cards, days=days, language=language,
                                              max_attempts=1)
        if foreground_identity.has_identity_signal(identity) and not pt._provider_identity_failure(warnings):
            merged["identity"] = {**(merged.get("identity") or {}), **identity}
        elif isinstance(merged.get("persona"), dict) and str(merged["persona"].get("content") or "").strip():
            baseline = worker.derive_identity_from_persona(
                user_id=store.user_id, job_id=job_id, runtime=runtime,
                persona_content=str(merged["persona"]["content"]), user_name=user_name)
            if baseline.get("agent_name") or baseline.get("dimensions"):
                merged["identity"] = {**(merged.get("identity") or {}), **baseline}
    pt._attach_plaintext_user_name(merged, user_name)
    pt._attach_plaintext_profile(store, api_key, job_id, runtime=runtime, output=merged,
                                 key_prefix=profile_key, llm=llm)
    return merged


def _run_background(store, api_key, job_id, *, runtime, source_groups, bg_sources, relationship_anchor,
                    msgs, user_name, llm, progress, runner: HostedImport, language: str,
                    write_identity: bool, include_persona_voice: bool, completion: dict) -> None:
    progress.publish(stage="genesis_v2_background", status="processing")
    with distillation_ledger.ArtifactAttempt(store, job_id, "memory") as attempt:
        result = runner.run(bg_sources, stage="genesis_v2_background")
        attempt.finish(_memory_outcome(result))
    merged = _finish_output(
        store, api_key, job_id, runtime=runtime, source_groups=source_groups,
        relationship_anchor=relationship_anchor, msgs=msgs, user_name=user_name, llm=llm,
        progress=progress, runner=runner, language=language, write_identity=write_identity,
        include_persona_voice=include_persona_voice, profile_key=f"{job_id}:background_profile")
    service.write_profile_artifact(store, job_id, merged, api_key)
    identity_status = ""
    if write_identity:
        with distillation_ledger.ArtifactAttempt(store, job_id, "identity") as attempt:
            identity_status = service.init_identity_if_absent(store, merged, api_key) or "not_provided"
            attempt.finish(identity_status)
    persona_ref, persona_sha = service.write_persona_artifact(store, job_id, merged)
    service.write_voice_artifact(store, job_id, merged)
    _emit_partial(store, job_id, result.dropped)
    pt._complete_plaintext_v2_job(
        store, job_id, progress=progress,
        memory_action_count=result.cards_written,
        identity_status=identity_status or str(completion.get("identity_status") or ""),
        persona_ref=persona_ref or str(completion.get("persona_ref") or ""),
        persona_sha256=persona_sha or str(completion.get("persona_sha256") or ""))


def _run_full(store, api_key, job_id, *, runtime, source_groups, relationship_anchor, msgs,
              user_name, llm, progress, runner: HostedImport, language: str) -> None:
    """一次做完（genesis v2 关闭，或前台窗口里一张卡都没有）。窗口不分前后台：
    前台已经跑过的会话自动跳过，其余窗口作为 ``bg:`` 会话跑完。"""
    _fg_groups, fg_idx, bg_idx = _foreground_split(source_groups)
    fg_sources = _sources(source_groups, prefix="fg:", relationship_anchor=relationship_anchor,
                          window_indices=fg_idx)
    entries = runner.state.get("sessions") or {}
    if any(s.key in entries for s in fg_sources):
        sources = [*fg_sources, *_sources(source_groups, prefix="bg:",
                                          relationship_anchor=relationship_anchor,
                                          window_indices=bg_idx)]
    else:
        sources = _sources(source_groups, prefix="all:", relationship_anchor=relationship_anchor)
    progress.publish(stage="plaintext_reducer", status="processing")
    with distillation_ledger.ArtifactAttempt(store, job_id, "memory") as attempt:
        result = runner.run(sources, stage="plaintext_reducer")
        attempt.finish(_memory_outcome(result))
    merged = _finish_output(
        store, api_key, job_id, runtime=runtime, source_groups=source_groups,
        relationship_anchor=relationship_anchor, msgs=msgs, user_name=user_name, llm=llm,
        progress=progress, runner=runner, language=language, write_identity=True,
        include_persona_voice=True, profile_key=f"{job_id}:merged_profile")
    progress.publish(stage="plaintext_reducer_done")
    notices_core.resolve(store, "genesis:")
    _emit_partial(store, job_id, result.dropped)
    applied = _apply_non_memory(store, api_key, job_id, merged, memory_action_count=result.cards_written)
    completed = db.genesis_complete_job(
        store.user_id, job_id, output=applied, memory_action_count=result.cards_written,
        identity_status=str(applied.get("identity_status") or ""),
        persona_ref=str(applied.get("persona_ref") or ""),
        persona_sha256=str(applied.get("persona_sha256") or ""))
    if completed:
        service.write_genesis_state(store, completed, status=service.DONE_JOB_STATUS)
    progress.mark_identity_ready()
    progress.publish(stage="plaintext_reducer_done", status=service.DONE_JOB_STATUS)
    pt._write_back_plaintext_user_name(store, api_key, user_name, job_id=job_id)


__all__ = ["HostedImport", "run_add_memory", "run_onboarding"]
