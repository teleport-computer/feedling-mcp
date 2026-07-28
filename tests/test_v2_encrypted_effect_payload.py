from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import nacl.public

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker
from enclave import envelope as enclave_envelope
from incident_guard_reference import legacy_reply_is_degenerate


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, True),
        ("", True),
        ("   \n", True),
        (".", True),
        ("。", True),
        ("……", True),
        ("?!", True),
        ("——", True),
        (". .", True),
        ("、", True),
        ("嗯", False),
        ("在忙吗?", False),
        ("ok.", False),
        ("1", False),
        ("🌙", False),
        ("好~", False),
        ("Hi", False),
    ],
)
def test_degenerate_reply_decision_matches_runtime_v1(text, expected):
    assert legacy_reply_is_degenerate(text) is expected
    assert worker._is_degenerate_reply(text) is expected


@pytest.mark.parametrize(
    "args",
    [
        {"patch": {"signature": "new"}},
        {"self_introduction": "I am Feedling"},
        {"signature": "curious and kind"},
    ],
)
def test_identity_effect_mapping_preserves_every_advertised_form(args):
    effect_type, payload = worker._write_tool_effect_payload(
        SimpleNamespace(name="identity_patch", args=args)
    )
    assert effect_type == "identity"
    assert payload == args
    # identity_patch stays op-less so its enqueued payload is byte-for-byte the
    # legacy shape an overlapping old sink still understands (Codex C1 wiring).
    assert "op" not in payload


def test_identity_nudge_effect_mapping_carries_trusted_op_from_tool_name():
    # identity_nudge shares the `identity` effect_type/sink with identity_patch,
    # disambiguated by an `op` taken from the TOOL NAME — never from model args.
    effect_type, payload = worker._write_tool_effect_payload(
        SimpleNamespace(
            name="identity_nudge",
            args={"dimension": "trust", "delta": 3, "reason": "kept a promise"},
        )
    )
    assert effect_type == "identity"
    assert payload == {
        "dimension": "trust", "delta": 3, "reason": "kept a promise",
        "op": "identity_nudge",
    }


def test_identity_nudge_effect_mapping_op_cannot_be_overridden_by_model_args():
    # A model that smuggled an `op` into args must not be able to steer the sink:
    # {**tc.args, "op": tc.name} puts the trusted op LAST.
    _effect_type, payload = worker._write_tool_effect_payload(
        SimpleNamespace(
            name="identity_nudge",
            args={"dimension": "trust", "delta": 1, "op": "identity_patch"},
        )
    )
    assert payload["op"] == "identity_nudge"


# --- Item 1: frozen relationship anchor round-trips producer -> validator ------

def test_identity_effect_mapping_freezes_relationship_anchor():
    from datetime import date, timedelta
    effect_type, payload = worker._write_tool_effect_payload(
        SimpleNamespace(name="identity_patch", args={"patch": {"relationship_days": 90}})
    )
    assert effect_type == "identity"
    # relationship_days is the 1-based "第 N 天" (met day = 第 1 天), so N=90
    # freezes to elapsed N-1=89 → today-89.
    assert payload["relationship_started_at"] == (date.today() - timedelta(days=89)).isoformat()
    # relative value kept for audit; frozen absolute is the trusted metadata.
    assert payload["patch"]["relationship_days"] == 90


def test_validate_identity_effect_accepts_frozen_anchor_metadata():
    # The trusted frozen anchor must pass the decrypted-effect re-validation even
    # though the model-facing top-level schema is additionalProperties=false — it
    # is stripped before that check, not fed to it.
    serve_worker._validate_decrypted_tool_effect(
        "identity",
        {"effect_id": "e", "patch": {"relationship_days": 90},
         "relationship_started_at": "2026-04-10"},
    )  # must not raise


def test_validate_identity_effect_rejects_malformed_frozen_anchor():
    with pytest.raises(RuntimeError, match="invalid encrypted identity anchor"):
        serve_worker._validate_decrypted_tool_effect(
            "identity",
            {"effect_id": "e", "patch": {"relationship_days": 90},
             "relationship_started_at": "some day"},
        )


@pytest.mark.parametrize("bad", ["2026-04-10garbage", "2026-04-10T00:00:00", "20260410"])
def test_validate_identity_effect_rejects_non_canonical_frozen_anchor(bad):
    # Round-3 fix: the old `[:10]` slice accepted a canonical prefix followed by
    # junk (or a datetime). Full-string parse + round-trip now rejects anything
    # that is not exactly YYYY-MM-DD (the only shape the producer ever emits).
    with pytest.raises(RuntimeError, match="invalid encrypted identity anchor"):
        serve_worker._validate_decrypted_tool_effect(
            "identity",
            {"effect_id": "e", "patch": {"relationship_days": 90},
             "relationship_started_at": bad},
        )


