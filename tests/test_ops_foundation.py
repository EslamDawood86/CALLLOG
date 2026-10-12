"""OpsStore + settings: the contract every collector and page relies on."""
import datetime as dt
import threading

import pytest

from calllog.ops import schemas, settings
from calllog.ops.store import OpsStore, safe_file_name


@pytest.fixture
def ops(tmp_path):
    return OpsStore(tmp_path / "data").init()


# --------------------------------------------------------------------------- store
def test_snapshot_roundtrip_and_validation(ops):
    assert ops.read_snapshot("sla") is None
    data = {"thresholds": {}, "totals": {}, "by_user": [], "requests": [
        {"key": "k", "status": "breached"}]}
    env = ops.write_snapshot("sla", data, generated_at="2026-10-11T09:15:03", host="PC1")
    assert env["module"] == "sla" and env["date"] == "2026-10-11"
    assert ops.read_snapshot("sla")["data"] == data
    assert schemas.validate_snapshot(env) == []
    assert schemas.validate_snapshot({**env, "data": {"requests": []}})


def test_unknown_names_are_rejected(ops):
    with pytest.raises(ValueError):
        ops.write_snapshot("../etc", {})
    with pytest.raises(ValueError):
        ops.read_heartbeat("nope")
    with pytest.raises(ValueError):
        ops.overrides("passwd")


def test_history_points_accumulate(ops):
    ops.add_history_point("sla", "2026-10-11", {"ts": "09:00", "open": 3}, extra={"first_breach": {"a": "09:00"}})
    ops.add_history_point("sla", "2026-10-11", {"ts": "09:15", "open": 4}, extra={"first_breach": {"a": "09:15", "b": "09:15"}})
    doc = ops.read_history("sla", "2026-10-11")
    assert [p["open"] for p in doc["points"]] == [3, 4]
    assert doc["first_breach"] == {"a": "09:00", "b": "09:15"}      # the first time a key was seen is kept
    assert ops.history_days("sla") == ["2026-10-11"]
    with pytest.raises(ValueError):
        ops.read_history("sla", "../../x")


def test_overrides_are_atomic_per_key(ops):
    ops.set_override("assignments", "m1", {"assigned_to_email": "a@nbe.com.eg"})
    ops.set_override("assignments", "m2", {"assigned_to_email": "b@nbe.com.eg"})
    assert set(ops.overrides("assignments")) == {"m1", "m2"}
    assert ops.update_override("assignments", "m1", lambda old: {**old, "note": "x"})["note"] == "x"
    ops.delete_override("assignments", "m2")
    assert set(ops.overrides("assignments")) == {"m1"}


def test_overrides_concurrent_writers_do_not_lose_updates(ops):
    def work(i):
        for j in range(10):
            ops.set_override("no_action", f"m{i}-{j}", {"n": j})
    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ops.overrides("no_action")) == 60


def test_heartbeat_merges_fields(ops):
    ops.write_heartbeat("email", host="PC1", running=True)
    hb = ops.write_heartbeat("email", running=False, last_status="ok")
    assert hb["host"] == "PC1" and hb["running"] is False and hb["job"] == "email" and hb["updated_at"]
    assert ops.read_heartbeats()["sla"] is None


def test_run_requests_are_consumed_once(ops):
    ops.add_run_request("sla", "admin")
    ops.add_run_request("sla", "admin")
    ops.add_run_request("itsm", "admin")
    assert len(ops.pending_run_requests("sla")) == 2
    assert len(ops.consume_run_requests("sla")) == 2
    assert ops.consume_run_requests("sla") == []
    assert len(ops.pending_run_requests()) == 1


def test_try_lock_is_exclusive(ops):
    with ops.try_lock("job-sla") as first:
        assert first is True
        with ops.try_lock("job-sla") as second:
            assert second is False
    with ops.try_lock("job-sla") as again:
        assert again is True


def test_log_tail_and_rotation(ops):
    for i in range(30):
        ops.append_log("sla", f"line {i}")
    tail = ops.tail_log("sla", 5)
    assert len(tail) == 5 and tail[-1].endswith("line 29")
    assert ops.tail_log("itsm") == []


