from .service import report_self_resource, start_sampler

__all__ = ["register", "report_self_resource", "start_sampler"]


def register(app) -> None:
    from observability import routes, fivexx
    app.register_blueprint(routes.bp)
    fivexx.install(app)
