"""Admin domain (token-gated)."""


def register(app):
    from admin import data_track

    app.register_blueprint(data_track.bp)
