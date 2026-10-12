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


def start_refresher(app, interval=1.0):
    """Keep the files and shared results that pages keep asking for fresh in the background, so a request is answered
    from memory and never waits for the shared folder (60 people, one slow share). ``run.py`` calls this once the app
    exists; tests and one-off scripts simply do not. Everything is read once before the first visitor arrives."""
    from .ops import api as ops_api, schemas

    def warm():
        with app.app_context():
            ops_api.hub_cfg()
            ops_api.job_states()
            ops_api.current_overrides()
            email, _ = ops_api.read_snapshot("email")
            for module in schemas.MODULES:
                ops_api.read_snapshot(module)
            if email:
                ovr, rev = ops_api.current_overrides()
                ops_api.email_view(email, None, ovr, False, rev)                 # the administrators' view of the mails
            ops_api.calls_totals(app.config["CLOCK"]().date().isoformat())

    try:
        warm()
    except Exception as exc:                                                       # noqa: BLE001 - warming is optional
        app.logger.warning("warm-up skipped: %s", exc)
    app.extensions["ops"].start_refresher(interval, warm=warm)


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

    @app.get("/healthz")
    def healthz():
        """For a monitoring tool or a quick look in the browser: no sign-in, no names, nothing but yes/no and the time."""
        from .ops import health as ops_health, settings as hub_settings
        ops = app.extensions["ops"]
        now = app.config["CLOCK"]()
        try:
            store.users()                                              # the data folder can be read
            states = ops_health.job_states(ops, hub_settings.load(ops), now)
        except Exception as exc:                                       # noqa: BLE001 - this page must always answer
            app.logger.error("healthz: %s", exc)
            return jsonify(ok=False, time=now.isoformat(timespec="seconds"), collectors={}), 503
        return jsonify(ok=True, time=now.isoformat(timespec="seconds"),
                       collectors={job: bool(state.get("online")) for job, state in states.items()})

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
