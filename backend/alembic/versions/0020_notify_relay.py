"""Notify Relay: push relay for self-hosted deployments.

Revision ID: 0020_notify_relay
Revises: 0019_tee_reconcile_state
Create Date: 2026-07-18

Self-hosted backends have no official APNs .p8 key (it cannot be shared), so
their users get zero pushes. The relay lets the official hosted backend push on
their behalf: the app enrolls its device token anonymously and receives a
relay auth token (``nrt_…``); the self-hosted backend then calls
``POST /v1/notify-relay/push`` with that token.

``notify_relay_configs.auth_token`` is stored in PLAINTEXT — a deliberate
departure from the api-key HMAC-pepper pattern (accounts/registry.py): the
enroll endpoint must be able to return the SAME token on re-enroll (idempotent
"already applied → show it again" UX). The token only authorizes pushing to
the holder's own device and is rate-limited, so the leak blast radius is small.

``device_token UNIQUE`` carries the idempotency: enrolling the same device
twice resolves to the existing row via ``ON CONFLICT (device_token)``.
"""

from alembic import op


revision = "0020_notify_relay"
down_revision = "0019_tee_reconcile_state"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS notify_relay_configs (
    auth_token    TEXT PRIMARY KEY,
    device_token  TEXT NOT NULL UNIQUE,
    user_id       TEXT,
    apns_env      TEXT NOT NULL DEFAULT 'production'
                  CHECK (apns_env IN ('sandbox', 'production')),
    disabled      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS notify_relay_logs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auth_token    TEXT NOT NULL,
    push_type     SMALLINT NOT NULL CHECK (push_type IN (1, 2, 3, 4)),
    target_token  TEXT NOT NULL,
    apns_env      TEXT,
    status        SMALLINT NOT NULL DEFAULT 1 CHECK (status IN (1, 2, 3)),
    err_msg       TEXT,
    content       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notify_relay_logs_token_idx
    ON notify_relay_logs (auth_token, created_at DESC);
CREATE INDEX IF NOT EXISTS notify_relay_logs_created_idx
    ON notify_relay_logs (created_at);
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notify_relay_logs")
    op.execute("DROP TABLE IF EXISTS notify_relay_configs")
