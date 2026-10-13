"""Operations Hub JSON API, mounted at ``/api/ops`` (see docs/HUB-DESIGN.md, section 7).

What lives here: the two dashboards (``/shared`` for everybody, ``/my`` for the signed-in person), the module answers
(snapshot + collector health + e-mail overrides applied), the e-mail actions (assign, no-action, team, quality,
classification rules), jobs ("Run now"), settings, reports, history, events and the application health.

Rules this file keeps:
* the web app never talks to Outlook or the portals - it reads snapshots and writes override files;
* an employee only ever gets numbers (``/shared``), the rows that are theirs and the e-mails of their own teams
  (``/my``) - never a mail of the "Unclassified" bucket unless it was handed to them by name;
* a manager acts only on the e-mails of their own teams (an account can hold several teams);
* every write is validated, answered as JSON and written to the audit log (``ops_*`` actions);
* scraped / e-mail text is returned as data only - the pages show it with textContent.
"""
import hashlib
import json
from datetime import date, datetime, timedelta
from urllib.parse import quote

from flask import Blueprint, current_app, g, jsonify, request, send_file

from .. import auth, overview as calls_overview, stats as call_stats
from ..api import public_user
from ..queries import ApiError, json_body, parse_day, reasons_by_key
from ..storage import STATUSES, StorageError, norm_uid
from . import health, mailbox, notifications, overlay, schemas
from . import settings as hub_settings
from .engines.common import norm_arabic
from .store import now_iso

bp = Blueprint("ops_api", __name__)

RATINGS = ("Excellent", "Good", "Needs Improvement")
DECISIONS = ("Accepted", "Rejected")
# Pre-set reasons offered when rating a reply (from the old e-mail dashboard) so ratings stay analysable.
QUALITY_REASONS = ["Incomplete answer", "Wrong information", "Tone / wording", "Too slow", "Missing follow-up",
                   "Not escalated properly"]
MANAGER_JOBS = ("email", "sla", "itsm")
MODULE_LABELS = {"email": "E-mail", "sla": "Security requests", "itsm": "ITSM"}
MODULE_LINKS = {"email": "/email", "sla": "/sla", "itsm": "/itsm"}
_SECRET_KEYS = {"password", "passwd", "pwd", "secret", "token", "api_key", "apikey"}
_MAX_LIST = 100
_MAX_ASSIGNED = 300          # assigned mails listed on My work (the page shows them 10 per page)


# ----------------------------------------------------------------------------- shared helpers
def ops():
    return current_app.extensions["ops"]


def hub_cfg():
    """The Hub settings (cached for the request; ``forget_settings`` after a change)."""
    cached = g.get("_hub_settings")
    if cached is None:
        cached = g._hub_settings = hub_settings.load(ops())
    return cached


def forget_settings():
    g.pop("_hub_settings", None)


def read_snapshot(module):
    """(snapshot or None, problem text or None). A damaged file is reported to the page instead of failing it."""
    try:
        return ops().read_snapshot(module), None
    except StorageError as exc:
        return None, str(exc)[:300]


def clean_text(value, field, maxlen, required=False, minlen=0, multiline=False):
    text = str(value or "").strip() if multiline else " ".join(str(value or "").split())
    if required and not text:
        raise ApiError(f"{field} is required.")
    if text and len(text) < minlen:
        raise ApiError(f"{field} is too short (at least {minlen} characters).")
    if len(text) > maxlen:
        raise ApiError(f"{field} is too long (max {maxlen} characters).")
    return text


def optional_body():
    """The JSON object of a request that may also carry its arguments in the query string (DELETE)."""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def clean_message_id(value):
    text = str(value or "").strip()
    if not text or len(text) > 500 or any(ord(c) < 32 for c in text):
        raise ApiError("message_id is required.")
    return text


