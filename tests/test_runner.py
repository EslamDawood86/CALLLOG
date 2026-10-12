"""The collector's scheduler (calllog/ops/runner.py) and launcher (collector.py).

Fake engines are registered into ``runner.JOBS``; a fake clock and calls to ``Runner.step`` replace sleeping. Fake data only
(user 1001, password "secret-pw", example.test addresses). A few tests start real child processes (``collector.py --run-job``)
to prove the timeout / kill / crash handling of isolated runs; none of them touches Outlook, a browser or the network.
"""
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import collector
from calllog.ops import collector_config, runner, schemas
from calllog.ops import settings as hub_settings
from calllog.ops.engines import common
from calllog.ops.engines.common import FakeMailer, JobResult
from calllog.ops.store import OpsStore

PASSWORD = "secret-pw"
MONDAY_10 = datetime(2026, 10, 12, 10, 0, 0)
REPO = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self, start=MONDAY_10):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, **kw):
        self.value += timedelta(**kw)


# ------------------------------------------------------------------------------------------------ fake engines
def sla_data(breached=0):
    requests = [{"key": f"k{i}", "security_user": "user a", "sent_at": None, "age_minutes": 5, "fields": {},
                 "status": schemas.SLA_BREACHED if i < breached else schemas.SLA_WITHIN} for i in range(3)]
    return {"thresholds": {"warning_minutes": 60, "breach_minutes": 120},
            "totals": {"open": 3, "within": 3 - breached, "reminder": 0, "breached": breached, "no_date": 0, "oldest_minutes": 5},
            "by_user": [], "requests": requests}


def point(ctx, **extra):
    return {"ts": ctx.now().isoformat(timespec="seconds"), "open": 3, **extra}


SEEN = {}


def good_sla(ctx):
    SEEN["running"] = ctx.store.read_heartbeat(ctx.job).get("running")
    ctx.log("scanning")
    return JobResult(ok=True, snapshot_data=sla_data(), summary="3 open", emails_sent=1, history_point=point(ctx),
                     history_extra={"first_breach": {}})


def good_itsm(ctx):
    return JobResult(ok=True, snapshot_data={"categories": [], "roles": [], "totals": {}, "changes": {"new": [], "closed": []}},
                     summary="0 requests")


def good_report(ctx):
    return JobResult(ok=True, summary="report sent", emails_sent=3, history_extra={"final": {"business_emails": 4}})


def recording_sla(ctx):
    SEEN["ctx"] = ctx
    SEEN["options"] = dict(ctx.options)
    return JobResult(ok=True, snapshot_data=sla_data(), summary="ok")


def failing_sla(ctx):
    return JobResult(ok=False, error="portal is down", summary="SLA scan failed")


def auth_failing_sla(ctx):
    return JobResult(ok=False, error="login rejected", error_kind="auth")


def leaking_sla(ctx):
    password = ctx.password("sla", ctx.settings["username"])
    ctx.log(f"signing in with {password}")
    raise RuntimeError(f"login failed for 1001 / {password}")


def blocking_sla(ctx):
    SEEN["blocked"].set()
    SEEN["release"].wait(10)
    return JobResult(ok=True, snapshot_data=sla_data(), summary="late")


def isolated_ok(ctx):
    password = ctx.password("sla", "1001")
    ctx.log(f"signing in with {password}")
    return JobResult(ok=True, snapshot_data=sla_data(), summary=f"pid {os.getpid()} user {ctx.settings.get('username')}",
                     history_point=point(ctx))


def isolated_sleep(ctx):
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    (Path(ctx.data_dir) / "ops" / "pids.txt").write_text(f"{os.getpid()} {grandchild.pid}")
    time.sleep(120)
    return JobResult(ok=True)


def isolated_crash(ctx):
    os._exit(3)


# ------------------------------------------------------------------------------------------------ fixtures
@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("NBE_SSO_PASSWORD", raising=False)
    monkeypatch.delenv("ITSM_PASS", raising=False)
    monkeypatch.delenv(collector.SUPERVISED_FLAG, raising=False)
    SEEN.clear()
    SEEN["blocked"], SEEN["release"] = threading.Event(), threading.Event()


@pytest.fixture
def store(tmp_path):
    return OpsStore(tmp_path / "data").init()


@pytest.fixture
def clock():
    return Clock()


def write_config(tmp_path, data=None, name="collector_config.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data if data is not None else {"sla": {"username": "1001", "password": PASSWORD},
                                                              "email": {"target_archive_name": "2026_Part3"}}), encoding="utf-8")
    return collector_config.load(path)


def make_runner(store, clock, cc=None, jobs=("sla",), **kw):
    kw.setdefault("heartbeat_seconds", 0)
    kw.setdefault("poll_seconds", 0.02)
    kw.setdefault("mailer_factory", FakeMailer)
    return runner.Runner(store, cc, jobs=jobs, clock=clock, **kw)


def fresh(store):
    """A second view of the same folder (no cached files)."""
    return OpsStore(store.root.parent)


def hb(store, job="sla"):
    return fresh(store).read_heartbeat(job)


def all_text(root):
    return "\n".join(p.read_bytes().decode("utf-8", "replace") for p in Path(root).rglob("*") if p.is_file())


