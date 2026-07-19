"""Build the versioned Runtime V2 E2B template.

Run from this directory with ``E2B_API_KEY`` set. Output is a JSON build lock;
its content-addressed ``template`` is the runner configuration value.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from e2b import Template, default_build_logger

from template import TEMPLATE_TAG, TEMPLATE_VERSION, template


DEFAULT_TAG = TEMPLATE_TAG


def main() -> None:
    tag = os.environ.get("FEEDLING_V2_E2B_TEMPLATE", DEFAULT_TAG).strip()
    if tag != DEFAULT_TAG:
        raise SystemExit(
            "FEEDLING_V2_E2B_TEMPLATE does not match this extractor's "
            f"content-addressed tag ({DEFAULT_TAG})"
        )
    if Template.exists(tag):
        result = {
            "template": tag,
            "content_sha256": TEMPLATE_VERSION,
            "existing": True,
        }
    else:
        info = Template.build(
            template,
            tag,
            cpu_count=1,
            memory_mb=1024,
            on_build_logs=default_build_logger(),
        )
        result = {
            "template": tag,
            "template_id": info.template_id,
            "build_id": info.build_id,
            "content_sha256": TEMPLATE_VERSION,
            "existing": False,
        }
    rendered = json.dumps(result, sort_keys=True)
    lock_path = Path(
        os.environ.get("FEEDLING_V2_E2B_BUILD_LOCK", "e2b-template-build.json")
    )
    pending_path = lock_path.with_name(f".{lock_path.name}.tmp")
    pending_path.write_text(rendered + "\n", encoding="utf-8")
    pending_path.replace(lock_path)
    print(rendered)


if __name__ == "__main__":
    main()
