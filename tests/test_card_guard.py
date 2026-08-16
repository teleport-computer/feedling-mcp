"""记忆卡「模型原始输出泄漏」污染检测 —— 回归锁。

现场(2026-07-28):一张记忆卡的 bucket 字段被填成模型原始输出残片——
一段 harmony 通道元标记(`analysis to=functions.memory_write`)+ provider 报错回显
(`output error code: 400`)+ 撕裂协议 JSON 尾巴(`…relationship"}]}`),而 title/content
是干净的。整卡 JSON 合法(所以 `fix/json-tail-leak` 的 payload 级拒绝拦不住),只是
单个字段的值脏了。card_text.py 的占位符检测也拦不住(这些有实义字符)。

card_guard 是纯检测器:回答「这段字段值是不是协议/harmony/报错泄漏」,复用
core/protocol_leak 的通用证据原语,不含 I/O、不含硬/软路由(那在 card_text)。

三件事锁死:①各类泄漏必须被判脏;②合法记忆(讨论报错/贴代码/snake_case 桶/正常中文)
绝不误杀;③桶的机器 taxonomy 只靠精确 denylist + 强证据组合,不靠形状
(`long_term_preference_or_event_v1` 与合法的 `project_feedling_v1` 结构相同,
没有干净形状能区分 —— codex plan_review 拍板)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from memory_garden.text.card_guard import (  # noqa: E402
    bucket_pollution_reason,
    default_bucket_for_text,
    field_pollution_reason,
    hard_field_pollution_reason,
)

# --- 现场原样 --------------------------------------------------------------

INCIDENT_BUCKET = "relationship'}]}analysis to=functions.memory_write 大发 pkoutput error code: 400,"


def test_incident_string_is_flagged():
    # 事故串含通道前缀+route(强证据)→ 硬、软两级都判脏。
    assert hard_field_pollution_reason(INCIDENT_BUCKET) is not None
    assert field_pollution_reason(INCIDENT_BUCKET) is not None
    assert bucket_pollution_reason(INCIDENT_BUCKET) is not None


# --- 硬字段(summary/content):只认强证据或 ≥2 弱证据共现 -------------------

HARD_POLLUTED = [
    "analysis to=functions.memory_write",            # 通道前缀 + route(强)
    'commentary to=functions.memory_write {"op":"add"}',
    "final to=functions.identity_write",             # 强
    "<|channel|>analysis<|message|>",                # harmony 特殊 token(强)
    "<|start|>assistant",
    'analysis to=functions.memory_write 乱码 output error code: 400',  # 强 + 多弱
    'to=functions.memory_write reason":"x"}]}',      # 裸 route + 撕裂尾巴 = 2 弱
]


def test_hard_field_strong_or_multiweak_flagged():
    for text in HARD_POLLUTED:
        assert hard_field_pollution_reason(text) is not None, text


# --- 硬字段假阳性语料(codex code_review):开发者正常记忆,各只命中一个弱证据 ---

HARD_LEGIT = [
    "用户今天在成都，天气很热，体感偏干",             # 正常中文记忆
    "他终于答应去看医生了（拖了三个月）",             # 圆括号合法
    "我在调 API，遇到 output error code: 400，在查原因",  # 只报错回显(1 弱)
    "The API fixture is {\"cards\": []}",             # 只 protocol_head(1 弱)
    "修复脚本时发现尾部多了 \"enabled\": true}]}",     # 只撕裂尾巴(1 弱)
    "记住 parser 测试要原样保留 to=functions.memory_write 这个串",  # 只裸 route(1 弱)
    "发邮件 to=boss@example.com 抄送我",              # to= 但非 to=functions.
    "The user prefers dark mode in the evening",     # 正常英文记忆
    "Set redirect_to=/login after seeing output error code: 400",  # redirect_to= + 400,非协议
    "删掉多余的 }，把 \"port\": 8080 改成 8081",       # 配置 diff(1 弱:orphan tail)
    "Use to=functions.map in the DSL documentation.",  # DSL 文档里的 to=functions(1 弱裸 route)
]


def test_hard_field_single_weak_not_flagged():
    for text in HARD_LEGIT:
        assert hard_field_pollution_reason(text) is None, text


# --- 软字段(bucket/threads,短标签):任一弱证据即清洗(误杀=清洗,代价低) -----

def test_soft_field_single_weak_flagged():
    for text in ('to=functions.memory_write', 'foo relationship"}]}', '"reason":"..."}]}'):
        assert field_pollution_reason(text) is not None, text


def test_soft_field_legit_short_labels_survive():
    # 正常短标签不会命中弱证据。
    for text in ("工作", "user_onboarding", "健康饮食", "the house"):
        assert field_pollution_reason(text) is None, text


# --- 桶:合法自定义桶不能被形状误杀 ---------------------------------------

LEGIT_BUCKETS = [
    "妈妈",
    "the house",
    "工作",
    "health_diet_log_2026",       # 全小写 snake_case ≥4 段,但是合法用户桶
    "project_feedling_v1",        # snake_case + _v1,但是合法项目桶
    "Work",
]


def test_legit_buckets_survive():
    for b in LEGIT_BUCKETS:
        assert bucket_pollution_reason(b) is None, b


def test_known_taxonomy_residue_is_dropped_by_denylist():
    # 现场那个机器 taxonomy 桶:没有 harmony/报错/尾巴其它证据,
    # 只能靠精确 denylist 命中(形状与 project_feedling_v1 无法区分)。
    assert bucket_pollution_reason("long_term_preference_or_event_v1") is not None


# --- 桶的本地化默认值(Q3:未分类 不在双语 map,需显式 fallback) -----------

def test_default_bucket_localized():
    assert default_bucket_for_text("用户今天很开心") == "未分类"
    assert default_bucket_for_text("the user is happy today") == "Uncategorized"
    assert default_bucket_for_text("") == "未分类"   # 空 → 中文默认
