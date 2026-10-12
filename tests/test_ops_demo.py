"""Demo data (calllog/ops/demo.py): contract-valid, deterministic, invented, and good enough to try every page with.

The pages, the documentation screenshots and other tests all start from this data, so what it promises is checked here:
snapshots pass ``schemas.validate_snapshot``, e-mail rows carry what the engine writes (``row_id``, ``classified_by``,
``classification_hint``, ``data.classification``), some mails are Unclassified, and the same ``now`` gives the same files.
"""
import json
import re
from collections import Counter
from datetime import datetime, timedelta

import pytest

from calllog import security
from calllog.ops import demo, health, schemas
from calllog.ops import settings as hub_settings
from calllog.ops.store import OpsStore
from calllog.storage import Store

NOW = datetime(2026, 10, 12, 11, 40, 0)


@pytest.fixture
def ops(tmp_path):
    store = OpsStore(tmp_path / "data").init()
    demo.seed(store, NOW)
    return store


def files_of(ops):
    return {p.relative_to(ops.root).as_posix(): p for p in sorted(ops.root.rglob("*")) if p.is_file()}


# ------------------------------------------------------------------------------------------------ contract
def test_snapshots_are_valid_and_sized(ops):
    for module in schemas.MODULES:
        snap = ops.read_snapshot(module)
        assert schemas.validate_snapshot(snap) == [], module
        assert snap["host"] == demo.HOST and snap["date"] == "2026-10-12"
    email, sla, itsm = (ops.read_snapshot(m)["data"] for m in schemas.MODULES)
    assert 30 <= len(email["rows"]) <= 60
    assert 25 <= len(sla["requests"]) <= 40 and len(sla["by_user"]) == 5
    assert len(itsm["roles"]) == 2 and set(itsm["categories"]) == set(itsm["totals"])


def test_email_rows_carry_what_the_engine_writes(ops):
    data = ops.read_snapshot("email")["data"]
    rows = data["rows"]
    for row in rows:
        assert row["row_id"] and row["message_id"]
        assert row["classified_by"] in demo.CLASSIFIED_BY
        assert isinstance(row["classification_hint"], str)
        assert row["status"] in schemas.EMAIL_STATUSES
        if row.get("is_split_child"):
            assert row["row_id"] == f"{row['message_id']}|{row['team']}"      # one row per team of a multi-team mail
        else:
            assert row["row_id"] == row["message_id"]
    assert len({r["row_id"] for r in rows}) == len(rows)
    unclassified = [r for r in rows if r["team"] == "Unclassified"]
    assert len(unclassified) >= 2
    assert all(r["classified_by"] == "none" and r["classification_hint"] for r in unclassified)      # a hint to turn into a rule
    block = data["classification"]
    assert set(block["counts"]) == set(demo.CLASSIFIED_BY)
    assert block["counts"] == {k: sum(1 for r in rows if r["classified_by"] == k) for k in demo.CLASSIFIED_BY}
    assert sum(block["counts"].values()) == len(rows)
    assert block["unclassified"] == len(unclassified) and block["unclassified_team"] == ""
    assert block["counts"]["address"] and block["counts"]["override"] and block["counts"]["system"]


def test_email_rows_cover_every_situation_the_pages_show(ops):
    data = ops.read_snapshot("email")["data"]
    rows, stats = data["rows"], data["stats"]
    assert {r["status"] for r in rows} == set(schemas.EMAIL_STATUSES)
    assert any(r["is_carryover"] and r["age_days"] > 0 for r in rows)
    assert any(r["escalated"] for r in rows) and any(r["sla_status"] == "Breached" and r["status"] == schemas.EMAIL_REPLIED for r in rows)
    split = [r for r in rows if r.get("is_split_child")]
    assert len(split) >= 4 and {r["message_id"] for r in split}.__len__() < len(split)
    assert any(r["assigned_to_email"] for r in rows) and any(r["reply_by_manager"] for r in rows)
    assert any(r["quality_rating"] for r in rows) and any(r["quality_appeal"] for r in rows)
    assert {"Excellent", "Good", "Needs Improvement"} <= {r["quality_rating"] for r in rows}
    assert {"Flex Cube", "Card Management", "MFA Team", "Third Party", "Open System"} <= {r["team"] for r in rows}
    assert stats["pending"] == sum(1 for r in rows if r["status"] == schemas.EMAIL_PENDING)
    assert stats["quality"]["reviewed"] == sum(1 for r in rows if r["quality_rating"])
    assert data["quality"]["scorecard"] and data["quality"]["leaderboard_top"]


