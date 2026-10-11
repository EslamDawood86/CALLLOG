"""Settings: built-in defaults < config.json < environment variables."""
import json
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent

DEFAULTS = {
    # Where ALL data lives (users, call records, audit log). Point it at the shared folder.
    "data_dir": r"\\10.104.46.34\calllog\data",
    "host": "0.0.0.0",
    "port": 8080,
    # How many days back an employee may edit/delete their own records (null = no limit).
    "user_edit_days": None,
    # What counts as a valid IP-phone number.
    "ip_phone_regex": r"^\d{3,10}$",
    "session_hours": 12,
    "threads": 8,
}


def load_config():
    cfg = dict(DEFAULTS)
    path = Path(os.environ.get("CALLLOG_CONFIG", APP_DIR / "config.json"))
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if os.environ.get("CALLLOG_DATA_DIR"):
        cfg["data_dir"] = os.environ["CALLLOG_DATA_DIR"]
    if os.environ.get("CALLLOG_HOST"):
        cfg["host"] = os.environ["CALLLOG_HOST"]
    if os.environ.get("CALLLOG_PORT"):
        cfg["port"] = int(os.environ["CALLLOG_PORT"])
    return cfg
