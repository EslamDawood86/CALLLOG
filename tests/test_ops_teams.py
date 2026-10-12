"""Multi-team accounts: an employee sees the e-mails of ALL their own teams - and never an "Unclassified" one.

Covers the account profile (``teams`` list, the older single ``team``, import column), the employee's view
(``/api/ops/my`` -> ``email.team_emails`` / ``team_summary`` and the scoped ``/shared`` strip), a manager who holds
several teams, and a sweep of every GET endpoint as an employee that looks for a marker that only an Unclassified
mail carries (docs/HUB-DESIGN.md, sections 3, 3.1, 3.2 and 7).
"""
import io
import json
import re
import time
import zipfile

import pytest

from calllog.ops import overlay, schemas
from conftest import Browser
# helpers and fixtures shared with the API tests
from test_ops_api import (audit_actions, beat, email_data, email_row, hub, itsm_data, make_account, mgr,  # noqa: F401
                          flex_mgr, ops, put_snapshot, replied_row, sla_data, sla_request)

MARK = "UNCLASSIFIED-MARKER"            # carried by every field of the Unclassified mails below
HANDED = "HANDED-OVER-MARKER"           # an Unclassified mail that was assigned to Alice by name
FLEX, CARD, THIRD, OPEN, UNC = "Flex Cube", "Card Management", "Third Party", "Open System", "Unclassified"
ALICE = "alice@example.test"


# ---------------------------------------------------------------------------- builders
def mail(mid, team, **kw):
    """A pending mail of one team (``row_id`` as the engine writes it: message id | team)."""
    return email_row(mid, team=team, all_teams=[team], teams_pending=[team], row_id=f"{mid}|{team}", **kw)


def answered(mid, team, by="bob@example.test", minutes=20, **kw):
    return replied_row(mid, by, minutes, team=team, all_teams=[team], teams_replied=[team], row_id=f"{mid}|{team}", **kw)


def unclassified(mid, **kw):
    return mail(mid, UNC, subject=f"{MARK} {mid}", customer_name=f"{MARK} customer", customer_email=f"{MARK.lower()}@example.test",
                body_preview=f"{MARK} body", reply_body_preview=f"{MARK} reply", detected_systems="", **kw)


def board_rows():
    return [
        mail("<f1>", FLEX, subject="Flex pending oldest", received_time="2026-10-11T07:00:00", current_waiting_time="2 H",
             sla_status="Breached", escalated=True, duration_minutes=120, body_preview="private words", customer_email="c@example.test"),
        mail("<f2>", FLEX, subject="Flex pending newer", received_time="2026-10-11T08:30:00"),
        answered("<f3>", FLEX, subject="Flex replied", received_time="2026-10-11T08:00:00"),
        mail("<c1>", CARD, subject="Card pending", received_time="2026-10-11T08:00:00"),
        answered("<c2>", CARD, minutes=90, subject="Card replied late", received_time="2026-10-11T07:30:00"),
        mail("<c3>", CARD, subject="Card newsletter", status="No Action Needed", sla_status="N/A", teams_pending=[],
             no_action_reason="newsletter"),
        mail("<t1>", THIRD, subject="Third pending", received_time="2026-10-11T08:10:00"),
        email_row("<m1>", subject="Shared by two teams", team=THIRD, all_teams=[THIRD, CARD], teams_pending=[THIRD, CARD],
                  row_id="<m1>|Third Party", received_time="2026-10-11T08:20:00"),
        mail("<o1>", OPEN, subject="Open system pending", received_time="2026-10-11T08:40:00"),
        unclassified("<u1>"),
        replied_row("<u2>", "bob@example.test", 20, team=UNC, all_teams=[UNC], teams_replied=[UNC], row_id="<u2>|Unclassified",
                    subject=f"{MARK} replied", customer_name=f"{MARK} customer", customer_email=f"{MARK.lower()}@example.test",
                    body_preview=f"{MARK} body", reply_body_preview=f"{MARK} reply", detected_systems=""),
        mail("<u3>", UNC, subject=HANDED, customer_name="Handed customer", detected_systems=""),
    ]


@pytest.fixture
def board(ops, hub):
    """Replaces the API tests' snapshot by one that has several teams, a multi-team mail and Unclassified mails, and
    puts the marker into every place an Unclassified mail could leak from (events, ratings, a report file)."""
    rows = board_rows()
    put_snapshot(ops, "email", email_data(rows))
    ops.set_override("assignments", "<u3>", {"assigned_to_email": ALICE, "assigned_to_name": "Alice", "note": "yours now",
                                             "marked_by": "Mona Manager", "marked_at": "2026-10-11T08:31:00"})
    ops.set_override("quality", "<u2>", {"rating": "Good", "note": f"{MARK} rating note", "reasons": "", "subject": f"{MARK} replied",
                                         "replied_by_email": "bob@example.test", "reviewed_by": "Mona Manager",
                                         "reviewed_at": "2026-10-11T08:45:00", "team": UNC})
    ops.add_events([
        {"ts": "2026-10-11T09:01:00", "module": "email", "level": "info", "kind": "email_unclassified", "text": "1 unclassified",
         "items": [{"subject": f"{MARK} pending", "team": UNC}]},
        {"ts": "2026-10-11T09:02:00", "module": "email", "level": "danger", "kind": "email_escalated", "text": "1 e-mail",
         "items": [{"subject": "Flex pending oldest", "team": FLEX}]}])
    (ops.report_dir("email") / "marker-report.html").write_text(f"<p>{MARK} report</p>", encoding="utf-8")
    for job in schemas.JOBS:
        beat(ops, job)
    return rows


