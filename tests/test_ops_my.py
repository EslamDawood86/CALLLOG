"""The two dashboards: /api/ops/shared (counts only) and /api/ops/my (an employee sees only what is theirs)."""
import json

import pytest

from conftest import Browser
# helpers and fixtures shared with the API tests
from test_ops_api import (audit_actions, beat, email_data, email_row, hub, itsm_data, make_account, mgr,  # noqa: F401
                          flex_mgr, ops, put_snapshot, replied_row, sla_data, sla_request)

ALICE = "alice@example.test"
BOB = "bob@example.test"


def log_call(b, ip, reason):
    r = b.post("/api/records", {"ip_phone": ip, "reason": reason, "note": ""})
    assert r.status_code == 201, r.get_json()


@pytest.fixture
def alice(make_account):
    return make_account("1001", "user", "Alice Employee", email=ALICE, security_name="احمد سالم")


@pytest.fixture
def bob(make_account):
    return make_account("1002", "user", "Bob Employee", email=BOB, security_name="منى علي")


@pytest.fixture
def work(ops, alice, bob):
    """A realistic morning: mails, security requests and ratings that belong to Alice, to Bob and to nobody."""
    rows = [
        email_row("<a1>", subject="Alice pending task", customer_name="Customer Anna", team="Flex Cube"),
        replied_row("<a2>", ALICE, 20, subject="Alice replied one", customer_name="Customer Ann2", team="Card Management",
                    all_teams=["Card Management"]),
        replied_row("<a3>", "Alice@Example.TEST", 90, subject="Alice replied slow", customer_name="Customer Ann3"),
        email_row("<b1>", subject="Bob pending task", customer_name="Customer Bella"),
        replied_row("<b2>", BOB, 30, subject="Bob replied one", customer_name="Customer Bel2"),
        email_row("<x1>", subject="Nobody's mail", customer_name="Customer Xavier"),
    ]
    put_snapshot(ops, "email", email_data(rows))
    ops.set_override("assignments", "<a1>", {"assigned_to_email": ALICE, "assigned_to_name": "Alice", "note": "do it",
                                             "marked_by": "Mona Manager", "marked_at": "2026-10-11T08:30:00"})
    ops.set_override("assignments", "<b1>", {"assigned_to_email": BOB, "assigned_to_name": "Bob", "note": "bob only",
                                             "marked_by": "Mona Manager", "marked_at": "2026-10-11T08:31:00"})
    ops.set_override("quality", "<a2>", {"rating": "Needs Improvement", "note": "alice note", "reasons": "Too slow",
                                         "replied_by_email": ALICE, "reviewed_by": "Mona Manager",
                                         "reviewed_at": "2026-10-11T08:45:00", "subject": "Alice replied one",
                                         "appeal": "", "rating_withdrawn": False})
    ops.set_override("quality", "<b2>", {"rating": "Excellent", "note": "bob note", "reasons": "",
                                         "replied_by_email": BOB, "reviewed_by": "Mona Manager",
                                         "reviewed_at": "2026-10-11T08:46:00", "subject": "Bob replied one"})
    ops.set_override("quality", "<gone>", {"rating": "Good", "note": "old mail", "replied_by_email": ALICE,
                                           "reviewed_at": "2026-10-09T10:00:00", "subject": "Yesterday's mail",
                                           "appeal": "unfair", "appeal_decision": "Rejected", "rating_withdrawn": False})
    put_snapshot(ops, "sla", sla_data([
        sla_request("k1", "احمد سالم", "breached", 150, **{"اسم الموظف": "Requester One"}),
        sla_request("k2", "أحمد  سالم", "reminder", 70),                      # same person, other spelling
        sla_request("k3", "منى علي", "within", 10, **{"اسم الموظف": "Requester Three"}),
        sla_request("k4", "شخص آخر", "within", 5)]))
    put_snapshot(ops, "itsm", itsm_data())
    for job in ("email", "sla", "itsm"):
        beat(ops, job)


