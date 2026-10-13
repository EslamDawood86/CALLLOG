"""B4 - in-app notifications: the per-person store, the API, the hooks (assign / unassign / reassign / done / reopen /
replied), who is told and who is not, concurrent writers, and the cost of 60 people polling.

Fake data only; accounts and mails come from ``scene`` in tests/mailkit.py.
"""
import json
import multiprocessing
import statistics
import threading
import time
from datetime import datetime
from urllib.parse import quote

import pytest

from calllog.ops import notifications, schemas
from calllog.ops.store import OpsStore
from mailkit import ALICE, BOB, FADI, assignment, email_data, email_row, put_snapshot, replied_row, scene  # noqa: F401
from test_ops_api import make_account, ops  # noqa: F401

NOW = datetime(2026, 10, 11, 9, 0, 0)


def poll(who, since=None):
    return who.get("/api/ops/notifications" + (f"?since={quote(since)}" if since else "")).get_json()


def done(who, mid, note=None):
    return who.post("/api/ops/email/done", {"message_id": mid, **({"note": note} if note else {})})


def reopen(who, mid):
    return who.api("DELETE", f"/api/ops/email/done?message_id={quote(mid, safe='')}")


def titles(who):
    return [i["title"] for i in poll(who)["items"]]


# ----------------------------------------------------------------------------- the store
@pytest.fixture
def store(tmp_path):
    return OpsStore(tmp_path / "data").init()


def item(n, **kw):
    return notifications.make_item(kw.pop("kind", "assigned"), f"title {n}", f"text {n}", "/my", "Someone", f"<m{n}@x>", NOW)


def test_an_unknown_person_has_an_empty_unchanged_answer(store):
    first = notifications.poll(store, "1001")
    assert first == {"unread": 0, "items": [], "ts": "0", "seen_ts": ""}
    assert notifications.poll(store, "1001", "0") == {"unchanged": True, "unread": 0, "ts": "0"}
    assert list(store.notifications_dir.iterdir()) == []                 # asking creates nothing


def test_add_puts_the_newest_first_and_changes_the_token(store):
    first = notifications.add(store, "1001", [item(1)], NOW)
    second = notifications.add(store, "1001", [item(2)], NOW)                  # the clock did not move: the token still changes
    assert first["ts"] != second["ts"] and second["ts"] > first["ts"]
    out = notifications.poll(store, "1001")
    assert [i["title"] for i in out["items"]] == ["title 2", "title 1"] and out["unread"] == 2
    assert notifications.poll(store, "1001", out["ts"]) == {"unchanged": True, "unread": 2, "ts": out["ts"]}
    assert "items" in notifications.poll(store, "1001", first["ts"])           # a token that is no longer current


def test_a_person_keeps_at_most_100_and_a_poll_returns_30(store):
    for n in range(130):
        notifications.add(store, "1001", [item(n)], NOW)
    doc = notifications.read(store, "1001")
    assert len(doc["items"]) == schemas.NOTIFICATION_KEEP == 100
    assert doc["items"][0]["title"] == "title 129" and doc["items"][-1]["title"] == "title 30"
    out = notifications.poll(store, "1001")
    assert len(out["items"]) == schemas.NOTIFICATION_PAGE == 30 and out["unread"] == 100
    assert len({i["id"] for i in doc["items"]}) == 100


def test_title_and_text_are_cut_to_200_and_the_shape_is_fixed():
    made = notifications.make_item("done", "T" * 500, "x" * 500 + "\n\n y", "/email?tab=pending&open=1", "A" * 200, "<m>" * 400, NOW)
    assert len(made["title"]) == 200 and len(made["text"]) == 200 and len(made["actor"]) == 80 and len(made["ref"]["message_id"]) == 500
    assert set(made) == {"id", "ts", "type", "title", "text", "link", "actor", "ref", "read"} and made["read"] is False
    assert made["ts"] == "2026-10-11T09:00:00"


