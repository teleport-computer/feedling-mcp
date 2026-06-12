"""Hosted (model_api) user line: config, chat, history import, wake consumer."""


def register(app):
    from hosted import chat_routes, history_import, onboarding_validation, setup_routes

    app.register_blueprint(setup_routes.bp)
    app.register_blueprint(history_import.bp)
    app.register_blueprint(chat_routes.bp)
    app.register_blueprint(onboarding_validation.bp)
