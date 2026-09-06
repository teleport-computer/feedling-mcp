from __future__ import annotations

import ast
import os
from pathlib import Path
import re

import pytest

from agent_runtime import spawners
from core import tool_markup_leak
from model_api_runtime.v2 import worker
from notices import catalog, error_contract, rejection_stats

for key, value in {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_t255_checkpoint.json",
}.items():
    os.environ.setdefault(key, value)

import tools.chat_resident_consumer as resident  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]

# Independent production evidence. Deliberately do not derive this tuple from
# MODEL_SENTINEL_TOKENS: removing a production token must leave the wire sample
# behind and turn this test red.
OBSERVED_WIRE_SENTINELS = ("</s>", "<end_of_turn>")


def _loaders():
    return dict(error_contract._DEFAULT_SOURCE_LOADERS)


def test_registry_export_normal_shape_is_importable():
    export = catalog.registry_export()
    assert error_contract.REGISTRY_STATUS_VALUES == {
        "ok", "partial", "unavailable"
    }
    assert isinstance(export, error_contract.RegistryExport)
    assert export.status == "ok"
    assert export.values
    assert export.unavailable_sources == ()


def test_registry_export_partial_names_exact_failed_source():
    loaders = _loaders()
    loaders["vision"] = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    export = catalog.registry_export(loaders)
    assert export.status == "partial"
    assert export.values
    assert export.unavailable_sources == ("vision",)


def test_registry_export_all_failed_is_none_not_empty_set():
    def unavailable():
        raise RuntimeError("down")

    export = catalog.registry_export({
        name: unavailable for name in error_contract.REGISTRY_SOURCE_NAMES
    })
    assert export.status == "unavailable"
    assert export.values is None
    assert export.unavailable_sources == tuple(
        sorted(error_contract.REGISTRY_SOURCE_NAMES)
    )


def test_failed_registry_source_is_retried_not_cached_as_success():
    loaders = _loaders()
    original = loaders["vision"]
    calls = 0

    def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first load failed")
        return original()

    loaders["vision"] = flaky
    assert catalog.registry_export(loaders).status == "partial"
    assert catalog.registry_export(loaders).status == "ok"
    assert calls == 2


def test_registry_views_are_derived_from_error_specs():
    public = error_contract.public_specs()
    assert catalog.ERROR_CLASSES == frozenset(spec.code for spec in public)
    assert catalog._CATALOG == {
        spec.code: (spec.blame, spec.safe_text_zh) for spec in public
    }
    assert resident.CONSUMER_ERROR_CLASSES == frozenset(
        spec.code for spec in error_contract.consumer_specs()
    )
    assert resident._ERROR_CLASS_RULES == tuple(
        (spec.code, spec.blame, spec.safe_text_zh, spec.matcher())
        for spec in error_contract.matcher_specs()
    )


def test_hosted_request_validation_codes_do_not_enter_resident_classifier():
    request_codes = {
        spec.code
        for spec in error_contract.public_specs()
        if spec.family == "request"
    }
    assert request_codes == {
        "image_count_exceeds_limit",
        "image_list_empty",
        "image_payload_conflict",
    }
    assert request_codes.isdisjoint(resident.CONSUMER_ERROR_CLASSES)


def test_dynamic_unknown_maps_to_registered_fallback_and_content_free_report():
    reports = []
    spec = error_contract.resolve_untrusted(
        "secret-provider-body-do-not-retain",
        domain="vision",
        boundary="v2_dedicated_vision",
        reporter=lambda *row: reports.append(row),
    )
    assert spec.code == error_contract.UNREGISTERED_ERROR_CLASS
    assert reports == [(
        "vision", "v2_dedicated_vision", error_contract.UNREGISTERED_ERROR_CLASS
    )]
    assert "secret-provider" not in repr(reports)


def test_registered_dynamic_value_does_not_increment_rejection_report():
    reports = []
    spec = error_contract.resolve_untrusted(
        "vision_model_unavailable",
        domain="vision",
        boundary="v2_dedicated_vision",
        reporter=lambda *row: reports.append(row),
    )
    assert spec.code == "vision_model_unavailable"
    assert reports == []