def test_sla_data_covers_all_four_statuses_and_adds_up(ops):
    sla = ops.read_snapshot("sla")["data"]
    reqs = sla["requests"]
    assert {r["status"] for r in reqs} == set(schemas.SLA_STATUSES)
    assert all(r["key"] and r["security_user"] for r in reqs) and len({r["key"] for r in reqs}) == len(reqs)
    totals = sla["totals"]
    assert totals["open"] == len(reqs) and totals["oldest_minutes"] == max(r["age_minutes"] or 0 for r in reqs)
    for status in schemas.SLA_STATUSES:
        assert totals[status] == sum(1 for r in reqs if r["status"] == status)
    assert [u["user"] for u in sla["by_user"]] == [u["user"] for u in sorted(
        sla["by_user"], key=lambda u: (-u["breached"], -u["reminder"], -u["total"], u["user"]))]
    thresholds = sla["thresholds"]
    for r in reqs:        # status follows the thresholds, as the engine decides it
        if r["age_minutes"] is None:
            assert r["status"] == "no_date"
        elif r["age_minutes"] >= thresholds["breach_minutes"]:
            assert r["status"] == "breached"
        elif r["age_minutes"] >= thresholds["warning_minutes"]:
            assert r["status"] == "reminder"
        else:
            assert r["status"] == "within"


def test_itsm_error_variant(tmp_path):
    ok, bad = OpsStore(tmp_path / "ok").init(), OpsStore(tmp_path / "bad").init()
    demo.seed(ok, NOW)
    demo.seed(bad, NOW, itsm_error=True)
    good_roles = ok.read_snapshot("itsm")["data"]["roles"]
    bad_roles = bad.read_snapshot("itsm")["data"]["roles"]
    assert [r["error"] for r in good_roles] == [None, None]
    assert [bool(r["error"]) for r in bad_roles] == [False, True] and bad_roles[1]["categories"] == {}
    assert schemas.validate_snapshot(bad.read_snapshot("itsm")) == []


def test_history_events_logs_reports_and_collector_info(ops):
    for module in schemas.MODULES:
        days = ops.history_days(module)
        assert days and len(ops.read_history(module, days[-1])["points"]) >= 8
    assert ops.read_history("sla", "2026-10-12")["first_breach"]
    assert len(ops.history_days("email")) >= 7                         # the finals of the last week feed the trend
    events = ops.read_events("2026-10-12")
    assert events and {e["level"] for e in events} >= {"danger", "warn", "ok"}
    assert all(e["ts"].startswith("2026-10-12") for e in events)
    for job in schemas.JOBS:
        assert len(ops.tail_log(job, 50)) == 3
    assert {r["module"] for r in ops.list_reports()} == set(schemas.MODULES)
    info = ops.read_state("collector_info", {})
    assert info["host"] == demo.HOST and info["accounts"]["sla"]["has_password"] is True
    assert "password" not in json.dumps(info).lower().replace("has_password", "").replace("password_source", "")


def test_heartbeats_show_one_stale_collector_and_the_failure_variants(tmp_path):
    cfg = hub_settings.DEFAULTS
    plain = OpsStore(tmp_path / "a").init()
    demo.seed(plain, NOW)
    states = health.job_states(plain, cfg, NOW)
    assert [states[j]["online"] for j in schemas.JOBS] == [True, True, True, False]       # itsm is the stale one
    assert all(states[j]["last_status"] == "ok" for j in schemas.JOBS)
    variants = OpsStore(tmp_path / "b").init()
    demo.seed(variants, NOW, failed_job="email", stale_job="", paused_job="sla")
    states = health.job_states(variants, cfg, NOW)
    assert all(states[j]["online"] for j in schemas.JOBS)
    assert states["email"]["last_status"] == "error" and "Outlook" in states["email"]["last_error"]
    assert states["sla"]["last_status"] == "error"
    assert variants.read_heartbeat("sla")["paused"] == "auth" and variants.read_heartbeat("email")["paused"] is None
    assert any("failed" in e["text"] for e in variants.read_events("2026-10-12"))
    with pytest.raises(ValueError):
        demo.seed(variants, NOW, failed_job="nope")


def test_nothing_real_is_in_the_demo_files(ops):
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for name, p in files_of(ops).items()
                     if name.endswith((".json", ".jsonl", ".log")))
    addresses = set(re.findall(r"[\w.+-]+@[\w.-]+", text))
    assert addresses and all(a.endswith((".example", ".test")) for a in addresses), sorted(addresses)[:5]
    assert not re.search(r"password\"\s*:\s*\"[^\"]", text)


# --------------------------------------------------------------------------------------------- determinism
def test_same_now_gives_the_same_files(tmp_path):
    a, b = OpsStore(tmp_path / "a").init(), OpsStore(tmp_path / "b").init()
    demo.seed(a, NOW)
    demo.seed(b, NOW)
    fa, fb = files_of(a), files_of(b)
    assert fa.keys() == fb.keys() and len(fa) > 20
    for name in fa:
        if not name.endswith(".xlsx"):                                  # a workbook carries the time it was saved
            assert fa[name].read_bytes() == fb[name].read_bytes(), name
    before = {n: p.read_bytes() for n, p in fa.items() if not n.endswith(".xlsx")}
    demo.seed(a, NOW)                                                   # seeding twice leaves the same files
    assert {n: p.read_bytes() for n, p in files_of(a).items() if not n.endswith(".xlsx")} == before


