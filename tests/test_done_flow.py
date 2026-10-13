"""B3 - Done / Reopen on assigned e-mails: who may do it, what it writes, what the pages and numbers show.

Reply statistics must not change (Done is the assignee's statement, not a reply). Fake data only; accounts and mails come from
``scene`` in tests/mailkit.py.
"""
import threading
from urllib.parse import quote

import pytest

from calllog.ops import overlay
from mailkit import ALICE, BOB, assignment, email_data, email_row, put_snapshot, replied_row, scene  # noqa: F401
from test_ops_api import audit_actions, make_account, ops  # noqa: F401

NOT_AVAILABLE = {"error": "That e-mail is not available."}


def done(who, mid, note=None):
    body = {"message_id": mid}
    if note is not None:
        body["note"] = note
    return who.post("/api/ops/email/done", body)


def reopen(who, mid):
    return who.api("DELETE", f"/api/ops/email/done?message_id={quote(mid, safe='')}")


def entry(scene, name):
    return scene.ops.overrides("assignments")[scene.mails[name]]


def rows(who):
    return {r["message_id"]: r for r in who.get("/api/ops/email").get_json()["data"]["rows"]}


# ----------------------------------------------------------------------------- marking done
def test_the_assignee_marks_a_mail_done(app, clock, scene):
    clock.advance(minutes=5)
    r = done(scene.alice, scene.mails["alice_open"], "Called the branch")
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True and body["already"] is False
    assert body["assignment"]["done"] is True and body["assignment"]["done_by"] == "Alice Employee"
    assert body["assignment"]["done_note"] == "Called the branch" and "marked_by_id" not in str(body)
    stored = entry(scene, "alice_open")
    assert stored["done_at"] == "2026-10-11T09:05:00" and stored["done_by"] == "Alice Employee"
    assert stored["done_by_id"] == "1001" and stored["done_note"] == "Called the branch"
    assert stored["assigned_to_email"] == ALICE and stored["note"] == "please handle"          # the rest is untouched
    assert ("ops_done", scene.mails["alice_open"], "1001") in audit_actions(app, clock)


def test_marking_done_twice_is_a_no_op_with_200(app, clock, scene):
    mid = scene.mails["alice_open"]
    done(scene.alice, mid, "first note")
    clock.advance(minutes=10)
    again = done(scene.alice, mid, "a different note")
    assert again.status_code == 200 and again.get_json()["already"] is True
    assert entry(scene, "alice_open")["done_note"] == "first note" and entry(scene, "alice_open")["done_at"] == "2026-10-11T09:00:00"
    assert [a for a in audit_actions(app, clock) if a[0] == "ops_done"] == [("ops_done", mid, "1001")]
    # a mail that was already done in the fixture behaves the same
    assert done(scene.alice, scene.mails["alice_done"]).get_json()["already"] is True


@pytest.mark.parametrize("note,ok", [(None, True), ("", True), ("x" * 300, True), ("x" * 301, False), ("  spaced   out  ", True)])
def test_the_note_is_optional_and_at_most_300_characters(scene, note, ok):
    r = done(scene.alice, scene.mails["alice_open"], note)
    assert (r.status_code == 200) is ok
    if ok:
        assert entry(scene, "alice_open")["done_note"] == (note or "").strip()      # a note may have several lines
    else:
        assert "too long" in r.get_json()["error"] and "done_at" not in entry(scene, "alice_open")


def test_a_manager_of_the_team_and_an_admin_can_mark_done_for_the_assignee(app, clock, scene):
    r = done(scene.fadi, scene.mails["alice_open"], "Checked with the branch")
    assert r.status_code == 200
    stored = entry(scene, "alice_open")
    assert stored["done_by"] == "Fadi Flex" and stored["done_by_id"] == "fadi" and stored["assigned_to_email"] == ALICE
    assert done(scene.admin, scene.mails["bob_open"]).status_code == 200
    assert entry(scene, "bob_open")["done_by"] == "admin" or entry(scene, "bob_open")["done_by_id"] == "admin"
    assert done(scene.mona, scene.mails["prep_alice"]).status_code == 200
    audit = [a for a in audit_actions(app, clock) if a[0] == "ops_done"]
    assert {a[2] for a in audit} == {"fadi", "admin", "mona"}


