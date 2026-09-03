"""Static guard for every frame source boundary.

Behavior tests cover the high-risk paths; this inventory makes newly missing or
mislabelled source declarations fail at the exact call site.
"""

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _declared_sources(relative_path: str, function_name: str) -> list[object]:
    tree = ast.parse((ROOT / relative_path).read_text())
    sources: list[object] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        if isinstance(called, ast.Attribute) and called.attr == function_name:
            pass
        elif (
            isinstance(called, ast.Attribute)
            and called.attr == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == function_name
        ):
            pass
        elif isinstance(called, ast.Name) and called.id == function_name:
            pass
        else:
            continue
        source = next(
            (kw.value for kw in node.keywords if kw.arg == "source"), None
        )
        sources.append(source.value if isinstance(source, ast.Constant) else None)
    return sources


@pytest.mark.parametrize(
    ("relative_path", "expected_sources"),
    [
        ("backend/core/store.py", ["screen"]),
        ("backend/screen/screen_read_core.py", ["screen", "screen"]),
        ("backend/screen/caption.py", ["screen", "screen"]),
        ("backend/model_api_runtime/v2/serve_worker.py", ["screen"]),
        ("backend/model_api_runtime/v2/worker.py", ["screen"]),
        ("backend/screen/frames.py", ["screen"]),
    ],
)
def test_every_screen_list_consumer_declares_screen_source(
    relative_path, expected_sources
):
    assert _declared_sources(relative_path, "frame_list_meta") == expected_sources


@pytest.mark.parametrize(
    ("relative_path", "expected_source"),
    [
        ("backend/perception/store.py", "photo"),
        ("backend/screen/frames.py", "screen"),
    ],
)
def test_every_frame_writer_declares_its_source(relative_path, expected_source):
    assert _declared_sources(relative_path, "frame_upsert") == [expected_source]
