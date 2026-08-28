from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from core.store_sections import (  # noqa: E402
    SectionSlot,
    SectionStatus,
    StoreLoadMode,
    StoreSection,
    StoreSectionUnavailable,
    store_load_mode,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, StoreLoadMode.LEGACY),
        ("legacy", StoreLoadMode.LEGACY),
        ("selective", StoreLoadMode.SELECTIVE),
        ("lazy", StoreLoadMode.LAZY),
    ],
)
def test_store_load_mode(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("FEEDLING_STORE_LOAD_MODE", raising=False)
    else:
        monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", raw)

    assert store_load_mode() is expected


def test_invalid_store_load_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "typo")

    with pytest.raises(RuntimeError, match="FEEDLING_STORE_LOAD_MODE"):
        store_load_mode()


def test_section_slot_first_load_and_stale_refresh():
    slot = SectionSlot(StoreSection.CHAT)
    calls: list[str] = []

    assert slot.ensure(
        lambda: calls.append("cold"), force=False, strict=True
    )
    assert slot.mark_stale(dirty_version=7)
    assert slot.ensure(
        lambda: calls.append("refresh"), force=False, strict=True
    )

    assert calls == ["cold", "refresh"]
    assert slot.status is SectionStatus.FRESH
    assert slot.dirty_version is None


def test_unloaded_mark_stale_records_hint_without_loading():
    slot = SectionSlot(StoreSection.CHAT)

    assert slot.mark_stale(dirty_version=7) is False

    assert slot.status is SectionStatus.UNLOADED
    assert slot.dirty_version == 7


def test_one_hundred_first_callers_share_one_load():
    slot = SectionSlot(StoreSection.CHAT)
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def load():
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(2)

    with ThreadPoolExecutor(max_workers=100) as pool:
        futures = [
            pool.submit(slot.ensure, load, force=False, strict=True)
            for _ in range(100)
        ]
        assert entered.wait(2)
        release.set()
        assert all(future.result(timeout=2) for future in futures)

    assert calls == 1


def test_first_failure_unloads_but_refresh_failure_keeps_stale():
    slot = SectionSlot(StoreSection.CHAT)

    def fail():
        raise RuntimeError("db down")

    with pytest.raises(StoreSectionUnavailable) as exc_info:
        slot.ensure(fail, force=False, strict=True)
    assert exc_info.value.section is StoreSection.CHAT
    assert exc_info.value.slug == "store_section_unavailable"
    assert slot.status is SectionStatus.UNLOADED

    assert slot.ensure(lambda: None, force=False, strict=True)
    slot.mark_stale()

    assert slot.ensure(fail, force=False, strict=False) is False
    assert slot.status is SectionStatus.STALE


def test_singleflight_failure_is_retryable_not_poisoned():
    slot = SectionSlot(StoreSection.CHAT)
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary")

    assert slot.ensure(fail_once, force=False, strict=False) is False
    assert slot.ensure(fail_once, force=False, strict=True) is True
    assert calls == 2
    assert slot.status is SectionStatus.FRESH
