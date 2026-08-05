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
            "content": '<|channel|>analysis<|message|> garbage relationship"}]}',
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


# --- round-2(codex code_review):threads 过滤 / 语言判定 / migrate 桶默认 -------

def test_inner_filters_polluted_threads():
    # proactive/genesis/继承旧卡的 threads 也过 _memory_inner_from_action —— 脏项必须被滤掉。
    inner = actions._memory_inner_from_action({
        "summary": "用户在排查后端问题",
        "content": "今天下午定位了一个解析 bug",
        "threads": ["排查", "analysis to=functions.memory_write", "后端"],
    })
    assert inner["threads"] == ["排查", "后端"]


def test_inner_default_bucket_english_description_only():
    # 纯英文、只有 description(无 content)、脏桶 → 默认桶应是 Uncategorized 而非中文未分类
    # (语言判定用原始文本,不用带中文标签的合成 content)。
    inner = actions._memory_inner_from_action({
        "summary": "The user prefers tea over coffee",
        "description": "Mentioned during an evening chat",
        "bucket": "analysis to=functions.memory_write",
    })
    assert inner["bucket"] == "Uncategorized"


def test_migrate_polluted_bucket_gets_localized_default():
    from memory.migrate_prompt_v1 import parse_migrated_cards
    upgrades, _unmigrated, err = parse_migrated_cards(
        '{"upgrades": [{"id": "m1", "summary": "用户喜欢普洱茶",'
        ' "content": "上次视频里提到", "bucket": "long_term_preference_or_event_v1"}]}',
        allowed_ids={"m1"},
    )
    assert err is None
    assert len(upgrades) == 1
    assert upgrades[0]["bucket"] == "未分类"       # 脏桶 → 就地降级(不是空串)


# --- 拒绝路径:墓碑注记 → 400(2026-08-06 usr_a40e 徒手 patch 潮) -----------


def test_supersede_rejects_tombstone_note():
    # usr_a40e 实况形状:agent 徒手 memory-patch 把「已被 <卡id> 取代」写成新卡。
    body, effects, code = actions._memory_supersede_action(None, None, {
        "type": "memory.supersede",
        "supersedes": ["c42ebb9618ae447df9d52107ea15de85"],
        "memory": {
            "type": "fact",
            "title": "已被 c42ebb9618ae447df9d52107ea15de85 取代——绿豆汤偏好",
            "summary": "已被 c42ebb9618ae447df9d52107ea15de85 取代——绿豆汤偏好",
            "content": "已被 c42ebb9618ae447df9d52107ea15de85 取代——饮食禁忌详情。",
        },
    })
    assert code == 400
    assert body.get("error") == "memory_card_tombstone"


def test_add_rejects_tombstone_note():
    body, _effects, code = actions._memory_add_action(None, {
        "type": "fact",
        "memory": {
            "summary": "superseded by 1a1f94f9fdc9ec86 — old note",
            "content": "superseded by 1a1f94f9fdc9ec86 — merged elsewhere.",
            "title": "superseded by 1a1f94f9fdc9ec86",
        },
    })
    assert code == 400
    assert body.get("error") == "memory_card_tombstone"


def test_supersede_prose_about_replacement_without_hex_not_rejected_by_tombstone_gate():
    # 正常散文「已被新手机取代」不带 hex id —— 不许被墓碑闸拦。store=None:
    # 通过闸后会在碰 DB 时炸,借 AttributeError 证明「没有在闸上被拒」。
    import pytest
    with pytest.raises(AttributeError):
        actions._memory_supersede_action(None, None, {
            "type": "memory.supersede",
            "supersedes": ["some_old_card_id_1234"],
            "memory": {
                "type": "fact",
                "title": "换了新手机",
                "summary": "换了新手机",
                "content": "旧手机已被新手机取代,数据迁移顺利。",
            },
        })
