"""Authentication and (designer-only) user management."""
from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session as web_session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select

from .. import totp
from ..db import SessionLocal
from ..forms.admin_forms import LoginForm, MfaCodeForm, PasswordChangeForm, UserForm
from ..helpers import designer_required, ensure_roles
from ..metadata.models import AppUser, Role

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

    secret = uri = None
    if not user.mfa_enabled:
        secret = web_session.get("_mfa_setup_secret") or totp.new_secret()
        web_session["_mfa_setup_secret"] = secret
        uri = totp.provisioning_uri(secret, user.username)
    return render_template("auth/mfa.html", user=user, form=form, secret=secret, uri=uri,
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
# User management (designer only)
# --------------------------------------------------------------------------- #
@bp.route("/users")
@login_required
@designer_required
def users():
    session = SessionLocal()
    rows = session.scalars(select(AppUser).order_by(AppUser.username)).all()
    return render_template("auth/users.html", users=rows)


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
