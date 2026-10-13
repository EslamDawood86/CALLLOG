"""Importing the settings of the three old scripts - by reading their source, never by running it.

The scripts below are SYNTHETIC: they copy the structure of the real ones (sets of addresses, dicts of sets, a CONFIG dict, values
written as ``os.getenv("X") or "default"``) with fake data only (example.test addresses, user 1001, made-up passwords).
"""
import json
import os
import stat
import sys

import pytest

import manage
from calllog.ops import legacy_import, settings as hub_settings
from calllog.ops.store import OpsStore

SLA_PASSWORD = "fake-sla-secret"
ITSM_PASSWORD = "fake-itsm-secret"

EMAIL_SCRIPT = '''
import os
import win32com.client                      # not installed here: proves the file is never imported
open(os.path.join(os.path.dirname(__file__), "EXECUTED.marker"), "w").write("the script ran")
raise SystemExit("the old script must never be executed")

BUSINESS_EMAILS = {
    "Support@Example.test",
    "ops@example.test",
    "not-an-address",
}
TEAM_MEMBERS = {"alice@example.test", "bob@example.test", "carol@example.test"}
TEAM_MEMBERS_BY_TEAM = {
    "Flex Cube": {
"alice@example.test",
"bob@example.test",
    },
    "Card Management": {"carol@example.test"},
    "Third Party": set(),
}
TEAM_LINK_GROUPS = {
    "Open System": {"Open System", "MFA Team"},
    "MFA Team": {"Open System", "MFA Team"},
}
IGNORED_SENDER_EMAILS = {"noreply@example.test"}
MANAGER_EMAILS = {
    # "boss@example.test",
}
MFA_TEAM_ADDRESSES = {"mfa@example.test", "mfa2@example.test"}
ALL_TEAMS = ["MFA Team", "Flex Cube", "Card Management", "Third Party", "Open System", "Unclassified"]
REPORT_TO = {"boss@example.test"}
REPORT_CC = {
    # "x@example.test",
}
TEAM_EMAIL_RECIPIENTS = {
    "Flex Cube": {"to": {"flex@example.test"}, "cc": set()},
    "MFA Team": {"to": {
        # "nobody@example.test",
    }, "cc": set()},
}
SEND_EMAIL_AUTOMATICALLY = True
ATTACH_EXCEL = False
ATTACH_PENDING_EMAILS = True
MAX_PENDING_ATTACHMENTS = 25
LOCAL_APP_DATA = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
EMAIL_SUBJECT_PREFIX = (
    "Daily Team Reply Analytics"
)
SEND_ASSIGNMENT_EMAILS = True
ASSIGNMENT_EMAIL_SUBJECT_PREFIX = "Action Required - Emails Assigned to You"
ASSIGNMENT_EMAIL_CC = {"assign-cc@example.test"}
SEND_QUALITY_FEEDBACK = False
QUALITY_FEEDBACK_SUBJECT_PREFIX = "Reply Quality Feedback"
QUALITY_SAMPLE_SIZE = 7
SEND_END_OF_DAY_SUMMARY = True
END_OF_DAY_SUBJECT_PREFIX = "End of Day Summary"
END_OF_DAY_TO = set(REPORT_TO)
END_OF_DAY_CC = set(REPORT_CC)
SCAN_END_BUFFER_MINUTES = 15
SLA_TARGET_MINUTES = 45
STATS_HISTORY_KEEP_DAYS = 21
OUTLOOK_CONNECT_RETRIES = 3
OUTLOOK_CONNECT_RETRY_SECONDS = 9
SKIP_ON_WEEKENDS = True
WEEKEND_WEEKDAYS = {4, 5}  # Friday, Saturday
HOLIDAY_DATES = {
    "2026-10-06",
    "2026-01-07",
    "bad date",
}
BACKLOG_LOOKBACK_DAYS = 2
TARGET_ARCHIVE_NAME = "2026_Part3"
IGNORED_FOLDER_TERMS = {"pgp"}
DASHBOARD_USERS = {"viewer": {"password": "dashboard-default-pw", "role": "manager"}}
NBE_LOGO_BASE64 = "iVBORw0KGgo="
'''

