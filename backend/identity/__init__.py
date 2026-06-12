"""Identity domain."""


def register(app):
    from identity import routes

    app.register_blueprint(routes.bp)
