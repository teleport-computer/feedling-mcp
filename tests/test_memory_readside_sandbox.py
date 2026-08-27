from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_memory_readside_sandbox_outputs_product_report():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "memory_readside_sandbox.py"),
            "--json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    body = json.loads(result.stdout)
    assert body["acceptance"]["index_count"] >= 5
    assert body["acceptance"]["fetch_count"] == 3
    assert body["acceptance"]["index_no_raw_quote"] == "PASS"
    assert all("verbatim" not in item for item in body["index"]["items"])
    assert all("her_quote" not in item for item in body["index"]["items"])
    assert all("follow_up" not in item for item in body["index"]["items"])
    assert any(item.get("verbatim") for item in body["fetch"]["items"])


def test_memory_readside_sandbox_plaintext_is_human_readable():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "memory_readside_sandbox.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    output = result.stdout
    assert "agent 先看到的安全摘要目录" in output
    assert "agent 命中后拿到的完整正文" in output
    assert "产品验收结论" in output
    assert "index_no_raw_quote=PASS" in output
    assert "人话" in output
