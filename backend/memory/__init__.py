"""Memory domain."""


def register(app):
    from memory import routes

    app.register_blueprint(routes.bp)
