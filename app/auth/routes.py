"""Authentication and (designer-only) user management."""
import secrets

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session as web_session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from .. import oidc, totp
from ..db import SessionLocal
from ..forms.admin_forms import LoginForm, MfaCodeForm, PasswordChangeForm, UserForm
from ..helpers import designer_required, ensure_roles
from ..metadata.models import AppUser, ROLE_USER, Role

bp = Blueprint("auth", __name__, url_prefix="/auth")


def _role_choices(session):
    ensure_roles(session)
    return [(r.name, r.label) for r in session.scalars(select(Role).order_by(Role.name))]


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("core.index"))
    form = LoginForm()
    if form.validate_on_submit():
        session = SessionLocal()
        user = session.scalar(select(AppUser).where(AppUser.username == form.username.data))
        if user and user.check_password(form.password.data) and user.is_active:
            if user.mfa_enabled:
                # password is correct, but require the second factor before logging in
                web_session["_mfa_uid"] = user.id
                web_session["_mfa_next"] = request.args.get("next") or ""
                return redirect(url_for("auth.mfa_verify"))
            login_user(user)
            return redirect(request.args.get("next") or url_for("core.index"))
        flash("Invalid credentials or inactive account.", "danger")
    return render_template("auth/login.html", form=form)


@bp.route("/mfa-verify", methods=["GET", "POST"])
def mfa_verify():
    """Second login step: verify a TOTP (or backup) code for the pending user."""
    uid = web_session.get("_mfa_uid")
    if not uid:
        return redirect(url_for("auth.login"))
    session = SessionLocal()
    user = session.get(AppUser, uid)
    if not user or not user.mfa_enabled or not user.is_active:
        web_session.pop("_mfa_uid", None)
        return redirect(url_for("auth.login"))
    form = MfaCodeForm()
    if form.validate_on_submit():
        code = form.code.data.strip()
        ok = totp.verify(user.totp_secret, code)
        if not ok:
            ok, new_codes = totp.consume_backup_code(user.mfa_backup_codes, code)
            if ok:
                user.mfa_backup_codes = new_codes
                session.commit()
        if ok:
            nxt = web_session.pop("_mfa_next", "")
            web_session.pop("_mfa_uid", None)
            login_user(user)
            return redirect(nxt or url_for("core.index"))
        flash("Invalid authentication code.", "danger")
    return render_template("auth/mfa_verify.html", form=form)


@bp.route("/mfa", methods=["GET", "POST"])
@login_required
def mfa():
    """Self-service: enable / disable two-factor and manage backup codes."""
    session = SessionLocal()
    user = session.get(AppUser, current_user.id)
    form = MfaCodeForm()
    shown_codes = None  # plaintext backup codes, displayed once

    if request.method == "POST":
        action = request.form.get("action")
        if action == "enable" and not user.mfa_enabled:
            secret = web_session.get("_mfa_setup_secret")
            if secret and form.validate() and totp.verify(secret, form.code.data):
                user.totp_secret = secret
                user.mfa_enabled = True
                plain, hashed = totp.make_backup_codes()
                user.mfa_backup_codes = hashed
                session.commit()
                web_session.pop("_mfa_setup_secret", None)
                shown_codes = plain
                flash("Two-factor authentication enabled.", "success")
            else:
                flash("That code didn't match — try again.", "danger")
        elif action == "disable" and user.mfa_enabled:
            if form.validate() and totp.verify(user.totp_secret, form.code.data):
                user.totp_secret, user.mfa_enabled, user.mfa_backup_codes = None, False, None
                session.commit()
                flash("Two-factor authentication disabled.", "info")
            else:
                flash("Enter a current code to disable two-factor.", "danger")
        elif action == "regenerate" and user.mfa_enabled:
            plain, hashed = totp.make_backup_codes()
            user.mfa_backup_codes = hashed
            session.commit()
            shown_codes = plain
            flash("New backup codes generated — the old ones no longer work.", "success")

    secret = uri = qr = None
    if not user.mfa_enabled:
        secret = web_session.get("_mfa_setup_secret") or totp.new_secret()
        web_session["_mfa_setup_secret"] = secret
        uri = totp.provisioning_uri(secret, user.username)
        qr = totp.qr_svg(uri)
    return render_template("auth/mfa.html", user=user, form=form, secret=secret, uri=uri, qr=qr,
                           shown_codes=shown_codes,
                           backup_remaining=totp.backup_count(user.mfa_backup_codes))


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    """Self-service: the current user changes their own password."""
    form = PasswordChangeForm()
    if form.validate_on_submit():
        session = SessionLocal()
        user = session.get(AppUser, current_user.id)
        if not user.check_password(form.current.data):
            flash("Current password is incorrect.", "danger")
        else:
            user.set_password(form.new.data)
            session.commit()
            flash("Password changed.", "success")
            return redirect(url_for("auth.account"))
    return render_template("auth/account.html", form=form)


