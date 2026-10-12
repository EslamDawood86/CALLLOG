"""Manual e-mail overrides applied on top of an e-mail snapshot, and the numbers recomputed from the result.

The collector builds the e-mail snapshot from Outlook and already folds the overrides in at scan time
(legacy ``Daily_Team_Reply_Analytics`` behaviour). People act on the web page between two scans, so the web app
applies the same rules again on every read: a click shows up at once and the next scan agrees with it.

Everything here is a pure function over plain dicts (no Flask, no files): ``apply_overrides`` -> new rows,
``recount`` -> new stats, ``scope_rows`` / ``rows_for_email`` -> who may see which rows. Inputs are never changed
(snapshots come out of a shared cache).
"""
import re
from collections import Counter
from datetime import datetime

from . import schemas

NO_ACTION_PREFIX = 'Manually marked "No Action Needed" by'
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_TEAM_PREFIX_RE = re.compile(r"^(flexcube|cardmanagement|mfa|opensystem|thirdparty)\.+", re.IGNORECASE)
_LIST_FIELDS = ("all_teams", "teams_pending", "teams_replied", "split_sibling_teams")


# --------------------------------------------------------------------------- small helpers
def normalize_email(value):
    return str(value or "").strip().lower()


def valid_email(value):
    return bool(EMAIL_RE.match(str(value or "")))


def display_name_from_email(raw_email):
    """'flexcube.alice.smith2@example.test' -> 'Alice Smith2' (same rule as the old dashboard's "Assign to" list)."""
    local = str(raw_email or "").split("@")[0]
    local = _TEAM_PREFIX_RE.sub("", local).strip(" .")
    name = " ".join(word.capitalize() for word in re.sub(r"[._\-]+", " ", local).split())
    return name or str(raw_email or "")


def message_key(row):
    """The id overrides are filed under (rows without a Message-ID cannot carry overrides)."""
    return row.get("message_id") or ""


def _same_team(a, b):
    return str(a or "").strip().casefold() == str(b or "").strip().casefold() and bool(str(a or "").strip())


def row_in_team(row, team):
    """A row belongs to a team when its ``team`` or any of ``all_teams`` is that team."""
    return _same_team(row.get("team"), team) or any(_same_team(t, team) for t in row.get("all_teams") or [])


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _minutes_between(start, end):
    a, b = _parse(start), _parse(end)
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() // 60)


def _fmt_minutes(minutes):
    if minutes is None:
        return ""
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} Min"
    h, m = divmod(minutes, 60)
    return f"{h} H {m} Min" if m else f"{h} H"


def _clone(row):
    out = dict(row)
    for field in _LIST_FIELDS:
        if isinstance(out.get(field), list):
            out[field] = list(out[field])
    return out


def response_minutes(row):
    """Minutes from receipt to the first reply (None when there is no reply)."""
    if row.get("first_reply_time") is None and row.get("response_minutes") is None:
        return None
    if row.get("response_minutes") is not None:
        return int(row["response_minutes"])
    return _minutes_between(row.get("received_time"), row.get("first_reply_time"))


# --------------------------------------------------------------------------- apply overrides
def _set_team(row, team):
    row["team"] = team
    row["all_teams"] = [team]
    row["teams_pending"] = [team] if row.get("status") == schemas.EMAIL_PENDING else []
    row["teams_replied"] = [team] if row.get("status") == schemas.EMAIL_REPLIED else []
    row.pop("is_split_child", None)
    row.pop("split_sibling_teams", None)


def _better_sibling(current, other):
    """Of two split children of one e-mail, the one that should stand for the whole e-mail: a replied one,
    and among replied ones the earliest reply (the legacy rule: the earliest reply answers the e-mail)."""
    cur_replied = current.get("status") == schemas.EMAIL_REPLIED
    new_replied = other.get("status") == schemas.EMAIL_REPLIED
    if new_replied and not cur_replied:
        return other
    if new_replied and cur_replied:
        a, b = current.get("first_reply_time") or "", other.get("first_reply_time") or ""
        return other if b and (not a or b < a) else current
    return current


def _collapse_and_retag(rows, team_overrides):
    """Team overrides win over everything. An e-mail the collector split into one row per team becomes a single
    row for the chosen team again (as the collector does when only one team is left)."""
    def chosen_team(row):
        key = message_key(row)
        return str((team_overrides.get(key) or {}).get("team") or "").strip() if key else ""

    out, at_index = [], {}
    for row in rows:
        if chosen_team(row) and row.get("is_split_child"):
            key = message_key(row)
            if key in at_index:
                out[at_index[key]] = _better_sibling(out[at_index[key]], row)
                continue
            at_index[key] = len(out)
        out.append(row)
    final = []
    for row in out:
        team = chosen_team(row)
        if team:
            collapsed = message_key(row) in at_index
            _set_team(row, team)
            # a collapsed e-mail that is answered and from an earlier day is no longer today's business
            if collapsed and row.get("status") == schemas.EMAIL_REPLIED and (row.get("age_days") or 0) > 0:
                continue
        final.append(row)
    return final


