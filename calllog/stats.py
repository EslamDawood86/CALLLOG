"""Numbers shown on the dashboards: totals, status split, calls per hour/day, per employee, per branch."""
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


# ------------------------------------------------------------------------------------------------
# Admin views work from one small summary ("rollup") per day instead of re-reading every call:
# rollup_day() summarises a day once, combine() merges any number of days.
# ------------------------------------------------------------------------------------------------
def _counts():
    return {"total": 0, **{s: 0 for s in STATUSES}}


def rollup_day(records, reasons_by_key):
    """Everything the admin overview needs to know about one day's calls."""
    totals = {**_counts(), "other": 0}
    by_reason = defaultdict(int)
    hours = [{s: 0 for s in STATUSES} for _ in range(24)]
    users, branches = {}, {}
    for rec in records:
        status = status_of(rec["reason"], reasons_by_key)
        stamp = rec["time"]
        totals["total"] += 1
        totals[status] += 1
        by_reason[rec["reason"]] += 1
        user = users.get(rec["user_id"])
        if user is None:
            user = users[rec["user_id"]] = {**_counts(), "first": stamp, "last": stamp}
        user["total"] += 1
        if stamp < user["first"]:
            user["first"] = stamp
        if stamp > user["last"]:
            user["last"] = stamp
        if status in STATUSES:
            user[status] += 1
            hours[int(stamp[:2])][status] += 1
        code = rec.get("branch")
        if code:
            key = (code, rec.get("branch_name", ""), rec.get("region", ""))
            branch = branches.get(key)
            if branch is None:
                branch = branches[key] = _counts()
            branch["total"] += 1
            if status in STATUSES:
                branch[status] += 1
    return {"totals": totals, "by_reason": dict(by_reason), "hours": hours, "users": users, "branches": branches}


def combine(rollups):
    """Merge [(day, rollup), ...] (oldest first) into totals / per-employee / per-branch figures."""
    totals = {**_counts(), "other": 0}
    by_reason = defaultdict(int)
    users, branches = {}, {}
    for day, roll in rollups:
        for key, value in roll["totals"].items():
            totals[key] += value
        for key, value in roll["by_reason"].items():
            by_reason[key] += value
        for uid, u in roll["users"].items():
            first, last = f"{day} {u['first']}", f"{day} {u['last']}"
            merged = users.get(uid)
            if merged is None:
                users[uid] = {**{k: u[k] for k in ("total", *STATUSES)}, "first_call": first, "last_call": last}
                continue
            for key in ("total", *STATUSES):
                merged[key] += u[key]
            merged["first_call"] = min(merged["first_call"], first)
            merged["last_call"] = max(merged["last_call"], last)
        for key, b in roll["branches"].items():
            merged = branches.get(key)
            if merged is None:
                branches[key] = dict(b)
            else:
                for field, value in b.items():
                    merged[field] += value
    return {"totals": totals, "by_reason": dict(by_reason), "users": users, "branches": branches}


def chart_buckets(rollups, kind, d_from, d_to):
    """Stacked-column data: 24 hours of one day, or one bar per date of a longer range."""
    if kind == "hour":
        hours = rollups[0][1]["hours"] if rollups else [{s: 0 for s in STATUSES} for _ in range(24)]
        return [{"key": h, **hours[h], "total": sum(hours[h].values())} for h in range(24)]
    per_day = {day: roll["totals"] for day, roll in rollups}
    out = []
    for day in date_range(d_from, d_to):
        counts = {s: per_day.get(day, {}).get(s, 0) for s in STATUSES}
        out.append({"key": day, **counts, "total": sum(counts.values())})
    return out


def employee_rows(users, merged_users):
    """One row per active employee account (idle people show zeros), busiest first."""
    def row(uid, name, department, active, m):
        m = m or {"first_call": None, "last_call": None, **_counts()}
        return {"user_id": uid, "name": name, "department": department, "active": active,
                **{k: m[k] for k in ("total", "first_call", "last_call", *STATUSES)}}

    rows, listed = [], set()
    for uid, user in users.items():
        if user.get("role") != "user":
            continue
        listed.add(uid)
        m = merged_users.get(uid)
        if not user.get("active", True) and not m:
            continue
        rows.append(row(uid, user["name"], user.get("department", ""), user.get("active", True), m))
    for uid, m in merged_users.items():   # calls whose account no longer exists
        if uid not in listed:
            rows.append(row(uid, uid, "", False, m))
    rows.sort(key=lambda r: (-r["total"], r["name"]))
    return rows


def branch_rows(merged_branches):
    """Calls grouped by the branch they came from (calls without a branch are not listed), busiest first."""
    rows = [{"code": code, "name": name, "region": region, **counts}
            for (code, name, region), counts in merged_branches.items()]
    rows.sort(key=lambda r: (-r["total"], r["code"], r["region"], r["name"]))   # same order every time
    return rows


def per_region(rows):
    totals = defaultdict(int)
    for row in rows:
        totals[row["region"] or "—"] += row["total"]
    return [{"region": k, "total": v} for k, v in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))]
