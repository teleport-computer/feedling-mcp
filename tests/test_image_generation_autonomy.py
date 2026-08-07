"""生图的决定权归伴侣自己 —— 这几条锁的是产品哲学,不是实现细节。

背景(2026-08-08):原来的实现用一组正则判断「用户是不是在要图」,若模型没调工具
就拿**用户原话**去生图,并把模型那一轮说的话整个丢掉。三个后果都违背产品设计:
伴侣的含蓄表达(「这时候有张图就更好了」)永远触发不了;它自己想画一张也没有入口;
拿用户原话当 prompt 画出来的东西不带伴侣的理解(不知道「自画像」里的「自」是谁)。

现在:工具交给它,何时调、prompt 怎么写都由它决定;它的话照常发;失败如实交回。
"""
import inspect

from model_api_runtime.v2 import context, tool_loop


def test_no_regex_gate_decides_whether_the_user_wants_an_image():
    """语义判断归模型。规则/正则只配做确定性的事,不配替它猜用户想要什么。"""
    assert not hasattr(context, "required_image_generation_prompt"), (
        "正则意图闸已废除:它替伴侣做了本该由伴侣做的判断。"
        "要恢复的话请先回答 —— 含蓄请求怎么办?它自己想画怎么办?"
    )
    src = inspect.getsource(tool_loop)
    assert "resolve_required_image_generation_prompt" not in src


def test_model_text_is_delivered_alongside_the_image():
    """图和话是一次表达的两半。之前这里硬编码空字符串,所以**即使模型正确调用了
    工具**,用户也只收到一张孤零零的图。"""
    src = inspect.getsource(tool_loop)
    assert 'await on_reply(pr.text or "", final=True, media=media)' in src, (
        "生图成功那条路径必须带上伴侣这一轮说的话"
    )
    assert 'await on_reply("", final=True, media=media)' not in src, (
        "不许再把伴侣的话硬编码成空"
    )


def test_generation_failure_is_handed_back_to_the_model():
    """失败是它该知道的事实,不是 runtime 替它隐藏的意外。原来直接 raise 打断整轮:
    用户既没有图也没有一句解释,而伴侣根本不知道发生过什么。"""
    src = inspect.getsource(tool_loop)
    assert "error: image generation failed" in src
    assert "不要声称图已经生成" in src


def test_a_lie_is_bounced_exactly_once():
    """说了没做要纠正,但只纠正一次 —— 再纠缠就是拿 runtime 跟模型较劲,
    而且会掩盖真正的问题(是模型不行,还是我们的提示没写清楚)。"""
    src = inspect.getsource(tool_loop)
    assert "image_claim_bounces < 1" in src, "谎报打回必须有次数上限"
    assert "image_claim_retry_instruction" in src


def test_claim_detector_is_conservative():
    """宁可漏判也不要误判:漏判只是少一次纠正,误判会把伴侣一句正常的话当成谎话。"""
    claims = [
        "已经为你画好了", "图片生成好了", "Here's the image you asked for",
        "I've created the illustration",
    ]
    innocents = [
        "我想画一张给你看", "你想要什么样的图?", "这张图让我想起那天",
        "我画不了图,但可以描述给你听",
        # 以下三条来自 codex 审查的实测反例(2026-08-07):第一版检测把它们
        # 全判成谎报 —— 那等于逼伴侣把诚实的失败说明改成假话。
        "图片生成失败了,请稍后再试",
        "图像生成需要配置模型",
        "Here is the image generation guide",
    ]
    for text in claims:
        assert tool_loop._claims_image_delivered(text), f"漏判谎报: {text}"
    for text in innocents:
        assert not tool_loop._claims_image_delivered(text), f"误判正常表达: {text}"
