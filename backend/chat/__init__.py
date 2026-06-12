"""Resident chat domain."""


def register(app):
    from chat import routes

    app.register_blueprint(routes.bp)
    from chat import verify_loop

    app.register_blueprint(verify_loop.bp)
