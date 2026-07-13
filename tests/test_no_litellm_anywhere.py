"""Guard: no backend code imports/references the (retired) in-CVM LiteLLM
gateway module. Deploy config cleanup is a later task; this only checks
Python source under backend/."""

import pathlib
import re


def test_no_litellm_imports_in_backend():
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for py in (root / "backend").rglob("*.py"):
        text = py.read_text()
        if re.search(r"^\s*import\s+litellm\b|^\s*from\s+litellm\b", text, re.M):
            offenders.append(str(py))
        if "litellm_gateway" in text:
            offenders.append(f"{py} (references litellm_gateway)")
    assert offenders == [], f"LiteLLM references remain: {offenders}"


def test_litellm_gateway_module_deleted():
    root = pathlib.Path(__file__).resolve().parents[1]
    assert not (root / "backend/agent_runtime/litellm_gateway.py").exists()


def test_no_litellm_or_pi_flag_in_deploy_configs():
    root = pathlib.Path(__file__).resolve().parents[1]
    targets = (list((root / "deploy").rglob("*.yaml"))
               + list((root / "deploy").rglob("*.yml"))
               + [root / ".github/workflows/ci.yml", root / "deploy/Dockerfile.agent-runner"])
    offenders = []
    for cfg in targets:
        if not cfg.exists():
            continue
        text = cfg.read_text()
        if ("FEEDLING_LITELLM" in text or "FEEDLING_PI_DRIVER_ENABLE" in text
                or "litellm" in text.lower()):
            offenders.append(str(cfg.relative_to(root)))
    assert offenders == [], f"LiteLLM/pi-flag deploy refs remain: {offenders}"


def test_no_feedling_litellm_env_var_anywhere_in_python():
    # The retired in-CVM LiteLLM gateway used FEEDLING_LITELLM_* env vars
    # (FEEDLING_LITELLM_ENABLE / FEEDLING_LITELLM_BASE_URL / ...). Every
    # read/write/mention of that family must be gone from backend/tools/tests
    # Python source — locks the residue out so it can't creep back in.
    root = pathlib.Path(__file__).resolve().parents[1]
    self_path = pathlib.Path(__file__).resolve()
    offenders = []
    for sub in ("backend", "tools", "tests"):
        for py in (root / sub).rglob("*.py"):
            if py.resolve() == self_path:
                continue  # this guard file itself references the string
            text = py.read_text()
            if "FEEDLING_LITELLM" in text:
                offenders.append(str(py.relative_to(root)))
    assert offenders == [], f"FEEDLING_LITELLM references remain: {offenders}"
