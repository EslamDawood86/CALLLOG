"""Settings: built-in defaults < config.json < environment variables."""
import json
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent

DEFAULTS = {
    # Where ALL data lives (users, call records, audit log). A plain name such as "data" means
    # "next to the program files", so running from the shared folder keeps the data there too.
    # A full path (e.g. \\10.104.46.34\calllog\data) also works.
    "data_dir": "data",
    # "127.0.0.1" = only this PC can open the site; "0.0.0.0" = other PCs can open it too.
    "host": "127.0.0.1",
    "port": 8080,
    # How many days back an employee may edit/delete their own records (null = no limit).
    "user_edit_days": None,
    # What counts as a valid IP-phone number.
    "ip_phone_regex": r"^\d{3,10}$",
    "session_hours": 12,
    "threads": 16,   # requests handled at the same time (they mostly wait for the shared folder)
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
    data_dir = Path(cfg["data_dir"])
    cfg["data_dir"] = str(data_dir if data_dir.is_absolute() else APP_DIR / data_dir)
    return cfg