SLA_SCRIPT = f'''
import pandas as pd
PASSWORD = "{SLA_PASSWORD}"

CONFIG = {{
    "username": "1001",
    "warning_minutes": 45,
    "breach_minutes": 90,
    "repeat_minutes": 30,
    "interval_minutes": 20,
    "active_hours": "07:30-16:00",
    "work_days": ["Sun", "Mon", "Tue", "Wed", "Thu"],
    "mail": {{
        "subject": "Security Requests SLA",
        "display_only": False,
        "cc_security_users": True,
        "to": ["sla-boss@example.test", "x"],
        "cc": ["sla-cc@example.test"],
    }},
    "owner_emails": {{
        "مستخدم اول": "first@example.test",
        "مستخدم ثان": "not-an-address",
    }},
    "base_url": "https://portal.example.test",
    "login_path": "/login.aspx",
    "detail_headers": ["A", "B"],
    "date_order": "mdy",
    "selectors": {{}},
    "ignore_https_errors": True,
    "timeout_seconds": 30,
    "output_dir": "output",
    "logo_file": "assets/logo.png",
    "weird": object(),
}}
'''

ITSM_SCRIPT = f'''
import os
ITSM_URL = os.getenv("ITSM_URL", "https://itsm.example.test/CAisd")
USER = os.getenv("ITSM_USER") or "1001"
PASS = os.getenv("ITSM_PASS") or "{ITSM_PASSWORD}"
HEADLESS = os.getenv("HEADLESS", "1") == "1"
PORT = int(os.getenv("CDP_PORT", "9333"))
MAIL_TO = os.getenv("MAIL_TO") or "itsm-boss@example.test; second@example.test"
MAIL_CC = os.getenv("MAIL_CC") or ""
ROLES = ["Role One", "Role Two", "Role One"]
CATEGORIES = {{
    "Implementation Requests": r"^\\s*implementation requests?\\s*\\(\\d+\\)",
    "Rollback Requests": r"^\\s*rollback requests?\\s*\\(\\d+\\)",
}}
BROWSERS = [r"C:\\chrome.exe", os.path.expandvars(r"%LOCALAPPDATA%\\x.exe")]
'''


@pytest.fixture
def scripts(tmp_path):
    folder = tmp_path / "old"
    folder.mkdir()
    paths = {}
    for name, text in (("email", EMAIL_SCRIPT), ("sla", SLA_SCRIPT), ("itsm", ITSM_SCRIPT)):
        paths[name] = folder / f"{name}.py"
        paths[name].write_text(text, encoding="utf-8")
    return paths


@pytest.fixture
def imported(scripts):
    return legacy_import.import_legacy(scripts["email"], scripts["sla"], scripts["itsm"])


def test_the_scripts_are_never_executed_or_imported(scripts, imported):
    assert not (scripts["email"].parent / "EXECUTED.marker").exists()
    assert "win32com" not in sys.modules or sys.modules["win32com"] is None


def test_email_settings(imported):
    patch, _, notes = imported
    e = patch["email"]
    assert e["business_emails"] == ["ops@example.test", "support@example.test"]            # lower-case, sorted, the bad one dropped
    assert e["team_members"] == ["alice@example.test", "bob@example.test", "carol@example.test"]
    assert e["team_members_by_team"] == {"Flex Cube": ["alice@example.test", "bob@example.test"],
                                         "Card Management": ["carol@example.test"]}
    assert e["team_link_groups"] == {"Open System": ["MFA Team", "Open System"], "MFA Team": ["MFA Team", "Open System"]}
    assert e["ignored_sender_emails"] == ["noreply@example.test"]
    assert "manager_emails" not in e                                                       # empty in the old script: left alone
    assert e["report"]["to"] == ["boss@example.test"] and "cc" not in e["report"]
    assert e["report"]["team_recipients"] == {"Flex Cube": {"to": ["flex@example.test"], "cc": []}}
    assert e["report"]["attach_excel"] is False and e["report"]["attach_pending_msg"] is True
    assert e["report"]["max_pending_attachments"] == 25 and e["report"]["subject_prefix"] == "Daily Team Reply Analytics"
    assert e["end_of_day"]["to"] == ["boss@example.test"] and e["end_of_day"]["subject_prefix"] == "End of Day Summary"
    assert e["assignment_notifications"] == {"send": True, "cc": ["assign-cc@example.test"],
                                             "subject_prefix": "Action Required - Emails Assigned to You"}
    assert e["quality_feedback"]["send"] is False and e["quality_feedback"]["sample_size"] == 7
    assert (e["sla_target_minutes"], e["backlog_lookback_days"], e["history_keep_days"]) == (45, 2, 21)
    assert (e["outlook_connect_retries"], e["outlook_connect_retry_seconds"]) == (3, 9)
    assert e["advanced"] == {"scan_end_buffer_minutes": 15}
    assert e["classification"]["address_team_map"] == {"mfa@example.test": "MFA Team", "mfa2@example.test": "MFA Team"}
    assert e["teams"] == ["MFA Team", "Flex Cube", "Card Management", "Third Party", "Open System", "Unclassified"]
    assert any("not e-mail addresses" in n for n in notes)


