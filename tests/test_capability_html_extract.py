"""Article extraction for web_fetch: the isolation, and the three outcomes.

trafilatura ships only in the worker image, so most of this file runs — and
must pass — without it installed. That is deliberate: "the dependency is
missing" is the exact state the backend image is always in, and it has to be an
ordinary no-article answer rather than a crash.

The isolation tests do not need trafilatura at all. They point the child at a
throwaway script, because what is being pinned is not "does extraction work" but
"can a child that misbehaves be killed" — which is the whole reason this runs in
a process instead of a thread.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from capabilities import html_extract  # noqa: E402
from capabilities import web as cap_web  # noqa: E402


@pytest.fixture()
def child_script(tmp_path, monkeypatch):
    """Redirect the extractor at a script this test writes."""
    def _use(source: str) -> None:
        (tmp_path / "fake_child.py").write_text(source, encoding="utf-8")
        monkeypatch.setattr(html_extract, "_CHILD_MODULE", "fake_child")
        monkeypatch.setattr(html_extract, "_CHILD_EXTRA_PYTHONPATH", str(tmp_path))
    return _use


# ------------------------------------------------------------------ isolation

def test_a_hung_child_is_killed_at_the_deadline(child_script, monkeypatch):
    """The load-bearing safety property.

    Extraction holds a slot in a capacity-2 semaphore that decryption also
    queues on, and it parses bytes an attacker chose. Measured, a page of
    repeated malformed close tags costs ~50x a normal page. A thread could not
    be taken back; this asserts the process is.
    """
    monkeypatch.setattr(html_extract, "_CHILD_TIMEOUT_SEC", 1.0)
    child_script("import time\ntime.sleep(30)\n")

    started = time.monotonic()
    assert html_extract.extract_article("<p>x</p>") is None
    assert time.monotonic() - started < 5.0, "the 30s child was waited on, not killed"


def test_a_child_that_dies_is_an_ordinary_no_article(child_script):
    child_script("import os\nos._exit(9)\n")
    assert html_extract.extract_article("<p>x</p>") is None


def test_a_child_that_crashes_on_a_signal_is_an_ordinary_no_article(child_script):
    # SIGKILL, not SIGSEGV: the property under test is "terminated by a signal ->
    # no article", and SIGKILL exercises it without waking the macOS crash
    # reporter, which pauses a segfaulting process for seconds. On Linux either
    # would do; the test should not depend on which OS runs it.
    child_script("import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    assert html_extract.extract_article("<p>x</p>") is None


def test_the_childs_own_empty_result_codes_are_not_errors(child_script):
    child_script("raise SystemExit(3)\n")
    assert html_extract.extract_article("<p>x</p>") is None


def test_a_missing_dependency_is_a_no_article_not_a_crash(monkeypatch):
    """The state the backend image is permanently in: trafilatura is worker-only.

    An ImportError here must not surface as a failed fetch, and must not have
    happened at process start either — hence the lazy import in the child.
    """
    monkeypatch.setattr(html_extract, "_CHILD_MODULE", "capabilities.no_such_module")
    assert html_extract.extract_article("<html><body><p>hi</p></body></html>") is None


def test_the_child_does_not_inherit_the_workers_secrets(child_script, monkeypatch):
    """It parses hostile input; it has no business seeing provider keys."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-visible")
    monkeypatch.setenv("DATABASE_URL", "postgresql://should-not-be-visible")
    child_script(
        "import os, sys\n"
        "leaked = [k for k in ('OPENAI_API_KEY', 'DATABASE_URL') if k in os.environ]\n"
        "sys.stdout.write('LEAKED:' + ','.join(leaked) if leaked else 'clean')\n"
    )
    assert html_extract.extract_article("<p>x</p>") == "clean"