def _resolve_untrusted_callsites_in_source(path: Path, source: str):
    rows = []
    dynamic = []
    tree = ast.parse(source, filename=str(path))
    direct_aliases = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "notices.error_contract"
        for alias in node.names
        if alias.name == "resolve_untrusted"
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_target = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "resolve_untrusted"
        ) or (
            isinstance(node.func, ast.Name)
            and node.func.id in direct_aliases
        )
        if not is_target:
            continue
        keywords = {
            keyword.arg: keyword.value
            for keyword in node.keywords
            if keyword.arg
        }
        domain = keywords.get("domain")
        boundary = keywords.get("boundary")
        if not (
            isinstance(domain, ast.Constant)
            and isinstance(domain.value, str)
            and isinstance(boundary, ast.Constant)
            and isinstance(boundary.value, str)
        ):
            dynamic.append(f"{path}:{node.lineno}")
            continue
        rows.append((domain.value, boundary.value))
    return rows, dynamic


def _resolve_untrusted_callsites():
    rows = []
    dynamic = []
    for source_root in (ROOT / "backend", ROOT / "tools"):
        for path in source_root.rglob("*.py"):
            found, found_dynamic = _resolve_untrusted_callsites_in_source(
                path, path.read_text()
            )
            rows.extend(found)
            dynamic.extend(found_dynamic)
    return rows, dynamic


def test_rejection_dimensions_are_closed_over_real_callsites_and_fit_wire_cap():
    callsites, dynamic_callsites = _resolve_untrusted_callsites()
    registered = {
        (domain, boundary)
        for boundary, domain in error_contract.REJECTION_BOUNDARY_DOMAINS.items()
    }
    assert dynamic_callsites == []
    assert set(callsites) == registered
    assert len(callsites) == len(registered)

    max_triples = len(registered) * len(
        error_contract.REJECTION_FALLBACK_CODES
    )
    assert max_triples < rejection_stats.MAX_REPORT_ROWS


def test_dynamic_boundary_mutation_turns_closed_callsite_guard_red():
    path = ROOT / "tools/chat_resident_consumer.py"
    source = path.read_text()
    old = 'boundary="resident_vision_response"'
    assert source.count(old) == 1
    mutated = source.replace(old, "boundary=runtime_boundary")
    _, dynamic_callsites = _resolve_untrusted_callsites_in_source(path, mutated)
    assert dynamic_callsites


@pytest.mark.parametrize(
    ("domain", "boundary", "fallback"),
    (
        ("vision", "unregistered_but_well_shaped", "error_class_unregistered"),
        ("vision", "v2_image_generation", "error_class_unregistered"),
        ("vision", "v2_dedicated_vision", "unknown"),
    ),
)
def test_rejection_dimensions_reject_open_or_mismatched_values(
    domain, boundary, fallback
):
    reporter = rejection_stats.ResidentRejectionReporter(
        writer_id="resident:test:closed", release_sha="abc1234"
    )
    with pytest.raises(ValueError, match="contract rejection"):
        reporter.record(domain, boundary, fallback)


def test_rejection_contract_mistake_is_loud_but_transport_failure_is_best_effort():
    with pytest.raises(ValueError, match="contract rejection boundary"):
        error_contract.resolve_untrusted(
            "unregistered-runtime-value",
            domain="vision",
            boundary="unregistered_but_well_shaped",
            reporter=lambda *_: None,
        )

    def failed_transport(*_):
        raise RuntimeError("injected reporting transport failure")

    spec = error_contract.resolve_untrusted(
        "unregistered-runtime-value",
        domain="vision",
        boundary="v2_dedicated_vision",
        reporter=failed_transport,
    )
    assert spec.code == error_contract.UNREGISTERED_ERROR_CLASS


def test_resident_absolute_rejection_report_replays_idempotently():
    reporter = rejection_stats.ResidentRejectionReporter(
        writer_id="resident:test:abc", release_sha="abc1234"
    )
    reporter.record("vision", "resident_vision_response", "error_class_unregistered")
    first = rejection_stats.parse_resident_header(reporter.header_value())
    replay = rejection_stats.parse_resident_header(reporter.header_value())
    assert first == replay
    assert first[0][5] == 1
    reporter.record("vision", "resident_vision_response", "error_class_unregistered")
    assert rejection_stats.parse_resident_header(reporter.header_value())[0][5] == 2


