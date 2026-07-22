"""Pull the readable article out of an HTML page, or say you could not.

``web_fetch`` hands the model at most 2000 characters of a page (the cap lives
downstream, in ``executor._RESULT_CHAR_CAP``). Stripping tags with a regex
spends most of that on the navigation menu: measured, "Jump to content / Main
menu / Random article / Donate / Create account / Log in" fills the first 2257
characters of an English Wikipedia article, so the article itself never arrives.
A weather page opens with "首页 预报 预警 雷达 云图 台风路径 热门城市".

This module returns only an *article candidate*. Deciding whether to use it,
mix it with the plain strip, or ignore it stays in ``web.py`` — that keeps this
module from reaching back into ``model_api_runtime.tools`` and from owning a
policy its caller is better placed to make.

Two things are deliberate and load-bearing:

- **The extractor runs in a child process.** See ``html_extract_child`` for why
  a thread is not good enough.
- **The dependency is optional.** trafilatura ships only in the worker image
  (``requirements-runner.lock``); the backend and the decrypt enclave do not
  have it. A missing dependency is an ordinary "no article" answer here, never
  an import error at startup.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

# Wall clock for the whole child: spawn, interpreter start, import, extraction.
# Not derived from "slowest of our sample pages times a factor" — a sample of
# well-behaved pages says nothing about a page chosen to be slow. It is derived
# from what the caller can afford to wait while holding a slot in a
# capacity-2 semaphore that decryption also queues on.
_CHILD_TIMEOUT_SEC = float(os.environ.get("FEEDLING_WEB_EXTRACT_TIMEOUT_SEC", "2.0"))

# Address-space ceiling for the child. A parser that would OOM the worker
# instead dies alone and we fall back. Linux honours RLIMIT_AS; macOS largely
# ignores it, which is fine — production is Linux and the timeout still applies.
_CHILD_MEMORY_BYTES = int(os.environ.get("FEEDLING_WEB_EXTRACT_MEM_BYTES", str(512 * 1024 * 1024)))

_CHILD_MODULE = "capabilities.html_extract_child"

# Prepended to the child's PYTHONPATH. Exists so the isolation tests can point
# the spawn at a throwaway script without wrapping `subprocess.run` — wrapping
# it made the tests hang under pytest's process capture, and a test harness that
# has to reimplement the thing it is testing is testing the wrong thing.
_CHILD_EXTRA_PYTHONPATH = ""


def extract_article(html: str) -> str | None:
    """The page's article text, or ``None`` when there is nothing trustworthy.

    ``None`` is an ordinary answer, not an error: plenty of URLs are not
    articles at all. Every failure mode collapses into it — timeout, non-zero
    exit, a killed child, a missing dependency — because the caller's response
    to all of them is identical, and a caller that must distinguish them would
    grow branches nobody tests.
    """
    if not html or not html.strip():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, "-m", _CHILD_MODULE],
            input=html,
            capture_output=True,
            text=True,
            timeout=_CHILD_TIMEOUT_SEC,
            # The child must not inherit the worker's environment wholesale:
            # it parses hostile input and has no business seeing provider keys
            # or database URLs. PYTHONPATH is what makes `-m` resolve.
            env={
                "PYTHONPATH": os.pathsep.join(
                    ([_CHILD_EXTRA_PYTHONPATH] if _CHILD_EXTRA_PYTHONPATH else [])
                    + (sys.path[:1] or ["."])
                ),
                "PATH": os.environ.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": os.environ.get("HOME", "/tmp"),
                # The child applies this to itself. Doing it from a preexec_fn
                # would mean running Python between fork and exec, which is not
                # safe in a threaded parent and deadlocked under pytest's IO
                # capture — a hardening measure is not worth an outage.
                "FEEDLING_WEB_EXTRACT_MEM_BYTES": str(_CHILD_MEMORY_BYTES),
            },
            check=False,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run has already killed and reaped it by the time this
        # raises, so there is no orphan burning CPU behind us.
        log.warning("[web_extract] child exceeded %.1fs, falling back", _CHILD_TIMEOUT_SEC)
        return None
    except Exception as e:  # noqa: BLE001 — spawning must never fail the fetch
        log.warning("[web_extract] child could not run: %s: %s", type(e).__name__, e)
        return None

    if proc.returncode != 0:
        # Exit 2/3 are the child's own "nothing here" codes and are expected on
        # non-article pages; anything else means it died. Log only the latter,
        # so a broken image is visible rather than silently degrading forever.
        if proc.returncode not in (2, 3):
            log.warning("[web_extract] child exit=%s stderr=%s",
                        proc.returncode, (proc.stderr or "")[:300])
        return None
    text = (proc.stdout or "").strip()
    return text or None
