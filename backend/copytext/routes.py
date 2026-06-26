"""HTTP surface for server-managed UI copy.

GET  /v1/copytext  — public read; the app overlays these onto its local
                     xcstrings. ETag = revision; honors If-None-Match (304).
POST /v1/copytext  — admin-only edit (FEEDLING_ADMIN_TOKEN); zero-deploy copy
                     changes. Bumps the revision so clients refetch.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from admin.data_track import require_admin

from . import service

bp = Blueprint("copytext", __name__)


def _etag(revision: int) -> str:
    # Weak-free strong ETag; quoted per RFC 7232.
    return f'"{revision}"'


@bp.route("/v1/copytext", methods=["GET"])
def get_copytext():
    """Return the managed copy bundle (both languages), with ETag caching."""
    revision = service.store.get_revision()
    etag = _etag(revision)
    if request.headers.get("If-None-Match", "").strip() == etag:
        resp = jsonify({})  # body ignored on 304
        resp.status_code = 304
        resp.headers["ETag"] = etag
        return resp
    bundle = service.build_bundle()
    resp = jsonify(bundle)
    resp.headers["ETag"] = _etag(bundle["revision"])
    return resp


@bp.route("/v1/copytext", methods=["POST"])
def post_copytext():
    """Admin upsert/delete. Body: {strings:{key:{lang:value}}, delete:[key,...]}."""
    require_admin()
    payload = request.get_json(silent=True) or {}
    try:
        result = service.apply_edits(payload)
    except service.CopytextValidationError as e:
        return jsonify({"error": "invalid_payload", "detail": str(e)}), 400
    return jsonify(result)