def make_team_account(make_account, uid, name, teams, **kw):
    return make_account(uid, "user", name, email=f"{name.split()[0].lower()}@example.test", teams=teams, **kw)


@pytest.fixture
def carl(make_account, board):
    return make_team_account(make_account, "1003", "Carl Employee", [FLEX, CARD])


@pytest.fixture
def dina(make_account, board):
    return make_team_account(make_account, "1004", "Dina Employee", [THIRD])


@pytest.fixture
def eli(make_account, board):
    return make_team_account(make_account, "1005", "Eli Employee", [])


@pytest.fixture
def alice(make_account, board):
    """An older account: only the single `team` text, no `teams` list."""
    return make_account("1001", "user", "Alice Employee", email=ALICE, team=FLEX)


def my_email(who, path="/api/ops/my"):
    return who.get(path).get_json()["email"]


def subjects(items):
    return [i["subject"] for i in items]


# ---------------------------------------------------------------------------- the employee's own teams
def test_an_employee_gets_the_union_of_their_teams_pending_first_and_never_unclassified(carl):
    mail_ = my_email(carl)
    items = mail_["team_emails"]
    # pending first (oldest first), then replied (oldest first); the no-action newsletter is counted, not listed
    assert subjects(items) == ["Flex pending oldest", "Card pending", "Shared by two teams", "Flex pending newer",
                               "Card replied late", "Flex replied"]
    assert [i["status"] for i in items] == ["Pending Reply"] * 4 + ["Replied"] * 2
    text = json.dumps(mail_, ensure_ascii=False)
    for hidden in (MARK, HANDED, "Third pending", "Open system pending", "Card newsletter", "Handed customer"):
        assert hidden not in text, hidden
    summary = mail_["team_summary"]
    assert summary["teams"] == [FLEX, CARD] and summary["all_teams"] is False
    assert (summary["pending"], summary["replied"], summary["no_action"]) == (4, 2, 1)
    assert (summary["sla_breached"], summary["escalated"], summary["reply_rate"]) == (2, 1, 33.33)
    assert (summary["listed"], summary["more"]) == (6, 0) and mail_["team_emails_hint"] == ""


def test_team_emails_carry_only_the_whitelisted_fields(carl):
    items = my_email(carl)["team_emails"]
    assert items and all(set(i) == set(overlay.TEAM_MAIL_FIELDS) for i in items)
    first = items[0]
    assert first["row_id"] == "<f1>|Flex Cube" and first["message_id"] == "<f1>" and first["escalated"] is True
    assert first["current_waiting_time"] == "2 H" and first["sla_status"] == "Breached" and first["team"] == FLEX
    text = json.dumps(items)
    for private in ("private words", "c@example.test", "cust-", "body_preview", "customer_email", "quality", "entry_id", "E-<"):
        assert private not in text, private


def test_two_employees_of_different_teams_are_isolated(carl, dina):
    assert set(subjects(my_email(dina)["team_emails"])) == {"Third pending", "Shared by two teams"}
    carls = set(subjects(my_email(carl)["team_emails"]))
    assert "Third pending" not in carls and "Shared by two teams" in carls            # the shared mail is Card's too
    text = json.dumps(my_email(dina))
    for other in ("Flex pending", "Card pending", "Card replied", "Flex replied", "Open system", MARK):
        assert other not in text, other
    assert my_email(dina)["team_summary"]["teams"] == [THIRD]


def test_a_mail_addressed_to_several_teams_is_visible_to_a_member_of_either(carl, dina, make_account):
    flex_only = make_team_account(make_account, "1006", "Finn Employee", [FLEX])
    open_only = make_team_account(make_account, "1007", "Olga Employee", [OPEN])
    assert "Shared by two teams" in subjects(my_email(dina)["team_emails"])                # its `team`
    assert "Shared by two teams" in subjects(my_email(carl)["team_emails"])                # only in `all_teams`
    for outsider in (flex_only, open_only):
        assert "Shared by two teams" not in subjects(my_email(outsider)["team_emails"])


