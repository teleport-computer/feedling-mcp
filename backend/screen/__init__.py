"""Screen domain: frame ingest/storage, WS server, aggregation, /v1/screen/*."""


def register(app):
    from screen import routes

    app.register_blueprint(routes.bp)
