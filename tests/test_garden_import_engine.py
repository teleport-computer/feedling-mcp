"""历史导入换到 memgarden ``import_session`` 之后，io 这一层的状态机。

守的是 io 自己加的那部分（包里的切批/去重/续传由 memgarden 自己的测试守）：

  · 进度可以 JSON 往返存下来，进程重启后从下一批接着跑，已提交的批次不再问模型
  · 写库写到一半崩了：续跑时不再问模型，只补写剩下的，不重复落卡
  · 同一批反复判不出来就跳过；一组全失败时抛可重试错误并清掉那组进度
  · 缺 locale 当场炸（两段式否则会整批被拒、被吞成「没什么可记」）
  · 前面来源写进去的卡（带真实 id）进入后面来源的「已有记忆索引」
  · 整组张数上限生效

全部是合成材料，不含真实用户数据。
"""
from __future__ import annotations

import json

import pytest

from memory import garden_import


def _card(summary: str, *, action: str = "add", target: str | None = None) -> dict:
    return {"action": action, "type": "fact", "target_id": target, "bucket": "饮食",
            "threads": [], "summary": summary,
            "content": f"{summary}。这是一段足够长、有实质内容的正文。",
            "importance": 0.5, "pulse": 0.2}


def _reply(*cards: dict) -> str:
    return json.dumps({"cards": list(cards)}, ensure_ascii=False)


def _candidates(*items: tuple[str, str]) -> str:
    return json.dumps({"candidates": [
        {"about": "person", "summary": s, "evidence": e, "occurred_at": None}
        for s, e in items]}, ensure_ascii=False)


class Model:
    """按窗口里的关键词回复；记录调用。"""

    def __init__(self, rules: list[tuple[str, str]], default: str = '{"cards": []}') -> None:
        self.rules = rules
        self.default = default
        self.prompts: list[str] = []

    def __call__(self, prompt: str, _purpose: str) -> tuple[str, bool]:
        self.prompts.append(prompt)
        material = _material(prompt)
        for needle, reply in self.rules:
            if needle in material:
                return reply, False
        return self.default, False


def _material(prompt: str) -> str:
    """只看提示词里「材料」那一段 —— 已有记忆索引里也会出现卡的摘要，不能拿来匹配。"""
    end = prompt.find("[Output]")
    start = max(prompt.rfind("[The material", 0, end), prompt.rfind("[The entries", 0, end))
    return prompt[start:end] if start >= 0 else ""


class Store:
    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.calls: list[list[dict]] = []
        self.fail_after: int | None = None

    def __call__(self, mutations: list[dict], _key: str) -> list[str]:
        self.calls.append(mutations)
        ids = []
        for m in mutations:
            if self.fail_after is not None and len(self.cards) >= self.fail_after:
                raise RuntimeError("store down")
            rid = f"mom_{len(self.cards) + 1}"
            self.cards[rid] = {**m["card"], "id": rid, "op": m["op"],
                               "target_id": m.get("target_id")}
            ids.append(rid)
        return ids


def _sources(*windows: str, family: str = "history") -> list[garden_import.ImportSource]:
    return [garden_import.ImportSource(key=f"1:{family}", family=family, windows=list(windows))]


def _run(state, sources, model, store, *, existing=None, saves=None, should_yield=None):
    def save(doc):
        if saves is not None:
            saves.append(json.loads(json.dumps(doc, ensure_ascii=False)))
    return garden_import.run_import(
        sources=sources, state=state, job_key="job1", owner_key="usr_x",
        existing_cards=existing or [], complete=model, write=store, save=save,
        should_yield=should_yield)


