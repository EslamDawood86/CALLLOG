"""HTML pages of the Operations Hub: /hub /my /calls /email /sla /itsm /system (docs/HUB-DESIGN.md, sections 3 and 8).

The pages are thin shells: the data comes from ``/api/ops`` and is drawn by ``static/*.js`` (strict CSP: no inline script or style).
This module only decides WHO may open WHICH page and what the shell needs to know:

* anonymous            -> the sign-in page
* employee             -> /my (and the Call Log page /dashboard); every other Hub page sends them back to /my
* manager              -> /hub /email /sla /itsm; /system sends them to /hub
* administrator        -> everything; /calls is the existing Call Log dashboard (/admin)
* /my                  -> everybody; an administrator or manager must say whose page with ``?as=<user id>`` (read-only), otherwise /hub
"""
from functools import wraps

from flask import Blueprint, g, redirect, render_template, request, url_for

from .. import auth
from ..storage import norm_uid

bp = Blueprint("hub_pages", __name__)


def home_for(user):
    """Where a person starts: the Admin dashboard for admin / manager, My work for an employee."""
    return "/hub" if user["role"] in auth.OPS_ROLES else "/my"


def page(*roles):
    """Page gate. Anonymous -> sign in; a role that may not see the page -> that person's own start page (never a bare error)."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not g.user:
                return redirect(url_for("pages.login"))
            if roles and g.user["role"] not in roles:
                return redirect(home_for(g.user))
            return view(*args, **kwargs)
        return wrapper
    return decorator


def employees_for_picker():
    """Active employees for the "View as employee" picker (id, name, department), by name."""
    users = [u for u in auth.store().users().values() if u.get("role") == "user" and u.get("active", True)]
    users.sort(key=lambda u: (str(u.get("name") or "").casefold(), u["user_id"]))
    return [{"user_id": u["user_id"], "name": u.get("name") or u["user_id"], "department": u.get("department", "")} for u in users]


@bp.get("/hub")
@page(*auth.OPS_ROLES)
def hub():
    return render_template("hub.html", employees=employees_for_picker(), today=auth.today())


@bp.get("/calls")
@page("admin")
def calls():
    return redirect("/admin#overview")


@bp.get("/email")
@page(*auth.OPS_ROLES)
def email():
    return render_template("email.html")


@bp.get("/sla")
@page(*auth.OPS_ROLES)
def sla():
    return render_template("sla.html")


@bp.get("/itsm")
@page(*auth.OPS_ROLES)
def itsm():
    return render_template("itsm.html")


@bp.get("/system")
@page("admin")
def system():
    return render_template("system.html")


@bp.get("/my")
@page()
def my():
    wanted = norm_uid(request.args.get("as"))
    if g.user["role"] not in auth.OPS_ROLES:
        if wanted and wanted != g.user["user_id"]:
            return redirect("/my")                      # an employee only ever opens their own page
        return render_template("my.html", view_as=None)
    if not wanted:
        return redirect("/hub")
    target = auth.store().get_user(wanted)
    if not target:
        return render_template("message.html", title="Person not found",
                               message="There is no account with that user ID. Go back to the Home page and choose from the list."), 404
    return render_template("my.html", view_as={"user_id": target["user_id"], "name": target.get("name") or target["user_id"]})
