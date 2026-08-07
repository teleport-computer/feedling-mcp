from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import context


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("帮我生成一张海边日落的图片", "帮我生成一张海边日落的图片"),
        ("画一幅小猫坐在月亮上的插画", "画一幅小猫坐在月亮上的插画"),
        ("生图：一只穿宇航服的柯基", "生图：一只穿宇航服的柯基"),
        ("Generate an image of a tiny red robot", "Generate an image of a tiny red robot"),
        ("Can you generate images?", None),
        ("怎么实现生成图片功能？", None),
        ("分析一下这张图片", None),
        ("不要生成图片，只解释构图", None),
    ],
)
def test_required_image_generation_prompt_is_conservative(text, expected):
    assert context.required_image_generation_prompt(
        [{"role": "user", "content": text}]
    ) == expected


def test_required_image_generation_prompt_includes_followup_refinement():
    assert context.required_image_generation_prompt(
        [
            {"role": "user", "content": "生成一张森林里的小屋图片"},
            {"role": "user", "content": "改成夜晚，窗户里有暖光"},
        ]
    ) == "生成一张森林里的小屋图片\n改成夜晚，窗户里有暖光"


def test_required_image_generation_prompt_follows_cancellation_and_new_request():
    assert context.required_image_generation_prompt(
        [
            {"role": "user", "content": "生成一张森林里的小屋图片"},
            {"role": "user", "content": "不用生成了，解释一下这个构图"},
        ]
    ) is None
    assert context.required_image_generation_prompt(
        [
            {"role": "user", "content": "生成一张森林里的小屋图片"},
            {"role": "user", "content": "不要小屋，改成画一张雪山照片"},
        ]
    ) == "不要小屋，改成画一张雪山照片"
