"""Plain-file storage for the shared folder - no database server needed.

Layout inside the data folder::

    users.json                     all accounts
    reasons.json                   the list of call reasons (editable in Notepad)
    records/2026-10-11/230966.json one file per employee per day (list of calls)
    logs/audit-2026-10-11.jsonl    append-only audit log, one JSON object per line

Safety rules:
* every change is written to a temp file and swapped in with os.replace(), so a
  reader never sees a half-written file;
* every read-modify-write happens under a lock (thread lock + OS file lock in ``.locks/``),
  so two requests - even from two server processes - cannot overwrite each other's changes.
  There are 16 locks for call records (chosen by employee+day), so different employees
  saving at the same moment do not wait for each other.

Speed rules (60 people use this at once over a network share):
* parsed files are cached in memory and re-checked against the share at most every few seconds
  (every 3 s for today's files, every 30 s for older days; writes made by this process show up at once);
* admin aggregates are computed once and shared by everybody who asks within ~2 seconds;
* every past day is summarised once (a "rollup"), so a month view merges 30 small summaries instead of
  re-reading ~140,000 calls - and cold reads from the share run in parallel.
"""
import contextlib
import json
import os
import re
import secrets
import threading
import time
import uuid
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from . import branches as branch_list

try:  # Windows
    import msvcrt
    fcntl = None
except ImportError:  # Linux / macOS (used for development and tests)
    msvcrt = None
    import fcntl

UID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

STATUSES = ("resolved", "transferred", "ticket_inquiry")
STATUS_LABELS = {
    "resolved": "Resolved",
    "transferred": "Transferred",
    "ticket_inquiry": "Ticket Inquiry",
}

# The four "Action Taken" values of the original Excel sheet and the Status each one maps to.
DEFAULT_REASONS = [
    {"key": "answered", "label_ar": "تم الرد على الاستفسار",
     "label_en": "Inquiry answered", "status": "resolved"},
    {"key": "solved", "label_ar": "تم حل المشكلة",
     "label_en": "Problem solved", "status": "resolved"},
    {"key": "transferred", "label_ar": "تم التحويل للجهة المختصة",
     "label_en": "Transferred to the competent party", "status": "transferred"},
    {"key": "followup", "label_ar": "متابعة طلب قائم",
     "label_en": "Follow-up on an existing request", "status": "ticket_inquiry"},
]


class StorageError(Exception):
    pass


def norm_uid(value):
    return (value or "").strip().lower()


def valid_uid(value):
    return bool(UID_RE.match(value or ""))


def valid_date(value):
    if not DATE_RE.match(value or ""):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def date_range(d_from, d_to):
    cur, end = date.fromisoformat(d_from), date.fromisoformat(d_to)
    while cur <= end:
        yield cur.isoformat()
        cur += timedelta(days=1)


def parse_record_id(rid):
    """'2026-10-11_230966_ab12cd' -> ('2026-10-11', '230966'); None if malformed."""
    if not isinstance(rid, str) or len(rid) < 13:
        return None
    day, sep, rest = rid[:10], rid[10:11], rid[11:]
    uid, sep2, rand = rest.rpartition("_")
    if sep != "_" or not sep2 or not rand or not valid_date(day) or not valid_uid(uid):
        return None
    return day, uid


