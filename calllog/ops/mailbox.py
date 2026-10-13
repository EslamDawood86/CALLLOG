"""Saved copies of mails: the files the collector writes and the web pages read (docs/FEATURES-V2.md, B1 and B2).

The web app cannot talk to Outlook, so the collector saves what the pages need into ``<data_dir>/ops/mail``::

    <key>.msg    the whole mail as Outlook saves it (Outlook ``item.SaveAs(path, 3)``), downloaded by people as it is
    <key>.json   the preview: subject, people, plain-text body (<= 30,000 characters), attachment names and sizes

``key = sha1(message_id).hexdigest()[:32]``. The two file names are built from that hash ONLY - never from text a person
typed - so a message id such as ``../../x`` cannot reach any other file. Both files are written atomically (temp file in the
same folder + rename), the ``.msg`` first and the ``.json`` last, so a preview that exists always has its ``.msg`` (or says
``msg_bytes: 0`` = "too large, preview only").

This module has no Flask and no Outlook: it is shared by the collector (which writes), the web app (which reads) and the
daily housekeeping (which prunes). Python 3.9 compatible.
"""
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from datetime import date, datetime
from pathlib import Path

from ..storage import _atomic_write_json
from . import schemas

KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_FILE_RE = re.compile(r"^([0-9a-f]{32})\.(msg|json)$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
STATE_NAME = "mail-copies"                   # ops/state/mail-copies.json: when each copy was first seen "left the set"
ORPHAN_GRACE_SECONDS = 3600                  # a half-written pair is left alone for an hour before it counts as an orphan
MAX_PEOPLE = 50                              # recipients listed in a preview
MAX_ATTACHMENTS = 100
MSG_MIME = "application/vnd.ms-outlook"


# ----------------------------------------------------------------------------------------------------------- names
def mail_key(message_id):
    """The 32-character name of a mail's files. Any text is accepted; the answer is always lower-case hex."""
    return hashlib.sha1(str(message_id).encode("utf-8", "replace")).hexdigest()[:32]


def _checked(key):
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise ValueError("bad mail key")
    return key


def msg_path(directory, key):
    return Path(directory) / f"{_checked(key)}.msg"


def preview_path(directory, key):
    return Path(directory) / f"{_checked(key)}.json"


def list_files(directory):
    """{file name: size in bytes} of the files that belong to copies (one directory listing, no per-file stat)."""
    out = {}
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if _FILE_RE.match(entry.name):
                    try:
                        out[entry.name] = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
    except OSError:
        pass
    return out


# ------------------------------------------------------------------------------------------------- what to copy
def _stamp(value):
    return str(value or "").replace(" ", "T")


def wanted_mails(rows, assignments, scope):
    """The mails that should have a saved copy, most important first: ``[(message_id, reason, row)]``.

    * ``scope`` ``"off"`` -> nothing; ``"assigned"`` -> the mails with an entry in ``assignments``;
      ``"assigned_and_pending"`` -> those plus the rows still waiting for a reply.
    * order: assigned and not done first, then assigned and done, then the oldest pending first (each mail once, even when
      the collector split it into one row per team).
    ``rows`` are the snapshot rows (a dict each); ``assignments`` is the override list ``{message_id: entry}``."""
    if scope not in schemas.MAIL_COPY_SCOPES or scope == "off":
        return []
    assignments = assignments if isinstance(assignments, dict) else {}
    seen, assigned, pending = set(), [], []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        message_id = str(row.get("message_id") or "")
        if not message_id or message_id in seen:
            continue
        if message_id in assignments:
            seen.add(message_id)
            done = bool((assignments[message_id] or {}).get("done_at"))
            assigned.append((done, _stamp(row.get("received_time")), message_id, row))
        elif scope == "assigned_and_pending" and row.get("status") == schemas.EMAIL_PENDING:
            seen.add(message_id)
            pending.append((_stamp(row.get("received_time")), message_id, row))
    assigned.sort(key=lambda t: t[:3])
    pending.sort(key=lambda t: t[:2])
    return ([(m, "assigned", r) for _, _, m, r in assigned] + [(m, "pending", r) for _, m, r in pending])


# ----------------------------------------------------------------------------------------------------- the preview
def clip(value, limit):
    return str(value or "").strip()[:limit]


def clean_body(text):
    """Plain text safe to keep and show: no NULs / control characters, one kind of line break."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", text)


def _people(values):
    if isinstance(values, str):
        values = [p for p in re.split(r"[;\n]", values)]
    out = []
    for value in values or []:
        text = clip(value, 200)
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_PEOPLE:
            break
    return out


def make_preview(message_id, subject="", from_name="", from_email="", to=(), cc=(), received=None, body="",
                 attachments=(), saved_at=None, msg_bytes=0, too_large=False):
    """The preview document of one mail (every field cut to its limit). ``received`` / ``saved_at``: datetime or ISO text."""
    text = clean_body(body)
    truncated = len(text) > schemas.MAIL_BODY_CHARS
    files = []
    for att in list(attachments or [])[:MAX_ATTACHMENTS]:
        if isinstance(att, dict):
            name, size = att.get("name"), att.get("size")
        elif isinstance(att, (tuple, list)) and len(att) == 2:
            name, size = att
        else:
            name, size = att, 0
        try:
            size = max(0, int(size or 0))
        except (TypeError, ValueError):
            size = 0
        files.append({"name": clip(name, 200) or "attachment", "size": size})

    def iso(value):
        if isinstance(value, datetime):
            return value.isoformat(timespec="seconds")
        return str(value or "") or None
    return {"message_id": clip(message_id, 500), "subject": clip(subject, 300), "from_name": clip(from_name, 200),
            "from_email": clip(from_email, 200).lower(), "to": _people(to), "cc": _people(cc), "received": iso(received),
            "body_text": text[:schemas.MAIL_BODY_CHARS], "truncated": truncated, "attachments": files,
            "saved_at": iso(saved_at or datetime.now()), "msg_bytes": max(0, int(msg_bytes or 0)),
            "too_large": bool(too_large)}


# ------------------------------------------------------------------------------------------------- reading copies
def read_preview(directory, key):
    """The preview document, or None when there is none (or it is damaged: the next scan writes it again)."""
    try:
        with open(preview_path(directory, key), "rb") as f:
            raw = f.read(2_000_000)
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    size = doc.get("msg_bytes", 0)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:      # a promise we cannot read is a damaged preview
        return None
    return doc


def load_copy(directory, key):
    """``(preview or None, has_msg)``. ``has_msg`` = the .msg file really exists and the preview says it should."""
    preview = read_preview(directory, key)
    if not preview:
        return None, False
    try:
        size = int(preview.get("msg_bytes") or 0)
    except (TypeError, ValueError):
        size = 0
    has_msg = size > 0 and msg_path(directory, key).is_file()
    return preview, has_msg


def copy_complete(files, key, directory=None):
    """Is the copy of ``key`` finished? ``files`` = ``list_files(directory)``. A preview with ``msg_bytes: 0`` (too large) is
    complete without a .msg; a preview that promises a .msg which is missing is not (the scan saves it again)."""
    if f"{key}.json" not in files:
        return False
    if f"{key}.msg" in files:
        return True
    preview = read_preview(directory, key) if directory is not None else None
    return bool(preview) and not int(preview.get("msg_bytes") or 0)


# ------------------------------------------------------------------------------------------------- writing copies
def _replace(tmp, path):
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                      # Windows: a reader has the file open for a moment
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def write_copy(directory, key, preview, msg_file=None):
    """Publish one copy: the .msg (copied from the local ``msg_file``) first, then the preview. Both appear atomically.
    Returns the preview as written (``msg_bytes`` filled in)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    final_msg, final_json = msg_path(directory, key), preview_path(directory, key)
    preview = dict(preview)
    if msg_file:
        tmp = directory / f".{key}.msg.{secrets.token_hex(4)}.tmp"
        try:
            shutil.copyfile(msg_file, tmp)
            preview["msg_bytes"] = tmp.stat().st_size
            _replace(tmp, final_msg)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
    else:
        preview["msg_bytes"] = 0
        with contextlib.suppress(OSError):
            final_msg.unlink()                       # an older, bigger copy must not stay next to a "too large" preview
    _atomic_write_json(final_json, preview)
    return preview


def remove_copy(directory, key):
    removed = 0
    for path in (msg_path(directory, key), preview_path(directory, key)):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# ----------------------------------------------------------------------------------------------------- housekeeping
def housekeeping(store, wanted_keys, keep_days, today=None, now=None):
    """Delete what is no longer needed (called once a day by the collector's housekeeping).

    * a copy whose mail is not in ``wanted_keys`` (assigned or still pending) for ``keep_days`` days: both files;
      the day it was first seen outside the set is kept in ``ops/state/mail-copies.json``, and a mail that comes back
      (re-assigned, pending again) is forgotten from that list;
    * a .msg without a preview, or a preview that promises a .msg which is gone, once an hour old (so a copy that is being
      written right now is never touched);
    * left-over temp files older than an hour.
    Returns ``{"copies": N, "removed": N, "orphans": N, "temp": N, "bytes": N}``."""
    directory = Path(store.mail_dir)
    today = today or date.today()
    now_ts = (now.timestamp() if isinstance(now, datetime) else time.time())
    result = {"copies": 0, "removed": 0, "orphans": 0, "temp": 0, "bytes": 0}
    try:
        names = [(e.name, e.stat(follow_symlinks=False)) for e in os.scandir(directory) if e.is_file(follow_symlinks=False)]
    except OSError:
        return result
    keys, files = {}, {}
    for name, st in names:
        if name.startswith(".") and name.endswith(".tmp"):
            if now_ts - st.st_mtime > ORPHAN_GRACE_SECONDS:
                with contextlib.suppress(OSError):
                    (directory / name).unlink()
                    result["temp"] += 1
            continue
        match = _FILE_RE.match(name)
        if match:
            keys.setdefault(match.group(1), {})[match.group(2)] = st
            files[name] = st
    try:
        state = store.read_state(STATE_NAME) or {}
    except Exception:                                   # a damaged state file only restarts the countdown
        state = {}
    left = dict(state.get("left") or {}) if isinstance(state.get("left"), dict) else {}
    wanted = set(wanted_keys or ())
    for key, parts in keys.items():
        old = (now_ts - max(st.st_mtime for st in parts.values())) > ORPHAN_GRACE_SECONDS
        if "json" not in parts:
            if old:                                     # a .msg that never got its preview
                result["orphans"] += remove_copy(directory, key)
            continue
        if "msg" not in parts and old:
            preview = read_preview(directory, key)
            if preview and int(preview.get("msg_bytes") or 0) > 0:   # promises a .msg that is gone
                result["orphans"] += remove_copy(directory, key)
                left.pop(key, None)
                continue
        if key in wanted:
            left.pop(key, None)
            result["copies"] += 1
            result["bytes"] += sum(st.st_size for st in parts.values())
            continue
        since = left.get(key)
        try:
            since_day = date.fromisoformat(since) if since else None
        except (TypeError, ValueError):
            since_day = None
        if since_day is None:
            left[key] = today.isoformat()
        elif (today - since_day).days >= keep_days:
            result["removed"] += remove_copy(directory, key)
            left.pop(key, None)
            continue
        result["copies"] += 1
        result["bytes"] += sum(st.st_size for st in parts.values())
    left = {k: v for k, v in left.items() if k in keys}  # forget copies that are gone
    try:
        store.write_state(STATE_NAME, {"day": today.isoformat(), "left": left})
    except Exception:                                   # noqa: BLE001 - housekeeping must never fail a collector
        pass
    return result


def wanted_keys_from(store):
    """The keys housekeeping must keep: every assigned mail plus the pending rows of the latest e-mail snapshot."""
    assignments = store.overrides("assignments")
    snap = store.read_snapshot("email") or {}
    rows = (snap.get("data") or {}).get("rows") or []
    keys = {mail_key(m) for m in assignments}
    for row in rows:
        if isinstance(row, dict) and row.get("status") == schemas.EMAIL_PENDING and row.get("message_id"):
            keys.add(mail_key(row["message_id"]))
    return keys


def folder_summary(directory):
    """``{"mails": N, "bytes": N, "oldest": ISO or None}`` for the System page (one directory listing)."""
    mails, size, oldest = set(), 0, None
    try:
        with os.scandir(directory) as it:
            for entry in it:
                match = _FILE_RE.match(entry.name)
                if not match:
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                mails.add(match.group(1))
                size += st.st_size
                oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
    except OSError:
        pass
    return {"mails": len(mails), "bytes": size,
            "oldest": datetime.fromtimestamp(oldest).isoformat(timespec="seconds") if oldest else None}


# ------------------------------------------------------------------------------------------------ download names
_BAD_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*;]+')


def download_name(subject, limit=60):
    """File name for the browser: the subject without anything a file system or a header dislikes, <= ``limit`` characters,
    always ending in .msg. Returns ``(name, ascii_fallback)``; the second is plain ASCII for old browsers."""
    text = _BAD_NAME_CHARS.sub(" ", str(subject or ""))
    text = " ".join(text.split()).strip(" .")[:limit].strip(" .") or "e-mail"
    reserved = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
    if text.split(".")[0].lower() in reserved:
        text = "_" + text
    ascii_name = re.sub(r"[^A-Za-z0-9 ._()\-]+", "_", text).strip(" .") or "e-mail"
    return f"{text}.msg", f"{ascii_name}.msg"
