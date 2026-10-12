"""Application factory."""
import importlib.util
import secrets
from datetime import datetime, timedelta

from flask import Flask, g, jsonify, render_template, request, session

from . import auth
from .config import APP_DIR, load_config
from .queries import ApiError, api_error_response
from .ops.store import OpsStore
from .security import generate_password, hash_password
from .storage import StorageError, Store, iso_now

# Pages the user may still reach while a password change is being forced.
_ALLOWED_WHEN_PASSWORD_DUE = {"pages.password", "pages.logout", "static"}


def _secret_key():
    """A random key kept next to the app (not in the shared folder); created on first run."""
    path = APP_DIR / ".secret_key"
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        try:
            path.write_text(key)
        except OSError:
            pass  # read-only install: sessions then reset on every restart
        return key


def bootstrap_admin(store, clock):
    """First run: create the 'admin' account with a random one-time password."""
    if store.users():
        return None
    password = generate_password()
    try:
        store.create_user({
            "user_id": "admin", "name": "Administrator", "department": "", "role": "admin",
            "password_hash": hash_password(password), "active": True,
            "must_change_password": True, "created_at": iso_now(clock()), "last_login": None,
        })
    except StorageError:  # another PC created it a moment ago
        return None
    return password


def create_app(overrides=None):
    cfg = load_config()
    cfg.update(overrides or {})

    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.config.update(
        SECRET_KEY=cfg.get("secret_key") or _secret_key(),
        SESSION_COOKIE_NAME="calllog_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=cfg["session_hours"]),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        CLOCK=cfg.get("clock") or datetime.now,
        USER_EDIT_DAYS=cfg["user_edit_days"],
        IP_PHONE_REGEX=cfg["ip_phone_regex"],
        SETTINGS=cfg,
    )

    store = Store(cfg["data_dir"])
    store.init()
    app.extensions["store"] = store
    # Operations Hub files (snapshots, overrides, settings...) live in <data_dir>/ops, next to the Call Log data
    app.extensions["ops"] = OpsStore(cfg["data_dir"]).init()
    app.extensions["throttles"] = auth.make_throttles()
    app.config["INITIAL_ADMIN_PASSWORD"] = bootstrap_admin(store, app.config["CLOCK"])

    from . import api, exports, views
    from .ops import api as ops_api, exports as ops_exports
    app.register_blueprint(views.bp)
    app.register_blueprint(api.bp, url_prefix="/api")
    app.register_blueprint(exports.bp, url_prefix="/export")
    app.register_blueprint(ops_api.bp, url_prefix="/api/ops")
    app.register_blueprint(ops_exports.bp, url_prefix="/export/ops")
    # The Hub pages (/hub /my /email /sla /itsm /system) are a separate module; registered once it exists.
    if importlib.util.find_spec("calllog.ops.views"):
        from .ops import views as ops_views
        app.register_blueprint(ops_views.bp)         # that module must expose `bp`

    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        uid = session.get("uid")
        if uid:
            user = store.get_user(uid)
            if user and user.get("active", True):
                g.user = user
            else:
                session.pop("uid", None)
        if request.method in auth.UNSAFE_METHODS and not auth.csrf_ok():
            if auth.wants_json():
                return jsonify(error="Your session expired. Reload the page and try again."), 400
            return render_template("message.html", title="Session expired",
                                   message="Your session expired. Please go back and try again."), 400
        if (g.user and g.user.get("must_change_password")
                and request.endpoint not in _ALLOWED_WHEN_PASSWORD_DUE):
            if auth.wants_json():
                return jsonify(error="You must change your password first."), 403
            from flask import redirect, url_for
            return redirect(url_for("pages.password"))

    @app.context_processor
    def inject_globals():
        return {"csrf_token": auth.csrf_token, "current_user": g.get("user")}

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    app.register_error_handler(ApiError, api_error_response)

    @app.errorhandler(StorageError)
    def storage_error(exc):
        app.logger.error("Storage problem: %s", exc)
        msg = f"Data folder problem: {exc}"
        if auth.wants_json():
            return jsonify(error=msg), 503
        return render_template("message.html", title="Data folder problem", message=msg), 503

    @app.errorhandler(404)
    def not_found(exc):
        if auth.wants_json():
            return jsonify(error="Not found."), 404
        return render_template("message.html", title="Page not found",
                               message="That page does not exist."), 404

    @app.errorhandler(413)
    def too_large(exc):
        return jsonify(error="That file is too large (limit 2 MB)."), 413

    return app
