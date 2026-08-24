"""花园语言判定：几个英文桶不许把整个中文花园翻成英文。

## 线上事故（2026-08-24）

一个真实用户的中文花园（226 张卡，archive_language=zh-Hans-US），两天内新落的卡
整个变成英文 —— 摘要是英文散文，桶是 Our relationship / Feelings & comfort。

## 根因：拿字符数比大小，中英文不对等

    中文桶平均 3.4 字符（「工作」）
    英文桶平均 11.6 字符（「Our relationship」）
    → 一个英文桶顶三个半中文桶

实测「6 个中文桶 + 3 个英文桶」就翻。而那几个英文桶是更早一个 bug 的残留
（老提示词同时给中英两套让模型挑，约 1/3 的中文记忆被贴错），于是形成回路：

    旧残留 → 判成英文花园 → 新卡全用英文桶 → 英文桶更多 → 自我强化

**用得越久越容易中招** —— 攒的英文桶越多。

## 为什么之前的测试没抓到

原来的用例只有「全中文」和「全英文」两个干净极端。而现实里只有混合态：
干净的两极在生产中根本不存在。这个文件补的就是混合态。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from chat.reply_language import infer_garden_language  # noqa: E402


@pytest.mark.parametrize("desc,buckets", [
    ("事故现场：中英各半，英文桶名更长",
     "我们的关系、工作、健康、Our relationship、Feelings & comfort、GitHub"),
    ("6 个中文桶 + 3 个英文桶（实测的翻转下限）",
     "工作、健康、宠物、家庭、朋友、爱好、Work、Health、Pets"),
    ("压倒性中文 + 2 个英文",
     "工作、健康、宠物、家庭、朋友、爱好、金钱、饮食、Our relationship、Feelings & comfort"),
    ("一个超长英文桶 vs 三个短中文桶",
     "工作、健康、宠物、Preferences & boundaries"),
])
def test_english_buckets_cannot_flip_a_chinese_garden(desc, buckets):
    assert infer_garden_language({}, existing_buckets=buckets) == "zh-Hans", desc


def test_a_tie_stays_chinese():
    """平票保持中文。

    不对称是刻意的：把中文花园翻成英文，用户立刻看见自己的记忆换了语言；
    保持中文最多是"没跟上"。两种错误代价差得远。
    """
    assert infer_garden_language({}, existing_buckets="工作、健康、Work、Health") == "zh-Hans"


@pytest.mark.parametrize("buckets", [
    "Work / Health / Pets / Family",
    "Work、Health、Pets、工作",
])
def test_a_genuinely_english_garden_still_reads_as_english(buckets):
    """修复不能矫枉过正 —— 真的多数是英文，还得判英文。"""
    assert infer_garden_language({}, existing_buckets=buckets) == "en"


def test_custom_buckets_are_recognised_too():
    """自造桶（不在通用集里）同样要认得出语言。"""
    assert infer_garden_language({}, existing_buckets="妈妈、房子、崽崽") == "zh-Hans"


def test_no_buckets_falls_back_without_crashing():
    """新花园还没有任何桶时，走后面的身份卡/locale 兜底，不能炸。"""
    assert infer_garden_language({}, existing_buckets="") in ("zh-Hans", "en")
