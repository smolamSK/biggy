"""Application factory."""
import click
from flask import Flask, g, redirect, request, url_for

from .config import Config
from .db import SessionLocal, init_engine
from .extensions import csrf, login_manager


def create_app(config_object=Config):
    import os

    app = Flask(__name__)
    app.config.from_object(config_object)
    if not app.config.get("UPLOAD_FOLDER"):
        app.config["UPLOAD_FOLDER"] = os.path.join(app.instance_path, "uploads")
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    init_engine(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    # bring an already-bootstrapped database up to the current metadata schema:
    # create any new app_* tables (idempotent) and add any new columns.
    try:
        from .db import get_engine
        from .metadata.models import Base
        from .metadata.schema_service import ensure_meta_schema

        Base.metadata.create_all(get_engine())
        ensure_meta_schema(get_engine())
    except Exception:  # noqa: BLE001 - DB may be unreachable at startup
        pass

    # import side-effect: registers user_loader
    from . import helpers  # noqa: F401

    from .api.routes import bp as api_bp
    from .auth.routes import bp as auth_bp
    from .core.routes import bp as core_bp
    from .designer.routes import bp as designer_bp
    from .hooks.routes import bp as hooks_bp
    from .user.routes import bp as user_bp

    app.register_blueprint(core_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(designer_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(api_bp)
    csrf.exempt(api_bp)  # token-auth API: no browser session, no CSRF
    app.register_blueprint(hooks_bp)
    csrf.exempt(hooks_bp)  # public inbound webhooks: token-in-URL auth, no CSRF

    @app.template_filter("fromjson")
    def _fromjson(s):
        import json
        try:
            return json.loads(s) if s else []
        except (ValueError, TypeError):
            return s

    _register_lifecycle(app)
    _register_context(app)
    _register_errors(app)
    _register_cli(app)

    from . import scheduler
    scheduler.start_ticker(app)  # no-op unless SCHEDULER_ENABLED (never under TESTING)
    return app


def _register_errors(app):
    from flask import render_template

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403,
                               message="You don't have access to that page."), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404,
                               message="That page could not be found."), 404


def _register_lifecycle(app):
    @app.teardown_appcontext
    def remove_session(exc=None):
        SessionLocal.remove()

    @app.before_request
    def require_bootstrap():
        from .helpers import is_bootstrapped

        endpoint = request.endpoint or ""
        allowed = endpoint.startswith("core.setup") or endpoint == "static" \
            or endpoint == "core.health" or endpoint.startswith("hooks.")
        if not allowed and not is_bootstrapped():
            return redirect(url_for("core.setup"))


def _register_context(app):
    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        from .helpers import menu_tree, menu_url, menu_visible

        from sqlalchemy import func, select
        from .db import SessionLocal
        from .metadata.models import MetaTable, Notification

        nav, designer_tables, unread = [], [], 0
        try:
            if current_user.is_authenticated:
                session = SessionLocal()
                nav = [m for m in menu_tree()
                       if menu_visible(session, current_user, m)]
                if current_user.is_designer:
                    designer_tables = session.scalars(
                        select(MetaTable).order_by(MetaTable.label)
                    ).all()
                unread = session.scalar(select(func.count()).select_from(Notification).where(
                    Notification.channel == "in_app", Notification.user_id == current_user.id,
                    Notification.status == "unread")) or 0
        except Exception:  # noqa: BLE001 - never break rendering on menu/table errors
            nav, designer_tables, unread = [], [], 0

        def can_see(item):
            try:
                return menu_visible(SessionLocal(), current_user, item)
            except Exception:  # noqa: BLE001
                return True

        def can_view(table_id):
            from .helpers import can_view as _cv
            try:
                return _cv(SessionLocal(), current_user, table_id)
            except Exception:  # noqa: BLE001
                return False

        return {"nav_menu": nav, "current_user": current_user,
                "menu_url": menu_url, "designer_tables": designer_tables,
                "menu_can_see": can_see, "can_view": can_view,
                "unread_notifications": unread}


def _register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create the application metadata tables (app_*)."""
        from .db import get_engine
        from .metadata.models import Base
        from .metadata.schema_service import ensure_meta_schema

        Base.metadata.create_all(get_engine())
        ensure_meta_schema(get_engine())
        click.echo("Metadata tables created.")

    @app.cli.command("create-designer")
    @click.argument("username")
    @click.password_option()
    def create_designer(username, password):
        """Create a designer account."""
        from .db import SessionLocal
        from .metadata.models import AppUser, ROLE_DESIGNER

        session = SessionLocal()
        user = AppUser(username=username, role=ROLE_DESIGNER)
        user.set_password(password)
        session.add(user)
        session.commit()
        click.echo(f"Designer '{username}' created.")

    @app.cli.command("run-jobs")
    def run_jobs():
        """Run all due scheduled jobs once — triggers, feeds and report digests.

        Cron-friendly: `flask --app run run-jobs`. This is the canonical runner;
        `flask sync` is kept as an alias.
        """
        from . import scheduler
        from .db import SessionLocal, get_engine

        summary = scheduler.run_due(SessionLocal(), get_engine())
        click.echo("Ran jobs — triggers: {triggers}, feeds: {feeds}, pulls: {pulls}, "
                   "reports: {reports}.".format(**summary))

    @app.cli.command("sync")
    def sync():
        """Alias for `run-jobs` (kept for existing cron). Runs all due scheduled jobs."""
        from . import scheduler
        from .db import SessionLocal, get_engine

        summary = scheduler.run_due(SessionLocal(), get_engine())
        click.echo("Ran jobs — triggers: {triggers}, feeds: {feeds}, pulls: {pulls}, "
                   "reports: {reports}.".format(**summary))

    @app.cli.command("dump-examples")
    @click.argument("directory", default="examples")
    def dump_examples(directory):
        """Write each built-in example to <directory>/<key>.schema.json + .data.json."""
        import json
        import os

        from .examples import EXAMPLES

        os.makedirs(directory, exist_ok=True)
        for key, ex in EXAMPLES.items():
            schema, data = ex["build"]()
            for suffix, payload in (("schema", schema), ("data", data)):
                path = os.path.join(directory, f"{key}.{suffix}.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, default=str)
        click.echo(f"Wrote {len(EXAMPLES)} example(s) to {directory}/")
