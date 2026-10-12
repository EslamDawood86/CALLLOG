"""Settings of the Operations Hub: defaults, validation, load/save (``<data_dir>/ops/settings.json``).

No secret is stored here - portal passwords live in Windows Credential Manager (see docs/HUB-DESIGN.md, section 10).
Unknown keys are kept as they are; known keys are checked and normalised (e-mail addresses lower-case, de-duplicated).
"""
import copy
import json
import re

DEFAULT_TEAMS = ["MFA Team", "Flex Cube", "Card Management", "Third Party", "Open System", "Unclassified"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

DEFAULTS = {
    "version": 1,
    "general": {
        "work_days": ["Sun", "Mon", "Tue", "Wed", "Thu"],   # scheduled runs only on these days ([] = every day)
        "active_hours": "08:00-17:00",                       # scheduled runs only in this window ("" = all day)
        "holidays": [],                                      # YYYY-MM-DD, no scheduled runs
    },
    "email": {
        "enabled": True,
        "interval_minutes": 10,                 # read-only live scan -> dashboard
        "schedule": {"work_days": None, "active_hours": None},   # None = use general
        "target_archive_name": "",              # Outlook archive/root to scan, e.g. "2026_Part3" ("" = default mailbox)
        "business_emails": [],                  # an e-mail counts when one of these is in TO
        "team_members": [],                     # people whose reply counts as "the team replied"
        "team_members_by_team": {},             # team -> [addresses]   (optional, per-team reply tracking)
        "team_link_groups": {},                 # team -> [teams that answer for it]
        "ignored_sender_emails": [],
        "manager_emails": [],                   # their replies are not quality-reviewed
        "teams": list(DEFAULT_TEAMS),
        "sla_target_minutes": 60,
        "backlog_lookback_days": 1,             # unanswered mails from previous days that are still shown
        "history_keep_days": 30,
        "classification": {                     # how e-mails are sent to a team; tune it when too many land in "Unclassified"
            "system_team_map": {},              # keyword / application name -> team (added to the built-in map, wins over it)
            "address_team_map": {},             # business mailbox the mail was sent TO -> team
            "unclassified_team": "",            # mails with no detected team go here instead of "Unclassified" ("" = keep)
            "extra_no_action_keywords": [],     # extra phrases (Arabic or English) that mean "no reply needed"
        },
        "outlook_connect_retries": 5,
        "outlook_connect_retry_seconds": 15,
        "report": {                             # the old daily run
            "send": True,
            "send_times": ["16:30"],
            "to": [], "cc": [],
            "subject_prefix": "Daily Team Reply Analytics",
            "attach_excel": True,
            "attach_pending_msg": True,
            "max_pending_attachments": 40,
            "team_recipients": {},              # team -> {"to": [], "cc": []}
        },
        "assignment_notifications": {"send": True, "cc": []},
        "quality_feedback": {"send": True, "cc": [], "sample_size": 5},
        "end_of_day": {"send": True, "to": [], "cc": []},
        "advanced": {},                         # everything else the engine may read (keywords, system map overrides, ...)
    },
    "sla": {
        "enabled": True,
        "interval_minutes": 15,
        "schedule": {"work_days": None, "active_hours": None},
        "username": "",
        "warning_minutes": 60,
        "breach_minutes": 120,
        "repeat_minutes": 60,                   # while requests stay overdue, e-mail at most this often (0 = only on escalation)
        "mail": {"send": True, "to": [], "cc": [], "subject": "Security Requests SLA",
                 "display_only": False, "cc_security_users": False},
        "owner_emails": {},                     # security user name (as on the portal) -> e-mail
        "base_url": "https://nbesrv.nbe.ahly.bank",
        "login_path": "/App_Security_Login/Login.aspx",
        "reassign_path": "/SecurityCodeRequest/Security_Assign_Maker/Request_ReAssign.aspx",
        "app_url_fragment": "securitycoderequest",
        "package_text": "موظف اسناد - مستخدم السرية",
        "package_link_text": "تحويل",
        "login_button_text": "دخول",
        "search_button_text": "بحث",
        "sent_date_header": "تاريخ الارسال للسرية",
        "owner_header": "مستخدم السرية",
        "detail_headers": ["اسم الموظف", "رقم المستخدم", "الفرع / الادارة", "النظام", "نوع الطلب", "موظف الاسناد"],
        "date_order": "dmy",
        "selectors": {},                        # {"username": css, "password": css, "login_button": css}
        "browser_channel": "chrome",
        "browser_executable": "",
        "ignore_https_errors": False,
        "timeout_seconds": 60,
    },
    "itsm": {
        "enabled": True,
        "interval_minutes": 30,
        "schedule": {"work_days": None, "active_hours": None},
        "url": "https://itsm.nbe.com.eg:8443/CAisd/nbeitsm5",
        "username": "",
        "roles": ["IMP User provisioning", "IMP UAT I-Flex SMS"],
        "categories": {                         # label -> regex that matches the scoreboard line, e.g. "Rollback Requests (3)"
            "Implementation Requests": r"^\s*implementation requests?\s*\(\d+\)",
            "Rollback Requests": r"^\s*rollback requests?\s*\(\d+\)",
        },
        "headless": True,
        "cdp_port": 9222,
        "browser_executable": "",
        "mail": {"send": True, "send_times": ["08:30"], "to": [], "cc": [], "display_only": False},
    },
}

_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HOURS_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


class SettingsError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors[:5]))


