"""Reads the three old stand-alone scripts (e-mail analytics, SLA monitor, ITSM status) WITHOUT running them and turns
what is written in their code into Operations Hub settings.

The old scripts import win32com, open Outlook and start work as soon as they are loaded, so they are never imported
or executed here: the source is parsed with ``ast`` and only plain values are read (literals, sets, dicts,
``set(NAME)``, ``os.getenv("X") or "default"`` ...). Anything else is skipped and mentioned in the notes - this module
never raises because of an odd value in a legacy file.

``import_legacy`` returns ``(settings_patch, collector_config, notes)``:

* ``settings_patch``    goes into ``<data_dir>/ops/settings.json`` (shared, no secrets, already validated)
* ``collector_config``  the content for ``collector_config.json``: portal / ITSM user names, **passwords**, and the
                        Outlook archive name (see docs/HUB-DESIGN.md, section 10)
* ``notes``             human-readable remarks (counts and names of things, never a password or an address)

The old dashboard users (``DASHBOARD_USERS``) and the embedded logo are deliberately not read.
"""
import ast
import re
from pathlib import Path

from . import settings as hub_settings

_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALL_DAYS = list(hub_settings.WEEKDAYS)
_USUAL_WEEKEND = {4, 5}                      # Friday, Saturday (python weekday numbers)
HELP_TEXT = ("Written by 'python manage.py import-legacy' from the old scripts. Edit it in Notepad. Only the collector reads "
             "this file - never the web server. Do not e-mail it or put it in git.")


class _Skip(Exception):
    """A value that cannot be read safely from the source (not a plain literal)."""


_CASTS = {"int": int, "str": str, "float": float, "bool": bool, "set": set, "frozenset": frozenset, "list": list,
          "tuple": tuple, "sorted": sorted, "dict": dict}


def _is_env_call(node):
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
        return True
    return func.attr == "get" and isinstance(func.value, ast.Attribute) and func.value.attr == "environ"


class _Module:
    """The top-level assignments of one legacy file, evaluated on demand (never executed)."""

    def __init__(self, path):
        self.path = Path(path)
        self.nodes = {}
        self.cache = {}
        self.busy = set()
        self.error = None
        try:
            tree = ast.parse(self.path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError, RecursionError) as exc:
            self.error = f"{self.path.name}: cannot be read ({type(exc).__name__})"
            return
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.nodes[target.id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                self.nodes[node.target.id] = node.value

    def has(self, name):
        return name in self.nodes

    def get(self, name, default=None):
        """The value of a top-level name, or ``default`` when it is missing or not a plain value."""
        if name in self.cache:
            return self.cache[name]
        node = self.nodes.get(name)
        if node is None or name in self.busy:
            return default
        self.busy.add(name)
        try:
            self.cache[name] = self.value(node)
        except (_Skip, RecursionError, TypeError, ValueError):
            return default
        finally:
            self.busy.discard(name)
        return self.cache[name]

    def value(self, node, lenient=False, _depth=0):
        if _depth > 40:
            raise _Skip
        down = _depth + 1
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.value(e, lenient, down) for e in node.elts]
        if isinstance(node, ast.Set):
            return {self._hashable(self.value(e, lenient, down)) for e in node.elts}
        if isinstance(node, ast.Dict):
            return self._dict(node, lenient, down)
        if isinstance(node, ast.Name):
            value = self.get(node.id, _Skip)
            if value is _Skip:
                raise _Skip
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
            operand = self.value(node.operand, lenient, down)
            return -operand if isinstance(node.op, ast.USub) else operand if isinstance(node.op, ast.UAdd) else not operand
        if isinstance(node, ast.BoolOp):
            return self._bool_op(node, lenient, down)
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            same = self.value(node.left, lenient, down) == self.value(node.comparators[0], lenient, down)
            return same if isinstance(node.ops[0], ast.Eq) else not same
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(node.left, lenient, down), self.value(node.right, lenient, down)
            if isinstance(left, (str, list)) and type(left) is type(right):
                return left + right
            raise _Skip
        if isinstance(node, ast.Call):
            return self._call(node, lenient, down)
        raise _Skip

    @staticmethod
    def _hashable(value):
        if isinstance(value, (list, set, dict)):
            raise _Skip
        return value

    def _dict(self, node, lenient, depth):
        out = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:                       # {**other}
                if lenient:
                    continue
                raise _Skip
            try:
                out[self._hashable(self.value(key_node, lenient, depth))] = self.value(value_node, lenient, depth)
            except (_Skip, TypeError, ValueError):
                if not lenient:
                    raise
        return out

    def _bool_op(self, node, lenient, depth):
        is_or = isinstance(node.op, ast.Or)
        result = None
        for operand in node.values:
            result = self.value(operand, lenient, depth)
            if bool(result) == is_or:
                return result
        return result

    def _call(self, node, lenient, depth):
        if _is_env_call(node):                         # os.getenv("X") / os.environ.get("X", "default") -> the default
            default = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "default"), None)
            return self.value(default, lenient, depth) if default is not None else None
        if isinstance(node.func, ast.Name) and node.func.id in _CASTS and len(node.args) <= 1 and not node.keywords:
            cast = _CASTS[node.func.id]
            if not node.args:
                return cast()
            argument = self.value(node.args[0], lenient, depth)
            if isinstance(argument, dict) and cast in (set, frozenset, list, tuple, sorted):
                argument = list(argument)
            return cast(argument)
        raise _Skip


