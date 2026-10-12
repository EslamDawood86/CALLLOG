"""Multi-team accounts and the employee team view.

The two user requirements behind these tests:
  1. an employee sees only the e-mails of *their own* team(s) and *never* an "Unclassified" one;
  2. an account (employee or manager) may belong to more than one team, and then sees the union.

Backend scope helpers live in ``calllog/ops/overlay.py`` and ``calllog/auth.py``; the fixtures and row builders are
shared with ``test_ops_api.py``. Fake data only (example.test addresses)."""
import io
import json
import time

import pytest

from conftest import Browser
from test_ops_api import (beat, email_data, email_row, itsm_data, make_account, ops,  # noqa: F401
                          put_snapshot, replied_row, sla_data, sla_request)

# A subject (and customer) an employee must never see anywhere: it belongs to the "Unclassified" bucket.
MARK = "ZZUNCLASSIFIEDSECRETMARKER"


def seed(store):
    """A snapshot with rows across several teams, a mail shared by two teams, and one "Unclassified" mail."""
    rows = [
        email_row("<flex1>", subject="Flex pending one", team="Flex Cube", all_teams=["Flex Cube"]),
        replied_row("<flex2>", "rep@example.test", 20, subject="Flex replied", team="Flex Cube",
                    all_teams=["Flex Cube"], teams_replied=["Flex Cube"]),
        email_row("<card1>", subject="Card pending one", team="Card Management", all_teams=["Card Management"],
                  teams_pending=["Card Management"]),
        email_row("<third1>", subject="Third pending one", team="Third Party", all_teams=["Third Party"],
                  teams_pending=["Third Party"]),
        email_row("<multi1>", subject="Shared flex and card", team="Flex Cube",
                  all_teams=["Flex Cube", "Card Management"], teams_pending=["Flex Cube", "Card Management"]),
        email_row("<uncl1>", subject=MARK, customer_name=MARK, team="Unclassified", all_teams=["Unclassified"],
                  teams_pending=["Unclassified"], detected_systems=""),
    ]
    put_snapshot(store, "email", email_data(rows))
    put_snapshot(store, "sla", sla_data([sla_request("s1", "someone", "breached", 150)]))
    put_snapshot(store, "itsm", itsm_data())
    for job in ("email", "sla", "itsm"):
        beat(store, job)


@pytest.fixture
def world(ops):
    seed(ops)
    return ops


def team_subjects(my_body):
    return {i["subject"] for i in my_body["email"]["team_emails"]}


# --------------------------------------------------------------------- the employee team view
def test_two_team_employee_sees_the_union_and_never_unclassified(make_account, world):
    emp = make_account("1001", "user", "Dina", email="dina@example.test", teams=["Flex Cube", "Card Management"])
    body = emp.get("/api/ops/my").get_json()
    assert team_subjects(body) == {"Flex pending one", "Flex replied", "Card pending one", "Shared flex and card"}
    assert "Third pending one" not in team_subjects(body)
    assert MARK not in json.dumps(body, ensure_ascii=False)
    summary = body["email"]["team_summary"]
    assert summary["teams"] == ["Flex Cube", "Card Management"] and summary["all_teams"] is False
    assert (summary["pending"], summary["replied"]) == (3, 1)          # flex1 + card1 + multi1 pending, flex2 replied
    assert body["email"]["team_emails_hint"] == ""
    # only the whitelisted fields reach the employee - no bodies, customer addresses or quality notes
    item = body["email"]["team_emails"][0]
    for leaked in ("body_preview", "customer_email", "quality_note", "reply_body_preview", "entry_id"):
        assert leaked not in item


def test_single_team_employees_are_isolated_and_a_shared_mail_reaches_both(make_account, world):
    flex = make_account("1001", "user", "Flo", email="flo@example.test", teams=["Flex Cube"])
    card = make_account("1002", "user", "Cara", email="cara@example.test", teams=["Card Management"])
    fsubs = team_subjects(flex.get("/api/ops/my").get_json())
    csubs = team_subjects(card.get("/api/ops/my").get_json())
    assert fsubs == {"Flex pending one", "Flex replied", "Shared flex and card"}
    assert csubs == {"Card pending one", "Shared flex and card"}
    assert "Card pending one" not in fsubs and "Flex pending one" not in csubs   # neither sees the other's team
    assert "Shared flex and card" in fsubs and "Shared flex and card" in csubs   # the two-team mail reaches both


def test_an_employee_without_a_team_gets_an_empty_list_and_a_hint(make_account, world):
    emp = make_account("1003", "user", "Nemo", email="nemo@example.test")
    mail = emp.get("/api/ops/my").get_json()["email"]
    assert mail["team_emails"] == [] and mail["team_emails_hint"] == "no_team"
    assert mail["team_summary"]["teams"] == [] and mail["team_summary"]["pending"] == 0


def test_a_team_removed_from_the_settings_does_not_break_the_account(admin, make_account, world):
    emp = make_account("1001", "user", "Flo", email="flo@example.test", teams=["Flex Cube"])
    cfg = admin.get("/api/ops/settings").get_json()["settings"]
    cfg["email"]["teams"] = [t for t in cfg["email"]["teams"] if t != "Flex Cube"]
    assert admin.put("/api/ops/settings", cfg).status_code == 200
    # the scope comes from the account, not the live settings, so the employee still works and still sees their rows
    body = emp.get("/api/ops/my")
    assert body.status_code == 200 and "Flex pending one" in team_subjects(body.get_json())
    # and the admin can still edit the account without being forced to drop the now-unknown team
    assert admin.put("/api/admin/users/1001", {"name": "Flo Renamed"}).status_code == 200


