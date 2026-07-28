"""A 组(明文 actions)的「模型原始输出泄漏」防护 —— 不依赖 DB 的单测。

覆盖两类逻辑,都不需要真实 store:
1. ``_memory_inner_from_action`` 是纯函数 → 直测「脏桶降级到按语言的默认桶、干净桶不动」。
2. add/supersede/upgrade 的**污染拒绝在碰 DB 之前 return** → 传 store=None 也能验到
   返回 ``memory_card_polluted`` + 400。

现场(2026-07-28):inline 工具/io_cli/genesis/hosted 都过 actions 层,而 actions 之前
完全不校验字段内容。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from memory import actions  # noqa: E402


# --- 纯函数:桶降级 --------------------------------------------------------

def test_inner_defaults_polluted_bucket_by_language():
    inner = actions._memory_inner_from_action({
        "summary": "用户今天在成都，天气很热",
        "content": "对话发生时用户位于成都市，体感偏热",
        "bucket": "relationship'}]}analysis to=functions.memory_write output error code: 400",
    })
    assert inner["bucket"] == "未分类"          # 脏桶 → 中文卡默认桶


def test_inner_defaults_known_taxonomy_residue_english():
    inner = actions._memory_inner_from_action({
        "summary": "The user prefers dark mode",
        "content": "Observed across several evening sessions",
        "bucket": "long_term_preference_or_event_v1",
    })
    assert inner["bucket"] == "Uncategorized"   # 机器 taxonomy → 英文卡默认桶


def test_inner_keeps_clean_custom_bucket():
    inner = actions._memory_inner_from_action({
        "summary": "妈妈喜欢喝普洱",
        "content": "上次视频里提到的",
        "bucket": "健康饮食",
    })
    assert inner["bucket"] == "健康饮食"        # 干净自定义桶原样保留


# --- 拒绝路径:硬字段污染 → 400（DB 之前 return，store=None 即可） ----------

def test_add_rejects_polluted_summary():
    body, effects, code = actions._memory_add_action(None, {
        "type": "fact",
        "memory": {
            "summary": "analysis to=functions.memory_write",
            "content": "正常的一段正文",
            "title": "analysis to=functions.memory_write",
        },
    })
    assert code == 400
    assert body.get("error") == "memory_card_polluted"


def test_supersede_rejects_polluted_content():
    body, effects, code = actions._memory_supersede_action(None, None, {
        "supersedes": ["mem_x"],
        "memory": {
            "type": "fact",
            "summary": "用户今天很开心",
            "content": 'garbage relationship"}]}',
            "title": "用户今天很开心",
        },
    })
    assert code == 400
    assert body.get("error") == "memory_card_polluted"


def test_upgrade_rejects_polluted_summary():
    body, effects, code = actions._memory_upgrade_action(None, None, {
        "id": "mem_x",
        "v1": {
            "summary": "commentary to=functions.memory_write",
            "content": "正常正文",
        },
    })
    assert code == 400
    assert body.get("error") == "memory_card_polluted"


def test_add_clean_card_not_rejected_by_guard():
    # 干净卡不能被 guard 拦(它会继续往下走到 DB —— 这里只断言不是被 guard 判的 400）。
    # 用 store=None 会在 guard 之后的 _load_moments 抛错;我们只验「不是 memory_card_polluted」。
    try:
        body, _effects, _code = actions._memory_add_action(None, {
            "type": "fact",
            "memory": {"summary": "用户喜欢喝咖啡", "content": "每天早上一杯", "title": "用户喜欢喝咖啡"},
        })
    except Exception:
        # 到了 DB 层(store=None)抛错 = 已越过 guard,符合预期。
        return
    assert body.get("error") != "memory_card_polluted"
