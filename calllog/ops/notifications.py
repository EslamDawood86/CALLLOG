"""In-app notifications (docs/FEATURES-V2.md, B4): the bell on every page.

One small file per person, ``<data_dir>/ops/notifications/<user id>.json``::

    {"items": [{"id", "ts", "type", "title", "text", "link", "actor", "ref": {"message_id"}, "read"}], "ts": "<token>", "seen_ts": ""}

Newest first, at most 100 items. ``ts`` changes on every write (a new item or a read mark) and is what a page sends back as
``?since=`` - an unchanged file is answered with a few bytes. Items never hold a mail body: title and text are cut to 200
characters (the subject, who, what).

Writers: the web app (on assign / unassign / done / reopen) and the web app again when it notices that an assignee answered in
Outlook. Several people can act at the same moment, so every write is a read-modify-write under a per-person lock
(``OpsStore._update_json``: a thread lock plus an OS file lock, so a second process on the shared folder is safe too).
Readers use the store's cached read; a person's file is NOT watched by the background refresher (60 files would mean 60 share
round trips a second) - a poll re-checks its own file once the 1 s cache time has passed.

No Flask here: the functions take plain dicts (``users`` = the accounts file, ``actor`` = the signed-in account).
Python 3.9 compatible.
"""
import hashlib
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote

from . import overlay, schemas
from .store import safe_file_name

TITLE_CHARS = 200
TEXT_CHARS = 200
EMPTY_TS = "0"


# ------------------------------------------------------------------------------------------------------------ files
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def user_stem(user_id):
    """File name (without .json) of a person's notifications. Account ids are already file-safe; anything else (never
    expected: a Windows device name such as ``con``, a trailing dot, odd characters) is replaced by a hash so no id can
    reach another folder or another file."""
    uid = str(user_id or "").strip().lower()
    if safe_file_name(uid) and not uid.endswith(".") and uid.split(".")[0] not in _WINDOWS_DEVICES:
        return uid
    return "u-" + hashlib.sha1(uid.encode("utf-8", "replace")).hexdigest()[:24]


def path_for(store, user_id):
    return store.notifications_dir / f"{user_stem(user_id)}.json"


def _lock_name(user_id):
    return "notif-" + user_stem(user_id)


def _empty():
    return {"items": [], "ts": EMPTY_TS, "seen_ts": ""}


def _next_ts(previous, now):
    """A change token that is different from ``previous`` even when the clock has not moved (tests, a clock set back)."""
    ts = now.isoformat(timespec="microseconds")
    if previous and previous != EMPTY_TS and ts <= previous:
        try:
            ts = (datetime.fromisoformat(previous) + timedelta(microseconds=1)).isoformat(timespec="microseconds")
        except ValueError:
            ts = ts + "1"
    return ts


def _clip(value, limit):
    return " ".join(str(value or "").split())[:limit]


# ----------------------------------------------------------------------------------------------------------- reading
def read(store, user_id):
    """The person's document (cached; treat as read-only). A missing or damaged file reads as 'no notifications'."""
    try:
        doc = store.read_json(path_for(store, user_id), None, watch=False)
    except Exception:                                # a damaged file must not break every page
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        return _empty()
    return doc


def unread_count(doc):
    return sum(1 for item in doc.get("items", ()) if isinstance(item, dict) and not item.get("read"))


def poll(store, user_id, since=None):
    """The answer of ``GET /api/ops/notifications``: everything new, or just ``unchanged`` when the file did not change since
    the ``ts`` the page already has."""
    doc = read(store, user_id)
    ts = str(doc.get("ts") or EMPTY_TS)
    unread = unread_count(doc)
    if since and str(since) == ts:
        return {"unchanged": True, "unread": unread, "ts": ts}
    return {"unread": unread, "items": [i for i in doc["items"][:schemas.NOTIFICATION_PAGE] if isinstance(i, dict)],
            "ts": ts, "seen_ts": str(doc.get("seen_ts") or "")}


