"""Deterministic trusted-prefix rendering for workspace skills.

Dynamic ``/workspace``, ``/artifacts``, and working-memory files are
intentionally absent from automatic prompt construction.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from workspace.backends import SKILLS_ROOT, WorkspaceBackend


_SKILL_SENTINEL_LIMIT = 500


@dataclass(frozen=True)
class TrustedPromptBlock:
    name: str
    version: str
    content: str
    cache_key: str


def _block(name: str, revision: int, content: str) -> TrustedPromptBlock:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    version = f"v1:r{int(revision)}:{digest[:16]}"
    return TrustedPromptBlock(
        name=name,
        version=version,
        content=content,
        cache_key=f"feedling:v2:prefix:{name}:{version}",
    )


def render_trusted_prefix_blocks(
    backend: WorkspaceBackend,
) -> tuple[TrustedPromptBlock, ...]:
    """Return byte-stable skills ordered by canonical path."""
    blocks: list[TrustedPromptBlock] = []
    skills = sorted(
        backend.list(
            SKILLS_ROOT,
            recursive=True,
            limit=_SKILL_SENTINEL_LIMIT,
        ),
        key=lambda entry: entry.path,
    )
    # Fail conservatively at the sentinel instead of silently omitting a
    # policy-bearing skill from the trusted prompt. The backend API has no
    # unbounded listing mode, so an exact sentinel-sized result is ambiguous.
    if len(skills) >= _SKILL_SENTINEL_LIMIT:
        raise RuntimeError("workspace skill prompt limit exceeded")
    for meta in skills:
        entry = backend.read(meta.path)
        body = (
            f"<feedling-skill path={entry.path!r} revision={entry.revision}>\n"
            f"{entry.content}\n"
            "</feedling-skill>"
        )
        blocks.append(_block(f"skill:{entry.path}", entry.revision, body))

    return tuple(blocks)
