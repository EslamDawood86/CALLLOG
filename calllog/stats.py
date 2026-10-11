"""Numbers shown on the dashboards: totals, status split, calls per hour/day."""
from collections import defaultdict

from .storage import STATUSES, date_range


def status_of(reason_key, reasons_by_key):
    reason = reasons_by_key.get(reason_key)
    return reason["status"] if reason and reason.get("status") in STATUSES else "other"


def summarize(records, reasons_by_key):
    """Totals, per-status counts, per-reason counts and first/last call of a list of records."""
    out = {"total": 0, **{s: 0 for s in STATUSES}, "other": 0,
           "by_reason": defaultdict(int), "first_call": None, "last_call": None}
    for rec in records:
        out["total"] += 1
        out[status_of(rec["reason"], reasons_by_key)] += 1
        out["by_reason"][rec["reason"]] += 1
        stamp = (rec["date"], rec["time"])
        if out["first_call"] is None or stamp < out["first_call"]:
            out["first_call"] = stamp
        if out["last_call"] is None or stamp > out["last_call"]:
            out["last_call"] = stamp
    out["by_reason"] = dict(out["by_reason"])
    out["first_call"] = " ".join(out["first_call"]) if out["first_call"] else None
    out["last_call"] = " ".join(out["last_call"]) if out["last_call"] else None
    return out


def buckets(records, reasons_by_key, kind, d_from=None, d_to=None):
    """Stacked-column data. kind='hour' -> 24 buckets; kind='day' -> one per date in the range."""
    table = defaultdict(lambda: {s: 0 for s in STATUSES})
    if kind == "hour":
        for h in range(24):
            table[h]
    elif d_from and d_to:
        for day in date_range(d_from, d_to):
            table[day]
    for rec in records:
        status = status_of(rec["reason"], reasons_by_key)
        if status not in STATUSES:
            continue
        key = int(rec["time"][:2]) if kind == "hour" else rec["date"]
        table[key][status] += 1
    out = []
    for key in sorted(table):
        row = table[key]
        out.append({"key": key, **row, "total": sum(row.values())})
    return out


def per_employee(users, records, reasons_by_key):
    """One row per active employee account (idle people show zeros), busiest first."""
    by_user = defaultdict(list)
    for rec in records:
        by_user[rec["user_id"]].append(rec)
    rows = []
    for uid, user in users.items():
        if user.get("role") != "user":
            continue
        mine = by_user.pop(uid, [])
        if not user.get("active", True) and not mine:
            continue
        s = summarize(mine, reasons_by_key)
        rows.append({"user_id": uid, "name": user["name"], "department": user.get("department", ""),
                     "active": user.get("active", True), **{k: s[k] for k in
                     ("total", "first_call", "last_call", *STATUSES)}})
    for uid, mine in by_user.items():  # records whose account was removed from users.json
        s = summarize(mine, reasons_by_key)
        rows.append({"user_id": uid, "name": uid, "department": "", "active": False,
                     **{k: s[k] for k in ("total", "first_call", "last_call", *STATUSES)}})
    rows.sort(key=lambda r: (-r["total"], r["name"]))
    return rows