# ----------------------------------------------------------------------------------------------------------- writing
def make_item(kind, title, text, link, actor="", message_id="", now=None):
    now = now or datetime.now()
    return {"id": secrets.token_hex(6), "ts": now.isoformat(timespec="seconds"), "type": kind,
            "title": _clip(title, TITLE_CHARS), "text": _clip(text, TEXT_CHARS), "link": str(link or "")[:600],
            "actor": _clip(actor, 80), "ref": {"message_id": str(message_id or "")[:500]}, "read": False}


def add(store, user_id, items, now=None):
    """Put ``items`` (oldest first or newest first - they are kept in the order given, ahead of the older ones) at the top of
    the person's list. Returns the new document."""
    now = now or datetime.now()
    items = [i for i in (items or []) if isinstance(i, dict)]
    if not items:
        return read(store, user_id)

    def change(doc):
        current = doc.get("items") if isinstance(doc.get("items"), list) else []
        doc["items"] = (items + current)[:schemas.NOTIFICATION_KEEP]
        doc["ts"] = _next_ts(doc.get("ts"), now)
        doc.setdefault("seen_ts", "")
    return store._update_json(path_for(store, user_id), _lock_name(user_id), _empty, change)


def mark_read(store, user_id, ids, now=None):
    """``ids`` = a list of item ids or ``"all"``. Returns ``{"unread", "ts", "changed"}``. Nothing is written when there is
    nothing to mark (no file is created for a person who never had a notification)."""
    now = now or datetime.now()
    doc = read(store, user_id)
    wanted = None if ids == "all" else {str(i) for i in (ids or [])}
    if not any(isinstance(i, dict) and not i.get("read") and (wanted is None or i.get("id") in wanted)
               for i in doc["items"]):
        return {"unread": unread_count(doc), "ts": str(doc.get("ts") or EMPTY_TS), "changed": 0}
    changed = []

    def change(current):
        n = 0
        for item in current.get("items", []):
            if isinstance(item, dict) and not item.get("read") and (wanted is None or item.get("id") in wanted):
                item["read"] = True
                n += 1
        if n:
            current["ts"] = _next_ts(current.get("ts"), now)
            if wanted is None:
                current["seen_ts"] = now.isoformat(timespec="seconds")
        changed.append(n)
    out = store._update_json(path_for(store, user_id), _lock_name(user_id), _empty, change)
    return {"unread": unread_count(out), "ts": str(out.get("ts") or EMPTY_TS), "changed": changed[0] if changed else 0}


def prune(store, keep_days=30, now=None):
    """Housekeeping: drop items older than ``keep_days`` from every person's file. Returns the number of items removed."""
    import os
    now = now or datetime.now()
    limit = (now - timedelta(days=keep_days)).isoformat(timespec="seconds")
    removed = 0
    try:
        names = [e.name for e in os.scandir(store.notifications_dir) if e.is_file() and e.name.endswith(".json")]
    except OSError:
        return 0
    for name in names:
        stem = name[:-5]
        path = store.notifications_dir / name
        doc = store.read_json(path, None, watch=False)
        if not isinstance(doc, dict) or not any(isinstance(i, dict) and str(i.get("ts") or "") < limit
                                                for i in doc.get("items", [])):
            continue
        count = []

        def change(current, count=count):
            items = [i for i in current.get("items", []) if isinstance(i, dict) and str(i.get("ts") or "") >= limit]
            count.append(len(current.get("items", [])) - len(items))
            current["items"] = items
            current["ts"] = _next_ts(current.get("ts"), now)
        store._update_json(path, "notif-" + stem, _empty, change)
        removed += count[0] if count else 0
    return removed


# ------------------------------------------------------------------------------------------------------ recipients
def accounts_by_email(users):
    """{mailbox: account} of the active accounts that have a mailbox (the link between an assignment and a person)."""
    out = {}
    for user in (users or {}).values():
        email = overlay.normalize_email(user.get("email"))
        if email and user.get("active", True) and email not in out:
            out[email] = user
    return out


def team_managers(users, rows, team=""):
    """Active managers whose teams include the team of any of ``rows`` (or ``team`` when there are no rows).
    A manager with no team, or the all-teams marker, covers every team (the same scope rule as the E-mail page)."""
    out = []
    for user in (users or {}).values():
        if user.get("role") != "manager" or not user.get("active", True):
            continue
        scope = overlay.scope_for_user(user)
        if scope is None:
            out.append(user)
        elif rows:
            if any(overlay.row_in_scope(row, scope) for row in rows):
                out.append(user)
        elif team and overlay.team_in(scope, team):
            out.append(user)
    return out