def test_an_employee_without_a_team_gets_nothing_and_the_reason(eli):
    mail_ = my_email(eli)
    assert mail_["team_emails"] == [] and mail_["team_emails_hint"] == "no_team"
    summary = mail_["team_summary"]
    assert summary["teams"] == [] and (summary["pending"], summary["replied"], summary["listed"]) == (0, 0, 0)
    assert MARK not in json.dumps(mail_)
    strip = eli.get("/api/ops/shared").get_json()
    assert strip["email"]["scope"] == "teams" and strip["email"]["teams"] == []
    assert strip["email"]["business_emails"] == 0 and strip["email"]["pending"] == 0
    assert strip["calls"] == eli.get("/api/ops/my").get_json()["shared"]["calls"]            # the sector-wide blocks stay


def test_the_older_single_team_account_still_works(alice, admin):
    row = next(u for u in admin.get("/api/admin/users").get_json()["users"] if u["user_id"] == "1001")
    assert row["team"] == FLEX and row["teams"] == [FLEX]
    assert set(subjects(my_email(alice)["team_emails"])) == {"Flex pending oldest", "Flex pending newer", "Flex replied"}
    me = alice.get("/api/me").get_json()["user"]
    assert me["team"] == FLEX and me["teams"] == [FLEX]


def test_a_mail_handed_over_by_name_stays_visible_even_when_unclassified(alice, carl):
    mail_ = my_email(alice)
    assert [i["subject"] for i in mail_["assigned"]["items"]] == [HANDED]
    assert mail_["assigned"]["items"][0]["note"] == "yours now"
    assert HANDED not in json.dumps(mail_["team_emails"])                        # but it is not part of the team list
    assert MARK not in json.dumps(mail_)                                        # and nothing else Unclassified comes with it
    assert HANDED not in json.dumps(my_email(carl))                              # only the person it was handed to sees it


def test_classifying_a_mail_makes_it_visible_to_that_team_only(admin, carl, dina):
    assert admin.post("/api/ops/email/team", {"message_id": "<u1>", "team": THIRD}).status_code == 200
    assert f"{MARK} <u1>" in subjects(my_email(dina)["team_emails"])
    assert f"{MARK} <u1>" not in subjects(my_email(carl)["team_emails"])
    # moving a mail out of a team takes it away from that team's employees at once
    assert admin.post("/api/ops/email/team", {"message_id": "<f2>", "team": OPEN}).status_code == 200
    assert "Flex pending newer" not in subjects(my_email(carl)["team_emails"])
    assert my_email(carl)["team_summary"]["pending"] == 3


def test_team_emails_follow_the_overrides_made_after_the_scan(admin, carl):
    assert admin.post("/api/ops/email/no-action", {"message_id": "<c1>"}).status_code == 200
    assert admin.post("/api/ops/email/assign", {"message_id": "<f1>", "assigned_to_email": "dave@example.test",
                                                 "assigned_to_name": "Dave"}).status_code == 200
    mail_ = my_email(carl)
    assert "Card pending" not in subjects(mail_["team_emails"]) and mail_["team_summary"]["no_action"] == 2
    first = mail_["team_emails"][0]
    assert first["subject"] == "Flex pending oldest" and first["assigned_to_name"] == "Dave"
    assert "dave@example.test" not in json.dumps(mail_["team_emails"])                # the name, not the address


def test_the_list_is_capped_and_says_how_many_more(ops, make_account, board):
    rows = [mail(f"<bulk{i}>", FLEX, subject=f"Bulk {i}", received_time=f"2026-10-11T0{i % 9}:{i % 60:02d}:00") for i in range(340)]
    put_snapshot(ops, "email", email_data(rows + board_rows()))
    who = make_team_account(make_account, "1008", "Gil Employee", [FLEX])
    mail_ = my_email(who)
    assert len(mail_["team_emails"]) == overlay.TEAM_MAIL_LIMIT == 300
    summary = mail_["team_summary"]
    assert summary["pending"] == 342 and summary["listed"] == 300 and summary["more"] == 43      # 342 pending + 1 replied - 300


def test_view_as_intersects_the_viewers_and_the_employees_teams(admin, mgr, flex_mgr, carl):
    assert set(subjects(admin.get("/api/ops/my?as=1003").get_json()["email"]["team_emails"])) == {
        "Flex pending oldest", "Flex pending newer", "Flex replied", "Card pending", "Card replied late", "Shared by two teams"}
    scoped = flex_mgr.get("/api/ops/my?as=1003").get_json()["email"]               # Flex Cube manager looks at Carl
    assert set(subjects(scoped["team_emails"])) == {"Flex pending oldest", "Flex pending newer", "Flex replied"}
    assert scoped["team_summary"]["teams"] == [FLEX]
    open_mgr = Browser(flex_mgr.client.application)
    assert open_mgr is not None


