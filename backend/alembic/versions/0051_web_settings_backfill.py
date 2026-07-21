"""0051 web-search toggle: one-time backfill so existing users keep what they had.

Web search shipped unconditionally on the V2 runtime, so the users already on
that runtime have been searching all along. Introducing the toggle with a
default of OFF would silently take that away — replies would stop using live
results and the only way back is a settings screen those users have no reason to
visit. This migration writes an explicit `enabled: true` for them, once.

Deliberately NOT "a missing blob means on": the code default stays `false`, so
new accounts are opt-in and there is no permanent "absence == enabled" rule to
unwind later. Only the accounts that exist at this moment get a row.

Scope is the users whose PERSISTED target runtime is V2 (`hosted_runtime_mode =
db_action_v2`). Deliberately not also requiring `hosted_runtime_state = 'v2'`:
that column is transient ownership/fence state and can legitimately read
`draining` mid-handover, so an existing V2 user who happened to be draining at
migration time would be skipped and then lose a capability they had the moment
they settle back. The persisted mode expresses intent; the state is just where
the handover currently is.

Self-hosted / `resident_cli` users never had these tools (their consumer does
not run the V2 tool loop), so writing `true` for them would not preserve their
status quo — it would hand them something new the day they migrate.

`ON CONFLICT DO NOTHING` so a user who somehow already expressed a preference
is never overwritten.

Revision ID: 0051_web_settings_backfill
"""
from alembic import op

revision = "0051_web_settings_backfill"
down_revision = "0050_v2_web_halted_columns"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        INSERT INTO user_blobs (user_id, kind, doc)
        SELECT users.user_id,
               'web_settings',
               '{"version": 1, "enabled": true}'::jsonb
        FROM users
        LEFT JOIN user_blobs AS runtime_blob
          ON runtime_blob.user_id = users.user_id
         AND runtime_blob.kind = 'model_api_runtime'
        WHERE COALESCE(runtime_blob.doc->>'hosted_runtime_mode', 'resident_cli')
              = 'db_action_v2'
        ON CONFLICT (user_id, kind) DO NOTHING
        """
    )


def downgrade():
    # Intentionally a no-op. There is no way to tell a row this migration wrote
    # from one the user set themselves — matching on the document value would
    # also delete the preference of anyone who has since switched web on. A data
    # backfill must not guess at ownership and delete user settings; leaving the
    # rows behind is harmless (the code default only applies when a row is
    # absent), while deleting them is not reversible.
    pass
