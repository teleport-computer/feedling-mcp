"""Server-managed UI copy — a self-contained backend feature module.

The iOS app overlays a bundle of managed copy keys on top of its compiled-in
Localizable.xcstrings (which stays the offline fallback). Editing copy needs
neither an App Store release nor a backend deploy — only a DB row change via
the admin-gated POST /v1/copytext.

Integration with the rest of the backend is one line in app.py:
    import copytext as copytext_pkg
    copytext_pkg.register(app)

Everything else (routing, DB access, validation) lives here.
"""
from __future__ import annotations

from .routes import bp

__all__ = ["register"]


def register(app) -> None:
    """Mount the /v1/copytext blueprint onto the Flask app."""
    app.register_blueprint(bp)
