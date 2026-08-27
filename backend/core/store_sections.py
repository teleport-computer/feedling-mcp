"""Explicit lazy-load state for worker-local UserStore sections."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class StoreSection(str, Enum):
    CHAT = "chat"
    FRAMES = "frames"
    WORLD_BOOKS = "world_books"
    TOKENS = "tokens"
    PUSH_STATE = "push_state"
    LIVE_ACTIVITY = "live_activity"


class SectionStatus(str, Enum):
    UNLOADED = "unloaded"
    LOADING = "loading"
    FRESH = "fresh"
    STALE = "stale"


class StoreLoadMode(str, Enum):
    LEGACY = "legacy"
    SELECTIVE = "selective"
    LAZY = "lazy"


def store_load_mode() -> StoreLoadMode:
    raw = os.environ.get("FEEDLING_STORE_LOAD_MODE", "legacy").strip().lower()
    try:
        return StoreLoadMode(raw)
    except ValueError as exc:
        raise RuntimeError(
            "FEEDLING_STORE_LOAD_MODE must be legacy, selective, or lazy"
        ) from exc


class StoreSectionUnavailable(RuntimeError):
    """Content-free retryable failure for a required Store section."""

    slug = "store_section_unavailable"

    def __init__(self, section: StoreSection):
        super().__init__(f"store section unavailable: {section.value}")
        self.section = section


@dataclass
class SectionSlot:
    """One per-user section load state with condition-based singleflight."""

    section: StoreSection
    status: SectionStatus = SectionStatus.UNLOADED
    loaded_at_mono: float = 0.0
    dirty_version: int | None = None
    _condition: threading.Condition = field(
        default_factory=threading.Condition,
        init=False,
        repr=False,
    )
    _has_cache: bool = field(default=False, init=False, repr=False)
    _revision: int = field(default=0, init=False, repr=False)
    _load_epoch: int = field(default=0, init=False, repr=False)
    _last_failure_epoch: int = field(default=0, init=False, repr=False)
    _last_failure: Exception | None = field(default=None, init=False, repr=False)

    @property
    def has_cache(self) -> bool:
        with self._condition:
            return self._has_cache

    def mark_stale(self, *, dirty_version: int | None = None) -> bool:
        """Record invalidation without turning an unloaded slot into a load."""
        with self._condition:
            self._revision += 1
            if dirty_version is not None:
                value = int(dirty_version)
                self.dirty_version = (
                    value
                    if self.dirty_version is None
                    else max(self.dirty_version, value)
                )
            if self.status is SectionStatus.FRESH:
                self.status = SectionStatus.STALE
            return self._has_cache

    def ensure(
        self,
        loader: Callable[[], object],
        *,
        force: bool,
        strict: bool,
    ) -> bool:
        """Load or refresh once; concurrent callers share the same outcome."""
        while True:
            with self._condition:
                if self.status is SectionStatus.LOADING:
                    waited_epoch = self._load_epoch
                    while self.status is SectionStatus.LOADING:
                        self._condition.wait()
                    if self._last_failure_epoch == waited_epoch:
                        if strict:
                            raise StoreSectionUnavailable(self.section) from (
                                self._last_failure
                            )
                        return False
                    if self.status is SectionStatus.FRESH:
                        return True
                    continue

                if self.status is SectionStatus.FRESH and not force:
                    return True

                start_revision = self._revision
                self.status = SectionStatus.LOADING
                self._load_epoch += 1
                load_epoch = self._load_epoch

            try:
                loader()
            except Exception as exc:
                with self._condition:
                    self.status = (
                        SectionStatus.STALE
                        if self._has_cache
                        else SectionStatus.UNLOADED
                    )
                    self._last_failure_epoch = load_epoch
                    self._last_failure = exc
                    self._condition.notify_all()
                if strict:
                    raise StoreSectionUnavailable(self.section) from exc
                return False

            with self._condition:
                self._has_cache = True
                self.loaded_at_mono = time.monotonic()
                self._last_failure = None
                if self._revision != start_revision:
                    self.status = SectionStatus.STALE
                    self._condition.notify_all()
                    continue
                self.status = SectionStatus.FRESH
                self.dirty_version = None
                self._condition.notify_all()
                return True
