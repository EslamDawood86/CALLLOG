"""``collector_config.json`` - the file YOU edit in Notepad on the PC that runs the collector.

It holds what must not live in the shared settings: the portal / ITSM accounts **with their passwords**, and any
machine-specific value such as the Outlook archive name to scan. It has the same shape as the settings, plus
``username`` / ``password`` inside ``sla`` and ``itsm``::

    {
      "sla":   {"username": "1001", "password": "...", "base_url": "https://nbesrv.nbe.ahly.bank"},
      "itsm":  {"username": "1001", "password": "..."},
      "email": {"target_archive_name": "2026_Part3"}
    }

Every key written here **wins** over the same key in the settings saved from the web page (the System page shows
which ones are pinned). ``password`` is never copied into the settings, a snapshot, a log or the web app: only the
collector reads this file; the web server never opens it. Keys starting with ``_`` are comments and are ignored.

Password order: environment variable (NBE_SSO_PASSWORD / ITSM_PASS)  >  this file  >  Windows Credential Manager.
"""
import json
import os
from pathlib import Path

from ..config import APP_DIR, load_config
from .settings import deep_merge

FILE_NAME = "collector_config.json"
SECTIONS = ("general", "email", "sla", "itsm")
ACCOUNT_KINDS = ("sla", "itsm")
SECRET_KEYS = ("password",)


class CollectorConfigError(Exception):
    pass


def default_path():
    return Path(os.environ.get("CALLLOG_COLLECTOR_CONFIG") or APP_DIR / FILE_NAME)


def _strip_comments(value):
    if isinstance(value, dict):
        return {k: _strip_comments(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [_strip_comments(v) for v in value]
    return value


def _flatten(prefix, value, out):
    if isinstance(value, dict) and value:
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else k, v, out)
    else:
        out.append(prefix)


class CollectorConfig:
    def __init__(self, data=None, path=None):
        self.path = Path(path) if path else None
        self.data = _strip_comments(data or {})

    @property
    def exists(self):
        return bool(self.path and self.path.is_file())

    # ------------------------------------------------------------- accounts
    def _account(self, kind):
        section = self.data.get(kind)
        return section if isinstance(section, dict) else {}

    def username(self, kind):
        return str(self._account(kind).get("username") or "").strip()

    def file_password(self, kind):
        return str(self._account(kind).get("password") or "")

    def has_password(self, kind):
        return bool(self.file_password(kind))

    # ------------------------------------------------------------- data dir
    def data_dir(self, default=None):
        """Where the data folder is. The file may point somewhere else; otherwise the same as the web app's."""
        wanted = self.data.get("data_dir")
        if wanted:
            p = Path(str(wanted))
            return str(p if p.is_absolute() else APP_DIR / p)
        return default if default is not None else load_config()["data_dir"]

    # ------------------------------------------------------------- settings
    def overlay(self):
        """The settings keys this file pins, without the secrets: {section: {...}}."""
        out = {}
        for section in SECTIONS:
            values = self.data.get(section)
            if isinstance(values, dict):
                out[section] = {k: v for k, v in values.items() if k not in SECRET_KEYS}
        return out

    def pinned_keys(self):
        """Dotted names of the settings pinned by this file, e.g. ['email.target_archive_name', 'sla.username']."""
        keys = []
        _flatten("", self.overlay(), keys)
        return sorted(k for k in keys if k)

    def apply(self, settings):
        """``settings`` (already merged with defaults) with this file's keys on top. Validate afterwards."""
        return deep_merge(settings, self.overlay())


def load(path=None):
    """Read the file (missing file = empty config). Raises CollectorConfigError with a readable message if broken."""
    path = Path(path) if path else default_path()
    if not path.is_file():
        return CollectorConfig({}, path)
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise CollectorConfigError(
            f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}. "
            "Check commas and quotes - every text must be inside double quotes, and backslashes in paths are doubled "
            "(C:\\\\Program Files\\\\...).") from exc
    except OSError as exc:
        raise CollectorConfigError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CollectorConfigError(f"{path} must contain a JSON object ({{ ... }}).")
    return CollectorConfig(data, path)


def get_password(cc, kind, username=None):
    """Password for 'sla' / 'itsm': environment variable, then the config file, then Windows Credential Manager."""
    from .engines import common
    env = os.environ.get(common.PASSWORD_ENV[kind])
    if env:
        return env
    if cc is not None and cc.file_password(kind):
        return cc.file_password(kind)
    return common.get_password(kind, username or (cc.username(kind) if cc else ""))