def minutes_since(stamp, now=None):
    try:
        return max(0, int(((now or auth.now()) - datetime.fromisoformat(str(stamp))).total_seconds() // 60))
    except (TypeError, ValueError):
        return None


def _fmt_threshold(minutes):
    minutes = int(minutes or 0)
    return f"{minutes // 60} h" if minutes and minutes % 60 == 0 else f"{minutes} min"


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


# ----------------------------------------------------------------------------- collector state
def job_states():
    cached = g.get("_job_states")
    if cached is None:
        cached = g._job_states = health.job_states(ops(), hub_cfg(), auth.now())
    return cached


def module_meta(module, snap, problem=None):
    """Freshness of one module: is its collector alive (``online``), is the data too old (``stale``)."""
    cfg, now = hub_cfg(), auth.now()
    jobs = {job: state for job, state in job_states().items() if schemas.JOB_MODULE[job] == module}
    main = jobs[module]
    interval = health.module_interval(cfg, module)
    age = minutes_since(snap.get("generated_at"), now) if snap else None
    active = hub_settings.is_active(cfg, module, now)
    enabled = bool((cfg.get(module) or {}).get("enabled", True))
    # outside the working hours / days nothing is scheduled, so old data is expected and not a fault
    stale = bool(enabled and active and snap and age is not None and age > 3 * max(1, interval))
    return {"module": module, "enabled": enabled, "interval_minutes": interval, "online": main["online"],
            "stale": stale, "active_now": active, "age_minutes": age,
            "generated_at": snap.get("generated_at") if snap else None, "date": snap.get("date") if snap else None,
            "running": main.get("running", False), "host": main.get("host", ""), "last_status": main.get("last_status"),
            "last_error": main.get("last_error"), "last_ok_at": main.get("last_ok_at"),
            "last_run_at": main.get("last_run_at"), "next_run_at": main.get("next_run_at"),
            "last_summary": main.get("last_summary", ""), "jobs": jobs, "problem": problem,
            "server_time": now.isoformat(timespec="seconds")}


# ----------------------------------------------------------------------------- overrides
def read_overrides():
    return {name: ops().overrides(name) for name in schemas.OVERRIDES}


def overrides_rev(ovr):
    """Short fingerprint of the override lists (pages send it back as ``rev`` to learn that someone acted).

    Almost every request needs it and the lists only grow (assignments, ratings, Done marks), so it is computed once per
    version of the files: the store hands out the same parsed objects until a file changes, and that identity is the key."""
    parts = tuple(ovr.get(name) for name in schemas.OVERRIDES)
    memo = current_app.extensions.setdefault("ops_rev_memo", {})
    held = memo.get("held")                                   # (the parsed lists, their fingerprint): one tuple, set at once
    if held is not None and all(a is b or (not a and not b) for a, b in zip(held[0], parts)):
        return held[1]
    blob = json.dumps(ovr, sort_keys=True, ensure_ascii=False, default=str)
    rev = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]
    memo["held"] = (parts, rev)
    return rev


def _latest_override_stamp(ovr):
    latest = ""
    for entries in ovr.values():
        for entry in entries.values():
            for field in ("marked_at", "reviewed_at", "appeal_at", "decided_at", "done_at"):
                stamp = str(entry.get(field) or "").replace(" ", "T")
                latest = max(latest, stamp)
    return latest


_CACHE_LIMIT = 96


def _view_cache():
    return current_app.extensions.setdefault("ops_view_cache", {})


def _remember(cache, key, snap, value):
    """Store ``value`` for ``key`` (the last item of a key is the 5-second bucket, so old entries can be dropped)."""
    if len(cache) >= _CACHE_LIMIT:
        for old in [k for k in cache if k[-1] != key[-1]]:
            del cache[old]
        if len(cache) >= _CACHE_LIMIT:
            cache.clear()
    cache[key] = (snap, value)


def current_overrides():
    """(override lists, their fingerprint) for a read-only request - read once per request."""
    cached = g.get("_overrides")
    if cached is None:
        ovr = read_overrides()
        cached = g._overrides = (ovr, overrides_rev(ovr))
    return cached


def overlaid_rows(snap, ovr=None, rev=None):
    """All rows of the e-mail snapshot with the overrides applied (every team, shared by every scope below). Callers
    must treat the list and its rows as read-only."""
    if ovr is None:
        ovr, rev = current_overrides()
    rev = rev if rev is not None else overrides_rev(ovr)
    now = auth.now()
    key = ("rows", snap.get("generated_at"), rev, int(now.timestamp() // 5))
    cache = _view_cache()
    hit = cache.get(key)
    if hit is not None and hit[0] is snap:                # same parsed file, not just the same timestamp
        return hit[1]
    rows = overlay.apply_overrides((snap.get("data") or {}).get("rows") or [], ovr, now)
    _remember(cache, key, snap, rows)
    return rows


def email_view(snap, scope_team=None, ovr=None, hide_unclassified=False, rev=None):
    """The e-mail snapshot data with overrides applied, narrowed to a scope (None = every team, else a team name or a
    tuple of names) and recounted.

    Many pages ask for the same thing every few seconds, so the result is shared for a moment (and until the snapshot
    or an override changes). Callers must treat it as read-only."""
    ovr = ovr if ovr is not None else read_overrides()
    rev = rev if rev is not None else overrides_rev(ovr)
    scope = overlay.as_scope(scope_team)
    now = auth.now()
    key = ("view", snap.get("generated_at"), rev, overlay.scope_key(scope), bool(hide_unclassified),
           int(now.timestamp() // 5))
    cache = _view_cache()
    hit = cache.get(key)
    if hit is not None and hit[0] is snap:
        return hit[1]
    view = overlay.view_email_data(snap.get("data") or {}, ovr, scope, now,
                                   rows=overlaid_rows(snap, ovr, rev), hide_unclassified=hide_unclassified)
    _remember(cache, key, snap, view)
    return view


def current_email_view(scope_team=None):
    """(view or None, snapshot or None) for the latest e-mail snapshot."""
    snap, _ = read_snapshot("email")
    return (email_view(snap, scope_team) if snap else None), snap


def team_mail(snap, scope, ovr=None, rev=None):
    """(items, summary) of the e-mails of ``scope`` for the 'my team' list (see ``overlay.team_mail``), computed once
    per snapshot version, overrides version and team set and shared by everybody with the same teams. Read-only."""
    if ovr is None:
        ovr, rev = current_overrides()
    rev = rev if rev is not None else overrides_rev(ovr)
    now = auth.now()
    key = ("team_mail", snap.get("generated_at"), rev, overlay.scope_key(scope), int(now.timestamp() // 5))
    cache = _view_cache()
    hit = cache.get(key)
    if hit is not None and hit[0] is snap:
        return hit[1]
    value = overlay.team_mail(overlaid_rows(snap, ovr, rev), scope)
    _remember(cache, key, snap, value)
    return value


# ----------------------------------------------------------------------------- headline numbers
EMAIL_HEADLINE = ("business_emails", "replied", "pending", "no_action_needed", "needed_reply", "reply_rate",
                  "sla_breached", "sla_compliance_rate", "escalated_count", "carryover_count", "carryover_oldest_days",
                  "assigned_count", "assigned_done_count", "average_response")


def email_headline(stats):
    return {k: stats.get(k, 0) for k in EMAIL_HEADLINE}


def sla_headline(data):
    totals = data.get("totals") or {}
    return {k: totals.get(k, 0) for k in ("open", "within", "reminder", "breached", "no_date", "oldest_minutes")}


def calls_totals(day):
    """Team calls of one day from the Call Log storage (counts only)."""
    data = calls_overview.merged(day, day)
    t = data["totals"]
    users = auth.store().users()
    employees = sum(1 for u in users.values() if u.get("role") == "user" and u.get("active", True))
    return {"date": day, "total": t["total"], **{s: t[s] for s in STATUSES}, "other": t.get("other", 0),
            "active_employees": sum(1 for u in data["users"].values() if u["total"] > 0), "employees": employees,
            "_hours": call_stats.chart_buckets(data["rollups"], "hour", day, day)}


def _public_calls(totals):
    return {k: v for k, v in totals.items() if not k.startswith("_")}


def shared_scope(user, viewer=None):
    """The teams the 'Team status' strip counts e-mails of for ``user``: None = every team. When somebody else (a
    manager) looks at another person's page, only what both of them may see."""
    scope = overlay.scope_for_user(user)
    if viewer is not None:
        scope = overlay.intersect_scopes(scope, overlay.scope_for_user(viewer))
    return scope


def build_shared(user=None, viewer=None):
    """Section 3.1: counts only - never a subject, a customer or a name. The ONE function behind ``/shared``,
    ``/my`` (its ``shared`` block) and Home, so every dashboard shows the same numbers for the same viewer.

    Calls, security requests and ITSM are sector-wide counts. The e-mail numbers are scoped to the viewer: an
    administrator (and a manager of every team) gets all rows, everybody else the rows of their own teams - an employee
    never counts a mail of the "Unclassified" bucket. The block says so with ``scope`` ('all' | 'teams') and ``teams``."""
    user = user if user is not None else g.user
    scope = shared_scope(user, viewer)
    hide = user.get("role") == "user"
    snaps = {m: read_snapshot(m) for m in schemas.MODULES}
    modules, updates = {}, []
    ovr, rev = current_overrides()
    for module in schemas.MODULES:
        snap, problem = snaps[module]
        meta = module_meta(module, snap, problem)
        block = {"available": bool(snap), "online": meta["online"], "stale": meta["stale"], "enabled": meta["enabled"],
                 "updated_at": meta["generated_at"], "date": meta["date"]}
        if module == "email":
            block.update(scope="all" if scope is None else "teams", teams=list(scope or ()))
        if snap:
            updates.append(snap["generated_at"])
            data = snap.get("data") or {}
            if module == "email":
                block.update(email_headline(email_view(snap, scope, ovr, hide, rev)["stats"]))
            elif module == "sla":
                block.update(sla_headline(data))
            else:
                block["totals"] = dict(data.get("totals") or {})
        modules[module] = block
    enabled = [b for b in modules.values() if b["enabled"]]
    return {"generated_at": auth.now().isoformat(timespec="seconds"),
            "calls": _public_calls(calls_totals(auth.today())),
            "email": modules["email"], "sla": modules["sla"], "itsm": modules["itsm"],
            "collector": {"online": bool(enabled) and all(b["online"] for b in enabled),
                          "last_update": max(updates) if updates else None,
                          "modules": {m: {"online": b["online"], "stale": b["stale"], "updated_at": b["updated_at"]}
                                      for m, b in modules.items()}}}


@bp.get("/shared")
@auth.login_required
def shared():
    return jsonify(build_shared())


# ----------------------------------------------------------------------------- my work (section 3.2)
def _day_summary(uid, day, reasons):
    recs = auth.store().day_records(day, uid)
    s = call_stats.summarize(recs, reasons)
    return recs, {"date": day, "total": s["total"], **{k: s[k] for k in STATUSES}, "other": s["other"],
                  "first_call": s["first_call"], "last_call": s["last_call"], "_by_reason": s["by_reason"]}


def my_calls(uid, day):
    reasons = reasons_by_key()
    today = date.fromisoformat(day)
    daily, today_recs = [], []
    for back in range(6, -1, -1):
        d = (today - timedelta(days=back)).isoformat()
        recs, summary = _day_summary(uid, d, reasons)
        if back == 0:
            today_recs = recs
        daily.append(summary)
    by_reason = [{"key": k, "label_ar": reasons.get(k, {}).get("label_ar", k),
                  "label_en": reasons.get(k, {}).get("label_en", k), "count": n}
                 for k, n in sorted(daily[-1]["_by_reason"].items(), key=lambda kv: -kv[1])]
    daily = [{k: v for k, v in d.items() if not k.startswith("_")} for d in daily]
    last7 = {k: sum(d[k] for d in daily) for k in ("total", *STATUSES, "other")}
    return {"today": daily[-1], "yesterday": daily[-2], "last7": last7, "daily": daily,
            "per_hour_today": call_stats.buckets(today_recs, reasons, "hour"), "by_reason_today": by_reason}


def _my_email_row(row):
    """Only what the person needs about a mail that is theirs."""
    return {"message_id": row.get("message_id", ""), "subject": row.get("subject", ""),
            "customer_name": row.get("customer_name", ""), "team": row.get("team", ""),
            "received_time": row.get("received_time"), "status": row.get("status", ""),
            "sla_status": row.get("sla_status", ""), "escalated": bool(row.get("escalated")),
            "waiting": row.get("current_waiting_time", "") or row.get("first_response_time", ""),
            "first_reply_time": row.get("first_reply_time"), "response_minutes": row.get("response_minutes"),
            "assigned_by": row.get("assigned_by", ""), "assigned_at": row.get("assigned_at", ""),
            "note": row.get("assigned_note", ""), "quality_rating": row.get("quality_rating", ""),
            "is_carryover": bool(row.get("is_carryover")),
            "assignment_done": bool(row.get("assignment_done")), "done_at": row.get("done_at", ""),
            "done_by": row.get("done_by", ""), "done_note": row.get("done_note", ""),
            "done_auto": bool(row.get("done_auto")), "mail_copy": row.get("mail_copy", "")}


def _team_summary(teams, all_teams=False):
    return {"teams": list(teams), "all_teams": all_teams, "pending": 0, "replied": 0, "no_action": 0, "sla_breached": 0,
            "escalated": 0, "reply_rate": 0.0, "listed": 0, "more": 0}


def _one_row_per_mail(rows):
    """A mail the collector split into one row per team is still ONE assignment: keep a single row (a pending one wins)."""
    kept, at = [], {}
    for row in rows:
        key = row.get("message_id") or id(row)
        if key not in at:
            at[key] = len(kept)
            kept.append(row)
        elif row.get("status") == schemas.EMAIL_PENDING and kept[at[key]].get("status") != schemas.EMAIL_PENDING:
            kept[at[key]] = row
    return kept


def my_email(user, viewer=None):
    """The e-mail part of /my: the mails that are the person's by name (``assigned`` / ``replied``) and the e-mails of
    their own teams (``team_emails`` + ``team_summary``; never the "Unclassified" bucket).

    ``viewer`` = the manager / administrator looking at somebody else's page: the manager only sees what both of them
    may see."""
    email = overlay.normalize_email(user.get("email"))
    own_scope = overlay.scope_for_user(user)
    scope = overlay.intersect_scopes(own_scope, overlay.scope_for_user(viewer)) if viewer is not None else own_scope
    hint = ""
    if scope is not None and not scope:
        hint = "no_team" if not own_scope else "out_of_scope"
    empty = {"configured": bool(email), "available": False, "email": email,
             "assigned": {"pending": 0, "done": 0, "other": 0, "items": []},
             "replied": {"count": 0, "average_first_reply_minutes": None, "sla_met": 0, "sla_breached": 0, "items": []},
             "team_emails": [], "team_summary": _team_summary(scope or (), scope is None), "team_emails_hint": hint}
    snap, _ = read_snapshot("email")
    if not snap:
        return empty
    ovr, rev = current_overrides()
    out = {**empty, "available": True, "date": snap.get("date"), "updated_at": snap.get("generated_at")}
    if scope is None or scope:
        items, summary = team_mail(snap, scope, ovr, rev)
        out["team_emails"], out["team_summary"] = items, {**summary, "teams": list(summary["teams"])}
    if not email:
        return out
    rows = overlaid_rows(snap, ovr, rev)
    if viewer is not None:                        # a manager looking at someone else sees only rows of their own teams
        rows = overlay.scope_rows(rows, viewer)
    mine_assigned, mine_replied = _one_row_per_mail(overlay.assigned_rows(rows, email)), overlay.replied_rows(rows, email)
    open_rows = [r for r in mine_assigned if not r.get("assignment_done") and r.get("status") == schemas.EMAIL_PENDING]
    done_rows = [r for r in mine_assigned if r.get("assignment_done")]
    listed = {id(r) for r in open_rows} | {id(r) for r in done_rows}
    other_rows = [r for r in mine_assigned if id(r) not in listed]        # closed by somebody else / no action needed
    newest = lambda r: (str(r.get("assigned_at") or ""), str(r.get("received_time") or ""))    # noqa: E731
    ordered = [r for group in (open_rows, done_rows, other_rows) for r in sorted(group, key=newest, reverse=True)]
    times = [m for m in (overlay.response_minutes(r) for r in mine_replied) if m is not None]
    mine_replied.sort(key=lambda r: str(r.get("first_reply_time") or ""), reverse=True)
    out["assigned"] = {"pending": len(open_rows), "done": len(done_rows), "other": len(other_rows),
                       "items": [_my_email_row(r) for r in ordered[:_MAX_ASSIGNED]]}
    out["replied"] = {"count": len(mine_replied),
                      "average_first_reply_minutes": round(sum(times) / len(times), 1) if times else None,
                      "sla_met": sum(1 for r in mine_replied if r.get("sla_status") == "Met"),
                      "sla_breached": sum(1 for r in mine_replied if r.get("sla_status") == "Breached"),
                      "items": [_my_email_row(r) for r in mine_replied[:_MAX_LIST]]}
    return out


def _my_review(message_id, entry):
    open_appeal = bool(entry.get("appeal")) and not entry.get("appeal_decision")
    return {"message_id": message_id, "subject": entry.get("subject", ""), "rating": entry.get("rating", ""),
            "note": entry.get("note", ""), "reasons": entry.get("reasons", ""),
            "reviewed_by": entry.get("reviewed_by", ""), "reviewed_at": entry.get("reviewed_at", ""),
            "appeal": entry.get("appeal", ""), "appeal_at": entry.get("appeal_at", ""),
            "appeal_decision": entry.get("appeal_decision", ""), "decided_at": entry.get("decided_at", ""),
            "rating_withdrawn": bool(entry.get("rating_withdrawn")),
            "can_appeal": not (entry.get("appeal") or entry.get("appeal_decision") or entry.get("rating_withdrawn")),
            "appeal_open": open_appeal}


def my_quality(user):
    email = overlay.normalize_email(user.get("email"))
    if not email:
        return {"configured": False, "reviews": [], "counts": {}}
    mine = [(mid, e) for mid, e in ops().overrides("quality").items()
            if overlay.normalize_email(e.get("replied_by_email")) == email]
    mine.sort(key=lambda kv: str(kv[1].get("reviewed_at") or ""), reverse=True)
    counted = [e for _, e in mine if not e.get("rating_withdrawn")]
    return {"configured": True,
            "counts": {"reviewed": len(counted),
                       "excellent": sum(1 for e in counted if e.get("rating") == "Excellent"),
                       "good": sum(1 for e in counted if e.get("rating") == "Good"),
                       "needs_improvement": sum(1 for e in counted if e.get("rating") == "Needs Improvement"),
                       "appeals": sum(1 for _, e in mine if e.get("appeal")),
                       "withdrawn": sum(1 for _, e in mine if e.get("rating_withdrawn"))},
            "reviews": [_my_review(mid, e) for mid, e in mine[:50]]}


def my_sla(user):
    name = str(user.get("security_name") or "").strip()
    totals = {"open": 0, "within": 0, "reminder": 0, "breached": 0, "no_date": 0, "oldest_minutes": 0}
    empty = {"configured": bool(name), "available": False, "security_name": name, "totals": totals, "requests": []}
    if not name:
        return empty
    snap, _ = read_snapshot("sla")
    if not snap:
        return empty
    wanted = norm_arabic(name)
    mine = [r for r in (snap.get("data") or {}).get("requests") or []
            if isinstance(r, dict) and norm_arabic(r.get("security_user")) == wanted]
    for r in mine:
        totals["open"] += 1
        if r.get("status") in schemas.SLA_STATUSES:
            totals[r["status"]] += 1
        totals["oldest_minutes"] = max(totals["oldest_minutes"], r.get("age_minutes") or 0)
    return {**empty, "available": True, "updated_at": snap.get("generated_at"),
            "thresholds": (snap.get("data") or {}).get("thresholds") or {}, "totals": totals,
            "requests": [{"key": r.get("key", ""), "sent_at": r.get("sent_at"), "age_minutes": r.get("age_minutes"),
                          "status": r.get("status"), "fields": r.get("fields") or {}}
                         for r in mine[:_MAX_LIST]]}


def build_my(user, viewer=None):
    day = auth.today()
    return {"user": public_user(user), "read_only": viewer is not None, "viewer": viewer["user_id"] if viewer else None,
            "date": day, "server_time": auth.now().isoformat(timespec="seconds"),
            "calls": my_calls(user["user_id"], day), "email": my_email(user, viewer), "quality": my_quality(user),
            "sla": my_sla(user), "shared": build_shared(user, viewer)}


@bp.get("/my")
@auth.login_required
def my():
    target, viewer = g.user, None
    wanted = norm_uid(request.args.get("as"))
    if wanted and wanted != g.user["user_id"]:
        if g.user["role"] not in auth.OPS_ROLES:
            raise ApiError("Only administrators and managers can open another person's page.", 403)
        target = auth.store().get_user(wanted)
        if not target:
            raise ApiError("User not found.", 404)
        viewer = g.user
        auth.audit("ops_view_as", target=wanted)
    return jsonify(build_my(target, viewer))


# ----------------------------------------------------------------------------- overview (Home)
def _attention(level, module, text, link=None):
    return {"level": level, "module": module, "text": text, "link": link or MODULE_LINKS.get(module, "/hub")}


def build_attention(email_stats, email_rows, sla_data, itsm_data, metas):
    items = []
    if sla_data is not None:
        totals, limits = sla_data.get("totals") or {}, sla_data.get("thresholds") or {}
        if totals.get("breached"):
            items.append(_attention("danger", "sla", f"{_plural(totals['breached'], 'security request')} passed the "
                                                      f"{_fmt_threshold(limits.get('breach_minutes', 120))} SLA"))
        if totals.get("reminder"):
            items.append(_attention("warn", "sla", f"{_plural(totals['reminder'], 'security request')} reached the "
                                                     f"reminder time ({_fmt_threshold(limits.get('warning_minutes', 60))})"))
        if totals.get("no_date"):
            items.append(_attention("info", "sla", f"{_plural(totals['no_date'], 'security request')} without a sent date"))
    if email_stats is not None:
        if email_stats.get("escalated_count"):
            items.append(_attention("danger", "email", f"{_plural(email_stats['escalated_count'], 'e-mail')} waiting past "
                                                         f"the SLA with no team reply"))
        late = (email_stats.get("sla_breached") or 0) - (email_stats.get("escalated_count") or 0)
        if late > 0:
            items.append(_attention("warn", "email", f"{_plural(late, 'e-mail')} answered after the SLA"))
        if email_stats.get("carryover_count"):
            items.append(_attention("warn", "email", f"{_plural(email_stats['carryover_count'], 'e-mail')} carried over "
                                                       f"from earlier days (oldest {email_stats.get('carryover_oldest_days', 0)} days)"))
        unclassified = sum(1 for r in email_rows if str(r.get("team") or "").casefold() == "unclassified")
        if unclassified:
            items.append(_attention("info", "email", f"{_plural(unclassified, 'e-mail')} not classified to a team"))
        appeals = sum(1 for r in email_rows if r.get("quality_appeal") and not r.get("appeal_decision"))
        if appeals:
            items.append(_attention("info", "email", f"{_plural(appeals, 'quality appeal')} waiting for a decision"))
    if itsm_data is not None:
        for category, n in (itsm_data.get("totals") or {}).items():
            if n and "rollback" in str(category).lower():
                items.append(_attention("warn", "itsm", f"{_plural(n, 'rollback request')} waiting"))
        for role in itsm_data.get("roles") or []:
            if role.get("error"):
                items.append(_attention("danger", "itsm", f"ITSM role '{str(role.get('role'))[:60]}' could not be read: "
                                                            f"{str(role['error'])[:120]}"))
    for module, meta in metas.items():
        label = MODULE_LABELS[module]
        if not meta["enabled"]:
            continue
        has_hb = meta["jobs"][module].get("present")
        if not has_hb and not meta["generated_at"]:
            items.append(_attention("info", module, f"{label}: no data yet - the collector has not run", "/system"))
            continue
        if has_hb and not meta["online"] and meta["active_now"]:
            items.append(_attention("danger", module, f"{label} collector is offline (last seen "
                                                        f"{meta['jobs'][module].get('age_minutes')} min ago)", "/system"))
        elif meta["stale"]:
            items.append(_attention("warn", module, f"{label} data is {meta['age_minutes']} minutes old", "/system"))
        for job, state in meta["jobs"].items():
            if state.get("last_status") == "error":
                what = "scan" if job == module else "daily report"
                items.append(_attention("danger", module, f"The last {label} {what} failed: "
                                                            f"{state.get('last_error') or 'unknown error'}", "/system"))
    order = {"danger": 0, "warn": 1, "info": 2}
    return sorted(items, key=lambda i: order[i["level"]])


def _team_table(stats):
    rows = [{"team": team, **{k: t.get(k, 0) for k in ("business_emails", "replied", "pending", "no_action_needed",
                                                        "escalated_count", "carryover_count", "sla_breached_count")}}
            for team, t in (stats.get("team_stats") or {}).items() if t.get("business_emails")]
    rows.sort(key=lambda r: (-r["pending"], -r["business_emails"], r["team"]))
    return rows


@bp.get("/overview")
@auth.ops_view_required
def overview():
    day = auth.today()
    yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    today_calls = calls_totals(day)
    snaps = {m: read_snapshot(m) for m in schemas.MODULES}
    metas = {m: module_meta(m, *snaps[m]) for m in schemas.MODULES}
    scope = auth.team_scope()
    scope_text = overlay.scope_label(scope)
    ovr, rev = current_overrides()

    email_stats = email_rows = None
    email_block = {"available": False, "scope": scope_text}
    if snaps["email"][0]:
        view = email_view(snaps["email"][0], scope, ovr, rev=rev)
        email_stats, email_rows = view["stats"], view["rows"]
        email_block = {"available": True, "scope": scope_text, **email_headline(email_stats),
                       "quality": email_stats.get("quality", {}), "teams": _team_table(email_stats),
                       "unclassified": sum(1 for r in email_rows if str(r.get("team") or "").casefold() == "unclassified"),
                       "updated_at": snaps["email"][0]["generated_at"]}
    sla_data = (snaps["sla"][0] or {}).get("data")
    sla_block = {"available": False}
    if sla_data is not None:
        sla_block = {"available": True, **sla_headline(sla_data), "thresholds": sla_data.get("thresholds") or {},
                     "top_users": (sla_data.get("by_user") or [])[:5], "updated_at": snaps["sla"][0]["generated_at"]}
    itsm_data = (snaps["itsm"][0] or {}).get("data")
    itsm_block = {"available": False}
    if itsm_data is not None:
        changes = itsm_data.get("changes") or {}
        itsm_block = {"available": True, "categories": itsm_data.get("categories") or [],
                      "totals": itsm_data.get("totals") or {},
                      "role_errors": [{"role": r.get("role"), "error": str(r.get("error"))[:160]}
                                      for r in itsm_data.get("roles") or [] if r.get("error")],
                      "changes": {"new": len(changes.get("new") or []), "closed": len(changes.get("closed") or [])},
                      "updated_at": snaps["itsm"][0]["generated_at"]}
    yesterday_calls = calls_totals(yesterday)
    return jsonify(
        server_time=auth.now().isoformat(timespec="seconds"), date=day, scope=scope_text,
        scope_teams=None if scope is None else list(scope), shared=build_shared(),
        calls={"today": _public_calls(today_calls), "yesterday": _public_calls(yesterday_calls),
               "per_hour_today": today_calls["_hours"]},
        email=email_block, sla=sla_block, itsm=itsm_block,
        attention=build_attention(email_stats, email_rows or [], sla_data, itsm_data, metas),
        collectors=job_states())


# ----------------------------------------------------------------------------- module answers
def people_directory(scope_team=None):
    """[{email, name, team}] of the people an e-mail can be assigned to (settings team members). ``scope_team`` = None
    (everybody) or the team names of a manager."""
    scope = overlay.as_scope(scope_team)
    email_cfg = hub_cfg()["email"]
    seen, out = set(), []
    pairs = [(team, addr) for team, addrs in (email_cfg.get("team_members_by_team") or {}).items() for addr in addrs]
    pairs += [("", addr) for addr in email_cfg.get("team_members") or []]
    for team, addr in pairs:
        addr = overlay.normalize_email(addr)
        if not addr or addr in seen:
            continue
        if scope is not None and not overlay.team_in(scope, team):
            continue
        seen.add(addr)
        out.append({"email": addr, "name": overlay.display_name_from_email(addr), "team": team})
    out.sort(key=lambda p: (p["name"].casefold(), p["email"]))
    return out


def email_options(scope_team=None):
    """Lists for the e-mail page. ``scope`` = the teams text of a manager ('Flex Cube' or 'Flex Cube, Card Management'),
    None for every team; ``scope_teams`` = the same as a list (None for every team)."""
    scope = overlay.as_scope(scope_team)
    email_cfg = hub_cfg()["email"]
    cls = email_cfg["classification"]
    rules = {k: v for k, v in cls["system_team_map"].items() if overlay.team_in(scope, v)}
    return {"teams": list(email_cfg["teams"]), "quality_reasons": list(QUALITY_REASONS), "ratings": list(RATINGS),
            "people": people_directory(scope), "unclassified_team": cls["unclassified_team"],
            "system_team_map": rules, "scope": overlay.scope_label(scope),
            "scope_teams": None if scope is None else list(scope)}


@bp.get("/<any(email,sla,itsm):module>")
@auth.ops_view_required
def module_answer(module):
    snap, problem = read_snapshot(module)
    meta = module_meta(module, snap, problem)
    is_email = module == "email"
    scope = auth.team_scope()
    options = email_options(scope) if is_email else None
    if snap is None:
        return jsonify(snapshot=None, meta=meta, **({"options": options} if is_email else {}))
    ovr, rev = current_overrides() if is_email else ({}, "")
    meta["rev"] = rev
    if request.args.get("since") == snap["generated_at"]:
        sent_rev = request.args.get("rev")
        same = (sent_rev == rev) if sent_rev is not None else (
            not is_email or _latest_override_stamp(ovr) < snap["generated_at"].replace(" ", "T"))
        if same:
            return jsonify(unchanged=True, meta=meta)
    out = dict(snap)
    if is_email:
        out["data"] = email_view(snap, scope, ovr, rev=rev)
        out["options"] = options
    out["meta"] = meta
    return jsonify(out)


def _thin(points, limit=300):
    if len(points) <= limit:
        return points
    step = len(points) / (limit - 1)
    picked = [points[int(i * step)] for i in range(limit - 1)]
    return picked + [points[-1]]


@bp.get("/<any(email,sla,itsm):module>/history")
@auth.ops_view_required
def module_history(module):
    try:
        days = max(1, min(int(request.args.get("days", 14)), 60))
    except ValueError:
        raise ApiError("days must be a number.")
    out = []
    for day in ops().history_days(module)[-days:]:
        doc = ops().read_history(module, day) or {}
        out.append({**doc, "day": day, "points": _thin(list(doc.get("points") or []))})
    return jsonify(module=module, days=out)


@bp.get("/events")
@auth.ops_view_required
def events():
    day = parse_day(request.args.get("day"), auth.today())
    try:
        limit = int(request.args.get("limit", 200))
    except ValueError:
        raise ApiError("limit must be a number.")
    items = ops().read_events(day, limit)
    scope = auth.team_scope()
    if scope is not None:
        items = [e for e in (_scope_event(e, scope) for e in items) if e]
    return jsonify(day=day, events=items)


def _scope_event(event, scope):
    """A team manager sees e-mail events only for their teams (items carry subjects)."""
    if event.get("module") != "email" or not event.get("items"):
        return event
    mine = [i for i in event["items"] if isinstance(i, dict) and overlay.team_in(scope, i.get("team"))]
    return {**event, "items": mine} if mine else None


# ----------------------------------------------------------------------------- e-mail actions
def raw_rows(message_id):
    snap, _ = read_snapshot("email")
    return [r for r in ((snap or {}).get("data") or {}).get("rows") or [] if r.get("message_id") == message_id]


def mail_rows(message_id, ovr=None):
    """The rows of one e-mail as people see them (overrides applied); [] when it is not in the latest scan."""
    raw = raw_rows(message_id)
    return overlay.apply_overrides(raw, ovr if ovr is not None else read_overrides(), auth.now()) if raw else []


def current_rows(message_id):
    """The e-mail's rows as people see them now (overrides applied). 404 when it is not in the latest scan; 403 when
    it belongs to another team than the manager's."""
    rows = mail_rows(message_id)
    if not rows:
        raise ApiError("That e-mail is not in the latest scan. Reload the page.", 404)
    scope = auth.team_scope()
    if scope is not None and not any(overlay.row_in_scope(r, scope) for r in rows):
        raise ApiError("That e-mail belongs to another team.", 403)
    return rows


def _stamp():
    return now_iso(auth.now())


def _who():
    return g.user.get("name") or g.user["user_id"]


def _subject(rows):
    return str(rows[0].get("subject") or "")[:200] if rows else ""


def _clean_team(value):
    team = str(value or "").strip()
    if team not in hub_cfg()["email"]["teams"]:
        raise ApiError("Choose one of the teams in the list.")
    return team


# fields of an assignment the assignee's own actions set; a new note for the SAME person must not undo them
_KEPT_WHEN_SAME_PERSON = ("done_at", "done_by", "done_by_id", "done_note", "replied_notified")


def _row_team(rows, entry=None):
    """The team the assignment is filed under: the mail's team (for a mail sent to several teams, the first one the signed-in
    manager looks after, so they can still handle it when it has left the scan)."""
    scope = auth.team_scope()
    row = next((r for r in rows if overlay.row_in_scope(r, scope)), rows[0] if rows else {})
    return str(row.get("team") or (entry or {}).get("team") or "")[:80]


def _notify(kind, entry, rows, message_id, old_entry=None):
    """In-app notifications of one event (docs/FEATURES-V2.md B4). A failure here is logged and never undoes the action."""
    try:
        subject = _subject(rows) or str((entry or {}).get("subject") or "")[:200]
        return notifications.notify(ops(), auth.store().users(), kind, g.user, entry, rows, subject, message_id,
                                    auth.now(), old_entry)
    except Exception as exc:                                    # noqa: BLE001
        current_app.logger.warning("notification %s failed: %s: %s", kind, type(exc).__name__, exc)
        return []


@bp.post("/email/assign")
@auth.ops_act_required
def email_assign():
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    address = overlay.normalize_email(body.get("assigned_to_email"))
    if not overlay.valid_email(address):
        raise ApiError("Enter a valid e-mail address to assign to.")
    name = clean_text(body.get("assigned_to_name"), "Name", 80) or overlay.display_name_from_email(address)
    note = clean_text(body.get("note"), "Note", 300, multiline=True)
    rows = current_rows(message_id)
    team = _row_team(rows)
    seen = {}

    def change(old):
        seen["old"] = old
        same = bool(old and old.get("assigned_to_email") == address)
        new = {"assigned_to_email": address, "assigned_to_name": name, "note": note, "marked_by": _who(),
               "marked_by_id": g.user["user_id"], "marked_at": _stamp(), "subject": _subject(rows), "team": team,
               "notified": bool(same and old.get("notified") and old.get("note") == note)}
        if same:
            new.update({k: old[k] for k in _KEPT_WHEN_SAME_PERSON if old.get(k)})
        return new
    entry = ops().update_override("assignments", message_id, change)
    old = seen.get("old")
    auth.audit("ops_assign", target=message_id, details={"to": address, "name": name, "note": note,
                                                          "subject": _subject(rows)[:80]})
    if not old or old.get("assigned_to_email") != address:       # another person than before: tell them (and the one it was taken from)
        if old:
            _notify(schemas.NOTIFY_UNASSIGNED, old, rows, message_id, old)
        _notify(schemas.NOTIFY_ASSIGNED, entry, rows, message_id)
    return jsonify(ok=True, assignment=entry, rev=overrides_rev(read_overrides()))


def manager_rows(message_id, entry):
    """The rows of a mail for a manager / administrator action: 404 when nothing is known about it, 403 when it belongs to
    another team. A mail that has left the latest scan can still be handled while its assignment remembers its team."""
    rows = mail_rows(message_id)
    scope = auth.team_scope()
    if rows:
        if scope is not None and not any(overlay.row_in_scope(r, scope) for r in rows):
            raise ApiError("That e-mail belongs to another team.", 403)
    elif entry:
        if scope is not None and not overlay.team_in(scope, entry.get("team")):
            raise ApiError("That e-mail belongs to another team.", 403)
    else:
        raise ApiError("That e-mail is not in the latest scan. Reload the page.", 404)
    return rows


@bp.delete("/email/assign")
@auth.ops_act_required
def email_unassign():
    body = optional_body()
    message_id = clean_message_id(body.get("message_id") or request.args.get("message_id"))
    entry = ops().overrides("assignments").get(message_id)
    rows = manager_rows(message_id, entry)
    if not entry:
        raise ApiError("That e-mail is not assigned.", 404)
    taken = {}

    def remove(old):
        taken["old"] = old
        return None
    ops().update_override("assignments", message_id, remove)
    old = taken.get("old") or entry
    auth.audit("ops_unassign", target=message_id, details={"subject": (_subject(rows) or str(old.get("subject") or ""))[:80]})
    _notify(schemas.NOTIFY_UNASSIGNED, old, rows, message_id, old)
    return jsonify(ok=True, rev=overrides_rev(read_overrides()))


@bp.post("/email/no-action")
@auth.ops_act_required
def email_no_action():
    message_id = clean_message_id(json_body().get("message_id"))
    rows = current_rows(message_id)
    if all(r.get("status") == schemas.EMAIL_REPLIED for r in rows):
        raise ApiError("That e-mail already has a reply, so it cannot be marked as needing no action.", 409)
    entry = ops().set_override("no_action", message_id, {
        "marked_by": _who(), "marked_by_id": g.user["user_id"], "marked_at": _stamp(), "subject": _subject(rows)})
    auth.audit("ops_no_action", target=message_id, details={"subject": _subject(rows)[:80]})
    return jsonify(ok=True, mark=entry, rev=overrides_rev(read_overrides()))


@bp.delete("/email/no-action")
@auth.ops_act_required
def email_no_action_undo():
    body = optional_body()
    message_id = clean_message_id(body.get("message_id") or request.args.get("message_id"))
    if not ops().overrides("no_action").get(message_id):
        raise ApiError("That e-mail is not marked as needing no action.", 404)
    scope = auth.team_scope()
    raw = raw_rows(message_id)       # the overlay hides marked carry-over mails, so look at the snapshot itself
    if scope is not None and not any(overlay.row_in_scope(r, scope) for r in raw):
        raise ApiError("That e-mail belongs to another team.", 403)
    ops().delete_override("no_action", message_id)
    auth.audit("ops_no_action_undo", target=message_id, details={"subject": _subject(raw)[:80]})
    return jsonify(ok=True, rev=overrides_rev(read_overrides()))


@bp.post("/email/team")
@auth.ops_act_required
def email_team():
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    team = _clean_team(body.get("team"))
    rows = current_rows(message_id)
    previous = rows[0].get("team", "")
    entry = ops().set_override("team", message_id, {
        "team": team, "marked_by": _who(), "marked_by_id": g.user["user_id"], "marked_at": _stamp(),
        "subject": _subject(rows)})
    auth.audit("ops_team", target=message_id, details={"from": previous, "to": team, "subject": _subject(rows)[:80]})
    return jsonify(ok=True, team=entry, rev=overrides_rev(read_overrides()))


def _clean_reasons(value):
    if value in (None, ""):
        return ""
    items = value if isinstance(value, list) else [p.strip() for p in str(value).split(",")]
    out = []
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        if item not in QUALITY_REASONS:
            raise ApiError(f"'{item[:40]}' is not one of the quality reasons.")
        if item not in out:
            out.append(item)
    return ", ".join(out)


@bp.post("/email/quality")
@auth.ops_act_required
def email_quality():
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    rating = str(body.get("rating") or "").strip()
    if rating not in RATINGS:
        raise ApiError("Rating must be Excellent, Good or Needs Improvement.")
    note = clean_text(body.get("note"), "Note", 500, multiline=True)
    reasons = _clean_reasons(body.get("reasons"))
    rows = current_rows(message_id)
    row = next((r for r in rows if r.get("status") == schemas.EMAIL_REPLIED), None)
    if row is None:
        raise ApiError("Only e-mails that have a reply can be rated.", 409)
    if row.get("reply_by_manager"):
        raise ApiError("Replies written by managers are not quality-reviewed.", 409)
    replier = overlay.normalize_email(row.get("first_reply_email"))
    if replier and replier == overlay.normalize_email(g.user.get("email")):
        raise ApiError("You cannot rate your own reply.", 403)

    def change(old):
        old = old or {}
        return {"rating": rating, "note": note, "reasons": reasons, "replied_by": row.get("first_reply_by", ""),
                "replied_by_email": replier, "reviewed_by": _who(), "reviewed_by_id": g.user["user_id"],
                "reviewed_at": _stamp(), "subject": _subject(rows), "team": row.get("team", ""),
                "feedback_sent": False,
                # an appeal is kept as history; a new rating has not been decided on yet
                "appeal": old.get("appeal", ""), "appeal_by": old.get("appeal_by", ""), "appeal_at": old.get("appeal_at", ""),
                "appeal_decision": "", "decided_by": "", "decided_at": "", "rating_withdrawn": False}
    entry = ops().update_override("quality", message_id, change)
    auth.audit("ops_quality", target=message_id, details={"rating": rating, "reasons": reasons, "replied_by": replier,
                                                           "subject": _subject(rows)[:80]})
    return jsonify(ok=True, review=entry, rev=overrides_rev(read_overrides()))


@bp.post("/email/quality/appeal")
@auth.login_required
def email_quality_appeal():
    """The person who wrote the rated reply asks for the rating to be looked at again."""
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    text = clean_text(body.get("appeal"), "Appeal", 500, required=True, minlen=3, multiline=True)
    mine = overlay.normalize_email(g.user.get("email"))
    review = ops().overrides("quality").get(message_id)
    if not review or not mine or overlay.normalize_email(review.get("replied_by_email")) != mine:
        # the same answer whether or not somebody else's rating exists, so nothing can be probed
        raise ApiError("There is no rating of your own reply for that e-mail.", 404)
    if review.get("rating_withdrawn"):
        raise ApiError("That rating was already withdrawn.", 409)
    if review.get("appeal_decision"):
        raise ApiError("That appeal was already decided.", 409)

    def change(old):
        return {**old, "appeal": text, "appeal_by": _who(), "appeal_at": _stamp()}
    entry = ops().update_override("quality", message_id, change)
    auth.audit("ops_quality_appeal", target=message_id, details={"subject": str(review.get("subject") or "")[:80]})
    return jsonify(ok=True, review=entry, rev=overrides_rev(read_overrides()))


@bp.post("/email/quality/decision")
@auth.ops_act_required
def email_quality_decision():
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    decision = str(body.get("decision") or "").strip().capitalize()
    if decision not in DECISIONS:
        raise ApiError("Decision must be Accepted or Rejected.")
    review = ops().overrides("quality").get(message_id)
    if not review:
        raise ApiError("There is no rating for that e-mail.", 404)
    if not review.get("appeal"):
        raise ApiError("There is no appeal to decide.", 409)
    team = review.get("team") or next((r.get("team") for r in raw_rows(message_id)), "")
    if not auth.may_act_on_team(team):
        raise ApiError("That e-mail belongs to another team.", 403)

    def change(old):
        return {**old, "appeal_decision": decision, "decided_by": _who(), "decided_at": _stamp(),
                "rating_withdrawn": decision == "Accepted"}
    entry = ops().update_override("quality", message_id, change)
    auth.audit("ops_quality_decision", target=message_id, details={"decision": decision,
                                                                    "subject": str(review.get("subject") or "")[:80]})
    return jsonify(ok=True, review=entry, rev=overrides_rev(read_overrides()))


# ----------------------------------------------------------------------------- assigned mail: open, download, Done
# docs/FEATURES-V2.md B2 / B3. THE rule of this section: a person gets a mail only when the assignment names them (any role),
# or when they are a manager of its team, or an administrator. Everything else - a mail that does not exist, a mail that
# is somebody else's, an "Unclassified" mail of no one - gets the SAME answer, so ids cannot be probed.
def not_available():
    return ApiError("That e-mail is not available.", 404)


def is_assignee(user, entry):
    mine = overlay.normalize_email((user or {}).get("email"))
    return bool(entry) and bool(mine) and overlay.normalize_email(entry.get("assigned_to_email")) == mine


def may_open_mail(user, message_id, entry, rows):
    """The access rule for opening, downloading, marking done and reopening one e-mail (see above)."""
    if is_assignee(user, entry):
        return True
    if (user or {}).get("role") not in auth.OPS_ROLES:
        return False                                            # an employee: only what is assigned to them
    scope = overlay.scope_for_user(user)
    if rows:
        return scope is None or any(overlay.row_in_scope(r, scope) for r in rows)
    if entry:                                                   # the mail left the scan: the assignment remembers its team
        return scope is None or overlay.team_in(scope, entry.get("team"))
    # nothing in the scan and no assignment: only someone who sees every team, and only if a saved copy exists
    return scope is None and mailbox.preview_path(ops().mail_dir, mailbox.mail_key(message_id)).is_file()


def mail_access(message_id, route):
    """(assignment entry or None, overlaid rows) when the signed-in person may handle ``message_id``; otherwise the one
    404 for 'not found' and 'not yours' alike (and an audit line, so probing leaves a trace)."""
    ovr = read_overrides()
    entry = ovr["assignments"].get(message_id)
    rows = mail_rows(message_id, ovr)
    if not may_open_mail(g.user, message_id, entry, rows):
        auth.audit("ops_mail_denied", target=message_id[:200], details={"route": route})
        raise not_available()
    return entry, rows


def read_only_view():
    """True when the page is somebody else's (``?as=<user>``): looking is allowed, acting is not."""
    wanted = norm_uid(str(request.args.get("as") or optional_body().get("as") or ""))
    return bool(wanted) and wanted != g.user["user_id"]


def mail_state(message_id, entry, rows):
    """(key, preview or None, has_msg, state) of the saved copy of one mail."""
    key = mailbox.mail_key(message_id)
    preview, has_msg = mailbox.load_copy(ops().mail_dir, key)
    scope = hub_cfg()["email"]["mail_copies"]["scope"]
    wanted = scope != "off" and (bool(entry) or (scope == "assigned_and_pending" and any(
        r.get("status") == schemas.EMAIL_PENDING for r in rows)))
    if preview and has_msg:
        state = schemas.MAIL_STATE_READY
    elif preview and not int(preview.get("msg_bytes") or 0):
        state = schemas.MAIL_STATE_TOO_LARGE
    else:
        state = schemas.MAIL_STATE_PREPARING if wanted else schemas.MAIL_STATE_UNAVAILABLE
    return key, preview, has_msg, state


def assignment_view(entry, row=None):
    """What a person sees of an assignment (no internal ids)."""
    if not entry:
        return None
    manual = bool(entry.get("done_at"))
    auto = bool(row and row.get("done_auto")) and not manual
    return {"assigned_to_email": overlay.normalize_email(entry.get("assigned_to_email")),
            "assigned_to_name": entry.get("assigned_to_name", ""), "assigned_by": entry.get("marked_by", ""),
            "assigned_at": entry.get("marked_at", ""), "note": entry.get("note", ""),
            "done": manual or auto, "done_auto": auto,
            "done_at": entry.get("done_at", "") if manual else (row.get("done_at", "") if auto else ""),
            "done_by": entry.get("done_by", "") if manual else "", "done_note": entry.get("done_note", "") if manual else ""}


def _mail_block(rows):
    if not rows:
        return None
    row = next((r for r in rows if r.get("status") == schemas.EMAIL_PENDING), rows[0])
    block = {k: row.get(k, "") for k in ("status", "sla_status", "sla_minutes", "team", "received_time",
                                          "current_waiting_time", "first_response_time", "first_reply_by",
                                          "first_reply_time", "detected_systems", "customer_name")}
    block.update(escalated=bool(row.get("escalated")), is_carryover=bool(row.get("is_carryover")))
    return block


@bp.get("/email/message")
@auth.login_required
def email_message():
    message_id = clean_message_id(request.args.get("message_id"))
    entry, rows = mail_access(message_id, "message")
    key, preview, has_msg, state = mail_state(message_id, entry, rows)
    row = rows[0] if rows else {}
    view = assignment_view(entry, next((r for r in rows if r.get("assigned_to_email")), row))
    out = {"message_id": message_id, "state": state, "has_msg": has_msg, "source": "copy" if preview else "snapshot"}
    if preview:
        out.update({k: preview.get(k) for k in ("subject", "from_name", "from_email", "to", "cc", "received", "body_text",
                                                 "truncated", "attachments", "saved_at", "msg_bytes")})
    else:                                  # no saved copy yet: what the scan knows (the first 300 characters of the text)
        out.update(subject=row.get("subject") or (entry or {}).get("subject", ""), from_name=row.get("customer_name", ""),
                   from_email=row.get("customer_email", ""), to=[], cc=[], received=row.get("received_time"),
                   body_text=row.get("body_preview", ""), truncated=bool(row.get("body_preview")), attachments=[],
                   saved_at=None, msg_bytes=0)
    acting = not read_only_view()
    handler = is_assignee(g.user, entry) or g.user["role"] in auth.OPS_ROLES
    out.update(mail=_mail_block(rows), assignment=view, can={
        "download": state == schemas.MAIL_STATE_READY, "prepare": state == schemas.MAIL_STATE_PREPARING,
        "done": bool(entry) and acting and handler and not (view or {}).get("done"),
        "reopen": bool(entry) and acting and handler and bool(entry.get("done_at"))})
    auth.audit("ops_mail_open", target=message_id, details={"state": state, "subject": str(out.get("subject") or "")[:80]})
    return jsonify(out)


@bp.get("/email/message/download")
@auth.login_required
def email_message_download():
    message_id = clean_message_id(request.args.get("message_id"))
    entry, rows = mail_access(message_id, "download")
    key, preview, has_msg, state = mail_state(message_id, entry, rows)
    if state != schemas.MAIL_STATE_READY:
        raise ApiError("The copy of this e-mail is " + ("too large to keep - open it in Outlook."
                       if state == schemas.MAIL_STATE_TOO_LARGE else "not ready yet."), 404)
    name, ascii_name = mailbox.download_name(preview.get("subject"))
    try:
        resp = send_file(mailbox.msg_path(ops().mail_dir, key), mimetype=mailbox.MSG_MIME, as_attachment=True,
                         download_name=ascii_name, conditional=False, etag=False, max_age=0)
    except OSError:                                               # housekeeping removed it a moment ago
        raise ApiError("The copy of this e-mail is not ready yet.", 404)
    resp.headers["Content-Disposition"] = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    auth.audit("ops_mail_download", target=message_id, details={"subject": str(preview.get("subject") or "")[:80],
                                                                  "bytes": preview.get("msg_bytes")})
    return resp


@bp.post("/email/message/prepare")
@auth.login_required
def email_message_prepare():
    """'Prepare now': ask the collector for an e-mail scan so the copy of a mail that has none yet is saved."""
    message_id = clean_message_id(json_body().get("message_id"))
    entry, rows = mail_access(message_id, "prepare")
    key, preview, has_msg, state = mail_state(message_id, entry, rows)
    if state != schemas.MAIL_STATE_PREPARING:
        return jsonify(ok=True, queued=False, state=state)
    if ops().pending_run_requests("email"):
        return jsonify(ok=True, queued=True, already_pending=True, state=state)
    name = ops().add_run_request("email", g.user["user_id"])
    auth.audit("ops_mail_prepare", target=message_id, details={"request": name})
    return jsonify(ok=True, queued=True, already_pending=False, state=state)


def assignment_target(message_id):
    """(entry, rows, via) for Done / Reopen: the person must be the assignee or a manager / administrator of the mail's team.
    Anybody else gets the one 'not available' answer; an in-scope manager on a mail nobody holds gets 409."""
    if read_only_view():
        raise ApiError("This is somebody else's page - it is read-only.", 403)
    entry, rows = mail_access(message_id, "done")
    if not entry:
        raise ApiError("That e-mail is not assigned.", 409)
    return entry, rows, ("assignee" if is_assignee(g.user, entry) else "manager")


@bp.post("/email/done")
@auth.login_required
def email_done():
    """The assignee (or a manager / administrator of the mail's team) says the assigned e-mail is handled."""
    body = json_body()
    message_id = clean_message_id(body.get("message_id"))
    note = clean_text(body.get("note"), "Note", schemas.DONE_NOTE_CHARS, multiline=True)
    entry, rows, via = assignment_target(message_id)
    state = {}

    def change(old):
        if not old or (via == "assignee" and not is_assignee(g.user, old)):
            state["gone"] = True                                  # unassigned / given to somebody else a moment ago
            return old
        if old.get("done_at"):
            state["already"] = True
            return old
        state["changed"] = True
        return {**old, "done_at": _stamp(), "done_by": _who(), "done_by_id": g.user["user_id"], "done_note": note}
    updated = ops().update_override("assignments", message_id, change)
    if state.get("gone"):
        raise not_available()
    if state.get("changed"):
        auth.audit("ops_done", target=message_id, details={"by": via, "note": note[:80],
                                                           "subject": (_subject(rows) or str(updated.get("subject") or ""))[:80]})
        _notify(schemas.NOTIFY_DONE, updated, rows, message_id)
    return jsonify(ok=True, already=bool(state.get("already")), assignment=assignment_view(updated),
                   rev=overrides_rev(read_overrides()))


@bp.delete("/email/done")
@auth.login_required
def email_reopen():
    """Take the Done mark back (the assignee, or a manager / administrator of the mail's team)."""
    body = optional_body()
    message_id = clean_message_id(body.get("message_id") or request.args.get("message_id"))
    entry, rows, via = assignment_target(message_id)
    if not entry.get("done_at"):
        if any(r.get("done_auto") for r in rows):
            raise ApiError("The assignee answered that e-mail in Outlook, so it stays done.", 409)
        return jsonify(ok=True, already=True, assignment=assignment_view(entry), rev=overrides_rev(read_overrides()))
    state = {}

    def change(old):
        if not old or (via == "assignee" and not is_assignee(g.user, old)):
            state["gone"] = True
            return old
        if not old.get("done_at"):
            state["already"] = True
            return old
        state["changed"] = True
        return {k: v for k, v in old.items() if k not in ("done_at", "done_by", "done_by_id", "done_note")}
    updated = ops().update_override("assignments", message_id, change)
    if state.get("gone"):
        raise not_available()
    if state.get("changed"):
        auth.audit("ops_reopen", target=message_id, details={"by": via,
                                                             "subject": (_subject(rows) or str(updated.get("subject") or ""))[:80]})
        _notify(schemas.NOTIFY_REOPENED, updated, rows, message_id)
    return jsonify(ok=True, already=bool(state.get("already")), assignment=assignment_view(updated),
                   rev=overrides_rev(read_overrides()))


# ----------------------------------------------------------------------------- notifications (the bell)
def sweep_replied():
    """Tell the assigner and the team's managers - once - when an assignee answered an assigned e-mail in Outlook.

    The collector only writes snapshots, so the web app notices: when the snapshot or the assignments changed it looks for
    rows that are Replied by their assignee and whose assignment has not been announced yet (``replied_notified``). The
    flag is set under the assignments lock, so two requests at once (or two servers) announce a reply only once."""
    snap, _ = read_snapshot("email")
    if not snap:
        return
    assignments = ops().overrides("assignments")
    memo = current_app.extensions.setdefault("ops_replied_sweep", {})
    if memo.get("stamp") == snap.get("generated_at") and memo.get("assignments") is assignments:
        return
    memo["stamp"], memo["assignments"] = snap.get("generated_at"), assignments
    found = {}
    for row in (snap.get("data") or {}).get("rows") or []:
        entry = assignments.get(row.get("message_id")) if isinstance(row, dict) else None
        if entry and not entry.get("replied_notified") and row.get("status") == schemas.EMAIL_REPLIED \
                and overlay.replied_by_address(row, overlay.normalize_email(entry.get("assigned_to_email"))):
            found.setdefault(row["message_id"], row)
    for message_id, row in found.items():
        flipped = {}

        def flip(old, flipped=flipped):
            if old and not old.get("replied_notified"):
                flipped["entry"] = {**old, "replied_notified": _stamp()}
                return flipped["entry"]
            return old
        ops().update_override("assignments", message_id, flip)
        if flipped:
            entry = flipped["entry"]
            assignee = next((u for u in auth.store().users().values()
                             if overlay.normalize_email(u.get("email")) == overlay.normalize_email(entry.get("assigned_to_email"))
                             and u.get("active", True)), None)
            rows = overlay.apply_overrides([row], read_overrides(), auth.now())
            try:
                notifications.notify(ops(), auth.store().users(), schemas.NOTIFY_REPLIED, None, entry, rows,
                                     row.get("subject", ""), message_id, auth.now(),
                                     actor_name=(assignee or {}).get("name") or entry.get("assigned_to_name", ""))
            except Exception as exc:                          # noqa: BLE001
                current_app.logger.warning("notification replied failed: %s: %s", type(exc).__name__, exc)


@bp.get("/notifications")
@auth.login_required
def notifications_get():
    """The signed-in person's bell: ``{unread, items, ts}`` or, when ``since`` is still current, ``{unchanged, unread}``.
    The person comes from the session only - there is no way to ask for somebody else's."""
    try:
        sweep_replied()
    except Exception as exc:                                  # noqa: BLE001 - the bell must keep answering
        current_app.logger.warning("replied sweep failed: %s: %s", type(exc).__name__, exc)
    return jsonify(notifications.poll(ops(), g.user["user_id"], request.args.get("since")))


@bp.post("/notifications/read")
@auth.login_required
def notifications_read():
    body = json_body()
    ids = body.get("ids")
    if ids != "all":
        if not isinstance(ids, list) or len(ids) > 200 or not all(isinstance(i, str) and 0 < len(i) <= 64 for i in ids):
            raise ApiError('ids must be a list of notification ids or "all".')
    result = notifications.mark_read(ops(), g.user["user_id"], ids, auth.now())
    if result["changed"]:
        auth.audit("ops_notifications_read", details={"count": result["changed"], "all": ids == "all"})
    return jsonify(ok=True, unread=result["unread"], ts=result["ts"])


# ----------------------------------------------------------------------------- classification rules
def _rule_keyword(body):
    keyword = " ".join(str(body.get("keyword") or "").split())
    if not 2 <= len(keyword) <= 80:
        raise ApiError("The keyword must be 2 to 80 characters.")
    return keyword


def _find_rule(rules, keyword):
    return next((k for k in rules if k.casefold() == keyword.casefold()), None)


def _save_rules(rules):
    try:
        saved = hub_settings.patch(ops(), {"email": {"classification": {"system_team_map": rules}}})
    except hub_settings.SettingsError as exc:
        raise ApiError("; ".join(f"{e['path']}: {e['message']}" for e in exc.errors[:3]), 400)
    forget_settings()
    return saved["email"]["classification"]["system_team_map"]


@bp.post("/email/classification/rule")
@auth.ops_act_required
def classification_rule_add():
    body = json_body()
    keyword = _rule_keyword(body)
    team = _clean_team(body.get("team"))
    if team.casefold() == "unclassified":
        raise ApiError("A rule must point to a real team, not to Unclassified.")
    if not auth.may_act_on_team(team):
        raise ApiError("You can only create rules for your own team.", 403)
    rules = dict(hub_cfg()["email"]["classification"]["system_team_map"])
    existing = _find_rule(rules, keyword)
    if existing:
        if not auth.may_act_on_team(rules[existing]):
            raise ApiError("That keyword already belongs to another team's rule.", 403)
        del rules[existing]
    rules[keyword] = team
    saved = _save_rules(rules)
    auth.audit("ops_classification_rule", target=keyword, details={"op": "add", "team": team})
    return jsonify(ok=True, rule={"keyword": keyword, "team": team}, system_team_map=saved)


@bp.delete("/email/classification/rule")
@auth.ops_act_required
def classification_rule_remove():
    body = optional_body()
    keyword = " ".join(str(body.get("keyword") or request.args.get("keyword") or "").split())
    if not keyword:
        raise ApiError("Say which keyword to remove.")
    rules = dict(hub_cfg()["email"]["classification"]["system_team_map"])
    existing = _find_rule(rules, keyword)
    if not existing:
        raise ApiError("There is no such rule.", 404)
    if not auth.may_act_on_team(rules[existing]):
        raise ApiError("You can only remove rules of your own team.", 403)
    team = rules.pop(existing)
    saved = _save_rules(rules)
    auth.audit("ops_classification_rule", target=existing, details={"op": "remove", "team": team})
    return jsonify(ok=True, system_team_map=saved)


# ----------------------------------------------------------------------------- jobs
def _sanitize_info(value):
    """Defence in depth: whatever the collector wrote, no secret leaves the server."""
    if isinstance(value, dict):
        return {k: _sanitize_info(v) for k, v in value.items() if str(k).lower() not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_sanitize_info(v) for v in value]
    return value


def collector_info():
    try:
        info = ops().read_state("collector_info", {})
    except (StorageError, ValueError):
        info = {}
    return _sanitize_info(info) if isinstance(info, dict) else {}


@bp.get("/jobs")
@auth.admin_required
def jobs():
    pending = ops().pending_run_requests()
    return jsonify(jobs=job_states(), pending=pending, collector_info=collector_info(),
                   server_time=auth.now().isoformat(timespec="seconds"))


def _known_job(job):
    if not schemas.valid_job(job):
        raise ApiError("Unknown job.", 404)
    return job


@bp.post("/jobs/<job>/run")
@auth.ops_act_required
def job_run(job):
    _known_job(job)
    if g.user["role"] != "admin" and job not in MANAGER_JOBS:
        raise ApiError("Only an administrator can start this job.", 403)
    if ops().pending_run_requests(job):
        return jsonify(queued=True, already_pending=True)
    name = ops().add_run_request(job, g.user["user_id"])
    auth.audit("ops_run_job", target=job, details={"request": name})
    return jsonify(queued=True, already_pending=False, request=name)


@bp.get("/jobs/<job>/log")
@auth.admin_required
def job_log(job):
    _known_job(job)
    try:
        lines = int(request.args.get("lines", 200))
    except ValueError:
        raise ApiError("lines must be a number.")
    return jsonify(job=job, lines=ops().tail_log(job, lines))


# ----------------------------------------------------------------------------- settings
def _secret_paths(value, path=""):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            sub = f"{path}.{key}" if path else str(key)
            # sla.selectors.password is the CSS selector of the password box, not a password
            if str(key).lower() in _SECRET_KEYS and not path.endswith("selectors"):
                found.append(sub)
            found += _secret_paths(item, sub)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found += _secret_paths(item, f"{path}[{i}]")
    return found


def _changed_paths(old, new, prefix=""):
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in sorted(set(old) | set(new)):
            out += _changed_paths(old.get(key), new.get(key), f"{prefix}.{key}" if prefix else str(key))
        return out
    return [] if old == new else [prefix]


@bp.get("/settings")
@auth.admin_required
def settings_get():
    info = collector_info()
    return jsonify(settings=hub_cfg(), pinned_keys=list(info.get("pinned_keys") or []), collector_info=info,
                   weekdays=hub_settings.WEEKDAYS)


@bp.put("/settings")
@auth.admin_required
def settings_put():
    body = json_body()
    doc = body["settings"] if isinstance(body.get("settings"), dict) else body
    secrets_found = _secret_paths(doc)
    if secrets_found:
        message = "Passwords are never stored in the settings - put them in collector_config.json on the collector PC."
        return jsonify(error=message, errors=[{"path": p, "message": message} for p in secrets_found]), 400
    before = hub_cfg()
    try:
        saved = hub_settings.save(ops(), doc)
    except hub_settings.SettingsError as exc:
        return jsonify(error="Some settings are not valid: " + "; ".join(
            f"{e['path']}: {e['message']}" for e in exc.errors[:3]), errors=exc.errors), 400
    forget_settings()
    changed = _changed_paths(before, saved)
    auth.audit("ops_settings_saved", details={"changed": changed[:40], "count": len(changed)})
    return jsonify(ok=True, settings=saved, changed=changed)


# ----------------------------------------------------------------------------- reports, health
@bp.get("/reports")
@auth.ops_view_required
def reports():
    module = request.args.get("module") or None
    if module and not schemas.valid_module(module):
        raise ApiError("Unknown module.", 404)
    items = ops().list_reports(module)
    return jsonify(reports=[{**r, "url": f"/export/ops/report/{r['module']}/{r['name']}"} for r in items])


@bp.get("/health")
@auth.admin_required
def health_report():
    return jsonify(health.collect(current_app.config["SETTINGS"]["data_dir"], ops(), auth.store(), hub_cfg(), auth.now()))