def test_resume_from_saved_state_skips_committed_batches():
    sources = _sources("窗口一：我不吃辣\n", "窗口二：我养了一只橘猫叫蛋子\n")
    state = garden_import.new_state(locale="zh-Hans", strategy="single_pass")
    saves: list[dict] = []
    calls = {"n": 0}

    def yield_after_first() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # 第一批提交后、问第二批之前让路

    model = Model([("不吃辣", _reply(_card("不吃辣"))), ("蛋子", _reply(_card("橘猫蛋子")))])
    store = Store()
    first = _run(state, sources, model, store, saves=saves, should_yield=yield_after_first)
    assert first.yielded and not first.done
    assert len(model.prompts) == 1 and len(store.cards) == 1

    # 进程重启：只剩存下来的 JSON。已有卡从库里重新读（含第一批写进去的那张）。
    restored = saves[-1]
    model2 = Model(model.rules)
    existing = [{"id": rid, "summary": c["summary"]} for rid, c in store.cards.items()]
    second = _run(restored, sources, model2, store, existing=existing)
    assert second.done
    assert len(model2.prompts) == 1, "已提交的第一批不能再问模型"
    assert "窗口二" in model2.prompts[0]
    assert sorted(c["summary"] for c in store.cards.values()) == ["不吃辣", "橘猫蛋子"]
    assert restored["totals"]["cards_written"] == 2
    assert [w["summary"] for w in restored["written"]] == ["不吃辣", "橘猫蛋子"]


def test_crash_mid_write_replays_pending_without_model_or_duplicates(monkeypatch):
    monkeypatch.setattr(garden_import, "WRITE_CHUNK", 1)
    sources = _sources("窗口：三件事\n")
    state = garden_import.new_state(locale="zh-Hans", strategy="single_pass")
    saves: list[dict] = []
    model = Model([("三件事", _reply(_card("周末常去西湖边骑车"), _card("每天早上一杯冰美式"),
                                     _card("三月十二号是妈妈的生日")))])
    store = Store()
    store.fail_after = 1  # 写进第一张后「进程死了」
    with pytest.raises(RuntimeError, match="store down"):
        _run(state, sources, model, store, saves=saves)
    assert len(store.cards) == 1
    saved = saves[-1]
    assert saved["pending"]["ids"] == ["mom_1"]

    store.fail_after = None
    model2 = Model([], default="SHOULD NOT BE CALLED")
    result = _run(saved, sources, model2, store,
                  existing=[{"id": "mom_1", "summary": "周末常去西湖边骑车"}])
    assert result.done
    assert model2.prompts == [], "pending 就是当前这批：只补写，不再问模型"
    assert [c["summary"] for c in store.cards.values()] == [
        "周末常去西湖边骑车", "每天早上一杯冰美式", "三月十二号是妈妈的生日"]
    assert saved["pending"] is None


def test_bad_batch_is_retried_then_skipped_and_others_still_land():
    sources = _sources("坏窗口\n", "好窗口：我不吃辣\n")
    state = garden_import.new_state(locale="zh-Hans", strategy="single_pass")
    model = Model([("坏窗口", "not json at all"), ("不吃辣", _reply(_card("不吃辣")))])
    store = Store()
    result = _run(state, sources, model, store)
    assert result.done
    assert result.batches_skipped == 1
    assert [c["summary"] for c in store.cards.values()] == ["不吃辣"]
    # 坏批整批重来过（包里自己的格式重问之外），然后才跳过：首问提示词出现了 BATCH_ATTEMPTS 次
    bad = [p for p in model.prompts if "坏窗口" in _material(p)]
    assert bad.count(bad[0]) == garden_import.BATCH_ATTEMPTS == 2


def test_every_batch_failing_raises_and_resets_that_source():
    sources = _sources("坏一\n", "坏二\n")
    state = garden_import.new_state(locale="zh-Hans", strategy="single_pass")
    saves: list[dict] = []
    with pytest.raises(garden_import.GardenImportFailed):
        _run(state, sources, Model([], default="garbage"), Store(), saves=saves)
    entry = saves[-1]["sessions"]["1:history"]
    assert entry["progress"] is None and not entry["done"], "重试要从头来，而不是当成做完了"


def test_locale_is_required():
    state = garden_import.new_state(locale="", strategy="two_pass")
    with pytest.raises(ValueError, match="locale_required"):
        _run(state, _sources("x\n"), Model([]), Store())


