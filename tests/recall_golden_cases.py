"""Frozen inputs for the automatic-recall golden (T523 step 3).

The expected outputs in ``tests/data/recall_golden_834c1545.json`` were produced
by running ``generate()`` against origin/test 834c154586b557afc84673253a3b50af22ea22c8
(memgarden 0.22.0) BEFORE the hybrid wiring existed. They must never be
regenerated from a tree that contains the hybrid code: the point of the file is
to pin the pre-change behaviour.

    python3 -c "import sys; sys.path[:0]=['tests','backend']; \
        import recall_golden_cases as g; g.write()"

Clock-dependent fields are frozen, not skipped: ``time.monotonic`` (the log's
duration_ms) and "now" for the 7-day fresh-card rule are fixed below.
"""
from __future__ import annotations

import functools
import json
from datetime import datetime, timezone
from pathlib import Path

GOLDEN_PATH = Path(__file__).parent / "data" / "recall_golden_834c1545.json"
FIXED_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
FIXED_MONOTONIC = 1000.0


def _card(cid, summary="", content="", *, created="2026-01-05T08:00:00+00:00", **extra):
    card = {"id": cid, "summary": summary, "content": content, "status": "active",
            "created_at": created}
    card.update(extra)
    return card


def _garden():
    cards = [
        _card("cat", title="猫咪照顾", linked_dimension="猫咪",
              description="用户聊猫咪健康问题时，先需要被安抚，再给观察饮水和精神状态的建议。"),
        _card("kidney", "咪咪是只三岁的橘猫，去年体检肾指标偏高", "医生让每半年复查一次"),
        _card("archived_cat", "猫咪 咪咪 吃饭 旧卡", archived_at="2026-01-01"),
        _card("superseded_cat", "猫咪 不吃饭 旧结论", superseded_by="kidney"),
        _card("bookshelf", "The new bookshelf came from IKEA", "Assembled it over the weekend",
              bucket="home"),
        _card("commute", "每天去公司单程 45 分钟", "路上听 Audible", bucket="work",
              threads=["通勤", "工作"]),
        _card("sister", "Sister Emily lives in Denver", "Planning a trip out to see her"),
        _card("fresh1", "刚开始学吉他，每天练三十分钟", created="2026-09-18T09:00:00+00:00"),
        _card("fresh2", "周末去了新开的咖啡店", created="2026-09-19T20:00:00+00:00"),
        _card("fresh3", "买了新的笔记本，封面很好看", created="2026-09-20T07:00:00+00:00"),
        _card("fresh4", "搬家后通勤变短了", created="2026-09-17T07:00:00+00:00"),
        _card("turning", "决定辞职去读研", "这是今年最大的转折", bucket="turning",
              importance=0.9),
        _card("mixed", "Learning guitar 吉他 fingerpicking 指弹", "30 minutes a day"),
    ]
    cards += [_card(f"f{i}", f"第{i}次整理工作周报和会议纪要", "项目例会记录") for i in range(40)]
    return cards


WINDOW_CN = [
    {"role": "user", "content": "今天好累啊，刚下班到家"},
    {"role": "assistant", "content": "辛苦啦，晚饭吃了吗？要不要先歇会儿"},
    {"role": "user", "content": "还行，就是担心家里猫咪最近不吃饭"},
    {"role": "assistant", "content": "是咪咪吗？吃饭情况持续几天了？"},
]
WINDOW_EN = [
    {"role": "user", "content": "Where did I buy my new bookshelf from?"},
]
WINDOW_MIXED = [
    {"role": "user", "content": "我妹妹 Emily 住在哪来着"},
    {"role": "assistant", "content": "我记得她在丹佛"},
    {"role": "user", "content": "我每天练 guitar 多久？"},
]
WINDOW_EMPTY = [{"role": "assistant", "content": "你好呀"}]

BASE_ARGS = {"context_mode": "", "authorized_user_id": "u", "content_sk": None}

# (name, window, args, unified_ranker env value or None for unset)
SCENARIOS = [
    ("v2_cn_recent", WINDOW_CN, {"want_trace": True, "context_recent": True}, None),
    ("v1_cn_no_recent", WINDOW_CN, {"want_trace": True, "context_recent": False}, None),
    ("v1_cn_no_trace", WINDOW_CN, {"want_trace": False, "context_recent": False}, None),
    ("v2_en_single_message", WINDOW_EN, {"want_trace": True, "context_recent": True}, None),
    ("v2_mixed", WINDOW_MIXED, {"want_trace": True, "context_recent": True}, None),
    ("v2_empty_query", WINDOW_EMPTY, {"want_trace": True, "context_recent": True}, None),
    ("v1_legacy_ranker", WINDOW_CN, {"want_trace": True, "context_recent": False}, "0"),
    ("v2_legacy_ranker", WINDOW_MIXED, {"want_trace": True, "context_recent": True}, "0"),
]


def run_scenario(chat, recall_metadata, monkeypatch, name, window, args, ranker):
    """Drive the real _build_context_memories with frozen clocks; return a JSON-able triple."""
    cards = _garden()
    monkeypatch.setattr(chat.readside, "moments_to_cards", lambda *a, **k: [dict(c) for c in cards])
    monkeypatch.setattr(chat.time, "monotonic", lambda: FIXED_MONOTONIC)
    monkeypatch.setattr(recall_metadata, "recent_cards",
                        functools.partial(_ORIG_RECENT[0], now=FIXED_NOW))
    if ranker is None:
        monkeypatch.delenv(chat.RECALL_RANKER_ENV, raising=False)
    else:
        monkeypatch.setenv(chat.RECALL_RANKER_ENV, ranker)
    picked, trace, log = chat._build_context_memories([], window, {**BASE_ARGS, **args})
    return json.loads(json.dumps({"context_memories": picked, "context_memory_trace": trace,
                                  "context_memory_log": log}, ensure_ascii=False, sort_keys=True))


_ORIG_RECENT: list = []


class _MiniPatch:
    """Minimal monkeypatch stand-in so generate() runs outside pytest."""
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def setenv(self, key, value):
        import os
        self._undo.append((os.environ, key, os.environ.get(key)))
        os.environ[key] = value

    def delenv(self, key, raising=True):
        import os
        self._undo.append((os.environ, key, os.environ.get(key)))
        os.environ.pop(key, None)

    def undo(self):
        import os
        for obj, name, value in reversed(self._undo):
            if obj is os.environ:
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            else:
                setattr(obj, name, value)
        self._undo.clear()


def generate() -> dict:
    from enclave.routes import chat
    from memory import recall_metadata
    if not _ORIG_RECENT:
        _ORIG_RECENT.append(recall_metadata.recent_cards)
    out = {}
    for name, window, args, ranker in SCENARIOS:
        mp = _MiniPatch()
        try:
            out[name] = run_scenario(chat, recall_metadata, mp, name, window, args, ranker)
        finally:
            mp.undo()
    return out


def write() -> None:
    import importlib.metadata as md
    import subprocess
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    data = {"generated_from": head, "memgarden": md.version("memgarden"), "cases": generate()}
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