# ---------------------------------------------------------------------------- isolation
def test_an_employee_only_receives_their_own_rows(alice, bob, work):
    mine = alice.get("/api/ops/my").get_json()
    text = json.dumps(mine, ensure_ascii=False)
    for subject in ("Alice pending task", "Alice replied one", "Alice replied slow", "Yesterday's mail", "Requester One"):
        assert subject in text, subject
    for other in ("Bob pending task", "Bob replied one", "Nobody's mail", "Customer Bella", "Customer Bel2",
                  "Customer Xavier", "Requester Three", BOB, "bob only", "bob note", "Bob Employee", "منى علي", "شخص آخر"):
        assert other not in text, other
    theirs = bob.get("/api/ops/my").get_json()
    btext = json.dumps(theirs, ensure_ascii=False)
    assert "Bob pending task" in btext and "Bob replied one" in btext and "Requester Three" in btext
    for other in ("Alice pending task", "Alice replied", "Customer Anna", ALICE, "do it", "alice note", "احمد سالم",
                  "Yesterday's mail", "Nobody's mail"):
        assert other not in btext, other


def test_an_employee_cannot_peek_at_somebody_else_or_at_other_endpoints(alice, bob, work):
    r = alice.get("/api/ops/my?as=1002")
    assert r.status_code == 403 and "Bob" not in json.dumps(r.get_json())
    assert alice.get("/api/ops/my?as=1001").status_code == 200                 # asking for yourself is harmless
    for path in ("/api/ops/email", "/api/ops/sla", "/api/ops/itsm", "/api/ops/overview", "/api/ops/events",
                 "/api/ops/reports", "/export/ops/email.xlsx", "/api/ops/health", "/api/ops/settings"):
        assert alice.get(path).status_code == 403, path


def test_anonymous_gets_nothing(app, work):
    c = app.test_client()
    assert c.get("/api/ops/my").status_code == 401 and c.get("/api/ops/shared").status_code == 401


def test_an_account_without_links_gets_empty_states_and_no_stranger_rows(make_account, work):
    nobody = make_account("2001", "user", "No Links")
    body = nobody.get("/api/ops/my").get_json()
    assert body["email"]["configured"] is False and body["email"]["assigned"]["items"] == []
    assert body["email"]["replied"]["count"] == 0 and body["quality"]["configured"] is False
    assert body["sla"]["configured"] is False and body["sla"]["requests"] == []
    assert "Nobody's mail" not in json.dumps(body)          # rows with an empty assignee must not match an empty e-mail


def test_my_without_any_snapshot_is_empty_not_an_error(alice):
    body = alice.get("/api/ops/my").get_json()
    assert body["email"]["configured"] is True and body["email"]["available"] is False
    assert body["sla"]["configured"] is True and body["sla"]["available"] is False
    assert body["calls"]["today"]["total"] == 0 and body["shared"]["email"]["available"] is False


# ---------------------------------------------------------------------------- the content of /my
def test_my_calls_today_yesterday_week_and_hours(clock, alice, bob):
    def at(day, hh, mm):
        import datetime as dt
        clock.value = dt.datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00")
    at("2026-10-03", 10, 0)
    log_call(alice, "70001", "solved")                       # 8 days ago: outside the 7-day window
    at("2026-10-06", 10, 0)
    log_call(alice, "70002", "answered")                     # 5 days ago
    at("2026-10-10", 10, 15)
    log_call(alice, "70003", "solved")
    log_call(alice, "70004", "followup")
    at("2026-10-11", 8, 30)
    log_call(alice, "70005", "solved")
    at("2026-10-11", 9, 5)
    log_call(alice, "70006", "transferred")
    log_call(alice, "70007", "solved")
    log_call(bob, "70008", "solved")                         # someone else's call
    calls = alice.get("/api/ops/my").get_json()["calls"]
    today = calls["today"]
    assert (today["total"], today["resolved"], today["transferred"], today["ticket_inquiry"]) == (3, 2, 1, 0)
    assert (calls["yesterday"]["total"], calls["yesterday"]["resolved"], calls["yesterday"]["ticket_inquiry"]) == (2, 1, 1)
    assert calls["last7"]["total"] == 1 + 2 + 3 and [d["date"] for d in calls["daily"]][0] == "2026-10-05"
    assert len(calls["daily"]) == 7 and calls["daily"][-1]["date"] == "2026-10-11"
    hours = {h["key"]: h["total"] for h in calls["per_hour_today"]}
    assert len(hours) == 24 and hours[8] == 1 and hours[9] == 2 and sum(hours.values()) == 3
    assert today["first_call"].endswith("08:30:00") and today["last_call"].endswith("09:05:00")
    assert sum(r["count"] for r in calls["by_reason_today"]) == 3
    assert bob.get("/api/ops/my").get_json()["calls"]["today"]["total"] == 1
    assert alice.get("/api/ops/shared").get_json()["calls"]["total"] == 4          # the team strip counts everybody


