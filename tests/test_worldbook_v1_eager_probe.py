"""tools/e2e/worldbook_v1_eager_probe.py 的 harness 边界单测（不打网络）。

1. check(False) 必须让 E2EClient 保留失败现场：光记全局 FAIL，离开 with 时 exc_type=None、
   preserve_reason 为空，teardown 会把账号连同现场删掉。
2. D 闸的泄漏哨兵必须从 ENTRIES / 提示常量派生，覆盖全部 id、关键词、正文、标记、两轮原文
   —— 手写会漂（第一版漏了第三条 id「历法」与 C 句原文）。
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "backend", ROOT / "tools"):
    sys.path.insert(0, str(p))
os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")

probe = importlib.import_module("tools.e2e.worldbook_v1_eager_probe")


class _Client:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def preserve_failure(self, reason: str) -> None:
        self.reasons.append(reason)


def test_check_false_preserves_failure_site_and_check_true_does_not(monkeypatch):
    client = _Client()
    monkeypatch.setattr(probe, "ACTIVE_CLIENT", client)
    monkeypatch.setattr(probe, "FAIL", [])

    probe.check("green", True, "fine")
    assert client.reasons == [] and probe.FAIL == []

    probe.check("red-step", False, "detail here")
    assert probe.FAIL == ["red-step"]
    assert client.reasons and client.reasons[0].startswith("red-step: detail here")


def test_leak_sentinels_are_derived_from_entries_and_prompts():
    sentinels = set(probe.leak_sentinels())
    for e in probe.ENTRIES:
        assert e["name"] in sentinels, e["name"]
        assert e["content"] in sentinels
        for k in e["keys"]:
            assert k in sentinels, k
    assert set(probe.MARK.values()) <= sentinels
    assert probe.PROMPT_B in sentinels and probe.PROMPT_C in sentinels
    # 第一版手写漏掉的两个，现在必须在
    assert "历法" in sentinels and probe.PROMPT_C in sentinels


def test_pure_import_has_no_proxy_environment_side_effects():
    """纯 import 不得改进程环境。fresh 子进程预置 review 代理值，只 import，再读回。"""
    env = os.environ.copy()
    env.update({
        "HTTP_PROXY": "http://review-proxy:1", "HTTPS_PROXY": "http://review-proxy:2",
        "NO_PROXY": "review-keep", "no_proxy": "review-keep-lower",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), str(ROOT / "backend"),
                                                    str(ROOT / "tools"), env.get("PYTHONPATH")])),
    })
    code = (
        "import os, json, importlib; "
        "importlib.import_module('tools.e2e.worldbook_v1_eager_probe'); "
        "print(json.dumps({k: os.environ.get(k) for k in "
        "('HTTP_PROXY','HTTPS_PROXY','NO_PROXY','no_proxy')}))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
    assert json.loads(out) == {
        "HTTP_PROXY": "http://review-proxy:1", "HTTPS_PROXY": "http://review-proxy:2",
        "NO_PROXY": "review-keep", "no_proxy": "review-keep-lower",
    }