@pytest.mark.parametrize("wire_value", OBSERVED_WIRE_SENTINELS)
def test_independent_observed_wire_sentinel_is_suppressed(wire_value):
    assert tool_markup_leak.is_degenerate_visible_text(wire_value) is True


def test_v1_v2_share_exact_degenerate_predicate_and_corpus():
    assert resident._is_degenerate_reply is tool_markup_leak.is_degenerate_visible_text
    assert worker._is_degenerate_reply is tool_markup_leak.is_degenerate_visible_text
    corpus = {
        "</s>": True,
        "<end_of_turn>": True,
        ".": True,
        "。": True,
        "hello </s>": False,
        "讨论 <end_of_turn> token": False,
        "<s>删除线</s>": False,
    }
    assert {text: resident._is_degenerate_reply(text) for text in corpus} == corpus
    assert {text: worker._is_degenerate_reply(text) for text in corpus} == corpus


def _runtime_entry_paths() -> set[Path]:
    """Derive scan roots from real deploy commands and the real spawner."""
    entries = {Path(spawners._RESIDENT_CONSUMER).resolve()}
    service = (ROOT / "deploy/feedling-chat-resident.service").read_text()
    match = re.search(r"^ExecStart=.*?python\s+([^\s]+\.py)$", service, re.M)
    assert match
    entries.add((ROOT / match.group(1)).resolve())
    compose_files = set(ROOT.glob("deploy/docker-compose*.yaml")) | set(
        ROOT.glob("deploy/docker-compose*.yml")
    )
    for compose in compose_files:
        for script in re.findall(
            r'command:\s*(?:&[\w-]+\s+)?\["python",\s*"-u",\s*"([^"]+\.py)"\]',
            compose.read_text(),
        ):
            entries.add((ROOT / script).resolve())
    assert ROOT / "tools/chat_resident_consumer.py" in entries
    assert ROOT / "backend/model_api_runtime/v2/serve_worker.py" in entries
    assert ROOT / "backend/enclave_app.py" in entries
    return entries


def _anti_bypass_violations(path: Path, source: str | None = None) -> list[str]:
    source_text = source if source is not None else path.read_text()
    tree = ast.parse(source_text)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_is_degenerate_reply":
            violations.append(f"{path.name}:{node.lineno}:local degenerate predicate")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "AgentErrorNotice":
                owner = next(
                    (
                        parent
                        for parent in ast.walk(tree)
                        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node in ast.walk(parent)
                    ),
                    None,
                )
                if owner is None or owner.name != "_notice_for_code":
                    violations.append(f"{path.name}:{node.lineno}:direct notice constructor")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {
                target.id for target in targets if isinstance(target, ast.Name)
            }
            protected = names & {
                "ERROR_CLASSES", "_CATALOG", "_UPSTREAM_RULES",
                "CONSUMER_ERROR_CLASSES", "_ERROR_CLASS_RULES",
                "IMAGE_GENERATION_RESULT_CODES",
            }
            value = node.value
            if (
                protected
                and "error_contract" in source_text
                and "error_contract" not in ast.dump(value)
            ):
                violations.append(
                    f"{path.name}:{node.lineno}:handwritten registry view"
                )
    return violations


def _resolve_local_module(module: str, member: str = "") -> set[Path]:
    relative = Path(*module.split(".")) if module else Path()
    found = set()
    for base in (ROOT, ROOT / "backend", ROOT / "tools"):
        candidates = [base / relative.with_suffix(".py"), base / relative / "__init__.py"]
        if member:
            candidates.insert(0, base / relative / f"{member}.py")
        found.update(
            candidate.resolve()
            for candidate in candidates
            if candidate.is_file()
        )
    return found


