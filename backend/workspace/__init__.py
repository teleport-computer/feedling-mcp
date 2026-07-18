"""Runtime V2 virtual workspace.

The workspace is deliberately separate from Memory Garden: Garden cards are
user-facing semantic memories, while ``/memory/WORKING.md`` is agent-maintained
working state.  File content is encrypted at rest by the production backend;
the model only ever sees plaintext after the runner asks the enclave to open an
entry for the current user.
"""

from workspace.backends import (  # noqa: F401
    ARTIFACTS_ROOT,
    MEMORY_ROOT,
    SKILLS_ROOT,
    WORKING_MEMORY_PATH,
    WORKSPACE_ROOT,
    InMemoryWorkspaceBackend,
    PostgresWorkspaceBackend,
    WorkspaceBackend,
    WorkspaceConflict,
    WorkspaceEntry,
    WorkspaceError,
    WorkspaceNotFound,
    WorkspaceReadOnly,
    canonical_path,
)

