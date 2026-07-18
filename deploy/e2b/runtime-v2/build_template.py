"""Build the versioned Runtime V2 E2B template.

Run from this directory with ``E2B_API_KEY`` set. The printed tag is the value
to store in ``FEEDLING_V2_E2B_TEMPLATE`` on the runner deployment.
"""

from __future__ import annotations

import os

from e2b import Template, default_build_logger

from template import template


DEFAULT_TAG = "feedling-runtime-v2-artifacts-v1"


def main() -> None:
    tag = os.environ.get("FEEDLING_V2_E2B_TEMPLATE", DEFAULT_TAG).strip()
    if not tag:
        raise SystemExit("FEEDLING_V2_E2B_TEMPLATE must not be empty")
    Template.build(
        template,
        tag,
        cpu_count=1,
        memory_mb=1024,
        on_build_logs=default_build_logger(),
    )
    print(tag)


if __name__ == "__main__":
    main()