def _collapse_no_action(rows, no_action):
    """A no-action mark makes a split e-mail one row again (first sibling, all teams restored)."""
    out, done = [], set()
    for row in rows:
        key = message_key(row)
        if (key and key in no_action and row.get("is_split_child")
                and row.get("status") == schemas.EMAIL_PENDING):
            if key in done:
                continue
            done.add(key)
            row["all_teams"] = list(row.get("split_sibling_teams") or row.get("all_teams") or [])
            row.pop("is_split_child", None)
            row.pop("split_sibling_teams", None)
        out.append(row)
    return out


def _mark_no_action(row, mark):
    who = mark.get("marked_by") or "someone"
    row["status"] = schemas.EMAIL_NO_ACTION
    row["no_action_reason"] = f'{NO_ACTION_PREFIX} {who} on {mark.get("marked_at", "")}'
    row["sla_status"] = "N/A"
    row["escalated"] = False
    row["teams_pending"] = []
    row["duration_minutes"] = None
    # the collector drops a mail from an earlier day once it needs no more work
    return not (row.get("is_carryover") or (row.get("age_days") or 0) > 0)


def _unmark_no_action(row, now):
    """A manual mark was removed but the snapshot still shows its result: show the mail as pending again (the next
    scan recomputes everything; the automatic keyword rules may still call it no-action then)."""
    waiting = _minutes_between(row.get("received_time"), now.isoformat()) if now else None
    row["status"] = schemas.EMAIL_PENDING
    row["no_action_reason"] = ""
    row["teams_pending"] = list(row.get("all_teams") or ([row["team"]] if row.get("team") else []))
    row["is_carryover"] = (row.get("age_days") or 0) > 0
    if waiting is None:
        row["sla_status"] = "Within SLA"
        return
    limit = row.get("sla_minutes") or 60
    row["current_waiting_time"] = _fmt_minutes(waiting)
    row["duration_minutes"] = waiting
    row["sla_status"] = "Breached" if waiting > limit else "Within SLA"
    row["escalated"] = waiting > limit


def _apply_assignment(row, entry):
    entry = entry or {}
    row["assigned_to_email"] = normalize_email(entry.get("assigned_to_email"))
    row["assigned_to_name"] = entry.get("assigned_to_name", "") if entry else ""
    row["assigned_note"] = entry.get("note", "") if entry else ""
    row["assigned_by"] = entry.get("marked_by", "") if entry else ""
    row["assigned_at"] = entry.get("marked_at", "") if entry else ""


def _apply_quality(row, entry):
    rated = bool(entry) and row.get("status") == schemas.EMAIL_REPLIED       # a rating belongs to a real reply
    entry = entry if rated else {}
    row["quality_rating"] = entry.get("rating", "")
    row["quality_note"] = entry.get("note", "")
    row["quality_reasons"] = entry.get("reasons", "")
    row["quality_appeal"] = entry.get("appeal", "")
    row["appeal_decision"] = entry.get("appeal_decision", "")
    row["rating_withdrawn"] = bool(entry.get("rating_withdrawn", False))
    row["quality_reviewed_by"] = entry.get("reviewed_by", "")
    row["quality_reviewed_at"] = entry.get("reviewed_at", "")


def apply_overrides(rows, overrides, now=None):
    """New rows with the four override lists applied the way the collector applies them.

    ``overrides`` = {"no_action", "team", "assignments", "quality"} -> {message_id: entry}. ``now`` (datetime) is only
    needed to show a mail as pending again after its no-action mark was removed."""
    overrides = overrides or {}
    no_action = overrides.get("no_action") or {}
    team_ov = overrides.get("team") or {}
    assignments = overrides.get("assignments") or {}
    quality = overrides.get("quality") or {}

    rows = _collapse_and_retag([_clone(r) for r in rows or []], team_ov)
    rows = _collapse_no_action(rows, no_action)
    out = []
    for row in rows:
        key = message_key(row)
        mark = no_action.get(key) if key else None
        if mark and row.get("status") == schemas.EMAIL_PENDING:
            if not _mark_no_action(row, mark):
                continue
        elif (not mark and row.get("status") == schemas.EMAIL_NO_ACTION
              and str(row.get("no_action_reason") or "").startswith(NO_ACTION_PREFIX)):
            _unmark_no_action(row, now)
        _apply_assignment(row, assignments.get(key) if key else None)
        _apply_quality(row, quality.get(key) if key else None)
        out.append(row)
    return out