@pytest.mark.parametrize("actor", ["bob", "carol", "cara"])
def test_nobody_else_can_mark_it_and_cannot_tell_whether_it_exists(scene, actor):
    who = getattr(scene, actor)
    for name in ("alice_open", "free_flex"):
        r = done(who, scene.mails[name])
        assert r.status_code == 404 and r.get_json() == NOT_AVAILABLE
    ghost = done(who, "<nobody-ever-sent-this@x>")
    assert (ghost.status_code, ghost.get_json()) == (404, NOT_AVAILABLE)
    assert "done_at" not in entry(scene, "alice_open")
    assert reopen(who, scene.mails["alice_done"]).get_json() == NOT_AVAILABLE
    assert entry(scene, "alice_done")["done_at"]


def test_a_manager_in_scope_on_an_unassigned_mail_is_told_it_is_not_assigned(scene):
    r = done(scene.fadi, scene.mails["free_flex"])
    assert r.status_code == 409 and "not assigned" in r.get_json()["error"]
    assert done(scene.alice, scene.mails["free_flex"]).get_json() == NOT_AVAILABLE      # an employee cannot tell the difference
    assert done(scene.admin, "<nobody-ever-sent-this@x>").status_code == 404


def test_an_assignment_that_left_the_scan_can_still_be_finished(scene):
    mid = scene.mails["gone_alice"]
    assert done(scene.alice, mid, "late but done").status_code == 200
    assert entry(scene, "gone_alice")["done_note"] == "late but done"
    assert done(scene.cara, mid).get_json() == NOT_AVAILABLE                  # its team (Flex Cube) is remembered by the assignment
    assert reopen(scene.fadi, mid).status_code == 200 and "done_at" not in entry(scene, "gone_alice")


def test_done_validates_its_input(scene):
    assert scene.alice.post("/api/ops/email/done", {}).status_code == 400
    assert scene.alice.post("/api/ops/email/done", {"message_id": "x" * 600}).status_code == 400
    assert scene.alice.post("/api/ops/email/done", {"message_id": scene.mails["alice_open"], "note": ["a"]}).status_code < 500


# ----------------------------------------------------------------------------- reopening
def test_reopen_clears_the_mark_and_is_idempotent(app, clock, scene):
    mid = scene.mails["alice_done"]
    r = reopen(scene.alice, mid)
    assert r.status_code == 200 and r.get_json()["already"] is False and r.get_json()["assignment"]["done"] is False
    stored = entry(scene, "alice_done")
    assert not any(k in stored for k in ("done_at", "done_by", "done_by_id", "done_note"))
    assert stored["assigned_to_email"] == ALICE
    again = reopen(scene.alice, mid)
    assert again.status_code == 200 and again.get_json()["already"] is True
    assert [a for a in audit_actions(app, clock) if a[0] == "ops_reopen"] == [("ops_reopen", mid, "1001")]
    assert reopen(scene.fadi, scene.mails["alice_open"]).status_code == 200                       # never done: a quiet no-op


def test_reopen_by_a_manager_and_with_the_id_in_the_body(scene):
    mid = scene.mails["alice_done"]
    r = scene.fadi.api("DELETE", "/api/ops/email/done", json={"message_id": mid})
    assert r.status_code == 200 and "done_at" not in entry(scene, "alice_done")


def test_done_then_reopen_then_done_again_starts_fresh(clock, scene):
    mid = scene.mails["alice_open"]
    done(scene.alice, mid, "one")
    reopen(scene.alice, mid)
    clock.advance(minutes=30)
    done(scene.alice, mid, "two")
    assert entry(scene, "alice_open")["done_note"] == "two" and entry(scene, "alice_open")["done_at"] == "2026-10-11T09:30:00"


