"""花园语言判定 eval（宿主侧）—— 跑**内核随包分发的那份语料**，验 io 自己的取证。

## 分界线

2026-08-24 之后，判据本身搬进了内核（``memgarden.garden_language``），因为**错的是
算法，不是 io 的数据** —— 任何接入方都要做同一件判断，判据留在宿主等于让每个接入方
各踩一遍那次事故。

留在 io 的是**取证**：桶名从哪张表读、没有桶时看身份卡还是 locale、证据门槛多少。
这些是宿主的数据形状，内核碰不到。

所以这条 eval 的形状是：**语料用内核的，判定器用 io 的。**

    memgarden.contract 里的语料（随 wheel 分发）
        │
        └──→ io 的 garden_language_decision()   ←── 这条 eval 测的是它

两边守同一条线。要是 io 哪天在取证层面又发明了一套判据（比如读桶名之前先做个
"归一化"把英文桶全折叠掉），内核的语料会立刻把它照出来 —— 而只跑内核自己的 eval
是照不出来的，那边用的是内核自己的判定器。

## 为什么语料在包里而不在这个仓库

早先这份语料放在内核的**源码仓库**里，io 的 CI 上没有那个仓库，于是这条检查只会
打印「找不到语料，跳过」并返回 0 —— 看起来绿的，其实什么都没验。
**装饰性的检查比没有检查更糟**：它让人以为有防护。

现在语料作为包数据随 wheel 走，``pip install`` 之后就在，CI 上真的会跑。

跑法：``python3 evals/language.py``（需要装好 backend/requirements.txt）
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))

from agent_protocol_core import self_thinking  # noqa: E402
from memgarden.contract import basis_matches, garden_language_cases  # noqa: E402

from chat import language_follow  # noqa: E402
from chat.reply_language import (  # noqa: E402
    ReplyLanguage,
    garden_language_decision,
    reply_language_system_line,
)


SELF_THINKING_METRICS = (
    "think_first_char",
    "think_language_follow",
    "reply_language_follow",
)
MEASURED = "MEASURED"
UNMEASURED = "UNMEASURED"


class SelfThinkingEvalError(ValueError):
    """The result artifact cannot support the comparison it claims to make."""


@dataclass(frozen=True)
class ArmProvenance:
    """One language-specific instruction and the file that supplied it."""

    module_file: str
    text_sha256: str
    module_sha256: str = ""
    distribution: str = ""
    version: str = ""

    @classmethod
    def from_text(
        cls,
        *,
        module_file: str | pathlib.Path,
        text: str,
        module_sha256: str = "",
        distribution: str = "",
        version: str = "",
    ) -> "ArmProvenance":
        return cls(
            module_file=str(pathlib.Path(module_file).resolve()),
            text_sha256=hashlib.sha256(str(text).encode()).hexdigest(),
            module_sha256=module_sha256,
            distribution=distribution,
            version=version,
        )


def assert_distinct_arm_sources(
    baseline: ArmProvenance,
    candidate: ArmProvenance,
) -> None:
    """Fail closed when both arms resolve to one module file.

    Text hashes are deliberately not required to differ.  A language-specific
    rewrite may preserve one rendering byte-for-byte; provenance is about which
    artifact supplied the text, not whether two strings happen to compare equal.
    """

    if pathlib.Path(baseline.module_file).resolve() == pathlib.Path(
        candidate.module_file
    ).resolve():
        raise SelfThinkingEvalError(
            "baseline and candidate resolve to the same self-thinking source file"
        )


def _expected_script(language: str) -> str:
    if language == "en":
        return "latin"
    if language in {"zh", "zh-Hans"}:
        return "han"
    raise SelfThinkingEvalError(f"unsupported evaluation language: {language!r}")


def self_thinking_reply_language_rule(language: str) -> str:
    """Render the production reply-language rule for a T403 language cell."""

    policy_language = "en" if language == "en" else "zh-Hans"
    return reply_language_system_line(ReplyLanguage(policy_language))


def score_self_thinking_response(response: str, *, language: str) -> dict[str, Any]:
    """Score the three independent T403 numerators for one model response.

    A missing/malformed thinking block is a measured failure for both thinking
    metrics, not a reason to remove the row from the denominator.  Transport
    failures are represented separately by ``UNMEASURED`` trial rows.
    """

    raw = str(response or "")
    expected = _expected_script(language)
    status, thinking, reply = self_thinking.strip_all_thinking(
        raw,
        sanitize=False,
    )
    think_script = language_follow.classify_writing_system(thinking)
    reply_script = language_follow.classify_writing_system(reply)
    thinking_available = status in {self_thinking.COMPLETE, self_thinking.SILENT}
    return {
        "protocol_status": status,
        "think_script": think_script,
        "reply_script": reply_script,
        "think_first_char": raw.startswith("<think>"),
        "think_language_follow": thinking_available and think_script == expected,
        # Keep the visible-reply numerator independent from the think protocol.
        # ABSENT returns the untouched response as ``reply`` and can therefore
        # still follow the user's language even while first-character compliance
        # and think-language following fail.
        "reply_language_follow": reply_script == expected,
    }


def build_self_thinking_trial(
    *,
    run_id: str,
    access_cell: str,
    provider: str,
    model: str,
    tier: str,
    language: str,
    arm: str,
    replicate: int,
    trial: int,
    baseline: ArmProvenance,
    candidate: ArmProvenance,
    response: str | None = None,
    unmeasured_reason: str = "",
) -> dict[str, Any]:
    """Build one auditable JSONL row with both arms' provenance attached."""

    if arm not in {"baseline", "candidate"}:
        raise SelfThinkingEvalError(f"unknown arm: {arm!r}")
    assert_distinct_arm_sources(baseline, candidate)
    provenance = {
        "baseline": asdict(baseline),
        "candidate": asdict(candidate),
    }
    active = provenance[arm]
    row: dict[str, Any] = {
        "schema_version": 1,
        "run_id": str(run_id),
        "access_cell": str(access_cell),
        "provider": str(provider),
        "model": str(model),
        "tier": str(tier),
        "language": str(language),
        "arm": arm,
        "replicate": int(replicate),
        "trial": int(trial),
        "arm_provenance": provenance,
        "active_source_file": active["module_file"],
        "active_text_sha256": active["text_sha256"],
        "arms_text_identical": (
            provenance["baseline"]["text_sha256"]
            == provenance["candidate"]["text_sha256"]
        ),
    }
    if unmeasured_reason:
        row.update(
            {
                "measurement_status": UNMEASURED,
                "unmeasured_reason": str(unmeasured_reason),
                "metrics": {metric: None for metric in SELF_THINKING_METRICS},
            }
        )
    else:
        scored = score_self_thinking_response(response or "", language=language)
        row.update(
            {
                "measurement_status": MEASURED,
                "response": str(response or ""),
                "response_sha256": hashlib.sha256(
                    str(response or "").encode()
                ).hexdigest(),
                "protocol_status": scored["protocol_status"],
                "think_script": scored["think_script"],
                "reply_script": scored["reply_script"],
                "metrics": {
                    metric: bool(scored[metric])
                    for metric in SELF_THINKING_METRICS
                },
            }
        )
    validate_self_thinking_trial(row)
    return row


