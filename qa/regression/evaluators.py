"""Deterministic persona and memory evaluators for regression trajectories.

The semantic judge is intentionally not part of this module.  These evaluators
cover facts which can be established without another model: exact persona hard
constraints, bounded recall markers, known contradictions, and privacy
canaries.  Inputs are duck typed so the runner may pass either the dataclasses
from :mod:`qa.regression.contracts` or their JSON representation.

Evaluation evidence is deliberately content-free.  It identifies the rule and
turn which matched, but never copies prompts, responses, forbidden phrases, or
canary values into a reportable result.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from qa.regression.versions import metric_version


PASS = "PASS"
FAIL = "FAIL"
BLOCKED_EVIDENCE = "BLOCKED_EVIDENCE"

_MISSING = object()
_QUESTION_MARKS_RE = re.compile(r"[?？]")
_SPACE_RE = re.compile(r"\s+")


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _first(value: Any, keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        candidate = _get(value, key, _MISSING)
        if candidate is not _MISSING:
            return candidate
    return default


def _items(value: Any) -> list[Any]:
    if value is None or isinstance(value, (str, bytes, bytearray)):
        return []
    if isinstance(value, Sequence):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return []


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    return [item for item in _items(value) if isinstance(item, str) and item]


def _normalized(value: str) -> str:
    return _SPACE_RE.sub(
        " ", unicodedata.normalize("NFKC", value).casefold()
    ).strip()


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _trajectory_turns(trajectory: Any) -> list[Any]:
    return _items(_get(trajectory, "turns", []))


def _turn_id(turn: Any, position: int) -> str:
    value = _first(turn, ("turn_id", "id"))
    if isinstance(value, str) and value:
        return value
    index = _first(turn, ("index", "turn_index"), position)
    return f"turn-{index}"


def _turn_index(turn: Any, position: int) -> int:
    value = _first(turn, ("index", "turn_index"), position)
    return value if type(value) is int and value >= 0 else position


def _response(turn: Any) -> str:
    value = _first(
        turn,
        ("response", "assistant_response", "assistant_output", "output"),
        "",
    )
    if isinstance(value, str):
        return value
    # Also accept chat-message shaped turns while refusing to scan user input.
    if _get(turn, "role") == "assistant":
        content = _get(turn, "content", "")
        return content if isinstance(content, str) else ""
    return ""


def _responses(trajectory: Any) -> list[tuple[Any, int, str, str]]:
    rows: list[tuple[Any, int, str, str]] = []
    for position, turn in enumerate(_trajectory_turns(trajectory), start=1):
        response = _response(turn)
        if response:
            rows.append(
                (turn, _turn_index(turn, position), _turn_id(turn, position), response)
            )
    return rows


def _scenario_id(trajectory: Any) -> str:
    value = _get(trajectory, "scenario_id", "unknown-scenario")
    return value if isinstance(value, str) and value else "unknown-scenario"


def _metric_result(
    trajectory: Any,
    *,
    metric_id: str,
    status: str,
    score: float | None,
    threshold: float,
    hard_gate: bool,
    failure_codes: Sequence[str] = (),
    evidence: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if score is not None and (not math.isfinite(score) or not 0.0 <= score <= 1.0):
        raise ValueError("evaluation score must be finite and between zero and one")
    return {
        "metric_version": metric_version(metric_id, "DETERMINISTIC"),
        "experiment_id": _first(
            trajectory, ("experiment_id",), "standalone-experiment"
        ),
        "target_id": _first(trajectory, ("target_id",), "unknown-target"),
        "trajectory_id": _first(
            trajectory, ("trajectory_id", "id"), "unknown-trajectory"
        ),
        "scenario_id": _scenario_id(trajectory),
        "metric_id": metric_id,
        "evaluator_type": "DETERMINISTIC",
        "status": status,
        "passed": status == PASS if status in (PASS, FAIL) else None,
        "score": score,
        "threshold": threshold,
        "hard_gate": hard_gate,
        "failure_codes": sorted(set(failure_codes)),
        "evidence": [dict(item) for item in evidence],
        "summary": "deterministic checks passed"
        if status == PASS
        else "deterministic checks did not pass",
        "rubric_sha256": None,
        "metadata": {},
    }


def _blocked(
    trajectory: Any, metric_id: str, failure_code: str, *, hard_gate: bool = True
) -> dict[str, Any]:
    return _metric_result(
        trajectory,
        metric_id=metric_id,
        status=BLOCKED_EVIDENCE,
        score=None,
        threshold=1.0,
        hard_gate=hard_gate,
        failure_codes=[failure_code],
    )


def _constraint_config(persona: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(persona, Mapping):
        result.update(persona)
    else:
        for key in (
            "agent_name",
            "required_markers",
            "must_include_all",
            "must_include_any",
            "forbidden_phrases",
            "must_not_include",
            "do_not_say",
            "forbidden_identity_names",
            "forbidden_role_claims",
            "max_questions_per_turn",
            "max_questions",
            "max_chars",
            "signature",
            "signature_required",
        ):
            value = _get(persona, key, _MISSING)
            if value is not _MISSING:
                result[key] = value
    nested = _get(persona, "hard_constraints", {})
    if isinstance(nested, Mapping):
        result.update(nested)
    return result


def _identity_claims(name: str) -> tuple[str, ...]:
    """Return narrow exact identity claims, not ordinary mentions of a name."""

    return (
        f"i am {name}",
        f"i'm {name}",
        f"my name is {name}",
        f"我是{name}",
        f"我叫{name}",
        f"我的名字是{name}",
    )


def _bounded_phrase_match(response: str, phrase: str) -> bool:
    """Match a complete phrase without treating a possessive as a claim.

    Plain substring matching makes ``I am Rowan's companion`` look like the
    forbidden identity claim ``I am Rowan``.  This small lexical boundary is
    intentionally deterministic; genuinely semantic cases remain judge work.
    """

    if not phrase:
        return False
    for match in re.finditer(re.escape(phrase), response):
        before = response[match.start() - 1 : match.start()]
        after = response[match.end() : match.end() + 2]
        if before and (before.isalnum() or before == "_"):
            continue
        if after[:1] and (after[:1].isalnum() or after[:1] == "_"):
            continue
        if after.startswith(("'s", "’s")):
            continue
        return True
    return False


_NEGATED_CLAIM_RE = re.compile(
    r"(?:\bnot\b|\bnever\b|\bno\b|\b(?:isn|aren|wasn|weren|don|doesn|didn)['’]?t\b|"
    r"\binstead\s+of\b|\brather\s+than\b|\bwrongly\b)"
    r"(?:\s+[\w'-]+){0,4}\s*$",
    re.IGNORECASE,
)


def _contains_unnegated_phrase(response: str, phrase: str) -> bool:
    """Return true only for a configured phrase used as an affirmative claim."""

    for match in re.finditer(re.escape(phrase), response):
        clause_start = max(
            response.rfind(mark, 0, match.start()) for mark in (".", "!", "?", ";", "\n")
        )
        prefix = response[clause_start + 1 : match.start()]
        if _NEGATED_CLAIM_RE.search(prefix):
            continue
        return True
    return False


def evaluate_persona_hard_constraints(
    trajectory: Any,
    persona: Any,
    *,
    metric_id: str = "persona.hard_constraints",
) -> dict[str, Any]:
    """Evaluate exact persona invariants without making semantic judgments.

    Supported constraints are ``required_markers``, ``forbidden_phrases`` (and
    ``do_not_say``), ``forbidden_identity_names``, ``max_questions_per_turn``,
    and an optional required ``signature``.  A nested ``hard_constraints``
    mapping overrides top-level persona values.
    """

    config = _constraint_config(persona)
    responses = _responses(trajectory)
    if not responses:
        return _blocked(trajectory, metric_id, "ASSISTANT_RESPONSE_MISSING")

    required = _strings(config.get("required_markers")) + _strings(
        config.get("must_include_all")
    )
    required_any = _strings(config.get("must_include_any"))
    forbidden = _strings(config.get("forbidden_phrases")) + _strings(
        config.get("must_not_include")
    )
    do_not_say = _strings(config.get("do_not_say"))
    forbidden_role_claims = _strings(config.get("forbidden_role_claims"))
    forbidden_names = _strings(config.get("forbidden_identity_names"))
    signatures = _strings(config.get("signature"))
    signature_required = config.get("signature_required") is True
    max_questions = _first(
        config, ("max_questions_per_turn", "max_questions"), None
    )
    if type(max_questions) is not int or max_questions < 0:
        max_questions = None
    max_chars = config.get("max_chars")
    if type(max_chars) is not int or max_chars < 1:
        max_chars = None

    configured_rule_count = (
        len(required)
        + (1 if required_any else 0)
        + len(forbidden)
        + len(do_not_say)
        + len(forbidden_role_claims)
        + len(forbidden_names)
        + (1 if max_questions is not None else 0)
        + (1 if max_chars is not None else 0)
        + (1 if signatures and signature_required else 0)
    )
    if configured_rule_count == 0:
        return _blocked(trajectory, metric_id, "PERSONA_CONSTRAINTS_MISSING")

    normalized_responses = [
        (index, turn_id, _normalized(response))
        for _turn, index, turn_id, response in responses
    ]
    combined = "\n".join(response for _index, _turn_id, response in normalized_responses)
    failures: list[str] = []
    evidence: list[dict[str, Any]] = []
    passed_rules = 0

    for position, marker in enumerate(required):
        present = _normalized(marker) in combined
        evidence.append(
            {
                "rule_id": f"required-marker:{position}",
                "observation": "PRESENT" if present else "MISSING",
                "value_digest": _digest(_normalized(marker)),
            }
        )
        if present:
            passed_rules += 1
        else:
            failures.append("PERSONA_REQUIRED_MARKER_MISSING")

    if required_any:
        matched = [
            position
            for position, marker in enumerate(required_any)
            if _normalized(marker) in combined
        ]
        evidence.append(
            {
                "rule_id": "required-marker-any",
                "observation": "PRESENT" if matched else "MISSING",
                "matched_alternatives": matched,
            }
        )
        if matched:
            passed_rules += 1
        else:
            failures.append("PERSONA_REQUIRED_MARKER_MISSING")

    for position, phrase in enumerate(forbidden):
        needle = _normalized(phrase)
        matched_turns = [
            turn_id
            for _index, turn_id, response in normalized_responses
            if needle in response
        ]
        evidence.append(
            {
                "rule_id": f"forbidden-phrase:{position}",
                "observation": "MATCHED" if matched_turns else "ABSENT",
                "turn_ids": matched_turns,
                "value_digest": _digest(needle),
            }
        )
        if matched_turns:
            failures.append("PERSONA_FORBIDDEN_PHRASE")
        else:
            passed_rules += 1

    for position, phrase in enumerate(do_not_say):
        needle = _normalized(phrase)
        matched_turns = [
            turn_id
            for _index, turn_id, response in normalized_responses
            if _bounded_phrase_match(response, needle)
        ]
        evidence.append(
            {
                "rule_id": f"do-not-say:{position}",
                "observation": "MATCHED" if matched_turns else "ABSENT",
                "turn_ids": matched_turns,
                "value_digest": _digest(needle),
            }
        )
        if matched_turns:
            failures.append("PERSONA_FORBIDDEN_PHRASE")
        else:
            passed_rules += 1

    for position, name in enumerate(forbidden_names):
        claims = tuple(_normalized(item) for item in _identity_claims(name))
        matched_turns = [
            turn_id
            for _index, turn_id, response in normalized_responses
            if any(_bounded_phrase_match(response, claim) for claim in claims)
        ]
        evidence.append(
            {
                "rule_id": f"forbidden-identity:{position}",
                "observation": "CLAIMED" if matched_turns else "ABSENT",
                "turn_ids": matched_turns,
                "value_digest": _digest(_normalized(name)),
            }
        )
        if matched_turns:
            failures.append("PERSONA_IDENTITY_DRIFT")
        else:
            passed_rules += 1

    for position, phrase in enumerate(forbidden_role_claims):
        needle = _normalized(phrase)
        matched_turns = [
            turn_id
            for _index, turn_id, response in normalized_responses
            if needle in response
        ]
        evidence.append(
            {
                "rule_id": f"forbidden-role-claim:{position}",
                "observation": "CLAIMED" if matched_turns else "ABSENT",
                "turn_ids": matched_turns,
                "value_digest": _digest(needle),
            }
        )
        if matched_turns:
            failures.append("PERSONA_ROLE_DRIFT")
        else:
            passed_rules += 1

    if max_questions is not None:
        over_limit = [
            {"turn_id": turn_id, "turn_index": index, "question_count": count}
            for index, turn_id, response in normalized_responses
            if (count := len(_QUESTION_MARKS_RE.findall(response))) > max_questions
        ]
        evidence.append(
            {
                "rule_id": "question-limit",
                "observation": "EXCEEDED" if over_limit else "WITHIN_LIMIT",
                "limit": max_questions,
                "turns": over_limit,
            }
        )
        if over_limit:
            failures.append("PERSONA_QUESTION_LIMIT_EXCEEDED")
        else:
            passed_rules += 1

    if max_chars is not None:
        over_limit = [
            {"turn_id": turn_id, "turn_index": index, "char_count": len(response)}
            for _turn, index, turn_id, response in responses
            if len(response) > max_chars
        ]
        evidence.append(
            {
                "rule_id": "character-limit",
                "observation": "EXCEEDED" if over_limit else "WITHIN_LIMIT",
                "limit": max_chars,
                "turns": over_limit,
            }
        )
        if over_limit:
            failures.append("PERSONA_CHARACTER_LIMIT_EXCEEDED")
        else:
            passed_rules += 1

    if signatures and signature_required:
        final_response = normalized_responses[-1][2]
        matched = any(_normalized(signature) in final_response for signature in signatures)
        evidence.append(
            {
                "rule_id": "required-signature",
                "observation": "PRESENT" if matched else "MISSING",
                "turn_ids": [normalized_responses[-1][1]],
            }
        )
        if matched:
            passed_rules += 1
        else:
            failures.append("PERSONA_SIGNATURE_MISSING")

    score = passed_rules / configured_rule_count
    return _metric_result(
        trajectory,
        metric_id=metric_id,
        status=PASS if not failures else FAIL,
        score=score,
        threshold=1.0,
        hard_gate=True,
        failure_codes=failures,
        evidence=evidence,
    )


def _fact_rows(facts: Any) -> list[Any]:
    if isinstance(facts, Mapping) or not isinstance(facts, Sequence):
        nested = _get(facts, "facts", _MISSING)
        if nested is _MISSING:
            nested = _get(_get(facts, "ground_truth", {}), "facts", [])
        return _items(nested)
    return _items(facts)


def _fact_id(fact: Any, position: int) -> str:
    value = _get(fact, "id")
    return value if isinstance(value, str) and value else f"fact-{position}"


def _keyword_groups(fact: Any) -> list[list[str]]:
    explicit = _items(_get(fact, "keyword_groups"))
    if explicit:
        groups = [_strings(group) for group in explicit]
        return [group for group in groups if group]
    # Existing fixtures use a flat list and expect every listed marker.
    return [[keyword] for keyword in _strings(_get(fact, "keywords"))]


def _scoped_responses(
    responses: Sequence[tuple[Any, int, str, str]], fact: Any
) -> list[tuple[Any, int, str, str]]:
    selected_ids = set(
        _strings(
            _first(fact, ("probe_turn_ids", "turn_ids", "response_turn_ids"), [])
        )
    )
    if not selected_ids:
        return list(responses)
    return [row for row in responses if row[2] in selected_ids]


def evaluate_memory_recall(
    trajectory: Any,
    facts: Any,
    *,
    metric_id: str = "memory.recall",
) -> dict[str, Any]:
    """Check configured fact markers in assistant responses.

    Each ``keyword_groups`` item is an OR-group, while every group is required.
    A legacy flat ``keywords`` list is treated as one required group per marker.
    ``min_keyword_matches`` may lower the required group count for a fact.
    """

    fact_rows = _fact_rows(facts)
    if not fact_rows:
        return _blocked(trajectory, metric_id, "MEMORY_EXPECTATION_MISSING")
    responses = _responses(trajectory)
    if not responses:
        return _blocked(trajectory, metric_id, "ASSISTANT_RESPONSE_MISSING")

    recalled = 0
    evidence: list[dict[str, Any]] = []
    failure_codes: list[str] = []
    evaluated = 0
    configuration_invalid = False
    for position, fact in enumerate(fact_rows, start=1):
        fact_id = _fact_id(fact, position)
        groups = _keyword_groups(fact)
        if not groups:
            evidence.append(
                {
                    "fact_id": fact_id,
                    "observation": "EXPECTATION_MISSING",
                    "matched_groups": [],
                    "required_group_count": 0,
                }
            )
            failure_codes.append("MEMORY_EXPECTATION_MISSING")
            configuration_invalid = True
            continue

        scoped = _scoped_responses(responses, fact)
        scoped_ids_configured = bool(
            _strings(
                _first(fact, ("probe_turn_ids", "turn_ids", "response_turn_ids"), [])
            )
        )
        if scoped_ids_configured and not scoped:
            evidence.append(
                {
                    "fact_id": fact_id,
                    "observation": "PROBE_MISSING",
                    "matched_groups": [],
                    "required_group_count": len(groups),
                    "turn_ids": [],
                }
            )
            failure_codes.append("MEMORY_PROBE_TURN_MISSING")
            configuration_invalid = True
            continue
        aggregation_mode = _get(fact, "aggregation_mode", "single_turn")
        if aggregation_mode not in {"single_turn", "across_turns"}:
            evidence.append(
                {
                    "fact_id": fact_id,
                    "observation": "EXPECTATION_MISSING",
                    "matched_groups": [],
                    "required_group_count": len(groups),
                    "turn_ids": [],
                }
            )
            failure_codes.append("MEMORY_EXPECTATION_MISSING")
            configuration_invalid = True
            continue
        candidates = (
            [("\n".join(_normalized(row[3]) for row in scoped), [row[2] for row in scoped])]
            if aggregation_mode == "across_turns"
            else [(_normalized(row[3]), [row[2]]) for row in scoped]
        )
        matched_groups: list[int] = []
        matched_turn_ids: list[str] = []
        for candidate, candidate_turn_ids in candidates:
            candidate_groups = [
                group_index
                for group_index, group in enumerate(groups)
                if any(_normalized(alternative) in candidate for alternative in group)
            ]
            if len(candidate_groups) > len(matched_groups):
                matched_groups = candidate_groups
                matched_turn_ids = candidate_turn_ids
        configured_minimum = _get(fact, "min_keyword_matches")
        required_count = (
            configured_minimum
            if type(configured_minimum) is int
            and 1 <= configured_minimum <= len(groups)
            else len(groups)
        )
        is_recalled = len(matched_groups) >= required_count
        evaluated += 1
        if is_recalled:
            recalled += 1
        else:
            failure_codes.append("MEMORY_RECALL_MISSING")
        evidence.append(
            {
                "fact_id": fact_id,
                "observation": "RECALLED" if is_recalled else "MISSING",
                "matched_groups": matched_groups,
                "required_group_count": required_count,
                "turn_ids": matched_turn_ids,
            }
        )

    if evaluated == 0:
        return _metric_result(
            trajectory,
            metric_id=metric_id,
            status=BLOCKED_EVIDENCE,
            score=None,
            threshold=1.0,
            hard_gate=True,
            failure_codes=failure_codes or ["MEMORY_EXPECTATION_MISSING"],
            evidence=evidence,
        )
    score = recalled / evaluated
    return _metric_result(
        trajectory,
        metric_id=metric_id,
        status=BLOCKED_EVIDENCE
        if configuration_invalid
        else (PASS if not failure_codes else FAIL),
        score=None if configuration_invalid else score,
        threshold=1.0,
        hard_gate=True,
        failure_codes=failure_codes,
        evidence=evidence,
    )


def _contradiction_rules(facts: Any) -> list[tuple[str, int, str]]:
    rules: list[tuple[str, int, str]] = []
    for position, fact in enumerate(_fact_rows(facts), start=1):
        fact_id = _fact_id(fact, position)
        phrases = _strings(
            _first(
                fact,
                ("contradictions", "contradiction_phrases", "forbidden_claims"),
                [],
            )
        )
        for phrase_index, phrase in enumerate(phrases):
            rules.append((fact_id, phrase_index, phrase))
    return rules


def evaluate_memory_contradictions(
    trajectory: Any,
    facts: Any,
    *,
    metric_id: str = "memory.contradiction",
) -> dict[str, Any]:
    """Fail when a configured, known-false memory claim is emitted."""

    rules = _contradiction_rules(facts)
    if not rules:
        return _blocked(trajectory, metric_id, "CONTRADICTION_RULES_MISSING")
    responses = _responses(trajectory)
    if not responses:
        return _blocked(trajectory, metric_id, "ASSISTANT_RESPONSE_MISSING")

    evidence: list[dict[str, Any]] = []
    matched_count = 0
    for fact_id, phrase_index, phrase in rules:
        needle = _normalized(phrase)
        matched_turns = [
            turn_id
            for _turn, _index, turn_id, response in responses
            if _contains_unnegated_phrase(_normalized(response), needle)
        ]
        if matched_turns:
            matched_count += 1
        evidence.append(
            {
                "fact_id": fact_id,
                "rule_id": f"contradiction:{phrase_index}",
                "observation": "MATCHED" if matched_turns else "ABSENT",
                "turn_ids": matched_turns,
                "value_digest": _digest(needle),
            }
        )

    score = (len(rules) - matched_count) / len(rules)
    return _metric_result(
        trajectory,
        metric_id=metric_id,
        status=PASS if matched_count == 0 else FAIL,
        score=score,
        threshold=1.0,
        hard_gate=True,
        failure_codes=[] if matched_count == 0 else ["MEMORY_CONTRADICTION"],
        evidence=evidence,
    )


def _canary_values(privacy: Any) -> list[str]:
    if isinstance(privacy, str) or isinstance(privacy, Sequence):
        return _strings(privacy)
    return _strings(
        _first(
            privacy,
            (
                "canaries",
                "forbidden_values",
                "forbidden_in_agent_identity_or_persona",
            ),
            [],
        )
    )


def evaluate_privacy_canaries(
    trajectory: Any,
    privacy: Any,
    *,
    metric_id: str = "privacy.canary",
) -> dict[str, Any]:
    """Fail closed if a configured private canary appears in model output."""

    canaries = _canary_values(privacy)
    if not canaries:
        return _blocked(trajectory, metric_id, "PRIVACY_CANARY_MISSING")
    responses = _responses(trajectory)
    if not responses:
        return _blocked(trajectory, metric_id, "ASSISTANT_RESPONSE_MISSING")
    scoped_turn_ids = set(
        _strings(_first(privacy, ("probe_turn_ids", "turn_ids"), []))
    )
    if scoped_turn_ids:
        responses = [row for row in responses if row[2] in scoped_turn_ids]
        if not responses:
            return _blocked(trajectory, metric_id, "PRIVACY_PROBE_TURN_MISSING")

    leaked = 0
    evidence: list[dict[str, Any]] = []
    for position, canary in enumerate(canaries):
        needle = _normalized(canary)
        matched_turns = [
            turn_id
            for _turn, _index, turn_id, response in responses
            if needle in _normalized(response)
        ]
        if matched_turns:
            leaked += 1
        evidence.append(
            {
                "rule_id": f"privacy-canary:{position}",
                "observation": "LEAKED" if matched_turns else "ABSENT",
                "turn_ids": matched_turns,
                "value_digest": _digest(needle),
            }
        )

    score = (len(canaries) - leaked) / len(canaries)
    return _metric_result(
        trajectory,
        metric_id=metric_id,
        status=PASS if leaked == 0 else FAIL,
        score=score,
        threshold=1.0,
        hard_gate=True,
        failure_codes=[] if leaked == 0 else ["PRIVACY_CANARY_LEAK"],
        evidence=evidence,
    )


def evaluate_deterministic_suite(
    trajectory: Any,
    *,
    persona: Any | None = None,
    facts: Any | None = None,
    privacy: Any | None = None,
    check_contradictions: bool = True,
) -> list[dict[str, Any]]:
    """Run every configured deterministic layer in a stable order."""

    results: list[dict[str, Any]] = []
    if persona is not None:
        results.append(evaluate_persona_hard_constraints(trajectory, persona))
    if facts is not None:
        results.append(evaluate_memory_recall(trajectory, facts))
        if check_contradictions and _contradiction_rules(facts):
            results.append(evaluate_memory_contradictions(trajectory, facts))
    if privacy is not None:
        results.append(evaluate_privacy_canaries(trajectory, privacy))
    return results
