"""resident/V1 生图:伴侣自己决定,谎报**运行时**打回一次。

V1 与 V2 必须给用户同一个产品:V2 在 tool_loop 里有谎报打回的控制流,V1 起初
只有提示词文案 —— **prod 主链路上模型谎报会照常发布**(codex 审出)。跨运行时
基线不一致就是"改了一半",而 prod 大部分用户在 V1 这条路上。

这里测的是控制流,不是源码文本。
"""
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    # ⚠️ 这里只是"没人设过就用 cli"。AGENT_MODE 是 consumer 的**模块作用域**常量,
    # 别的测试文件(如 test_chat_resident_consumer_image.py)若先设成 http 并导入,
    # setdefault 就成了空操作,模块里也已经是 http —— 于是
    # outbound_file_turn_active 恒 False,打回分支根本不进,而**单独跑这个文件时
    # 一切正常**。所以下面每个回合都显式 patch crc.AGENT_MODE,不靠环境变量。
    "AGENT_MODE": "cli",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_img_autonomy_checkpoint.json",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402


def _text_msg(content, *, ts=7000.0, msg_id="m1"):
    return {
        "id": msg_id,
        "ts": ts,
        "content": content,
        "content_type": "text",
        "role": "user",
        "source": "chat",
    }


def _run_turn(replies, *, staged_images=(), user_text="画一张你自己"):
    """跑一个前台回合,返回 (call_agent 收到的 prompt 列表, 发出去的回复列表)。

    ``replies`` 按顺序回给每次 call_agent。``staged_images`` 是回合结束时
    "确实 stage 了的图"。
    """
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    prompts = []
    posted = []

    def fake_call(message, **kwargs):
        prompts.append(message)
        index = min(len(prompts) - 1, len(replies) - 1)
        return {"messages": [replies[index]]}

    def fake_post(*args, **kwargs):
        body = kwargs.get("content")
        if body is None and args:
            body = args[0]
        posted.append(body)
        return {"id": f"reply-{len(posted)}"}

    with patch.object(crc, "AGENT_MODE", "cli"), \
         patch.object(crc, "call_agent", side_effect=fake_call), \
         patch.object(crc, "post_reply", side_effect=fake_post), \
         patch.object(crc, "_begin_outbound_file_turn", return_value=None), \
         patch.object(crc, "_staged_outbound_image_snapshot",
                      return_value=list(staged_images)), \
         patch.object(crc, "_finish_outbound_attachment_turn",
                      return_value=([], list(staged_images))):
        crc._process_messages([_text_msg(user_text)])
    return prompts, posted


def test_unbacked_image_claim_is_bounced_once_on_the_v1_lane():
    """它说画好了、一张图都没 stage → 必须打回一次,而不是照常发布。

    这是 V1/V2 的产品基线:同一个谎报,在两条 lane 上必须得到同样的对待。
    """
    prompts, posted = _run_turn(
        ["图片已经生成,你看看喜欢吗", "抱歉,我其实还没画,要我现在画吗?"],
        staged_images=(),
    )

    assert len(prompts) == 2, f"谎报必须触发一次打回,实际 call_agent 次数={len(prompts)}"
    assert "没有任何图片真的被生成" in prompts[1], "打回指令要把事实讲清楚"
    assert "generate-image" in prompts[1], "要告诉它怎么真的去画"
    # 用户看到的是纠正后的话,不是那句谎话
    assert any("还没画" in str(body) for body in posted), (
        f"发出去的应该是纠正后的回复,实际={posted}"
    )


def test_bounce_happens_only_once_then_the_reply_goes_out_as_is():
    """只纠正一次。再撒谎就照原样发出并留痕 —— 不拿 runtime 跟模型较劲。

    Seven 定的上限:「你最多打回去一次,如果重试还是不成功,你就让它正常地去吐
    就行了」。无限打回会把一次对话拖死,而且掩盖真正的问题(是模型不行,还是
    我们提示写得不清楚)。
    """
    prompts, posted = _run_turn(
        ["图片已经生成", "图片已经生成"],  # 两轮都撒谎
        staged_images=(),
    )
    assert len(prompts) == 2, "第二次谎报不能再打回(否则就是无限纠缠)"
    assert posted, "第二次谎报也要照原样发出去,不能让用户什么都收不到"


def test_honest_failure_is_never_bounced():
    """诚实地说没画成 —— 绝不能打回。

    误判的代价比漏判高得多:打回一句真话,等于逼它把真话改成假话。
    """
    prompts, _posted = _run_turn(
        ["图片生成失败了,我这边暂时画不出来"],
        staged_images=(),
    )
    assert len(prompts) == 1, "诚实的失败说明不该被当成谎报打回"


def test_real_image_means_no_bounce():
    """图真的 stage 了,说"图已经生成"就是实话 —— 不打回。"""
    staged = [
        crc.StagedChatImage(
            source_path="/tmp/x.png", name="x.png", mime_type="image/png",
            data=b"img",
        )
    ]
    prompts, _posted = _run_turn(["图片已经生成"], staged_images=staged)
    assert len(prompts) == 1, "有图就不是谎报"


def test_v1_and_v2_share_one_detector_implementation():
    """两条 lane 必须用**同一个**判定函数,不能各写一份。

    各写一份正是当年字面 `user:` 标签只修了 V2、漏掉托管路径的根因
    (worker.py:9047 注释记录的那次事故)。这里锁死同一性:改了 V2 的判定,
    V1 自动跟着变。
    """
    from model_api_runtime.v2 import tool_loop

    assert crc._claims_image_delivered is tool_loop._claims_image_delivered


@pytest.mark.parametrize(
    "text,expected",
    [
        # 真谎报
        ("图片已经生成", True),
        ("Here's the image!", True),
        ("自画像已经画好了", True),
        # 诚实 / 元话语 —— 一条都不能误判(每一条都来自 codex 的实测反例)
        ("图片生成失败了,请稍后再试", False),
        ("图像生成需要配置模型", False),
        ("Here is the image-generation guide", False),
        ("I've created the image prompt for you", False),
        ("已经为你做好了图片生成提示词", False),
        ("已经为你画好了构图思路,接下来可以开始生图", False),
        ("如果我说'图片已经生成',那是在骗你", False),
        ("I do not think I've created the image yet", False),
        # codex 第三轮:疑问与裸引用。根因是小句切分把问号当分隔符吃掉了,
        # 切完小句里没有 ?,任何疑问检测都不可能生效。
        ("你想让我说“图片已经生成”吗?", False),
        ("图片已经生成了吗?", False),
        ("“图片已经生成”只是一个示例。", False),
        ("Heres the image?", False),
        # 我另造的:开集宾语靠词表挡不住,只有"必须在小句末尾"挡得住
        ("已经为你做好了准备", False),
        ("你说图片已经生成了?", False),
        # 无宾语完成态仍必须成立(codex 指出我删过头,把自己锁的正例判死了)
        ("已经为你画好了", True),
    ],
)
def test_detector_cases_on_the_v1_lane(text, expected):
    """把判定用例固化在 V1 这一侧,确保共享没被人悄悄改回各写一份。"""
    assert crc._claims_image_delivered(text) is expected