def test_view_as_by_a_manager_of_another_team_shows_nothing_and_says_so(make_account, dina):
    outsider = make_account("zed", "manager", "Zed Manager", email="zed@example.test", teams=[OPEN])
    mail_ = outsider.get("/api/ops/my?as=1004").get_json()["email"]
    assert mail_["team_emails"] == [] and mail_["team_emails_hint"] == "out_of_scope"
    assert "Third pending" not in json.dumps(mail_)


# ---------------------------------------------------------------------------- the "Team status" strip
def strip_of(who, path="/api/ops/shared"):
    body = who.get(path).get_json()
    return {k: v for k, v in (body["shared"] if path.endswith("/my") else body).items() if k != "generated_at"}


def test_the_strip_is_scoped_and_the_same_wherever_it_is_drawn(admin, mgr, flex_mgr, carl, dina, eli):
    for who in (admin, mgr, flex_mgr, carl, dina, eli):
        assert strip_of(who) == strip_of(who, "/api/ops/my"), "the strip on /my must equal /shared"
    everything = strip_of(admin)["email"]
    assert everything["scope"] == "all" and everything["teams"] == []
    assert everything["business_emails"] == 12 and everything["pending"] == 8        # every row, Unclassified ones included
    mine = strip_of(carl)["email"]
    assert mine["scope"] == "teams" and mine["teams"] == [FLEX, CARD]
    assert (mine["business_emails"], mine["pending"], mine["replied"], mine["no_action_needed"]) == (7, 4, 2, 1)
    assert (mine["sla_breached"], mine["escalated_count"]) == (2, 1)
    assert strip_of(mgr)["email"]["scope"] == "all" and strip_of(mgr)["email"]["business_emails"] == 12
    assert strip_of(flex_mgr)["email"]["teams"] == [FLEX] and strip_of(flex_mgr)["email"]["business_emails"] == 3
    # the headline numbers of the list are the strip's numbers
    summary = my_email(carl)["team_summary"]
    assert (summary["pending"], summary["replied"], summary["no_action"]) == (mine["pending"], mine["replied"], mine["no_action_needed"])
    assert summary["reply_rate"] == mine["reply_rate"] and summary["sla_breached"] == mine["sla_breached"]
    # calls, security requests and ITSM are the same sector-wide counts for everybody
    for who in (carl, dina, eli, flex_mgr):
        for block in ("calls", "sla", "itsm"):
            assert strip_of(who)[block] == strip_of(admin)[block], block


def test_the_strip_of_an_employee_never_counts_unclassified_mails(admin, carl):
    before = strip_of(carl)["email"]
    admin.post("/api/ops/email/no-action", {"message_id": "<u1>"})                    # the administrator acts on one
    assert strip_of(carl)["email"] == before
    admin.post("/api/ops/email/team", {"message_id": "<u1>", "team": FLEX})
    after = strip_of(carl)["email"]
    assert after["business_emails"] == before["business_emails"] + 1 and after["no_action_needed"] == before["no_action_needed"] + 1


def test_the_strip_is_counts_only(carl):
    text = json.dumps(strip_of(carl), ensure_ascii=False)
    for secret in ("Flex pending", "Card ", "Customer", "example.test", MARK, "Third pending"):
        assert secret not in text, secret


# ---------------------------------------------------------------------------- the sweep
FILL = {"module": ["email", "sla", "itsm"], "job": list(schemas.JOBS), "name": ["marker-report.html", "missing.xlsx"],
        "fmt": ["xlsx", "csv"], "filename": ["app.css"], "uid": ["1003", "1004"], "rid": ["1003-20261011-0001"]}
QUERIES = ("", "?as=1004", "?as=1003&since=2026-10-11T09:00:00&rev=x", "?day=2026-10-11&days=30&limit=500&lines=500",
           f"?q={MARK}&team=Unclassified&date=2026-10-11&from=2026-10-01&to=2026-10-11")


def expand(rule):
    """Every concrete URL a Flask rule can stand for (converter options and the names above)."""
    urls = [rule]
    for token in re.findall(r"<[^>]+>", rule):
        inner = token[1:-1]
        converter, _, name = inner.rpartition(":")
        options = re.match(r"any\((.*)\)", converter)
        values = options.group(1).split(",") if options else FILL.get(name, ["x"])
        urls = [u.replace(token, v, 1) for u in urls for v in values]
    return urls


def get_urls(app):
    urls = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if "GET" in rule.methods:
            urls += expand(rule.rule)
    return sorted(set(urls))


def body_text(resp):
    """The text of a response; a zip (xlsx) is opened so that a marker inside a workbook is found too."""
    data = resp.get_data()
    chunks = [data.decode("utf-8", errors="replace")]
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                chunks += [z.read(n).decode("utf-8", errors="replace") for n in z.namelist()]
        except zipfile.BadZipFile:
            pass
    return "\n".join(chunks + [str(resp.headers)])


