"""Strict, snapshot-based admission for uploaded Genesis jobs without enclave."""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import psycopg

import genesis_managed_store as persistence
from accounts import registry
from core import envelope
from genesis import service
from memory import garden_import, service as memory_service
import memory_readside_core

Unavailable = persistence.DependencyUnavailable


def require_policy(user_id: str):
    if (not envelope.PLAINTEXT_WRITES_ACCEPTED
            or registry.effective_content_encryption(user_id) != "off"
            or envelope.resolve_content_encryption(user_id) != "off"):
        raise Unavailable("account_policy", "dependency_unavailable")


def plaintext(value, user_id, dependency):
    shape = envelope.classify_envelope_shape(value)
    if shape == "sealed":
        raise Unavailable(dependency, "enclave_required")
    try:
        return envelope.read_plaintext_envelope_body(value, owner_user_id=user_id).decode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise Unavailable(dependency, "dependency_invalid") from None


def object_body(value, user_id, dependency):
    try:
        parsed = json.loads(plaintext(value, user_id, dependency))
        if not isinstance(parsed, dict):
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise Unavailable(dependency, "dependency_invalid") from None


def local_index(moments, user_id):
    rows = [memory_service.to_v1_card(row) for row in moments]
    for row in rows:
        if not isinstance(row, dict):
            raise Unavailable("garden", "dependency_invalid")
        # Apply existing visibility/lifecycle rules independently of readability.
        # Otherwise missing/foreign bodies disappear before strict validation.
        lifecycle_probe = {**row, "owner_user_id": user_id, "body": "{}"}
        if memory_readside_core.memory_available(lifecycle_probe, user_id):
            object_body(row, user_id, "garden")
    # This admission needs the complete corpus, not the public recall window.
    candidates, count = memory_readside_core.readside_candidates(rows, user_id, exact_query=True)
    if len(candidates) != count:
        raise Unavailable("garden", "dependency_unavailable")

    def no_enclave(*_args, **_kwargs):
        raise Unavailable("garden", "enclave_required")

    items = memory_readside_core._memory_index_partition(
        None, candidates, user_id, {"limit": count}, post=no_enclave,
    )
    if len(items) != len(candidates):
        raise Unavailable("garden", "dependency_invalid")
    return garden_import.index_cards(items)


@dataclass(repr=False)
class Prepared:
    """Contents/credentials deliberately excluded from repr and persistence."""
    runtime: object
    texts: list[str]
    persona: dict
    voice: dict
    cards: list[dict]
    identity: dict | None
    locale: str


def prepare(raw, authenticated_envelope):
    from genesis import worker

    job = raw["job"]
    uid = job["user_id"]
    require_policy(uid)
    if not isinstance(raw["user"], dict):
        raise Unavailable("account_policy")
    route = raw["route"]
    if not isinstance(route, dict) or not isinstance(authenticated_envelope, dict):
        raise Unavailable("provider_credential")
    key_envelope = route.get("api_key_envelope")
    if key_envelope != authenticated_envelope:
        raise Unavailable("provider_credential", "dependency_changed")
    key_shape = envelope.classify_envelope_shape(key_envelope)
    if key_shape == "sealed":
        raise Unavailable("provider_credential", "enclave_required")
    # Existing provider-key envelopes do not necessarily carry content ownership.
    # The authenticated endpoint and the same-snapshot route JOIN establish it.
    if key_shape != "plaintext_text":
        raise Unavailable("provider_credential", "dependency_invalid")
    key = key_envelope["body"]
    if not key.strip():
        raise Unavailable("provider_credential", "dependency_invalid")
    try:
        runtime = worker._runtime_from_config(route, key)
    except Exception:
        raise Unavailable("provider_credential", "dependency_invalid") from None
    chunks = raw["chunks"]
    total = job.get("total_chunks")
    if not isinstance(total, int) or total <= 0 or [c["seq"] for c in chunks] != list(range(total)):
        raise Unavailable("chunk", "dependency_invalid")
    texts = []
    for chunk in chunks:
        try:
            restored = service.chunk_envelope_from_row(chunk)
        except (TypeError, ValueError, UnicodeError):
            raise Unavailable("chunk", "dependency_invalid") from None
        texts.append(plaintext(restored, uid, "chunk"))
    blobs = raw["blobs"]
    persona = {}
    old_persona = blobs[service.GENESIS_PERSONA_BLOB]
    if old_persona is not None:
        if not isinstance(old_persona, dict):
            raise Unavailable("persona", "dependency_invalid")
        # Validate even the lower-priority old artifact; never certify a failed read.
        text = plaintext(old_persona.get("content_envelope"), uid, "persona")
        try:
            priority = int(old_persona.get("source_priority") or 0)
        except (ValueError, TypeError):
            raise Unavailable("persona", "dependency_invalid") from None
        if priority >= 100 and text.strip():
            persona = {"content": text.strip(), "source_priority": priority,
                       "source_family": str(old_persona.get("source_family") or "")}
    voice = {}
    old_voice = blobs[service.GENESIS_VOICE_BLOB]
    if old_voice is not None:
        if not isinstance(old_voice, dict):
            raise Unavailable("voice", "dependency_invalid")
        voice = object_body(old_voice.get("content_envelope"), uid, "voice")
    identity = blobs["identity"]
    if identity is not None:
        object_body(identity, uid, "identity")
    cards = []
    if worker._source_family(str(job.get("source_kind") or "history")) != "ai_persona":
        cards = local_index(raw["moments"], uid)
    return Prepared(runtime, texts, persona, voice, cards, identity,
                    str(raw["user"].get("archive_language") or ""))


def fetch_key_envelope(api_url, token):
    try:
        response = httpx.get(
            f"{api_url.rstrip('/')}/v1/model_api/key_envelope",
            headers={"X-Feedling-Runtime-Token": token}, timeout=15,
        )
        response.raise_for_status()
        doc = response.json()
        return doc.get("api_key_envelope") if isinstance(doc, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


@dataclass
class Sweep:
    cursor: tuple | None = None
    highwater: tuple | None = None

    def claim(self, *, api_url, worker_id, mint_token, page_size=16):
        rows, self.highwater = persistence.scan_page(
            self.cursor, self.highwater, limit=page_size,
        )
        if not rows:
            self.cursor = self.highwater = None
            return None
        for key in rows:
            self.cursor = key
            _, uid, jid = key
            token = mint_token(uid)
            authenticated = fetch_key_envelope(api_url, token)
            try:
                claimed = persistence.prepare_claim(uid, jid, worker_id, authenticated, prepare)
            except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected):
                continue  # Not admitted. Revisit on the next finite sweep.
            if claimed:
                return (*claimed, token)
        return None
