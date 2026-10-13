"""The collector's scheduler: runs the four jobs (email, email_report, sla, itsm) and feeds the shared folder.

``collector.py`` builds a :class:`Runner` and starts it (docs/HUB-DESIGN.md, sections 6, 10, 12 and 15). One worker thread per
job, so a long Outlook scan never delays the SLA check. What the runner owns, so that the engines stay simple:

* **when** a job runs: every ``interval_minutes`` while ``settings.is_active`` (work days, active hours, holidays); the report job
  at its ``email.report.send_times`` (once per slot and day, with a 3 hour catch-up); "Run now" requests from the web app at once;
* **one run at a time** per job, even with two collector PCs (``store.try_lock("job-<job>")`` is held for the whole run), and one
  Outlook conversation at a time inside this process;
* **settings** are re-read on every cycle (a change on the System page applies without a restart); ``collector_config.json`` is
  re-read when its modification time changes and its pinned keys win over the saved settings;
* **writing the result**: snapshot only for good scans, history point, timeline events (``events.diff_events`` against the previous
  snapshot, ``events.job_event`` when a scan starts failing or recovers) and the heartbeat;
* **24/7 safety**: with ``isolate=True`` every run is a child process (``collector.py --run-job``) with a hard timeout and a kill
  of its whole process tree; a rejected login (``error_kind == "auth"``) pauses the scheduled runs of that job until the credentials
  change or somebody presses "Run now"; repeated failures back off (1x, 2x, 4x ... capped at 30 min); housekeeping once a day.

Passwords: the runner never reads a password itself. Engines ask ``ctx.password(kind, username)``, which goes to
``collector_config.get_password``; every password that passes through is remembered and masked in every log line, error text and
summary the runner stores. The runner also never writes a password into a heartbeat, snapshot or ``collector_info.json``.

Only the standard library is needed to import this module (Outlook, Chrome, keyring ... are loaded by the engines).
"""
import contextlib
import dataclasses
import hashlib
import importlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from . import collector_config, events, mailbox, notifications, schemas
from . import settings as hub_settings
from .engines import common
from .store import HISTORY_KEEP_DAYS, LOG_ROTATE_BYTES, OpsStore, now_iso

JOBS = {                       # job -> (module, function); a plain callable is accepted too (tests)
    "email": ("calllog.ops.engines.email_engine", "run_scan"),
    "email_report": ("calllog.ops.engines.email_report", "run_report"),
    "sla": ("calllog.ops.engines.sla_engine", "run"),
    "itsm": ("calllog.ops.engines.itsm_engine", "run"),
}
OUTLOOK_JOBS = ("email", "email_report")
DEFAULT_TIMEOUT_MINUTES = {"email": 20, "email_report": 30, "sla": 5, "itsm": 10}
MAX_TIMEOUT_MINUTES = 240

POLL_SECONDS = 5               # how often a worker looks for work (and for "Run now" requests)
HEARTBEAT_SECONDS = 30
CATCH_UP = timedelta(hours=3)  # a report slot that passed less than this long ago is still sent
MIN_GAP = timedelta(minutes=1)
BACKOFF_CAP_MINUTES = 30
REPORT_RETRY_MINUTES = 10
REPORT_STATE = "runner-email_report"
HOUSEKEEPING_STATE = "runner-housekeeping"
ACCOUNT_KINDS = collector_config.ACCOUNT_KINDS
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
REPO_DIR = Path(__file__).resolve().parents[2]

REPORT_KEEP_DAYS = 60
REPORT_KEEP_COPIES = 30
DEBUG_KEEP_DAYS = 7
REQUEST_KEEP_DAYS = 1
EVENT_KEEP_DAYS = 30
NOTIFICATION_KEEP_DAYS = 30


def app_version():
    try:
        from . import health
        return health.APP_VERSION
    except Exception:
        return ""


# ------------------------------------------------------------------------------------------------ secrets
class Scrubber:
    """Masks every password this process has seen. Call it with any text before it is stored."""

    def __init__(self):
        self._secrets = set()

    def add(self, secret):
        secret = str(secret or "")
        if len(secret) >= 3:
            self._secrets.add(secret)

    def __call__(self, text):
        text = "" if text is None else str(text)
        for secret in sorted(tuple(self._secrets), key=len, reverse=True):
            text = text.replace(secret, "***")
            quoted = quote(secret, safe="")
            if quoted != secret:
                text = text.replace(quoted, "***")
        return text


def password_source(cc, kind, username=None):
    """Where the password of 'sla' / 'itsm' would come from: 'environment', 'file', 'credential-manager' or None."""
    if os.environ.get(common.PASSWORD_ENV[kind]):
        return "environment"
    if cc is not None and cc.file_password(kind):
        return "file"
    username = username or (cc.username(kind) if cc is not None else "")
    if username:
        try:
            import keyring
            if keyring.get_password(common.KEYRING_SERVICES[kind], str(username)):
                return "credential-manager"
        except Exception:
            pass
    return None


