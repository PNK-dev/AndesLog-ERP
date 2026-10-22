from __future__ import annotations

import os

import click
from flask import Flask
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect

from .models import User, db

login_manager = LoginManager()
migrate = Migrate()
csrf = CSRFProtect()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY"),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///andeslog.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("APP_ENV", "development") == "production",
        REMEMBER_COOKIE_HTTPONLY=True,
        WTF_CSRF_TIME_LIMIT=3600,
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("Define SECRET_KEY en el entorno antes de iniciar la aplicación.")

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "main.login"
    login_manager.login_message = "Inicia sesión para continuar."
    login_manager.login_message_category = "info"

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        return db.session.get(User, int(user_id))

    from .routes import bp
    app.register_blueprint(bp)

    @app.cli.command("seed-admin")
    @click.option("--username", envvar="BOOTSTRAP_ADMIN_USERNAME", default="admin")
    @click.option("--password", envvar="BOOTSTRAP_ADMIN_PASSWORD", required=True)
    def seed_admin(username: str, password: str) -> None:
        """Crea el primer usuario administrador si todavía no existe."""
        if len(password) < 12:
            raise click.ClickException("La contraseña inicial debe tener al menos 12 caracteres.")
        if User.query.filter_by(username=username).first():
            click.echo(f"El usuario {username} ya existe; no se hicieron cambios.")
            return
        user = User(username=username, role="admin")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Usuario administrador {username} creado.")

    return app