def test_mark_read_by_id_and_all(store):
    items = [item(n) for n in range(4)]
    for it in items:
        notifications.add(store, "1001", [it], NOW)
    before = notifications.poll(store, "1001")["ts"]
    out = notifications.mark_read(store, "1001", [items[0]["id"], "no-such-id"], NOW)
    assert out["unread"] == 3 and out["changed"] == 1 and out["ts"] != before
    again = notifications.mark_read(store, "1001", [items[0]["id"]], NOW)
    assert again["changed"] == 0 and again["ts"] == out["ts"]                     # nothing to do: nothing written
    everything = notifications.mark_read(store, "1001", "all", NOW)
    assert everything["unread"] == 0 and everything["changed"] == 3
    assert notifications.poll(store, "1001")["seen_ts"] == "2026-10-11T09:00:00"
    assert all(i["read"] for i in notifications.read(store, "1001")["items"])


def test_marking_read_for_somebody_with_no_file_creates_no_file(store):
    assert notifications.mark_read(store, "1009", "all", NOW)["changed"] == 0
    assert not any(store.notifications_dir.iterdir())


@pytest.mark.parametrize("uid", ["../../users", "..\\x", "a/b", "", "x" * 300, "ÄÖ", "con:", "a b", "con", "NUL", "com1.x", "lpt9", "1001."])
def test_an_odd_user_id_can_only_ever_reach_its_own_file(store, uid):
    notifications.add(store, uid, [item(1)], NOW)
    files = [p for p in store.root.rglob("*") if p.is_file() and p.suffix == ".json"]
    assert all(p.parent == store.notifications_dir for p in files) and len(files) == 1
    assert notifications.read(store, uid)["items"][0]["title"] == "title 1"


def test_normal_ids_keep_their_own_readable_file_names(store):
    for uid in ("1001", "mgr.flex", "Alice-2", "a_b"):
        assert notifications.path_for(store, uid).name == f"{uid.lower()}.json"
    assert notifications.path_for(store, "CON").name.startswith("u-") and notifications.path_for(store, "1001.").name.startswith("u-")
    assert notifications.path_for(store, "x" * 300) != notifications.path_for(store, "x" * 301)


def test_a_damaged_file_reads_as_empty_not_as_an_error(store):
    notifications.path_for(store, "1001").write_text("{ nope", encoding="utf-8")
    assert notifications.read(store, "1001")["items"] == []
    notifications.add(store, "1001", [item(1)], NOW)                               # and the next write repairs it
    assert notifications.poll(store, "1001")["unread"] == 1


def test_prune_drops_only_old_items(store):
    old = notifications.make_item("done", "old", "t", "/x", "a", "<o>", datetime(2026, 8, 1, 9, 0, 0))
    new = notifications.make_item("done", "new", "t", "/x", "a", "<n>", NOW)
    notifications.add(store, "1001", [new, old], NOW)
    assert notifications.prune(store, 30, NOW) == 1
    assert [i["title"] for i in notifications.read(store, "1001")["items"]] == ["new"]
    assert notifications.prune(store, 30, NOW) == 0


def test_reads_are_cached_and_not_followed_by_the_refresher(store):
    notifications.add(store, "1001", [item(1)], NOW)
    path = str(notifications.path_for(store, "1001"))
    notifications.read(store, "1001")
    assert path in store._cache and path not in store._used           # 60 files must not be re-checked every second
    store.TTL = 3600.0
    first = notifications.read(store, "1001")
    assert notifications.read(store, "1001") is first                  # the very same parsed object: no new parse


# ----------------------------------------------------------------------------- the API
def test_the_bell_is_for_every_signed_in_role_and_only_for_yourself(app, scene):
    for who in (scene.alice, scene.fadi, scene.admin):
        r = who.get("/api/ops/notifications")
        assert r.status_code == 200 and set(r.get_json()) == {"unread", "items", "ts", "seen_ts"}
    assert app.test_client().get("/api/ops/notifications").status_code == 401
    scene.fadi.post("/api/ops/email/assign", {"message_id": scene.mails["free_flex"], "assigned_to_email": ALICE})
    assert len(poll(scene.alice)["items"]) == 1
    # there is no way to name another person: neither a parameter, nor the view-as switch, nor a header
    for extra in ("&as=1001", "&user=1001", "&user_id=1001", "&uid=1001"):
        assert scene.bob.get("/api/ops/notifications?x=1" + extra).get_json()["items"] == []
    assert scene.bob.post("/api/ops/notifications/read", {"ids": "all", "user_id": "1001"}).status_code == 200
    assert poll(scene.alice)["unread"] == 1