# --------------------------------------------------------------------------- #
# SSO (OpenID Connect)
# --------------------------------------------------------------------------- #
def _oidc_redirect_uri():
    return current_app.config.get("OIDC_REDIRECT_URI") \
        or url_for("auth.oidc_callback", _external=True)


@bp.route("/oidc/login")
def oidc_login():
    if not current_app.config.get("OIDC_ENABLED"):
        abort(404)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    web_session["_oidc_state"] = state
    web_session["_oidc_nonce"] = nonce
    web_session["_oidc_next"] = request.args.get("next") or ""
    try:
        return redirect(oidc.authorize_url(state, nonce, _oidc_redirect_uri()))
    except oidc.OidcError as exc:
        flash(f"SSO is unavailable: {exc}", "danger")
        return redirect(url_for("auth.login"))


@bp.route("/oidc/callback")
def oidc_callback():
    if not current_app.config.get("OIDC_ENABLED"):
        abort(404)
    if request.args.get("error"):
        flash(f"SSO error: {request.args.get('error')}", "danger")
        return redirect(url_for("auth.login"))
    state = request.args.get("state")
    if not state or state != web_session.pop("_oidc_state", None):
        flash("SSO state mismatch — please try again.", "danger")
        return redirect(url_for("auth.login"))
    nonce = web_session.pop("_oidc_nonce", None)
    nxt = web_session.pop("_oidc_next", "")
    code = request.args.get("code")
    if not code:
        flash("SSO did not return a code.", "danger")
        return redirect(url_for("auth.login"))
    try:
        tokens = oidc.exchange_code(code, _oidc_redirect_uri())
        claims = oidc.verify_id_token(tokens["id_token"], nonce)
    except oidc.OidcError as exc:
        flash(f"SSO sign-in failed: {exc}", "danger")
        return redirect(url_for("auth.login"))

    user = _map_oidc_user(claims)
    if not user:
        flash("No Biggy account is linked to this identity.", "danger")
        return redirect(url_for("auth.login"))
    if not user.is_active:
        flash("That account is inactive.", "danger")
        return redirect(url_for("auth.login"))
    login_user(user)
    return redirect(nxt or url_for("core.index"))


def _map_oidc_user(claims):
    """Map verified OIDC claims to an AppUser (link existing; JIT-create if configured)."""
    sub = claims.get("sub")
    if not sub:
        return None
    session = SessionLocal()
    user = session.scalar(select(AppUser).where(AppUser.oidc_subject == sub))
    if user:
        return user
    claim = current_app.config.get("OIDC_USERNAME_CLAIM", "email")
    uname = claims.get(claim) or claims.get("email") or claims.get("preferred_username")
    if uname:
        user = session.scalar(select(AppUser).where(AppUser.username == uname))
        if user:                                  # link this identity for future logins
            user.oidc_subject = sub
            session.commit()
            return user
    if current_app.config.get("OIDC_PROVISION") == "jit" and uname:
        user = AppUser(username=uname, oidc_subject=sub, is_active_flag=True,
                       role=current_app.config.get("OIDC_DEFAULT_ROLE", "user"))
        user.password_hash = generate_password_hash("oidc-" + secrets.token_urlsafe(32))
        session.add(user)
        session.commit()
        return user
    return None


