"""Article extraction, run as a throwaway child process. Never imported.

Reads HTML on stdin, writes the extracted text on stdout, exits non-zero when
it has nothing. The parent (``html_extract.py``) treats *any* failure — non-zero
exit, timeout, a signal, garbage on stdout — as "no article", so nothing in here
needs to be defensive about its own error reporting.

Why a separate process at all, when a thread would be less machinery: this is
the only part of ``web_fetch`` that runs a C parser over bytes an attacker
chose. Measured on real inputs at our 300 KB cap, a page of repeated malformed
close-tags takes ~0.6 s and ~135 MB against ~0.011 s for a normal page — 50x,
and that is on a dev laptop rather than the 1-vCPU runner. The extraction runs
inside the shared ENCLAVE_SEMAPHORE (capacity 2, also used by decryption), so a
couple of hostile pages must not be able to sit on it. A thread cannot be
killed; a process can. ``asyncio.wait_for`` around ``to_thread`` would only
*stop waiting* — the work would keep burning the CPU it was supposed to release.
"""

from __future__ import annotations

import sys


def _self_limit() -> None:
    """Cap our own address space before touching the parser.

    Applied here rather than from the parent's `preexec_fn`: running Python
    between fork and exec is unsafe in a threaded parent, and it deadlocked
    under pytest's IO capture. Best-effort — macOS refuses RLIMIT_AS, and the
    parent's wall timeout is the guarantee either way.
    """
    import os

    try:
        import resource

        cap = int(os.environ.get("FEEDLING_WEB_EXTRACT_MEM_BYTES", "0"))
        if cap > 0:
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _self_limit()
    html = sys.stdin.read()
    if not html.strip():
        return 2
    import trafilatura  # noqa: PLC0415 — deliberately inside the child

    text = trafilatura.extract(
        html,
        include_comments=False,
        # Tables first yield Wikipedia's infobox and its "this article has
        # multiple issues" maintenance banner — hundreds of characters out of a
        # 2000-character budget, before any prose.
        include_tables=False,
        # Headings are nearly free (+0.4% measured) and give the model the
        # document's shape; links cost +54% and it cannot click them.
        output_format="markdown",
        include_links=False,
    )
    if not text:
        return 3
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