def test_cards_from_earlier_source_enter_later_index_with_real_ids():
    sources = [
        garden_import.ImportSource(key="1:memory_summary", family="memory_summary",
                                   windows=["- 不吃辣\n"]),
        garden_import.ImportSource(key="2:history", family="history",
                                   windows=["聊天里又说了一次不吃辣，而且更讨厌香菜\n"]),
    ]
    state = garden_import.new_state(locale="zh-Hans", strategy="single_pass")
    model = Model([
        ("聊天里", _reply(_card("不吃辣也不吃香菜", action="supersede", target="mom_1"))),
        ("- 不吃辣", _reply(_card("不吃辣"))),
    ])
    store = Store()
    result = _run(state, sources, model, store)
    assert result.done
    history_prompt = next(p for p in model.prompts if "聊天里" in p)
    assert "mom_1" in history_prompt, "第二组的已有记忆索引里必须有第一组刚写的卡（真实 id）"
    assert store.cards["mom_2"]["op"] == "supersede"
    assert store.cards["mom_2"]["target_id"] == "mom_1"
    assert "curated_archive" not in history_prompt  # 各组各用自己的尺子


def test_max_total_cards_caps_the_source():
    src = garden_import.ImportSource(key="1:history", family="history",
                                     windows=["一：甲乙\n", "二：丙丁\n"], max_total_cards=1)
    state = garden_import.new_state(locale="zh-Hans", strategy="single_pass")
    model = Model([("甲乙", _reply(_card("周末常去西湖边骑车"))),
                   ("丙丁", _reply(_card("每天早上一杯冰美式")))])
    store = Store()
    result = _run(state, [src], model, store)
    assert result.done
    assert len(store.cards) == 1
    assert len(model.prompts) == 1, "上限满了之后剩下的批次不再调模型"


def test_two_pass_candidates_then_single_write_batch():
    sources = _sources("窗一：我不吃辣\n", "窗二：再说一次我不吃辣，我养猫\n")
    state = garden_import.new_state(locale="zh-Hans", strategy="two_pass")
    model = Model([
        ("Candidate facts", _reply(_card("不吃辣"), _card("养猫"))),  # 写卡提示词
        ("我养猫", _candidates(("不吃辣", "再说一次"), ("养猫", "我养猫"))),
        ("窗一", _candidates(("不吃辣", "我不吃辣"))),
    ])
    store = Store()
    saves: list[dict] = []
    result = _run(state, sources, model, store, saves=saves)
    assert result.done
    assert sorted(c["summary"] for c in store.cards.values()) == ["不吃辣", "养猫"]
    # 候选阶段的进度（含用户内容）也存进了 state —— 宿主必须加密保存
    assert any(s["sessions"]["1:history"]["progress"]["candidates"] for s in saves)


def test_family_policy_and_fallback_date_only_for_listed_families():
    groups = [
        {"source_family": "memory_summary", "chunk_texts": ["a"]},
        {"source_family": "history", "chunk_texts": ["b", "  ", "c"]},
    ]
    sources = garden_import.sources_from_groups(
        groups, fallback_occurred_at="2021-06-18", fallback_families={"memory_summary"},
        window_indices={2: [2]})
    assert [(s.key, s.policy, s.windows, s.fallback_occurred_at) for s in sources] == [
        ("1:memory_summary", "curated_archive", ["a"], "2021-06-18"),
        ("2:history", "history_import", ["c"], ""),
    ]


def test_mutation_item_keeps_retrieval_cues_and_dates():
    item = garden_import.mutation_item({"op": "add", "card": {
        "summary": "s", "content": "c", "type": "event", "occurred_at": "2024-05-20",
        "retrieval_cues": ["半马", " "], "importance": 0.8}})
    assert item["occurred_at"] == "2024-05-20"
    assert item["retrieval_cues"] == ["半马"]
    assert item["type"] == "event" and item["importance"] == 0.8


