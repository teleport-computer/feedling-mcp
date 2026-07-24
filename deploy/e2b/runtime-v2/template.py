"""Content-addressed E2B template for Runtime V2 artifact processing."""

from __future__ import annotations

import hashlib
from pathlib import Path

from e2b import Template


_ROOT = Path(__file__).resolve().parent
BASE_IMAGE = (
    "python:3.11.9-slim-bookworm@"
    "sha256:8fb099199b9f2d70342674bd9dbccd3ed03a258f26bbd1d556822c6dfc60c317"
)
_BUILD_CONTRACT = b"\n".join(
    (
        b"feedling-runtime-v2-artifacts-v1",
        f"base={BASE_IMAGE}".encode(),
        b"pypdf=5.9.0",
        b"extractor=/opt/feedling/bin/extract-artifact",
    )
)
TEMPLATE_VERSION = hashlib.sha256(
    _BUILD_CONTRACT + b"\0" + (_ROOT / "extract_artifact.py").read_bytes()
).hexdigest()
TEMPLATE_TAG = f"feedling-runtime-v2-artifacts-v1-{TEMPLATE_VERSION}"


template = (
    Template()
    .from_image(BASE_IMAGE)
    .pip_install(["pypdf==5.9.0"])
    .make_dir("/opt/feedling/bin", mode=0o755)
    .run_cmd(
        "printf '%s\\n' "
        f"'{TEMPLATE_VERSION}' > /opt/feedling/TEMPLATE_VERSION"
    )
    .copy(
        "extract_artifact.py",
        "/opt/feedling/bin/extract-artifact",
        user="root",
        mode=0o755,
    )
)
