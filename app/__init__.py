from __future__ import annotations

import os

import click
from flask import Flask
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.middleware.proxy_fix import ProxyFix

from .models import User, db
from .outbox import enqueue_event

login_manager = LoginManager()
migrate = Migrate()
csrf = CSRFProtect()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY"),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///andeslog.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 300},
        ERP_AZURE_INGEST_URL=os.getenv("ERP_AZURE_INGEST_URL", ""),
        ERP_AZURE_INGEST_FALLBACK_URL=os.getenv("ERP_AZURE_INGEST_FALLBACK_URL", ""),
        ERP_AZURE_INGEST_FALLBACK_HMAC_SECRET=os.getenv("ERP_AZURE_INGEST_FALLBACK_HMAC_SECRET", ""),
        ERP_AZURE_INGEST_HMAC_SECRET=os.getenv("ERP_AZURE_INGEST_HMAC_SECRET", ""),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("APP_ENV", "development") == "production",
        TRUST_PROXY_PROTO=os.getenv("TRUST_PROXY_PROTO", "false").lower() == "true",
        REMEMBER_COOKIE_HTTPONLY=True,
        WTF_CSRF_TIME_LIMIT=3600,
    )
    if test_config:
        app.config.update(test_config)
    # Fail a stalled PostgreSQL connection attempt promptly so readiness and
    # callers can recover instead of exhausting the web-worker pool. libpq
    # applies this per host; explicit URI or engine settings take precedence.
    database_url = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    if database_url.get_backend_name() == "postgresql" and "connect_timeout" not in database_url.query:
        engine_options = dict(app.config.get("SQLALCHEMY_ENGINE_OPTIONS") or {})
        connect_args = dict(engine_options.get("connect_args") or {})
        connect_args.setdefault("connect_timeout", 5)
        engine_options["connect_args"] = connect_args
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("Define SECRET_KEY en el entorno antes de iniciar la aplicación.")

    if app.config.get("TRUST_PROXY_PROTO") is True:
        # Enable only when network policy guarantees exactly one trusted proxy.
        # Do not trust forwarded client IP, host, port, or prefix.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)

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

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'",
        )
        return response

    @app.get("/api/health/live")
    def api_health_live():
        """Process health only; safe for load balancer instance replacement checks."""
        return {"status": "alive", "service": "andeslog-erp"}, 200

    @app.get("/api/health/ready")
    def api_health_ready():
        """Dependency readiness, including the PostgreSQL server's write mode.

        This is a non-mutating signal, not proof that all writes will succeed.
        """
        try:
            if db.engine.dialect.name == "postgresql":
                read_only = db.session.execute(text(
                    "SELECT pg_is_in_recovery() OR current_setting('transaction_read_only') <> 'off'"
                )).scalar_one()
                if read_only is not False:
                    return {"status": "not_ready", "service": "andeslog-erp", "database": "read_only"}, 503
            else:
                db.session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            db.session.rollback()
            return {"status": "not_ready", "service": "andeslog-erp", "database": "unavailable"}, 503
        return {"status": "ready", "service": "andeslog-erp", "database": "ok"}, 200

    @app.get("/api/health")
    def api_health():
        """Compatibility endpoint used by deployment smoke checks."""
        return api_health_ready()

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

    @app.cli.command("outbox-smoke")
    def outbox_smoke() -> None:
        """Queue a harmless event for an end-to-end delivery check."""
        from uuid import uuid4

        event = enqueue_event(
            "erp.integration.smoke_test",
            "integration_smoke_test",
            uuid4(),
            {"purpose": "deployment_verification"},
        )
        db.session.commit()
        click.echo(event.id)

    @app.cli.command("outbox-status")
    @click.option("--event-id", required=True)
    def outbox_status(event_id: str) -> None:
        """Report delivery state for a single integration event."""
        from .models import OutboxEvent

        event = db.session.get(OutboxEvent, event_id)
        if event is None:
            raise click.ClickException("Outbox event not found")
        click.echo(event.status)

    @app.cli.command("outbox-retry-dead")
    @click.option("--event-id", required=True)
    def outbox_retry_dead(event_id: str) -> None:
        """Requeue one permanently rejected event after its cause is corrected."""
        from .models import OutboxEvent, utcnow_naive

        event = db.session.get(OutboxEvent, event_id)
        if event is None:
            raise click.ClickException("Outbox event not found")
        if event.status != "dead":
            raise click.ClickException("Only a dead event can be manually requeued")
        event.status = "pending"
        event.available_at = utcnow_naive()
        event.locked_until = None
        db.session.commit()
        click.echo("pending")

    return app
