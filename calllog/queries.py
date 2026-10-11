"""Validation and record-listing helpers shared by the JSON API and the exports."""
import re
from datetime import date

from flask import current_app, jsonify, request

from . import auth
from .storage import STATUS_LABELS, norm_uid, valid_date

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
MAX_RANGE_DAYS = 366
MAX_NOTE = 300


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def api_error_response(exc):
    return jsonify(error=exc.message), exc.status


def json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError("Send a JSON object.")
    return body


# ------------------------------------------------------------------ reasons
def reasons_by_key():
    return {r["key"]: r for r in auth.store().reasons()}


def status_label(status):
    return STATUS_LABELS.get(status, "Other")


# ------------------------------------------------------- record field checks
def clean_ip_phone(value):
    text = re.sub(r"\s+", "", str(value or "")).translate(ARABIC_DIGITS)
    if not re.match(current_app.config["IP_PHONE_REGEX"], text):
        raise ApiError("IP phone must be digits only, for example 75942.")
    return text


def clean_reason(value):
    if value not in reasons_by_key():
        raise ApiError("Choose a reason for the call.")
    return value


def clean_note(value):
    text = str(value or "").strip()
    if len(text) > MAX_NOTE:
        raise ApiError(f"Note is too long (max {MAX_NOTE} characters).")
    return text


# ------------------------------------------------------------- date handling
def parse_day(value, default=None):
    value = value or default
    if not valid_date(value):
        raise ApiError("Dates look like 2026-10-11.")
    return value


def parse_range():
    d_from = parse_day(request.args.get("from"), auth.today())
    d_to = parse_day(request.args.get("to"), d_from)
    if d_from > d_to:
        raise ApiError("'From' date is after 'To' date.")
    if (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days >= MAX_RANGE_DAYS:
        raise ApiError("Pick a range of at most one year.")
    return d_from, d_to


# ----------------------------------------------------------------- listings
def decorate(rec, users, reasons):
    """Record + display fields (employee name, reason labels, status)."""
    user = users.get(rec["user_id"], {})
    reason = reasons.get(rec["reason"], {})
    status = reason.get("status", "other")
    return {
        "branch": "", "branch_name": "", "region": "",   # calls from a sector / other caller have no branch
        **rec,
        "name": user.get("name", rec["user_id"]),
        "department": user.get("department", ""),
        "status": status,
        "status_label": status_label(status),
        "reason_ar": reason.get("label_ar", rec["reason"]),
        "reason_en": reason.get("label_en", rec["reason"]),
        "edited": bool(rec.get("updated_at")),
    }


def filtered_raw(d_from, d_to, args):
    """Raw records in range, newest first, narrowed by user / department / reason / region / q.

    Shared for ~2 s between admins asking the same thing - never modify the list."""
    uid = norm_uid(args.get("user_id")) or None
    department = (args.get("department") or "").strip()
    reason = args.get("reason") or ""
    region = (args.get("region") or "").strip()
    q = (args.get("q") or "").strip().lower()

    def compute():
        users = auth.store().users()
        out = []
        for rec in auth.store().records_between(d_from, d_to, uid):
            if reason and rec["reason"] != reason:
                continue
            if region and rec.get("region") != region:
                continue
            user = users.get(rec["user_id"], {})
            if department and user.get("department") != department:
                continue
            if q and not (q in rec["ip_phone"] or q in (rec.get("note") or "").lower()
                          or q in user.get("name", "").lower() or q in rec["user_id"]
                          or q in rec.get("branch", "") or q in rec.get("branch_name", "")):
                continue
            out.append(rec)
        out.reverse()                     # equal seconds: later-saved call first (the sort below is stable)
        out.sort(key=lambda r: (r["date"], r["time"]), reverse=True)
        return out

    return auth.store().memo(("records", d_from, d_to, uid, department, reason, region, q), compute)


def filtered_records(d_from, d_to, args):
    """Like filtered_raw, with display fields added (used by the exports)."""
    users, reasons = auth.store().users(), reasons_by_key()
    return [decorate(r, users, reasons) for r in filtered_raw(d_from, d_to, args)]


def paginate(items, default_size=50):
    try:
        page = max(1, int(request.args.get("page", 1)))
        size = min(200, max(1, int(request.args.get("page_size", default_size))))
    except ValueError:
        raise ApiError("Page numbers must be integers.")
    pages = max(1, -(-len(items) // size))
    page = min(page, pages)
    return {"total": len(items), "page": page, "pages": pages, "page_size": size,
            "items": items[(page - 1) * size: page * size]}