# --------------------------------------------------------------------------- #
# User management (designer only)
# --------------------------------------------------------------------------- #
@bp.route("/users")
@login_required
@designer_required
def users():
    session = SessionLocal()
    rows = session.scalars(select(AppUser).order_by(AppUser.username)).all()
    return render_template("auth/users.html", users=rows)


@bp.route("/users/bulk", methods=["POST"])
@login_required
@designer_required
def users_bulk():
    """Create many users at once from pasted ``username,role[,password]`` lines."""
    session = SessionLocal()
    ensure_roles(session)
    valid_roles = {r.name for r in session.scalars(select(Role))}
    created = skipped = errors = 0
    for line in (request.form.get("rows") or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        username = parts[0]
        role = parts[1] if len(parts) > 1 and parts[1] else ROLE_USER
        password = parts[2] if len(parts) > 2 else ""
        if not username or role not in valid_roles:
            errors += 1
            continue
        if session.scalar(select(AppUser).where(AppUser.username == username)):
            skipped += 1
            continue
        user = AppUser(username=username, role=role, is_active_flag=True)
        if password:
            user.set_password(password)
        else:                       # no password → SSO-only / awaiting an admin reset
            user.password_hash = generate_password_hash("set-" + secrets.token_urlsafe(24))
        session.add(user)
        created += 1
    session.commit()
    flash(f"Bulk import: {created} created, {skipped} skipped (existing), {errors} error(s).",
          "success" if created else "info")
    return redirect(url_for("auth.users"))


@bp.route("/users/new", methods=["GET", "POST"])
@login_required
@designer_required
def user_new():
    session = SessionLocal()
    form = UserForm()
    form.role.choices = _role_choices(session)
    if form.validate_on_submit():
        if session.scalar(select(AppUser).where(AppUser.username == form.username.data)):
            flash("Username already exists.", "danger")
        elif not form.password.data:
            flash("Password is required for a new user.", "danger")
        else:
            user = AppUser(username=form.username.data, role=form.role.data,
                          is_active_flag=form.is_active.data)
            user.set_password(form.password.data)
            session.add(user)
            session.commit()
            flash("User created.", "success")
            return redirect(url_for("auth.users"))
    return render_template("auth/user_form.html", form=form, title="New user")


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@designer_required
def user_edit(user_id):
    session = SessionLocal()
    user = session.get(AppUser, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("auth.users"))
    form = UserForm(obj=user)
    form.role.choices = _role_choices(session)
    form.is_active.data = user.is_active_flag if request.method == "GET" else form.is_active.data
    if form.validate_on_submit():
        user.username = form.username.data
        user.role = form.role.data
        user.is_active_flag = form.is_active.data
        if form.password.data:
            user.set_password(form.password.data)
        session.commit()
        flash("User updated.", "success")
        return redirect(url_for("auth.users"))
    return render_template("auth/user_form.html", form=form, title=f"Edit {user.username}",
                           user=user)


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@designer_required
def user_delete(user_id):
    session = SessionLocal()
    user = session.get(AppUser, user_id)
    if user and user.id != current_user.id:
        session.delete(user)
        session.commit()
        flash("User deleted.", "info")
    else:
        flash("Cannot delete this account.", "warning")
    return redirect(url_for("auth.users"))


@bp.route("/users/<int:user_id>/reset-mfa", methods=["POST"])
@login_required
@designer_required
def user_reset_mfa(user_id):
    """Clear a user's two-factor (e.g. lost device) so they can re-enroll."""
    session = SessionLocal()
    user = session.get(AppUser, user_id)
    if user:
        user.totp_secret, user.mfa_enabled, user.mfa_backup_codes = None, False, None
        session.commit()
        flash(f"Two-factor reset for {user.username}.", "info")
    return redirect(url_for("auth.user_edit", user_id=user_id))