# --------------------------------------------------------------------------- #
# 切换前的失败口径 / 称呼兜底，在引擎上恢复
# --------------------------------------------------------------------------- #

def _rejecting_writer(written: list[dict], *, reject: set[str]):
    """真 ``write_with_executor``；执行器按摘要拒卡（``memory_card_polluted`` = 卡本身不合格）。"""
    def execute(actions: list[dict]) -> list[dict]:
        rows = []
        for action in actions:
            summary = action["card"]["summary"]
            if summary in reject:
                rows.append({"status": "error", "error": "memory_card_polluted", "http_status": 422})
            else:
                written.append(action["card"])
                rows.append({"status": "ok", "http_status": 201,
                             "memory": {"id": f"mom_{len(written)}"}})
        return rows

    def write(mutations: list[dict], _key: str) -> list[str]:
        return garden_import.write_with_executor(
            mutations, build_action=lambda m: {"type": "memory.add", "card": m["card"]},
            execute=execute)
    return write


def test_batch_with_every_card_rejected_fails_and_retry_asks_the_model_again():
    """之前（6972427d）：一段写卡指令全被判不合格 → 任务失败（可重试），不是「完成、0 张卡」。
    重试时这批一张没写，所以重新问模型，而不是把被拒的卡原样再写。"""
    sources = _sources("窗口：两件事\n")
    state = garden_import.new_state(locale="zh-Hans", strategy="single_pass")
    saves: list[dict] = []
    written: list[dict] = []
    model = Model([("两件事", _reply(_card("坏卡一"), _card("坏卡二")))])
    with pytest.raises(garden_import.GardenImportCardsRejected, match="memory_card_polluted"):
        _run(state, sources, model, _rejecting_writer(written, reject={"坏卡一", "坏卡二"}),
             saves=saves)
    assert written == []
    assert saves[-1]["pending"] is None
    assert not saves[-1]["sessions"]["1:history"].get("done")

    model2 = Model([("两件事", _reply(_card("周末常去西湖边骑车")))])
    result = _run(saves[-1], sources, model2, _rejecting_writer(written, reject=set()))
    assert result.done and model2.prompts, "重试重新问了模型"
    assert [c["summary"] for c in written] == ["周末常去西湖边骑车"]


def test_partial_rejection_logs_counts_only_and_keeps_going(caplog):
    sources = _sources("窗口：两件事\n")
    state = garden_import.new_state(locale="zh-Hans", strategy="single_pass")
    written: list[dict] = []
    model = Model([("两件事", _reply(_card("周末常去西湖边骑车"), _card("被拒的那张秘密内容")))])
    with caplog.at_level("WARNING", logger="memory.garden_import"):
        result = _run(state, sources, model,
                      _rejecting_writer(written, reject={"被拒的那张秘密内容"}))
    assert result.done and (result.cards_written, result.dropped) == (1, 1)
    partial = [r.getMessage() for r in caplog.records if "partial" in r.getMessage()]
    assert partial == ["garden import batch partial job=job1 source=history written=1 dropped=1"]
    assert "秘密" not in caplog.text and "西湖" not in caplog.text, "告警里不许有卡的内容"


def test_later_chunk_rejected_after_earlier_chunk_landed_counts_as_partial(monkeypatch):
    """同一批前面几段已经写进去：整批不是「一张没写」，被拒的那段按丢弃记，不抛、不重放。"""
    monkeypatch.setattr(garden_import, "WRITE_CHUNK", 1)
    sources = _sources("窗口：两件事\n")
    state = garden_import.new_state(locale="zh-Hans", strategy="single_pass")
    written: list[dict] = []
    model = Model([("两件事", _reply(_card("周末常去西湖边骑车"), _card("坏卡")))])
    result = _run(state, sources, model, _rejecting_writer(written, reject={"坏卡"}))
    assert result.done and (result.cards_written, result.dropped) == (1, 1)