class _WriteLock:
    """Thread lock + OS-level file lock. Not re-entrant: never nest."""

    def __init__(self, path):
        self.path = str(path)
        self._thread_lock = threading.Lock()

    @contextlib.contextmanager
    def __call__(self, timeout=30):
        with self._thread_lock:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                self._acquire(fd, timeout)
                try:
                    yield
                finally:
                    self._release(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _acquire(fd, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                if msvcrt:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise StorageError("Could not lock the data folder (another server busy?).")
                time.sleep(0.05)

    @staticmethod
    def _release(fd):
        if msvcrt:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader has the file open for a moment
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


class Store:
    CACHE_LIMIT = 2500          # parsed day-files kept in memory
    TTL_TODAY = 3.0             # seconds before today's files are re-checked on the share
    TTL_OLD = 30.0              # ... and older days'
    TTL_SMALL = 2.0             # users.json
    TTL_LISTS = 10.0            # reasons.json, branches.json
    SHARE_TTL = 2.0             # how long a computed admin result is shared
    ROLLUP_TTL_OLD = 300.0      # a past day's summary is re-built at most every 5 minutes (edits here: at once)
    IO_THREADS = 16             # parallel reads when a lot of files are needed

    def __init__(self, root):
        self.root = Path(root)
        self.users_path = self.root / "users.json"
        self.reasons_path = self.root / "reasons.json"
        self.branches_path = self.root / "branches.json"
        self.records_dir = self.root / "records"
        self.logs_dir = self.root / "logs"
        self.locks_dir = self.root / ".locks"
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._cache = OrderedDict()        # path -> (signature, data, checked_at)
        self._dirs = {}                    # path -> (checked_at, names)
        self._range = OrderedDict()        # (from, to, uid) -> (computed_at, records)
        self._memo = OrderedDict()         # key -> (computed_at, value)
        self._rollups = OrderedDict()      # (day, version) -> (computed_at, summary)
        self._pool = ThreadPoolExecutor(max_workers=self.IO_THREADS, thread_name_prefix="calllog-io")
        self._cache_lock = threading.Lock()
        self._compute_lock = threading.RLock()   # re-entrant: one shared result may be built from another

    # ------------------------------------------------------------------ setup
    def init(self):
        """Create the folders and prove we can write there. Raises StorageError."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.records_dir.mkdir(exist_ok=True)
            self.logs_dir.mkdir(exist_ok=True)
            self.locks_dir.mkdir(exist_ok=True)
            probe = self.root / f".probe-{uuid.uuid4().hex[:6]}"
            probe.write_text("ok")
            probe.unlink()
            if not self.reasons_path.exists():
                _atomic_write_json(self.reasons_path, DEFAULT_REASONS)
            if not self.branches_path.exists():
                _atomic_write_json(self.branches_path, branch_list.load_default())
        except OSError as exc:
            raise StorageError(
                f"Cannot use the data folder '{self.root}': {exc}. "
                "Check that the shared folder is reachable and this account can write to it."
            ) from exc

    def _lock(self, name):
        with self._locks_guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = self._locks[name] = _WriteLock(self.locks_dir / f"{name}.lock")
            return lock

    @staticmethod
    def _record_lock_name(day, uid):
        return "r%02d" % (zlib.crc32(f"{day}/{uid}".encode()) % 16)

    # ------------------------------------------------------------- file reads
    def _read_json(self, path, default, fresh=False, ttl=0.0, keep=True):
        """Parsed JSON, cached. Re-checked on the share only every ``ttl`` seconds. Treat as read-only.
        keep=False: do not add a newly read file to the cache (bulk summaries must not fill the memory)."""
        key, now = str(path), time.monotonic()
        hit = None
        if not fresh:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit and ttl and now - hit[2] < ttl:
                    self._cache.move_to_end(key)
                    return hit[1]
        try:
            st = os.stat(path)
        except FileNotFoundError:
            with self._cache_lock:
                self._cache.pop(key, None)
            return default
        sig = (st.st_mtime_ns, st.st_size)
        if hit and hit[0] == sig:
            with self._cache_lock:
                self._cache[key] = (sig, hit[1], now)
                self._cache.move_to_end(key)
            return hit[1]
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise StorageError(f"{path} is damaged and cannot be read: {exc}") from exc
        if not fresh and keep:
            with self._cache_lock:
                self._cache[key] = (sig, data, now)
                while len(self._cache) > self.CACHE_LIMIT:
                    self._cache.popitem(last=False)
        return data

    def _names(self, path, ttl):
        """Names inside a folder, cached for ``ttl`` seconds."""
        key, now = str(path), time.monotonic()
        with self._cache_lock:
            hit = self._dirs.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        try:
            names = sorted(e.name for e in os.scandir(path))
        except FileNotFoundError:
            names = []
        with self._cache_lock:
            if len(self._dirs) > 500:
                self._dirs.clear()
            self._dirs[key] = (now, names)
        return names

    @staticmethod
    def _day_ttl(day):
        return Store.TTL_TODAY if day >= date.today().isoformat() else Store.TTL_OLD

    def _write_json(self, path, data):
        _atomic_write_json(path, data)
        with self._cache_lock:        # what this process just wrote is visible immediately
            self._cache.pop(str(path), None)
            self._dirs.pop(str(path.parent), None)
            self._dirs.pop(str(self.records_dir), None)
            self._range.clear()
            self._memo.clear()
            if path.parent.parent == self.records_dir:     # a call of that day changed: its summary is stale
                for k in [k for k in self._rollups if k[0] == path.parent.name]:
                    del self._rollups[k]

    def memo(self, key, compute, ttl=None):
        """Compute ``compute()`` once and share the result for a moment (heavy admin aggregates)."""
        ttl = self.SHARE_TTL if ttl is None else ttl
        with self._cache_lock:
            hit = self._memo.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        with self._compute_lock:      # everybody else waits for the first one, then reuses its result
            with self._cache_lock:
                hit = self._memo.get(key)
                if hit and time.monotonic() - hit[0] < ttl:
                    return hit[1]
            value = compute()
            with self._cache_lock:
                self._memo[key] = (time.monotonic(), value)
                while len(self._memo) > 40:
                    self._memo.popitem(last=False)
            return value

    # ---------------------------------------------------------------- reasons
    def reasons(self):
        data = self._read_json(self.reasons_path, DEFAULT_REASONS, ttl=self.TTL_LISTS)
        return [r for r in data if isinstance(r, dict) and r.get("key")]

    # --------------------------------------------------------------- branches
    def branches(self):
        return self._read_json(self.branches_path, [], ttl=self.TTL_LISTS)

    def branch_by_id(self, branch_id):
        items = self.branches()
        cached = getattr(self, "_branch_index", None)
        if cached is None or cached[0] is not items:      # list reloaded -> rebuild the lookup
            cached = self._branch_index = (items, {b["id"]: b for b in items})
        return cached[1].get(branch_id)

    def replace_branches(self, entries):
        with self._lock("branches")():
            self._write_json(self.branches_path, entries)

    # ------------------------------------------------------------------ users
    def users(self):
        return self._read_json(self.users_path, {}, ttl=self.TTL_SMALL)

    def get_user(self, uid):
        return self.users().get(norm_uid(uid))

    def create_user(self, user):
        uid = user["user_id"]
        with self._lock("users")():
            users = dict(self._read_json(self.users_path, {}, fresh=True))
            if uid in users:
                raise StorageError("That User ID already exists.")
            users[uid] = user
            self._write_json(self.users_path, users)
        return user

    def create_users(self, new_users):
        """Add many users at once. Returns (created, duplicates)."""
        created, dup = [], []
        with self._lock("users")():
            users = dict(self._read_json(self.users_path, {}, fresh=True))
            for user in new_users:
                if user["user_id"] in users:
                    dup.append(user["user_id"])
                else:
                    users[user["user_id"]] = user
                    created.append(user)
            if created:
                self._write_json(self.users_path, users)
        return created, dup

    def update_user(self, uid, changes, guard=None):
        """Merge ``changes`` into a user. ``guard(users, new_user)`` may raise to veto."""
        uid = norm_uid(uid)
        with self._lock("users")():
            users = dict(self._read_json(self.users_path, {}, fresh=True))
            if uid not in users:
                return None
            before = dict(users[uid])
            after = {**before, **changes}
            if guard:
                guard(users, after)
            users[uid] = after
            self._write_json(self.users_path, users)
        return before, after

    # ---------------------------------------------------------------- records
    def _day_path(self, day, uid):
        return self.records_dir / day / f"{uid}.json"

    def day_records(self, day, uid):
        return self._read_json(self._day_path(day, uid), [], ttl=self._day_ttl(day))

    def add_record(self, record):
        path = self._day_path(record["date"], record["user_id"])
        with self._lock(self._record_lock_name(record["date"], record["user_id"]))():
            items = list(self._read_json(path, [], fresh=True))
            items.append(record)
            self._write_json(path, items)
        return record

    def get_record(self, rid):
        parsed = parse_record_id(rid)
        if not parsed:
            return None
        day, uid = parsed
        for rec in self.day_records(day, uid):
            if rec["id"] == rid:
                return rec
        return None

    def update_record(self, rid, changes):
        """Apply ``changes`` to a record. Returns (before, after) or None if not found."""
        parsed = parse_record_id(rid)
        if not parsed:
            return None
        path = self._day_path(*parsed)
        with self._lock(self._record_lock_name(*parsed))():
            items = list(self._read_json(path, [], fresh=True))
            for i, rec in enumerate(items):
                if rec["id"] == rid:
                    after = {**rec, **changes}
                    items[i] = after
                    self._write_json(path, items)
                    return rec, after
        return None

    def delete_record(self, rid):
        parsed = parse_record_id(rid)
        if not parsed:
            return None
        path = self._day_path(*parsed)
        with self._lock(self._record_lock_name(*parsed))():
            items = list(self._read_json(path, [], fresh=True))
            for i, rec in enumerate(items):
                if rec["id"] == rid:
                    del items[i]
                    self._write_json(path, items)
                    return rec
        return None

    def records_between(self, d_from, d_to, uid=None):
        """Every record with d_from <= date <= d_to (ISO strings), optionally one employee.

        The returned list is shared between requests for ~2 s - never modify it."""
        key = (d_from, d_to, uid)
        with self._cache_lock:
            hit = self._range.get(key)
            if hit and time.monotonic() - hit[0] < self.SHARE_TTL:
                return hit[1]
        with self._compute_lock:
            with self._cache_lock:
                hit = self._range.get(key)
                if hit and time.monotonic() - hit[0] < self.SHARE_TTL:
                    return hit[1]
            paths = []
            for day in self.days_with_data(d_from, d_to):
                if uid:
                    paths.append((self._day_path(day, uid), self._day_ttl(day)))
                    continue
                ttl = self._day_ttl(day)
                paths += [(self.records_dir / day / name, ttl) for name in self._names(self.records_dir / day, ttl)
                          if name.endswith(".json") and not name.startswith(".")]
            out = []
            for items in self._pool.map(lambda pt: self._read_json(pt[0], [], ttl=pt[1]), paths):
                out.extend(items)
            with self._cache_lock:
                self._range[key] = (time.monotonic(), out)
                while len(self._range) > 24:
                    self._range.popitem(last=False)
            return out

    def days_with_data(self, d_from, d_to):
        """Dates in the range that have a records folder, oldest first."""
        return [d for d in self._names(self.records_dir, 5.0) if DATE_RE.match(d) and d_from <= d <= d_to]

    def day_all(self, day, keep=False):
        """All employees' calls of one day (files are read in parallel)."""
        ttl = self._day_ttl(day)
        paths = [self.records_dir / day / n for n in self._names(self.records_dir / day, ttl)
                 if n.endswith(".json") and not n.startswith(".")]
        out = []
        for items in self._pool.map(lambda p: self._read_json(p, [], ttl=ttl, keep=keep), paths):
            out.extend(items)
        return out

    def rollup(self, day, version, build):
        """Summary of one day: ``build(records)`` runs once, then is reused (3 s for today, 5 min for past days).

        ``version`` changes when the meaning of the data changes (the reason->status table)."""
        key = (day, version)
        ttl = self.SHARE_TTL if day >= date.today().isoformat() else self.ROLLUP_TTL_OLD
        with self._cache_lock:
            hit = self._rollups.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        summary = build(self.day_all(day, keep=day >= date.today().isoformat()))
        with self._cache_lock:
            self._rollups[key] = (time.monotonic(), summary)
            while len(self._rollups) > 400:
                self._rollups.popitem(last=False)
        return summary

    # ------------------------------------------------------------------ audit
    def audit(self, entry):
        """Append one line to today's audit file. Never raises into the request."""
        day = entry["ts"][:10]
        path = self.logs_dir / f"audit-{day}.jsonl"
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:  # one audit lock: appends from two PCs must not interleave
            with self._lock("audit")():
                with open(path, "ab") as f:
                    f.write(line.encode("utf-8"))
        except (OSError, StorageError):
            pass

    def read_audit(self, d_from, d_to):
        out = []
        for day in date_range(d_from, d_to):
            path = self.logs_dir / f"audit-{day}.jsonl"
            if not path.is_file():
                continue
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        return out


def new_record_id(day, uid):
    return f"{day}_{uid}_{secrets.token_hex(3)}"


def iso_now(now):
    return now.replace(microsecond=0).isoformat(sep=" ")