def test_since_gives_a_tiny_unchanged_answer(scene):
    scene.fadi.post("/api/ops/email/assign", {"message_id": scene.mails["free_flex"], "assigned_to_email": ALICE})
    first = poll(scene.alice)
    again = poll(scene.alice, first["ts"])
    assert again == {"unchanged": True, "unread": 1, "ts": first["ts"]}
    scene.mona.post("/api/ops/email/assign", {"message_id": scene.mails["free_card"], "assigned_to_email": ALICE})
    fresh = poll(scene.alice, first["ts"])
    assert "unchanged" not in fresh and fresh["unread"] == 2 and fresh["ts"] != first["ts"]
    assert len(json.dumps(again)) < 80


def test_reading_marks_and_changes_the_token(app, clock, scene):
    from test_ops_api import audit_actions
    scene.fadi.post("/api/ops/email/assign", {"message_id": scene.mails["free_flex"], "assigned_to_email": ALICE})
    scene.mona.post("/api/ops/email/assign", {"message_id": scene.mails["free_card"], "assigned_to_email": ALICE})
    before = poll(scene.alice)
    ids = [i["id"] for i in before["items"]]
    r = scene.alice.post("/api/ops/notifications/read", {"ids": [ids[0]]}).get_json()
    assert r["ok"] is True and r["unread"] == 1 and r["ts"] != before["ts"]
    after = poll(scene.alice, before["ts"])
    assert "unchanged" not in after and [i["read"] for i in after["items"]] == [True, False]
    assert scene.alice.post("/api/ops/notifications/read", {"ids": "all"}).get_json()["unread"] == 0
    assert [a[0] for a in audit_actions(app, clock)].count("ops_notifications_read") == 2
    # nothing left to mark: no write, no audit line
    scene.alice.post("/api/ops/notifications/read", {"ids": "all"})
    assert [a[0] for a in audit_actions(app, clock)].count("ops_notifications_read") == 2


@pytest.mark.parametrize("ids", [None, "everything", 5, [1, 2], [""], ["x" * 65], ["a"] * 201, {"a": 1}])
def test_read_validates_its_ids(scene, ids):
    r = scene.alice.post("/api/ops/notifications/read", {"ids": ids})
    assert r.status_code == 400 and "ids" in r.get_json()["error"]


# ----------------------------------------------------------------------------- the hooks: assign / unassign / reassign
def test_assigning_tells_the_assignee_and_nobody_else(scene):
    mid = scene.mails["free_flex"]
    scene.fadi.post("/api/ops/email/assign", {"message_id": mid, "assigned_to_email": ALICE.upper(), "note": "urgent"})
    got = poll(scene.alice)["items"]
    assert len(got) == 1
    n = got[0]
    assert n["type"] == "assigned" and n["title"] == "New e-mail assigned to you" and n["read"] is False
    assert n["text"] == "Free flex mail - by Fadi Flex" and n["actor"] == "Fadi Flex" and n["ref"] == {"message_id": mid}
    assert n["link"] == "/my?mail=" + quote(mid, safe="")
    for other in (scene.bob, scene.carol, scene.fadi, scene.cara, scene.mona, scene.admin):
        assert poll(other)["items"] == []                       # not the actor, not the other people


def test_a_new_note_for_the_same_person_is_not_a_new_notification(scene):
    mid = scene.mails["free_flex"]
    for note in ("first", "second"):
        scene.fadi.post("/api/ops/email/assign", {"message_id": mid, "assigned_to_email": ALICE, "note": note})
    assert len(poll(scene.alice)["items"]) == 1


def test_assigning_to_somebody_without_an_account_notifies_nobody_and_still_works(scene):
    r = scene.fadi.post("/api/ops/email/assign", {"message_id": scene.mails["free_flex"], "assigned_to_email": "ghost@example.test"})
    assert r.status_code == 200
    assert all(poll(w)["items"] == [] for w in (scene.alice, scene.bob, scene.carol, scene.mona))


def test_assigning_to_yourself_tells_you_nothing(scene):
    scene.fadi.post("/api/ops/email/assign", {"message_id": scene.mails["free_flex"], "assigned_to_email": FADI})
    assert poll(scene.fadi)["items"] == []