def test_an_answer_written_in_outlook_stays_done_even_after_a_reopen(scene):
    ops = scene.ops
    mid = "<answered@x>"
    row = replied_row(mid, ALICE, 15, subject="Alice answered it")
    put_snapshot(ops, "email", email_data([row]))
    ops.set_override("assignments", mid, assignment(ALICE, "Alice", done_at="2026-10-11T08:40:00", done_by="Alice Employee",
                                                    done_by_id="1001", done_note="n"))
    assert reopen(scene.alice, mid).status_code == 200                   # the manual mark is taken back ...
    r = reopen(scene.alice, mid)
    assert r.status_code == 409 and "answered" in r.get_json()["error"]  # ... but the reply cannot be taken back
    assert rows(scene.fadi)[mid]["assignment_done"] is True


# ----------------------------------------------------------------------------- an assignment that changes hands
def test_reassigning_to_somebody_else_or_unassigning_clears_the_done_mark(scene):
    mid = scene.mails["alice_done"]
    r = scene.fadi.post("/api/ops/email/assign", {"message_id": mid, "assigned_to_email": BOB, "assigned_to_name": "Bob"})
    assert r.status_code == 200
    stored = entry(scene, "alice_done")
    assert stored["assigned_to_email"] == BOB and not any(k in stored for k in ("done_at", "done_by", "done_by_id", "done_note"))
    # unassigned: the entry is gone altogether, so a new assignment later starts open
    assert scene.fadi.api("DELETE", f"/api/ops/email/assign?message_id={quote(mid, safe='')}").status_code == 200
    assert mid not in scene.ops.overrides("assignments")
    assert scene.fadi.post("/api/ops/email/assign", {"message_id": mid, "assigned_to_email": ALICE}).status_code == 200
    assert "done_at" not in entry(scene, "alice_done")


def test_a_new_note_for_the_same_person_keeps_what_they_already_did(scene):
    mid = scene.mails["alice_done"]
    r = scene.fadi.post("/api/ops/email/assign", {"message_id": mid, "assigned_to_email": ALICE.upper(), "note": "new note"})
    assert r.status_code == 200
    stored = entry(scene, "alice_done")
    assert stored["note"] == "new note" and stored["done_at"] == "2026-10-11T08:50:00" and stored["done_note"] == "all fine"


def test_the_assignment_remembers_the_team_of_the_mail(scene):
    scene.fadi.post("/api/ops/email/assign", {"message_id": scene.mails["free_flex"], "assigned_to_email": BOB})
    assert entry(scene, "free_flex")["team"] == "Flex Cube"
    scene.cara.post("/api/ops/email/assign", {"message_id": scene.mails["free_card"], "assigned_to_email": BOB})
    assert entry(scene, "free_card")["team"] == "Card Management"


def test_a_mail_sent_to_two_teams_is_filed_under_the_team_of_the_manager_who_assigned_it(scene):
    ops = scene.ops
    flex = email_row("<two@x>", subject="Two teams", is_split_child=True, row_id="<two@x>|Flex Cube",
                     split_sibling_teams=["Card Management"])
    card = email_row("<two@x>", subject="Two teams", team="Card Management", all_teams=["Card Management"],
                     teams_pending=["Card Management"], is_split_child=True, row_id="<two@x>|Card Management",
                     split_sibling_teams=["Flex Cube"])
    put_snapshot(ops, "email", email_data([flex, card]))
    assert scene.cara.post("/api/ops/email/assign", {"message_id": "<two@x>", "assigned_to_email": BOB}).status_code == 200
    assert ops.overrides("assignments")["<two@x>"]["team"] == "Card Management"
    scene.cara.api("DELETE", "/api/ops/email/assign?message_id=" + quote("<two@x>", safe=""))
    assert scene.fadi.post("/api/ops/email/assign", {"message_id": "<two@x>", "assigned_to_email": BOB}).status_code == 200
    assert ops.overrides("assignments")["<two@x>"]["team"] == "Flex Cube"
    assert scene.mona.post("/api/ops/email/assign", {"message_id": "<two@x>", "assigned_to_email": ALICE}).status_code == 200
    assert ops.overrides("assignments")["<two@x>"]["team"] == "Flex Cube"                # an every-team manager: the first row