def test_validate_identity_effect_accepts_canonical_frozen_anchor():
    # The canonical date the producer emits still passes.
    serve_worker._validate_decrypted_tool_effect(
        "identity",
        {"effect_id": "e", "patch": {"relationship_days": 90},
         "relationship_started_at": "2026-04-10"},
    )  # must not raise


# --- _validate_decrypted_tool_effect: identity op routing (Codex C1) ----------

def test_validate_identity_effect_accepts_legacy_patch_without_op():
    # Every pre-nudge / in-flight identity effect has NO op key and must keep
    # validating as identity_patch.
    serve_worker._validate_decrypted_tool_effect(
        "identity", {"effect_id": "e", "patch": {"signature": "kind"}}
    )  # must not raise


def test_validate_identity_effect_accepts_explicit_patch_op():
    serve_worker._validate_decrypted_tool_effect(
        "identity",
        {"effect_id": "e", "op": "identity_patch", "self_introduction": "hi"},
    )  # must not raise


def test_validate_identity_effect_accepts_nudge_op():
    serve_worker._validate_decrypted_tool_effect(
        "identity",
        {"effect_id": "e", "op": "identity_nudge", "dimension": "trust", "delta": 2},
    )  # must not raise


def test_validate_identity_effect_rejects_unknown_op_fail_closed():
    with pytest.raises(RuntimeError, match="invalid encrypted identity operation"):
        serve_worker._validate_decrypted_tool_effect(
            "identity",
            {"effect_id": "e", "op": "identity_wipe", "dimension": "trust"},
        )


def test_validate_identity_nudge_effect_rejects_bad_nudge_args():
    # op is trusted, but the nudge args still cross the model boundary and are
    # re-checked against the identity_nudge schema (delta must be an integer).
    with pytest.raises(RuntimeError, match="invalid encrypted effect arguments"):
        serve_worker._validate_decrypted_tool_effect(
            "identity",
            {"effect_id": "e", "op": "identity_nudge",
             "dimension": "trust", "delta": "lots"},
        )


def test_tool_effect_builder_persists_only_ciphertext_and_uses_stable_id(monkeypatch):
    calls = []

    def fake_build(store, plaintext, *, item_id=None):
        calls.append((store.user_id, plaintext, item_id))
        return ({
            "id": item_id,
            "owner_user_id": store.user_id,
            "body_ct": "opaque-ciphertext",
        }, "")

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        fake_build,
    )
    store = SimpleNamespace(user_id="u_effect_cipher")
    payload = {"actions": [{"text": "private marker", "op": "add"}]}

    first = worker._build_encrypted_tool_effect_payload(
        store,
        payload,
        effect_id="effect-123",
    )
    second = worker._build_encrypted_tool_effect_payload(
        store,
        payload,
        effect_id="effect-123",
    )

    expected_plaintext = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_item_id = hashlib.sha256(
        b"v2-tool-effect:effect-123"
    ).hexdigest()[:32]
    assert first == second
    assert "private marker" not in json.dumps(first)
    assert calls == [
        ("u_effect_cipher", expected_plaintext, expected_item_id),
        ("u_effect_cipher", expected_plaintext, expected_item_id),
    ]


def test_tool_effect_payload_real_crypto_round_trip(monkeypatch):
    user_id = "u_effect_real_crypto"
    user_key = nacl.public.PrivateKey.generate()
    enclave_key = nacl.public.PrivateKey.generate()
    monkeypatch.setattr(
        worker.core_envelope,
        "get_user_public_key",
        lambda requested_user_id: (
            base64.b64encode(bytes(user_key.public_key)).decode("ascii")
            if requested_user_id == user_id else ""
        ),
    )
    monkeypatch.setattr(
        worker.core_envelope.enclave,
        "_get_enclave_info",
        lambda: {
            "content_pk_hex": bytes(enclave_key.public_key).hex(),
            "compose_hash": "test",
        },
    )
    plaintext_payload = {
        "signature": "a genuinely encrypted private signature",
    }
    effect_id = "job-real-crypto:identity_encrypted_v1:0"

    stored = worker._build_encrypted_tool_effect_payload(
        SimpleNamespace(user_id=user_id),
        plaintext_payload,
        effect_id=effect_id,
    )
    envelope = stored["effect_envelope"]

    assert plaintext_payload["signature"] not in json.dumps(stored)
    assert envelope["id"] == worker._tool_effect_item_id(effect_id)
    decrypted = enclave_envelope.decrypt_envelope(
        envelope,
        user_id,
        enclave_key,
    )
    assert json.loads(decrypted.decode("utf-8")) == plaintext_payload