def deep_merge(base, over):
    """``over`` on top of ``base``: dicts merge key by key, everything else is replaced. Neither argument is changed."""
    out = copy.deepcopy(base)
    _merge_into(out, over)
    return out


def _merge_into(out, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in _FREE_DICTS:
            _merge_into(out[k], v)
        else:
            out[k] = copy.deepcopy(v)


# dict-valued settings whose KEYS are data (teams, names), so a saved dict must replace the default, not merge into it
_FREE_DICTS = {"team_members_by_team", "team_link_groups", "team_recipients", "owner_emails", "categories",
               "selectors", "advanced", "system_team_map", "address_team_map"}


# --------------------------------------------------------------- field checkers
class _Ctx:
    def __init__(self):
        self.errors = []

    def err(self, path, message):
        self.errors.append({"path": path, "message": message})


def _bool(ctx, path, v):
    if isinstance(v, bool):
        return v
    ctx.err(path, "must be true or false")
    return False


def _int(lo, hi):
    def check(ctx, path, v):
        if isinstance(v, bool) or not isinstance(v, int) and not (isinstance(v, str) and v.strip().lstrip("-").isdigit()):
            ctx.err(path, f"must be a whole number between {lo} and {hi}")
            return lo
        n = int(v)
        if not lo <= n <= hi:
            ctx.err(path, f"must be between {lo} and {hi}")
            return min(max(n, lo), hi)
        return n
    return check


def _str(maxlen=300, allow_empty=True):
    def check(ctx, path, v):
        if v is None:
            v = ""
        if not isinstance(v, str):
            ctx.err(path, "must be text")
            return ""
        v = v.strip()
        if len(v) > maxlen:
            ctx.err(path, f"is too long (max {maxlen})")
        if not v and not allow_empty:
            ctx.err(path, "must not be empty")
        return v
    return check


def _choice(*options):
    def check(ctx, path, v):
        if v not in options:
            ctx.err(path, f"must be one of {', '.join(options)}")
            return options[0]
        return v
    return check


def _email_one(ctx, path, v):
    v = (v or "").strip().lower() if isinstance(v, str) else v
    if not isinstance(v, str) or not _EMAIL_RE.match(v):
        ctx.err(path, f"'{v}' is not an e-mail address")
        return ""
    return v


def _emails(ctx, path, v):
    if v is None:
        return []
    if isinstance(v, str):               # allow "a@x.com; b@y.com"
        v = [p for p in re.split(r"[;,\s]+", v) if p]
    if not isinstance(v, list):
        ctx.err(path, "must be a list of e-mail addresses")
        return []
    out = []
    for item in v:
        addr = _email_one(ctx, path, item)
        if addr and addr not in out:
            out.append(addr)
    return sorted(out)


def _strs(maxlen=200, lower=False):
    def check(ctx, path, v):
        if v is None:
            return []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            ctx.err(path, "must be a list of text values")
            return []
        out = []
        for x in v:
            x = x.strip()
            x = x.lower() if lower else x
            if not x:
                continue
            if len(x) > maxlen:
                ctx.err(path, f"an entry is too long (max {maxlen})")
            if x not in out:
                out.append(x)
        return out
    return check


def _times(ctx, path, v):
    items = _strs(5)(ctx, path, v)
    for t in items:
        if not _TIME_RE.match(t):
            ctx.err(path, f"'{t}' is not a time HH:MM")
    return sorted(t for t in items if _TIME_RE.match(t))


def _days(ctx, path, v):
    if v is None:
        return None
    items = _strs(20)(ctx, path, v)
    out = []
    for d in items:
        short = d[:3].title()
        if short not in WEEKDAYS:
            ctx.err(path, f"'{d}' is not a weekday (Mon..Sun)")
        elif short not in out:
            out.append(short)
    return out


def _hours(ctx, path, v):
    if v is None:
        return None
    v = _str(11)(ctx, path, v).replace(" ", "")
    if v and not _HOURS_RE.match(v):
        ctx.err(path, "must look like 08:00-17:00 (or be empty for all day)")
        return ""
    return v


def _dates(ctx, path, v):
    items = _strs(10)(ctx, path, v)
    for d in items:
        if not _DAY_RE.match(d):
            ctx.err(path, f"'{d}' is not a date YYYY-MM-DD")
    return sorted(d for d in items if _DAY_RE.match(d))


def _map(value_check, key_max=120):
    def check(ctx, path, v):
        if v is None:
            return {}
        if not isinstance(v, dict):
            ctx.err(path, "must be an object")
            return {}
        out = {}
        for k, val in v.items():
            if not isinstance(k, str) or not k.strip() or len(k) > key_max:
                ctx.err(path, "has an invalid name")
                continue
            out[k.strip()] = value_check(ctx, f"{path}.{k}", val)
        return out
    return check


def _regex(ctx, path, v):
    v = _str(300, allow_empty=False)(ctx, path, v)
    try:
        re.compile(v)
    except re.error as exc:
        ctx.err(path, f"is not a valid pattern ({exc})")
    return v


def _addr_map(ctx, path, v):
    """business mailbox -> team; the mailbox is lower-cased and must be an address."""
    if v is None:
        return {}
    if not isinstance(v, dict):
        ctx.err(path, "must be an object")
        return {}
    out = {}
    for k, team in v.items():
        addr = _email_one(ctx, f"{path}.{k}", k)
        if addr:
            out[addr] = _str(80, allow_empty=False)(ctx, f"{path}.{k}", team)
    return out


def _recipients(ctx, path, v):
    if not isinstance(v, dict):
        ctx.err(path, "must be an object with to / cc")
        return {"to": [], "cc": []}
    return {"to": _emails(ctx, f"{path}.to", v.get("to")), "cc": _emails(ctx, f"{path}.cc", v.get("cc"))}


def _any_dict(ctx, path, v):
    if v is None:
        return {}
    if not isinstance(v, dict):
        ctx.err(path, "must be an object")
        return {}
    return v


def _schedule(ctx, path, v):
    v = v if isinstance(v, dict) else {}
    return {"work_days": _days(ctx, f"{path}.work_days", v.get("work_days")),
            "active_hours": _hours(ctx, f"{path}.active_hours", v.get("active_hours"))}


_MAIL_BASE = {"send": _bool, "to": _emails, "cc": _emails}

FIELDS = {
    "general": {"work_days": _days, "active_hours": _hours, "holidays": _dates},
    "email": {
        "enabled": _bool, "interval_minutes": _int(1, 1440), "schedule": _schedule,
        "target_archive_name": _str(120), "business_emails": _emails, "team_members": _emails,
        "team_members_by_team": _map(_emails), "team_link_groups": _map(_strs(80)),
        "ignored_sender_emails": _emails, "manager_emails": _emails, "teams": _strs(80),
        "sla_target_minutes": _int(1, 10080), "backlog_lookback_days": _int(0, 30), "history_keep_days": _int(1, 365),
        "outlook_connect_retries": _int(1, 20), "outlook_connect_retry_seconds": _int(1, 300),
        "classification": {"system_team_map": _map(_str(80, allow_empty=False)), "address_team_map": _addr_map,
                           "unclassified_team": _str(80), "extra_no_action_keywords": _strs(120)},
        "report": {"send": _bool, "send_times": _times, "to": _emails, "cc": _emails, "subject_prefix": _str(120),
                   "attach_excel": _bool, "attach_pending_msg": _bool, "max_pending_attachments": _int(1, 500),
                   "team_recipients": _map(_recipients)},
        "assignment_notifications": {"send": _bool, "cc": _emails},
        "quality_feedback": {"send": _bool, "cc": _emails, "sample_size": _int(1, 100)},
        "end_of_day": {"send": _bool, "to": _emails, "cc": _emails},
        "advanced": _any_dict,
    },
    "sla": {
        "enabled": _bool, "interval_minutes": _int(1, 1440), "schedule": _schedule, "username": _str(60),
        "warning_minutes": _int(1, 10080), "breach_minutes": _int(1, 10080), "repeat_minutes": _int(0, 10080),
        "mail": {**_MAIL_BASE, "subject": _str(150), "display_only": _bool, "cc_security_users": _bool},
        "owner_emails": _map(_email_one),
        "base_url": _str(300, False), "login_path": _str(300), "reassign_path": _str(300), "app_url_fragment": _str(100),
        "package_text": _str(200), "package_link_text": _str(100), "login_button_text": _str(100),
        "search_button_text": _str(100), "sent_date_header": _str(100), "owner_header": _str(100),
        "detail_headers": _strs(100), "date_order": _choice("dmy", "mdy"), "selectors": _map(_str(300)),
        "browser_channel": _str(40), "browser_executable": _str(400), "ignore_https_errors": _bool,
        "timeout_seconds": _int(5, 600),
    },
    "itsm": {
        "enabled": _bool, "interval_minutes": _int(1, 1440), "schedule": _schedule, "url": _str(300, False),
        "username": _str(60), "roles": _strs(120), "categories": _map(_regex),
        "headless": _bool, "cdp_port": _int(1024, 65535), "browser_executable": _str(400),
        "mail": {**_MAIL_BASE, "send_times": _times, "display_only": _bool},
    },
}


def _apply(ctx, spec, values, path):
    """Check ``values`` against ``spec``; return a dict with the checked keys normalised and the others untouched."""
    if not isinstance(values, dict):
        ctx.err(path, "must be an object")
        return {}
    out = dict(values)
    for key, checker in spec.items():
        sub = f"{path}.{key}" if path else key
        if key not in values:
            continue
        if isinstance(checker, dict):
            out[key] = _apply(ctx, checker, values[key], sub)
        else:
            out[key] = checker(ctx, sub, values[key])
    return out


def validate(raw):
    """Merge ``raw`` over the defaults and check it. Returns ``(clean_settings, errors)``."""
    ctx = _Ctx()
    merged = deep_merge(DEFAULTS, raw if isinstance(raw, dict) else {})
    clean = dict(merged)
    for section, spec in FIELDS.items():
        clean[section] = _apply(ctx, spec, merged.get(section), section)
    if clean["sla"]["breach_minutes"] <= clean["sla"]["warning_minutes"]:
        ctx.err("sla.breach_minutes", "must be larger than warning_minutes")
    if clean["email"]["teams"] and "Unclassified" not in clean["email"]["teams"]:
        clean["email"]["teams"].append("Unclassified")
    teams = clean["email"]["teams"]
    cls = clean["email"]["classification"]
    for name in ("system_team_map", "address_team_map"):
        for key, team in cls[name].items():
            if team not in teams:
                ctx.err(f"email.classification.{name}.{key}", f"'{team}' is not one of the teams")
    if cls["unclassified_team"] and cls["unclassified_team"] not in teams:
        ctx.err("email.classification.unclassified_team", f"'{cls['unclassified_team']}' is not one of the teams")
    return clean, ctx.errors


def load(store):
    """Current settings (defaults + saved file). A damaged value falls back to the default for that key.

    Checking all the settings takes a moment and nearly every page request needs them, so the checked result is kept
    until the saved file changes. The caller gets its own copy and may change it."""
    raw = store.read_settings_raw()
    cached = getattr(store, "_validated_settings", None)
    if cached is None or not (cached[0] is raw or cached[0] == raw):
        clean, _ = validate(raw)
        cached = store._validated_settings = (raw, json.dumps(clean, ensure_ascii=False))
    return json.loads(cached[1])


def save(store, new_settings):
    """Validate and save the WHOLE settings document. Raises SettingsError listing every problem."""
    clean, errors = validate(new_settings)
    if errors:
        raise SettingsError(errors)
    store.write_settings_raw(clean)
    return clean


def patch(store, changes):
    """Merge ``changes`` over the saved settings, validate and save."""
    return save(store, deep_merge(load(store), changes))


# ------------------------------------------------------------------ scheduling helpers
def effective_schedule(settings, module):
    """(work_days, active_hours) for a module: its own override, else the general values."""
    own = settings.get(module, {}).get("schedule") or {}
    general = settings.get("general", {})
    days = own.get("work_days") if own.get("work_days") is not None else general.get("work_days", [])
    hours = own.get("active_hours") if own.get("active_hours") is not None else general.get("active_hours", "")
    return days, hours


def is_active(settings, module, now):
    """True when a scheduled run is allowed at ``now`` (work day, active hours, not a holiday)."""
    if now.date().isoformat() in settings.get("general", {}).get("holidays", []):
        return False
    days, hours = effective_schedule(settings, module)
    if days and WEEKDAYS[now.weekday()] not in days:
        return False
    if hours:
        start, end = hours.split("-")
        return start <= f"{now:%H:%M}" <= end
    return True