def test_a_manager_can_unassign_a_mail_that_left_the_scan_only_inside_their_team(scene):
    gone = quote(scene.mails["gone_alice"], safe="")
    assert scene.cara.api("DELETE", f"/api/ops/email/assign?message_id={gone}").status_code == 403
    assert scene.fadi.api("DELETE", f"/api/ops/email/assign?message_id={gone}").status_code == 200
    assert scene.fadi.api("DELETE", f"/api/ops/email/assign?message_id={gone}").status_code == 404
    unknown = quote("<never-seen@x>", safe="")
    assert scene.fadi.api("DELETE", f"/api/ops/email/assign?message_id={unknown}").status_code == 404


# ----------------------------------------------------------------------------- what the pages and numbers show
def test_rows_carry_the_done_fields(scene):
    seen = rows(scene.fadi)
    done_row = seen[scene.mails["alice_done"]]
    assert done_row["assignment_done"] is True and done_row["done_auto"] is False
    assert (done_row["done_at"], done_row["done_by"], done_row["done_note"]) == ("2026-10-11T08:50:00", "Alice Employee", "all fine")
    open_row = seen[scene.mails["alice_open"]]
    assert open_row["assignment_done"] is False and open_row["done_at"] == "" and open_row["done_by"] == ""
    free = seen[scene.mails["free_flex"]]
    assert free["assignment_done"] is False and free["assigned_to_email"] == ""
    done(scene.alice, scene.mails["alice_open"], "ok")
    after = rows(scene.fadi)[scene.mails["alice_open"]]
    assert after["assignment_done"] is True and after["done_by"] == "Alice Employee" and after["done_note"] == "ok"


def test_done_never_changes_the_reply_statistics(scene):
    before = scene.mona.get("/api/ops/email").get_json()["data"]["stats"]
    done(scene.alice, scene.mails["alice_open"])
    done(scene.fadi, scene.mails["bob_open"])
    snap = scene.mona.get("/api/ops/email").get_json()["data"]
    after = snap["stats"]
    for key in ("business_emails", "replied", "pending", "no_action_needed", "needed_reply", "reply_rate", "sla_breached",
                "sla_compliance_rate", "escalated_count", "average_response"):
        assert after[key] == before[key], key
    assert rows(scene.mona)[scene.mails["alice_open"]]["status"] == "Pending Reply"      # still pending for the SLA
    assert after["assigned_count"] == before["assigned_count"]
    assert after["assigned_done_count"] == before["assigned_done_count"] + 2


def test_a_page_that_does_not_send_its_revision_still_learns_that_somebody_marked_done(clock, scene):
    snapshot_at = "2026-10-11T09:00:00"
    assert scene.mona.get(f"/api/ops/email?since={snapshot_at}").get_json()["unchanged"] is True
    clock.advance(minutes=5)
    done(scene.alice, scene.mails["alice_open"])
    assert "unchanged" not in scene.mona.get(f"/api/ops/email?since={snapshot_at}").get_json()
    rev = scene.mona.get("/api/ops/email").get_json()["meta"]["rev"]
    assert scene.mona.get(f"/api/ops/email?since={snapshot_at}&rev={rev}").get_json()["unchanged"] is True


def test_the_override_fingerprint_is_computed_once_per_version_of_the_files(scene, monkeypatch):
    """Every request needs it and the lists only grow: no re-hashing of all of them on every poll."""
    import json
    real, calls = json.dumps, []

    def counting(*a, **k):
        if k.get("sort_keys") and k.get("default") is str:          # the fingerprint's own call, not Flask's answers
            calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(json, "dumps", counting)
    first = scene.mona.get("/api/ops/email").get_json()["meta"]["rev"]
    for _ in range(5):
        assert scene.fadi.get("/api/ops/email").get_json()["meta"]["rev"] == first
        scene.alice.get("/api/ops/my")
    assert len(calls) == 1
    done(scene.alice, scene.mails["alice_open"])                               # a change: a new fingerprint, once
    second = scene.mona.get("/api/ops/email").get_json()["meta"]["rev"]
    assert second != first and scene.fadi.get("/api/ops/email").get_json()["meta"]["rev"] == second
    assert len(calls) == 2                                                    # once more for the new version, shared by every later request