def test_weekend_and_holidays(imported):
    patch, _, notes = imported
    assert patch["email"]["schedule"]["work_days"] == ["Mon", "Tue", "Wed", "Thu", "Sun"]       # weekend {4, 5} = Fri, Sat
    assert patch["general"]["holidays"] == ["2026-01-07", "2026-10-06"]
    assert not any("WEEKEND_WEEKDAYS" in n for n in notes)                                  # Fri/Sat is the usual weekend


@pytest.mark.parametrize("weekend, skip, expected, noted", [
    ("{3, 1}", "True", ["Mon", "Wed", "Fri", "Sat", "Sun"], True),                          # python numbers: Thursday and Tuesday
    ("{4, 5}", "False", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], False),
    ("{6}", "True", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], True),
])
def test_weekend_numbers_become_day_names(tmp_path, weekend, skip, expected, noted):
    path = tmp_path / "e.py"
    path.write_text(f"SKIP_ON_WEEKENDS = {skip}\nWEEKEND_WEEKDAYS = {weekend}\n", encoding="utf-8")
    patch, _, notes = legacy_import.import_legacy(email_path=path)
    assert patch["email"]["schedule"]["work_days"] == expected
    assert any("WEEKEND_WEEKDAYS" in n for n in notes) is noted


def test_sla_settings_and_secrets_split(imported):
    patch, cc, notes = imported
    s = patch["sla"]
    assert (s["warning_minutes"], s["breach_minutes"], s["repeat_minutes"], s["interval_minutes"]) == (45, 90, 30, 20)
    assert s["schedule"] == {"active_hours": "07:30-16:00", "work_days": ["Mon", "Tue", "Wed", "Thu", "Sun"]}
    assert s["mail"] == {"subject": "Security Requests SLA", "display_only": False, "cc_security_users": True,
                         "to": ["sla-boss@example.test"], "cc": ["sla-cc@example.test"]}
    assert s["owner_emails"] == {"مستخدم اول": "first@example.test"}
    assert s["base_url"] == "https://portal.example.test" and s["date_order"] == "mdy" and s["timeout_seconds"] == 30
    assert s["ignore_https_errors"] is True and s["detail_headers"] == ["A", "B"]
    assert "username" not in s and "password" not in s and "output_dir" not in s and "selectors" not in s
    assert cc["sla"] == {"username": "1001", "password": SLA_PASSWORD}
    assert cc["itsm"] == {"username": "1001", "password": ITSM_PASSWORD}
    assert cc["email"] == {"target_archive_name": "2026_Part3"}


def test_itsm_settings(imported):
    patch, _, _ = imported
    i = patch["itsm"]
    assert i["url"] == "https://itsm.example.test/CAisd" and i["headless"] is True and i["cdp_port"] == 9333
    assert i["roles"] == ["Role One", "Role Two"] and list(i["categories"]) == ["Implementation Requests", "Rollback Requests"]
    assert i["mail"] == {"to": ["itsm-boss@example.test", "second@example.test"]}


