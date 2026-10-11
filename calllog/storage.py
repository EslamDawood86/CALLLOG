"""Plain-file storage for the shared folder - no database server needed.

Layout inside the data folder::

    users.json                     all accounts
    reasons.json                   the list of call reasons (editable in Notepad)
    records/2026-10-11/230966.json one file per employee per day (list of calls)
    logs/audit-2026-10-11.jsonl    append-only audit log, one JSON object per line

Safety rules:
* every change is written to a temp file and swapped in with os.replace(), so a
  reader never sees a half-written file;
* every read-modify-write happens under one lock (thread lock + OS file lock on
  ``.write.lock``), so two requests - even from two server processes - cannot
  overwrite each other's changes.
"""
import contextlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path

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
    CACHE_LIMIT = 3000

    def __init__(self, root):
        self.root = Path(root)
        self.users_path = self.root / "users.json"
        self.reasons_path = self.root / "reasons.json"
        self.records_dir = self.root / "records"
        self.logs_dir = self.root / "logs"
        self._write_lock = _WriteLock(self.root / ".write.lock")
        self._cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._audit_lock = threading.Lock()

    # ------------------------------------------------------------------ setup
    def init(self):
        """Create the folders and prove we can write there. Raises StorageError."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.records_dir.mkdir(exist_ok=True)
            self.logs_dir.mkdir(exist_ok=True)
            probe = self.root / f".probe-{uuid.uuid4().hex[:6]}"
            probe.write_text("ok")
            probe.unlink()
            if not self.reasons_path.exists():
                _atomic_write_json(self.reasons_path, DEFAULT_REASONS)
        except OSError as exc:
            raise StorageError(
                f"Cannot use the data folder '{self.root}': {exc}. "
                "Check that the shared folder is reachable and this account can write to it."
            ) from exc

    # ------------------------------------------------------------- file reads
    def _read_json(self, path, default, fresh=False):
        """Parsed JSON, cached until the file's mtime/size changes. Treat as read-only."""
        key = str(path)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return default
        sig = (st.st_mtime_ns, st.st_size)
        if not fresh:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit and hit[0] == sig:
                    self._cache.move_to_end(key)
                    return hit[1]
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise StorageError(f"{path} is damaged and cannot be read: {exc}") from exc
        if not fresh:
            with self._cache_lock:
                self._cache[key] = (sig, data)
                while len(self._cache) > self.CACHE_LIMIT:
                    self._cache.popitem(last=False)
        return data

    def _write_json(self, path, data):
        _atomic_write_json(path, data)
        with self._cache_lock:
            self._cache.pop(str(path), None)

    # ---------------------------------------------------------------- reasons
    def reasons(self):
        data = self._read_json(self.reasons_path, DEFAULT_REASONS)
        return [r for r in data if isinstance(r, dict) and r.get("key")]

    # ------------------------------------------------------------------ users
    def users(self):
        return self._read_json(self.users_path, {})

    def get_user(self, uid):
        return self.users().get(norm_uid(uid))

    def create_user(self, user):
        uid = user["user_id"]
        with self._write_lock():
            users = dict(self._read_json(self.users_path, {}, fresh=True))
            if uid in users:
                raise StorageError("That User ID already exists.")
            users[uid] = user
            self._write_json(self.users_path, users)
        return user

    def create_users(self, new_users):
        """Add many users at once. Returns (created, duplicates)."""
        created, dup = [], []
        with self._write_lock():
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
        with self._write_lock():
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
        return self._read_json(self._day_path(day, uid), [])

    def add_record(self, record):
        path = self._day_path(record["date"], record["user_id"])
        with self._write_lock():
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
        with self._write_lock():
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
        with self._write_lock():
            items = list(self._read_json(path, [], fresh=True))
            for i, rec in enumerate(items):
                if rec["id"] == rid:
                    del items[i]
                    self._write_json(path, items)
                    return rec
        return None

    def records_between(self, d_from, d_to, uid=None):
        """Every record with d_from <= date <= d_to (ISO strings), optionally one employee."""
        out = []
        try:
            day_names = sorted(e.name for e in os.scandir(self.records_dir)
                               if e.is_dir() and DATE_RE.match(e.name) and d_from <= e.name <= d_to)
        except FileNotFoundError:
            return out
        for day in day_names:
            if uid:
                out.extend(self.day_records(day, uid))
                continue
            for e in os.scandir(self.records_dir / day):
                if e.name.endswith(".json") and not e.name.startswith("."):
                    out.extend(self._read_json(Path(e.path), []))
        return out

    # ------------------------------------------------------------------ audit
    def audit(self, entry):
        """Append one line to today's audit file. Never raises into the request."""
        day = entry["ts"][:10]
        path = self.logs_dir / f"audit-{day}.jsonl"
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            with self._audit_lock, open(path, "ab") as f:
                f.write(line.encode("utf-8"))
        except OSError:
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