# --------------------------------------------------------------------------- recount
def _percent(numerator, denominator):
    return round(numerator / denominator * 100.0, 2) if denominator else 0.0


_TEAM_COUNT_KEYS = ("business_emails", "replied", "pending", "no_action_needed", "escalated_count", "carryover_count",
                    "carryover_oldest_days", "response_minutes_sum", "response_count", "sla_breached_count")


def _team_stats(rows, old):
    teams = {}
    for name, entry in (old or {}).items():
        teams[name] = {**entry, **{k: 0 for k in _TEAM_COUNT_KEYS}, "fastest_response_minutes": None}
    for row in rows:
        name = row.get("team") or "Unclassified"
        t = teams.setdefault(name, {**{k: 0 for k in _TEAM_COUNT_KEYS}, "fastest_response_minutes": None,
                                    "team_emails_sent": 0})
        t["business_emails"] += 1
        status = row.get("status")
        if status == schemas.EMAIL_REPLIED:
            t["replied"] += 1
        elif status == schemas.EMAIL_PENDING:
            t["pending"] += 1
        elif status == schemas.EMAIL_NO_ACTION:
            t["no_action_needed"] += 1
        if row.get("escalated"):
            t["escalated_count"] += 1
        if row.get("sla_status") == "Breached":
            t["sla_breached_count"] += 1
        if row.get("is_carryover"):
            t["carryover_count"] += 1
            t["carryover_oldest_days"] = max(t["carryover_oldest_days"], row.get("age_days") or 0)
        minutes = response_minutes(row)
        if minutes is not None and row.get("first_reply_time"):
            t["response_minutes_sum"] += minutes
            t["response_count"] += 1
            if t["fastest_response_minutes"] is None or minutes < t["fastest_response_minutes"]:
                t["fastest_response_minutes"] = minutes
    return teams


def _quality_block(rows, old):
    rated = [r for r in rows if r.get("quality_rating")]
    manager_replies = sum(1 for r in rows if r.get("reply_by_manager"))
    repliable = sum(1 for r in rows if r.get("status") == schemas.EMAIL_REPLIED and not r.get("reply_by_manager"))
    reviewed_nm = sum(1 for r in rated if not r.get("reply_by_manager"))
    return {**(old or {}),
            "reviewed": len(rated),
            "excellent": sum(1 for r in rated if r["quality_rating"] == "Excellent"),
            "good": sum(1 for r in rated if r["quality_rating"] == "Good"),
            "needs_improvement": sum(1 for r in rated if r["quality_rating"] == "Needs Improvement"),
            "appeals": sum(1 for r in rows if r.get("quality_appeal")),
            "coverage_rate": _percent(reviewed_nm, repliable or 1),
            "repliable_count": repliable, "manager_reply_count": manager_replies}


def recount(rows, stats):
    """``stats`` with every number that depends on the rows recomputed from ``rows`` (the same formulas the collector
    uses, so with no overrides the result equals the collector's own figures). Numbers that need Outlook itself
    (``team_replies``, ``team_emails_sent``, ...) are carried over."""
    out = dict(stats or {})
    rows = list(rows)
    business = sum(1 for r in rows if not r.get("is_carryover"))
    replied = sum(1 for r in rows if r.get("status") == schemas.EMAIL_REPLIED)
    pending = sum(1 for r in rows if r.get("status") == schemas.EMAIL_PENDING)
    no_action = sum(1 for r in rows if r.get("status") == schemas.EMAIL_NO_ACTION)
    needed = business - no_action
    out.update(business_emails=business, replied=replied, pending=pending, no_action_needed=no_action,
               needed_reply=needed, reply_rate=_percent(replied, needed))
    if "business_emails_incl_carryover" in out:
        out["business_emails_incl_carryover"] = len(rows)

    times = [m for m in (response_minutes(r) for r in rows if r.get("first_reply_time")) if m is not None]
    out.update(average_response=(sum(times) / len(times)) if times else 0,
               fastest_response=min(times) if times else 0, slowest_response=max(times) if times else 0)

    applicable = [r for r in rows if r.get("sla_status") != "N/A"]
    breached = sum(1 for r in applicable if r.get("sla_status") == "Breached")
    escalated = [r for r in rows if r.get("escalated")]
    out.update(sla_breached=breached, sla_compliance_rate=_percent(len(applicable) - breached, len(applicable)),
               escalated_count=len(escalated),
               escalated_items=[{"subject": r.get("subject", ""), "customer_name": r.get("customer_name", ""),
                                 "team": r.get("team", ""), "current_waiting_time": r.get("current_waiting_time", "")}
                                for r in sorted(escalated, key=lambda r: str(r.get("received_time") or ""))[:10]])
    slow = [r for r in rows if r.get("status") == schemas.EMAIL_REPLIED and r.get("first_reply_time")
            and response_minutes(r) is not None]
    slow.sort(key=lambda r: response_minutes(r), reverse=True)
    out["top_slowest_replies"] = [
        {"subject": r.get("subject", ""), "customer_name": r.get("customer_name", ""), "team": r.get("team", ""),
         "first_reply_by": r.get("first_reply_by", ""), "entry_id": r.get("entry_id", ""),
         "response_minutes": response_minutes(r)} for r in slow[:5]]

    carry = [r for r in rows if r.get("is_carryover")]
    out.update(carryover_count=len(carry), carryover_oldest_days=max((r.get("age_days") or 0 for r in carry), default=0),
               assigned_count=sum(1 for r in rows if r.get("assigned_to_email")))
    out["quality"] = _quality_block(rows, out.get("quality"))
    out["team_stats"] = _team_stats(rows, out.get("team_stats"))
    return out


