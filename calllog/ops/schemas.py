"""Names and light validators shared by the collectors and the web app (see docs/HUB-DESIGN.md, section 5)."""
import re

SCHEMA_VERSION = 1

MODULES = ("email", "sla", "itsm")                  # things that have a snapshot
JOBS = ("email", "email_report", "sla", "itsm")     # things the collector runs
JOB_MODULE = {"email": "email", "email_report": "email", "sla": "sla", "itsm": "itsm"}
OVERRIDES = ("no_action", "team", "assignments", "quality")

# SLA request status codes (the portal script used English labels; the codes are stable, labels are for people)
SLA_WITHIN, SLA_REMINDER, SLA_BREACHED, SLA_NO_DATE = "within", "reminder", "breached", "no_date"
SLA_STATUSES = (SLA_WITHIN, SLA_REMINDER, SLA_BREACHED, SLA_NO_DATE)
SLA_LABELS = {SLA_WITHIN: "Within SLA", SLA_REMINDER: "Reminder", SLA_BREACHED: "SLA breached", SLA_NO_DATE: "No sent date"}

# E-mail row statuses (kept exactly as the old script wrote them)
EMAIL_REPLIED, EMAIL_PENDING, EMAIL_NO_ACTION = "Replied", "Pending Reply", "No Action Needed"
EMAIL_STATUSES = (EMAIL_REPLIED, EMAIL_PENDING, EMAIL_NO_ACTION)

ALL_TEAMS_MARK = "__all__"      # user.team value meaning "every team"

_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_module(name):
    return name in MODULES


def valid_job(name):
    return name in JOBS


def is_iso_datetime(value):
    return isinstance(value, str) and bool(_ISO_DT.match(value))


def is_iso_day(value):
    return isinstance(value, str) and bool(_ISO_DAY.match(value))


def make_envelope(module, data, generated_at, duration_seconds=0.0, host="", date=None):
    """Wrap module data in the snapshot envelope."""
    return {
        "schema": SCHEMA_VERSION, "module": module, "generated_at": generated_at,
        "duration_seconds": round(float(duration_seconds or 0), 2), "host": host or "",
        "date": date or generated_at[:10], "data": data,
    }


def validate_snapshot(snap):
    """Return a list of problems (empty = fine). Deliberately light: the UI must survive extra keys."""
    problems = []
    if not isinstance(snap, dict):
        return ["snapshot is not an object"]
    if snap.get("schema") != SCHEMA_VERSION:
        problems.append(f"schema must be {SCHEMA_VERSION}")
    module = snap.get("module")
    if module not in MODULES:
        problems.append(f"module must be one of {MODULES}")
    if not is_iso_datetime(snap.get("generated_at")):
        problems.append("generated_at must be an ISO datetime")
    data = snap.get("data")
    if not isinstance(data, dict):
        return problems + ["data must be an object"]
    if module == "sla":
        for key, kind in (("thresholds", dict), ("totals", dict), ("by_user", list), ("requests", list)):
            if not isinstance(data.get(key), kind):
                problems.append(f"sla.data.{key} must be a {kind.__name__}")
        for i, r in enumerate(data.get("requests") or []):
            if not isinstance(r, dict) or r.get("status") not in SLA_STATUSES:
                problems.append(f"sla.data.requests[{i}].status invalid")
                break
    elif module == "itsm":
        for key, kind in (("categories", list), ("roles", list), ("totals", dict), ("changes", dict)):
            if not isinstance(data.get(key), kind):
                problems.append(f"itsm.data.{key} must be a {kind.__name__}")
    elif module == "email":
        for key, kind in (("stats", dict), ("rows", list), ("teams", list)):
            if not isinstance(data.get(key), kind):
                problems.append(f"email.data.{key} must be a {kind.__name__}")
        for i, r in enumerate(data.get("rows") or []):
            if not isinstance(r, dict) or not r.get("message_id") and not r.get("entry_id"):
                problems.append(f"email.data.rows[{i}] needs message_id or entry_id")
                break
            if r.get("status") not in EMAIL_STATUSES:
                problems.append(f"email.data.rows[{i}].status invalid")
                break
    return problems
