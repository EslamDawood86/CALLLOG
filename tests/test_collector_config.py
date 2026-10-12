"""collector_config.json: accounts, pinned settings, and the rule that secrets never reach the settings."""
import json

import pytest

from calllog.ops import collector_config, settings


def write(tmp_path, data):
    p = tmp_path / "collector_config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_missing_file_is_empty(tmp_path):
    cc = collector_config.load(tmp_path / "nope.json")
    assert not cc.exists and cc.overlay() == {} and cc.username("sla") == "" and not cc.has_password("sla")


def test_accounts_and_overlay_without_secrets(tmp_path):
    p = write(tmp_path, {"_help": "x", "sla": {"username": "239582", "password": "s3cret", "warning_minutes": 45},
                         "itsm": {"username": "u2", "password": "p2"}, "email": {"target_archive_name": "2026_Part3"}})
    cc = collector_config.load(p)
    assert cc.username("sla") == "239582" and cc.file_password("sla") == "s3cret"
    assert cc.overlay()["sla"] == {"username": "239582", "warning_minutes": 45}
    assert "password" not in json.dumps(cc.overlay())
    assert cc.pinned_keys() == ["email.target_archive_name", "itsm.username", "sla.username", "sla.warning_minutes"]


def test_file_wins_over_saved_settings_and_stays_valid(tmp_path):
    cc = collector_config.load(write(tmp_path, {"email": {"target_archive_name": "2026_Part3"}, "sla": {"username": "1"}}))
    base, _ = settings.validate({"email": {"target_archive_name": "old"}, "sla": {"username": "2"}})
    merged, errors = settings.validate(cc.apply(base))
    assert errors == []
    assert merged["email"]["target_archive_name"] == "2026_Part3" and merged["sla"]["username"] == "1"
    assert "password" not in merged["sla"]


def test_password_order_env_then_file_then_keyring(tmp_path, monkeypatch):
    cc = collector_config.load(write(tmp_path, {"sla": {"username": "u", "password": "from-file"}}))
    monkeypatch.delenv("NBE_SSO_PASSWORD", raising=False)
    assert collector_config.get_password(cc, "sla") == "from-file"
    monkeypatch.setenv("NBE_SSO_PASSWORD", "from-env")
    assert collector_config.get_password(cc, "sla") == "from-env"


def test_missing_password_is_a_clear_error(tmp_path, monkeypatch):
    from calllog.ops.engines.common import CredentialError
    monkeypatch.delenv("ITSM_PASS", raising=False)
    cc = collector_config.load(write(tmp_path, {"itsm": {"username": "u"}}))
    with pytest.raises(CredentialError, match="set-password itsm"):
        collector_config.get_password(cc, "itsm")


def test_broken_json_gets_a_helpful_message(tmp_path):
    p = tmp_path / "collector_config.json"
    p.write_text('{"sla": {"username": "x",}}', encoding="utf-8")
    with pytest.raises(collector_config.CollectorConfigError, match="line 1"):
        collector_config.load(p)
    p.write_text("[1]", encoding="utf-8")
    with pytest.raises(collector_config.CollectorConfigError):
        collector_config.load(p)


def test_data_dir_override(tmp_path):
    cc = collector_config.load(write(tmp_path, {"data_dir": "/srv/calllog/data"}))
    assert cc.data_dir() == "/srv/calllog/data"
    assert collector_config.load(write(tmp_path, {})).data_dir("/x") == "/x"