def test_no_password_or_dashboard_user_reaches_the_settings(imported):
    patch, cc, notes = imported
    blob = json.dumps(patch, ensure_ascii=False) + "\n".join(notes)
    for secret in (SLA_PASSWORD, ITSM_PASSWORD, "dashboard-default-pw", "iVBORw0KGgo"):
        assert secret not in blob
    assert "DASHBOARD_USERS" in blob
    assert json.dumps(cc).count(SLA_PASSWORD) == 1 and "dashboard-default-pw" not in json.dumps(cc)


def test_patch_is_valid_and_saveable(imported, tmp_path):
    patch, _, _ = imported
    clean, errors = hub_settings.validate(patch)
    assert errors == []
    ops = OpsStore(tmp_path / "data").init()
    hub_settings.patch(ops, patch)
    assert hub_settings.load(ops)["email"]["business_emails"] == ["ops@example.test", "support@example.test"]


def test_no_passwords_flag(scripts):
    patch, cc, notes = legacy_import.import_legacy(scripts["email"], scripts["sla"], scripts["itsm"], include_passwords=False)
    assert "password" not in json.dumps(cc) and cc["sla"] == {"username": "1001"}
    assert any("--set-password sla" in n for n in notes) and any("--set-password itsm" in n for n in notes)


def test_odd_input_is_skipped_with_a_note_never_an_exception(tmp_path):
    odd = tmp_path / "odd.py"
    odd.write_text('''
BUSINESS_EMAILS = compute_addresses()
TEAM_MEMBERS = {"a@example.test", some_function("x")}
REPORT_TO = "one@example.test; two@example.test"
SLA_TARGET_MINUTES = "sixty"
MAX_PENDING_ATTACHMENTS = 9999999
TEAM_MEMBERS_BY_TEAM = [1, 2, 3]
HOLIDAY_DATES = None
TARGET_ARCHIVE_NAME = ""
''', encoding="utf-8")
    broken = tmp_path / "broken.py"
    broken.write_text("def oops(:\n", encoding="utf-8")
    patch, cc, notes = legacy_import.import_legacy(email_path=odd, sla_path=broken, itsm_path=tmp_path / "missing.py")
    assert patch["email"]["report"]["to"] == ["one@example.test", "two@example.test"]
    assert "business_emails" not in patch["email"] and "team_members" not in patch["email"]
    assert "sla_target_minutes" not in patch["email"]
    assert cc == {}
    text = "\n".join(notes)
    assert "BUSINESS_EMAILS: not a plain value" in text and "SLA_TARGET_MINUTES" in text and "TEAM_MEMBERS_BY_TEAM" in text
    assert "broken.py: cannot be read" in text and "missing.py: cannot be read" in text
    assert hub_settings.validate(patch)[1] == []                                              # whatever is left can be saved


def test_validation_problems_drop_only_the_bad_key(tmp_path):
    path = tmp_path / "sla.py"
    path.write_text('CONFIG = {"username": "1", "warning_minutes": 50, "interval_minutes": 99999, "date_order": "xyz"}\n', encoding="utf-8")
    patch, cc, notes = legacy_import.import_legacy(sla_path=path)
    assert patch["sla"] == {"warning_minutes": 50} and cc["sla"] == {"username": "1"}
    assert any("sla.interval_minutes: skipped" in n for n in notes) and any("sla.date_order: skipped" in n for n in notes)


def test_summary_has_counts_only(imported):
    patch, cc, _ = imported
    lines = dict(legacy_import.describe(patch, cc))
    assert lines["business mailboxes"] == 2 and lines["team members"] == 3 and lines["passwords found"] == 2
    assert lines["accounts found (user name)"] == 2 and lines["Outlook archive name found"] == 1
    assert all(isinstance(v, int) for v in lines.values())


# ------------------------------------------------------------------------------------------ manage.py
@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLLOG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CALLLOG_CONFIG", str(tmp_path / "no-config.json"))
    return tmp_path / "data"


def import_args(scripts, out, *extra):
    return ["import-legacy", "--email", str(scripts["email"]), "--sla", str(scripts["sla"]), "--itsm", str(scripts["itsm"]),
            "--out-config", str(out), *extra]


