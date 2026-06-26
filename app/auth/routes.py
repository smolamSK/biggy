"""Authentication and (designer-only) user management."""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select

from ..db import SessionLocal
from ..forms.admin_forms import LoginForm, PasswordChangeForm, UserForm
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
            login_user(user)
            return redirect(request.args.get("next") or url_for("core.index"))
        flash("Invalid credentials or inactive account.", "danger")
    return render_template("auth/login.html", form=form)


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
    return render_template("auth/user_form.html", form=form, title=f"Edit {user.username}")


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