# --------------------------------------------------------------------------------------------- small converters
class _Patch:
    """Nested settings patch with a path setter (``set(("email", "report", "to"), [...])``)."""

    def __init__(self):
        self.data = {}

    def set(self, path, value):
        node = self.data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value

    def get(self, path, default=None):
        node = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def _as_items(value):
    if isinstance(value, dict):
        return list(value)
    if isinstance(value, (set, frozenset, list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [p for p in re.split(r"[;,\s]+", value) if p]
    return []


def _addresses(value, label, notes):
    """Sorted, lower-case, de-duplicated valid addresses; invalid entries are counted in the notes, not shown."""
    good, bad = set(), 0
    for item in _as_items(value):
        item = str(item).strip().lower()
        if _EMAIL_RE.match(item):
            good.add(item)
        elif item:
            bad += 1
    if bad:
        notes.append(f"{label}: {bad} entr{'y' if bad == 1 else 'ies'} that are not e-mail addresses were skipped")
    return sorted(good)


def _strings(value):
    out = []
    for item in _as_items(value):
        item = str(item).strip()
        if item and item not in out:
            out.append(item)
    return out


def _day_names(value):
    out = []
    for item in _as_items(value):
        short = str(item).strip()[:3].title()
        if short in _ALL_DAYS and short not in out:
            out.append(short)
    return [d for d in _ALL_DAYS if d in out]


def _convert(value, kind):
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise _Skip
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise _Skip
        return int(value)
    if kind == "str":
        if isinstance(value, str):
            return value.strip()
        raise _Skip
    raise _Skip


def _scalar(module, patch, notes, name, path, kind):
    if not module.has(name):
        return
    value = module.get(name, _Skip)
    if value is _Skip:
        notes.append(f"{name}: not a plain value - skipped")
        return
    try:
        patch.set(path, _convert(value, kind))
    except (_Skip, ValueError):
        notes.append(f"{name}: unexpected value - skipped")


# ------------------------------------------------------------------------------------------------------- e-mail
EMAIL_SCALARS = (
    ("SLA_TARGET_MINUTES", ("email", "sla_target_minutes"), "int"),
    ("BACKLOG_LOOKBACK_DAYS", ("email", "backlog_lookback_days"), "int"),
    ("STATS_HISTORY_KEEP_DAYS", ("email", "history_keep_days"), "int"),
    ("OUTLOOK_CONNECT_RETRIES", ("email", "outlook_connect_retries"), "int"),
    ("OUTLOOK_CONNECT_RETRY_SECONDS", ("email", "outlook_connect_retry_seconds"), "int"),
    ("SEND_EMAIL_AUTOMATICALLY", ("email", "report", "send"), "bool"),
    ("ATTACH_EXCEL", ("email", "report", "attach_excel"), "bool"),
    ("ATTACH_PENDING_EMAILS", ("email", "report", "attach_pending_msg"), "bool"),
    ("MAX_PENDING_ATTACHMENTS", ("email", "report", "max_pending_attachments"), "int"),
    ("EMAIL_SUBJECT_PREFIX", ("email", "report", "subject_prefix"), "str"),
    ("SEND_ASSIGNMENT_EMAILS", ("email", "assignment_notifications", "send"), "bool"),
    ("ASSIGNMENT_EMAIL_SUBJECT_PREFIX", ("email", "assignment_notifications", "subject_prefix"), "str"),
    ("SEND_QUALITY_FEEDBACK", ("email", "quality_feedback", "send"), "bool"),
    ("QUALITY_FEEDBACK_SUBJECT_PREFIX", ("email", "quality_feedback", "subject_prefix"), "str"),
    ("QUALITY_SAMPLE_SIZE", ("email", "quality_feedback", "sample_size"), "int"),
    ("SEND_END_OF_DAY_SUMMARY", ("email", "end_of_day", "send"), "bool"),
    ("END_OF_DAY_SUBJECT_PREFIX", ("email", "end_of_day", "subject_prefix"), "str"),
    # read by the engine from email.advanced
    ("SCAN_END_BUFFER_MINUTES", ("email", "advanced", "scan_end_buffer_minutes"), "int"),
    ("FORWARD_FALLBACK_WORD_LIMIT", ("email", "advanced", "forward_fallback_words"), "int"),
    ("REPEAT_ISSUE_THRESHOLD", ("email", "advanced", "repeat_issue_threshold"), "int"),
    ("REPEAT_ISSUE_LOOKBACK_DAYS", ("email", "advanced", "repeat_issue_lookback_days"), "int"),
    ("SHORT_REPLY_WORD_LIMIT", ("email", "advanced", "short_reply_limit"), "int"),
)

EMAIL_ADDRESS_LISTS = (
    ("BUSINESS_EMAILS", ("email", "business_emails")),
    ("TEAM_MEMBERS", ("email", "team_members")),
    ("IGNORED_SENDER_EMAILS", ("email", "ignored_sender_emails")),
    ("MANAGER_EMAILS", ("email", "manager_emails")),
    ("REPORT_TO", ("email", "report", "to")),
    ("REPORT_CC", ("email", "report", "cc")),
    ("ASSIGNMENT_EMAIL_CC", ("email", "assignment_notifications", "cc")),
    ("QUALITY_FEEDBACK_CC", ("email", "quality_feedback", "cc")),
    ("END_OF_DAY_TO", ("email", "end_of_day", "to")),
    ("END_OF_DAY_CC", ("email", "end_of_day", "cc")),
)


def _team_dict(module, name, notes, convert):
    """{team: converted value} from a legacy dict-of-sets; empty results are left out."""
    value = module.get(name, _Skip)
    if value is _Skip:
        if module.has(name):
            notes.append(f"{name}: not a plain value - skipped")
        return {}
    if not isinstance(value, dict):
        notes.append(f"{name}: expected a dictionary - skipped")
        return {}
    out = {}
    for team, content in value.items():
        converted = convert(str(team), content)
        if converted:
            out[str(team).strip()] = converted
    return out


def _import_email(path, patch, cc, notes):
    module = _Module(path)
    if module.error:
        notes.append(module.error)
        return
    if module.has("TARGET_ARCHIVE_NAME"):
        name = module.get("TARGET_ARCHIVE_NAME", "")
        if isinstance(name, str) and name.strip():
            cc.setdefault("email", {})["target_archive_name"] = name.strip()

    for name, path_ in EMAIL_ADDRESS_LISTS:
        if not module.has(name):
            continue
        value = module.get(name, _Skip)
        if value is _Skip:
            notes.append(f"{name}: not a plain value - skipped")
        elif _addresses(value, name, notes):
            patch.set(path_, _addresses(value, name, []))
    for name, path_, kind in EMAIL_SCALARS:
        _scalar(module, patch, notes, name, path_, kind)

    teams = module.get("ALL_TEAMS", None)
    team_list = _strings(teams) if isinstance(teams, (list, tuple, set)) else []

    by_team = _team_dict(module, "TEAM_MEMBERS_BY_TEAM", notes, lambda team, v: _addresses(v, f"TEAM_MEMBERS_BY_TEAM[{team}]", notes))
    if by_team:
        patch.set(("email", "team_members_by_team"), by_team)
    links = _team_dict(module, "TEAM_LINK_GROUPS", notes, lambda team, v: sorted(_strings(v)))
    if links:
        patch.set(("email", "team_link_groups"), links)
    recipients = _team_dict(module, "TEAM_EMAIL_RECIPIENTS", notes, lambda team, v: _recipients(team, v, notes))
    if recipients:
        patch.set(("email", "report", "team_recipients"), recipients)

    mfa = _addresses(module.get("MFA_TEAM_ADDRESSES", ()), "MFA_TEAM_ADDRESSES", notes)
    if mfa:
        patch.set(("email", "classification", "address_team_map"), {a: "MFA Team" for a in mfa})

    known = list(team_list)
    for extra in list(by_team) + list(links) + list(recipients) + (["MFA Team"] if mfa else []):
        if extra not in known:
            known.append(extra)
    if team_list:
        patch.set(("email", "teams"), known)
    elif known:
        base = [t for t in hub_settings.DEFAULT_TEAMS]
        patch.set(("email", "teams"), base + [t for t in known if t not in base])

    _import_calendar(module, patch, notes)
    if module.has("DASHBOARD_USERS"):
        notes.append("DASHBOARD_USERS (the old dashboard sign-ins) were not read: the web app has its own users")


def _recipients(team, value, notes):
    if not isinstance(value, dict):
        return None
    to = _addresses(value.get("to"), f"TEAM_EMAIL_RECIPIENTS[{team}].to", notes)
    cc = _addresses(value.get("cc"), f"TEAM_EMAIL_RECIPIENTS[{team}].cc", notes)
    return {"to": to, "cc": cc} if to or cc else None


def _import_calendar(module, patch, notes):
    """SKIP_ON_WEEKENDS / WEEKEND_WEEKDAYS (python weekday numbers) -> work days; HOLIDAY_DATES -> holidays."""
    if module.has("SKIP_ON_WEEKENDS") or module.has("WEEKEND_WEEKDAYS"):
        skip = module.get("SKIP_ON_WEEKENDS", True)
        weekend = module.get("WEEKEND_WEEKDAYS", None)
        numbers = {n for n in _as_items(weekend) if isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 6}
        if skip is False:
            patch.set(("email", "schedule", "work_days"), list(_ALL_DAYS))
            notes.append("SKIP_ON_WEEKENDS is off: the e-mail scan is allowed on every day")
        elif numbers:
            days = [d for i, d in enumerate(_ALL_DAYS) if i not in numbers]
            patch.set(("email", "schedule", "work_days"), days)
            if numbers != _USUAL_WEEKEND:
                skipped = ", ".join(_ALL_DAYS[i] for i in sorted(numbers))
                notes.append(f"WEEKEND_WEEKDAYS means {skipped} (python numbers {sorted(numbers)}) - the e-mail work days are now "
                             f"{', '.join(days)}. Check this if your weekend is Friday and Saturday (numbers 4 and 5).")
        elif module.has("WEEKEND_WEEKDAYS"):
            notes.append("WEEKEND_WEEKDAYS: not a set of weekday numbers - skipped")
    if module.has("HOLIDAY_DATES"):
        days = sorted({d for d in _strings(module.get("HOLIDAY_DATES", ())) if _DAY_RE.match(d)})
        if days:
            patch.set(("general", "holidays"), days)


# ---------------------------------------------------------------------------------------------------------- SLA
SLA_SPECIAL = {"username", "password", "mail", "active_hours", "work_days", "owner_emails", "output_dir", "logo_file"}


def _import_sla(path, patch, cc, notes, include_passwords):
    module = _Module(path)
    if module.error:
        notes.append(module.error)
        return
    node = module.nodes.get("CONFIG")
    config = {}
    if node is not None:
        try:
            config = module.value(node, lenient=True)
        except (_Skip, RecursionError, TypeError, ValueError):
            notes.append("CONFIG: not a plain dictionary - skipped")
    else:
        notes.append("SLA script: no CONFIG dictionary found")
    if not isinstance(config, dict):
        config = {}

    username = str(config.get("username") or "").strip()
    if username:
        cc.setdefault("sla", {})["username"] = username
    if module.has("PASSWORD"):
        password = module.get("PASSWORD", "")
        if isinstance(password, str) and password:
            if include_passwords:
                cc.setdefault("sla", {})["password"] = password
            else:
                notes.append("SLA password found in the script but not copied (--no-passwords): "
                             "use  python collector.py --set-password sla")

    defaults = hub_settings.DEFAULTS["sla"]
    for key, value in config.items():
        if key in SLA_SPECIAL or key not in defaults:
            continue
        if value in ("", [], {}, None):
            continue
        patch.set(("sla", key), value)
    if "active_hours" in config and isinstance(config["active_hours"], str):
        patch.set(("sla", "schedule", "active_hours"), config["active_hours"].strip())
    if "work_days" in config:
        days = _day_names(config["work_days"])
        patch.set(("sla", "schedule", "work_days"), days)

    mail = config.get("mail")
    if isinstance(mail, dict):
        for key in ("subject", "display_only", "cc_security_users", "send"):
            if key in mail:
                patch.set(("sla", "mail", key), mail[key])
        for key in ("to", "cc"):
            addresses = _addresses(mail.get(key), f"SLA mail.{key}", notes)
            if addresses:
                patch.set(("sla", "mail", key), addresses)
    owners = config.get("owner_emails")
    if isinstance(owners, dict):
        clean = {}
        for name, address in owners.items():
            found = _addresses(address, "SLA owner_emails", notes)
            if found and str(name).strip():
                clean[str(name).strip()] = found[0]
        if clean:
            patch.set(("sla", "owner_emails"), clean)


# --------------------------------------------------------------------------------------------------------- ITSM
def _import_itsm(path, patch, cc, notes, include_passwords):
    module = _Module(path)
    if module.error:
        notes.append(module.error)
        return
    user = module.get("USER", "")
    if user not in ("", None):
        cc.setdefault("itsm", {})["username"] = str(user).strip()
    if module.has("PASS"):
        password = module.get("PASS", "")
        if isinstance(password, str) and password:
            if include_passwords:
                cc.setdefault("itsm", {})["password"] = password
            else:
                notes.append("ITSM password found in the script but not copied (--no-passwords): "
                             "use  python collector.py --set-password itsm")
    url = module.get("ITSM_URL", "")
    if isinstance(url, str) and url.strip():
        patch.set(("itsm", "url"), url.strip())
    roles = module.get("ROLES", None)
    if isinstance(roles, (list, tuple, set)) and _strings(roles):
        patch.set(("itsm", "roles"), _strings(roles))
    categories = module.get("CATEGORIES", None)
    if isinstance(categories, dict) and categories:
        patch.set(("itsm", "categories"), {str(k).strip(): str(v) for k, v in categories.items() if str(k).strip()})
    elif module.has("CATEGORIES"):
        notes.append("CATEGORIES: not a plain dictionary - skipped")
    headless = module.get("HEADLESS", None)
    if isinstance(headless, bool):
        patch.set(("itsm", "headless"), headless)
    port = module.get("PORT", None)
    if isinstance(port, int) and not isinstance(port, bool):
        patch.set(("itsm", "cdp_port"), port)
    for name, key in (("MAIL_TO", "to"), ("MAIL_CC", "cc")):
        addresses = _addresses(module.get(name, ""), name, notes)
        if addresses:
            patch.set(("itsm", "mail", key), addresses)


# --------------------------------------------------------------------------------------------------- validation
def _drop_path(tree, path):
    """Remove the key a validation error points at. Keys may contain dots (addresses), so match them greedily."""
    for key in list(tree):
        if path == key:
            del tree[key]
            return True
        if path.startswith(key + ".") and isinstance(tree[key], dict) and _drop_path(tree[key], path[len(key) + 1:]):
            return True
    return False


_ADDRESS_LIKE = re.compile(r"[^\s'\"]+@[^\s'\"]+")


def _mask(text):
    return _ADDRESS_LIKE.sub("<address>", str(text))


def _make_valid(patch, notes):
    """Drop whatever the settings validation rejects (with a note), so the patch can always be saved."""
    for _ in range(4):
        _, errors = hub_settings.validate(patch)
        if not errors:
            return
        for err in errors:
            if _drop_path(patch, err["path"]):
                notes.append(f"{_mask(err['path'])}: skipped ({_mask(err['message'])})")
    _, errors = hub_settings.validate(patch)
    for err in errors:
        notes.append(f"{_mask(err['path'])}: still invalid ({_mask(err['message'])}) - check it on the System page")


def _prune(tree):
    """Remove empty dicts left behind after dropping keys."""
    for key in list(tree):
        if isinstance(tree[key], dict):
            _prune(tree[key])
            if not tree[key]:
                del tree[key]


def import_legacy(email_path=None, sla_path=None, itsm_path=None, include_passwords=True):
    """Read the given legacy scripts. Returns ``(settings_patch, collector_config, notes)`` - see the module docstring."""
    patch, cc, notes = _Patch(), {}, []
    if email_path:
        _import_email(email_path, patch, cc, notes)
    if sla_path:
        _import_sla(sla_path, patch, cc, notes, include_passwords)
    if itsm_path:
        _import_itsm(itsm_path, patch, cc, notes, include_passwords)
    data = patch.data
    _make_valid(data, notes)
    _prune(data)
    collector_config = {"_help": HELP_TEXT, **cc} if cc else {}
    return data, collector_config, notes


# --------------------------------------------------------------------------------------------------- summary
def describe(patch, collector_config):
    """Counts only: what was found, for the console summary. Never a password or an address."""
    p = _Patch()
    p.data = patch
    email_ = patch.get("email", {})
    lines = []

    def count(label, path):
        value = p.get(path)
        if isinstance(value, (list, dict)) and value:
            lines.append((label, len(value)))

    count("business mailboxes", ("email", "business_emails"))
    count("team members", ("email", "team_members"))
    by_team = email_.get("team_members_by_team", {})
    if by_team:
        lines.append(("teams with their own members", len(by_team)))
    count("teams", ("email", "teams"))
    count("link groups", ("email", "team_link_groups"))
    count("ignored senders", ("email", "ignored_sender_emails"))
    count("manager addresses", ("email", "manager_emails"))
    count("report recipients (to)", ("email", "report", "to"))
    count("report recipients (cc)", ("email", "report", "cc"))
    count("per-team recipient rules", ("email", "report", "team_recipients"))
    count("mailbox-to-team rules", ("email", "classification", "address_team_map"))
    count("holidays", ("general", "holidays"))
    count("SLA owner addresses", ("sla", "owner_emails"))
    count("SLA mail recipients (to)", ("sla", "mail", "to"))
    count("ITSM roles", ("itsm", "roles"))
    count("ITSM categories", ("itsm", "categories"))
    lines.append(("settings sections filled in", len(patch)))
    accounts = [k for k in ("sla", "itsm") if (collector_config.get(k) or {}).get("username")]
    lines.append(("accounts found (user name)", len(accounts)))
    lines.append(("passwords found", sum(1 for k in ("sla", "itsm") if (collector_config.get(k) or {}).get("password"))))
    lines.append(("Outlook archive name found", 1 if (collector_config.get("email") or {}).get("target_archive_name") else 0))
    return lines
