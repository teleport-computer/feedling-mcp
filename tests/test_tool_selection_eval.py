from __future__ import annotations

import copy
import json
import sys

import pytest

from tools.e2e import tool_selection_eval as evaluator


def _scored(actual: str, case: dict) -> dict:
    observation = {"outcome": "picked", "picked": actual, "picked_all": [actual]}
    return {**case, **observation, "_d": evaluator.classify_row(observation, case)}


def test_catalog_preserves_intentional_chat_and_folded_boundaries():
    full = {t["function"]["name"]: t for t in evaluator.build_catalog("full")}
    folded = {t["function"]["name"]: t for t in evaluator.build_catalog("folded")}

    assert "stay_silent" not in full
    assert full.keys() == folded.keys()
    for name in evaluator.v2_tool_surface.RESIDENT_TOOL_NAMES:
        assert folded[name] == full[name]


def test_real_cases_use_per_case_runtime_fixtures_without_screen_pollution():
    doc = evaluator.load_cases()
    screen_case = next(c for c in doc["cases"] if c["id"] == "scr_read_1")
    memory_case = next(c for c in doc["cases"] if c["id"] == "mem_search_1")

    assert evaluator.runtime_data_for_case(screen_case)["runtime_data"] == {
        "screen_share": {"active": True, "latest_frame_age_sec": 5}
    }
    assert evaluator.runtime_data_for_case(memory_case)["runtime_data"] == {}


def test_case_health_rejects_global_screen_fixture_pollution():
    doc = evaluator.load_cases()
    polluted = copy.deepcopy(doc)
    memory_case = next(c for c in polluted["cases"] if c["id"] == "mem_search_1")
    memory_case["runtime_fixtures"] = ["screen_share_active"]

    problems = evaluator.check_case_health(polluted)

    assert any("screen_share_active 污染了非 screen case" in p for p in problems)


def test_case_health_keeps_current_message_images_out_of_photo_tools():
    doc = evaluator.load_cases()
    broken = copy.deepcopy(doc)
    broken["native_image_cases"][0]["expect"] = "photo_read"

    problems = evaluator.check_case_health(broken)

    assert any("native image 不得期望 photo 工具" in p for p in problems)


def test_case_health_requires_native_image_input_fixture():
    doc = evaluator.load_cases()
    broken = copy.deepcopy(doc)
    broken["native_image_cases"][0].pop("input_fixture")

    problems = evaluator.check_case_health(broken)

    assert any("必须声明当前消息原生图片 fixture" in p for p in problems)


def test_also_ok_frontier_is_a_correct_first_action():
    case = {
        "id": "workspace-frontier",
        "family": "workspace",
        "expect": "workspace_read",
        "also_ok": ["workspace_list"],
        "utterance": "读我上次那份方案",
    }
    observation = {
        "outcome": "picked",
        "picked": "workspace_list",
        "picked_all": ["workspace_list"],
    }

    assert evaluator.classify_row(observation, case)["klass"] == "singleton_also_ok"


def test_wrong_gold_mutation_lowers_score():
    good_case = {
        "id": "gold",
        "family": "workspace",
        "expect": "workspace_read",
        "utterance": "读 /workspace/plan.md",
    }
    bad_case = {**good_case, "expect": "workspace_delete"}

    good = evaluator.report([_scored("workspace_read", good_case)])["all"]
    bad = evaluator.report([_scored("workspace_read", bad_case)])["all"]

    assert good["strict_ok"] == 1.0
    assert bad["strict_ok"] == 0.0


def test_report_keeps_unfiltered_denominator_visible():
    case = {
        "id": "denominator",
        "family": "workspace",
        "expect": "workspace_read",
        "utterance": "读 /workspace/plan.md",
    }
    invalid_observation = {"outcome": "transport_error", "picked_all": []}
    rows = [
        _scored("workspace_read", case),
        {**case, **invalid_observation,
         "_d": evaluator.classify_row(invalid_observation, case)},
    ]

    metrics = evaluator.report(rows)["all"]

    assert metrics["strict_ok"] == 1.0
    assert metrics["strict_ok_all_rows"] == 0.5
    assert metrics["strict_ok_n"] == 1
    assert metrics["n"] == 1
    assert metrics["all_rows_n"] == 2
    assert metrics["invalid"] == 1