def test_manage_import_writes_settings_without_secrets_and_config_with_them(scripts, data_dir, tmp_path, capsys):
    out = tmp_path / "collector_config.json"
    manage.main(import_args(scripts, out))
    settings_text = (data_dir / "ops" / "settings.json").read_text(encoding="utf-8")
    assert SLA_PASSWORD not in settings_text and ITSM_PASSWORD not in settings_text and "dashboard-default-pw" not in settings_text
    assert json.loads(settings_text)["email"]["business_emails"] == ["ops@example.test", "support@example.test"]
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["sla"] == {"username": "1001", "password": SLA_PASSWORD}
    assert written["itsm"] == {"username": "1001", "password": ITSM_PASSWORD}
    assert written["email"]["target_archive_name"] == "2026_Part3" and "_help" in written
    if os.name == "posix":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
    console = capsys.readouterr().out
    assert "business mailboxes: 2" in console and "passwords found: 2" in console
    for secret in (SLA_PASSWORD, ITSM_PASSWORD, "example.test"):
        assert secret not in console
    from calllog.ops import collector_config
    cc = collector_config.load(out)                                                          # the real loader accepts what was written
    assert cc.username("sla") == "1001" and cc.has_password("itsm") and "email.target_archive_name" in cc.pinned_keys()


def test_manage_import_refuses_to_overwrite_without_force(scripts, data_dir, tmp_path):
    out = tmp_path / "collector_config.json"
    out.write_text('{"sla": {"username": "keep-me"}}', encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        manage.main(import_args(scripts, out))
    assert "already exists" in str(stop.value) and "keep-me" in out.read_text(encoding="utf-8")
    assert not (data_dir / "ops" / "settings.json").exists()                                 # nothing was changed at all
    manage.main(import_args(scripts, out, "--force"))
    assert "keep-me" not in out.read_text(encoding="utf-8")


def test_manage_import_without_passwords(scripts, data_dir, tmp_path):
    out = tmp_path / "cc.json"
    manage.main(import_args(scripts, out, "--no-passwords"))
    assert "password" not in out.read_text(encoding="utf-8")


def test_manage_import_needs_a_source_and_existing_files(data_dir, tmp_path):
    with pytest.raises(SystemExit, match="at least one"):
        manage.main(["import-legacy", "--out-config", str(tmp_path / "x.json")])
    with pytest.raises(SystemExit, match="file not found"):
        manage.main(["import-legacy", "--sla", str(tmp_path / "nope.py"), "--out-config", str(tmp_path / "x.json")])


def test_manage_import_merges_into_existing_settings(scripts, data_dir, tmp_path):
    ops = OpsStore(data_dir).init()
    hub_settings.patch(ops, {"sla": {"mail": {"subject": "My own subject"}}, "itsm": {"interval_minutes": 5}})
    manage.main(["import-legacy", "--itsm", str(scripts["itsm"]), "--out-config", str(tmp_path / "cc.json")])
    saved = hub_settings.load(OpsStore(data_dir))
    assert saved["sla"]["mail"]["subject"] == "My own subject" and saved["itsm"]["interval_minutes"] == 5
    assert saved["itsm"]["roles"] == ["Role One", "Role Two"]


def test_seed_demo_explains_when_the_demo_module_is_missing(data_dir, monkeypatch):
    monkeypatch.setitem(sys.modules, "calllog.ops.demo", None)
    with pytest.raises(SystemExit, match="demo data module"):
        manage.main(["seed-demo", "--data-dir", str(data_dir)])


def test_seed_demo_calls_the_demo_module(data_dir, monkeypatch):
    import types
    calls = []
    fake = types.ModuleType("calllog.ops.demo")
    fake.seed = lambda ops: calls.append(("ops", type(ops).__name__, ops.root.parent))
    fake.seed_users_and_calls = lambda store: calls.append(("calls", type(store).__name__))
    monkeypatch.setitem(sys.modules, "calllog.ops.demo", fake)
    import calllog.ops
    monkeypatch.setattr(calllog.ops, "demo", fake, raising=False)
    manage.main(["seed-demo", "--data-dir", str(data_dir)])
    assert calls == [("ops", "OpsStore", data_dir.resolve()), ("calls", "Store")]


def test_existing_manage_commands_still_work(data_dir, capsys):
    manage.main(["list-users"])
    manage.main(["add-admin", "boss", "Boss Name"])
    assert "Administrator 'boss' created" in capsys.readouterr().out