def credential_signature(cc, kind, username):
    """Fingerprint of the account (kept in memory only) to notice that somebody fixed the user or the password."""
    raw = "|".join([str(username or ""), os.environ.get(common.PASSWORD_ENV[kind], ""), cc.file_password(kind) if cc else ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------- settings
def effective_settings(store, cc):
    """Saved settings with the pinned keys of collector_config.json on top, validated. Returns (settings, errors)."""
    base, _ = hub_settings.validate(store.read_settings_raw())
    return hub_settings.validate(cc.apply(base) if cc is not None else base)


def timeout_seconds(settings, job):
    """Hard limit of one run: the default per job, or ``general.job_timeout_minutes[job]`` from the settings."""
    wanted = ((settings.get("general") or {}).get("job_timeout_minutes") or {})
    minutes = DEFAULT_TIMEOUT_MINUTES[job]
    if isinstance(wanted, dict):
        try:
            minutes = int(wanted.get(job) or minutes)
        except (TypeError, ValueError):
            pass
    return max(1, min(minutes, MAX_TIMEOUT_MINUTES)) * 60


# --------------------------------------------------------------------------------------- calendar rules
def day_allowed(settings, module, now):
    """Work day and not a holiday (the part of ``is_active`` that does not look at the hour)."""
    if now.date().isoformat() in (settings.get("general") or {}).get("holidays", []):
        return False
    days, _ = hub_settings.effective_schedule(settings, module)
    return not days or hub_settings.WEEKDAYS[now.weekday()] in days


def next_active_start(settings, module, moment):
    """The first moment at or after ``moment`` when scheduled runs are allowed (``moment`` itself if they are now)."""
    _, hours = hub_settings.effective_schedule(settings, module)
    start, end = (hours.split("-") if hours else ("00:00", "23:59"))
    for offset in range(9):
        day = moment + timedelta(days=offset)
        if not day_allowed(settings, module, day):
            continue
        begin = datetime.combine(day.date(), datetime.strptime(start, "%H:%M").time())
        finish = datetime.combine(day.date(), datetime.strptime(end, "%H:%M").time()) + timedelta(seconds=59)
        if moment <= finish:
            return max(moment, begin)
    return moment


def _slot_time(now, slot):
    hour, minute = (int(x) for x in slot.split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def report_enabled(settings):
    email_ = settings.get("email") or {}
    return bool(email_.get("enabled", True) and (email_.get("report") or {}).get("send", True))


def report_slots_due(settings, now, done):
    """Report slots (``HH:MM``) that should be sent now: passed, not sent yet today, and passed less than 3 hours ago."""
    if not report_enabled(settings) or not day_allowed(settings, "email", now):
        return []
    slots = (settings["email"].get("report") or {}).get("send_times") or []
    return [s for s in slots if s not in done and _slot_time(now, s) <= now < _slot_time(now, s) + CATCH_UP]


def next_report_slot(settings, now, done):
    """When the report job will next have something to do (None when reports are off)."""
    if not report_enabled(settings):
        return None
    slots = (settings["email"].get("report") or {}).get("send_times") or []
    for offset in range(9):
        day = now + timedelta(days=offset)
        if not day_allowed(settings, "email", day):
            continue
        for slot in slots:
            moment = _slot_time(day, slot)
            if moment > now and not (offset == 0 and slot in done):
                return moment
    return None


# ------------------------------------------------------------------------------------------- running a job
def _entry(job, target=None):
    return tuple(target) if target else JOBS[job]


def resolve_job(job, target=None):
    entry = _entry(job, target)
    if callable(entry):
        return entry
    module_name, func_name = entry
    return getattr(importlib.import_module(module_name), func_name)


def failed_result(message, **kw):
    return common.JobResult(ok=False, error=message, **kw)


def call_job(job, ctx, scrub, target=None):
    """Run the engine. Never raises: a crash becomes a failed JobResult and the traceback goes to the job log."""
    try:
        function = resolve_job(job, target)
    except ModuleNotFoundError as exc:
        entry = _entry(job, target)
        wanted = "" if callable(entry) else entry[0]
        if exc.name and wanted and (wanted == exc.name or wanted.startswith(exc.name + ".")):
            return failed_result(f"The {job} engine ({wanted}) is not available on this PC.")
        return failed_result(f"The {job} engine needs a Python package that is missing: {exc.name or exc}. "
                             "Run  python collector.py  once in a window to install the requirements.")
    except Exception as exc:
        return failed_result(f"Cannot load the {job} engine: {type(exc).__name__}: {scrub(exc)}")
    try:
        result = function(ctx)
    except (Exception, SystemExit) as exc:
        ctx.log(scrub(traceback.format_exc()))
        return failed_result(f"{type(exc).__name__}: {scrub(exc)}".strip(": "))
    if not isinstance(result, common.JobResult):
        return failed_result(f"The {job} engine returned no result.")
    result.error = scrub(result.error) if result.error else result.error
    result.summary = scrub(result.summary)
    return result


def make_context(job, settings, store, *, clock, log, options, scrub, cc=None, mailer=None, debug=False, dry_run=False,
                 no_email=False, headed=False, host=""):
    """The JobContext an engine receives (docs/HUB-DESIGN.md section 6)."""
    def credentials(kind, username):
        password = collector_config.get_password(cc, kind, username)
        scrub.add(password)
        return password

    return common.JobContext(
        job=job, settings=settings.get(schemas.JOB_MODULE[job]) or {}, general=settings.get("general") or {}, store=store,
        now=clock, log=log, mailer=mailer, debug=debug, dry_run=dry_run, no_email=no_email, host=host or socket.gethostname(),
        data_dir=str(store.root.parent), headed=headed, options=dict(options or {}), credentials=credentials)


def stopped_result():
    """The collector was asked to stop before or during the run: nothing happened that the pages should hear about."""
    return common.JobResult(ok=False, skipped=True, error_kind="stopped", summary="Stopped.")


def result_from_json(doc):
    names = {f.name for f in dataclasses.fields(common.JobResult)}
    return common.JobResult(**{k: v for k, v in (doc or {}).items() if k in names})


def kill_process_tree(pid):
    """End a child process and everything it started (Outlook scan helpers, Chrome / Edge and their helpers)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW, timeout=30)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def run_child(job, spec_path, result_path):
    """Entry of ``collector.py --run-job``: run one job in this fresh process and write its JobResult as JSON.

    The parent (Runner._run_child) wrote the spec; the job log is written here, directly, so a hung job still leaves its lines."""
    scrub = Scrubber()
    try:
        with open(spec_path, encoding="utf-8") as f:
            spec = json.load(f)
        cc = collector_config.load(spec["config_path"]) if spec.get("config_path") else collector_config.CollectorConfig({}, None)
        for kind in ACCOUNT_KINDS:
            scrub.add(cc.file_password(kind))
            scrub.add(os.environ.get(common.PASSWORD_ENV[kind]))
        store = OpsStore(spec["data_dir"])
        settings, _ = effective_settings(store, cc)

        def log(message):
            store.append_log(job, scrub(message))

        ctx = make_context(job, settings, store, clock=datetime.now, log=log, options=spec.get("options"), scrub=scrub, cc=cc,
                           mailer=common.FakeMailer() if spec.get("dry_run") else common.OutlookMailer(),
                           debug=spec.get("debug", False), dry_run=spec.get("dry_run", False), no_email=spec.get("no_email", False),
                           headed=spec.get("headed", False), host=spec.get("host", ""))
        result = call_job(job, ctx, scrub, spec.get("target"))
    except Exception as exc:
        result = failed_result(f"The {job} job could not start: {type(exc).__name__}: {scrub(exc)}")
    doc = dataclasses.asdict(result)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, default=str)
    return 0


# --------------------------------------------------------------------------------------------- collector log
def append_collector_log(store, line):
    """Lines about the collector itself (start, stop, settings problems, housekeeping): ops/jobs/logs/collector.log."""
    path = store.logs_dir / "collector.log"
    try:
        store.logs_dir.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(path, path.with_suffix(".log.1"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")
    except OSError:
        pass


def _parse_iso(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


class _JobState:
    def __init__(self):
        self.next_run = None            # not before this moment (None = decide on first look)
        self.fail_streak = 0
        self.paused = None              # "auth": scheduled runs stopped until the credentials change
        self.paused_since = None        # ISO text for the pages
        self.paused_ts = None           # epoch seconds, for the "has the config file changed since" test
        self.cred_sig = None
        self.runs_ok = 0
        self.runs_failed = 0
        self.last_status = None
        self.running = False
        self.last_beat = 0.0
        self.noted = {}


class Runner:
    def __init__(self, store, cc=None, jobs=None, clock=datetime.now, mailer_factory=None, isolate=False, dry_run=False,
                 no_email=False, debug=False, headed=False, console=None, poll_seconds=POLL_SECONDS,
                 heartbeat_seconds=HEARTBEAT_SECONDS, timeouts=None, script=None):
        self.store = store
        self.cc = cc or collector_config.CollectorConfig({}, None)
        self.jobs = tuple(j for j in (jobs or schemas.JOBS) if j in schemas.JOBS)
        self.clock = clock
        self.mailer_factory = mailer_factory
        self.isolate = isolate
        self.dry_run, self.no_email, self.debug, self.headed = dry_run, no_email, debug, headed
        self.console = console
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.timeouts = dict(timeouts or {})           # job -> seconds (tests); the settings decide otherwise
        self.script = Path(script) if script else REPO_DIR / "collector.py"
        self.host = socket.gethostname()
        self.scrub = Scrubber()
        self.started_at = now_iso(clock())
        self.states = {}
        self.last_results = {}
        self._settings = None
        self._raw = None
        self._problem_key = None
        self._info_key = None
        self._cc_sig = self._file_signature()
        self._reload_lock = threading.RLock()
        self._stop = threading.Event()
        self._threads = []
        self._children = {}
        self._children_lock = threading.Lock()
        self._pruned = set()
        self._noted = {}
        self._remember_secrets()
        for job in self.jobs:
            self._load_counters(job)

    # ------------------------------------------------------------------ small helpers
    def _state(self, job):
        if job not in self.states:
            self.states[job] = _JobState()
        return self.states[job]

    def _echo(self, job, text):
        if self.console:
            try:
                self.console(f"[{job}] {text}" if job else text)
            except Exception:
                pass

    def _say(self, text, job=None):
        """A line about one job (its log) or about the collector itself (collector.log); also shown on the console."""
        text = self.scrub(text)
        if not self.dry_run:
            if job:
                self.store.append_log(job, text)
            else:
                append_collector_log(self.store, text)
        self._echo(job, text)

    def _note(self, job, key, text, every=300):
        """Like _say, but the same problem is repeated at most every ``every`` seconds (a down share must not flood the log)."""
        book = self._state(job).noted if job else self._noted
        if time.monotonic() - book.get(key, -1e9) >= every:
            book[key] = time.monotonic()
            self._say(text, job)

    def _job_logger(self, job):
        def log(message):
            text = self.scrub(message)
            if not self.dry_run:
                self.store.append_log(job, text)
            self._echo(job, text)
        return log

    def _remember_secrets(self):
        for kind in ACCOUNT_KINDS:
            self.scrub.add(self.cc.file_password(kind))
            self.scrub.add(os.environ.get(common.PASSWORD_ENV[kind]))

    def _file_signature(self):
        path = getattr(self.cc, "path", None)
        try:
            st = Path(path).stat()
            return st.st_mtime_ns, st.st_size
        except (OSError, TypeError):
            return None

    def _load_counters(self, job):
        st = self._state(job)
        try:
            hb = self.store.read_heartbeat(job)
        except Exception:
            hb = None
        if not isinstance(hb, dict):
            return
        st.runs_ok, st.runs_failed = int(hb.get("runs_ok") or 0), int(hb.get("runs_failed") or 0)
        st.last_status = hb.get("last_status")
        if st.last_status == "skipped":
            st.last_status = "error" if hb.get("last_error") else "ok"
        st.fail_streak = int(hb.get("consecutive_failures") or 0)
        if hb.get("paused") == "auth":
            st.paused, st.paused_since, st.paused_ts = "auth", hb.get("paused_since"), hb.get("paused_ts")

    # ------------------------------------------------------------------ settings and config
    def reload(self):
        """Current effective settings. Cheap when nothing changed (the store caches the file by size and mtime)."""
        with self._reload_lock:
            config_changed = self._reload_config()
            try:
                raw = self.store.read_settings_raw()
            except Exception as exc:            # a damaged settings.json: keep working with the last good values
                self._note(None, "settings-read", f"settings.json cannot be read, keeping the previous settings: {exc}")
                raw = self._raw
            if self._settings is None or config_changed or (raw is not self._raw and raw != self._raw):
                self._raw = raw
                clean, errors = effective_settings(self.store, self.cc)
                key = tuple((e["path"], e["message"]) for e in errors)
                if key != self._problem_key:
                    self._problem_key = key
                    if errors:
                        shown = "; ".join(f"{e['path']}: {e['message']}" for e in errors[:5])
                        self._say(f"Settings problem (the safe value is used until it is fixed): {shown}")
                self._settings = clean
                self.write_collector_info()
            return self._settings

    def _reload_config(self):
        signature = self._file_signature()
        if signature == self._cc_sig:
            return False
        self._cc_sig = signature
        try:
            self.cc = collector_config.load(self.cc.path)
        except collector_config.CollectorConfigError as exc:
            self._say(f"collector_config.json problem, keeping the previous values: {exc}")
            return False
        self._remember_secrets()
        self._say("collector_config.json changed - reloaded.")
        return True

    def write_collector_info(self):
        """ops/state/collector_info.json for the System page: who/where, which keys are pinned, which accounts have a password."""
        if self.dry_run:
            return
        settings = self._settings or {}
        accounts = {}
        for kind in ACCOUNT_KINDS:
            username = str((settings.get(kind) or {}).get("username") or self.cc.username(kind) or "")
            source = password_source(self.cc, kind, username)
            accounts[kind] = {"username": username, "has_password": bool(source), "password_source": source}
        info = {"host": self.host, "pid": os.getpid(), "started_at": self.started_at, "version": app_version(),
                "config_path": str(self.cc.path) if self.cc.path else "", "config_found": bool(self.cc.exists),
                "pinned_keys": self.cc.pinned_keys(), "accounts": accounts, "jobs": list(self.jobs),
                "isolated": bool(self.isolate), "python": sys.version.split()[0]}
        key = json.dumps(info, sort_keys=True)
        if key == self._info_key:
            return
        self._info_key = key
        try:
            self.store.write_state("collector_info", {**info, "updated_at": now_iso()})
        except Exception as exc:
            self._note(None, "info", f"collector_info.json could not be written: {exc}")

    # ------------------------------------------------------------------ heartbeat
    def _beat(self, job, **fields):
        if self.dry_run:
            return
        base = {"host": self.host, "pid": os.getpid(), "version": app_version(), "updated_ts": round(time.time(), 1)}
        try:
            self.store.write_heartbeat(job, **base, **fields)
        except Exception as exc:
            self._note(job, "heartbeat", f"heartbeat could not be written: {exc}")
        self._state(job).last_beat = time.monotonic()

    def _enabled(self, job, settings):
        if job == "email_report":
            return report_enabled(settings)
        return bool((settings.get(schemas.JOB_MODULE[job]) or {}).get("enabled", True))

    def _interval(self, job, settings):
        if job == "email_report":
            return REPORT_RETRY_MINUTES
        return max(1, int((settings.get(schemas.JOB_MODULE[job]) or {}).get("interval_minutes") or 10))

    def _retry_minutes(self, job, settings):
        """Minutes until the next scheduled run: the interval, doubled after each further failure, capped at 30 (or the interval)."""
        base = self._interval(job, settings)
        streak = self._state(job).fail_streak
        if streak <= 1:
            return base
        return min(base * 2 ** (streak - 1), max(base, BACKOFF_CAP_MINUTES))

    def _next_run_at(self, job, settings, now):
        """When the job will next run, for the heartbeat (None when it will not: disabled or waiting for a fixed password)."""
        st = self._state(job)
        if not self._enabled(job, settings) or st.paused:
            return None
        if job == "email_report":
            moment = next_report_slot(settings, now, self._slots_done(now))
            return max(moment, st.next_run) if moment and st.next_run else moment
        moment = max(st.next_run or now, now)
        return next_active_start(settings, schemas.JOB_MODULE[job], moment)

    def _idle_state(self, job, settings, now):
        st = self._state(job)
        if not self._enabled(job, settings):
            return "disabled"
        if st.paused:
            return "paused_auth"
        if job != "email_report" and not hub_settings.is_active(settings, schemas.JOB_MODULE[job], now):
            return "outside_hours"
        return "idle"

    def _idle_beat(self, job, settings, now, force=False):
        st = self._state(job)
        if not force and time.monotonic() - st.last_beat < self.heartbeat_seconds:
            return
        moment = self._next_run_at(job, settings, now)
        self._beat(job, running=False, enabled=self._enabled(job, settings), state=self._idle_state(job, settings, now),
                   next_run_at=now_iso(moment) if moment else None, paused=st.paused,
                   paused_reason=self._paused_reason(st), consecutive_failures=st.fail_streak,
                   retry_multiplier=self._retry_minutes(job, settings) // self._interval(job, settings))

    @staticmethod
    def _paused_reason(st):
        return "login failed - fix the password, retries paused" if st.paused == "auth" else None

    # ------------------------------------------------------------------ decisions
    def _slots_done(self, now):
        try:
            doc = self.store.read_state(REPORT_STATE)
        except Exception:
            return set()
        return set(doc.get("done") or []) if doc.get("day") == now.date().isoformat() else set()

    def _mark_slots(self, now, slots):
        if not slots or self.dry_run:
            return
        done = self._slots_done(now) | set(slots)
        try:
            self.store.write_state(REPORT_STATE, {"day": now.date().isoformat(), "done": sorted(done)})
        except Exception as exc:
            self._note("email_report", "slots", f"could not remember the sent report slots: {exc}")

    def _initial_next_run(self, job, settings, now):
        try:
            hb = self.store.read_heartbeat(job)
        except Exception:
            hb = None
        last = _parse_iso((hb or {}).get("last_run_at"))
        if last is None:
            return now
        return max(now, last + timedelta(minutes=self._interval(job, settings)))

    def _username(self, kind):
        return str(((self._settings or {}).get(kind) or {}).get("username") or self.cc.username(kind) or "")

    def _credentials_changed(self, job):
        st = self._state(job)
        kind = job if job in ACCOUNT_KINDS else None
        if kind and st.cred_sig is not None:
            return credential_signature(self.cc, kind, self._username(kind)) != st.cred_sig
        if st.paused_ts and self._cc_sig:                          # paused by an earlier collector process
            return self._cc_sig[0] / 1e9 > float(st.paused_ts)
        return False

    def _due(self, job, settings, now):
        st = self._state(job)
        if st.paused == "auth":
            if not self._credentials_changed(job):
                return False
            self._say("The account details changed - scheduled runs resume.", job)
            st.paused, st.paused_since, st.paused_ts, st.cred_sig, st.fail_streak = None, None, None, None, 0
        if job == "email_report":
            if st.next_run and now < st.next_run:
                return False
            return bool(report_slots_due(settings, now, self._slots_done(now)))
        if st.next_run is None:
            st.next_run = self._initial_next_run(job, settings, now)
        return now >= st.next_run and hub_settings.is_active(settings, schemas.JOB_MODULE[job], now)

    def step(self, job, now=None):
        """One look at one job: run it if it is due or somebody asked, otherwise keep the heartbeat alive.

        Returns "ran", "idle" or "busy" (another collector holds the job lock)."""
        now = now or self.clock()
        settings = self.reload()
        try:
            pending = self.store.pending_run_requests(job)
        except Exception as exc:
            self._note(job, "requests", f"could not look for 'Run now' requests: {exc}")
            pending = []
        if pending:
            trigger = "run-now"
        elif self._enabled(job, settings) and self._due(job, settings, now):
            trigger = "schedule"
        else:
            self._idle_beat(job, settings, now)
            return "idle"
        with self.store.try_lock(f"job-{job}") as got:
            if not got:
                self._note(job, "busy", "Another collector is running this job - waiting.", every=900)
                return "busy"
            options = {"trigger": trigger}
            slots = []
            if trigger == "run-now":
                taken = self.store.consume_run_requests(job)
                if not taken:                                   # another collector took the request first
                    return "idle"
                options.update(requested_by=next((r.get("requested_by") for r in taken if r.get("requested_by")), ""),
                               send_mail=any(bool(r.get("send_mail")) for r in taken), force=any(bool(r.get("force")) for r in taken))
            elif job == "email_report":
                slots = report_slots_due(settings, now, self._slots_done(now))
                if not slots:
                    return "idle"
            self._execute(job, settings, options, slots)
        return "ran"

    def run_once(self, job, options=None):
        """The ``--once`` command: the normal pipeline, but ignoring the schedule and the enabled flag.

        Returns ``(status, result)`` where status is "ran" or "busy"."""
        settings = self.reload()
        merged = {"trigger": "cli", "requested_by": "command line", **(options or {})}
        with self.store.try_lock(f"job-{job}") as got:
            if not got:
                return "busy", None
            return "ran", self._execute(job, settings, merged, [])

    # ------------------------------------------------------------------ one run
    def _execute(self, job, settings, options, slots):
        st = self._state(job)
        started = self.clock()
        clock0 = time.monotonic()
        log = self._job_logger(job)
        who = f", requested by {options['requested_by']}" if options.get("requested_by") else ""
        st.running = True
        self._beat(job, running=True, state="running", started_at=now_iso(started), enabled=self._enabled(job, settings))
        log(f"Started ({options.get('trigger', 'schedule')}{who}){' - dry run' if self.dry_run else ''}")
        try:
            with self._outlook_turn(job) as have_turn:
                result = self._run(job, settings, options, log) if have_turn else stopped_result()
        except Exception as exc:                                 # e.g. the job process could not even be started
            log(self.scrub(traceback.format_exc()))
            result = failed_result(f"{type(exc).__name__}: {self.scrub(exc)}")
        finally:
            st.running = False
        elapsed = time.monotonic() - clock0
        self._finish(job, settings, result, started, self.clock(), elapsed, options, slots)
        self.last_results[job] = result
        return result

    @contextlib.contextmanager
    def _outlook_turn(self, job):
        """Outlook automation is one conversation at a time: the e-mail jobs wait for each other here."""
        if job not in OUTLOOK_JOBS:
            yield True
            return
        while not common.OUTLOOK_LOCK.acquire(timeout=1):
            if self._stop.is_set():
                yield False
                return
        try:
            yield True
        finally:
            common.OUTLOOK_LOCK.release()

    def _run(self, job, settings, options, log):
        if self.isolate and not self.dry_run:
            return self._run_child(job, settings, options, log)
        if self.dry_run:
            mailer = common.FakeMailer()
        else:
            mailer = self.mailer_factory() if self.mailer_factory else common.OutlookMailer()
        ctx = make_context(job, settings, self.store, clock=self.clock, log=log, options=options, scrub=self.scrub, cc=self.cc,
                           mailer=mailer, debug=self.debug, dry_run=self.dry_run, no_email=self.no_email, headed=self.headed,
                           host=self.host)
        return call_job(job, ctx, self.scrub)

    def _run_child(self, job, settings, options, log):
        seconds = self.timeouts.get(job) or timeout_seconds(settings, job)
        entry = JOBS[job]
        target = list(entry) if not callable(entry) else [entry.__module__, entry.__name__]
        work = tempfile.mkdtemp(prefix="nbe-job-")
        spec_path, result_path, out_path = (os.path.join(work, n) for n in ("spec.json", "result.json", "output.txt"))
        spec = {"job": job, "target": target, "config_path": str(self.cc.path) if self.cc.path else "",
                "data_dir": str(self.store.root.parent), "options": options, "debug": self.debug, "headed": self.headed,
                "no_email": self.no_email, "dry_run": False, "host": self.host}
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump(spec, f)
        cmd = [sys.executable, str(self.script), "--run-job", job, "--job-spec", spec_path, "--job-result", result_path]
        kw = {"creationflags": NO_WINDOW | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        outcome = "done"
        try:
            with open(out_path, "wb") as out:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=str(self.script.parent),
                                        **kw)
                with self._children_lock:
                    self._children[job] = proc
                deadline = time.monotonic() + seconds
                while True:
                    try:
                        proc.wait(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if self._stop.is_set():
                        outcome = "stopped"
                    elif time.monotonic() > deadline:
                        outcome = "timeout"
                    if outcome != "done":
                        kill_process_tree(proc.pid)
                        proc.wait()
                        break
            with self._children_lock:
                self._children.pop(job, None)
            return self._child_result(job, outcome, proc.returncode, seconds, result_path, out_path, log)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _child_result(self, job, outcome, code, seconds, result_path, out_path, log):
        if outcome == "timeout":
            log(f"The run took longer than {seconds // 60} min and was stopped (the programs it started were closed too).")
            return failed_result(f"Timed out after {seconds // 60} minutes - the run was stopped.", error_kind="timeout")
        if outcome == "stopped":
            return stopped_result()
        try:
            with open(result_path, encoding="utf-8") as f:
                result = result_from_json(json.load(f))
            result.error = self.scrub(result.error) if result.error else result.error
            result.summary = self.scrub(result.summary)
            return result
        except (OSError, ValueError):
            pass
        try:
            with open(out_path, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-1500:]
        except OSError:
            tail = ""
        log(f"The job process ended without a result (exit code {code}).\n{self.scrub(tail)}".strip())
        return failed_result(f"The {job} job process ended unexpectedly (exit code {code}).")

    # ------------------------------------------------------------------ after a run
    def _finish(self, job, settings, result, started, finished, elapsed, options, slots):
        st = self._state(job)
        module = schemas.JOB_MODULE[job]
        status = "skipped" if result.skipped else ("ok" if result.ok else "error")
        previous_ok = {"ok": True, "error": False}.get(st.last_status)
        error = self.scrub(result.error) if result.error else None
        summary = self.scrub(result.summary or "")
        ts = now_iso(finished)
        extra = {}
        if result.error_kind == "stopped":
            self._beat(job, running=False, state="stopped")
            self._job_logger(job)("Stopped before the run was finished.")
            return

        if status == "ok":
            st.runs_ok += 1
            st.fail_streak, st.paused, st.paused_since, st.paused_ts, st.cred_sig = 0, None, None, None, None
            self._store_result(job, module, result, finished, elapsed)
            extra.update(last_ok_at=ts, last_error=None)
        elif status == "error":
            st.runs_failed += 1
            st.fail_streak += 1
            extra.update(last_error=(error or "unknown error")[:500])
            if result.error_kind == "auth":
                st.paused, st.paused_since, st.paused_ts = "auth", ts, time.time()
                st.cred_sig = credential_signature(self.cc, job, self._username(job)) if job in ACCOUNT_KINDS else None
                self._say("The login failed (wrong or missing user / password) - scheduled runs of this job are paused until "
                          "the account details are fixed (or someone presses Run now).", job)
        extra.update(last_run_at=ts, last_status=status)
        if status != "skipped":                                 # a skipped run says nothing about whether scans work
            self._job_event(job, ts, status == "ok", previous_ok, error)
            st.last_status = status
        if slots and (status in ("ok", "skipped") or result.emails_sent):
            self._mark_slots(finished, slots)

        if job == "email_report" and status != "error":
            st.next_run = None
        else:
            st.next_run = max(started + timedelta(minutes=self._retry_minutes(job, settings)), finished + MIN_GAP)
        moment = self._next_run_at(job, settings, finished)
        self._beat(job, running=False, state="paused_auth" if st.paused else "idle", enabled=self._enabled(job, settings),
                   last_summary=summary, last_duration_seconds=round(elapsed, 2), runs_ok=st.runs_ok, runs_failed=st.runs_failed,
                   consecutive_failures=st.fail_streak,
                   retry_multiplier=self._retry_minutes(job, settings) // self._interval(job, settings),
                   paused=st.paused, paused_since=st.paused_since, paused_ts=st.paused_ts, paused_reason=self._paused_reason(st),
                   next_run_at=now_iso(moment) if moment else None, emails_sent=int(result.emails_sent or 0), **extra)
        line = {"ok": f"Finished OK - {summary}", "error": f"FAILED - {error or 'unknown error'}",
                "skipped": f"Skipped - {summary or 'nothing to do'}"}[status]
        self._job_logger(job)(f"{line} ({elapsed:.1f} s)" + (f", {result.emails_sent} e-mail(s) sent" if result.emails_sent else ""))

    def _store_result(self, job, module, result, finished, elapsed):
        """Snapshot (only a good scan with data), history point, timeline events."""
        if self.dry_run:
            return
        log = self._job_logger(job)
        day = result.snapshot_date or finished.date().isoformat()
        if result.snapshot_data is not None:
            try:
                previous = self.store.read_snapshot(module)
            except Exception:
                previous = None
            try:
                envelope = self.store.write_snapshot(module, result.snapshot_data, generated_at=now_iso(finished),
                                                     duration_seconds=elapsed, host=self.host, date=result.snapshot_date)
            except Exception as exc:
                log(f"The snapshot could not be written: {exc}")
                return
            try:
                found = events.diff_events(module, previous, envelope)
                if found:
                    self.store.add_events(found)
            except Exception as exc:
                log(f"Timeline events could not be written: {type(exc).__name__}: {exc}")
        if result.history_point or result.history_extra:
            try:
                self._write_history(module, day, result.history_point, result.history_extra)
            except Exception as exc:
                log(f"The history point could not be written: {type(exc).__name__}: {exc}")

    def _write_history(self, module, day, point, extra):
        if point:
            self.store.add_history_point(module, day, point, extra)
        else:
            def change(doc):
                for key, value in extra.items():
                    if isinstance(value, dict) and isinstance(doc.get(key), dict):
                        for k, v in value.items():
                            doc[key].setdefault(k, v)
                    else:
                        doc[key] = value
            self.store.update_history(module, day, change)
        if (module, day) not in self._pruned:                  # once per day is enough
            self._pruned = {m for m in self._pruned if m[1] == day} | {(module, day)}
            keep = HISTORY_KEEP_DAYS
            if module == "email":
                keep = max(keep, int((self._settings or {}).get("email", {}).get("history_keep_days") or 0))
            self.store.prune_history(module, keep)

    def _job_event(self, job, ts, ok, previous_ok, error):
        if self.dry_run:
            return
        try:
            event = events.job_event(job, ts, ok, previous_ok, self.scrub(error) if error else error)
            if event:
                self.store.add_events([event])
        except Exception as exc:
            self._note(job, "events", f"timeline event could not be written: {exc}")

    # ------------------------------------------------------------------ threads
    def start(self):
        """Start one worker thread per job (and the maintenance thread). Returns at once."""
        settings = self.reload()
        now = self.clock()
        self._say(f"Collector started on {self.host} (pid {os.getpid()}), jobs: {', '.join(self.jobs)}"
                  f"{', each run in its own process' if self.isolate else ''}.")
        for job in self.jobs:
            self._idle_beat(job, settings, now, force=True)
            thread = threading.Thread(target=self._worker, args=(job,), name=f"job-{job}", daemon=True)
            self._threads.append(thread)
            thread.start()
        thread = threading.Thread(target=self._maintenance, name="maintenance", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _worker(self, job):
        while not self._stop.is_set():
            try:
                self.step(job)
            except Exception:
                self._note(job, "crash", "The scheduler hit an unexpected error (it keeps running):\n"
                                         + self.scrub(traceback.format_exc()), every=600)
            self._stop.wait(self.poll_seconds)

    def _maintenance(self):
        """Keeps the heartbeat of a long-running job alive and runs the daily housekeeping."""
        rounds = 0
        while not self._stop.wait(HEARTBEAT_SECONDS):
            rounds += 1
            try:
                for job in self.jobs:
                    if self._state(job).running:
                        self._beat(job)
                if rounds % 10 == 0:
                    self.write_collector_info()
                self.housekeeping_if_due()
            except Exception:
                self._note(None, "maintenance", "Maintenance error:\n" + self.scrub(traceback.format_exc()), every=1800)

    def wait(self):
        while not self._stop.wait(1):
            pass

    def request_stop(self):
        """Signal handlers call this; ``stop`` does the actual cleanup."""
        self._stop.set()

    @property
    def stopping(self):
        return self._stop.is_set()

    def stop(self, timeout=20):
        """Ask everything to finish: running children are killed, workers are given ``timeout`` seconds."""
        self._stop.set()
        with self._children_lock:
            children = list(self._children.values())
        for proc in children:
            with contextlib.suppress(Exception):
                kill_process_tree(proc.pid)
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        for job in self.jobs:
            self._beat(job, running=False, state="stopped")
        self._say("Collector stopped.")

    # ------------------------------------------------------------------ housekeeping
    def housekeeping_if_due(self, now=None):
        """Once a day, outside business hours: delete what is old (HUB-DESIGN section 15)."""
        now = now or self.clock()
        settings = self.reload()
        try:
            done = self.store.read_state(HOUSEKEEPING_STATE).get("day")
        except Exception:
            done = None
        if done == now.date().isoformat():
            return None
        modules = {schemas.JOB_MODULE[j] for j in self.jobs if self._enabled(j, settings)}
        quiet = not any(hub_settings.is_active(settings, m, now) for m in modules) or 1 <= now.hour < 6
        return self.housekeeping(now) if quiet else None

    def housekeeping(self, now=None):
        """Prune history, events, reports, debug dumps and stale run requests. Returns what was removed (counts)."""
        now = now or self.clock()
        with self.store.try_lock("housekeeping") as got:
            if not got:
                return None
            removed = {}
            steps = (("history", self._prune_history), ("events", self._prune_events), ("reports", self._prune_reports),
                     ("debug files", self._prune_debug), ("run requests", self._prune_requests),
                     ("mail copies", self._prune_mail_copies), ("notifications", self._prune_notifications))
            for name, function in steps:
                try:
                    removed[name] = function(now)
                except Exception as exc:
                    removed[name] = f"failed ({type(exc).__name__})"
            if not self.dry_run:
                with contextlib.suppress(Exception):
                    self.store.write_state(HOUSEKEEPING_STATE, {"day": now.date().isoformat(), "removed": removed})
        self._say("Housekeeping: " + ", ".join(f"{k} {v}" for k, v in removed.items()))
        for job in self.jobs:
            self._job_logger(job)("Housekeeping done: " + ", ".join(f"{k} {v}" for k, v in removed.items()))
        return removed

    def _prune_history(self, now):
        before = sum(len(self.store.history_days(m)) for m in schemas.MODULES)
        for module in schemas.MODULES:
            keep = HISTORY_KEEP_DAYS
            if module == "email":
                keep = max(keep, int((self._settings or {}).get("email", {}).get("history_keep_days") or 0))
            self.store.prune_history(module, keep)
        return before - sum(len(self.store.history_days(m)) for m in schemas.MODULES)

    def _prune_events(self, now):
        before = len(list(self.store.events_dir.glob("*.jsonl")))
        self.store.prune_events(EVENT_KEEP_DAYS)
        return before - len(list(self.store.events_dir.glob("*.jsonl")))

    def _prune_reports(self, now):
        """Older than 60 days, or beyond the newest 30 of the same kind of file. ``*latest*`` files are never removed."""
        removed = 0
        limit = (now - timedelta(days=REPORT_KEEP_DAYS)).timestamp()
        for module in schemas.MODULES:
            series = {}
            try:
                entries = [e for e in os.scandir(self.store.reports_dir / module) if e.is_file()]
            except OSError:
                continue
            for entry in entries:
                if "latest" in entry.name.lower():
                    continue
                kind = "".join("#" if c.isdigit() else c for c in entry.name)
                series.setdefault(kind, []).append((entry.stat().st_mtime, entry.path))
            for files in series.values():
                files.sort(reverse=True)
                for rank, (mtime, path) in enumerate(files):
                    if mtime < limit or rank >= REPORT_KEEP_COPIES:
                        with contextlib.suppress(OSError):
                            os.remove(path)
                            removed += 1
        return removed

    def _prune_debug(self, now):
        removed = 0
        limit = (now - timedelta(days=DEBUG_KEEP_DAYS)).timestamp()
        for root, _, names in os.walk(self.store.root / "debug"):
            for name in names:
                path = os.path.join(root, name)
                with contextlib.suppress(OSError):
                    if os.path.getmtime(path) < limit:
                        os.remove(path)
                        removed += 1
        return removed

    def _prune_mail_copies(self, now):
        """Saved copies of mails (ops/mail): files of mails that left the pending / assigned set ``keep_days`` ago, orphans
        and left-over temp files. Returns how many files were removed."""
        keep = int((((self._settings or {}).get("email") or {}).get("mail_copies") or {}).get("keep_days") or 21)
        done = mailbox.housekeeping(self.store, mailbox.wanted_keys_from(self.store), keep, today=now.date(), now=now)
        return done["removed"] + done["orphans"] + done["temp"]

    def _prune_notifications(self, now):
        """In-app notifications older than 30 days (each person's file is also cut to 100 items when it is written)."""
        return notifications.prune(self.store, NOTIFICATION_KEEP_DAYS, now)

    def _prune_requests(self, now):
        removed = 0
        limit = (now - timedelta(days=REQUEST_KEEP_DAYS)).timestamp()
        try:
            names = [e for e in os.scandir(self.store.requests_dir) if e.is_file()]
        except OSError:
            return 0
        for entry in names:
            with contextlib.suppress(OSError):
                if entry.stat().st_mtime < limit:
                    os.remove(entry.path)
                    removed += 1
        return removed