def test_report_labels_every_ratio_and_distinguishes_also_ok(capsys):
    case = {
        "id": "frontier",
        "family": "workspace",
        "expect": "workspace_read",
        "also_ok": ["workspace_list"],
        "utterance": "读那份方案",
    }

    report = evaluator.report([_scored("workspace_list", case)])
    definitions = report["metric_definitions"]
    ratio_keys = {
        key
        for key, value in report["all"].items()
        if isinstance(value, float) or value is None
    }

    assert ratio_keys == set(definitions)
    assert "excludes also_ok" in definitions["strict_exact"]
    assert "expect or also_ok" in definitions["strict_ok"]
    stdout = capsys.readouterr().out
    assert "strict_exact: valid observations" in stdout
    assert "strict_ok: valid observations" in stdout


def test_completion_requires_exact_chain_and_visible_reply():
    case = {
        "tool_steps": [
            {"expect": "photo_recent", "result": {}},
            {"expect": "photo_read", "result": {}},
        ],
        "require_visible_reply": True,
    }

    complete = evaluator.score_completion_row(
        {"actual_calls": ["photo_recent", "photo_read"],
         "actual_rounds": [["photo_recent"], ["photo_read"]],
         "final_text": "是一只金毛。"},
        case,
    )
    overcalled = evaluator.score_completion_row(
        {"actual_calls": ["photo_recent", "history_search", "photo_read"],
         "actual_rounds": [["photo_recent"], ["history_search"], ["photo_read"]],
         "final_text": "是一只金毛。"},
        case,
    )
    silent = evaluator.score_completion_row(
        {"actual_calls": ["photo_recent", "photo_read"],
         "actual_rounds": [["photo_recent"], ["photo_read"]],
         "final_text": ""},
        case,
    )
    same_round = evaluator.score_completion_row(
        {"actual_calls": ["photo_recent", "photo_read"],
         "actual_rounds": [["photo_recent", "photo_read"]],
         "final_text": "是一只金毛。"},
        case,
    )

    assert complete["completed"] is True
    assert complete["total_calls"] == 2
    assert overcalled["completed"] is False
    assert overcalled["overcall"] is True
    assert silent["completed"] is False
    assert silent["visible_reply"] is False
    assert same_round["completed"] is False


def test_completion_probe_replays_deterministic_tool_exchange(monkeypatch):
    case = {
        "id": "completion",
        "utterance": "读那份方案",
        "tool_steps": [
            {"expect": "workspace_list", "result": {"path": "/workspace/plan.md"}},
            {"expect": "workspace_read", "result": {"content": "周五上线"}},
        ],
        "require_visible_reply": True,
    }
    responses = iter([
        {
            "outcome": "picked",
            "_assistant_message": {"role": "assistant", "tool_calls": [
                {"id": "call-1", "function": {"name": "workspace_list", "arguments": "{}"}}
            ]},
            "_tool_calls": [
                {"id": "call-1", "function": {"name": "workspace_list", "arguments": "{}"}}
            ],
        },
        {
            "outcome": "picked",
            "_assistant_message": {"role": "assistant", "tool_calls": [
                {"id": "call-2", "function": {"name": "workspace_read", "arguments": "{}"}}
            ]},
            "_tool_calls": [
                {"id": "call-2", "function": {"name": "workspace_read", "arguments": "{}"}}
            ],
        },
        {"outcome": "no_tool_call", "text": "方案写的是周五上线。",
         "_assistant_message": {"role": "assistant", "content": "方案写的是周五上线。"},
         "_tool_calls": []},
    ])

    monkeypatch.setattr(evaluator, "_probe_deepseek_messages",
                        lambda _key, _tools, _messages: next(responses))

    result = evaluator.probe_deepseek_completion("key", [], case)

    assert result["actual_calls"] == ["workspace_list", "workspace_read"]
    assert result["actual_rounds"] == [["workspace_list"], ["workspace_read"]]
    assert result["_completion"]["completed"] is True


