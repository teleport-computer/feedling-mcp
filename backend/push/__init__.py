"""Push domain: APNs, Live Activity, push decision, /v1/push/* routes."""


def register(app):
    from push import routes

    app.register_blueprint(routes.bp)
