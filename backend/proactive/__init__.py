"""Proactive domain: V2 wake decisions, jobs, debug dashboard, routes."""


def register(app):
    from proactive import routes

    app.register_blueprint(routes.bp)
