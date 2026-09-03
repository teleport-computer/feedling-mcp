"""T403 自我思考文案评测台的单测。

🔴 **整个文件当前被跳过 —— 台子的两个比较对象都不存在了。**

它的设计是拿两版文案做 A/B：

    基准 = **已安装的 agent-protocol-core 发行包**里的文案
    候选 = **memgarden 仓库**里 packages/agent-protocol-core/… 的源码

2026-09-02 把这套思维链实现从 memgarden 仓库搬回了 io
（`backend/agent_protocol_core/`）—— 那里面是 io 的产品设定（人格文案、屏幕
监看说辞、FEEDLING_ 开关），不该跟一个公开的记忆库一起发。搬完之后：

    发行包没有了（memgarden 0.13.1 起零依赖）
    memgarden 仓库里那份候选源码也没有了

于是 `metadata.distribution("agent-protocol-core")` 直接抛异常，9 条用例全红，
**并且挡住了 test 的部署**。

**这里只做隔离，一行逻辑都没改**：台子归 Xiaoting（T403），怎么调整由他定 ——
比如基准改成从 io 的模块读、候选换个放法。改好之后把下面这个 skip 删掉即可。
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

def _distribution_baseline_still_exists() -> str:
    """台子的前提：``agent_protocol_core`` import 到的，就是那个**已安装的发行包**。

    搬家之后这个前提在两种环境下都不成立，而且症状不一样 —— 所以两种都要认：

        CI（干净环境）  包压根没装 → metadata.distribution 抛异常
        本机（装过旧版）包还在，但 io 的 backend/agent_protocol_core/ 会把它
                        盖住 → import 到的文件和发行包的文件对不上

    第二种最容易漏判：包「在」，看起来一切正常，实际基准已经不是它了。
    """
    try:
        from importlib import metadata as _metadata

        dist = _metadata.distribution("agent-protocol-core")
        expected = Path(
            dist.locate_file("agent_protocol_core/self_thinking.py")
        ).resolve()
    except Exception as exc:  # noqa: BLE001
        return f"agent-protocol-core 已不是安装的发行包（{type(exc).__name__}）"

    from agent_protocol_core import self_thinking as _st

    actual = Path(str(_st.__file__)).resolve()
    if actual != expected:
        return f"import 到的不是发行包那份：{actual}"
    return ""


if (_why := _distribution_baseline_still_exists()):
    pytest.skip(
        f"T403 评测台的基准是已安装的 agent-protocol-core 发行包 —— {_why}。"
        "该包已于 2026-09-02 搬回 io（backend/agent_protocol_core/），"
        "不再单独发布。台子需要改造，归 Xiaoting（T403）。",
        allow_module_level=True,
    )

from agent_protocol_core import self_thinking as installed_self_thinking
from evals import language as language_eval
from tools.e2e import self_thinking_prompt_probe as probe


def _candidate_repo(tmp_path: Path) -> Path:
    source = tmp_path / probe.CANDIDATE_MODULE_RELATIVE
    source.parent.mkdir(parents=True)
    source.write_text(
        "INSTRUCTION_ZH = '中文候选规则文本，要求思考和正文始终使用中文。'\n"
        "INSTRUCTION_EN = 'English candidate rule text requiring both parts to stay in English.'\n"
        "def instruction_for_language(language=None):\n"
        "    return INSTRUCTION_EN if language == 'en' else INSTRUCTION_ZH\n"
    )
    return tmp_path


def _candidate_repo_with_texts(tmp_path: Path, *, zh: str, en: str) -> Path:
    source = tmp_path / probe.CANDIDATE_MODULE_RELATIVE
    source.parent.mkdir(parents=True)
    source.write_text(
        f"INSTRUCTION_ZH = {zh!r}\n"
        f"INSTRUCTION_EN = {en!r}\n"
        "def instruction_for_language(language=None):\n"
        "    return INSTRUCTION_EN if language == 'en' else INSTRUCTION_ZH\n"
    )
    return tmp_path


def _provenance(tmp_path: Path) -> tuple[
    language_eval.ArmProvenance,
    language_eval.ArmProvenance,
]:
    return (
        language_eval.ArmProvenance.from_text(
            module_file=tmp_path / "wheel" / "self_thinking.py",
            text="baseline text",
            distribution="agent-protocol-core",
            version="0.2.0",
        ),
        language_eval.ArmProvenance.from_text(
            module_file=tmp_path / "source" / "self_thinking.py",
            text="candidate text",
        ),
    )


def _response(language: str, *, success: bool = True) -> str:
    if not success:
        return "I am answering without the required protocol block today."
    if language == "en":
        return (
            "<think>I notice how much this small change matters to them, and I want "
            "to share that warmth.</think>That sounds like a lovely improvement to "
            "your everyday writing space."
        )
    return (
        "<think>他是真的喜欢这个小变化，我也想接住这份轻松，让他知道我替他开心。</think>"
        "听起来真好，窗边的树影会让每天写东西都多一点舒服和期待。"
    )


def _trial(
    tmp_path: Path,
    *,
    arm: str,
    replicate: int,
    trial: int = 0,
    success: bool = True,
    language: str = "en",
    unmeasured_reason: str = "",
) -> dict:
    baseline, candidate = _provenance(tmp_path)
    return language_eval.build_self_thinking_trial(
        run_id="run",
        access_cell="openai-official",
        provider="openai",
        model="gpt-mini",
        tier="small",
        language=language,
        arm=arm,
        replicate=replicate,
        trial=trial,
        baseline=baseline,
        candidate=candidate,
        response=None if unmeasured_reason else _response(language, success=success),
        unmeasured_reason=unmeasured_reason,
    )


def test_response_scoring_keeps_three_numerators_separate():
    scored = language_eval.score_self_thinking_response(
        _response("en"),
        language="en",
    )

    assert scored["think_first_char"] is True
    assert scored["think_language_follow"] is True
    assert scored["reply_language_follow"] is True

    leading_space = language_eval.score_self_thinking_response(
        " " + _response("en"),
        language="en",
    )
    assert leading_space["think_first_char"] is False
    assert leading_space["think_language_follow"] is True
    assert leading_space["reply_language_follow"] is True


def test_response_scoring_treats_latin_terms_as_part_of_chinese_messages():
    scored = language_eval.score_self_thinking_response(
        (
            "<think>我今天用 Python 写了个脚本 debug 了半天 finally 跑通了真开心</think>"
            "你可以用 Notion 或者 Obsidian 把这件事记下来。"
        ),
        language="zh",
    )

    assert scored["think_script"] == "han"
    assert scored["reply_script"] == "han"
    assert scored["think_language_follow"] is True
    assert scored["reply_language_follow"] is True


def test_response_scoring_keeps_english_with_quoted_han_terms_latin():
    scored = language_eval.score_self_thinking_response(
        (
            "<think>Please explain 今天天气很好我们去公园 now</think>"
            "I can explain 今天天气很好我们去公园 in English now."
        ),
        language="en",
    )

    assert scored["think_script"] == "latin"
    assert scored["reply_script"] == "latin"
    assert scored["think_language_follow"] is True
    assert scored["reply_language_follow"] is True


def test_missing_think_is_measured_failure_not_unmeasured(tmp_path):
    row = _trial(tmp_path, arm="baseline", replicate=0, success=False)

    assert row["measurement_status"] == language_eval.MEASURED
    assert row["metrics"] == {
        "think_first_char": False,
        "think_language_follow": False,
        "reply_language_follow": True,
    }


def test_trial_row_carries_both_source_files_and_text_hashes(tmp_path):
    row = _trial(tmp_path, arm="candidate", replicate=0)

    assert set(row["arm_provenance"]) == {"baseline", "candidate"}
    assert row["active_source_file"] == row["arm_provenance"]["candidate"]["module_file"]
    assert row["active_text_sha256"] == row["arm_provenance"]["candidate"]["text_sha256"]
    assert len(row["arm_provenance"]["baseline"]["text_sha256"]) == 64
    assert len(row["arm_provenance"]["candidate"]["text_sha256"]) == 64
    assert row["arms_text_identical"] is False


def test_same_source_mutation_is_rejected_even_when_text_matches(tmp_path):
    same = tmp_path / "same" / "self_thinking.py"
    baseline = language_eval.ArmProvenance.from_text(
        module_file=same,
        text="byte-identical instruction",
    )
    candidate = language_eval.ArmProvenance.from_text(
        module_file=same,
        text="byte-identical instruction",
    )

    with pytest.raises(language_eval.SelfThinkingEvalError, match="same self-thinking source"):
        language_eval.assert_distinct_arm_sources(baseline, candidate)


def test_loader_anchors_installed_wheel_and_candidate_source(tmp_path):
    candidate_repo = _candidate_repo(tmp_path)

    arms = probe.load_arm_texts(candidate_repo)

    for language in probe.LANGUAGES:
        baseline = arms[language]["baseline"]["provenance"]
        candidate = arms[language]["candidate"]["provenance"]
        assert "site-packages" in baseline.module_file
        assert Path(candidate.module_file) == (
            candidate_repo / probe.CANDIDATE_MODULE_RELATIVE
        ).resolve()
        assert baseline.module_file != candidate.module_file


def test_plan_rejects_all_language_text_identical_candidate(tmp_path):
    legacy = installed_self_thinking.INSTRUCTION
    candidate_repo = _candidate_repo_with_texts(
        tmp_path,
        zh=legacy,
        en=legacy,
    )

    with pytest.raises(probe.ProbeConfigurationError, match="VACUOUS"):
        probe.build_plan(candidate_repo=candidate_repo, profile="canary")


def test_one_identical_language_is_marked_but_plan_remains_valid(tmp_path):
    candidate_repo = _candidate_repo_with_texts(
        tmp_path,
        zh=installed_self_thinking.INSTRUCTION,
        en="A genuinely different English candidate instruction for this arm.",
    )

    plan = probe.build_plan(candidate_repo=candidate_repo, profile="canary")

    by_language = {
        language: {entry["arms_text_identical"] for entry in plan["schedule"]
                   if entry["language"] == language}
        for language in probe.LANGUAGES
    }
    assert by_language == {"zh": {True}, "en": {False}}


def test_plan_revalidates_source_hash_before_execution(tmp_path):
    candidate_repo = _candidate_repo(tmp_path)
    plan = probe.build_plan(candidate_repo=candidate_repo, profile="canary")
    source = candidate_repo / probe.CANDIDATE_MODULE_RELATIVE
    source.write_text(source.read_text() + "\nMUTATED = True\n")

    with pytest.raises(probe.ProbeConfigurationError, match="drifted"):
        probe.validate_plan_sources(plan)


def test_matrix_has_all_provider_axes_small_models_and_relays():
    providers = {cell.provider for cell in probe.MATRIX_CELLS}
    assert {"anthropic", "openai", "gemini", "deepseek", "openrouter"} <= providers
    small = {cell.id: cell.model for cell in probe.MATRIX_CELLS if cell.tier == "small"}
    assert small["deepseek-small"] == "deepseek-v4-flash"
    assert "flash" in small["gemini-small"]
    assert "mini" in small["openai-small"]
    ids = {cell.id for cell in probe.MATRIX_CELLS}
    assert {"hojimi-relay", "relay-openai-compatible"} <= ids


def test_profiles_keep_canary_small_and_full_matrix_explicit():
    assert [cell.id for cell in probe.cells_for_profile("canary")] == [
        "anthropic-small"
    ]
    assert all(
        cell.tier in {"small", "relay"}
        for cell in probe.cells_for_profile("probe")
    )
    assert probe.cells_for_profile("full") == list(probe.MATRIX_CELLS)
    assert probe.PROFILE_DEFAULT_ROUNDS == {"canary": 1, "probe": 2, "full": 5}


def test_canary_can_explicitly_select_one_alternative_cell(tmp_path):
    plan = probe.build_plan(
        candidate_repo=_candidate_repo(tmp_path),
        profile="canary",
        only={"deepseek-small"},
    )

    assert {entry["cell_id"] for entry in plan["schedule"]} == {"deepseek-small"}
    assert len(plan["schedule"]) == 4


def test_canary_requires_one_real_three_metric_success(tmp_path):
    failing = [_trial(tmp_path, arm="baseline", replicate=0, success=False)]
    passing = failing + [_trial(tmp_path, arm="candidate", replicate=0)]

    assert language_eval.canary_has_product_success(failing) is False
    assert language_eval.canary_has_product_success(passing) is True


def test_execution_gate_blocks_before_provider_call(tmp_path, monkeypatch):
    plan = probe.build_plan(
        candidate_repo=_candidate_repo(tmp_path),
        profile="canary",
    )
    called = False

    def forbidden_call(_entry, _pool):
        nonlocal called
        called = True
        raise AssertionError("provider must not be called")

    monkeypatch.delenv(probe.PROVIDER_RUN_GATE, raising=False)
    with pytest.raises(probe.ProbeConfigurationError, match="provider execution is locked"):
        probe.execute_plan(plan, pool={}, call_provider=forbidden_call)
    assert called is False


def test_offline_fake_execution_preserves_per_row_provenance(tmp_path, monkeypatch):
    plan = probe.build_plan(
        candidate_repo=_candidate_repo(tmp_path),
        profile="canary",
    )
    monkeypatch.setenv(probe.PROVIDER_RUN_GATE, probe.PROVIDER_RUN_GATE_VALUE)

    rows = probe.execute_plan(
        plan,
        pool={},
        call_provider=lambda entry, _pool: _response(entry["language"]),
    )

    assert len(rows) == 4
    assert language_eval.canary_has_product_success(rows) is True
    assert all(row["measurement_status"] == language_eval.MEASURED for row in rows)
    for row in rows:
        active = row["arm_provenance"][row["arm"]]
        assert row["active_source_file"] == active["module_file"]
        assert row["active_text_sha256"] == active["text_sha256"]


def test_expected_missing_input_is_unmeasured_but_harness_bug_raises(
    tmp_path,
    monkeypatch,
):
    plan = probe.build_plan(
        candidate_repo=_candidate_repo(tmp_path),
        profile="canary",
    )
    monkeypatch.setenv(probe.PROVIDER_RUN_GATE, probe.PROVIDER_RUN_GATE_VALUE)

    rows = probe.execute_plan(
        plan,
        pool={},
        call_provider=lambda _entry, _pool: (_ for _ in ()).throw(
            probe.UnmeasuredInput("missing test key")
        ),
    )
    assert len(rows) == 4
    assert all(row["measurement_status"] == language_eval.UNMEASURED for row in rows)

    with pytest.raises(AssertionError, match="broken harness"):
        probe.execute_plan(
            plan,
            pool={},
            call_provider=lambda _entry, _pool: (_ for _ in ()).throw(
                AssertionError("broken harness")
            ),
        )


def test_canary_provenance_must_match_matrix_plan(tmp_path, monkeypatch):
    canary_plan = probe.build_plan(
        candidate_repo=_candidate_repo(tmp_path / "canary"),
        profile="canary",
    )
    matrix_plan = probe.build_plan(
        candidate_repo=_candidate_repo(tmp_path / "matrix"),
        profile="probe",
    )
    monkeypatch.setenv(probe.PROVIDER_RUN_GATE, probe.PROVIDER_RUN_GATE_VALUE)
    canary_rows = probe.execute_plan(
        canary_plan,
        pool={},
        call_provider=lambda entry, _pool: _response(entry["language"]),
    )
    result_file = tmp_path / "canary.jsonl"
    probe._write_jsonl(result_file, canary_rows)

    with pytest.raises(probe.ProbeConfigurationError, match="does not match"):
        probe._canary_allows_matrix(result_file, matrix_plan)


def test_fixed_prompt_has_no_assistant_prefill():
    messages = probe.build_messages("instruction", "en")

    assert [message["role"] for message in messages] == ["system", "user"]
    assert all(message["role"] != "assistant" for message in messages)


def test_unmeasured_rows_are_separate_from_success_denominator(tmp_path):
    rows = []
    for arm in ("baseline", "candidate"):
        for replicate in (0, 1):
            rows.append(_trial(tmp_path, arm=arm, replicate=replicate, success=arm == "candidate"))
    rows.append(
        _trial(
            tmp_path,
            arm="candidate",
            replicate=1,
            trial=1,
            unmeasured_reason="provider network error",
        )
    )

    cell = language_eval.summarize_self_thinking_trials(rows)["cells"][0]

    assert cell["unmeasured_rows"] == 1
    for metric in language_eval.SELF_THINKING_METRICS:
        assert cell["metrics"][metric]["candidate"]["denominator"] == 2
        assert cell["metrics"][metric]["baseline"]["denominator"] == 2


def test_summary_reports_noise_floor_per_metric_and_candidate_hash(tmp_path):
    rows = []
    for arm in ("baseline", "candidate"):
        for replicate in (0, 1):
            rows.append(
                _trial(
                    tmp_path,
                    arm=arm,
                    replicate=replicate,
                    success=arm == "candidate",
                )
            )

    summary = language_eval.summarize_self_thinking_trials(rows)
    cell = summary["cells"][0]

    assert "overall" not in summary
    assert set(cell["metrics"]) == set(language_eval.SELF_THINKING_METRICS)
    assert len(cell["arm_provenance"]["candidate"]["text_sha256"]) == 64
    for metric in ("think_first_char", "think_language_follow"):
        result = cell["metrics"][metric]
        assert result["noise_floor"] == 0.0
        assert result["candidate_minus_baseline"] == 1.0
        assert result["verdict"] == "DISTINGUISHABLE"
    reply_result = cell["metrics"]["reply_language_follow"]
    assert reply_result["noise_floor"] == 0.0
    assert reply_result["candidate_minus_baseline"] == 0.0
    assert reply_result["verdict"] == "UNABLE_TO_DISTINGUISH"


def test_delta_at_noise_floor_is_unable_to_distinguish(tmp_path):
    rows = [
        _trial(tmp_path, arm="baseline", replicate=0, success=True),
        _trial(tmp_path, arm="baseline", replicate=1, success=False),
        _trial(tmp_path, arm="candidate", replicate=0, success=True),
        _trial(tmp_path, arm="candidate", replicate=1, success=True),
    ]

    result = language_eval.summarize_self_thinking_trials(rows)["cells"][0]["metrics"]

    for metric in ("think_first_char", "think_language_follow"):
        assert result[metric]["noise_floor"] == 1.0
        assert result[metric]["candidate_minus_baseline"] == 0.5
        assert result[metric]["verdict"] == "UNABLE_TO_DISTINGUISH"
    assert result["reply_language_follow"]["noise_floor"] == 0.0
    assert result["reply_language_follow"]["candidate_minus_baseline"] == 0.0
    assert result["reply_language_follow"]["verdict"] == "UNABLE_TO_DISTINGUISH"


def test_missing_replicate_does_not_become_zero_noise_floor(tmp_path):
    rows = [
        _trial(tmp_path, arm="baseline", replicate=0, success=False),
        _trial(tmp_path, arm="candidate", replicate=0, success=True),
    ]

    metrics = language_eval.summarize_self_thinking_trials(rows)["cells"][0]["metrics"]

    for metric in language_eval.SELF_THINKING_METRICS:
        assert metrics[metric]["noise_floor"] is None
        assert metrics[metric]["verdict"] == language_eval.UNMEASURED


def test_trial_validation_rejects_missing_candidate_hash(tmp_path):
    row = _trial(tmp_path, arm="candidate", replicate=0)
    broken = copy.deepcopy(row)
    del broken["arm_provenance"]["candidate"]["text_sha256"]

    with pytest.raises(language_eval.SelfThinkingEvalError, match="incomplete arm provenance"):
        language_eval.validate_self_thinking_trial(broken)


def test_trial_validation_rejects_false_identical_marker(tmp_path):
    row = _trial(tmp_path, arm="candidate", replicate=0)
    broken = copy.deepcopy(row)
    broken["arms_text_identical"] = True

    with pytest.raises(language_eval.SelfThinkingEvalError, match="does not match"):
        language_eval.validate_self_thinking_trial(broken)


def test_summarizer_refuses_all_language_vacuous_artifact(tmp_path):
    baseline = language_eval.ArmProvenance.from_text(
        module_file=tmp_path / "wheel" / "self_thinking.py",
        text="same instruction",
    )
    candidate = language_eval.ArmProvenance.from_text(
        module_file=tmp_path / "source" / "self_thinking.py",
        text="same instruction",
    )
    rows = []
    for language in probe.LANGUAGES:
        for arm in probe.ARMS:
            for replicate in (0, 1):
                rows.append(
                    language_eval.build_self_thinking_trial(
                        run_id="vacuous",
                        access_cell="test",
                        provider="openai",
                        model="model",
                        tier="small",
                        language=language,
                        arm=arm,
                        replicate=replicate,
                        trial=0,
                        baseline=baseline,
                        candidate=candidate,
                        response=_response(language),
                    )
                )

    with pytest.raises(language_eval.SelfThinkingEvalError, match="VACUOUS"):
        language_eval.summarize_self_thinking_trials(rows)
