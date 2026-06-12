"""Accounts domain: registry, auth, onboarding route, recovery, HTTP routes."""


def register(app):
    from accounts import routes

    app.register_blueprint(routes.bp)