def test_a_different_now_moves_the_times_not_the_shape(tmp_path):
    a, b = OpsStore(tmp_path / "a").init(), OpsStore(tmp_path / "b").init()
    demo.seed(a, NOW)
    demo.seed(b, NOW + timedelta(minutes=30))
    ra, rb = (s.read_snapshot("email")["data"]["rows"] for s in (a, b))
    assert len(ra) == len(rb) and [r["subject"] for r in ra] == [r["subject"] for r in rb]
    assert a.read_snapshot("email")["generated_at"] != b.read_snapshot("email")["generated_at"]


def test_options_without_history_or_settings(tmp_path):
    ops = OpsStore(tmp_path / "x").init()
    out = demo.seed(ops, NOW, with_history=False, with_settings=False)
    assert out["history"] is False and out["settings_written"] is False
    assert ops.history_days("email") == [] and ops.read_events("2026-10-12") == [] and ops.read_settings_raw() == {}
    ops2 = OpsStore(tmp_path / "y").init()
    hub_settings.patch(ops2, {"email": {"business_emails": ["mine@example.test"]}})
    assert demo.seed(ops2, NOW)["settings_written"] is False             # a real configuration is never touched
    assert hub_settings.load(ops2)["email"]["business_emails"] == ["mine@example.test"]


def test_demo_settings_are_valid_and_match_the_rows(ops):
    cfg = hub_settings.load(ops)
    assert hub_settings.validate(ops.read_settings_raw())[1] == []
    rules = cfg["email"]["classification"]["address_team_map"]
    assert set(rules.values()) == set(demo.ADDRESS_RULES)
    assert set(rules) <= set(cfg["email"]["business_emails"])
    people = {a for addrs in cfg["email"]["team_members_by_team"].values() for a in addrs}
    assert {r["first_reply_email"] for r in ops.read_snapshot("email")["data"]["rows"] if r["first_reply_email"]} - {demo.MANAGER[0] + "@" + demo.DOMAIN} <= people


# ------------------------------------------------------------------------------------- accounts and calls
@pytest.fixture
def call_store(tmp_path):
    store = Store(tmp_path / "calls")
    store.init()
    return store


def test_users_and_calls(call_store):
    creds = demo.seed_users_and_calls(call_store, NOW)
    users = call_store.users()
    assert sorted(u for u, v in users.items() if v["role"] == "user") == ["1001", "1002", "1003", "1004"]
    manager = users[demo.MANAGER_ID]
    assert manager["role"] == "manager" and manager["team"] == "Flex Cube"
    assert creds["employees"] == ["1001", "1002", "1003", "1004"] and creds["manager"] == demo.MANAGER_ID
    for uid, info in creds["users"].items():
        assert security.verify_password(users[uid]["password_hash"], info["password"])
    today = [r for uid in creds["employees"] for r in call_store.day_records("2026-10-12", uid)]
    yesterday = [r for uid in creds["employees"] for r in call_store.day_records("2026-10-11", uid)]
    assert len(today) == creds["records"]["today"] == 60 and len(yesterday) == creds["records"]["yesterday"] > 20
    assert all(r["time"] < "11:40:00" for r in today)
    assert Counter(r["user_id"] for r in today) == {"1001": 21, "1002": 17, "1003": 13, "1004": 9}


def test_users_and_calls_twice_adds_nothing_and_is_deterministic(tmp_path):
    a, b = Store(tmp_path / "a"), Store(tmp_path / "b")
    a.init(), b.init()
    demo.seed_users_and_calls(a, NOW)
    demo.seed_users_and_calls(b, NOW)
    again = demo.seed_users_and_calls(a, NOW)
    assert again["records"] == {"today": 0, "yesterday": 0}
    key = lambda s: sorted((r["user_id"], r["time"], r["reason"], r["ip_phone"]) for uid in ("1001", "1002", "1003", "1004")
                           for r in s.day_records("2026-10-12", uid))
    assert key(a) == key(b) and len(key(a)) == 60


def test_two_employees_match_rows_of_the_snapshots(tmp_path, call_store):
    ops = OpsStore(tmp_path / "ops").init()
    demo.seed(ops, NOW)
    creds = demo.seed_users_and_calls(call_store, NOW)
    with_mail = [u for u in creds["users"].values() if u["role"] == "user" and u["email"]]
    assert len(with_mail) == 2 and all(u["security_name"] for u in with_mail)
    rows = ops.read_snapshot("email")["data"]["rows"]
    sla_users = {r["security_user"] for r in ops.read_snapshot("sla")["data"]["requests"]}
    for u in with_mail:
        mine = [r for r in rows if u["email"] in (r["first_reply_email"], r["assigned_to_email"])]
        assert mine, u["email"]
        assert u["security_name"] in sla_users
    assert {u["team"] for u in with_mail} == {"Flex Cube"}
    assert sorted(len(u["teams"]) for u in creds["users"].values() if u["role"] == "user") == [0, 0, 1, 2]      # one employee has two teams
    assert creds["users"][demo.MANAGER_ID]["teams"] == ["Flex Cube"]