# --------------------------------------------------------------------------- who sees which rows
def scope_rows(rows, user):
    """Rows ``user`` may see: everything for an admin or a manager without a team (empty / '__all__'), the team's
    rows for a manager with a team, nothing for anybody else."""
    role = (user or {}).get("role")
    if role == "admin":
        return list(rows)
    if role != "manager":
        return []
    team = str(user.get("team") or "").strip()
    if team in ("", schemas.ALL_TEAMS_MARK):
        return list(rows)
    return [r for r in rows if row_in_team(r, team)]


def assigned_rows(rows, email):
    email = normalize_email(email)
    return [r for r in rows if email and normalize_email(r.get("assigned_to_email")) == email]


def replied_rows(rows, email):
    email = normalize_email(email)
    return [r for r in rows if email and r.get("status") == schemas.EMAIL_REPLIED
            and normalize_email(r.get("first_reply_email")) == email]


def rows_for_email(rows, email):
    """Rows that are 'mine' for the mailbox ``email``: assigned to it, or first replied to by it (each row once)."""
    email = normalize_email(email)
    if not email:
        return []
    return [r for r in rows if normalize_email(r.get("assigned_to_email")) == email
            or (r.get("status") == schemas.EMAIL_REPLIED and normalize_email(r.get("first_reply_email")) == email)]


# --------------------------------------------------------------------------- a team-scoped view
def _keep_team(items, team):
    return [i for i in items or [] if isinstance(i, dict) and _same_team(i.get("team"), team)]


def _scope_quality(quality, team):
    if not isinstance(quality, dict):
        return {}
    out = {}
    for key, value in quality.items():
        if isinstance(value, list):
            out[key] = _keep_team(value, team)
        elif isinstance(value, dict):
            out[key] = {k: v for k, v in value.items() if _same_team(k, team)}
        else:
            out[key] = value
    return out


def view_email_data(data, overrides, scope_team=None, now=None):
    """The snapshot ``data`` with overrides applied, narrowed to ``scope_team`` (None = every team) and the numbers
    recomputed from what is left. A scoped view carries nothing about other teams' rows or people."""
    data = data if isinstance(data, dict) else {}
    rows = apply_overrides(data.get("rows") or [], overrides, now)
    stats = data.get("stats") or {}
    if scope_team:
        rows = [r for r in rows if row_in_team(r, scope_team)]
    new_stats = recount(rows, stats)
    out = {**data, "rows": rows, "stats": new_stats}
    if scope_team:
        unique = {message_key(r) or id(r): r for r in rows}.values()
        new_stats["team_stats"] = {k: v for k, v in new_stats["team_stats"].items() if _same_team(k, scope_team)}
        new_stats["system_volume"] = dict(Counter(
            s for r in rows for s in str(r.get("detected_systems") or "").split("; ") if s))
        new_stats["business_address_volume"] = {}
        new_stats["reply_breakdown"] = _keep_team(stats.get("reply_breakdown"), scope_team)
        new_stats["total_team_replies"] = sum(r.get("reply_count") or 0 for r in unique)
        for name, method in (("conversation_matches", "ConversationID"), ("conversation_index_matches", "ConversationIndex"),
                             ("subject_matches", "Subject"), ("no_matches", "No Match")):
            new_stats[name] = sum(1 for r in unique if r.get("match_method") == method)
        new_stats["trend"] = {}
        out["quality"] = _scope_quality(data.get("quality"), scope_team)
        out["teams"] = [t for t in data.get("teams") or [] if _same_team(t, scope_team)]
    return out
