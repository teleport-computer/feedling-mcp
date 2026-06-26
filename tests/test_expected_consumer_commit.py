"""
Tests for backend ``chat.consumer.expected_consumer_commit`` — the value the
backend advertises to resident consumers so they can self-update to the commit
the backend currently deploys.

Run with: pytest tests/test_expected_consumer_commit.py -v
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from chat import consumer as chat_consumer  # noqa: E402


def test_returns_explicit_pin_when_set(monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "pinnedaa")
    monkeypatch.setenv("FEEDLING_GIT_COMMIT", "deployedbb")
    assert chat_consumer.expected_consumer_commit() == "pinnedaa"


def test_falls_back_to_deployed_commit(monkeypatch):
    monkeypatch.delenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", raising=False)
    monkeypatch.setenv("FEEDLING_GIT_COMMIT", "deployedbb")
    assert chat_consumer.expected_consumer_commit() == "deployedbb"


def test_empty_when_neither_set(monkeypatch):
    monkeypatch.delenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", raising=False)
    monkeypatch.delenv("FEEDLING_GIT_COMMIT", raising=False)
    assert chat_consumer.expected_consumer_commit() == ""


def test_strips_whitespace(monkeypatch):
    monkeypatch.delenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", raising=False)
    monkeypatch.setenv("FEEDLING_GIT_COMMIT", "  deployedcc\n")
    assert chat_consumer.expected_consumer_commit() == "deployedcc"
