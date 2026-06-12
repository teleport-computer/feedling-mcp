"""Bootstrap gates + onboarding validation."""


def register(app):
    from bootstrap import routes

    app.register_blueprint(routes.bp)