def _runtime_import_closure(entries: set[Path]) -> set[Path]:
    pending = list(entries)
    seen = set()
    while pending:
        path = pending.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        tree = ast.parse(path.read_text())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports |= _resolve_local_module(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imports |= _resolve_local_module(node.module or "")
                for alias in node.names:
                    imports |= _resolve_local_module(node.module or "", alias.name)
        pending.extend(imports - seen)
    return seen


def test_deployed_runtime_entries_have_no_registry_or_sentinel_bypass():
    entries = _runtime_entry_paths()
    sources = _runtime_import_closure(entries)
    assert ROOT / "tools/chat_resident_consumer.py" in sources
    assert ROOT / "backend/model_api_runtime/v2/worker.py" in sources
    violations = [
        violation
        for path in sources
        for violation in _anti_bypass_violations(path)
    ]
    assert violations == []


def test_real_tools_callsite_mutation_turns_anti_bypass_red():
    path = ROOT / "tools/chat_resident_consumer.py"
    source = path.read_text()
    old = 'return _notice_for_code("unknown", detail)'
    assert source.count(old) == 1
    mutated = source.replace(
        old,
        'return AgentErrorNotice("unknown", "system", "x", detail)',
    )
    assert any("direct notice constructor" in item for item in _anti_bypass_violations(path, mutated))


def test_restoring_real_tools_handwritten_view_turns_anti_bypass_red():
    path = ROOT / "tools/chat_resident_consumer.py"
    source = path.read_text()
    old = """_ERROR_CLASS_RULES = tuple(
    (spec.code, spec.blame, spec.safe_text_zh, spec.matcher())
    for spec in _error_contract.matcher_specs()
)"""
    assert source.count(old) == 1
    mutated = source.replace(
        old,
        '_ERROR_CLASS_RULES = (("unknown", "system", "x", re.compile("x")),)',
    )
    assert any(
        "handwritten registry view" in item
        for item in _anti_bypass_violations(path, mutated)
    )


# --- T497:上游「通用 403 体」不再被判成 auth_invalid ------------------------
#
# 2026-09-06 线上:一个用户整晚看到「API Key 无效或已过期,请到设置里重新保存。」
# 这条文案会把人**引向**去重存 key,而 key 从来不是问题所在 —— 库里唯一那条
# credential 自 7-14 起就没更新过、route 自检返 ok,失败却是间歇的。真相是中转站
# 因自己的原因回了 403 + 一个没有任何鉴权语义的 OpenAI 风格空壳,而 auth_invalid
# 的裸 40[13] 分支把它先抓走了。
#(用户到底有没有真去重存/换过 key,T497 里是 UNMEASURED —— 别把它当事实写。)

_T497_GENERIC_403 = (
    "feedling:empty_provider_reply: pi agent produced no reply: 403: "
    '{"message":"Request failed. Please try again later.","type":"api_error",'
    '"param":"","code":null}'
)
# 同一份通用体,只把 message 换成明确的鉴权词:负向先行断言绝不能把真鉴权错误
# 偷到 upstream_unavailable 去。用「同一份体 + 叠加鉴权词」而不是另起一条
# "403 Unauthorized",否则测的是另一个形状,证不了这件事。
_T497_GENERIC_403_WITH_AUTH_WORDS = _T497_GENERIC_403.replace(
    "Request failed. Please try again later.", "Unauthorized: invalid api key"
)
# 下面三条是 r9 复审实跑出来的反例,第一版判据(逐个 403 occurrence + 只看
# type=api_error)全部漏掉。它们是这条规则真正的边界,单独列出来别被合并掉。
_T497_API_ERROR_BUT_NOT_GENERIC = (
    '403: {"message":"Account suspended by provider","type":"api_error"}'
)
_T497_GENERIC_403_REPEATED_STATUS = (
    'HTTP 403: {"message":"Request failed. Please try again later.",'
    '"type":"api_error","code":"403"}'
)
_T497_GENERIC_403_STATUS_LAST = (
    '{"type":"api_error","message":"Request failed. Please try again later."}'
    "; status 403"
)
# 那句话裸着出现在外层日志里,而 body 的 message 是别的 ⇒ 不是通用体。
# 判据必须钉在 JSON 的 `message` 字段上,不是文本里任意一处。
_T497_PHRASE_IN_LOG_BUT_OTHER_BODY = (
    "HTTP 403: note Request failed. Please try again later. "
    'body={"message":"something else","type":"api_error"}'
)
# 字段冒号两侧带空白仍应识别(容忍空白,不容忍换字段)。
_T497_GENERIC_403_SPACED = (
    '403 {"message" : "Request failed. Please try again later." , '
    '"type" : "api_error"}'
)


@pytest.mark.parametrize(
    "text, expected",
    [
        (_T497_GENERIC_403, "upstream_unavailable"),
        (_T497_GENERIC_403_WITH_AUTH_WORDS, "auth_invalid"),
        ("cli agent exited 1: unexpected status 401 Unauthorized", "auth_invalid"),
        ("provider_http_403: forbidden", "auth_invalid"),
        # 只有 api_error 信封、没有那条精确的通用 message ⇒ 规则不得放行
        (_T497_API_ERROR_BUT_NOT_GENERIC, "auth_invalid"),
        # 体里再出现一次 403,不得让判据被绕开
        (_T497_GENERIC_403_REPEATED_STATUS, "upstream_unavailable"),
        # 状态码写在体后面,顺序不得影响判据
        (_T497_GENERIC_403_STATUS_LAST, "upstream_unavailable"),
        # 短语只在外层日志、body message 不同 ⇒ 不得放行
        (_T497_PHRASE_IN_LOG_BUT_OTHER_BODY, "auth_invalid"),
        # 字段间空白不影响识别
        (_T497_GENERIC_403_SPACED, "upstream_unavailable"),
    ],
    ids=[
        "generic_403_body",
        "generic_403_plus_auth_words",
        "bare_401",
        "provider_http_403",
        "api_error_body_without_generic_message",
        "generic_403_with_repeated_status",
        "generic_403_with_status_last",
        "phrase_in_log_but_other_body_message",
        "generic_403_with_spaced_fields",
    ],
)
def test_t497_generic_upstream_403_is_not_auth_invalid(text, expected):
    spec = error_contract.classify_text(text)
    assert spec is not None, f"no spec matched: {text!r}"
    assert spec.code == expected


def test_t497_rule_requires_the_api_error_envelope_not_just_the_message():
    """三项条件缺一不可:少了 envelope 同样不改类。"""
    without_envelope = '403: {"message":"Request failed. Please try again later."}'
    assert error_contract.classify_text(without_envelope).code == "auth_invalid"


def test_t497_auth_words_still_win_even_on_a_genuine_generic_body():
    """整条就是通用体、但另外带了鉴权词 ⇒ 仍归 auth_invalid。

    安全性质来自 auth_invalid 其余分支与裸 403 分支是并列项:负向断言只掐掉
    裸 403 这一支,掐不掉 unauthorized / invalid api key 那几支。
    """
    text = _T497_GENERIC_403 + " unauthorized"
    assert error_contract.classify_text(text).code == "auth_invalid"


def test_t497_positive_and_negative_forms_share_one_shape():
    """正向式与 auth_invalid 的负向先行断言必须同源。

    两处各写一遍正则,早晚会各改各的 —— 那时 403 会同时不被两条规则认领(或被
    两条都认领),而单测若各自写死字面量是看不出来的。所以这里从被测模块**派生**。
    """
    shape = error_contract._GENERIC_UPSTREAM_403_SHAPE
    assert error_contract._GENERIC_UPSTREAM_403 == r"\A" + shape
    assert error_contract._AUTH_403 == r"\A(?!" + shape + r")[\s\S]*?\b403\b"
    assert error_contract._GENERIC_UPSTREAM_403_MESSAGE in shape
    specs = {spec.code: spec for spec in error_contract.all_specs()}
    assert error_contract._AUTH_403 in specs["auth_invalid"].matcher_pattern
    assert (
        error_contract._GENERIC_UPSTREAM_403
        in specs["upstream_unavailable"].matcher_pattern
    )


def test_t497_neighbouring_classes_are_untouched():
    """本次收窄只动 403 一格,相邻的状态码分类不得漂。"""
    for text, expected in (
        ("provider_http_502: bad gateway", "upstream_unavailable"),
        ("exceeded retry limit, last status: 429 Too Many Requests", "rate_limited"),
        ("cli agent exited 1: unexpected status 403: 额度不足", "quota_insufficient"),
    ):
        assert error_contract.classify_text(text).code == expected, text
