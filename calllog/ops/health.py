"""Health of the application itself: the data folder, the collectors' files, the accounts and the server process.

``collect`` returns one JSON-friendly dict for ``GET /api/ops/health`` (System page, "Monitoring"). Everything is
read-only apart from a tiny probe file that proves the data folder is writable. The numbers are plain facts; the
``problems`` list turns the worrying ones into sentences so nobody has to read the rest to see that something is wrong.
"""
import os
import platform
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import mailbox, schemas
from .settings import is_active

APP_VERSION = "NBE Operations Hub 1.0"
STARTED_AT = datetime.now()
_START_MONOTONIC = time.monotonic()

LOW_DISK_BYTES = 1_000_000_000
LOW_DISK_PERCENT = 10.0
MAIL_COPIES_WARN_BYTES = 2_000_000_000
_ROLES = ("admin", "manager", "user")


def started_at():
    return STARTED_AT.isoformat(timespec="seconds")


def _minutes_since(stamp, now):
    try:
        return max(0, int((now - datetime.fromisoformat(str(stamp))).total_seconds() // 60))
    except (TypeError, ValueError):
        return None


def collector_online(heartbeat, interval_minutes, now):
    """A collector is online when its heartbeat is younger than 3 x interval + 2 minutes (HUB-DESIGN section 6)."""
    if not isinstance(heartbeat, dict):
        return False
    age = _minutes_since(heartbeat.get("updated_at"), now)
    return age is not None and age < 3 * int(interval_minutes or 0) + 2


def module_interval(settings, module):
    return int((settings.get(module) or {}).get("interval_minutes") or 0)


def job_interval(settings, job):
    return module_interval(settings, schemas.JOB_MODULE[job])


# ------------------------------------------------------------------------------ data folder
def probe_folder(folder):
    """Create, read and delete a small file in ``folder``. Returns {writable, write_ms, error}."""
    path = Path(folder) / f".health-probe-{uuid.uuid4().hex[:8]}"
    started = time.perf_counter()
    try:
        path.write_text("ok", encoding="utf-8")
        ok = path.read_text(encoding="utf-8") == "ok"
        path.unlink()
        return {"writable": ok, "write_ms": round((time.perf_counter() - started) * 1000, 1), "error": None}
    except OSError as exc:
        try:
            path.unlink()
        except OSError:
            pass
        return {"writable": False, "write_ms": None, "error": str(exc)[:200]}


def disk_usage(folder):
    try:
        total, used, free = shutil.disk_usage(folder)
    except OSError as exc:
        return {"total_bytes": None, "used_bytes": None, "free_bytes": None, "free_percent": None, "error": str(exc)[:200]}
    return {"total_bytes": total, "used_bytes": used, "free_bytes": free,
            "free_percent": round(free / total * 100, 1) if total else None, "error": None}


def folder_size(folder, max_files=200_000):
    """{bytes, files} of everything under ``folder`` (stops counting at ``max_files`` and says so)."""
    size = files = 0
    truncated = False
    stack = [str(folder)]
    while stack and not truncated:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            files += 1
                            size += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if files >= max_files:
                        truncated = True
                        break
        except OSError:
            continue
    return {"bytes": size, "files": files, "truncated": truncated}


def _snapshot_ages(ops, now):
    out = {}
    for module in schemas.MODULES:
        try:
            snap = ops.read_snapshot(module)
            problem = None
        except Exception as exc:                      # a damaged file must show up here, not break the page
            snap, problem = None, str(exc)[:200]
        if not snap:
            out[module] = {"present": False, "generated_at": None, "age_minutes": None, "host": "", "problem": problem}
            continue
        path = ops.snapshots_dir / f"{module}.json"
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        out[module] = {"present": True, "generated_at": snap.get("generated_at"), "date": snap.get("date"),
                       "age_minutes": _minutes_since(snap.get("generated_at"), now), "host": snap.get("host", ""),
                       "duration_seconds": snap.get("duration_seconds"), "size_bytes": size, "problem": None}
    return out


def job_state(job, hb, settings, now):
    """What the pages show about one collector job (never more than the heartbeat file holds)."""
    interval = job_interval(settings, job)
    if not isinstance(hb, dict):
        return {"job": job, "present": False, "online": False, "interval_minutes": interval}
    return {"job": job, "present": True, "online": collector_online(hb, interval, now), "interval_minutes": interval,
            "running": bool(hb.get("running")), "enabled": hb.get("enabled", True), "host": hb.get("host", ""),
            "last_status": hb.get("last_status"), "last_error": str(hb.get("last_error") or "")[:300] or None,
            "last_summary": str(hb.get("last_summary") or "")[:300], "last_run_at": hb.get("last_run_at"),
            "last_ok_at": hb.get("last_ok_at"), "next_run_at": hb.get("next_run_at"),
            "last_duration_seconds": hb.get("last_duration_seconds"), "updated_at": hb.get("updated_at"),
            "age_minutes": _minutes_since(hb.get("updated_at"), now), "runs_ok": hb.get("runs_ok", 0),
            "runs_failed": hb.get("runs_failed", 0), "version": hb.get("version", "")}


def job_states(ops, settings, now):
    out = {}
    for job in schemas.JOBS:
        try:
            hb = ops.read_heartbeat(job)
        except Exception:                             # a damaged heartbeat is the same as no heartbeat
            hb = None
        out[job] = job_state(job, hb, settings, now)
    return out


def _users(users):
    counts = {role: {"active": 0, "disabled": 0} for role in _ROLES}
    for user in users.values():
        bucket = counts.setdefault(user.get("role", "user"), {"active": 0, "disabled": 0})
        bucket["active" if user.get("active", True) else "disabled"] += 1
    return {"by_role": counts, "total": len(users),
            "active": sum(b["active"] for b in counts.values()), "disabled": sum(b["disabled"] for b in counts.values())}


def _audit_today(store, day):
    entries = store.read_audit(day, day)
    failed = sum(1 for e in entries if e.get("action") == "login_failed")
    blocked = sum(1 for e in entries if e.get("action") == "login_blocked")
    return {"day": day, "entries": len(entries), "failed_sign_ins": failed, "blocked_sign_ins": blocked}


def _problems(report, settings, now):
    out = []

    def add(level, text):
        out.append({"level": level, "text": text})

    folder = report["data_folder"]
    if not folder["writable"]:
        add("danger", f"The data folder is not writable: {folder['error'] or 'unknown error'}")
    disk = folder["disk"]
    if disk.get("free_bytes") is not None and (
            disk["free_bytes"] < LOW_DISK_BYTES or (disk["free_percent"] or 100) < LOW_DISK_PERCENT):
        add("warn", f"Low disk space on the data folder: {disk['free_bytes'] // 1_000_000} MB free ({disk['free_percent']}%)")
    if folder.get("write_ms") and folder["write_ms"] > 2000:
        add("warn", f"The data folder is slow ({folder['write_ms']:.0f} ms to write a small file)")
    for module, snap in report["snapshots"].items():
        if snap.get("problem"):
            add("danger", f"The {module} snapshot cannot be read: {snap['problem']}")
        if not (settings.get(module) or {}).get("enabled", True):
            continue
        if not snap["present"]:
            add("info", f"No {module} data yet - the collector has not written a snapshot.")
        elif is_active(settings, module, now) and snap["age_minutes"] is not None \
                and snap["age_minutes"] > 3 * max(1, module_interval(settings, module)) + 2:
            add("warn", f"The {module} data is {snap['age_minutes']} minutes old.")
    for job, hb in report["heartbeats"].items():
        module = schemas.JOB_MODULE[job]
        if not (settings.get(module) or {}).get("enabled", True):
            continue
        if hb.get("present") and not hb["online"] and is_active(settings, module, now):
            add("danger", f"The {job} collector is offline (last seen {hb.get('age_minutes')} minutes ago).")
        if hb.get("last_status") == "error":
            add("danger", f"The last {job} run failed: {hb.get('last_error') or 'unknown error'}")
    copies = report.get("mail_copies") or {}
    if copies.get("bytes", 0) > MAIL_COPIES_WARN_BYTES:
        add("warn", f"The saved copies of mails take {copies['bytes'] // 1_000_000} MB - lower email.mail_copies.keep_days or "
                    "max_per_scan in the settings.")
    if report["pending_requests"]["count"] > 5:
        add("warn", f"{report['pending_requests']['count']} 'Run now' requests are waiting - is a collector running?")
    if report["audit_today"]["failed_sign_ins"] >= 10:
        add("warn", f"{report['audit_today']['failed_sign_ins']} failed sign-ins today.")
    return out


def collect(data_dir, ops, store, settings, now, extra=None):
    """The whole health report. ``store`` = the Call Log Store (users, audit), ``ops`` = OpsStore, ``settings`` = the
    loaded Hub settings, ``now`` = naive local datetime of the app clock."""
    folder = {"path": str(data_dir), **probe_folder(data_dir), "disk": disk_usage(data_dir)}
    ops_folder = folder_size(ops.root)
    requests = ops.pending_run_requests()
    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "data_folder": folder,
        "ops_folder": {"path": str(ops.root), **ops_folder},
        "records_days": _count_dirs(Path(data_dir) / "records"),
        "mail_copies": {**mailbox.folder_summary(ops.mail_dir),
                        "scope": ((settings.get("email") or {}).get("mail_copies") or {}).get("scope", "")},
        "notifications": {"people": _count_files(ops.notifications_dir, ".json")},
        "snapshots": _snapshot_ages(ops, now),
        "heartbeats": job_states(ops, settings, now),
        "pending_requests": {"count": len(requests),
                             "items": [{"job": r.get("job"), "requested_by": r.get("requested_by", ""),
                                        "requested_at": r.get("requested_at")} for r in requests[:20]]},
        "users": _users(store.users()),
        "audit_today": _audit_today(store, now.date().isoformat()),
        "server": {"started_at": started_at(), "uptime_seconds": int(time.monotonic() - _START_MONOTONIC),
                   "python": sys.version.split()[0], "platform": platform.platform(), "pid": os.getpid(),
                   "threads": threading.active_count(), "version": APP_VERSION},
    }
    if extra:
        report["server"].update(extra)
    report["problems"] = _problems(report, settings, now)
    levels = {p["level"] for p in report["problems"]}
    report["status"] = "error" if "danger" in levels else "warn" if "warn" in levels else "ok"
    return report


def _count_files(folder, suffix):
    try:
        with os.scandir(folder) as it:
            return sum(1 for e in it if e.name.endswith(suffix) and e.is_file(follow_symlinks=False))
    except OSError:
        return 0


def _count_dirs(folder):
    try:
        with os.scandir(folder) as it:
            return sum(1 for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return 0
