"""Content envelope domain."""


def register(app):
    from content import routes

    app.register_blueprint(routes.bp)
