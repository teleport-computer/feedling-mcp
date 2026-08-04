"""Q2:V2 dream 渲染卡时带上桶,让模型能复用已有桶(此前只渲染正文、丢了桶)。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from model_api_runtime.v2.serve_worker import _render_card_line  # noqa: E402


def test_card_line_includes_bucket():
    line = _render_card_line({"id": "m1", "summary": "用户很喜欢自己的猫", "bucket": "宠物"})
    assert "宠物" in line and "m1" in line and "用户很喜欢自己的猫" in line


def test_card_line_no_bucket_clean():
    line = _render_card_line({"id": "m2", "summary": "一张没有桶的卡"})
    assert line == "- id=m2 | bucket= | source= | created_at= | summary=一张没有桶的卡"


def test_card_line_includes_summary_and_full_content():
    line = _render_card_line({
        "id": "m3",
        "summary": "咖啡偏好",
        "content": "每天早上会用 V60 手冲，水温通常是 92 度。",
        "bucket": "生活偏好",
        "source": "memory_capture",
        "created_at": "2026-07-01T00:00:00Z",
    })

    assert "summary=咖啡偏好" in line
    assert "content=每天早上会用 V60 手冲，水温通常是 92 度。" in line
    assert "source=memory_capture" in line
