"""Guard the retired application-side Redis boundary.

Redis CVM deployment artifacts remain available for audit and an explicitly
reviewed recovery.  The backend client, however, had no consumers and its
public entry point always refused to connect, so production must not silently
start carrying that implementation or dependency again.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _requirement_names(contents: str) -> set[str]:
    names: set[str] = set()
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "--")):
            continue
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if match is not None:
            names.add(match.group(0).lower().replace("_", "-"))
    return names


def test_retirement_guard_normalizes_requirement_case_and_extras() -> None:
    contents = "  Redis[hiredis]>=5\nredis_client==1\n"

    assert _requirement_names(contents) == {"redis", "redis-client"}


def test_backend_does_not_ship_the_retired_redis_client() -> None:
    assert not (REPO / "backend" / "redis_pool.py").exists()

    for relative_path in (
        "backend/requirements.txt",
        "backend/requirements.lock",
    ):
        contents = (REPO / relative_path).read_text(encoding="utf-8")
        assert "redis" not in _requirement_names(contents)


def test_redis_recovery_assets_remain_separate_from_the_backend_client() -> None:
    expected_assets = (
        "deploy/docker-compose.phala.redis.yaml",
        "deploy/redis/Dockerfile",
        "deploy/redis/entrypoint-wrapper.sh",
        "deploy/redis/redis.conf",
        "deploy/verify-redis.sh",
    )

    assert all((REPO / relative_path).is_file() for relative_path in expected_assets)
