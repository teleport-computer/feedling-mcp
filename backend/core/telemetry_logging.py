"""Logging helpers for low-volume, content-free rollout telemetry."""

from __future__ import annotations

import logging


_HANDLER_MARKER = "_feedling_telemetry_stderr"


def stderr_info_logger(name: str) -> logging.Logger:
    """Return an INFO logger that always emits once to process stderr."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(handler, _HANDLER_MARKER, False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        setattr(handler, _HANDLER_MARKER, True)
        logger.addHandler(handler)
    return logger