def test_reassigning_tells_the_old_and_the_new_assignee(scene):
    mid = scene.mails["alice_open"]
    scene.fadi.post("/api/ops/email/assign", {"message_id": mid, "assigned_to_email": BOB, "assigned_to_name": "Bob"})
    old, new = poll(scene.alice)["items"], poll(scene.bob)["items"]
    assert [(i["type"], i["title"]) for i in old] == [("unassigned", "Assignment removed")]
    assert old[0]["text"] == "Alice open mail - by Fadi Flex" and old[0]["link"] == "/my?mail=" + quote(mid, safe="")
    assert [(i["type"], i["title"]) for i in new] == [("assigned", "New e-mail assigned to you")]


def test_unassigning_tells_the_previous_assignee(scene):
    mid = scene.mails["alice_open"]
    assert scene.fadi.api("DELETE", f"/api/ops/email/assign?message_id={quote(mid, safe='')}").status_code == 200
    got = poll(scene.alice)["items"]
    assert [(i["type"], i["title"]) for i in got] == [("unassigned", "Assignment removed")]
    assert poll(scene.bob)["items"] == [] and poll(scene.mona)["items"] == []


def test_an_actor_without_a_mailbox_on_the_account_is_still_a_valid_actor(scene):
    scene.admin.post("/api/ops/email/assign", {"message_id": scene.mails["free_flex"], "assigned_to_email": BOB})
    assert titles(scene.bob) == ["New e-mail assigned to you"] and poll(scene.bob)["items"][0]["actor"] == "Administrator"


# ----------------------------------------------------------------------------- the hooks: done / reopen
def test_done_by_the_assignee_tells_the_assigner_and_the_managers_of_the_team(scene):
    mid = scene.mails["alice_open"]                              # assigned by mona (every team); fadi manages Flex Cube
    done(scene.alice, mid, "Called the branch")
    for boss in (scene.mona, scene.fadi):
        got = poll(boss)["items"]
        assert len(got) == 1, boss
        n = got[0]
        assert n["type"] == "done" and n["title"] == "Alice Employee marked an e-mail as done"
        assert n["text"] == "Alice open mail - Called the branch" and n["link"] == "/email?tab=pending&open=" + quote(mid, safe="")
        assert n["actor"] == "Alice Employee"
    assert poll(scene.cara)["items"] == []                       # another team's manager
    assert poll(scene.alice)["items"] == [] and poll(scene.bob)["items"] == []          # not the actor, not other employees
    assert poll(scene.admin)["items"] == []                      # an administrator only when they were the assigner


def test_an_administrator_who_assigned_it_is_told_when_it_is_done(scene):
    scene.ops.set_override("assignments", scene.mails["alice_open"], assignment(ALICE, "Alice", by="admin", by_id="admin"))
    done(scene.alice, scene.mails["alice_open"])
    assert titles(scene.admin) == ["Alice Employee marked an e-mail as done"]
    assert len(poll(scene.mona)["items"]) == 1 and len(poll(scene.fadi)["items"]) == 1       # every-team and Flex Cube managers too


def test_nobody_is_told_twice(scene):
    done(scene.alice, scene.mails["alice_open"])             # mona is both the assigner and a manager of every team
    assert len(poll(scene.mona)["items"]) == 1
    done(scene.alice, scene.mails["alice_open"])             # pressing again: no new notification
    assert len(poll(scene.mona)["items"]) == 1 and len(poll(scene.fadi)["items"]) == 1


def test_the_note_of_a_done_mail_never_pushes_the_text_past_200_characters(scene):
    done(scene.alice, scene.mails["alice_open"], "n" * 300)
    text = poll(scene.fadi)["items"][0]["text"]
    assert len(text) == 200 and text.startswith("Alice open mail - nnn")


def test_done_by_a_manager_for_the_assignee_tells_the_assignee(scene):
    done(scene.fadi, scene.mails["alice_open"])
    got = poll(scene.alice)["items"]
    assert [(i["type"], i["title"]) for i in got] == [("done", "Fadi Flex marked an e-mail as done")]
    assert got[0]["link"] == "/my?mail=" + quote(scene.mails["alice_open"], safe="")
    assert poll(scene.mona)["items"] == [] and poll(scene.fadi)["items"] == []