def test_report_paths_cannot_escape(ops):
    folder = ops.report_dir("email")
    (folder / "ok.xlsx").write_bytes(b"x")
    (ops.reports_dir / "secret.txt").write_text("no")
    assert ops.report_path("email", "ok.xlsx") is not None
    for bad in ("../secret.txt", "..\\secret.txt", "/etc/passwd", ".hidden", "a/b.xlsx", "", "x" * 300, None):
        assert ops.report_path("email", bad) is None
    assert ops.report_path("../email", "ok.xlsx") is None
    assert not safe_file_name("a..b")
    assert [r["name"] for r in ops.list_reports("email")] == ["ok.xlsx"]


def test_state_files(ops):
    assert ops.read_state("sla") == {}
    ops.write_state("sla", {"last_sent": "x"})
    assert ops.read_state("sla") == {"last_sent": "x"}
    with pytest.raises(ValueError):
        ops.read_state("../x")


# ------------------------------------------------------------------------ settings
def test_defaults_are_valid():
    clean, errors = settings.validate({})
    assert errors == []
    assert clean["sla"]["breach_minutes"] == 120 and clean["itsm"]["roles"][0] == "IMP User provisioning"


def test_emails_normalised_and_checked():
    clean, errors = settings.validate({"email": {"team_members": ["B@NBE.com.eg", "a@nbe.com.eg", "b@nbe.com.eg"],
                                                 "report": {"to": "x@nbe.com.eg; y@nbe.com.eg"}}})
    assert errors == []
    assert clean["email"]["team_members"] == ["a@nbe.com.eg", "b@nbe.com.eg"]
    assert clean["email"]["report"]["to"] == ["x@nbe.com.eg", "y@nbe.com.eg"]
    _, errors = settings.validate({"email": {"business_emails": ["not-an-address"]}})
    assert errors and errors[0]["path"] == "email.business_emails"


def test_bad_values_are_named():
    _, errors = settings.validate({"sla": {"interval_minutes": 0, "breach_minutes": 30, "warning_minutes": 60},
                                   "general": {"active_hours": "8-5", "work_days": ["Funday"]},
                                   "itsm": {"categories": {"X": "("}}})
    paths = {e["path"] for e in errors}
    assert {"sla.interval_minutes", "sla.breach_minutes", "general.active_hours", "general.work_days"} <= paths
    assert "itsm.categories.X" in paths


def test_free_dicts_replace_defaults_not_merge():
    clean, errors = settings.validate({"itsm": {"categories": {"Only": r"^only"}}})
    assert errors == [] and list(clean["itsm"]["categories"]) == ["Only"]


def test_unknown_keys_survive_and_save_roundtrip(ops):
    saved = settings.save(ops, {"custom": {"a": 1}, "email": {"business_emails": ["a@nbe.com.eg"]}})
    assert saved["custom"] == {"a": 1}
    assert settings.load(ops)["email"]["business_emails"] == ["a@nbe.com.eg"]
    with pytest.raises(settings.SettingsError):
        settings.save(ops, {"sla": {"timeout_seconds": 1}})
    settings.patch(ops, {"sla": {"username": "239582"}})
    assert settings.load(ops)["sla"]["username"] == "239582"
    assert settings.load(ops)["email"]["business_emails"] == ["a@nbe.com.eg"]


def test_schedule_rules():
    s, _ = settings.validate({"general": {"holidays": ["2026-10-14"]}, "sla": {"schedule": {"active_hours": "09:00-10:00"}}})
    sunday = dt.datetime(2026, 10, 11, 9, 30)        # Sunday
    assert settings.is_active(s, "email", sunday)
    assert not settings.is_active(s, "email", dt.datetime(2026, 10, 11, 7, 59))
    assert not settings.is_active(s, "email", dt.datetime(2026, 10, 9, 9, 30))      # Friday
    assert not settings.is_active(s, "email", dt.datetime(2026, 10, 14, 9, 30))     # holiday
    assert settings.is_active(s, "sla", sunday) and not settings.is_active(s, "sla", dt.datetime(2026, 10, 11, 12, 0))