def test_my_emails_assigned_and_replied(alice, work):
    mail = alice.get("/api/ops/my").get_json()["email"]
    assert mail["configured"] and mail["available"] and mail["email"] == ALICE
    assert (mail["assigned"]["pending"], mail["assigned"]["done"]) == (1, 0)
    task = mail["assigned"]["items"][0]
    assert task["subject"] == "Alice pending task" and task["status"] == "Pending Reply"
    assert task["note"] == "do it" and task["assigned_by"] == "Mona Manager" and task["waiting"] == "1 H"
    replied = mail["replied"]
    assert replied["count"] == 2 and replied["average_first_reply_minutes"] == 55.0
    assert (replied["sla_met"], replied["sla_breached"]) == (1, 1)
    assert {i["subject"] for i in replied["items"]} == {"Alice replied one", "Alice replied slow"}
    # only whitelisted fields are returned for a row
    assert "customer_email" not in task and "body_preview" not in task and "entry_id" not in task


def test_my_view_follows_overrides_made_after_the_scan(admin, alice, work):
    assert admin.post("/api/ops/email/assign", {"message_id": "<x1>", "assigned_to_email": ALICE}).status_code == 200
    assert admin.post("/api/ops/email/no-action", {"message_id": "<a1>"}).status_code == 200
    mail = alice.get("/api/ops/my").get_json()["email"]
    # "done" means marked done or answered by me; a mail the manager said needs no action is neither open nor done
    assert (mail["assigned"]["pending"], mail["assigned"]["done"], mail["assigned"]["other"]) == (1, 0, 1)
    by_subject = {i["subject"]: i for i in mail["assigned"]["items"]}
    assert by_subject["Nobody's mail"]["status"] == "Pending Reply" and by_subject["Alice pending task"]["status"] == "No Action Needed"


def test_my_quality_reviews_and_appeal(alice, bob, work):
    q = alice.get("/api/ops/my").get_json()["quality"]
    assert q["configured"] and q["counts"]["reviewed"] == 2 and q["counts"]["needs_improvement"] == 1
    reviews = {r["message_id"]: r for r in q["reviews"]}
    assert set(reviews) == {"<a2>", "<gone>"}
    assert reviews["<a2>"]["can_appeal"] is True and reviews["<a2>"]["reasons"] == "Too slow"
    assert reviews["<gone>"]["can_appeal"] is False and reviews["<gone>"]["appeal_decision"] == "Rejected"
    assert [r["message_id"] for r in q["reviews"]] == ["<a2>", "<gone>"]                 # newest first
    assert alice.post("/api/ops/email/quality/appeal", {"message_id": "<a2>", "appeal": "I replied within the hour"}).status_code == 200
    q = alice.get("/api/ops/my").get_json()["quality"]
    mine = {r["message_id"]: r for r in q["reviews"]}["<a2>"]
    assert mine["appeal"] == "I replied within the hour" and mine["appeal_open"] is True and mine["can_appeal"] is False
    assert q["counts"]["appeals"] == 2
    assert bob.post("/api/ops/email/quality/appeal", {"message_id": "<a2>", "appeal": "not my reply"}).status_code == 404


def test_my_security_requests_match_the_security_name(alice, bob, work):
    sla = alice.get("/api/ops/my").get_json()["sla"]
    assert sla["configured"] and sla["available"] and sla["security_name"] == "احمد سالم"
    assert sla["totals"] == {"open": 2, "within": 0, "reminder": 1, "breached": 1, "no_date": 0, "oldest_minutes": 150}
    assert [r["key"] for r in sla["requests"]] == ["k1", "k2"] and sla["requests"][0]["age_minutes"] == 150
    assert sla["requests"][0]["fields"]["اسم الموظف"] == "Requester One"
    assert sla["thresholds"]["breach_minutes"] == 120
    other = bob.get("/api/ops/my").get_json()["sla"]
    assert [r["key"] for r in other["requests"]] == ["k3"] and other["totals"]["within"] == 1


