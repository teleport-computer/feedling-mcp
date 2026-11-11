"""Read-only fixed-page projection for effective-off recall."""
from __future__ import annotations

import json
import hashlib
from core import envelope, history_view, plaintext_row
from memory import recall_contract, recall_attachments


def _read(row, user_id):
    recall_contract.owner(row, user_id)
    try:
        return plaintext_row.read_plaintext_row(row, user_id, ValueError)
    except (ValueError, UnicodeError) as exc:
        raise recall_contract.RecallFailure("content_storage_invalid") from exc


def turn(rows, user_id: str, seq: int, message_id: str = "", *, require_current=True, attachment_reader=None, source_snapshots=None):
    if (not isinstance(rows, list) or isinstance(seq, bool)
            or not isinstance(seq, int) or seq < 1):
        raise recall_contract.RecallFailure("frontier_invalid", category="binding_error")
    originals = json.loads(rows.original_json) if isinstance(rows, recall_attachments.HistoryPage) else rows
    if len(originals) != len(rows):
        raise recall_contract.RecallFailure("frontier_invalid", category="binding_error")
    original_by_id = {row.get("id"): row for row in originals if isinstance(row, dict)}
    anchor_id = message_id or next((row.get("id") for row in reversed(rows)
                                   if isinstance(row, dict) and row.get("role") == "user"), "")
    verified_attachments = set()
    attachment_hashes = []
    ids = set()
    previous = 0
    projected = []
    newest_user = ""
    for source in rows:
        recall_contract.owner(source, user_id)
        mid = source.get("id")
        row_seq = source.get("seq")
        if (not isinstance(mid, str) or not mid or mid in ids
                or isinstance(row_seq, bool) or not isinstance(row_seq, int)
                or not previous < row_seq <= seq):
            raise recall_contract.RecallFailure("frontier_invalid", category="binding_error")
        ids.add(mid)
        previous = row_seq
        if source.get("role") == "user":
            newest_user = mid
        storage_source = original_by_id.get(mid)
        recall_contract.owner(storage_source, user_id)
        expected_source = (source_snapshots or {}).get(mid)
        if expected_source is not None and storage_source != expected_source:
            # Only a verified, byte-identical inline→object promotion may
            # differ from the source frozen by the prompt reader.
            proof = attachment_reader(storage_source) if attachment_reader else None
            if (not isinstance(proof, recall_attachments.AttachmentProof)
                    or not recall_attachments.same_source(expected_source, storage_source, proof.body)):
                raise recall_contract.RecallFailure("attachment_changed", category="binding_error")
        if storage_source.get("seq") != row_seq:
            raise recall_contract.RecallFailure("frontier_invalid", category="binding_error")
        row = dict(source)
        if storage_source.get("body_key") and row.get("content_type") in {"image", "file"}:
            # A pointer is metadata until the required attachment adapter has
            # validated its canonical bytes. Historical media stays text-only.
            row["body_omitted"] = True
        try:
            caption = envelope.caption_envelope_from_row(row)
        except (ValueError, TypeError) as exc:
            raise recall_contract.RecallFailure("caption_storage_invalid") from exc
        if caption is not None:
            recall_contract.owner(caption, user_id)
        local_only = storage_source.get("visibility") == "local_only"
        sealed = bool(storage_source.get("body_ct") or storage_source.get("K_enclave") or
                      (storage_source.get("body_key") and storage_source.get("body_object_format") != "plaintext_v1"))
        caption_sealed = caption is not None and bool(caption.get("body_ct") or caption.get("K_enclave"))
        caption_text = ""
        if caption is not None and not caption_sealed and not local_only:
            try:
                caption_text = _read(caption, user_id).decode("utf-8")
            except UnicodeError as exc:
                raise recall_contract.RecallFailure("caption_storage_invalid") from exc
        if (require_current and mid == anchor_id and not local_only and not sealed
                and row.get("content_type") in {"image", "file"}
                and (row.get("body_omitted") or isinstance(rows, recall_attachments.HistoryPage))):
            if attachment_reader is None:
                raise recall_contract.RecallFailure("attachment_unconfirmed")
            proof = attachment_reader(storage_source)
            if (not isinstance(proof, recall_attachments.AttachmentProof)
                    or proof.user_id != user_id or proof.message_id != mid or proof.seq != row_seq):
                raise recall_contract.RecallFailure("attachment_binding_invalid", category="binding_error")
            verified_attachments.add(mid)
            attachment_hashes.append({"id": mid, "seq": row_seq,
                                      "sha256": hashlib.sha256(proof.body).hexdigest()})
        if local_only or sealed:
            # Nothing derived from the excluded body, including quoted memory
            # expansion or old decoded fields, can survive this projection.
            view = {k: row.get(k) for k in ("id", "seq", "role", "ts", "source", "content_type")}
            view.update(content=caption_text if not local_only else "",
                        body_unavailable_reason="sealed_history_excluded" if sealed else "local_only")
        else:
            if caption_sealed:
                row = {k: v for k, v in row.items() if not k.startswith("caption_")}
            if not row.get("body_omitted"):
                raw = _read(row, user_id)
                if row.get("content_type", "text") not in {"image", "file"}:
                    try:
                        raw.decode("utf-8")
                    except UnicodeError as exc:
                        raise recall_contract.RecallFailure("content_storage_invalid") from exc
            try:
                items, errors = history_view.history_items(
                    [row], lambda value: _read(value, user_id), recall_contract.RecallFailure)
            except Exception as exc:
                raise recall_contract.RecallFailure("content_storage_invalid") from exc
            if errors or len(items) != 1:
                raise recall_contract.RecallFailure("content_storage_invalid")
            view = items[0]
        projected.append(view)
    expected = message_id or newest_user
    if require_current and (not expected or newest_user != expected or expected not in ids):
        raise recall_contract.RecallFailure("message_not_in_window", category="binding_error")
    anchor = next((row for row in projected if row["id"] == expected), {})
    if require_current and expected not in verified_attachments and (not isinstance(anchor.get("content"), str) or not anchor["content"].strip()):
        raise recall_contract.RecallFailure("unreadable_current_message")
    return recall_contract.ValidatedTurnContent(
        user_id, seq, expected, json.dumps(rows, ensure_ascii=True),
        json.dumps(projected, ensure_ascii=True), json.dumps(attachment_hashes))
