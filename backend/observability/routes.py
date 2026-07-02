from __future__ import annotations
from flask import Blueprint, Response, jsonify, request
from admin.data_track import require_admin
from observability import store, render

bp = Blueprint("observability", __name__)


@bp.route("/v1/admin/observability/samples", methods=["GET"])
def obs_samples():
    require_admin()
    hours = float(request.args.get("hours", "6") or "6")
    return jsonify(store.recent_samples(hours))


@bp.route("/admin/observability", methods=["GET"])
def obs_page():
    require_admin()
    hours = float(request.args.get("hours", "6") or "6")
    return Response(render.render_page(store.latest_sample(), store.recent_samples(hours)),
                    mimetype="text/html")