# ---------------------------------------------------------------------------- view as
def test_admin_can_view_as_an_employee_read_only_and_it_is_audited(app, admin, alice, work, clock):
    body = admin.get("/api/ops/my?as=1001").get_json()
    assert body["read_only"] is True and body["viewer"] == "admin" and body["user"]["user_id"] == "1001"
    assert body["email"]["assigned"]["items"][0]["subject"] == "Alice pending task"
    assert ("ops_view_as", "1001", "admin") in audit_actions(app, clock)
    assert admin.get("/api/ops/my?as=nobody").status_code == 404
    own = admin.get("/api/ops/my").get_json()
    assert own["read_only"] is False and own["user"]["user_id"] == "admin" and own["email"]["configured"] is False
    # the page is read-only: it offers no write, and the API refuses an employee's write by someone else
    assert admin.post("/api/ops/email/quality/appeal", {"message_id": "<a2>", "appeal": "on behalf of alice"}).status_code == 404


def test_manager_view_as_is_limited_to_their_team(mgr, flex_mgr, alice, work):
    full = mgr.get("/api/ops/my?as=1001").get_json()
    assert {i["subject"] for i in full["email"]["replied"]["items"]} == {"Alice replied one", "Alice replied slow"}
    scoped = flex_mgr.get("/api/ops/my?as=1001").get_json()          # Flex Cube manager: "Alice replied one" is Card Management
    assert {i["subject"] for i in scoped["email"]["replied"]["items"]} == {"Alice replied slow"}
    assert "Alice replied one" not in json.dumps(scoped["email"])
    assert scoped["read_only"] is True


# ---------------------------------------------------------------------------- one set of numbers
def strip(shared, scope_fields=True):
    """A shared strip without its timestamp (and, when asked, without the fields that name the viewer's scope)."""
    out = {k: v for k, v in shared.items() if k != "generated_at"}
    if not scope_fields:
        out["email"] = {k: v for k, v in out["email"].items() if k not in ("scope", "teams")}
    return out


def test_every_dashboard_shows_the_same_numbers_for_the_same_viewer(admin, mgr, alice, bob, make_account, work):
    carl = make_account("1003", "user", "Carl Employee", email="carl@example.test", teams=["Flex Cube", "Card Management"])
    alice.post("/api/records", {"ip_phone": "70001", "reason": "solved", "note": ""})
    shared = strip(admin.get("/api/ops/shared").get_json())
    assert shared["email"]["scope"] == "all" and shared["email"]["teams"] == []
    # the same viewer sees the same numbers wherever the strip is drawn
    for who in (admin, mgr, carl, alice, bob):
        assert strip(who.get("/api/ops/shared").get_json()) == strip(who.get("/api/ops/my").get_json()["shared"])
    assert strip(admin.get("/api/ops/overview").get_json()["shared"]) == shared
    assert strip(mgr.get("/api/ops/overview").get_json()["shared"]) == shared
    # an employee of every team in the snapshot counts exactly what the administrator counts (no Unclassified mail here)
    assert strip(carl.get("/api/ops/shared").get_json(), False) == strip(admin.get("/api/ops/shared").get_json(), False)
    assert carl.get("/api/ops/shared").get_json()["email"]["scope"] == "teams"
    # calls, security requests and ITSM stay sector-wide for everybody, even for an employee without a team
    for who in (alice, bob):
        theirs = strip(who.get("/api/ops/shared").get_json())
        for block in ("calls", "sla", "itsm", "collector"):
            assert theirs[block] == shared[block], block
        assert theirs["email"]["scope"] == "teams" and theirs["email"]["teams"] == []
        assert theirs["email"]["business_emails"] == 0 and theirs["email"]["pending"] == 0
    # and the headline numbers on Home are the shared ones
    home = admin.get("/api/ops/overview").get_json()
    for key in ("business_emails", "replied", "pending", "reply_rate", "sla_breached"):
        assert home["email"][key] == shared["email"][key]
    assert home["calls"]["today"]["total"] == shared["calls"]["total"] == 1
    assert home["sla"]["breached"] == shared["sla"]["breached"] == 1


def test_shared_follows_actions_taken_on_the_email_page(admin, make_account, work):
    carl = make_account("1003", "user", "Carl Employee", teams=["Flex Cube"])
    before = carl.get("/api/ops/shared").get_json()["email"]
    admin.post("/api/ops/email/no-action", {"message_id": "<x1>"})
    after = carl.get("/api/ops/shared").get_json()["email"]
    assert after["pending"] == before["pending"] - 1 and after["no_action_needed"] == before["no_action_needed"] + 1
    assert after["needed_reply"] == before["needed_reply"] - 1
