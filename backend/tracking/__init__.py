"""Tracking domain."""


def register(app):
    from tracking import routes

    app.register_blueprint(routes.bp)