def test_reopening_tells_the_other_side(scene):
    mid = scene.mails["alice_done"]
    reopen(scene.alice, mid)                                      # the assignee reopens: assigner + team managers
    assert titles(scene.mona) == ["E-mail reopened"] and titles(scene.fadi) == ["E-mail reopened"]
    assert poll(scene.mona)["items"][0]["text"] == "Alice done mail - by Alice Employee"
    assert poll(scene.cara)["items"] == [] and poll(scene.alice)["items"] == []
    done(scene.alice, mid)
    reopen(scene.fadi, mid)                                       # a manager reopens: the assignee is told
    assert titles(scene.alice) == ["E-mail reopened"]
    assert poll(scene.alice)["items"][0]["text"] == "Alice done mail - by Fadi Flex"


def test_a_deactivated_manager_is_not_told(app, scene):
    app.extensions["store"].update_user("fadi", {"active": False})
    done(scene.alice, scene.mails["alice_open"])
    assert poll(scene.mona)["items"] != []
    assert notifications.read(scene.ops, "fadi")["items"] == []


def test_no_notification_ever_holds_a_mail_body(scene):
    body_marker = "SECRETBODYTEXT"
    from mailkit import put_copy
    put_copy(scene.ops, scene.mails["alice_open"], subject="Alice open mail", body=body_marker * 20)
    done(scene.alice, scene.mails["alice_open"], "ok")
    scene.fadi.post("/api/ops/email/assign", {"message_id": scene.mails["free_flex"], "assigned_to_email": ALICE})
    for doc in scene.ops.notifications_dir.glob("*.json"):
        assert body_marker not in doc.read_text(encoding="utf-8")


def test_a_failing_notification_never_undoes_the_action(scene, monkeypatch):
    monkeypatch.setattr(notifications, "add", lambda *a, **k: (_ for _ in ()).throw(OSError("share unavailable")))
    r = done(scene.alice, scene.mails["alice_open"], "still done")
    assert r.status_code == 200 and scene.ops.overrides("assignments")[scene.mails["alice_open"]]["done_note"] == "still done"


# ----------------------------------------------------------------------------- the hook: replied (seen by the scan)
def test_a_reply_written_in_outlook_tells_the_assigner_and_team_managers_once(scene):
    ops = scene.ops
    mid = "<answered-in-outlook@x>"
    ops.set_override("assignments", mid, assignment(ALICE, "Alice"))
    put_snapshot(ops, "email", email_data([replied_row(mid, ALICE, 12, subject="Answered in Outlook")]))
    got = poll(scene.mona)["items"]                               # anybody's poll notices the new snapshot
    assert [(i["type"], i["title"]) for i in got] == [("replied", "Alice Employee replied to an assigned e-mail")]
    assert got[0]["text"] == "Answered in Outlook" and got[0]["link"] == "/email?tab=pending&open=" + quote(mid, safe="")
    assert len(poll(scene.fadi)["items"]) == 1 and poll(scene.cara)["items"] == []
    assert poll(scene.alice)["items"] == [] and poll(scene.admin)["items"] == []
    for _ in range(3):                                            # more polls, a newer scan of the same data: still once
        poll(scene.mona), poll(scene.bob)
    put_snapshot(ops, "email", email_data([replied_row(mid, ALICE, 12, subject="Answered in Outlook")]), at="2026-10-11T09:10:00")
    poll(scene.alice)
    assert len(poll(scene.mona)["items"]) == 1 and len(poll(scene.fadi)["items"]) == 1
    assert ops.overrides("assignments")[mid]["replied_notified"]


def test_a_reply_by_somebody_else_or_a_pending_mail_tells_nobody(scene):
    ops = scene.ops
    ops.set_override("assignments", "<by-bob@x>", assignment(ALICE, "Alice"))
    ops.set_override("assignments", "<waiting@x>", assignment(ALICE, "Alice"))
    put_snapshot(ops, "email", email_data([replied_row("<by-bob@x>", BOB, 5), email_row("<waiting@x>")]))
    poll(scene.mona)
    assert poll(scene.mona)["items"] == [] and poll(scene.fadi)["items"] == []
    assert "replied_notified" not in ops.overrides("assignments")["<by-bob@x>"]


