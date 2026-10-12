"""HTML pages: login, logout, password change, the two dashboards."""
from flask import Blueprint, g, redirect, render_template, request, session, url_for

from . import auth
from .security import password_problem, verify_password, hash_password
from .storage import iso_now, norm_uid

bp = Blueprint("pages", __name__)


@bp.get("/")
def index():
    if not g.user:
        return redirect(url_for("pages.login"))
    # Hub pages are written in calllog/ops/views.py; plain paths so this works whichever blueprint serves them
    return redirect("/hub" if g.user["role"] in auth.OPS_ROLES else "/my")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if g.user:
            return redirect(url_for("pages.index"))
        return render_template("login.html", error=None, user_id="")

    uid = norm_uid(request.form.get("user_id"))
    password = request.form.get("password", "")
    ip = request.remote_addr
    limits = auth.throttles()
    wait = max(limits["user"].retry_after((uid, ip)), limits["ip"].retry_after(ip))
    if wait:
        auth.audit("login_blocked", target=uid, actor={"user_id": uid})
        minutes = -(-wait // 60)
        return render_template("login.html", user_id=uid, error=(
            f"Too many failed attempts. Try again in {minutes} minute(s).")), 429

    user = auth.store().get_user(uid)
    # Always hash-check (even for unknown IDs) so timing does not reveal which IDs exist.
    password_ok = verify_password(user["password_hash"] if user else None, password)
    if not (user and password_ok):
        limits["user"].fail((uid, ip))
        limits["ip"].fail(ip)
        auth.audit("login_failed", target=uid, actor={"user_id": uid})
        return render_template("login.html", user_id=uid,
                               error="Wrong User ID or password."), 401
    if not user.get("active", True):
        auth.audit("login_failed", target=uid, details={"reason": "disabled"}, actor=user)
        return render_template("login.html", user_id=uid, error=(
            "This account is disabled. Please contact your administrator.")), 403

    limits["user"].clear((uid, ip))
    session.clear()  # new session id on every login
    session.permanent = True
    session["uid"] = uid
    auth.csrf_token()
    auth.store().update_user(uid, {"last_login": iso_now(auth.now())})
    auth.audit("login", actor=user)
    return redirect(url_for("pages.index"))


@bp.post("/logout")
def logout():
    if g.user:
        auth.audit("logout")
    session.clear()
    return redirect(url_for("pages.login"))


@bp.route("/account/password", methods=["GET", "POST"])
@auth.login_required
def password():
    forced = bool(g.user.get("must_change_password"))
    if request.method == "GET":
        return render_template("password.html", error=None, forced=forced)

    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    error = None
    if not verify_password(g.user["password_hash"], current):
        error = "Your current password is not correct."
    elif new != request.form.get("confirm_password", ""):
        error = "The new passwords do not match."
    elif new == current:
        error = "Choose a password different from the current one."
    else:
        error = password_problem(new)
    if error:
        return render_template("password.html", error=error, forced=forced), 400

    auth.store().update_user(g.user["user_id"], {
        "password_hash": hash_password(new), "must_change_password": False,
        "password_changed_at": iso_now(auth.now())})
    auth.audit("password_changed")
    return redirect(url_for("pages.index"))


@bp.get("/dashboard")
@auth.login_required
def dashboard():
    if g.user["role"] == "admin":
        return redirect(url_for("pages.admin"))
    return render_template("user.html")


@bp.get("/admin")
@auth.admin_required
def admin():
    return render_template("admin.html")