def test_user_placeholder_is_rewritten_before_writing_and_in_the_index():
    """之前（d72e74c4 / 67bf4b96）：导入卡写库前把「用户」这类系统占位确定性换成称呼。"""
    sources = _sources("窗口：骑车\n")
    state = garden_import.new_state(locale="zh-Hans", user_name="小雨", strategy="single_pass")
    card = _card("用户喜欢周末去西湖边骑车")
    card.update(bucket="关于用户", threads=["用户的周末"],
                content="用户在周末骑车。用户增长和用户画像是她的工作，不是在说她。")
    store = Store()
    result = _run(state, sources, Model([("骑车", _reply(card))]), store)
    written = list(store.cards.values())[0]
    assert written["summary"] == "小雨喜欢周末去西湖边骑车"
    assert written["bucket"] == "关于小雨" and written["threads"] == ["小雨的周末"]
    assert written["content"] == "小雨在周末骑车。用户增长和用户画像是她的工作，不是在说她。"
    assert result.known[0]["summary"] == "小雨喜欢周末去西湖边骑车"
    assert state["written"][0]["summary"] == "小雨喜欢周末去西湖边骑车"


def test_unknown_name_rewrites_to_the_neutral_referent_and_bad_names_never_crash():
    sources = _sources("窗口：骑车\n")
    for name, expected in (("", "对方喜欢骑车"), ("N\\A", "用户喜欢骑车")):
        state = garden_import.new_state(locale="zh-Hans", user_name=name, strategy="single_pass")
        store = Store()
        _run(state, sources, Model([("骑车", _reply(_card("用户喜欢骑车")))]), store)
        assert [c["summary"] for c in store.cards.values()] == [expected]


# --------------------------------------------------------------- host_note（VPS 张数引导）

_NOTE = "花园现有 2 张卡。参考 38–87 张。绝不编造。"


def test_host_note_goes_into_write_prompts_only_when_given():
    noted = garden_import.new_state(locale="zh-Hans", strategy="single_pass", host_note=_NOTE)
    model = Model([("窗一", _reply(_card("不吃辣")))])
    _run(noted, _sources("窗一 我不吃辣\n"), model, Store())
    assert f"[Host guidance]\n{_NOTE}\n" in model.prompts[0]

    # 托管 / 明文导入不传：状态形状和提示词都与之前一致。
    plain = garden_import.new_state(locale="zh-Hans", strategy="single_pass")
    assert "host_note" not in plain["params"]
    assert garden_import.new_state(locale="zh-Hans", strategy="single_pass",
                                   host_note="  ")["params"] == plain["params"]
    model2 = Model([("窗一", _reply(_card("不吃辣")))])
    _run(plain, _sources("窗一 我不吃辣\n"), model2, Store())
    assert "[Host guidance]" not in model2.prompts[0]
    assert model2.prompts[0] == model.prompts[0].replace(f"\n[Host guidance]\n{_NOTE}\n", "", 1)


def test_old_memgarden_without_host_note_field_builds_the_request_without_it(monkeypatch):
    import dataclasses

    import memgarden

    real = memgarden.ImportRequest
    old_fields = [(f.name, f.type, f) for f in dataclasses.fields(real) if f.name != "host_note"]
    OldImportRequest = dataclasses.make_dataclass(
        "ImportRequest", [(n, t, dataclasses.field(default=f.default,
                                                   default_factory=f.default_factory))
                          for n, t, f in old_fields])
    monkeypatch.setattr(memgarden, "ImportRequest", OldImportRequest)
    assert garden_import.import_request_accepts_host_note() is False
    params = garden_import.new_state(locale="zh-Hans", host_note=_NOTE)["params"]
    request = garden_import._request(_sources("窗一\n")[0], params, job_key="j")
    assert isinstance(request, OldImportRequest)

    monkeypatch.setattr(memgarden, "ImportRequest", real)
    assert garden_import.import_request_accepts_host_note() is True
    assert garden_import._request(_sources("窗一\n")[0], params, job_key="j").host_note == _NOTE
