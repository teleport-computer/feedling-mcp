from __future__ import annotations

import copy

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