def link_for(user, message_id):
    """Where a click leads: the employee's own page, or the E-mail page for managers and administrators."""
    mid = quote(str(message_id or ""), safe="")
    if (user or {}).get("role") == "user":
        return f"/my?mail={mid}"
    return f"/email?tab=pending&open={mid}"


def _uid(user):
    return str((user or {}).get("user_id") or "")


def audience(kind, users, actor, entry, rows, old_entry=None):
    """Who is told about one event, as ``[account, ...]`` in a stable order, without duplicates and never the actor.

    * assigned: the new assignee, if an account with that mailbox exists;
    * unassigned: the previous assignee (``old_entry``);
    * done / reopened by the assignee: the person who assigned it and every manager of the mail's team (an administrator only
      when they were the assigner); by somebody else (a manager acting for the assignee): the assignee;
    * replied (the assignee answered in Outlook): the assigner and the managers of the team."""
    by_email = accounts_by_email(users)
    me = _uid(actor)
    entry = entry or {}
    assignee_email = overlay.normalize_email(entry.get("assigned_to_email"))
    assignee = by_email.get(assignee_email)
    chosen = []

    def add_user(user):
        if user and _uid(user) and _uid(user) != me and all(_uid(user) != _uid(c) for c in chosen):
            chosen.append(user)

    if kind == schemas.NOTIFY_ASSIGNED:
        add_user(assignee)
    elif kind == schemas.NOTIFY_UNASSIGNED:
        add_user(by_email.get(overlay.normalize_email((old_entry or entry).get("assigned_to_email"))))
    else:
        by_assignee = bool(me and assignee_email and overlay.normalize_email((actor or {}).get("email")) == assignee_email)
        if kind in (schemas.NOTIFY_DONE, schemas.NOTIFY_REOPENED) and not by_assignee:
            add_user(assignee)                          # a manager acted for the assignee: the assignee is told
        else:                                           # the assignee acted (or answered in Outlook): the other side is told
            assigner = (users or {}).get(str(entry.get("marked_by_id") or ""))
            if assigner and assigner.get("active", True):
                add_user(assigner)
            for manager in team_managers(users, rows, entry.get("team", "")):
                add_user(manager)
    return chosen


_TITLES = {
    schemas.NOTIFY_ASSIGNED: "New e-mail assigned to you",
    schemas.NOTIFY_UNASSIGNED: "Assignment removed",
    schemas.NOTIFY_DONE: "{actor} marked an e-mail as done",
    schemas.NOTIFY_REOPENED: "E-mail reopened",
    schemas.NOTIFY_REPLIED: "{actor} replied to an assigned e-mail",
}


def notify(store, users, kind, actor, entry, rows, subject, message_id, now=None, old_entry=None, actor_name=""):
    """Create the notifications of one event (see ``audience``). Never raises into the caller's action; returns the ids of
    the people told. ``actor`` may be None for an event nobody triggered (a reply seen by the scan)."""
    if kind not in schemas.NOTIFICATION_TYPES:
        raise ValueError(f"unknown notification type {kind!r}")
    now = now or datetime.now()
    who = actor_name or (actor or {}).get("name") or _uid(actor) or "Someone"
    told = []
    note = " ".join(str((entry or {}).get("done_note") or "").split())
    for user in audience(kind, users, actor, entry, rows, old_entry):
        title = _TITLES[kind].format(actor=_clip(who, 60))
        text = _clip(subject, 150) or "(no subject)"
        if kind == schemas.NOTIFY_DONE and note:
            text = _clip(f"{text} - {note}", TEXT_CHARS)
        elif kind in (schemas.NOTIFY_ASSIGNED, schemas.NOTIFY_UNASSIGNED, schemas.NOTIFY_REOPENED) and who:
            text = _clip(f"{text} - by {who}", TEXT_CHARS)
        add(store, _uid(user), [make_item(kind, title, text, link_for(user, message_id), who, message_id, now)], now)
        told.append(_uid(user))
    return told