def test_completion_rejects_wrong_fixture_identifier(monkeypatch):
    case = {
        "id": "completion",
        "utterance": "读那份方案",
        "tool_steps": [
            {"expect": "workspace_list", "result": {"path": "/workspace/plan.md"}},
            {"expect": "workspace_read",
             "args_contains": {"path": "/workspace/plan.md"},
             "result": {"content": "周五上线"}},
        ],
        "require_visible_reply": True,
    }
    responses = iter([
        {"outcome": "picked", "_assistant_message": {"role": "assistant"},
         "_tool_calls": [{"id": "one", "function": {
             "name": "workspace_list", "arguments": "{}"}}]},
        {"outcome": "picked", "_assistant_message": {"role": "assistant"},
         "_tool_calls": [{"id": "two", "function": {
             "name": "workspace_read", "arguments": '{"path":"/workspace/wrong.md"}'}}]},
        {"outcome": "no_tool_call", "text": "读完了", "_tool_calls": []},
    ])
    monkeypatch.setattr(evaluator, "_probe_deepseek_messages",
                        lambda _key, _tools, _messages: next(responses))

    result = evaluator.probe_deepseek_completion("key", [], case)

    assert result["argument_errors"] == [{
        "round": 1,
        "tool": "workspace_read",
        "error": "path!='/workspace/plan.md'",
    }]
    assert result["_completion"]["completed"] is False


# --------------------------------------------------------------------------
# rescore 五条契约的回归网。
#
# 这五条的**实现**是上一窗按 codex 复审(20260819T155312Z)补的,并已随
# `0b688259` 入库;但入库时的验证是**一次性离线脚本**,没有留在仓库里。
# 2026-08-20 复核:对这五条各打一处突变(只取 blob[0] / 去掉 fail-closed /
# 从 JSON 里删掉指标 / 条件 overcall 退回无条件 / cases hash 回磁盘重读 /
# 产物旧标签反向覆盖金标),既有 12 条用例**六条全绿**——即"实现对但没有收口"。
# 下面每条都对着那处突变写,并各自带阴性对照。
# --------------------------------------------------------------------------


def _artifact_block(doc: dict, mode: str = "full", wrong_every: int = 0) -> dict:
    """构造一个 provenance 合法、case 覆盖完整的产物块。

    ``wrong_every>0`` 时每隔 N 条故意选错,好让 strict_exact 不是恒 100%
    (否则"JSON 与 stdout 同源"那条会退化成比较两个 1.0)。
    """
    tools = evaluator.build_catalog(mode)
    rows = []
    for index, case in enumerate(doc["cases"]):
        picked = case["expect"]
        if wrong_every and index % wrong_every == 0:
            picked = "stay_silent"          # 不在 chat 目录里,必然判 singleton_wrong
        rows.append({
            "id": case["id"], "family": case["family"], "expect": case["expect"],
            "utterance": case.get("utterance"),
            "outcome": "picked", "picked": picked, "picked_all": [picked],
        })
    return {
        "mode": mode, "arm": "deepseek",
        "provenance": evaluator.provenance(mode, tools, doc),
        "rows": rows,
    }


def _write(tmp_path, name: str, payload) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _run_main(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["tool_selection_eval.py", *argv])
    return evaluator.main()