def validate_self_thinking_trial(row: dict[str, Any]) -> None:
    """Reject rows that cannot answer which text each arm used."""

    provenance = row.get("arm_provenance")
    if not isinstance(provenance, dict):
        raise SelfThinkingEvalError("trial row lacks arm_provenance")
    try:
        baseline = ArmProvenance(**provenance["baseline"])
        candidate = ArmProvenance(**provenance["candidate"])
    except (KeyError, TypeError) as exc:
        raise SelfThinkingEvalError("trial row has incomplete arm provenance") from exc
    assert_distinct_arm_sources(baseline, candidate)
    arm = str(row.get("arm") or "")
    if arm not in provenance:
        raise SelfThinkingEvalError(f"trial row has unknown arm: {arm!r}")
    active = provenance[arm]
    if row.get("active_source_file") != active.get("module_file"):
        raise SelfThinkingEvalError("active source file does not match selected arm")
    if row.get("active_text_sha256") != active.get("text_sha256"):
        raise SelfThinkingEvalError("active text hash does not match selected arm")
    expected_identical = (
        provenance["baseline"].get("text_sha256")
        == provenance["candidate"].get("text_sha256")
    )
    if row.get("arms_text_identical") is not expected_identical:
        raise SelfThinkingEvalError(
            "arms_text_identical does not match the two text hashes"
        )
    status = row.get("measurement_status")
    if status not in {MEASURED, UNMEASURED}:
        raise SelfThinkingEvalError(f"invalid measurement status: {status!r}")
    metrics = row.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != set(SELF_THINKING_METRICS):
        raise SelfThinkingEvalError("trial row must carry exactly the three metrics")
    expected_values = {True, False} if status == MEASURED else {None}
    if any(value not in expected_values for value in metrics.values()):
        raise SelfThinkingEvalError("metric values do not match measurement status")


