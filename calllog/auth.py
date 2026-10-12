"""Request helpers: current user, CSRF, login throttling, audit entries, access decorators."""
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps

from flask import current_app, g, jsonify, redirect, request, session, url_for

from .ops import overlay
from .ops.schemas import ALL_TEAMS_MARK
from .storage import iso_now

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def store():
    return current_app.extensions["store"]


def now():
    return current_app.config["CLOCK"]()


def today():
    return now().date().isoformat()


def wants_json():
    return request.path.startswith("/api/")


def audit(action, target=None, details=None, actor=None):
    """Write one line to the audit log. ``actor`` defaults to the signed-in user."""
    who = actor if actor is not None else g.get("user")
    store().audit({
        "ts": iso_now(now()),
        "actor": who["user_id"] if who else None,
        "actor_name": who.get("name") if who else None,
        "action": action,
        "target": target,
        "ip": request.remote_addr,
        "details": details or {},
    })


# --------------------------------------------------------------------- CSRF
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_ok():
    expected = session.get("csrf", "")
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    return bool(expected) and hmac.compare_digest(sent.encode(), expected.encode())


# ----------------------------------------------------------- login throttling
class Throttle:
    """Allow ``limit`` failures per ``window`` seconds per key (in memory, per process)."""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key):
        q = self._hits[key]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def retry_after(self, key):
        with self._lock:
            q = self._trim(key)
            if len(q) < self.limit:
                return 0
            return int(q[0] + self.window - time.monotonic()) + 1

    def fail(self, key):
        with self._lock:
            self._trim(key).append(time.monotonic())

    def clear(self, key):
        with self._lock:
            self._hits.pop(key, None)


def make_throttles():
    """Fresh failure counters for one app instance: per (user, address) and per address."""
    return {"user": Throttle(limit=5, window=600), "ip": Throttle(limit=30, window=600)}


def throttles():
    return current_app.extensions["throttles"]


# ----------------------------------------------------------------- decorators
def _deny(status):
    if wants_json() or request.path.startswith("/export/"):
        msg = "Please sign in." if status == 401 else "You do not have access to this."
        return jsonify(error=msg), status
    if status == 401:
        return redirect(url_for("pages.login"))
    return redirect(url_for("pages.index"))


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user:
            return _deny(401)
        return view(*args, **kwargs)
    return wrapper


def roles_required(*roles):
    """Allow only signed-in users whose role is in ``roles`` (401 when anonymous, 403 otherwise)."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not g.user:
                return _deny(401)
            if g.user["role"] not in roles:
                return _deny(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


admin_required = roles_required("admin")

# Operations Hub: administrators and managers see the module pages and act on e-mails; the
# difference between them (settings, users, a manager's team) is checked where it matters.
OPS_ROLES = ("admin", "manager")
ops_view_required = roles_required(*OPS_ROLES)
ops_act_required = roles_required(*OPS_ROLES)


# ------------------------------------------------------------ team scope (managers)
def team_scope(user=None):
    """The one team ``user`` is limited to, or None when they see every team (admins; managers with no team or '__all__')."""
    user = user if user is not None else g.get("user")
    if not user or user.get("role") == "admin":
        return None
    team = str(user.get("team") or "").strip()
    return None if team in ("", ALL_TEAMS_MARK) else team


def may_act_on_team(team, user=None):
    """May ``user`` act on something that belongs to ``team``?"""
    scope = team_scope(user)
    return scope is None or str(team or "").strip().casefold() == scope.casefold()


def scope_email_rows(rows, user=None):
    """E-mail rows ``user`` may see (a manager with a team sees that team's rows only)."""
    return overlay.scope_rows(rows, user if user is not None else g.get("user"))
