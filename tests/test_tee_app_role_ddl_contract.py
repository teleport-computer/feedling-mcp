from __future__ import annotations

from pathlib import Path
import re


def test_tee_role_bootstrap_grants_owner_membership_to_app() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "deploy/postgres/ensure-roles.sh"
    ).read_text(encoding="utf-8")

    assert re.search(
        r'GRANT \\+"\$\{POSTGRES_USER\}\\+" TO app;', source
    )
    assert "非 owner" not in source