def test_blank_input_never_spawns_a_process(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("spawned a child for empty input")

    monkeypatch.setattr(html_extract.subprocess, "run", _boom)
    assert html_extract.extract_article("   \n  ") is None


# --------------------------------------------------------- the three outcomes

_NAV = "<nav>Jump to content Main menu Random article Donate Log in</nav>"
_PAGE = f"<html><head><title>The Title</title></head><body>{_NAV}<article><p>{{}}</p></article></body></html>"


def _readable(html, *, article, content_type="text/html", monkeypatch=None):
    monkeypatch.setattr(html_extract, "extract_article", lambda _h: article)
    return cap_web._readable_text(html, content_type=content_type)


def test_a_full_article_replaces_the_plain_strip(monkeypatch):
    body = "the real article body. " * 30
    got = _readable(_PAGE.format(body), article=body, monkeypatch=monkeypatch)
    assert "Jump to content" not in got, "navigation survived a successful extraction"
    assert body.strip() in got


def test_a_short_extraction_is_kept_and_topped_up_rather_than_discarded(monkeypatch):
    """Real pages are legitimately short — a status page, a term definition, a
    weather summary. Throwing a correct short answer away would re-bury it under
    the navigation the extractor had just removed."""
    got = _readable(_PAGE.format("body text"), article="42 degrees", monkeypatch=monkeypatch)
    # Title first (so a weather page's city is known), then the fragment, then the
    # plain strip fills the rest. The fragment leads the *content*, ahead of the
    # navigation the plain strip still carries.
    assert got.startswith("The Title\n\n42 degrees"), got[:60]
    assert got.index("42 degrees") < got.index("body text"), "fragment must precede the fill"
    assert "body text" in got, "the rest of the budget should still be filled"


def test_no_extraction_falls_back_to_the_plain_strip(monkeypatch):
    got = _readable(_PAGE.format("body text"), article=None, monkeypatch=monkeypatch)
    assert "body text" in got
    assert "Jump to content" in got, "fallback is the old behaviour, unchanged"


def test_the_title_is_prepended_when_the_article_lacks_it(monkeypatch):
    """A weather page extracts to "中雨 24℃ 晴转多云" — correct, and useless
    without knowing which city it is about."""
    got = _readable(_PAGE.format("x"), article="24℃ 晴转多云 " * 20, monkeypatch=monkeypatch)
    assert got.startswith("The Title")


def test_the_title_is_not_paid_for_twice(monkeypatch):
    article = "The Title\n\n" + ("prose about the subject. " * 20)
    got = _readable(_PAGE.format("x"), article=article, monkeypatch=monkeypatch)
    assert got.count("The Title") == 1


# ------------------------------------------------------------- content typing

@pytest.mark.parametrize("content_type", [
    "application/json", "text/plain", "application/xml", "text/csv",
    "application/rss+xml", "text/x-python",
])
def test_non_html_never_reaches_the_article_extractor(content_type, monkeypatch):
    """web_fetch is also how the model reads a JSON API or a raw source file.
    Running those through a "find the prose" heuristic drops fields silently,
    and the result can be long enough that no length guard would notice."""
    monkeypatch.setattr(html_extract, "extract_article",
                        lambda _h: pytest.fail("extractor ran on non-HTML"))
    payload = '{"temperature": 24, "city": "Jiaozuo"}'
    assert "24" in cap_web._readable_text(payload, content_type=content_type)


@pytest.mark.parametrize("declared", ["text/html; charset=utf-8", "application/xhtml+xml"])
def test_declared_html_reaches_the_extractor(declared, monkeypatch):
    monkeypatch.setattr(html_extract, "extract_article", lambda _h: "extracted " * 40)
    got = cap_web._readable_text(_PAGE.format("x"), content_type=declared)
    assert got.startswith("The Title") or "extracted" in got


def test_a_missing_content_type_is_sniffed(monkeypatch):
    monkeypatch.setattr(html_extract, "extract_article", lambda _h: "extracted " * 40)
    assert "extracted" in cap_web._readable_text(_PAGE.format("x"), content_type="")


def test_a_missing_content_type_on_non_html_still_skips_the_extractor(monkeypatch):
    monkeypatch.setattr(html_extract, "extract_article",
                        lambda _h: pytest.fail("extractor ran on sniffed non-HTML"))
    assert "24" in cap_web._readable_text('{"temperature": 24}', content_type="")


# --------------------------------------------------------------- kill switch

def test_the_kill_switch_returns_the_pre_extraction_behaviour(monkeypatch):
    monkeypatch.setattr(cap_web, "_EXTRACT_ARTICLE", False)
    monkeypatch.setattr(html_extract, "extract_article",
                        lambda _h: pytest.fail("extractor ran with the switch off"))
    got = cap_web._readable_text(_PAGE.format("body text"), content_type="text/html")
    assert "body text" in got and "Jump to content" in got