def test_classification_settings():
    clean, errors = settings.validate({"email": {"classification": {
        "system_team_map": {"Swift Alliance": "Third Party", "فليكس": "Flex Cube"},
        "address_team_map": {"MFA_Team@NBE.com.eg": "MFA Team"},
        "unclassified_team": "Open System", "extra_no_action_keywords": ["تم الاستلام", "fyi"]}}})
    assert errors == []
    cls = clean["email"]["classification"]
    assert cls["address_team_map"] == {"mfa_team@nbe.com.eg": "MFA Team"}
    assert cls["system_team_map"]["Swift Alliance"] == "Third Party" and cls["unclassified_team"] == "Open System"
    _, errors = settings.validate({"email": {"classification": {
        "system_team_map": {"X": "No Such Team"}, "unclassified_team": "Nope", "address_team_map": {"bad": "MFA Team"}}}})
    assert {e["path"] for e in errors} >= {"email.classification.system_team_map.X",
                                           "email.classification.unclassified_team",
                                           "email.classification.address_team_map.bad"}


# --------------------------------------------------------------------------- events
def _sla_snap(statuses, ts="2026-10-11T09:15:00"):
    reqs = [{"key": k, "status": s, "security_user": "u-" + k} for k, s in statuses.items()]
    return {"generated_at": ts, "data": {"thresholds": {"breach_minutes": 120}, "requests": reqs}}


def test_sla_events_are_aggregated():
    from calllog.ops import events
    old = _sla_snap({"a": "within", "b": "reminder", "c": "breached", "d": "within"})
    new = _sla_snap({"a": "reminder", "b": "breached", "c": "breached", "e": "within"}, "2026-10-11T09:30:00")
    kinds = {e["kind"]: e for e in events.diff_events("sla", old, new)}
    assert kinds["sla_breached"]["count"] == 1 and kinds["sla_breached"]["level"] == "danger"
    assert kinds["sla_reminder"]["count"] == 1 and kinds["sla_closed"]["count"] == 1   # d completed
    assert events.diff_events("sla", None, new) == []                                    # first scan: nothing to compare


def test_itsm_and_email_events():
    from calllog.ops import events
    new = {"generated_at": "2026-10-11T10:00:00", "data": {"changes": {
        "new": [{"role": "R", "category": "Rollback Requests", "number": "1", "summary": "s"}],
        "closed": [{"role": "R", "category": "Implementation Requests", "number": "2", "summary": "t"}]}}}
    evs = events.diff_events("itsm", None, new)
    assert [(e["kind"], e["level"]) for e in evs] == [("itsm_new", "warn"), ("itsm_closed", "ok")]
    old = {"data": {"rows": [{"message_id": "m1", "status": "Pending Reply", "escalated": False}]}}
    cur = {"generated_at": "2026-10-11T10:05:00", "data": {"rows": [
        {"message_id": "m1", "status": "Pending Reply", "escalated": True, "subject": "S", "team": "Flex Cube"},
        {"message_id": "m2", "status": "Replied", "escalated": False}]}}
    assert [e["kind"] for e in events.diff_events("email", old, cur)] == ["email_escalated"]


def test_job_events_only_on_transitions():
    from calllog.ops import events
    assert events.job_event("sla", "2026-10-11T09:00:00", False, True, "boom")["kind"] == "scan_failed"
    assert events.job_event("sla", "2026-10-11T09:01:00", False, False, "boom") is None
    assert events.job_event("sla", "2026-10-11T09:02:00", True, False)["kind"] == "scan_recovered"
    assert events.job_event("sla", "2026-10-11T09:03:00", True, True) is None


def test_event_store_roundtrip(ops):
    ops.add_events([{"ts": "2026-10-11T09:00:00", "text": "a"}, {"ts": "2026-10-11T10:00:00", "text": "b"},
                    {"ts": "2026-10-12T08:00:00", "text": "c"}, {"ts": "garbage", "text": "x"}])
    assert [e["text"] for e in ops.read_events("2026-10-11")] == ["b", "a"]
    assert [e["text"] for e in ops.read_events("2026-10-12")] == ["c"]
    assert ops.read_events("2026-10-13") == []