def test_two_polls_at_the_same_moment_announce_a_reply_only_once(scene):
    ops = scene.ops
    mid = "<race@x>"
    ops.set_override("assignments", mid, assignment(ALICE, "Alice"))
    put_snapshot(ops, "email", email_data([replied_row(mid, ALICE, 12, subject="Race")]))
    barrier = threading.Barrier(4)
    clients = [scene.bob, scene.carol, scene.cara, scene.alice]

    def go(who):
        barrier.wait()
        poll(who)
    threads = [threading.Thread(target=go, args=(c,)) for c in clients]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(poll(scene.mona)["items"]) == 1 and len(poll(scene.fadi)["items"]) == 1


def test_a_reassigned_mail_can_announce_a_reply_again(scene):
    ops = scene.ops
    mid = "<again@x>"
    ops.set_override("assignments", mid, assignment(ALICE, "Alice", replied_notified="2026-10-11T08:00:00"))
    put_snapshot(ops, "email", email_data([replied_row(mid, BOB, 12, subject="Bob answers")]))
    scene.admin.post("/api/ops/email/assign", {"message_id": mid, "assigned_to_email": BOB})
    assert "replied_notified" not in ops.overrides("assignments")[mid]
    poll(scene.mona)
    assert [i["type"] for i in poll(scene.mona)["items"]] == ["replied"]


# ----------------------------------------------------------------------------- nobody loses an entry
def test_many_threads_adding_to_one_person_lose_nothing(store):
    def add_many(worker):
        for i in range(10):
            notifications.add(store, "1001", [notifications.make_item("done", f"w{worker}-{i}", "t", "/x", "a", f"<{worker}-{i}>", NOW)], NOW)
    threads = [threading.Thread(target=add_many, args=(w,)) for w in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    got = notifications.read(store, "1001")["items"]
    assert len(got) == 80 and len({i["title"] for i in got}) == 80
    assert len({i["id"] for i in got}) == 80


def test_two_managers_marking_mails_done_at_once_lose_no_notification(scene):
    ops = scene.ops
    mails = [f"<bulk-{n}@x>" for n in range(12)]
    put_snapshot(ops, "email", email_data([email_row(m, subject=f"Bulk {m}") for m in mails]))
    for m in mails:
        ops.set_override("assignments", m, assignment(ALICE, "Alice"))
    barrier = threading.Barrier(2)

    def work(who, chunk):
        barrier.wait()
        for m in chunk:
            assert done(who, m).status_code == 200
    threads = [threading.Thread(target=work, args=(scene.fadi, mails[:6])), threading.Thread(target=work, args=(scene.mona, mails[6:]))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    got = poll(scene.alice)["items"]
    assert len(got) == 12 and {i["ref"]["message_id"] for i in got} == set(mails)         # the assignee was told about every one
    assert all(ops.overrides("assignments")[m].get("done_at") for m in mails)


def test_the_web_app_and_the_collector_writing_at_the_same_time_lose_nothing(scene):
    """The collector is another process on another PC: its own OpsStore on the same folder, and real processes below."""
    uid = "1001"
    other = OpsStore(scene.ops.root.parent)               # a second store object = a second process's view of the folder
    stop = threading.Event()
    added = []

    def collector():
        n = 0
        while not stop.is_set() and n < 25:
            notifications.add(other, uid, [notifications.make_item("replied", f"from collector {n}", "t", "/x", "c", f"<c{n}>", NOW)], NOW)
            added.append(n)
            n += 1
    t = threading.Thread(target=collector)
    t.start()
    for n in range(15):
        scene.fadi.post("/api/ops/email/assign", {"message_id": scene.mails["free_flex"], "assigned_to_email": ALICE if n % 2 == 0 else BOB})
        scene.alice.post("/api/ops/notifications/read", {"ids": "all"})
    t.join()
    stop.set()
    scene.ops._cache.clear()
    items = notifications.read(scene.ops, uid)["items"]
    assert len([i for i in items if i["title"].startswith("from collector")]) == 25


def test_separate_processes_writing_one_file_lose_nothing(tmp_path):
    root = tmp_path / "data"
    OpsStore(root).init()
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=__import__("mp_notify_worker").hammer, args=(str(root), n, 12, "1001")) for n in range(4)]
    [p.start() for p in procs]
    [p.join(90) for p in procs]
    assert [p.exitcode for p in procs] == [0, 0, 0, 0]
    items = notifications.read(OpsStore(root), "1001")["items"]
    assert len(items) == 48 and len({i["ref"]["message_id"] for i in items}) == 48
    text = notifications.path_for(OpsStore(root), "1001").read_text(encoding="utf-8")
    assert json.loads(text)["items"].__len__() == 48                                  # not a torn file


