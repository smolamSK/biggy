"""Core routes: landing, first-run setup wizard, health, connection info."""
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    url_for,
)
from flask_login import current_user, login_required, login_user

from ..db import SessionLocal, build_url, get_engine, test_connection
from ..forms.admin_forms import SetupForm
from ..helpers import designer_required, is_bootstrapped
from ..metadata.models import AppUser, Base, ROLE_DESIGNER
from ..metadata.schema_service import ensure_meta_schema

bp = Blueprint("core", __name__)


@bp.route("/health")
def health():
    ok, msg = test_connection(build_url(current_app.config))
    return {"status": "ok" if ok else "db_error", "detail": msg}, (200 if ok else 503)


@bp.route("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if current_user.is_designer:
        return redirect(url_for("designer.dashboard"))
    return redirect(url_for("user.dashboard"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if is_bootstrapped():
        return redirect(url_for("index" if False else "core.index"))

    conn_ok, conn_msg = test_connection(build_url(current_app.config))
    form = SetupForm()

    if form.validate_on_submit():
        if not conn_ok:
            flash("Cannot create account: database connection failed.", "danger")
        else:
            engine = get_engine()
            Base.metadata.create_all(engine)
            ensure_meta_schema(engine)
            session = SessionLocal()
            from ..helpers import ensure_roles
            ensure_roles(session)
            user = AppUser(username=form.username.data, role=ROLE_DESIGNER)
            user.set_password(form.password.data)
            session.add(user)
            session.commit()
            current_app.config["_BOOTSTRAPPED"] = True
            from ..auth.routes import establish_session
            establish_session(user)
            flash("Setup complete. Welcome to Biggy!", "success")
            return redirect(url_for("designer.dashboard"))

    return render_template(
        "core/setup.html", form=form, conn_ok=conn_ok, conn_msg=conn_msg,
        db_url=_safe_url(),
    )


@bp.route("/help")
@login_required
def help_index():
    return redirect(url_for("core.help_page", topic="user"))


@bp.route("/help/<topic>")
@login_required
def help_page(topic):
    from ..help import DESIGNER_TOPICS, render_manual
    if topic in DESIGNER_TOPICS and not current_user.is_designer:
        abort(403)
    result = render_manual(topic)
    if result is None:
        abort(404)
    title, content = result
    return render_template("core/help.html", title=title, topic=topic, content=content)


@bp.route("/connection")
@login_required
@designer_required
def connection():
    ok, msg = test_connection(build_url(current_app.config))
    return render_template("core/connection.html", ok=ok, msg=msg, db_url=_safe_url())


def _safe_url():
    """Connection string with the password masked, for display."""
    try:
        url = build_url(current_app.config)
        return url.render_as_string(hide_password=True) if hasattr(url, "render_as_string") \
            else str(url)
    except Exception:  # noqa: BLE001
        return "(unavailable)"
