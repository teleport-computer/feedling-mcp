"""Add plaintext frame storage and bound infrastructure evidence.

Revision ID: 0027_plaintext_shadow_gates
Revises: 0026_plaintext_shadow_control

Old restore-evidence rows predate the endpoint/capacity/HA binding contract and
cannot safely authorize Gate 2, so this migration invalidates them explicitly.
"""

from alembic import op


revision = "0027_plaintext_shadow_gates"
down_revision = "0026_plaintext_shadow_control"
branch_labels = None
depends_on = None


_UP = r"""
ALTER TABLE frames ADD COLUMN IF NOT EXISTS body_plaintext BYTEA;

TRUNCATE TABLE plaintext_shadow_restore_evidence;
ALTER TABLE plaintext_shadow_restore_evidence
    ADD COLUMN IF NOT EXISTS backup_artifact_digest TEXT NOT NULL,
    ADD COLUMN IF NOT EXISTS target_fingerprint TEXT NOT NULL,
    ADD COLUMN IF NOT EXISTS target_capacity_bytes BIGINT NOT NULL,
    ADD COLUMN IF NOT EXISTS target_connection_limit INTEGER NOT NULL,
    ADD COLUMN IF NOT EXISTS ha_verified BOOLEAN NOT NULL,
    ADD COLUMN IF NOT EXISTS attestation_key_fingerprint TEXT NOT NULL,
    ADD COLUMN IF NOT EXISTS attestation_signature_digest TEXT NOT NULL,
    ADD COLUMN IF NOT EXISTS attestation_payload BYTEA,
    ADD COLUMN IF NOT EXISTS attestation_signature BYTEA;

ALTER TABLE plaintext_shadow_restore_evidence
    ADD CONSTRAINT plaintext_shadow_restore_bound_scalars_nonempty
        CHECK (backup_artifact_digest <> '' AND target_fingerprint <> ''
               AND attestation_key_fingerprint <> ''
               AND attestation_signature_digest <> ''),
    ADD CONSTRAINT plaintext_shadow_restore_capacity_positive
        CHECK (target_capacity_bytes > 0 AND target_connection_limit > 0),
    ADD CONSTRAINT plaintext_shadow_restore_attestation_pair
        CHECK ((attestation_payload IS NULL AND attestation_signature IS NULL)
               OR (octet_length(attestation_payload) > 0
                   AND octet_length(attestation_signature) = 64));

CREATE OR REPLACE FUNCTION feedling_record_plaintext_shadow_restore_evidence(
    p_payload BYTEA,
    p_signature BYTEA,
    p_key_fingerprint TEXT,
    p_signature_digest TEXT,
    p_operator_id TEXT
) RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    claims JSONB;
    evidence_id BIGINT;
BEGIN
    claims := convert_from(p_payload, 'UTF8')::jsonb;
    INSERT INTO public.plaintext_shadow_restore_evidence
        (restored_at, source_backup_at, schema_head, verifier_digest,
         backup_artifact_digest, target_fingerprint, target_capacity_bytes,
         target_connection_limit, ha_verified, attestation_key_fingerprint,
         attestation_signature_digest, operator_id, expires_at,
         attestation_payload, attestation_signature)
    VALUES
        ((claims->>'restored_at')::timestamptz,
         (claims->>'source_backup_at')::timestamptz,
         claims->>'schema_head', claims->>'verifier_digest',
         claims->>'backup_artifact_digest', claims->>'target_fingerprint',
         (claims->>'target_capacity_bytes')::bigint,
         (claims->>'target_connection_limit')::integer,
         (claims->>'ha_verified')::boolean, p_key_fingerprint,
         p_signature_digest, p_operator_id,
         (claims->>'expires_at')::timestamptz, p_payload, p_signature)
    RETURNING id INTO evidence_id;
    RETURN evidence_id;
END;
$$;

REVOKE ALL ON FUNCTION feedling_record_plaintext_shadow_restore_evidence(
    BYTEA, BYTEA, TEXT, TEXT, TEXT
) FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON TABLE plaintext_shadow_restore_evidence FROM PUBLIC;

DO $grant_app$
DECLARE
    role_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['app', 'tee_replicator'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE '
                'public.plaintext_shadow_restore_evidence FROM %I', role_name
            );
            EXECUTE format(
                'GRANT SELECT ON TABLE '
                'public.plaintext_shadow_restore_evidence TO %I', role_name
            );
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION '
                'public.feedling_record_plaintext_shadow_restore_evidence('
                'BYTEA, BYTEA, TEXT, TEXT, TEXT) TO app';
    END IF;
END
$grant_app$;
"""


_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0027_plaintext_shadow_gates"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