def test_the_sweep_really_visits_the_hub_endpoints(app):
    urls = set(get_urls(app))
    for must in ("/api/ops/my", "/api/ops/shared", "/api/ops/email", "/api/ops/sla", "/api/ops/itsm", "/api/ops/overview",
                 "/api/ops/events", "/api/ops/health", "/api/ops/settings", "/api/ops/reports", "/api/ops/jobs",
                 "/api/ops/email/history", "/export/ops/email.xlsx", "/export/ops/email.csv",
                 "/export/ops/report/email/marker-report.html", "/api/admin/audit", "/api/admin/users", "/api/me", "/email", "/hub"):
        assert must in urls, must
    assert len(urls) >= 40


@pytest.mark.parametrize("who", ["carl", "dina", "eli"])
def test_no_get_endpoint_shows_an_unclassified_mail_to_an_employee(app, request, board, who):
    """The marker sits in the subject, customer, address, body, reply, rating note, an event and a report of
    Unclassified mails. An employee holding other teams (or none) must not find it anywhere."""
    employee = request.getfixturevalue(who)
    checked = 0
    for url in get_urls(app):
        for query in QUERIES:
            resp = employee.get(url + query)
            assert resp.status_code < 500, (url + query, resp.status_code)
            text = body_text(resp)
            assert MARK.lower() not in text.lower(), url + query
            assert HANDED not in text, url + query                    # handed to Alice by name, not to this person
            checked += 1
    assert checked >= 200


def test_the_marker_is_reachable_for_those_who_may_see_it(admin, mgr, ops, board):
    """Guards the sweep against passing for the wrong reason: the people allowed to see it do find it."""
    for who, path in ((admin, "/api/ops/email"), (mgr, "/api/ops/email"), (admin, "/api/ops/events?day=2026-10-11"),
                      (mgr, "/api/ops/events?day=2026-10-11"), (admin, "/export/ops/report/email/marker-report.html"),
                      (admin, "/export/ops/email.csv")):
        assert MARK in body_text(who.get(path)), path


def test_the_person_it_was_handed_to_sees_it_and_only_there(alice, board):
    seen = [url for url in get_urls(alice.client.application) if HANDED in body_text(alice.get(url))]
    assert seen == ["/api/ops/my"]
    assert not any(MARK.lower() in body_text(alice.get(url)).lower() for url in get_urls(alice.client.application))


# ---------------------------------------------------------------------------- accounts, API and import
def users(admin):
    return {u["user_id"]: u for u in admin.get("/api/admin/users").get_json()["users"]}


def test_create_and_update_accept_a_list_a_text_and_the_older_team(admin, app, clock):
    def create(uid, **kw):
        return admin.post("/api/admin/users", {"user_id": uid, "name": uid.title(), "password": "TempPass123", **kw})
    assert create("t1", teams=[FLEX, CARD]).get_json()["user"]["teams"] == [FLEX, CARD]
    made = create("t2", teams="flex cube; CARD MANAGEMENT ; flex cube").get_json()["user"]
    assert made["teams"] == [FLEX, CARD] and made["team"] == FLEX                      # canonical spelling, no duplicates
    assert create("t3", team=CARD).get_json()["user"]["teams"] == [CARD]
    both = create("t4", team=CARD, teams=[THIRD]).get_json()["user"]
    assert both["teams"] == [THIRD] and both["team"] == THIRD                          # `teams` wins over the older `team`
    assert create("t5").get_json()["user"]["teams"] == []
    updated = admin.put("/api/admin/users/t1", {"teams": [OPEN]}).get_json()["user"]
    assert updated["teams"] == [OPEN] and updated["team"] == OPEN
    assert admin.put("/api/admin/users/t1", {"teams": []}).get_json()["user"] == {**updated, "teams": [], "team": ""}
    assert admin.put("/api/admin/users/t1", {"name": "Renamed"}).get_json()["user"]["teams"] == []           # untouched when not sent
    assert users(admin)["t3"]["teams"] == [CARD]
    entries = {e["target"]: e["details"] for e in app.extensions["store"].read_audit("2026-10-11", "2026-10-11")
               if e["action"] == "user_created"}
    assert entries["t1"]["teams"] == [FLEX, CARD]


def test_team_rules_for_each_role(admin):
    def create(uid, role, **kw):
        return admin.post("/api/admin/users", {"user_id": uid, "name": uid.title(), "role": role, "password": "TempPass123", **kw})
    assert create("a1", "user", teams=["__all__"]).status_code == 400                  # an employee cannot follow every team
    assert create("a2", "user", teams=[UNC]).status_code == 400                        # nor sort out the Unclassified bucket
    assert create("a3", "user", teams=[FLEX, "No Such Team"]).status_code == 400
    assert create("a4", "user", teams=["x" * 90]).status_code == 400
    assert create("a5", "user", teams=[f"Team {i}" for i in range(41)]).status_code == 400
    all_ = create("a6", "manager", teams=["__all__", FLEX])
    assert all_.status_code == 201 and all_.get_json()["user"]["teams"] == ["__all__"] and all_.get_json()["user"]["team"] == "__all__"
    assert create("a7", "manager", teams=[UNC, FLEX]).status_code == 201               # a manager may hold Unclassified
    assert create("a8", "user", teams=" ").get_json()["user"]["teams"] == []
    # turning a manager into an employee: the all-teams marker must be replaced in the same request
    assert admin.put("/api/admin/users/a6", {"role": "user", "teams": [FLEX]}).status_code == 200
    assert admin.put("/api/admin/users/a7", {"role": "user"}).status_code == 200      # nothing new is asked: Unclassified stays as it was


