"""T406: P0 的 chat/decrypt 闸对「非空兜底话术」不再失明。

背景(实测，不是推演):hosted hojimi 格连打 12 次，**首轮交付兜底话术 9/12=75%**，
而 P0 判 FAIL 只有 4/12=33%。差额来自两道闸只断言「回来了一条非空字符串」——
而失效时交付给用户的兜底话术恰好也非空。**闸的盲区正对着失效方向。**

同一形状在另一类 cell 上被独立撞到:vps-claude-code 的 CLI 拒答、consumer 发布
固定 fallback，runner 仍报整格 PASS。所以这是**闸**的问题，不是某个 provider 的。

下面每条用例对应一处修复;单独回退那一处，对应用例必须红。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from nacl.public import PrivateKey

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.e2e import client as e2e_client, p0
from tools.e2e.client import (
    E2EClient, TEST_API, VERDICT_FAIL, VERDICT_FALLBACK, VERDICT_OK,
    decrypt_verdict,
)


def _client() -> E2EClient:
    return E2EClient(
        TEST_API, "e2e-user", "e2e-key",
        PrivateKey.generate(), bytes(PrivateKey.generate().public_key),
    )


class _Resp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


def _with_activity(monkeypatch, body: dict | None, status: int = 200):
    c = _client()
    monkeypatch.setattr(
        c, "get",
        lambda path, **kw: _Resp(status, body if body is not None else {}),
        raising=False,
    )
    return c


# 一条真实的兜底话术，**从后端常量取**而不是抄进测试。抄进来的话，后端改文案
# 时这个测试仍然绿，而它本该守护的正是「文案改了闸还认得出来」。
def _a_fallback_text() -> str:
    assert e2e_client.FALLBACK_TEXTS, "派生集为空 —— 见 test_fallback_set_is_derived"
    return sorted(e2e_client.FALLBACK_TEXTS)[0]


# -- 主闸:后端自己的判词 ----------------------------------------------------

def test_hosted_failure_is_not_ok_even_though_text_is_non_empty(monkeypatch):
    """v2 失败轮:正文非空(兜底话术)，旧闸判 PASS，新闸必须判 fallback。"""
    # 形状取自实测现场 ~/.feedling-e2e-failures/usr_e98864fd44bf0c64
    activity = {
        "turn_id": "t", "runtime": "v2", "complete": True, "phase": "error",
        "jobs": [{"job_id": "10143", "status": "failed"}],
        "failure": {"code": "turn_failed:empty_reply", "job_id": "10143"},
    }
    c = _with_activity(monkeypatch, activity)
    # 正文刻意**不用**兜底话术:否则辅助闸也能抓到它，这条就同时被两道闸覆盖，
    # 撤掉主闸时它仍可能绿 —— 测不出它声称在测的东西。
    verdict, detail = c.classify_reply({"activity_turn_id": "t"}, "一句非空的正文")
    assert verdict == VERDICT_FALLBACK
    assert "turn_failed:empty_reply" in detail


def test_resident_failure_caught_although_complete_phase_and_job_all_say_done(monkeypatch):
    """v1(vps)失败轮:complete/phase/job **三个字段都说「成了」**。

    这一条是整个修法的支点。实测现场 usr_4809826f27b83de3(QA 的
    vps-claude-code P0 失败)长这样:

        runtime=v1  complete=True  phase=done  jobs=[(…, "completed")]
        failure={"code": "unknown", …}

    只有 `failure` 这一格说没成。**按 job 状态、complete 或 phase 设闸，对这一格
    完全无效** —— 所以主闸只能锚 failure 的存在性。
    """
    activity = {
        "turn_id": "t", "runtime": "v1", "complete": True, "phase": "done",
        "jobs": [{"job_id": "v1:t", "status": "completed"}],
        "failure": {"code": "unknown", "job_id": "v1:t"},
    }
    c = _with_activity(monkeypatch, activity)
    verdict, _ = c.classify_reply({"activity_turn_id": "t"}, "一句看起来正常的回复")
    assert verdict == VERDICT_FALLBACK, (
        "complete=True/phase=done/job=completed 都是绿的，"
        "唯一的失败信号是 failure —— 闸锚错字段就会漏掉整个 vps 格"
    )


def test_genuine_reply_is_ok(monkeypatch):
    """反例镜像:没有 failure、正文不是兜底话术 ⇒ 必须判 ok。

    没有这一格，一个「永远返回 fallback」的闸也能让上面两条全绿。
    """
    activity = {"turn_id": "t", "runtime": "v2", "complete": True,
                "phase": "done", "jobs": [{"job_id": "1", "status": "completed"}]}
    c = _with_activity(monkeypatch, activity)
    verdict, _ = c.classify_reply({"activity_turn_id": "t"}, "青色，你刚说的。")
    assert verdict == VERDICT_OK


def test_unreadable_turn_activity_is_not_green(monkeypatch):
    """读不到判词 = 无法观测。按 qa SOP「缺证据绝不静默通过」，判 fail。"""
    c = _with_activity(monkeypatch, {"error": "turn_activity_not_found"}, status=404)
    monkeypatch.setattr(e2e_client, "TURN_SETTLE_TIMEOUT", 0.0)
    verdict, detail = c.classify_reply({"activity_turn_id": "t"}, "任意正文")
    assert verdict == VERDICT_FAIL
    assert "unreadable" in detail


def test_missing_turn_id_fails_fast_without_polling(monkeypatch):
    """没有轮次 id 是**永久**条件 —— 必须立刻判，不能空等一个超时。

    (第一版就是这么写的:每个这样的格子白等 60 秒。反例镜像放在这里，
    免得以后有人把它「顺手」并回轮询分支。)
    """
    calls = []
    c = _client()
    monkeypatch.setattr(
        c, "get", lambda path, **kw: calls.append(path) or _Resp(200, {}),
        raising=False)
    monkeypatch.setattr(e2e_client, "TURN_SETTLE_TIMEOUT", 3600.0)
    verdict, detail = c.classify_reply({}, "任意正文")
    assert verdict == VERDICT_FAIL
    assert "activity_turn_id" in detail
    assert calls == [], "没有 id 就不该发出任何请求"


def test_reply_without_turn_id_is_not_green(monkeypatch):
    """连轮次 id 都没有 ⇒ 主闸无从问起，同样不许算绿。"""
    c = _with_activity(monkeypatch, {})
    verdict, detail = c.classify_reply({}, "任意正文")
    assert verdict == VERDICT_FAIL
    assert "activity_turn_id" in detail


def test_unsettled_turn_is_not_green(monkeypatch):
    """判词还没落定就判绿 = 同一个失效方向的时序版本。

    回复气泡可能先于 job 终态可见。此时 turn-activity 里还没有 `failure`，
    如果直接读就判绿，一个真失败的轮次会被记成通过 —— 和 T406 本身同形。
    """
    unsettled = {"turn_id": "t", "runtime": "v2", "complete": False,
                 "phase": "running", "jobs": [{"job_id": "1", "status": "running"}]}
    c = _with_activity(monkeypatch, unsettled)
    monkeypatch.setattr("tools.e2e.client.time.sleep", lambda _s: None)
    monkeypatch.setattr(e2e_client, "TURN_SETTLE_TIMEOUT", 0.0)
    verdict, detail = c.classify_reply({"activity_turn_id": "t"}, "看起来正常的回复")
    assert verdict == VERDICT_FAIL
    assert "did not settle" in detail


def test_settled_turn_is_read_after_it_completes(monkeypatch):
    """反例镜像:先 running 后 complete，闸必须等到落定的那一份判词。

    没有这一格，一个「永远判 not-settled」的实现也能让上一条绿。
    """
    bodies = [
        {"turn_id": "t", "complete": False, "phase": "running", "jobs": []},
        {"turn_id": "t", "complete": True, "phase": "error", "jobs": [],
         "failure": {"code": "turn_failed:empty_reply"}},
    ]
    c = _client()
    monkeypatch.setattr(
        c, "get", lambda path, **kw: _Resp(200, bodies.pop(0) if bodies else bodies),
        raising=False)
    monkeypatch.setattr("tools.e2e.client.time.sleep", lambda _s: None)
    verdict, detail = c.classify_reply({"activity_turn_id": "t"}, "看起来正常的回复")
    assert verdict == VERDICT_FALLBACK
    assert "turn_failed:empty_reply" in detail
    assert bodies == [], "应当一直轮询到落定为止"


# -- 辅助闸:兜底话术常量比对 -------------------------------------------------

def test_aux_gate_catches_degenerate_substitution(monkeypatch):
    """主闸的已知洞:worker 的 degenerate 替换会把正文换成兜底话术，
    而 job 仍是 completed ⇒ 不产 failure。辅助闸补这一格。"""
    activity = {"turn_id": "t", "runtime": "v2", "complete": True,
                "phase": "done", "jobs": [{"job_id": "1", "status": "completed"}]}
    c = _with_activity(monkeypatch, activity)
    verdict, detail = c.classify_reply({"activity_turn_id": "t"}, _a_fallback_text())
    assert verdict == VERDICT_FALLBACK
    assert "byte-equal" in detail


def test_fallback_set_is_derived_from_backend_not_hardcoded():
    """兜底话术必须从后端常量派生。

    写死在探针里的话，后端改一句文案这道闸就静默失效 —— 而失效方向朝
    「没失败」，和我们正在修的毛病同向。
    """
    from model_api_runtime.v2 import jobs_store, worker
    assert e2e_client.FALLBACK_TEXTS, "派生集为空 = 辅助闸失明，且失明后与正常同形"
    assert jobs_store._TERMINAL_FAILURE_FALLBACK_REPLY.strip() in e2e_client.FALLBACK_TEXTS
    assert worker._DEGENERATE_REPLY_FALLBACK.strip() in e2e_client.FALLBACK_TEXTS


def test_aux_coverage_is_reported_on_hit_not_only_on_miss(monkeypatch):
    """「命中与否都打印」必须两侧都成立。

    第一版只在 miss 分支拼 coverage,hit 分支提前 return —— 恰恰在闸开火的那一刻
    不报覆盖面。而我在交审信里已经声称做到了(codex4 2026-08-31 实测打红)。
    ⇒ 声称过的性质要有反例守着,否则那句话只是一句话。
    """
    monkeypatch.setitem(
        e2e_client.FALLBACK_SOURCE_ERRORS,
        "model_api_runtime.v2.worker", "ImportError: boom")
    hit_verdict, hit_detail = decrypt_verdict(_a_fallback_text(), "")
    miss_verdict, miss_detail = decrypt_verdict("青色。", "")
    assert hit_verdict == VERDICT_FALLBACK and miss_verdict == VERDICT_OK
    for detail in (hit_detail, miss_detail):
        assert "aux gate checked" in detail
        assert "INCOMPLETE" in detail


def test_main_gate_fallback_detail_also_carries_coverage(monkeypatch):
    """主闸判 fallback 时同样要带辅助闸覆盖面 —— 同一条约束,别只修一处。"""
    activity = {"turn_id": "t", "runtime": "v2", "complete": True,
                "phase": "error", "jobs": [],
                "failure": {"code": "turn_failed:empty_reply"}}
    c = _with_activity(monkeypatch, activity)
    _, detail = c.classify_reply({"activity_turn_id": "t"}, "一句非空的正文")
    assert "aux gate checked" in detail


def test_empty_derived_set_reports_unavailable_not_green(monkeypatch):
    """派生集被清空时必须**大声**，不能表现得像通过。

    这是「派生掉表也派生掉了锚」那个坑:如果派生失败只是让集合变空，
    所有 `in FALLBACK_TEXTS` 判断都为假，闸会安静地一路绿灯。
    """
    monkeypatch.setattr(e2e_client, "FALLBACK_TEXTS", frozenset())
    verdict, detail = decrypt_verdict("任意明文", "")
    assert verdict == VERDICT_FAIL
    assert "UNAVAILABLE" in detail


# -- decrypt 闸 --------------------------------------------------------------

def test_decrypt_rejects_fallback_plaintext():
    verdict, _ = decrypt_verdict(_a_fallback_text(), "")
    assert verdict == VERDICT_FALLBACK


def test_decrypt_accepts_real_plaintext():
    verdict, detail = decrypt_verdict("青色。", "")
    assert verdict == VERDICT_OK
    assert "len=" in detail


def test_decrypt_empty_is_fail():
    verdict, detail = decrypt_verdict("", "BadSignatureError: boom")
    assert verdict == VERDICT_FAIL
    assert "BadSignatureError" in detail


# -- 报表与阻断 --------------------------------------------------------------

def test_fallback_blocks_release():
    """fallback 阻断发版:用户实际收到的是失败话术，那不是一次成功的发布。"""
    assert p0.p0_blocks_release([{"result": VERDICT_FALLBACK}]) is True
    assert p0.p0_blocks_release([{"result": "ok"}]) is False


def test_report_detail_never_claims_all_green_on_a_fallback_cell():
    """标签必须从数据派生。一个交付了失败话术的格子，detail 列不许写
    「all steps green」—— 那句话在这里是假的。"""
    steps = [("setup", "ok", ""),
             ("chat", VERDICT_FALLBACK, "backend reports turn failure: code='x'")]
    detail = p0.cell_detail(steps)
    assert "all steps green" not in detail
    assert "turn failure" in detail


def test_report_detail_still_says_green_when_everything_is_green():
    steps = [("setup", "ok", ""), ("chat", "ok", "3s")]
    assert p0.cell_detail(steps) == "all steps green"


def test_fallback_icon_exists():
    """新状态要有自己的词。没有词，它就会被别的状态冒名。"""
    assert p0._ICON.get(VERDICT_FALLBACK) not in (None, p0._ICON["ok"])


# -- 调用点覆盖 --------------------------------------------------------------
# 上面的用例测的都是 helper。只测 helper 的话，谁把 hosted.py 的闸改回
# 「非空即通过」，这一整个文件仍然全绿 —— 守卫的作用域就是它的盲区。
# 所以这里断言**每一个 P0 cell runner** 都真的用了新闸。
# 模块清单从代码派生(扫 `run_*_cell`)，新增一个 cell runner 会自动进来，
# 不会因为有人忘了改测试而漏掉。

def _cell_runner_sources() -> dict[str, str]:
    """扫出「跑一个 cell **并且**断言聊天回复」的模块。

    判据取两条:`run_*_cell` 是 P0 的 cell 入口，`wait_reply(` 才说明它真的
    在等一条聊天回复。只按前者筛会把 processing_probe(记忆管线，不判回复)
    也圈进来 —— 那是把比事实更宽的代理当成了判据。
    两条都从代码派生，不写豁免名单:豁免名单会漂，而漂的方向是悄悄放行。
    """
    import re as _re
    e2e_dir = Path(__file__).parent.parent / "tools" / "e2e"
    found = {}
    for path in sorted(e2e_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if _re.search(r"^def run_\w+_cell\(", source, _re.M) and "wait_reply(" in source:
            found[path.name] = source
    return found


def test_every_p0_cell_runner_is_discovered():
    """派生集非空断言。派生不到任何 runner 时，下面两条会**空转全绿**。"""
    runners = _cell_runner_sources()
    assert set(runners) == {"hosted.py", "vps.py"}, (
        f"判聊天回复的 P0 cell runner 派生结果异常: {sorted(runners)} —— "
        "多出来的要么该受这条约束，要么说明判据选错了；少了的就是失守")


def _called_names(source: str) -> set[str]:
    """`run_*_cell` **函数体内**真正被调用的名字。

    这个守卫被打红过两次,两次都是「作用域比事实宽」:

    1. 第一版用字符串包含 —— hosted.py 的注释里写着 "见 client.classify_reply",
       那句**注释**就能满足它,把调用点换回旧闸照样绿。
    2. 第二版改扫 AST,但对**整个模块** walk —— codex4 2026-08-31 的突变:
       把 runner 里的 `c.classify_reply(...)` 删掉,在模块末尾放一个从不被调用的
       `_dead_gate` 去调它 ⇒ 三条守卫全绿,而产品已经退回旧闸。

    ⇒ 现在只看 `run_*_cell` 的函数体。⚠️ 残余限制如实写在这里:它证明的是
    「调用出现在 runner 自己的函数体(含其内嵌 helper)里」，**不是完整可达性**
    —— 若有人把调用埋进一个从不被调用的内嵌函数,这道守卫仍会绿。
    真正的可达性要靠行为测试,本条不冒充它。
    """
    import ast
    import re as _re
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _re.fullmatch(r"run_\w+_cell", node.name):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


# codex4 的突变,固化成常驻反例:runner 里没有闸,模块末尾有个从不被调用的死函数。
# 守卫必须报「缺」。这一格红了,说明守卫的作用域又变宽了。
_DEAD_CALL_SOURCE = '''
def run_fake_cell(cell, pool):
    chat_verdict, chat_detail = ("ok", "")
    dec_verdict, dec_detail = ("ok", "")
    return {"result": "ok"}


def _dead_gate(c, reply, text, dec, dec_err):
    c.classify_reply(reply, text)
    return _decrypt_verdict(dec, dec_err)
'''


def test_guard_rejects_a_dead_call_outside_the_runner():
    """反例镜像:闸只出现在一个从不被调用的死函数里 ⇒ 守卫必须判「缺」。

    没有这一格,守卫的作用域悄悄变宽时不会有人知道 —— 它变宽的方向正是「放行」。
    """
    called = _called_names(_DEAD_CALL_SOURCE)
    assert "classify_reply" not in called, (
        "守卫又开始按模块扫了 —— 死函数里的调用不算调用点")
    assert not (called & {"decrypt_verdict", "_decrypt_verdict"})


def test_guard_accepts_a_call_inside_the_runner():
    """正例镜像:同样的调用放进 runner 体内 ⇒ 守卫必须认。

    只有上面那条负例的话,一个恒返回空集的实现也能全绿。
    """
    live = _DEAD_CALL_SOURCE.replace(
        '    chat_verdict, chat_detail = ("ok", "")',
        '    chat_verdict, chat_detail = c.classify_reply(reply, text)',
    ).replace(
        '    dec_verdict, dec_detail = ("ok", "")',
        '    dec_verdict, dec_detail = _decrypt_verdict(dec, dec_err)',
    )
    called = _called_names(live)
    assert "classify_reply" in called
    assert "_decrypt_verdict" in called


def test_every_cell_runner_uses_the_semantic_gate():
    for name, source in _cell_runner_sources().items():
        called = _called_names(source)
        assert "classify_reply" in called, (
            f"{name} 没有**调用**语义闸 —— 它的 chat 步会退回「非空即通过」")
        # decrypt 步在两个 runner 里都以 `_decrypt_verdict` 别名导入。
        assert called & {"decrypt_verdict", "_decrypt_verdict"}, (
            f"{name} 的 decrypt 步没有走三态判词")


def test_no_cell_runner_still_gates_on_mere_non_emptiness():
    """旧形状的字面守卫:`bool(text.strip())` 作为 chat 步的**唯一**判据。"""
    for name, source in _cell_runner_sources().items():
        assert 'step("chat", reply is not None and bool(text.strip())' not in source, (
            f"{name} 仍在用「回来了一条非空字符串」当 chat 闸 —— 这正是 T406 的缺陷")


# -- triage 库新鲜度自检 -----------------------------------------------------

class _FakeCursor:
    def __init__(self, rows_by_sql: dict):
        self._rows_by_sql = rows_by_sql
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self._rows = []
        for needle, rows in self._rows_by_sql.items():
            if needle in sql:
                self._rows = rows
                return

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows_by_sql: dict):
        self._rows_by_sql = rows_by_sql

    def cursor(self, row_factory=None):
        return _FakeCursor(self._rows_by_sql)


def test_triage_not_found_message_carries_db_freshness():
    """「找不到这个用户」与「连错库了」在旧输出里同形，且失明方向偏向
    「证据没了」—— 最容易让人直接放弃追查。新输出必须把库的新鲜度
    贴在同一句话里，让读的人能分辨这两件事。"""
    import tools.v2_user_triage as triage

    conn = _FakeConn({"FROM users WHERE user_id": []})
    freshness = {"users_total": 75, "users_newest": "2026-08-18",
                 "jobs_max_id": 6229, "jobs_newest": "2026-08-18"}
    with pytest.raises(SystemExit) as excinfo:
        triage.section_runtime(conn, "usr_deadbeef", freshness)
    message = str(excinfo.value)
    assert "6229" in message, "没有 max(agent_jobs.id) 就无法判断是不是连错库"
    assert "两种可能" in message


def test_triage_freshness_line_reports_membership_not_just_a_verdict():
    import tools.v2_user_triage as triage

    line = triage._freshness_line(
        {"users_total": 75, "users_newest": "2026-08-18",
         "jobs_max_id": 6229, "jobs_newest": "2026-08-18"})
    for token in ("users=75", "max(agent_jobs.id)=6229", "2026-08-18"):
        assert token in line