def canary_has_product_success(rows: list[dict[str, Any]]) -> bool:
    """A canary starts the matrix only after the model gets one row right."""

    for row in rows:
        validate_self_thinking_trial(row)
        if row["measurement_status"] != MEASURED:
            continue
        if all(row["metrics"][metric] is True for metric in SELF_THINKING_METRICS):
            return True
    return False


def _rate(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    measured = [row for row in rows if row["measurement_status"] == MEASURED]
    numerator = sum(row["metrics"][metric] is True for row in measured)
    denominator = len(measured)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def _single_provenance(rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    values = {
        (
            row["arm_provenance"][arm]["module_file"],
            row["arm_provenance"][arm]["text_sha256"],
            row["arm_provenance"][arm].get("module_sha256", ""),
            row["arm_provenance"][arm].get("distribution", ""),
            row["arm_provenance"][arm].get("version", ""),
        )
        for row in rows
    }
    if len(values) != 1:
        raise SelfThinkingEvalError(
            f"one result cell contains multiple {arm} prompt provenances"
        )
    module_file, text_sha256, module_sha256, distribution, version = values.pop()
    return {
        "module_file": module_file,
        "text_sha256": text_sha256,
        "module_sha256": module_sha256,
        "distribution": distribution,
        "version": version,
    }


def summarize_self_thinking_trials(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize per provider/model/language cell without an aggregate score.

    Noise floor for a metric is the larger same-prompt self-difference across
    the two arms: ``max(|baseline r0-r1|, |candidate r0-r1|)``.  A cross-arm
    delta whose magnitude does not exceed that floor is labelled
    ``UNABLE_TO_DISTINGUISH`` rather than reported as an effect.
    """

    identical_by_language: dict[str, set[bool]] = {}
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        validate_self_thinking_trial(row)
        identical_by_language.setdefault(str(row["language"]), set()).add(
            bool(row["arms_text_identical"])
        )
        key = (
            str(row["access_cell"]),
            str(row["provider"]),
            str(row["model"]),
            str(row["tier"]),
            str(row["language"]),
        )
        groups.setdefault(key, []).append(row)

    if any(len(values) != 1 for values in identical_by_language.values()):
        raise SelfThinkingEvalError(
            "one language contains inconsistent arms_text_identical markers"
        )
    if set(identical_by_language) == {"zh", "en"} and all(
        values == {True} for values in identical_by_language.values()
    ):
        raise SelfThinkingEvalError(
            "VACUOUS: baseline and candidate text are identical in every language"
        )

    cells = []
    for key in sorted(groups):
        access_cell, provider, model, tier, language = key
        cell_rows = groups[key]
        provenance = {
            arm: _single_provenance(cell_rows, arm)
            for arm in ("baseline", "candidate")
        }
        assert_distinct_arm_sources(
            ArmProvenance(**provenance["baseline"]),
            ArmProvenance(**provenance["candidate"]),
        )
        metric_results: dict[str, Any] = {}
        for metric in SELF_THINKING_METRICS:
            by_arm = {
                arm: _rate([row for row in cell_rows if row["arm"] == arm], metric)
                for arm in ("baseline", "candidate")
            }
            replicate_rates: dict[str, dict[str, Any]] = {}
            for arm in ("baseline", "candidate"):
                arm_rows = [row for row in cell_rows if row["arm"] == arm]
                replicate_rates[arm] = {
                    str(replicate): _rate(
                        [row for row in arm_rows if row["replicate"] == replicate],
                        metric,
                    )
                    for replicate in sorted({row["replicate"] for row in arm_rows})
                }
            two_replicates = all(
                set(replicate_rates[arm]) == {"0", "1"}
                and all(
                    replicate_rates[arm][rep]["rate"] is not None
                    for rep in ("0", "1")
                )
                for arm in ("baseline", "candidate")
            )
            both_arms = all(by_arm[arm]["rate"] is not None for arm in by_arm)
            if not two_replicates or not both_arms:
                noise_floor = None
                delta = None
                verdict = UNMEASURED
            else:
                self_differences = [
                    abs(
                        replicate_rates[arm]["0"]["rate"]
                        - replicate_rates[arm]["1"]["rate"]
                    )
                    for arm in ("baseline", "candidate")
                ]
                noise_floor = max(self_differences)
                delta = by_arm["candidate"]["rate"] - by_arm["baseline"]["rate"]
                verdict = (
                    "DISTINGUISHABLE"
                    if abs(delta) > noise_floor
                    else "UNABLE_TO_DISTINGUISH"
                )
            metric_results[metric] = {
                "baseline": by_arm["baseline"],
                "candidate": by_arm["candidate"],
                "replicates": replicate_rates,
                "candidate_minus_baseline": delta,
                "noise_floor": noise_floor,
                "verdict": verdict,
            }
        cells.append(
            {
                "access_cell": access_cell,
                "provider": provider,
                "model": model,
                "tier": tier,
                "language": language,
                "arm_provenance": provenance,
                "arms_text_identical": bool(cell_rows[0]["arms_text_identical"]),
                "unmeasured_rows": sum(
                    row["measurement_status"] == UNMEASURED for row in cell_rows
                ),
                "metrics": metric_results,
            }
        )
    return {
        "schema_version": 1,
        "metric_definitions": {
            "think_first_char": "raw model output begins with exact <think>",
            "think_language_follow": "complete thinking block uses the requested writing system",
            "reply_language_follow": "visible reply uses the requested writing system",
        },
        "cells": cells,
    }


def _decider(evidence: dict) -> dict:
    """把契约给的证据，翻译成 io 的取证入口。

    ⚠️ 证据里**没有桶名**，这是契约的形状决定的 —— 桶名是 AI 的输出，且大量是
    人名/公司名这类不携带语言信息的专有名词。io 这边照样把 existing_buckets 传下去，
    但那只走观测字段，不参与判定；契约里那几条 James / 品牌名的用例就是在守这一点。
    """
    d = garden_language_decision(
        {},
        written=evidence.get("written") or "",
        locale=evidence.get("locale") or "",
        # 故意塞一串英文桶名进去：判定**不该**因此改变。
        existing_buckets="James、Sarah、OpenAI、GitHub",
    )
    return {"locale": d["locale"], "basis": d["basis"]}


def _run_supported_contract() -> list[str]:
    """Run the pinned corpus rows whose evidence contract IO still accepts.

    The pinned memgarden wheel still carries an ``explicit`` input tier. IO no
    longer stores or consumes that derived field, so those rows describe a
    deliberately retired host input rather than the current adapter contract.
    Written-language and locale/default rows remain independent golden cases.
    """

    cases = [case for case in garden_language_cases() if not case.get("explicit")]
    failures: list[str] = []
    for case in cases:
        got = _decider({
            "written": case.get("written") or "",
            "locale": case.get("locale") or None,
        })
        if got.get("locale") != case["expect"] or not basis_matches(
            got.get("basis"), case.get("expect_basis")
        ):
            failures.append(str(case["id"]))
    print(f"支持中的语料：{len(cases) - len(failures)}/{len(cases)} 通过")
    return failures


def main() -> int:
    print("语料：memgarden.contract（随 wheel 分发）")
    print("判定器：io 的 chat.reply_language.garden_language_decision\n")
    fails = _run_supported_contract()
    if fails:
        print(f"  失败：{', '.join(fails)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