# ------------------------------------------------------------------------------------------ scheduling
def test_scheduled_run_writes_snapshot_history_heartbeat(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", good_sla)
    r = make_runner(store, clock)
    assert r.step("sla") == "ran"
    assert SEEN["running"] is True                               # the heartbeat said "running" while the engine worked
    snap = fresh(store).read_snapshot("sla")
    assert snap["module"] == "sla" and snap["data"]["totals"]["open"] == 3
    assert snap["generated_at"] == "2026-10-12T10:00:00" and snap["host"] and snap["date"] == "2026-10-12"
    day = fresh(store).read_history("sla", "2026-10-12")
    assert len(day["points"]) == 1 and day["points"][0]["open"] == 3 and day["first_breach"] == {}
    beat = hb(store)
    assert beat["running"] is False and beat["last_status"] == "ok" and beat["last_error"] is None
    assert beat["last_summary"] == "3 open" and beat["runs_ok"] == 1 and beat["runs_failed"] == 0
    assert beat["last_ok_at"] == "2026-10-12T10:00:00" and beat["last_run_at"] == "2026-10-12T10:00:00"
    assert beat["next_run_at"] == "2026-10-12T10:15:00" and beat["enabled"] is True and beat["state"] == "idle"
    assert beat["host"] == r.host and beat["pid"] == os.getpid() and beat["started_at"] == "2026-10-12T10:00:00"
    assert isinstance(beat["last_duration_seconds"], float) and beat["emails_sent"] == 1
    assert abs(beat["updated_ts"] - time.time()) < 60                # epoch stamp for the online check
    assert "scanning" in "\n".join(store.tail_log("sla"))


def test_interval_between_runs(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", good_sla)
    r = make_runner(store, clock)
    assert r.step("sla") == "ran"
    clock.advance(minutes=14)
    assert r.step("sla") == "idle"
    clock.advance(minutes=1)
    assert r.step("sla") == "ran"
    assert len(fresh(store).read_history("sla", "2026-10-12")["points"]) == 2
    assert hb(store)["runs_ok"] == 2


def test_not_scheduled_outside_hours_weekend_or_holiday(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", good_sla)
    r = make_runner(store, clock)
    clock.value = datetime(2026, 10, 12, 20, 0)                 # Monday evening
    assert r.step("sla") == "idle"
    assert hb(store)["state"] == "outside_hours" and hb(store)["next_run_at"] == "2026-10-13T08:00:00"
    clock.value = datetime(2026, 10, 16, 10, 0)                 # Friday = weekend
    assert r.step("sla") == "idle"
    assert hb(store)["next_run_at"] == "2026-10-18T08:00:00"    # Sunday
    clock.value = datetime(2026, 10, 14, 10, 0)                 # Wednesday, but a holiday
    hub_settings.patch(store, {"general": {"holidays": ["2026-10-14"]}})
    assert r.step("sla") == "idle"
    clock.value = datetime(2026, 10, 15, 10, 0)
    assert r.step("sla") == "ran"
    assert fresh(store).read_snapshot("sla") is not None


def test_run_now_ignores_the_schedule_and_passes_options(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", recording_sla)
    r = make_runner(store, clock)
    clock.value = datetime(2026, 10, 16, 23, 0)                 # Friday night: nothing is scheduled
    assert r.step("sla") == "idle"
    store.write_json(store.requests_dir / "sla-20261016230000-aaaaaa.json",
                     {"job": "sla", "requested_by": "admin", "requested_at": "2026-10-16T23:00:00", "send_mail": True, "force": True})
    assert r.step("sla") == "ran"
    assert SEEN["options"]["requested_by"] == "admin" and SEEN["options"]["send_mail"] is True and SEEN["options"]["force"] is True
    assert SEEN["options"]["trigger"] == "run-now"
    assert store.pending_run_requests("sla") == []                  # consumed
    assert r.step("sla") == "idle"
    store.add_run_request("sla", requested_by="boss")
    assert r.step("sla") == "ran" and SEEN["options"]["requested_by"] == "boss" and not SEEN["options"]["send_mail"]


def test_settings_changes_apply_without_restart(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", good_sla)
    r = make_runner(store, clock)
    hub_settings.patch(store, {"sla": {"enabled": False}})
    assert r.step("sla") == "idle"
    assert hb(store)["enabled"] is False and hb(store)["state"] == "disabled" and hb(store)["next_run_at"] is None
    hub_settings.patch(store, {"sla": {"enabled": True, "interval_minutes": 5}})
    assert r.step("sla") == "ran"
    clock.advance(minutes=5)
    assert r.step("sla") == "ran"


def test_idle_heartbeat_is_throttled(store, clock, monkeypatch):
    r = make_runner(store, clock, heartbeat_seconds=30)
    clock.value = datetime(2026, 10, 12, 22, 0)
    r.step("sla")
    first = hb(store)["updated_ts"]
    time.sleep(0.05)
    r.step("sla")
    assert hb(store)["updated_ts"] == first


# ---------------------------------------------------------------------------------- locks and threads
def test_one_run_at_a_time_across_two_runners(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", blocking_sla)
    first = make_runner(store, clock)
    second = make_runner(fresh(store), clock)                    # another collector PC: its own store object
    store.add_run_request("sla", requested_by="one")
    worker = threading.Thread(target=first.step, args=("sla",))
    worker.start()
    assert SEEN["blocked"].wait(10)
    store.add_run_request("sla", requested_by="two")
    assert second.step("sla") == "busy"
    assert len(fresh(store).pending_run_requests("sla")) == 1       # the busy collector did not swallow the request
    SEEN["release"].set()
    worker.join(10)
    assert not worker.is_alive()
    assert hb(store)["runs_ok"] == 1


def test_a_slow_job_does_not_delay_the_others(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", blocking_sla)
    monkeypatch.setitem(runner.JOBS, "itsm", good_itsm)
    r = make_runner(store, clock, jobs=("sla", "itsm"))
    r.start()
    try:
        assert SEEN["blocked"].wait(10)
        deadline = time.time() + 10
        while time.time() < deadline and (hb(store, "itsm") or {}).get("last_status") != "ok":
            time.sleep(0.05)
        assert hb(store, "itsm")["last_status"] == "ok"
        assert hb(store, "sla")["running"] is True and hb(store, "sla").get("last_status") is None
    finally:
        SEEN["release"].set()
        r.stop()
    assert hb(store, "sla")["last_status"] == "ok"
    assert all(hb(store, j)["running"] is False and hb(store, j)["state"] == "stopped" for j in ("sla", "itsm"))
    assert not any(t.is_alive() for t in r._threads)


# -------------------------------------------------------------------------- results, failures, events
def test_failed_scan_never_overwrites_the_snapshot(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", good_sla)
    r = make_runner(store, clock)
    r.step("sla")
    good = fresh(store).read_snapshot("sla")
    monkeypatch.setitem(runner.JOBS, "sla", failing_sla)
    clock.advance(minutes=15)
    assert r.step("sla") == "ran"
    assert fresh(store).read_snapshot("sla") == good
    assert len(fresh(store).read_history("sla", "2026-10-12")["points"]) == 1
    beat = hb(store)
    assert beat["last_status"] == "error" and beat["last_error"] == "portal is down" and beat["runs_failed"] == 1
    assert beat["last_ok_at"] == "2026-10-12T10:00:00" and beat["runs_ok"] == 1
    monkeypatch.setitem(runner.JOBS, "sla", good_sla)
    clock.advance(minutes=15)
    r.step("sla")
    kinds = [e["kind"] for e in fresh(store).read_events("2026-10-12")]
    assert kinds.count("scan_failed") == 1 and kinds.count("scan_recovered") == 1
    assert hb(store)["last_error"] is None and hb(store)["consecutive_failures"] == 0


def test_timeline_events_come_from_the_snapshot_difference(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", lambda ctx: JobResult(ok=True, snapshot_data=sla_data(0), summary="a"))
    r = make_runner(store, clock)
    r.step("sla")
    assert fresh(store).read_events("2026-10-12") == []              # nothing to compare with yet
    monkeypatch.setitem(runner.JOBS, "sla", lambda ctx: JobResult(ok=True, snapshot_data=sla_data(2), summary="b"))
    clock.advance(minutes=15)
    r.step("sla")
    events = fresh(store).read_events("2026-10-12")
    assert [e["kind"] for e in events] == ["sla_breached"] and events[0]["count"] == 2


def test_report_job_writes_history_extra_but_no_snapshot(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "email_report", good_report)
    clock.value = datetime(2026, 10, 12, 16, 31)
    r = make_runner(store, clock, jobs=("email_report",))
    assert r.step("email_report") == "ran"
    assert fresh(store).read_snapshot("email") is None
    assert fresh(store).read_history("email", "2026-10-12")["final"] == {"business_emails": 4}
    assert hb(store, "email_report")["emails_sent"] == 3


def test_exception_in_a_job_is_isolated_and_the_password_never_leaks(store, clock, monkeypatch, tmp_path):
    cc = write_config(tmp_path)
    monkeypatch.setitem(runner.JOBS, "sla", leaking_sla)
    monkeypatch.setitem(runner.JOBS, "itsm", good_itsm)
    r = make_runner(store, clock, cc, jobs=("sla", "itsm"))
    assert r.step("sla") == "ran"
    beat = hb(store)
    assert beat["last_status"] == "error" and "RuntimeError" in beat["last_error"]
    log = "\n".join(store.tail_log("sla", 500))
    assert "Traceback" in log and "RuntimeError" in log and "***" in log
    assert r.step("itsm") == "ran" and hb(store, "itsm")["last_status"] == "ok"      # the other job is unaffected
    assert PASSWORD not in all_text(store.root.parent)
    assert fresh(store).read_snapshot("sla") is None


def test_pinned_keys_win_and_secrets_stay_out_of_every_file(store, clock, monkeypatch, tmp_path):
    cc = write_config(tmp_path)
    hub_settings.patch(store, {"sla": {"username": "from-web-page"}, "email": {"target_archive_name": "old"}})
    monkeypatch.setitem(runner.JOBS, "sla", recording_sla)
    r = make_runner(store, clock, cc)
    assert r.step("sla") == "ran"
    ctx = SEEN["ctx"]
    assert ctx.settings["username"] == "1001" and ctx.password("sla", "1001") == PASSWORD
    assert "password" not in ctx.settings
    saved = json.loads((store.root / "settings.json").read_text(encoding="utf-8"))
    assert saved["sla"]["username"] == "from-web-page" and "password" not in saved["sla"]     # the runner never writes settings
    info = fresh(store).read_state("collector_info")
    assert info["accounts"]["sla"] == {"username": "1001", "has_password": True, "password_source": "file"}
    assert info["accounts"]["itsm"]["has_password"] is False
    assert {"sla.username", "email.target_archive_name"} <= set(info["pinned_keys"])
    assert info["host"] == r.host and info["pid"] == os.getpid() and info["config_path"] == str(cc.path)
    assert PASSWORD not in all_text(store.root.parent)


def test_config_file_is_reloaded_when_it_changes(store, clock, monkeypatch, tmp_path):
    cc = write_config(tmp_path)
    monkeypatch.setitem(runner.JOBS, "sla", recording_sla)
    r = make_runner(store, clock, cc)
    r.step("sla")
    assert fresh(store).read_state("collector_info")["accounts"]["sla"]["username"] == "1001"
    write_config(tmp_path, {"sla": {"username": "2002", "password": "other-pw", "warning_minutes": 45}})
    os.utime(cc.path, (time.time() + 5, time.time() + 5))
    clock.advance(minutes=15)
    r.step("sla")
    assert SEEN["ctx"].settings["username"] == "2002" and SEEN["ctx"].settings["warning_minutes"] == 45
    info = fresh(store).read_state("collector_info")
    assert info["accounts"]["sla"]["username"] == "2002" and "sla.warning_minutes" in info["pinned_keys"]
    assert "other-pw" not in all_text(store.root.parent)
    cc.path.write_text("{ broken", encoding="utf-8")                # a broken file keeps the last good values
    os.utime(cc.path, (time.time() + 10, time.time() + 10))
    clock.advance(minutes=15)
    assert r.step("sla") == "ran" and SEEN["ctx"].settings["username"] == "2002"
    assert "collector_config.json problem" in "\n".join(store.tail_log("sla", 5)) or \
           "collector_config.json problem" in (store.logs_dir / "collector.log").read_text(encoding="utf-8")


def test_job_context_contents(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", recording_sla)
    r = make_runner(store, clock, no_email=True, debug=True, headed=True)
    r.step("sla")
    ctx = SEEN["ctx"]
    assert ctx.job == "sla" and ctx.settings == r.reload()["sla"] and ctx.general == r.reload()["general"]
    assert ctx.now() == MONDAY_10 and ctx.store is store and ctx.data_dir == str(store.root.parent)
    assert isinstance(ctx.mailer, FakeMailer) and ctx.can_email() is False
    assert ctx.debug and ctx.headed and not ctx.dry_run and ctx.host == r.host


def test_email_report_uses_the_email_settings_section(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "email_report", recording_sla)
    clock.value = datetime(2026, 10, 12, 16, 40)
    r = make_runner(store, clock, jobs=("email_report",))
    r.step("email_report")
    assert "report" in SEEN["ctx"].settings and SEEN["ctx"].job == "email_report"


def test_dry_run_writes_and_sends_nothing(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", recording_sla)
    r = make_runner(store, clock, dry_run=True, mailer_factory=None)
    before = all_text(store.root)
    assert r.step("sla") == "ran"
    ctx = SEEN["ctx"]
    assert ctx.dry_run and isinstance(ctx.mailer, FakeMailer) and ctx.can_email() is False
    assert store.read_snapshot("sla") is None and store.read_heartbeat("sla") is None and store.tail_log("sla") == []
    assert store.read_history("sla", "2026-10-12") is None and all_text(store.root) == before


def test_missing_engine_fails_cleanly(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "email_report", ("calllog.ops.engines.not_written_yet", "run_report"))
    clock.value = datetime(2026, 10, 12, 16, 40)
    r = make_runner(store, clock, jobs=("email_report",))
    assert r.step("email_report") == "ran"
    beat = hb(store, "email_report")
    assert beat["last_status"] == "error" and "not available" in beat["last_error"]
    assert beat["next_run_at"] and beat["running"] is False


def test_real_registry_points_at_real_functions():
    for job, (module, name) in runner.JOBS.items():
        try:
            assert callable(runner.resolve_job(job))
        except ModuleNotFoundError as exc:
            assert job == "email_report" and exc.name == module        # that engine may still be missing


# -------------------------------------------------------------------------- report slots
def test_report_slot_rules():
    s, _ = hub_settings.validate({"email": {"report": {"send_times": ["16:30", "09:00"]}}})
    due = lambda hour, minute, done=(), day=12: runner.report_slots_due(s, datetime(2026, 10, day, hour, minute), set(done))
    assert due(16, 29) == []                              # 09:00 passed 7.5 hours ago: too late; 16:30 is not yet
    assert due(9, 0) == ["09:00"] and due(11, 59) == ["09:00"] and due(12, 1) == []
    assert due(16, 30) == ["16:30"] and due(19, 29) == ["16:30"] and due(19, 31) == []       # 3 hour catch-up
    assert due(16, 31, done=["16:30"]) == []                                                  # once per slot and day
    assert due(16, 31, day=16) == []                                                          # Friday
    assert runner.report_slots_due(hub_settings.validate({"email": {"report": {"send": False}}})[0],
                                   datetime(2026, 10, 12, 16, 31), set()) == []


def test_report_job_sends_once_per_slot_and_catches_up(store, clock, monkeypatch):
    calls = []
    monkeypatch.setitem(runner.JOBS, "email_report", lambda ctx: calls.append(ctx.now()) or JobResult(ok=True, summary="sent"))
    r = make_runner(store, clock, jobs=("email_report",))
    clock.value = datetime(2026, 10, 12, 16, 29)
    assert r.step("email_report") == "idle" and calls == []
    assert hb(store, "email_report")["next_run_at"] == "2026-10-12T16:30:00"
    clock.value = datetime(2026, 10, 12, 16, 30)
    assert r.step("email_report") == "ran" and len(calls) == 1
    clock.advance(minutes=5)
    assert r.step("email_report") == "idle" and len(calls) == 1
    assert hb(store, "email_report")["next_run_at"] == "2026-10-13T16:30:00"
    other = make_runner(fresh(store), clock, jobs=("email_report",))        # a second collector PC sees the same slot as done
    assert other.step("email_report") == "idle" and len(calls) == 1
    # a collector that was busy / off at 16:30 sends when it comes back within 3 hours, not later
    late = OpsStore(store.root.parent.parent / "other-data").init()
    clock.value = datetime(2026, 10, 12, 18, 45)
    assert make_runner(late, clock, jobs=("email_report",)).step("email_report") == "ran" and len(calls) == 2
    later = OpsStore(store.root.parent.parent / "third-data").init()
    clock.value = datetime(2026, 10, 12, 19, 45)
    assert make_runner(later, clock, jobs=("email_report",)).step("email_report") == "idle" and len(calls) == 2


def test_failed_report_is_retried_inside_the_window(store, clock, monkeypatch):
    results = [failing_sla, good_report]
    monkeypatch.setitem(runner.JOBS, "email_report", lambda ctx: results.pop(0)(ctx))
    r = make_runner(store, clock, jobs=("email_report",))
    clock.value = datetime(2026, 10, 12, 16, 30)
    assert r.step("email_report") == "ran" and hb(store, "email_report")["last_status"] == "error"
    clock.advance(minutes=5)
    assert r.step("email_report") == "idle"                                  # backing off
    clock.advance(minutes=6)
    assert r.step("email_report") == "ran" and hb(store, "email_report")["last_status"] == "ok"
    clock.advance(minutes=30)
    assert r.step("email_report") == "idle"


def test_a_skipped_run_is_reported_but_changes_nothing_else(store, clock, monkeypatch):
    outcomes = [JobResult(ok=False, error="down"), JobResult(ok=True, skipped=True, summary="already sent today"),
                JobResult(ok=True, summary="sent")]
    monkeypatch.setitem(runner.JOBS, "email_report", lambda ctx: outcomes.pop(0))
    hub_settings.patch(store, {"email": {"report": {"send_times": ["16:30", "16:40", "16:50"]}}})
    r = make_runner(store, clock, jobs=("email_report",))
    clock.value = datetime(2026, 10, 12, 16, 31)
    r.step("email_report")                                                   # fails, all due slots stay open
    clock.advance(minutes=20)
    assert r.step("email_report") == "ran"
    beat = hb(store, "email_report")
    assert beat["last_status"] == "skipped" and beat["last_summary"] == "already sent today"
    assert beat["runs_ok"] == 0 and beat["runs_failed"] == 1 and beat["last_error"] == "down"
    assert [e["kind"] for e in fresh(store).read_events("2026-10-12")] == ["scan_failed"]
    assert fresh(store).read_state("runner-email_report")["done"] == ["16:30", "16:40", "16:50"]   # one run covers every due slot


def test_next_active_start():
    s, _ = hub_settings.validate({"general": {"holidays": ["2026-10-13"]}})
    assert runner.next_active_start(s, "sla", datetime(2026, 10, 12, 10, 0)) == datetime(2026, 10, 12, 10, 0)
    assert runner.next_active_start(s, "sla", datetime(2026, 10, 12, 18, 0)) == datetime(2026, 10, 14, 8, 0)     # Tue is a holiday
    assert runner.next_active_start(s, "sla", datetime(2026, 10, 12, 6, 0)) == datetime(2026, 10, 12, 8, 0)
    all_day, _ = hub_settings.validate({"general": {"active_hours": "", "work_days": []}})
    assert runner.next_active_start(all_day, "sla", datetime(2026, 10, 16, 3, 0)) == datetime(2026, 10, 16, 3, 0)


# --------------------------------------------------------------------- 24/7 rules: pause, back-off
def test_failures_back_off_and_success_resets(store, clock, monkeypatch):
    monkeypatch.setitem(runner.JOBS, "sla", failing_sla)
    r = make_runner(store, clock)
    expected = [("2026-10-12T10:15:00", 1), ("2026-10-12T10:45:00", 2), ("2026-10-12T11:15:00", 2)]
    for next_run, multiplier in expected:
        while r.step("sla") != "ran":
            clock.advance(minutes=1)
        beat = hb(store)
        assert beat["next_run_at"] == next_run and beat["retry_multiplier"] == multiplier, (clock.value, beat)
        clock.value = datetime.fromisoformat(next_run)
    assert hb(store)["consecutive_failures"] == 3
    monkeypatch.setitem(runner.JOBS, "sla", good_sla)
    assert r.step("sla") == "ran"
    assert hb(store)["consecutive_failures"] == 0 and hb(store)["retry_multiplier"] == 1


def test_rejected_login_pauses_until_the_credentials_change(store, clock, monkeypatch, tmp_path):
    cc = write_config(tmp_path)
    monkeypatch.setitem(runner.JOBS, "sla", auth_failing_sla)
    r = make_runner(store, clock, cc)
    assert r.step("sla") == "ran"
    beat = hb(store)
    assert beat["paused"] == "auth" and beat["state"] == "paused_auth" and beat["next_run_at"] is None
    assert "retries paused" in beat["paused_reason"]
    for _ in range(3):
        clock.advance(hours=1)
        assert r.step("sla") == "idle"                                         # never hammers the login
    clock.advance(minutes=1)
    store.add_run_request("sla", requested_by="admin")
    assert r.step("sla") == "ran"                                              # "Run now" tries once
    monkeypatch.setitem(runner.JOBS, "sla", good_sla)
    clock.advance(hours=1)
    assert r.step("sla") == "idle"
    write_config(tmp_path, {"sla": {"username": "1001", "password": "fixed-pw"}})            # somebody fixes the password
    os.utime(cc.path, (time.time() + 5, time.time() + 5))
    assert r.step("sla") == "ran"
    assert hb(store)["paused"] is None and hb(store)["last_status"] == "ok"


def test_pause_survives_a_restart_of_the_collector(store, clock, monkeypatch, tmp_path):
    cc = write_config(tmp_path)
    monkeypatch.setitem(runner.JOBS, "sla", auth_failing_sla)
    make_runner(store, clock, cc).step("sla")
    monkeypatch.setitem(runner.JOBS, "sla", good_sla)
    restarted = make_runner(fresh(store), clock, collector_config.load(cc.path))           # a new process, same files
    clock.advance(hours=1)
    assert restarted.step("sla") == "idle"                                                  # still paused: nothing was fixed
    write_config(tmp_path, {"sla": {"username": "1001", "password": "fixed-pw"}})
    os.utime(cc.path, (time.time() + 60, time.time() + 60))
    assert restarted.step("sla") == "ran" and hb(store)["paused"] is None


# ----------------------------------------------------------------------------------- housekeeping
def test_housekeeping_removes_only_what_is_old(store, clock):
    day = lambda n: (clock.value - timedelta(days=n)).date().isoformat()
    for n in range(1, 66):
        store.write_json(store.history_dir / "sla" / f"{day(n)}.json", {"points": []})
    for n in range(1, 36):
        store.write_json(store.events_dir / f"{day(n)}.jsonl", {})
    old = time.time() - 100 * 86400
    reports = store.report_dir("sla")
    for i in range(35):
        path = reports / f"SLA_Report_202608{i:02d}_1000.xlsx"
        path.write_bytes(b"x")
        os.utime(path, (time.time() - i * 3600, time.time() - i * 3600))
    for name in ("SLA_latest.xlsx", "ancient.xlsx"):
        (reports / name).write_bytes(b"x")
        os.utime(reports / name, (old, old))
    debug = store.root / "debug" / "sla"
    debug.mkdir(parents=True)
    for name in ("old.html", "new.html"):
        (debug / name).write_text("x")
    os.utime(debug / "old.html", (old, old))
    stale = store.requests_dir / "sla-20260101000000-abcdef.json"
    store.write_json(stale, {"job": "sla"})
    os.utime(stale, (old, old))
    store.add_run_request("sla")
    r = make_runner(store, clock)
    removed = r.housekeeping(clock.value)
    assert store.history_days("sla") == [day(n) for n in range(60, 0, -1)] and removed["history"] == 5
    assert len(list(store.events_dir.glob("*.jsonl"))) == 30 and removed["events"] == 5
    assert not (reports / "ancient.xlsx").exists() and (reports / "SLA_latest.xlsx").exists()
    assert len(list(reports.glob("SLA_Report_*.xlsx"))) == 30 and removed["reports"] == 6          # 5 over the cap + ancient
    assert not (debug / "old.html").exists() and (debug / "new.html").exists()
    assert len(store.pending_run_requests()) == 1 and removed["run requests"] == 1
    assert "Housekeeping" in (store.logs_dir / "collector.log").read_text(encoding="utf-8")


def test_housekeeping_runs_once_a_day_outside_business_hours(store, clock):
    r = make_runner(store, clock)
    assert r.housekeeping_if_due(datetime(2026, 10, 12, 10, 0)) is None            # in business hours
    first = r.housekeeping_if_due(datetime(2026, 10, 12, 22, 0))
    assert isinstance(first, dict)
    assert r.housekeeping_if_due(datetime(2026, 10, 12, 23, 0)) is None            # already done today
    assert isinstance(r.housekeeping_if_due(datetime(2026, 10, 13, 22, 0)), dict)


# ---------------------------------------------------------------------------- isolated child processes
def isolated_runner(store, clock, tmp_path, monkeypatch, name, **kw):
    cc = write_config(tmp_path)
    monkeypatch.setitem(runner.JOBS, "sla", ("tests.test_runner", name))
    return make_runner(store, clock, cc, isolate=True, script=REPO / "collector.py", **kw), cc


def test_isolated_run_roundtrip(store, clock, monkeypatch, tmp_path):
    r, cc = isolated_runner(store, clock, tmp_path, monkeypatch, "isolated_ok")
    assert r.step("sla") == "ran"
    beat = hb(store)
    assert beat["last_status"] == "ok", beat
    assert f"pid {os.getpid()}" not in beat["last_summary"] and "user 1001" in beat["last_summary"]      # ran in another process
    assert fresh(store).read_snapshot("sla")["data"]["totals"]["open"] == 3
    assert len(fresh(store).read_history("sla", "2026-10-12")["points"]) == 1
    log = "\n".join(store.tail_log("sla", 50))
    assert "signing in with ***" in log and PASSWORD not in all_text(store.root.parent)


def test_isolated_run_is_killed_on_timeout(store, clock, monkeypatch, tmp_path):
    r, _ = isolated_runner(store, clock, tmp_path, monkeypatch, "isolated_sleep", timeouts={"sla": 3})
    started = time.monotonic()
    assert r.step("sla") == "ran"
    assert time.monotonic() - started < 40
    beat = hb(store)
    assert beat["last_status"] == "error" and "Timed out" in beat["last_error"] and beat["running"] is False
    assert fresh(store).read_snapshot("sla") is None


def start_sleeping_job(r, store, job):
    """Run ``job`` (a child process that starts a grandchild and sleeps) in a thread; returns (thread, child pid, grandchild pid)."""
    worker = threading.Thread(target=r.step, args=(job,))
    worker.start()
    pids_file = store.root / "pids.txt"
    deadline = time.time() + 40
    while time.time() < deadline and not pids_file.exists():
        time.sleep(0.1)
    assert pids_file.exists(), "the job process never started"
    time.sleep(0.3)
    return (worker, *(int(x) for x in pids_file.read_text().split()))


def test_stopping_kills_the_job_process_tree(store, clock, monkeypatch, tmp_path):
    r, _ = isolated_runner(store, clock, tmp_path, monkeypatch, "isolated_sleep")
    worker, child, grandchild = start_sleeping_job(r, store, "sla")
    r.request_stop()
    worker.join(30)
    assert not worker.is_alive()
    assert hb(store)["state"] == "stopped" and hb(store)["running"] is False and hb(store).get("last_status") is None
    time.sleep(0.5)
    assert_gone(child)
    assert_gone(grandchild)


def test_a_stopped_report_keeps_its_slot(store, clock, monkeypatch, tmp_path):
    cc = write_config(tmp_path)
    monkeypatch.setitem(runner.JOBS, "email_report", ("tests.test_runner", "isolated_sleep"))
    clock.value = datetime(2026, 10, 12, 16, 35)
    r = make_runner(store, clock, cc, jobs=("email_report",), isolate=True, script=REPO / "collector.py")
    worker, _, _ = start_sleeping_job(r, store, "email_report")
    r.request_stop()
    worker.join(30)
    assert fresh(store).read_state("runner-email_report") == {}                  # the slot is still open for the next start
    assert hb(store, "email_report")["state"] == "stopped"


def assert_gone(pid):
    """The process no longer runs (a zombie waiting to be collected by its parent counts as gone)."""
    try:
        if Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] == "Z":
            return
    except OSError:
        pass
    with pytest.raises(OSError):
        os.kill(pid, 0)


def test_a_crashing_job_process_becomes_a_failed_result(store, clock, monkeypatch, tmp_path):
    r, _ = isolated_runner(store, clock, tmp_path, monkeypatch, "isolated_crash")
    assert r.step("sla") == "ran"
    assert "exit code 3" in hb(store)["last_error"]


# --------------------------------------------------------------------------------------- collector.py
def run_collector(tmp_path, *args, config=None, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in ("NBE_SSO_PASSWORD", "ITSM_PASS")}
    env.update(CALLLOG_DATA_DIR=str(tmp_path / "data"), CALLLOG_COLLECTOR_CONFIG=str(config or tmp_path / "missing.json"),
               CALLLOG_CONFIG=str(tmp_path / "no-config.json"), **(extra_env or {}))
    return subprocess.run([sys.executable, str(REPO / "collector.py"), *args], capture_output=True, text=True, env=env, cwd=str(REPO),
                          timeout=120, stdin=subprocess.DEVNULL)


def test_check_reports_without_printing_a_password(tmp_path):
    (tmp_path / "data").mkdir()
    cfg = tmp_path / "collector_config.json"
    cfg.write_text(json.dumps({"sla": {"username": "1001", "password": PASSWORD}, "itsm": {"username": "1002"},
                               "email": {"target_archive_name": "2026_Part3"}}), encoding="utf-8")
    done = run_collector(tmp_path, "--check", config=cfg)
    out = done.stdout + done.stderr
    assert PASSWORD not in out
    assert "found:" in out and "2026_Part3" in out and "1001" in out and "yes (file)" in out
    assert "user: 1002   password: NO" in out and "--set-password itsm" in out
    assert "writable: yes" in out
    assert done.returncode == 1                                      # the ITSM password is missing


def test_check_without_a_config_file_says_what_to_do(tmp_path):
    done = run_collector(tmp_path, "--check")
    assert "NOT FOUND" in done.stdout and "collector_config.example.json" in done.stdout
    assert "user: NOT SET" in done.stdout and done.returncode == 1


def test_check_all_good_returns_zero(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    cfg = tmp_path / "collector_config.json"
    cfg.write_text(json.dumps({"sla": {"username": "1001", "password": PASSWORD}, "itsm": {"username": "1002", "password": "itsm-secret-2"},
                               "email": {"business_emails": ["mail@example.test"], "team_members": ["alice@example.test"]}}), encoding="utf-8")
    done = run_collector(tmp_path, "--check", config=cfg)
    out = done.stdout
    assert PASSWORD not in out and "itsm-secret-2" not in out
    assert "1 business mailbox" in out and out.count("password: yes (file)") == 2
    assert "Everything needed is in place." in out and done.returncode == 0
    assert "mail@example.test" not in out and "alice@example.test" not in out            # counts only, no addresses


def test_set_password_asks_twice_and_stores_it(tmp_path, monkeypatch, capsys):
    cfg = write_config(tmp_path, {"sla": {"username": "1001"}})
    saved = []
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(collector.getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(common, "set_password", lambda kind, user, pw: saved.append((kind, user, pw)))
    monkeypatch.setattr(collector, "missing_packages", lambda: [])
    code = collector.main(["--set-password", "sla", "--config", str(cfg.path), "--data-dir", str(tmp_path / "data")])
    assert code == 0 and saved == [("sla", "1001", PASSWORD)]
    assert PASSWORD not in capsys.readouterr().out
    answers = iter([PASSWORD, "different"])
    assert collector.main(["--set-password", "sla", "--config", str(cfg.path), "--data-dir", str(tmp_path / "data")]) == 1
    assert len(saved) == 1


def test_set_password_needs_a_user_name(tmp_path, monkeypatch, capsys):
    cfg = write_config(tmp_path, {})
    monkeypatch.setattr(collector, "missing_packages", lambda: [])
    assert collector.main(["--set-password", "itsm", "--config", str(cfg.path), "--data-dir", str(tmp_path / "data")]) == 1
    assert "No user name" in capsys.readouterr().out


def test_once_prints_the_result_and_sets_the_exit_code(tmp_path, monkeypatch, capsys):
    cfg = write_config(tmp_path)
    monkeypatch.setattr(collector, "missing_packages", lambda: [])
    base = ["--once", "sla", "--config", str(cfg.path), "--data-dir", str(tmp_path / "data")]
    monkeypatch.setitem(runner.JOBS, "sla", good_sla)
    assert collector.main(base + ["--no-email"]) == 0
    out = capsys.readouterr().out
    assert "sla: OK - 3 open" in out and "snapshot written" in out
    assert OpsStore(tmp_path / "data").read_snapshot("sla") is not None
    monkeypatch.setitem(runner.JOBS, "sla", failing_sla)
    assert collector.main(base) == 1
    out = capsys.readouterr().out
    assert "sla: FAILED" in out and "portal is down" in out
    monkeypatch.setitem(runner.JOBS, "sla", recording_sla)
    assert collector.main(base + ["--force", "--send-mail", "--dry-run", "--debug"]) == 0
    assert SEEN["options"]["force"] and SEEN["options"]["send_mail"] and SEEN["ctx"].dry_run and SEEN["ctx"].debug
    assert "dry run" in capsys.readouterr().out


def test_once_reports_a_job_that_is_running_elsewhere(tmp_path, monkeypatch, capsys):
    cfg = write_config(tmp_path)
    monkeypatch.setattr(collector, "missing_packages", lambda: [])
    other = OpsStore(tmp_path / "data").init()
    monkeypatch.setitem(runner.JOBS, "sla", good_sla)
    with other.try_lock("job-sla") as got:
        assert got
        code = collector.main(["--once", "sla", "--config", str(cfg.path), "--data-dir", str(tmp_path / "data")])
    assert code == 1 and "already running" in capsys.readouterr().out


def test_command_line_parsing():
    assert collector.JOB_NAMES == schemas.JOBS
    args = collector.parse_args(["--jobs", "sla, itsm", "--config", "x.json", "--no-email"])
    assert args.job_list == ["sla", "itsm"]
    with pytest.raises(SystemExit):
        collector.parse_args(["--jobs", "sla,nope"])
    with pytest.raises(SystemExit):
        collector.parse_args(["--once", "nope"])
    assert collector.parse_args([]).job_list == list(schemas.JOBS)
    passed = collector.passthrough(collector.parse_args(["--jobs", "sla", "--config", "x.json", "--debug", "--once", "sla"]))
    assert "--jobs" in passed and "--config" in passed and "--debug" in passed and "--once" not in passed
    arguments = collector.service_arguments(collector.parse_args(["--jobs", "sla"]))
    assert "--service" in arguments and "collector.py" in arguments and "--jobs sla" in arguments


def test_a_second_collector_on_the_same_pc_is_refused(tmp_path, monkeypatch, capsys):
    cfg = write_config(tmp_path)
    monkeypatch.setattr(collector, "missing_packages", lambda: [])
    monkeypatch.setattr(collector, "SINGLE_INSTANCE_PORT", 47999)
    assert collector.claim_instance() is True
    try:
        assert collector.monitor_running() is True
        assert collector.main(["--config", str(cfg.path), "--data-dir", str(tmp_path / "data")]) == 0
        assert "already running" in capsys.readouterr().out
    finally:
        collector._instance_socket.close()
    assert collector.monitor_running() is False


def test_missing_packages_are_listed_not_crashed_on(monkeypatch, capsys):
    monkeypatch.setattr(collector, "missing_packages", lambda: ["keyring"])
    monkeypatch.setenv(collector.RESTART_FLAG, "1")
    assert collector.ensure_packages() == 1
    assert "still missing" in capsys.readouterr().out
    monkeypatch.setattr(collector, "missing_packages", lambda: [])
    assert collector.ensure_packages() is None


def test_requirements_file_lists_what_the_collector_needs():
    text = (REPO / "requirements-collector.txt").read_text(encoding="utf-8")
    for name in ("pywin32", "playwright", "keyring", "openpyxl", "Flask"):
        assert name in text
    assert "matplotlib" in text and "pandas" not in text.lower().replace("no pandas", "")