def test_tool_effect_builder_fails_closed_when_envelope_cannot_be_built(monkeypatch):
    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda *args, **kwargs: (None, "no shared key"),
    )
    with pytest.raises(RuntimeError, match="no shared key"):
        worker._build_encrypted_tool_effect_payload(
            SimpleNamespace(user_id="u_no_key"),
            {"patch": {"signature": "secret"}},
            effect_id="effect-no-key",
        )


def test_tool_effect_decrypt_uses_enclave_and_trusts_outer_effect_id(monkeypatch):
    calls = []

    def fake_decrypt(envelope, api_key, *, purpose, runtime_token):
        calls.append((envelope, api_key, purpose, runtime_token))
        return json.dumps({
            "actions": [{"op": "add", "text": "likes tea"}],
            "effect_id": "attacker-controlled",
        }).encode("utf-8")

    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        fake_decrypt,
    )
    envelope = {
        "id": worker._tool_effect_item_id("trusted-row-id"),
        "owner_user_id": "u_decrypt",
        "body_ct": "ciphertext",
    }
    decoded = serve_worker._decrypt_tool_effect_payload(
        "u_decrypt",
        {"effect_envelope": envelope, "effect_id": "trusted-row-id"},
        runtime_token="runtime-token",
    )

    assert decoded == {
        "actions": [{"op": "add", "text": "likes tea"}],
        "effect_id": "trusted-row-id",
    }
    assert calls == [(envelope, None, "v2_effect_apply", "runtime-token")]


@pytest.mark.parametrize(
    "payload, message",
    [
        (
            {
                "effect_envelope": {"owner_user_id": "u_shape"},
                "effect_id": "eid",
                "actions": [],
            },
            "shape",
        ),
        (
            {"effect_envelope": "not-an-envelope", "effect_id": "eid"},
            "invalid encrypted effect payload",
        ),
        (
            {
                "effect_envelope": {
                    "id": worker._tool_effect_item_id("different-row"),
                    "owner_user_id": "u_shape",
                },
                "effect_id": "eid",
            },
            "id mismatch",
        ),
        (
            {
                "effect_envelope": {
                    "id": worker._tool_effect_item_id("eid"),
                    "owner_user_id": "somebody-else",
                },
                "effect_id": "eid",
            },
            "owner mismatch",
        ),
    ],
)
def test_tool_effect_decrypt_rejects_malformed_or_cross_user_payloads(
    payload,
    message,
):
    with pytest.raises(RuntimeError, match=message):
        serve_worker._decrypt_tool_effect_payload(
            "u_shape",
            payload,
            runtime_token="runtime-token",
        )


def test_production_applier_decrypts_tool_effects_with_one_lazy_token(monkeypatch):
    delivered = []
    minted = []
    decrypt_calls = []
    monkeypatch.setattr(
        serve_worker,
        "build_production_effect_dispatch",
        lambda user_id, **kwargs: lambda effect_type, payload: delivered.append(
            (user_id, effect_type, payload)
        ),
    )
    monkeypatch.setattr(
        serve_worker,
        "_mint_runtime_token",
        lambda user_id: minted.append(user_id) or "minted-token",
    )

    def fake_decrypt(value, api_key, *, purpose, runtime_token):
        decrypt_calls.append((value, api_key, purpose, runtime_token))
        if value["body_ct"] == "memory":
            return (
                b'{"actions":[{"type":"memory.add","memory":'
                b'{"type":"fact","title":"private","description":"private"}}]}'
            )
        return b'{"signature":"private"}'

    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        fake_decrypt,
    )

    def fake_apply(user_id, *, dispatch, dispatch_reply_in_transaction=None):
        for logical_type, effect_id in (("memory", "eid-1"), ("identity", "eid-2")):
            dispatch(worker.ENCRYPTED_TOOL_EFFECT_TYPES[logical_type], {
                "effect_envelope": {
                    "id": worker._tool_effect_item_id(effect_id),
                    "owner_user_id": "u_apply",
                    "body_ct": logical_type,
                },
                "effect_id": effect_id,
            })
        return {"applied": 2, "discarded": 0}

    monkeypatch.setattr(
        serve_worker.v2_effect_outbox,
        "apply_pending_effects",
        fake_apply,
    )

    result = serve_worker._apply_pending_effects_for_user("u_apply")

    assert result == {"applied": 2, "discarded": 0}
    assert minted == ["u_apply"]
    assert len(decrypt_calls) == 2
    assert delivered == [
        (
            "u_apply",
            "memory",
            {
                "actions": [{
                    "type": "memory.add",
                    "memory": {
                        "type": "fact",
                        "title": "private",
                        "description": "private",
                    },
                }],
                "effect_id": "eid-1",
            },
        ),
        (
            "u_apply",
            "identity",
            {"signature": "private", "effect_id": "eid-2"},
        ),
    ]