def test_the_assigned_tile_counts_assigned_and_done(scene):
    stats = scene.mona.get("/api/ops/email").get_json()["data"]["stats"]
    # in the scan: alice_open, alice_done, bob_open, uncl_alice, prep_alice, big_alice are assigned; only alice_done is done
    assert stats["assigned_count"] == 6 and stats["assigned_done_count"] == 1
    scoped = scene.fadi.get("/api/ops/email").get_json()["data"]["stats"]          # Flex Cube only: no Unclassified mail
    assert scoped["assigned_count"] == 5 and scoped["assigned_done_count"] == 1
    shared = scene.alice.get("/api/ops/shared").get_json()["email"]
    assert shared["assigned_count"] == 5 and shared["assigned_done_count"] == 1                # an employee never counts Unclassified
    overview = scene.fadi.get("/api/ops/overview").get_json()["email"]
    assert overview["assigned_done_count"] == 1


def test_my_work_counts_open_and_done_and_orders_the_list(scene):
    ops = scene.ops
    mine = scene.alice.get("/api/ops/my").get_json()["email"]
    assert (mine["assigned"]["pending"], mine["assigned"]["done"], mine["assigned"]["other"]) == (4, 1, 0)
    items = mine["assigned"]["items"]
    assert [i["assignment_done"] for i in items] == [False, False, False, False, True]          # open first, then done
    assert items[-1]["subject"] == "Alice done mail" and items[-1]["done_by"] == "Alice Employee"
    assert items[-1]["done_note"] == "all fine" and items[-1]["done_at"] == "2026-10-11T08:50:00"
    assert {"assigned_by", "note", "waiting", "sla_status", "received_time", "mail_copy"} <= set(items[0])
    # newest assignment first inside the open group
    ops.set_override("assignments", scene.mails["alice_open"], assignment(ALICE, "Alice", at="2026-10-11T08:59:00"))
    stamps = [i["assigned_at"] for i in scene.alice.get("/api/ops/my").get_json()["email"]["assigned"]["items"][:4]]
    assert stamps == sorted(stamps, reverse=True) and stamps[0] == "2026-10-11T08:59:00"
    done(scene.alice, scene.mails["alice_open"])
    mine = scene.alice.get("/api/ops/my").get_json()["email"]
    assert (mine["assigned"]["pending"], mine["assigned"]["done"]) == (3, 2)


def test_a_reply_written_in_outlook_counts_as_done_by_itself(scene):
    ops = scene.ops
    first = replied_row("<by-first@x>", ALICE, 15, subject="Alice replied first")
    last = replied_row("<by-last@x>", "bob@example.test", 5, subject="Bob replied first, Alice last",
                       last_reply_email=ALICE, last_reply_time="2026-10-11T08:40:00")
    other = replied_row("<by-bob@x>", "bob@example.test", 5, subject="Bob replied, Alice was assigned")
    put_snapshot(ops, "email", email_data([first, last, other, email_row("<open@x>", subject="Still open")]))
    for mid in ("<by-first@x>", "<by-last@x>", "<by-bob@x>", "<open@x>"):
        ops.set_override("assignments", mid, assignment(ALICE, "Alice"))
    seen = rows(scene.fadi)
    assert seen["<by-first@x>"]["assignment_done"] and seen["<by-first@x>"]["done_auto"] and seen["<by-first@x>"]["done_by"] == ""
    assert seen["<by-first@x>"]["done_at"] == first["first_reply_time"]
    assert seen["<by-last@x>"]["done_auto"] and seen["<by-last@x>"]["done_at"] == "2026-10-11T08:40:00"
    assert not seen["<by-bob@x>"]["assignment_done"] and not seen["<open@x>"]["assignment_done"]
    mine = scene.alice.get("/api/ops/my").get_json()["email"]["assigned"]
    assert (mine["pending"], mine["done"], mine["other"]) == (1, 2, 1)       # open / answered by me / answered by someone else
    assert {i["message_id"] for i in mine["items"] if i["done_auto"]} == {"<by-first@x>", "<by-last@x>"}