# --------------------------------------------------------------------- managers act only on their teams
def test_manager_with_two_teams_acts_on_both_but_not_a_third(make_account, world):
    m = make_account("mona2", "manager", "Mona Two", email="mona2@example.test",
                     teams=["Flex Cube", "Card Management"])
    assert m.post("/api/ops/email/no-action", {"message_id": "<flex1>"}).status_code == 200
    assert m.post("/api/ops/email/no-action", {"message_id": "<card1>"}).status_code == 200
    denied = m.post("/api/ops/email/no-action", {"message_id": "<third1>"})
    assert denied.status_code == 403 and "another team" in denied.get_json()["error"]
    # a mail of a team outside their scope is also invisible on the Email page (and so is any Unclassified one)
    page = m.get("/api/ops/email").get_json()
    subjects = {row["subject"] for row in page["data"]["rows"]}
    assert "Third pending one" not in subjects and MARK not in json.dumps(page, ensure_ascii=False)


def test_classification_rules_are_limited_to_a_managers_own_teams(make_account, world):
    m = make_account("mona3", "manager", "Mona Three", email="mona3@example.test", teams=["Flex Cube"])
    assert m.post("/api/ops/email/classification/rule", {"keyword": "flexword", "team": "Flex Cube"}).status_code == 200
    denied = m.post("/api/ops/email/classification/rule", {"keyword": "cardword", "team": "Card Management"})
    assert denied.status_code == 403


# --------------------------------------------------------------------- import
def test_import_users_with_a_teams_column(admin, app, world):
    csv = ("user_id,name,department,role,email,teams\n"
           "7001,Multi One,IT,user,m1@example.test,Flex Cube; Card Management\n"
           "7002,Single Two,IT,user,m2@example.test,Third Party\n")
    resp = admin.post("/api/admin/users/import", data={"file": (io.BytesIO(csv.encode("utf-8")), "u.csv")},
                      content_type="multipart/form-data")
    body = resp.get_json()
    assert resp.status_code == 200 and body["created"] == 2
    creds = {c["user_id"]: c["password"] for c in body["credentials"]}
    expected = {"7001": ["Flex Cube", "Card Management"], "7002": ["Third Party"]}
    for uid, teams in expected.items():
        b = Browser(app)
        b.login(uid, creds[uid])
        b.client.post("/account/password", data={"current_password": creds[uid], "new_password": "ImportPass123",
                                                 "confirm_password": "ImportPass123", "csrf_token": b.csrf})
        mail = b.get("/api/ops/my").get_json()["email"]
        assert mail["team_summary"]["teams"] == teams
        assert MARK not in json.dumps(mail, ensure_ascii=False)


# --------------------------------------------------------------------- nothing leaks, one set of numbers
GET_ENDPOINTS = ("/api/ops/my", "/api/ops/shared", "/api/ops/email", "/api/ops/sla", "/api/ops/itsm",
                 "/api/ops/overview", "/api/ops/events", "/api/ops/reports", "/api/ops/health", "/api/ops/settings",
                 "/api/ops/jobs", "/export/ops/email.xlsx", "/api/ops/email/history", "/api/ops/sla/history",
                 "/api/ops/itsm/history")


def test_no_get_endpoint_ever_leaks_an_unclassified_mail_to_an_employee(make_account, world):
    emp = make_account("1001", "user", "Flo", email="flo@example.test", teams=["Flex Cube", "Card Management"])
    for path in GET_ENDPOINTS:
        assert MARK not in emp.get(path).get_data(as_text=True), path       # 200 or 403 - the marker never appears


def test_the_shared_strip_is_identical_on_both_dashboards_for_a_multi_team_employee(make_account, world):
    emp = make_account("1001", "user", "Flo", email="flo@example.test", teams=["Flex Cube", "Card Management"])

    def strip(s):
        return {k: v for k, v in s.items() if k != "generated_at"}
    from_shared = strip(emp.get("/api/ops/shared").get_json())
    from_my = strip(emp.get("/api/ops/my").get_json()["shared"])
    assert from_shared == from_my
    assert from_shared["email"]["scope"] == "teams" and from_shared["email"]["teams"] == ["Flex Cube", "Card Management"]


# --------------------------------------------------------------------- performance of the scoping itself
def test_scoping_two_thousand_rows_is_fast():
    from calllog.ops import overlay
    teams = ["Flex Cube", "Card Management", "Third Party", "Open System", "Unclassified"]
    rows = [email_row(f"<r{i}>", subject=f"Subject {i}", team=teams[i % len(teams)],
                      all_teams=[teams[i % len(teams)]], teams_pending=[teams[i % len(teams)]])
            for i in range(2000)]
    scope = ("Flex Cube", "Card Management")

    def once():
        start = time.perf_counter()
        items, summary = overlay.team_mail(rows, scope)
        assert all(i["team"] != "Unclassified" for i in items)            # the bucket never survives the scope
        return time.perf_counter() - start
    assert min(once() for _ in range(3)) < 0.03                           # ~30 ms for 2000 rows, well inside budget