def test_rescore_scores_every_block_of_a_both_mode_artifact(tmp_path):
    """`--mode both` 的产物有 full+folded 两个块,第二个不许被静默丢掉。"""
    doc = evaluator.load_cases()
    path = _write(tmp_path, "both.json",
                  [_artifact_block(doc, "full"), _artifact_block(doc, "folded")])

    res = evaluator.rescore_artifacts([path])

    assert [f["block_index"] for f in res["files"]] == [0, 1]
    assert [f["mode"] for f in res["files"]] == ["full", "folded"]
    # 两个块的行都要在,且能按 (file, block_index) 追溯来源
    assert {r["_block"] for r in res["rows"]} == {0, 1}
    assert len(res["rows"]) == 2 * len(doc["cases"])
    assert len({tuple(f["group"]) for f in res["files"]}) == 2
    # 阴性对照:单块产物就该只出一个块,证明上面的 2 不是恒真
    single = _write(tmp_path, "one.json", _artifact_block(doc, "full"))
    only = evaluator.rescore_artifacts([single])
    assert [f["block_index"] for f in only["files"]] == [0]
    assert len(only["rows"]) == len(doc["cases"])


def test_rescore_fails_closed_on_structurally_bad_artifact(tmp_path, monkeypatch):
    """结构有问题的产物:默认非零退出且**不写正式 summary**,只有显式开关才放行。"""
    doc = evaluator.load_cases()
    block = _artifact_block(doc, "full")
    block["rows"].append({**block["rows"][0], "id": "not_a_real_case_id"})
    path = _write(tmp_path, "bad.json", block)
    out = tmp_path / "summary.json"

    rc = _run_main(monkeypatch, ["--rescore", path, "--out", str(out)])

    assert rc == 2
    assert not out.exists(), "fail closed 时绝不能发布正式 summary"

    # 阴性对照 ①:同一份产物,显式加开关就放行,但输出必须自标 invalid
    rc_allowed = _run_main(monkeypatch, [
        "--rescore", path, "--out", str(out), "--allow-invalid-artifacts"])
    assert rc_allowed == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["artifacts_invalid"] is True
    assert payload["allow_invalid_artifacts"] is True
    assert payload["files"][0]["unknown_cases"] == ["not_a_real_case_id"]

    # 阴性对照 ②:干净产物走默认路径必须 exit 0 并写出 summary,
    # 证明上面的 rc==2 是坏产物触发的,不是这条路径恒非零
    good = _write(tmp_path, "good.json", _artifact_block(doc, "full"))
    clean_out = tmp_path / "clean.json"
    assert _run_main(monkeypatch, ["--rescore", good, "--out", str(clean_out)]) == 0
    assert json.loads(clean_out.read_text(encoding="utf-8"))["artifacts_invalid"] is False


def test_rescore_machine_summary_carries_the_same_numbers_as_stdout(
        tmp_path, monkeypatch, capsys):
    """机器可读 summary 必须带指标,且与打印的表**同源**——不许两套算法。"""
    doc = evaluator.load_cases()
    path = _write(tmp_path, "artifact.json", _artifact_block(doc, "full", wrong_every=3))
    out = tmp_path / "summary.json"

    assert _run_main(monkeypatch, ["--rescore", path, "--out", str(out)]) == 0
    stdout = capsys.readouterr().out
    payload = json.loads(out.read_text(encoding="utf-8"))

    groups = payload["metrics_by_group"]
    assert len(groups) == 1
    metrics = next(iter(groups.values()))
    overall = metrics["all"]
    # 指标本身必须在 JSON 里(codex 复审:只有 files/summary_rows 不算机器可读)
    assert {"n", "all_rows_n", "invalid", "strict_exact", "strict_ok",
            "overcall_given_target", "classes"} <= set(overall)
    assert metrics["by_family"], "缺 by-family 指标"

    # 故意选错了一部分 ⇒ 这个数不是恒 1.0,比较才有意义
    assert 0.0 < overall["strict_exact"] < 1.0
    printed = next(line for line in stdout.splitlines() if line.startswith("(group)"))
    assert float(printed.split()[1].rstrip("%")) == pytest.approx(
        100 * overall["strict_exact"], abs=0.05)


