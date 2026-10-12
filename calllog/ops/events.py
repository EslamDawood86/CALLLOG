"""Timeline events: what changed between two scans (shown on Home as "Today", never e-mailed).

``diff_events(module, old, new)`` compares the previous and the new snapshot of a module and returns a few
aggregated events (one per kind per scan, never one per request, so a busy scan cannot flood the feed).
``job_event`` is for scan failures / recoveries. Events carry no passwords; subjects and names are included
only for the first few items and the web app shows the feed to admin/manager only.
"""
from . import schemas

SAMPLE = 3          # how many example items an event lists


def _event(ts, module, level, kind, text, items=(), count=None):
    ev = {"ts": ts, "module": module, "level": level, "kind": kind, "text": text}
    if items:
        ev["items"] = list(items)[:SAMPLE]
    if count is not None:
        ev["count"] = count
    return ev


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _sla_events(old, new, ts):
    reqs_new = {r["key"]: r for r in new["data"].get("requests", []) if r.get("key")}
    reqs_old = {r["key"]: r for r in (old or {}).get("data", {}).get("requests", []) if r.get("key")}
    if old is None:
        return []
    out = []
    newly_breached = [r for k, r in reqs_new.items()
                      if r["status"] == schemas.SLA_BREACHED and reqs_old.get(k, {}).get("status") != schemas.SLA_BREACHED]
    if newly_breached:
        limit = new["data"].get("thresholds", {}).get("breach_minutes", 120)
        out.append(_event(ts, "sla", "danger", "sla_breached",
                          f"{_plural(len(newly_breached), 'security request')} passed the {limit // 60:g} h SLA",
                          [{"user": r.get("security_user", ""), "key": r["key"]} for r in newly_breached],
                          len(newly_breached)))
    newly_warn = [r for k, r in reqs_new.items()
                  if r["status"] == schemas.SLA_REMINDER and reqs_old.get(k, {}).get("status") not in
                  (schemas.SLA_REMINDER, schemas.SLA_BREACHED)]
    if newly_warn:
        out.append(_event(ts, "sla", "warn", "sla_reminder",
                          f"{_plural(len(newly_warn), 'security request')} reached the reminder time",
                          [{"user": r.get("security_user", ""), "key": r["key"]} for r in newly_warn], len(newly_warn)))
    closed = [k for k in reqs_old if k not in reqs_new]
    if closed:
        out.append(_event(ts, "sla", "ok", "sla_closed", f"{_plural(len(closed), 'security request')} completed",
                          count=len(closed)))
    return out


def _itsm_events(new, ts):
    changes = new["data"].get("changes") or {}
    out = []
    for kind, label, ok_level in (("new", "new", None), ("closed", "closed", "ok")):
        by_cat = {}
        for ch in changes.get(kind, []):
            by_cat.setdefault(ch.get("category", ""), []).append(ch)
        for category, items in by_cat.items():
            rollback = "rollback" in category.lower()
            level = ok_level or ("warn" if rollback else "info")
            text = f"{len(items)} new {category}" if kind == "new" else f"{len(items)} {category} closed"
            out.append(_event(ts, "itsm", level, f"itsm_{label}", text,
                              [{"number": c.get("number", ""), "summary": c.get("summary", "")[:80],
                                "role": c.get("role", "")} for c in items], len(items)))
    return out


def _row_key(row):
    """One row per (mail, team): a mail sent to two teams has two rows with the same message_id."""
    return row.get("row_id") or row.get("message_id") or row.get("entry_id")


def _email_events(old, new, ts):
    if old is None:
        return []
    rows_new = new["data"].get("rows", [])
    rows_old = {_row_key(r): r for r in old.get("data", {}).get("rows", [])}
    out = []
    newly_escalated = [r for r in rows_new if r.get("escalated") and r.get("status") == schemas.EMAIL_PENDING
                       and not rows_old.get(_row_key(r), {}).get("escalated")]
    if newly_escalated:
        out.append(_event(ts, "email", "danger", "email_escalated",
                          f"{_plural(len(newly_escalated), 'e-mail')} waiting past the SLA with no team reply",
                          [{"subject": r.get("subject", "")[:80], "team": r.get("team", "")} for r in newly_escalated],
                          len(newly_escalated)))
    return out


def diff_events(module, old, new):
    """Events for the change from snapshot ``old`` (or None) to ``new``."""
    if not new:
        return []
    ts = new.get("generated_at") or ""
    if module == "sla":
        return _sla_events(old, new, ts)
    if module == "itsm":
        return _itsm_events(new, ts)
    if module == "email":
        return _email_events(old, new, ts)
    return []


def job_event(job, ts, ok, previous_ok, error=None):
    """A scan that starts failing, or recovers, is worth a line on the timeline; repeats are not."""
    if ok and previous_ok is False:
        return _event(ts, schemas.JOB_MODULE[job], "ok", "scan_recovered", f"{job} scan works again")
    if not ok and previous_ok is not False:
        return _event(ts, schemas.JOB_MODULE[job], "danger", "scan_failed",
                      f"{job} scan failed: {(error or 'unknown error')[:160]}")
    return None