# ----------------------------------------------------------------------------- the cost of 60 people polling
def make_sessions(app, count):
    store = app.extensions["store"]
    users = {}
    for n in range(count):
        uid = f"u{n:03d}"
        users[uid] = {"user_id": uid, "name": f"User {n}", "department": "IT", "role": "user", "active": True,
                      "must_change_password": False, "email": f"user{n}@example.test", "team": "", "teams": [],
                      "security_name": "", "password_hash": "x", "created_at": "2026-10-01 09:00:00"}
    store.create_users(list(users.values()))
    clients = {}
    for uid in users:
        c = app.test_client()
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = uid, "tok"
        clients[uid] = c
    return clients


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def test_sixty_people_polling_stay_far_below_the_budget(app, scene, monkeypatch, capsys):
    clients = make_sessions(app, 60)
    ops = scene.ops
    for n, uid in enumerate(clients):                              # everybody has a handful of notifications
        notifications.add(ops, uid, [notifications.make_item("assigned", f"t{k}", "s", "/my", "m", f"<m{k}>", NOW) for k in range(8)], NOW)
    first, unchanged, changed = [], [], []
    tokens = {}
    for uid, c in clients.items():                                  # the first poll of a page: the full answer
        t0 = time.perf_counter()
        r = c.get("/api/ops/notifications")
        first.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200 and len(r.get_json()["items"]) == 8
        tokens[uid] = r.get_json()["ts"]
    # worst case: the share is slow (5 ms per file operation) and every poll has to look at its file again, because the
    # last look was more than a second ago (a page polls every 30 s)
    real_stat, real_read = type(ops.root).stat, ops.read_json

    def slow_stat(self, *a, **k):
        if "notifications" in str(self):
            time.sleep(0.005)
        return real_stat(self, *a, **k)

    def read_json(path, default=None, watch=True):
        if "notifications" in str(path):
            monkeypatch.setattr(ops, "TTL", 0.0)
        try:
            return real_read(path, default, watch)
        finally:
            monkeypatch.setattr(ops, "TTL", 1.0)
    monkeypatch.setattr(type(ops.root), "stat", slow_stat)
    monkeypatch.setattr(ops, "read_json", read_json)
    for _ in range(5):
        for uid, c in clients.items():
            t0 = time.perf_counter()
            r = c.get("/api/ops/notifications?since=" + quote(tokens[uid]))
            unchanged.append((time.perf_counter() - t0) * 1000)
            assert r.get_json()["unchanged"] is True
    for uid, c in list(clients.items())[:30]:                       # half of them get something new
        notifications.add(ops, uid, [notifications.make_item("done", "new", "s", "/my", "m", "<new>", NOW)], NOW)
    for uid, c in clients.items():
        t0 = time.perf_counter()
        r = c.get("/api/ops/notifications?since=" + quote(tokens[uid]))
        changed.append((time.perf_counter() - t0) * 1000)
    p95 = {"first poll (full answer)": percentile(first, 0.95), "unchanged, slow share": percentile(unchanged, 0.95),
           "changed (full answer)": percentile(changed, 0.95)}
    with capsys.disabled():
        print("\n  notification poll, 60 users, Flask test client: " + "; ".join(
            f"{k} p95 {v:.1f} ms" for k, v in p95.items()) + f"; unchanged median {statistics.median(unchanged):.1f} ms")
    # HUB-DESIGN section 14: `?since=` unchanged < 5 ms is the target for a cached snapshot; here every poll also pays
    # 5 ms for the share, so < 15 ms is the bar - and the full answers stay far below the 100 ms of the /my target
    assert p95["unchanged, slow share"] < 15 and p95["first poll (full answer)"] < 20 and p95["changed (full answer)"] < 20
