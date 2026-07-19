from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).parent.parent
TEMPLATE_DIR = ROOT / "deploy" / "e2b" / "runtime-v2"


def _template_module():
    spec = importlib.util.spec_from_file_location(
        "feedling_e2b_template_contract",
        TEMPLATE_DIR / "template.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tracked_template_tag_matches_extractor_and_pinned_contract():
    module = _template_module()
    expected = hashlib.sha256(
        module._BUILD_CONTRACT
        + b"\0"
        + (TEMPLATE_DIR / "extract_artifact.py").read_bytes()
    ).hexdigest()
    assert module.TEMPLATE_VERSION == expected
    assert module.TEMPLATE_TAG.endswith(expected)
    assert "@sha256:" in module.BASE_IMAGE
    assert len(module.BASE_IMAGE.rsplit("@sha256:", 1)[1]) == 64
    assert (TEMPLATE_DIR / "template-tag.txt").read_text().strip() == module.TEMPLATE_TAG