def test_production_applier_replays_nudge_and_legacy_patch_through_validation(monkeypatch):
    """Full-chain replay (Codex C1): decrypt -> _validate_decrypted_tool_effect
    -> dispatch, for BOTH a new identity_nudge effect (carries `op`) and an old
    identity_patch effect enqueued before the op key existed (NO `op`). Both
    must survive the real validator and reach the sink with `identity` logical
    type; the nudge keeps its op so the sink can route it."""
    delivered = []
    monkeypatch.setattr(
        serve_worker,
        "build_production_effect_dispatch",
        lambda user_id, **kwargs: lambda effect_type, payload: delivered.append(
            (effect_type, payload)
        ),
    )
    monkeypatch.setattr(
        serve_worker, "_mint_runtime_token", lambda user_id: "minted-token"
    )

    def fake_decrypt(value, api_key, *, purpose, runtime_token):
        if value["body_ct"] == "nudge":
            return b'{"op":"identity_nudge","dimension":"trust","delta":2}'
        return b'{"patch":{"signature":"kind"}}'  # legacy patch, no op

    monkeypatch.setattr(
        serve_worker.core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt
    )

    def fake_apply(user_id, *, dispatch, dispatch_reply_in_transaction=None):
        for body_ct, effect_id in (("nudge", "eid-nudge"), ("patch", "eid-patch")):
            dispatch(worker.ENCRYPTED_TOOL_EFFECT_TYPES["identity"], {
                "effect_envelope": {
                    "id": worker._tool_effect_item_id(effect_id),
                    "owner_user_id": "u_replay",
                    "body_ct": body_ct,
                },
                "effect_id": effect_id,
            })
        return {"applied": 2, "discarded": 0}

    monkeypatch.setattr(
        serve_worker.v2_effect_outbox, "apply_pending_effects", fake_apply
    )

    result = serve_worker._apply_pending_effects_for_user("u_replay")

    assert result == {"applied": 2, "discarded": 0}
    assert delivered == [
        ("identity", {"op": "identity_nudge", "dimension": "trust", "delta": 2,
                      "effect_id": "eid-nudge"}),
        ("identity", {"patch": {"signature": "kind"}, "effect_id": "eid-patch"}),
    ]


def test_production_applier_rejects_malformed_encrypted_schedule(monkeypatch):
    delivered = []
    effect_id = "eid-bad-schedule"
    envelope = {
        "id": worker._tool_effect_item_id(effect_id),
        "owner_user_id": "u_bad_schedule",
        "body_ct": "ciphertext",
    }
    monkeypatch.setattr(
        serve_worker,
        "build_production_effect_dispatch",
        lambda user_id, **kwargs: lambda effect_type, payload: delivered.append(
            (effect_type, payload)
        ),
    )
    monkeypatch.setattr(
        serve_worker,
        "_mint_runtime_token",
        lambda user_id: "minted-token",
    )
    monkeypatch.setattr(
        serve_worker.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *args, **kwargs: b'{}',
    )

    def fake_apply(user_id, *, dispatch, dispatch_reply_in_transaction=None):
        dispatch(worker.ENCRYPTED_TOOL_EFFECT_TYPES["schedule"], {
            "effect_envelope": envelope,
            "effect_id": effect_id,
        })
        return {"applied": 1, "discarded": 0}

    monkeypatch.setattr(
        serve_worker.v2_effect_outbox,
        "apply_pending_effects",
        fake_apply,
    )

    with pytest.raises(RuntimeError, match="invalid encrypted schedule operation"):
        serve_worker._apply_pending_effects_for_user("u_bad_schedule")
    assert delivered == []


def test_production_applier_keeps_legacy_payloads_without_minting_token(monkeypatch):
    delivered = []
    monkeypatch.setattr(
        serve_worker,
        "build_production_effect_dispatch",
        lambda user_id, **kwargs: lambda effect_type, payload: delivered.append(
            (effect_type, payload)
        ),
    )
    monkeypatch.setattr(
        serve_worker,
        "_mint_runtime_token",
        lambda user_id: pytest.fail("legacy payload must not mint a decrypt token"),
    )

    def fake_apply(user_id, *, dispatch, dispatch_reply_in_transaction=None):
        dispatch("memory", {"actions": [], "effect_id": "legacy-id"})
        return {"applied": 1, "discarded": 0}

    monkeypatch.setattr(
        serve_worker.v2_effect_outbox,
        "apply_pending_effects",
        fake_apply,
    )

    assert serve_worker._apply_pending_effects_for_user("u_legacy") == {
        "applied": 1,
        "discarded": 0,
    }
    assert delivered == [
        ("memory", {"actions": [], "effect_id": "legacy-id"}),
    ]