def test_conditional_overcall_has_its_own_denominator():
    """P(overcall | target_present):分母是"目标在里面"的观测,不是全部观测。"""
    case = {"id": "c", "family": "workspace", "expect": "workspace_read",
            "utterance": "读那份方案"}

    def row(picked_all):
        observation = {"outcome": "picked", "picked": picked_all[0],
                       "picked_all": picked_all}
        return {**case, **observation,
                "_d": evaluator.classify_row(observation, case)}

    mixed = evaluator.report([
        row(["workspace_read", "memory_search"]),   # 目标在 + 多调
        row(["workspace_read"]),                    # 目标在 + 单调
        row(["memory_search", "history_search"]),   # 目标不在 + 多调
    ])["all"]
    # 无条件 2/3,条件 1/2 —— 两个数必须不同,否则这条测不出退化
    assert mixed["overcall"] == pytest.approx(2 / 3)
    assert mixed["overcall_given_target"] == pytest.approx(1 / 2)
    assert mixed["target_present_n"] == 2

    # 分母为 0 → N/A,**不许伪造成 0%**;此时无条件指标仍有值,是真正的区分点
    no_target = evaluator.report([row(["memory_search", "history_search"]),
                                  row(["memory_search"])])["all"]
    assert no_target["target_present_n"] == 0
    assert no_target["overcall_given_target"] is None
    assert no_target["overcall"] == pytest.approx(1 / 2)


def test_provenance_hashes_the_frozen_cases_not_the_file_on_disk():
    """cases hash 从**发题用的那份 frozen doc** 算,不回磁盘重读(与 catalog 同类竞态)。"""
    doc = evaluator.load_cases()
    frozen = copy.deepcopy(doc)
    frozen["cases"] = frozen["cases"][:3]          # 模拟"发题用的是这一份"

    prov = evaluator.provenance("full", evaluator.build_catalog("full"), frozen)

    canonical = evaluator._sha(
        json.dumps(frozen, ensure_ascii=False, sort_keys=True))
    assert prov["cases_sha256"] == canonical
    assert prov["cases_snapshot"] == frozen
    # 两个阴性对照:既不能等于磁盘原文的 hash,也不能等于磁盘那份 doc 的规范化 hash
    assert prov["cases_sha256"] != evaluator._sha(
        evaluator._CASES_PATH.read_text(encoding="utf-8"))
    assert prov["cases_sha256"] != evaluator._sha(
        json.dumps(doc, ensure_ascii=False, sort_keys=True))
    # 而且 snapshot↔hash 自洽,产物能自证(validate 用的就是这条)
    assert evaluator.validate_provenance(
        {"mode": "full", "arm": "deepseek", "provenance": prov}) == (True, [])


def test_rescore_lets_current_gold_override_stale_artifact_labels(tmp_path):
    """产物里的旧 expect/family 不许反向覆盖当前金标 —— 那是拿旧答案判新题。"""
    doc = evaluator.load_cases()
    block = _artifact_block(doc, "full")
    gold = doc["cases"][0]
    stale_expect = next(c["expect"] for c in doc["cases"]
                        if c["expect"] != gold["expect"])
    row = next(r for r in block["rows"] if r["id"] == gold["id"])
    row["expect"] = stale_expect
    row["family"] = "stale_family"
    row["picked"] = stale_expect
    row["picked_all"] = [stale_expect]
    path = _write(tmp_path, "stale.json", block)

    scored = next(r for r in evaluator.rescore_artifacts([path])["rows"]
                  if r["id"] == gold["id"])

    assert scored["expect"] == gold["expect"]
    assert scored["family"] == gold["family"]
    assert scored["_stale_labels"] == {"expect": stale_expect,
                                       "family": "stale_family"}
    # 按当前金标判:选了旧标签那个工具就是选错
    assert scored["_d"]["klass"] == "singleton_wrong"
    # 阴性对照:同一份产物里没被改标签的行,不许被标成 stale
    untouched = next(r for r in evaluator.rescore_artifacts([path])["rows"]
                     if r["id"] == doc["cases"][1]["id"])
    assert untouched["_stale_labels"] is None
    assert untouched["_d"]["klass"] == "singleton_exact"
