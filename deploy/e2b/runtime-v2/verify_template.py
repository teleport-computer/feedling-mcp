"""Create one fixed E2B canary and prove the audited extractor contract."""

from __future__ import annotations

import json
import os

from e2b import Sandbox

from template import TEMPLATE_TAG, TEMPLATE_VERSION


VERSION_PATH = "/opt/feedling/TEMPLATE_VERSION"
ARTIFACT_PATH = "/tmp/feedling-artifact.bin"
META_PATH = "/tmp/feedling-artifact-meta.json"
TEXT_PATH = "/tmp/feedling-artifact-text.utf8"
EXTRACT_COMMAND = "/opt/feedling/bin/extract-artifact"
MARKER = "feedling-runtime-v2-e2b-canary"


def main() -> None:
    configured = os.environ.get("FEEDLING_V2_E2B_TEMPLATE", TEMPLATE_TAG).strip()
    if configured != TEMPLATE_TAG:
        raise SystemExit(
            f"configured template {configured!r} does not match {TEMPLATE_TAG!r}"
        )
    sandbox = Sandbox.create(
        template=configured,
        timeout=120,
        secure=True,
        allow_internet_access=False,
        api_key=os.environ.get("E2B_API_KEY", "").strip() or None,
    )
    try:
        version = sandbox.files.read(VERSION_PATH)
        if isinstance(version, bytes):
            version = version.decode("ascii", errors="strict")
        if str(version).strip() != TEMPLATE_VERSION:
            raise RuntimeError("E2B template version mismatch")
        sandbox.files.write(ARTIFACT_PATH, MARKER.encode())
        sandbox.files.write(
            META_PATH,
            json.dumps(
                {"name": "canary.txt", "mime_type": "text/plain"},
                separators=(",", ":"),
            ).encode(),
        )
        sandbox.files.write(TEXT_PATH, b"")
        result = sandbox.commands.run(EXTRACT_COMMAND, timeout=30)
        if result.exit_code != 0:
            raise RuntimeError(f"extractor failed: {result.stderr}")
        extracted = sandbox.files.read(TEXT_PATH)
        if isinstance(extracted, bytes):
            extracted = extracted.decode("utf-8", errors="strict")
        if str(extracted) != MARKER:
            raise RuntimeError("E2B extractor returned unexpected text")
        python_result = sandbox.commands.run(
            "python -c \"print('feedling-python-canary')\"",
            timeout=30,
        )
        if python_result.exit_code != 0 or "feedling-python-canary" not in str(
            python_result.stdout
        ):
            raise RuntimeError("E2B Python runtime canary failed")
        print(
            json.dumps(
                {
                    "ok": True,
                    "template": configured,
                    "content_sha256": TEMPLATE_VERSION,
                },
                sort_keys=True,
            )
        )
    finally:
        sandbox.kill()


if __name__ == "__main__":
    main()
