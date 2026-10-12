"""Speed and 24/7 behaviour of the shared pieces: background refresher, shared results, settings cache, /healthz."""
import json
import threading
import time
from datetime import datetime

import pytest

from calllog.app import start_refresher
from calllog.ops import settings as hub_settings
from calllog.ops.store import OpsStore


@pytest.fixture
def ops(tmp_path):
    return OpsStore(str(tmp_path)).init()


def _write_raw(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------------------------------------------------ OpsStore cache
def test_missing_file_is_remembered_and_found_when_it_appears(ops, tmp_path):
    path = ops.snapshots_dir / "email.json"
    assert ops.read_json(path, "none") == "none"
    _write_raw(path, {"x": 1})                                 # written by another process: not seen before the TTL
    assert ops.read_json(path, "none") == "none"
    ops.TTL = 0.0
    assert ops.read_json(path, "none") == {"x": 1}


def test_write_through_the_store_is_visible_at_once(ops):
    ops.write_snapshot("sla", {"totals": {}, "requests": []}, generated_at="2026-10-12T09:00:00")
    assert ops.read_snapshot("sla")["generated_at"] == "2026-10-12T09:00:00"
    ops.write_snapshot("sla", {"totals": {}, "requests": []}, generated_at="2026-10-12T09:05:00")
    assert ops.read_snapshot("sla")["generated_at"] == "2026-10-12T09:05:00"


def test_refresher_picks_up_changes_from_another_process_without_a_request_waiting(ops):
    path = ops.snapshots_dir / "itsm.json"
    _write_raw(path, {"v": 1})
    assert ops.read_json(path) == {"v": 1}
    ops.TTL = 0.0                                              # every file is "expired": only the refresher keeps it warm
    ops.start_refresher(0.05)
    try:
        time.sleep(0.2)
        assert ops._refresher_alive(time.monotonic())
        _write_raw(path, {"v": 2, "padding": "x" * 10})        # size changes -> new signature
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and ops._cache[str(path)][1] != {"v": 2, "padding": "x" * 10}:
            time.sleep(0.05)
        assert ops.read_json(path) == {"v": 2, "padding": "x" * 10}
    finally:
        ops.stop_refresher()
    assert not ops._refresher_alive(time.monotonic())


def test_refresher_survives_a_failing_warm_function_and_a_damaged_file(ops):
    path = ops.snapshots_dir / "email.json"
    _write_raw(path, {"ok": True})
    ops.read_json(path)
    calls = []

    def warm():
        calls.append(1)
        raise RuntimeError("boom")
    ops.start_refresher(0.05, warm=warm)
    try:
        path.write_text("{not json", encoding="utf-8")
        time.sleep(0.4)
        assert len(calls) >= 2                                 # still running after the first failure
        assert ops._cache[str(path)][1] == {"ok": True}        # the old good copy is kept, the thread did not die
    finally:
        ops.stop_refresher()


def test_without_the_refresher_an_expired_file_is_rechecked_by_the_request(ops):
    path = ops.snapshots_dir / "email.json"
    _write_raw(path, {"v": 1})
    ops.read_json(path)
    ops.TTL = 0.0
    _write_raw(path, {"v": 22})
    assert ops.read_json(path) == {"v": 22}


# ------------------------------------------------------------------------------------------ shared results
def test_memo_serves_the_previous_result_while_another_request_recomputes(app):
    store = app.extensions["store"]
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
        return "new"

    assert store.memo("k", lambda: "old", ttl=0.01) == "old"
    time.sleep(0.05)                                           # expired
    worker = threading.Thread(target=lambda: store.memo("k", slow, ttl=0.01))
    worker.start()
    assert started.wait(5)
    try:
        t0 = time.perf_counter()
        assert store.memo("k", lambda: "unused", ttl=0.01) == "old"        # does not wait for the slow one
        assert time.perf_counter() - t0 < 0.5
    finally:
        release.set()
        worker.join(5)
    assert store.memo("k", lambda: "unused", ttl=60) == "new"
    assert len(calls) == 1


def test_memo_without_a_previous_result_still_waits_for_the_first_computation(app):
    store = app.extensions["store"]
    assert store.memo("first", lambda: 42) == 42


# ------------------------------------------------------------------------------------------ settings
def test_settings_are_checked_once_and_every_caller_gets_its_own_copy(ops):
    first = hub_settings.load(ops)
    first["email"]["teams"].append("Mutated Team")
    second = hub_settings.load(ops)
    assert "Mutated Team" not in second["email"]["teams"]
    assert second == hub_settings.load(ops)


def test_settings_cache_follows_a_saved_change(ops):
    before = hub_settings.load(ops)
    hub_settings.patch(ops, {"sla": {"warning_minutes": 45, "breach_minutes": 90}})
    after = hub_settings.load(ops)
    assert (before["sla"]["warning_minutes"], after["sla"]["warning_minutes"]) != (45, 45)
    assert after["sla"]["warning_minutes"] == 45


def test_deep_merge_changes_neither_argument():
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 4}
    over = {"a": {"b": 2, "c": [9]}}
    merged = hub_settings.deep_merge(base, over)
    assert merged == {"a": {"b": 2, "c": [9]}, "d": 4}
    assert base == {"a": {"b": 1, "c": [1, 2]}, "d": 4}
    merged["a"]["c"].append(3)
    assert over == {"a": {"b": 2, "c": [9]}}


# ------------------------------------------------------------------------------------------ /healthz and warm-up
def test_healthz_needs_no_sign_in_and_shows_no_names(app):
    client = app.test_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert set(body) == {"ok", "time", "collectors"}
    assert set(body["collectors"]) == {"email", "email_report", "sla", "itsm"}
    assert all(value is False for value in body["collectors"].values())          # nothing has reported yet


def test_healthz_reports_a_collector_that_just_reported(app):
    app.extensions["ops"].write_heartbeat("sla", enabled=True, host="PC1", running=False,
                                          last_status="ok", last_ok_at=datetime.now().isoformat(timespec="seconds"))
    body = app.test_client().get("/healthz").get_json()
    assert body["collectors"]["sla"] is True


def test_healthz_answers_503_when_the_data_folder_cannot_be_read(app, monkeypatch):
    monkeypatch.setattr(app.extensions["store"], "users", lambda: (_ for _ in ()).throw(OSError("share is gone")))
    resp = app.test_client().get("/healthz")
    assert resp.status_code == 503
    assert resp.get_json()["ok"] is False


def test_start_refresher_warms_up_and_can_be_stopped(app):
    start_refresher(app, interval=0.05)
    try:
        ops = app.extensions["ops"]
        assert ops._refresher_alive(time.monotonic())
        assert app.test_client().get("/healthz").status_code == 200
    finally:
        app.extensions["ops"].stop_refresher()