def test_a_mail_the_collector_split_between_teams_is_one_assignment_in_my_work(scene):
    ops = scene.ops
    a = email_row("<split@x>", subject="Split mail", is_split_child=True, row_id="<split@x>|Flex Cube",
                  split_sibling_teams=["Card Management"])
    b = email_row("<split@x>", subject="Split mail", team="Card Management", all_teams=["Card Management"],
                  teams_pending=["Card Management"], is_split_child=True, row_id="<split@x>|Card Management",
                  split_sibling_teams=["Flex Cube"])
    put_snapshot(ops, "email", email_data([a, b]))
    ops.set_override("assignments", "<split@x>", assignment(ALICE, "Alice"))
    mine = scene.alice.get("/api/ops/my").get_json()["email"]["assigned"]
    assert len(mine["items"]) == 1 and mine["pending"] == 1


def test_done_flags_are_pure_overlay_functions():
    row = {"message_id": "<m>", "status": "Pending Reply", "first_reply_email": "", "team": "Flex Cube", "all_teams": ["Flex Cube"]}
    out = overlay.apply_overrides([row], {"assignments": {"<m>": {"assigned_to_email": "A@X.test", "done_at": "t", "done_by": "A",
                                                                  "done_note": "n"}}})[0]
    assert out["assignment_done"] and not out["done_auto"] and out["done_by"] == "A" and out["done_note"] == "n"
    assert "done_at" not in row and row.get("assignment_done") is None                       # the input is never changed
    plain = overlay.apply_overrides([row], {})[0]
    assert plain["assignment_done"] is False and plain["done_at"] == "" and plain["done_auto"] is False
    # a done mark on an entry without an assignee e-mail means nothing
    assert overlay.apply_overrides([row], {"assignments": {"<m>": {"done_at": "t"}}})[0]["assignment_done"] is False


# ----------------------------------------------------------------------------- two people at once
def test_two_people_pressing_done_at_the_same_moment_mark_it_once(app, clock, scene):
    mid = scene.mails["alice_open"]
    results, barrier = [], threading.Barrier(2)

    def press(who, name):
        barrier.wait()
        results.append((name, done(who, mid, f"by {name}")))
    threads = [threading.Thread(target=press, args=(scene.alice, "alice")), threading.Thread(target=press, args=(scene.fadi, "fadi"))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert [r.status_code for _, r in results] == [200, 200]
    assert sorted(r.get_json()["already"] for _, r in results) == [False, True]
    assert len([a for a in audit_actions(app, clock) if a[0] == "ops_done"]) == 1
    winner = next(name for name, r in results if not r.get_json()["already"])
    assert entry(scene, "alice_open")["done_note"] == f"by {winner}"
    # the assignment survived intact
    assert entry(scene, "alice_open")["assigned_to_email"] == ALICE


def test_done_while_the_mail_is_being_given_to_somebody_else_is_refused(scene, monkeypatch):
    """The assignee pressed Done, but the manager had just reassigned the mail: the mark must not land on Bob's assignment."""
    mid = scene.mails["alice_open"]
    import calllog.ops.api as api
    real = api.assignment_target

    def stale_target(message_id):
        found = real(message_id)
        scene.fadi.post("/api/ops/email/assign", {"message_id": message_id, "assigned_to_email": BOB})   # the world moves on
        return found
    monkeypatch.setattr(api, "assignment_target", stale_target)
    r = done(scene.alice, mid)
    assert r.status_code == 404 and r.get_json() == NOT_AVAILABLE
    assert entry(scene, "alice_open")["assigned_to_email"] == BOB and "done_at" not in entry(scene, "alice_open")
