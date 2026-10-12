"""Files of the Operations Hub, all under ``<data_dir>/ops`` (see docs/HUB-DESIGN.md, section 4).

Used by the web app AND by the collector, so it must not import Flask.
Same safety rules as the Call Log storage: temp file + atomic rename, writes under a lock,
readers never see half-written files.
"""
import contextlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from ..storage import StorageError, _WriteLock, _atomic_write_json
from . import schemas

LOG_ROTATE_BYTES = 1_000_000
HISTORY_KEEP_DAYS = 60
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


def safe_file_name(name):
    """Reject anything that could leave the report folder (separators, '..', hidden files, odd characters)."""
    return isinstance(name, str) and bool(_SAFE_NAME.match(name)) and ".." not in name


def now_iso(now=None):
    return (now or datetime.now()).isoformat(timespec="seconds")


class OpsStore:
    TTL = 1.0       # seconds a parsed file is reused before the share is asked again

    def __init__(self, data_dir):
        self.root = Path(data_dir) / "ops"
        self.snapshots_dir = self.root / "snapshots"
        self.history_dir = self.root / "history"
        self.overrides_dir = self.root / "overrides" / "email"
        self.state_dir = self.root / "state"
        self.events_dir = self.root / "events"
        self.jobs_dir = self.root / "jobs"
        self.heartbeat_dir = self.jobs_dir / "heartbeat"
        self.requests_dir = self.jobs_dir / "requests"
        self.logs_dir = self.jobs_dir / "logs"
        self.reports_dir = self.root / "reports"
        self.locks_dir = self.root / "locks"
        self.settings_path = self.root / "settings.json"
        self._cache = {}
        self._cache_lock = threading.Lock()
        self._locks = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ setup
    def init(self):
        try:
            for d in (self.snapshots_dir, self.history_dir, self.overrides_dir, self.state_dir, self.events_dir, self.heartbeat_dir,
                      self.requests_dir, self.logs_dir, self.reports_dir, self.locks_dir):
                d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Cannot use the data folder '{self.root}': {exc}") from exc
        return self

    # ------------------------------------------------------------ primitives
    def _lock(self, name):
        with self._locks_guard:
            lock = self._locks.get(name)
            if lock is None:
                self.locks_dir.mkdir(parents=True, exist_ok=True)
                lock = self._locks[name] = _WriteLock(self.locks_dir / f"{name}.lock")
            return lock

    @contextlib.contextmanager
    def try_lock(self, name):
        """Hold a cross-process lock for as long as the ``with`` block runs. Yields False at once if someone else
        (this PC or another) holds it. Used so only one collector runs a given job at a time."""
        lock = self._lock(name)
        if not lock._thread_lock.acquire(blocking=False):
            yield False
            return
        try:
            self.locks_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock.path, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                try:
                    lock._acquire(fd, 0)
                except StorageError:
                    yield False
                    return
                try:
                    yield True
                finally:
                    lock._release(fd)
            finally:
                os.close(fd)
        finally:
            lock._thread_lock.release()

    def read_json(self, path, default=None):
        """Parsed JSON file (cached for TTL seconds, re-read when its size/mtime changed). Treat as read-only."""
        path = Path(path)
        key, now = str(path), time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and now - hit[2] < self.TTL:
                return hit[1]
        try:
            st = path.stat()
        except FileNotFoundError:
            with self._cache_lock:
                self._cache.pop(key, None)
            return default
        sig = (st.st_mtime_ns, st.st_size)
        if hit and hit[0] == sig:
            with self._cache_lock:
                self._cache[key] = (sig, hit[1], now)
            return hit[1]
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise StorageError(f"{path} is damaged and cannot be read: {exc}") from exc
        with self._cache_lock:
            self._cache[key] = (sig, data, now)
            if len(self._cache) > 400:
                self._cache.clear()
        return data

    def write_json(self, path, data):
        path = Path(path)
        _atomic_write_json(path, data)
        with self._cache_lock:
            self._cache.pop(str(path), None)

    def _update_json(self, path, lock_name, default, change):
        """Read-modify-write under a lock. ``change(data)`` mutates and/or returns the new value."""
        path = Path(path)
        with self._lock(lock_name)():
            self._cache.pop(str(path), None)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                data = default() if callable(default) else default
            except json.JSONDecodeError:
                data = default() if callable(default) else default
            result = change(data)
            data = data if result is None else result
            self.write_json(path, data)
            return data

    # --------------------------------------------------------------- settings
    def read_settings_raw(self):
        return self.read_json(self.settings_path, {})

    def write_settings_raw(self, data):
        with self._lock("settings")():
            self.write_json(self.settings_path, data)

    # -------------------------------------------------------------- snapshots
    def _module(self, module):
        if not schemas.valid_module(module):
            raise ValueError(f"unknown module {module!r}")
        return module

    def write_snapshot(self, module, data, generated_at=None, duration_seconds=0.0, host="", date=None):
        generated_at = generated_at or now_iso()
        envelope = schemas.make_envelope(self._module(module), data, generated_at, duration_seconds, host, date)
        with self._lock(f"snap-{module}")():
            self.write_json(self.snapshots_dir / f"{module}.json", envelope)
        return envelope

    def read_snapshot(self, module):
        return self.read_json(self.snapshots_dir / f"{self._module(module)}.json", None)

    # ---------------------------------------------------------------- history
    def _history_path(self, module, day):
        if not schemas.is_iso_day(day):
            raise ValueError(f"bad day {day!r}")
        return self.history_dir / self._module(module) / f"{day}.json"

    def update_history(self, module, day, change):
        """``change(doc)`` edits the day document (``{"points": [...]}`` by default)."""
        return self._update_json(self._history_path(module, day), f"hist-{module}", lambda: {"points": []}, change)

    def add_history_point(self, module, day, point, extra=None):
        """Append one scan point to the day file; ``extra`` keys are merged into the document (e.g. first_breach)."""
        def change(doc):
            doc.setdefault("points", []).append(point)
            if len(doc["points"]) > 2000:
                del doc["points"][:-2000]
            for k, v in (extra or {}).items():
                if isinstance(v, dict) and isinstance(doc.get(k), dict):
                    for kk, vv in v.items():
                        doc[k].setdefault(kk, vv)
                else:
                    doc[k] = v
        return self.update_history(module, day, change)

    def read_history(self, module, day):
        return self.read_json(self._history_path(module, day), None)

    def history_days(self, module):
        folder = self.history_dir / self._module(module)
        try:
            return sorted(p.stem for p in folder.glob("*.json") if schemas.is_iso_day(p.stem))
        except OSError:
            return []

    def prune_history(self, module, keep_days=HISTORY_KEEP_DAYS):
        days = self.history_days(module)
        for day in days[:-keep_days] if keep_days else []:
            with contextlib.suppress(OSError):
                self._history_path(module, day).unlink()

    # -------------------------------------------------------------- overrides
    def _override_path(self, name):
        if name not in schemas.OVERRIDES:
            raise ValueError(f"unknown override list {name!r}")
        return self.overrides_dir / f"{name}.json"

    def overrides(self, name):
        return self.read_json(self._override_path(name), {}) or {}

    def update_override(self, name, key, change):
        """Set/modify one entry: ``change(old_or_None)`` returns the new entry, or None to delete it."""
        def apply(doc):
            new = change(doc.get(key))
            if new is None:
                doc.pop(key, None)
            else:
                doc[key] = new
        return self._update_json(self._override_path(name), f"ovr-{name}", dict, apply).get(key)

    def set_override(self, name, key, value):
        return self.update_override(name, key, lambda old: value)

    def delete_override(self, name, key):
        return self.update_override(name, key, lambda old: None)

    # ----------------------------------------------------------------- events
    def add_events(self, events):
        """Append timeline events (see events.py) to ops/events/<day>.jsonl, one JSON object per line."""
        by_day = {}
        for ev in events:
            by_day.setdefault(str(ev.get("ts", ""))[:10], []).append(ev)
        for day, items in by_day.items():
            if not schemas.is_iso_day(day):
                continue
            self.events_dir.mkdir(parents=True, exist_ok=True)
            with self._lock("events")():
                with open(self.events_dir / f"{day}.jsonl", "a", encoding="utf-8") as f:
                    for ev in items:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def read_events(self, day, limit=200):
        """Newest first. A damaged line is skipped, never fatal."""
        if not schemas.is_iso_day(day):
            raise ValueError(f"bad day {day!r}")
        out = []
        try:
            with open(self.events_dir / f"{day}.jsonl", encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev, dict):
                        out.append(ev)
        except FileNotFoundError:
            return []
        out.sort(key=lambda e: str(e.get("ts", "")), reverse=True)
        return out[:max(1, min(int(limit), 1000))]

    def prune_events(self, keep_days=30):
        try:
            days = sorted(p.stem for p in self.events_dir.glob("*.jsonl") if schemas.is_iso_day(p.stem))
        except OSError:
            return
        for day in days[:-keep_days] if keep_days else []:
            with contextlib.suppress(OSError):
                (self.events_dir / f"{day}.jsonl").unlink()

    # ------------------------------------------------------------------ state
    def read_state(self, name, default=None):
        if not _SAFE_NAME.match(name):
            raise ValueError("bad state name")
        return self.read_json(self.state_dir / f"{name}.json", default if default is not None else {})

    def write_state(self, name, data):
        if not _SAFE_NAME.match(name):
            raise ValueError("bad state name")
        with self._lock(f"state-{name}")():
            self.write_json(self.state_dir / f"{name}.json", data)

    # ----------------------------------------------------------- heartbeats
    def _job(self, job):
        if not schemas.valid_job(job):
            raise ValueError(f"unknown job {job!r}")
        return job

    def write_heartbeat(self, job, **fields):
        """Merge ``fields`` into the job's heartbeat (and stamp ``updated_at``)."""
        path = self.heartbeat_dir / f"{self._job(job)}.json"
        def apply(doc):
            doc.update(fields)
            doc["job"] = job
            doc["updated_at"] = now_iso()
        return self._update_json(path, f"hb-{job}", dict, apply)

    def read_heartbeat(self, job):
        return self.read_json(self.heartbeat_dir / f"{self._job(job)}.json", None)

    def read_heartbeats(self):
        return {job: self.read_heartbeat(job) for job in schemas.JOBS}

    # --------------------------------------------------------- run-now requests
    def add_run_request(self, job, requested_by=""):
        self._job(job)
        name = f"{job}-{datetime.now():%Y%m%d%H%M%S}-{secrets.token_hex(3)}.json"
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.write_json(self.requests_dir / name, {"job": job, "requested_by": requested_by, "requested_at": now_iso()})
        return name

    def pending_run_requests(self, job=None):
        out = []
        try:
            names = sorted(p.name for p in self.requests_dir.glob("*.json"))
        except OSError:
            return out
        for name in names:
            if job and not name.startswith(f"{job}-"):
                continue
            doc = self.read_json(self.requests_dir / name, None)
            if isinstance(doc, dict) and schemas.valid_job(doc.get("job")) and (not job or doc["job"] == job):
                out.append({**doc, "file": name})
        return out

    def consume_run_requests(self, job):
        """Delete and return this job's pending requests (the runner calls this just before it starts the job)."""
        taken = []
        for req in self.pending_run_requests(job):
            try:
                os.remove(self.requests_dir / req["file"])
            except FileNotFoundError:
                continue            # another collector took it
            except OSError:
                continue
            with self._cache_lock:
                self._cache.pop(str(self.requests_dir / req["file"]), None)
            taken.append(req)
        return taken

    # ------------------------------------------------------------------- logs
    def log_path(self, job):
        return self.logs_dir / f"{self._job(job)}.log"

    def append_log(self, job, line):
        path = self.log_path(job)
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(path, path.with_suffix(".log.1"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")
        except OSError:
            pass            # logging must never break a job

    def tail_log(self, job, lines=200):
        lines = max(1, min(int(lines), 2000))
        try:
            with open(self.log_path(job), "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 400_000))
                text = f.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return []
        return text.splitlines()[-lines:]

    # ---------------------------------------------------------------- reports
    def report_dir(self, module):
        d = self.reports_dir / self._module(module)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_reports(self, module=None, limit=100):
        out = []
        for m in ([module] if module else schemas.MODULES):
            folder = self.reports_dir / self._module(m)
            try:
                entries = [e for e in os.scandir(folder) if e.is_file() and safe_file_name(e.name)]
            except OSError:
                continue
            for e in entries:
                st = e.stat()
                out.append({"module": m, "name": e.name, "size": st.st_size,
                            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
        out.sort(key=lambda r: r["modified"], reverse=True)
        return out[:limit]

    def report_path(self, module, name):
        """Path of a report, or None if the name is unsafe or the file does not exist."""
        if not schemas.valid_module(module) or not safe_file_name(name):
            return None
        path = self.reports_dir / module / name
        base = (self.reports_dir / module).resolve()
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if resolved.parent != base or not resolved.is_file():
            return None
        return resolved