def test_changing_teams_is_audited_with_the_lists(app, admin, clock):
    admin.post("/api/admin/users", {"user_id": "t1", "name": "T", "password": "TempPass123", "teams": [FLEX]})
    admin.put("/api/admin/users/t1", {"teams": [FLEX, CARD, THIRD]})
    admin.put("/api/admin/users/t1", {"teams": [CARD, THIRD, FLEX]})                  # same teams, other order: still a change of order
    changes = [e["details"] for e in app.extensions["store"].read_audit("2026-10-11", "2026-10-11") if e["action"] == "user_updated"]
    assert changes[0] == {"teams": {"from": [FLEX], "to": [FLEX, CARD, THIRD]}}
    assert all("team" not in c for c in changes)


def test_import_reads_a_teams_column(admin):
    csv_text = ('user_id,name,department,role,email,teams,team\n'
                '6001,Ann,IT,,ann@example.test,Flex Cube; Card Management,\n'
                '6002,Ben,IT,,ben@example.test,"Flex Cube, Third Party",\n'
                '6003,Cy,IT,manager,cy@example.test,__all__,\n'
                '6004,Di,IT,,di@example.test,,Open System\n'
                '6005,Ed,IT,,ed@example.test,Third Party,Card Management\n'
                '6006,Flo,IT,,flo@example.test,__all__,\n'
                '6007,Gus,IT,,gus@example.test,No Such Team,\n'
                '6008,Hal,IT,,hal@example.test,Unclassified,\n'
                '6009,Ivy,IT,manager,ivy@example.test,flex cube;Unclassified,\n')
    r = admin.post("/api/admin/users/import", data={"file": (io.BytesIO(csv_text.encode()), "users.csv")},
                   content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert {s["user_id"] for s in body["skipped"]} == {"6006", "6007", "6008"} and body["created"] == 6
    got = users(admin)
    assert got["6001"]["teams"] == [FLEX, CARD] and got["6001"]["team"] == FLEX
    assert got["6002"]["teams"] == [FLEX, THIRD]
    assert got["6003"]["teams"] == ["__all__"] and got["6003"]["role"] == "manager"
    assert got["6004"]["teams"] == [OPEN]                                              # only the older column given
    assert got["6005"]["teams"] == [THIRD]                                             # `teams` wins when both are given
    assert got["6009"]["teams"] == [FLEX, UNC]
    reasons = " ".join(s["reason"] for s in body["skipped"])
    assert "teams" in reasons.lower() or "team" in reasons.lower()


def test_import_reads_teams_from_an_excel_file(admin):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["User ID", "Name", "Department", "Team List"])
    ws.append([7001, "Xena", "IT", "Flex Cube; Card Management"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    r = admin.post("/api/admin/users/import", data={"file": (buf, "users.xlsx")}, content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["created"] == 1
    assert users(admin)["7001"]["teams"] == [FLEX, CARD]


def test_a_team_renamed_or_removed_in_the_settings_breaks_no_account(admin, make_account, ops, board):
    carl = make_team_account(make_account, "1003", "Carl Employee", [FLEX, THIRD])
    cfg = admin.get("/api/ops/settings").get_json()["settings"]
    cfg["email"]["teams"] = [t for t in cfg["email"]["teams"] if t != THIRD] + ["Third Party Ops"]
    assert admin.put("/api/ops/settings", cfg).status_code == 200
    # the account keeps its teams as text: it still works, and the removed team is simply not offered any more
    assert carl.get("/api/ops/my").status_code == 200 and carl.get("/api/ops/shared").status_code == 200
    assert "Third pending" in subjects(my_email(carl)["team_emails"])                  # the old scan still names the team
    row = users(admin)["1003"]
    assert row["teams"] == [FLEX, THIRD]
    assert admin.put("/api/admin/users/1003", {"name": "Carl Renamed", "teams": [FLEX, THIRD, "Third Party Ops"]}).status_code == 200
    assert admin.put("/api/admin/users/1003", {"teams": [FLEX, "Gone Team"]}).status_code == 400    # a new unknown name is refused
    # the next scan names the renamed team: the employee follows the name, so the administrator ticks the new one
    renamed = [dict(r, team="Third Party Ops", all_teams=["Third Party Ops"], row_id=f"{r['message_id']}|Third Party Ops")
               if r["team"] == THIRD else r for r in board_rows()]
    put_snapshot(ops, "email", email_data(renamed))
    assert "Third pending" in subjects(my_email(carl)["team_emails"])
    assert admin.put("/api/admin/users/1003", {"teams": [FLEX]}).status_code == 200
    assert "Third pending" not in subjects(my_email(carl)["team_emails"])


# ---------------------------------------------------------------------------- a manager with several teams
@pytest.fixture
def mira(make_account, board):
    return make_account("mira", "manager", "Mira Manager", email="mira@example.test", teams=[FLEX, CARD])


def test_a_manager_with_two_teams_sees_and_counts_exactly_those(mira):
    body = mira.get("/api/ops/email").get_json()
    shown = {r["subject"] for r in body["data"]["rows"]}
    assert shown == {"Flex pending oldest", "Flex pending newer", "Flex replied", "Card pending", "Card replied late",
                     "Card newsletter", "Shared by two teams"}
    assert MARK not in json.dumps(body) and "Third pending" not in json.dumps(body)
    assert body["options"]["scope_teams"] == [FLEX, CARD] and body["options"]["scope"] == f"{FLEX}, {CARD}"
    assert body["data"]["stats"]["business_emails"] == 7
    ov = mira.get("/api/ops/overview").get_json()
    assert ov["scope_teams"] == [FLEX, CARD] and ov["email"]["unclassified"] == 0
    assert "Open system pending" not in json.dumps(ov)
    csv_body = body_text(mira.get("/export/ops/email.csv"))
    assert "Card pending" in csv_body and "Third pending" not in csv_body and MARK not in csv_body


def test_a_manager_with_two_teams_acts_on_both_and_gets_403_on_a_third(mira, ops):
    assert mira.post("/api/ops/email/assign", {"message_id": "<f1>", "assigned_to_email": "dave@example.test"}).status_code == 200
    assert mira.post("/api/ops/email/no-action", {"message_id": "<c1>"}).status_code == 200
    assert mira.delete("/api/ops/email/no-action", json={"message_id": "<c1>"}).status_code == 200
    assert mira.post("/api/ops/email/team", {"message_id": "<f2>", "team": CARD}).status_code == 200
    assert mira.post("/api/ops/email/quality", {"message_id": "<c2>", "rating": "Good"}).status_code == 200
    assert mira.post("/api/ops/email/assign", {"message_id": "<m1>", "assigned_to_email": "dave@example.test"}).status_code == 200
    for message_id in ("<t1>", "<o1>", "<u1>"):
        assert mira.post("/api/ops/email/assign", {"message_id": message_id, "assigned_to_email": "dave@example.test"}).status_code == 403
        assert mira.post("/api/ops/email/no-action", {"message_id": message_id}).status_code == 403
        assert mira.post("/api/ops/email/team", {"message_id": message_id, "team": FLEX}).status_code == 403
    assert mira.post("/api/ops/email/quality", {"message_id": "<u2>", "rating": "Good"}).status_code == 403
    assert "<t1>" not in ops.overrides("assignments") and "<u1>" not in ops.overrides("no_action")


def test_a_manager_decides_appeals_of_either_team_only(admin, mira, make_account, ops):
    replier = make_account("ron", "user", "Ron Employee", email="bob@example.test", teams=[THIRD])
    third = answered("<t2>", THIRD, subject="Third replied")
    put_snapshot(ops, "email", email_data(board_rows() + [third]))
    assert admin.post("/api/ops/email/quality", {"message_id": "<t2>", "rating": "Needs Improvement"}).status_code == 200
    assert admin.post("/api/ops/email/quality", {"message_id": "<c2>", "rating": "Needs Improvement"}).status_code == 200
    assert replier.post("/api/ops/email/quality/appeal", {"message_id": "<t2>", "appeal": "unfair rating"}).status_code == 200
    assert replier.post("/api/ops/email/quality/appeal", {"message_id": "<c2>", "appeal": "unfair rating"}).status_code == 200
    assert mira.post("/api/ops/email/quality/decision", {"message_id": "<c2>", "decision": "Accepted"}).status_code == 200
    assert mira.post("/api/ops/email/quality/decision", {"message_id": "<t2>", "decision": "Accepted"}).status_code == 403


def test_classification_rules_are_limited_to_the_managers_teams(admin, mira, ops):
    rule = "/api/ops/email/classification/rule"
    assert admin.post(rule, {"keyword": "swift", "team": THIRD}).status_code == 200
    assert mira.post(rule, {"keyword": "ussd", "team": FLEX}).status_code == 200
    assert mira.post(rule, {"keyword": "atm", "team": CARD}).status_code == 200
    assert mira.post(rule, {"keyword": "other", "team": THIRD}).status_code == 403            # a third team
    assert mira.post(rule, {"keyword": "swift", "team": FLEX}).status_code == 403             # would take over another team's keyword
    assert mira.delete(rule, json={"keyword": "swift"}).status_code == 403
    assert mira.post(rule, {"keyword": "oops", "team": UNC}).status_code == 400
    assert mira.delete(rule, json={"keyword": "ussd"}).status_code == 200
    rules = mira.get("/api/ops/email").get_json()["options"]["system_team_map"]
    assert rules == {"atm": CARD}                                                              # only the rules of its own teams are shown
    assert admin.get("/api/ops/settings").get_json()["settings"]["email"]["classification"]["system_team_map"].get("swift") == THIRD


def test_a_manager_of_two_teams_sees_only_their_events(mira, ops):
    ops.add_events([{"ts": "2026-10-11T09:10:00", "module": "email", "level": "info", "kind": "email_new", "text": "3 new",
                     "items": [{"subject": "In Flex", "team": FLEX}, {"subject": "In Card", "team": CARD},
                               {"subject": "In Third", "team": THIRD}, {"subject": "Nowhere", "team": UNC}]}])
    text = json.dumps(mira.get("/api/ops/events?day=2026-10-11").get_json())
    assert "In Flex" in text and "In Card" in text and "In Third" not in text and "Nowhere" not in text and MARK not in text


def test_an_employee_cannot_act_or_read_what_the_managers_use(carl, board):
    for method, path, body in (("POST", "/api/ops/email/assign", {"message_id": "<f1>", "assigned_to_email": ALICE}),
                               ("POST", "/api/ops/email/team", {"message_id": "<u1>", "team": FLEX}),
                               ("POST", "/api/ops/email/classification/rule", {"keyword": "swift", "team": FLEX}),
                               ("GET", "/api/ops/email", None), ("GET", "/api/ops/events", None), ("GET", "/api/ops/overview", None)):
        assert carl.api(method, path, json=body).status_code == 403, path


# ---------------------------------------------------------------------------- speed and caching
def big_board(n=2000):
    names = [FLEX, CARD, THIRD, OPEN, UNC, "MFA Team"]
    rows = []
    for i in range(n):
        team = names[i % len(names)]
        make = answered if i % 3 == 0 else mail
        rows.append(make(f"<big{i}>", team, subject=f"Big {i}", received_time=f"2026-10-11T0{i % 9}:{i % 60:02d}:00"))
    return rows


def test_team_mail_over_2000_rows_is_fast():
    rows = big_board()
    best = 1e9
    for _ in range(5):
        started = time.perf_counter()
        items, summary = overlay.team_mail(rows, (FLEX, CARD))
        best = min(best, time.perf_counter() - started)
    assert len(items) == 300 and summary["pending"] + summary["replied"] == 2 * 2000 // 6 + 1
    assert best < 0.030, f"{best * 1000:.1f} ms"
    assert all(overlay.team_in((FLEX, CARD), i["team"]) for i in items)


def test_the_list_is_computed_once_per_snapshot_version_and_team_set(monkeypatch, make_account, ops, board, admin):
    calls = []
    real = overlay.team_mail

    def counting(rows, scope, limit=overlay.TEAM_MAIL_LIMIT):
        calls.append(overlay.scope_key(scope))
        return real(rows, scope, limit)
    monkeypatch.setattr(overlay, "team_mail", counting)
    first = make_team_account(make_account, "1021", "Una Employee", [FLEX, CARD])
    second = make_team_account(make_account, "1022", "Vic Employee", [CARD, FLEX])               # same set, other order
    third = make_team_account(make_account, "1023", "Wes Employee", [THIRD])
    for who in (first, second, first, second):
        assert who.get("/api/ops/my").status_code == 200
    assert len(calls) == 1
    third.get("/api/ops/my")
    third.get("/api/ops/my")
    assert len(calls) == 2 and calls[0] == (CARD.casefold(), FLEX.casefold()) and calls[1] == (THIRD.casefold(),)
    admin.post("/api/ops/email/no-action", {"message_id": "<f2>"})                                  # an override changes the version
    first.get("/api/ops/my")
    assert len(calls) == 3
    put_snapshot(ops, "email", email_data(board_rows()), at="2026-10-11T09:04:00")                  # a new scan changes it too
    first.get("/api/ops/my")
    second.get("/api/ops/my")
    assert len(calls) == 4


def test_two_thousand_rows_through_the_api_stay_quick(ops, make_account, board):
    put_snapshot(ops, "email", email_data(big_board()))
    who = make_team_account(make_account, "1031", "Xan Employee", [FLEX, CARD])
    assert who.get("/api/ops/my").status_code == 200                                                 # warms the shared caches
    started = time.perf_counter()
    for _ in range(5):
        assert who.get("/api/ops/my").status_code == 200
        assert who.get("/api/ops/shared").status_code == 200
    per_request = (time.perf_counter() - started) / 10
    assert per_request < 0.15, f"{per_request * 1000:.0f} ms per request"
