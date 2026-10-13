"""B2 - who may open / download a saved mail: the role matrix, identical answers for "not found" and "not yours",
path-traversal and oversized ids, the download headers, the states, "Prepare now" and the audit trail.

Fake data only (example.test). The accounts and mails are built by ``scene`` in tests/mailkit.py.
"""
import json
import re
from urllib.parse import quote

import pytest

from calllog.ops import mailbox
from mailkit import ALICE, MSG_MARK, put_copy, scene  # noqa: F401
from test_ops_api import audit_actions, make_account, ops  # noqa: F401

NOT_AVAILABLE = {"error": "That e-mail is not available."}

# (actor, mail) -> may open?   actors: alice (assignee), bob / carol (other employees), fadi (Flex Cube manager),
# cara (Card Management manager), mona (every team), admin
MATRIX = {
    "alice": {"alice_open": True, "alice_done": True, "bob_open": False, "free_flex": False, "free_card": False,
              "free_uncl": False, "uncl_alice": True, "replied_free": False, "prep_alice": True, "big_alice": True,
              "gone_alice": True},
    "bob": {"alice_open": False, "alice_done": False, "bob_open": True, "free_flex": False, "free_card": False,
            "free_uncl": False, "uncl_alice": False, "replied_free": False, "prep_alice": False, "big_alice": False,
            "gone_alice": False},
    "carol": {name: False for name in ("alice_open", "alice_done", "bob_open", "free_flex", "free_card", "free_uncl",
                                       "uncl_alice", "replied_free", "prep_alice", "big_alice", "gone_alice")},
    "fadi": {"alice_open": True, "alice_done": True, "bob_open": True, "free_flex": True, "free_card": False,
             "free_uncl": False, "uncl_alice": False, "replied_free": True, "prep_alice": True, "big_alice": True,
             "gone_alice": True},
    "cara": {"alice_open": False, "alice_done": False, "bob_open": False, "free_flex": False, "free_card": True,
             "free_uncl": False, "uncl_alice": False, "replied_free": False, "prep_alice": False, "big_alice": False,
             "gone_alice": False},
    "mona": {name: True for name in ("alice_open", "alice_done", "bob_open", "free_flex", "free_card", "free_uncl",
                                     "uncl_alice", "replied_free", "prep_alice", "big_alice", "gone_alice")},
    "admin": {name: True for name in ("alice_open", "alice_done", "bob_open", "free_flex", "free_card", "free_uncl",
                                      "uncl_alice", "replied_free", "prep_alice", "big_alice", "gone_alice")},
}
CASES = [(actor, mail, allowed) for actor, mails in MATRIX.items() for mail, allowed in mails.items()]


def message(who, mid, **kw):
    return who.get(f"/api/ops/email/message?message_id={quote(mid, safe='')}", **kw)


def download(who, mid, **kw):
    return who.get(f"/api/ops/email/message/download?message_id={quote(mid, safe='')}", **kw)


@pytest.mark.parametrize("actor,mail,allowed", CASES)
def test_the_role_matrix_for_the_preview(scene, actor, mail, allowed):
    mid = scene.mails[mail]
    r = message(getattr(scene, actor), mid)
    if allowed:
        assert r.status_code == 200, (actor, mail, r.get_json())
        assert r.get_json()["message_id"] == mid
    else:
        assert r.status_code == 404 and r.get_json() == NOT_AVAILABLE, (actor, mail)


@pytest.mark.parametrize("actor,mail,allowed", CASES)
def test_the_role_matrix_for_the_download(scene, actor, mail, allowed):
    """Allowed + a finished copy -> the file; allowed without a finished copy -> 404 about the copy; not allowed -> the
    one 'not available' answer. The same people, the same mails as for the preview."""
    mid = scene.mails[mail]
    r = download(getattr(scene, actor), mid)
    has_file = mail not in ("prep_alice", "big_alice", "replied_free")
    if allowed and has_file:
        assert r.status_code == 200 and MSG_MARK in r.data, (actor, mail)
    elif allowed:
        assert r.status_code == 404 and r.get_json() != NOT_AVAILABLE, (actor, mail)
    else:
        assert r.status_code == 404 and r.get_json() == NOT_AVAILABLE, (actor, mail)


def test_an_unknown_id_and_somebody_elses_id_get_exactly_the_same_answer(scene):
    unknown = "<nobody-ever-sent-this@x>"
    for actor in (scene.bob, scene.carol, scene.cara):
        for route in (message, download):
            ghost = route(actor, unknown)
            theirs = route(actor, scene.mails["alice_open"])
            assert (ghost.status_code, ghost.get_json()) == (theirs.status_code, theirs.get_json()) == (404, NOT_AVAILABLE)
            assert ghost.mimetype == theirs.mimetype == "application/json"
    # an administrator asking for an id that exists nowhere is told the same thing
    assert message(scene.admin, unknown).get_json() == NOT_AVAILABLE


def test_a_denied_probe_leaves_a_trace_in_the_audit_log(app, clock, scene):
    message(scene.bob, scene.mails["alice_open"])
    download(scene.bob, scene.mails["alice_open"])
    lines = [a for a in audit_actions(app, clock) if a[0] == "ops_mail_denied"]
    assert len(lines) == 2 and all(line[2] == "1002" for line in lines)


def test_anonymous_gets_401_everywhere(app, scene):
    c = app.test_client()
    mid = quote(scene.mails["free_flex"], safe="")
    assert c.get(f"/api/ops/email/message?message_id={mid}").status_code == 401
    assert c.get(f"/api/ops/email/message/download?message_id={mid}").status_code == 401
    assert c.get("/api/ops/notifications").status_code == 401
    with c.session_transaction() as s:
        s["csrf"] = "tok"
    for path, method in (("/api/ops/email/message/prepare", "POST"), ("/api/ops/email/done", "POST"),
                         ("/api/ops/email/done", "DELETE"), ("/api/ops/notifications/read", "POST")):
        r = c.open(path, method=method, json={"message_id": scene.mails["free_flex"], "ids": "all"},
                   headers={"X-CSRF-Token": "tok"})
        assert r.status_code == 401, path


@pytest.mark.parametrize("method,path", [("POST", "/api/ops/email/message/prepare"), ("POST", "/api/ops/email/done"),
                                         ("DELETE", "/api/ops/email/done"), ("POST", "/api/ops/notifications/read")])
def test_every_new_write_needs_the_csrf_header(scene, method, path):
    r = scene.alice.client.open(path, method=method, json={"message_id": scene.mails["alice_open"], "ids": "all"})
    assert r.status_code == 400 and "session expired" in r.get_json()["error"].lower()


# ----------------------------------------------------------------------------- the id as an attack surface
HOSTILE_IDS = [
    "../../users", "..\\..\\users.json", "../../../../etc/passwd", "....//....//x", "%2e%2e%2f%2e%2e%2fusers",
    "<a@x>/../../b", "C:\\Windows\\win.ini", "\\\\10.0.0.1\\share\\x", "/etc/passwd", "file:///etc/passwd",
    "a" * 32, "0" * 32, mailbox.mail_key("<alice-open@x>"), mailbox.mail_key("<alice-open@x>") + ".msg",
    "<a@x>\u202e", "الرسالة/../x", "' OR 1=1 --", "<script>alert(1)</script>", "~", "con", "nul", ".", "..", "x" * 499,
]


@pytest.mark.parametrize("who", ["alice", "bob", "fadi", "cara", "mona", "admin"])
def test_hostile_ids_never_reach_a_file_and_never_crash(scene, who):
    actor = getattr(scene, who)
    for hostile in HOSTILE_IDS:
        for route in (message, download):
            r = route(actor, hostile)
            assert r.status_code in (400, 404), (who, hostile, r.status_code)
            assert r.mimetype == "application/json" and b"root:" not in r.data and MSG_MARK not in r.data


def test_the_hash_of_a_copy_is_not_a_way_in(scene):
    """Only the message id opens a mail. Its hash (the file name on disk) is just another unknown id."""
    key = mailbox.mail_key(scene.mails["alice_open"])
    for who in (scene.admin, scene.mona, scene.alice):
        assert message(who, key).status_code == 404
        assert download(who, key).status_code == 404


def test_oversized_or_empty_ids_are_refused_without_touching_the_disk(scene, monkeypatch):
    opened = []
    real = mailbox.read_preview
    monkeypatch.setattr(mailbox, "read_preview", lambda *a, **k: opened.append(a) or real(*a, **k))
    for bad in ("", "   ", "x" * 501, "x" * 100_000, "a\nb", "a\x00b"):
        for route in (message, download):
            r = route(scene.admin, bad)
            assert r.status_code == 400 and "message_id" in r.get_json()["error"]
    assert opened == []
    assert scene.admin.get("/api/ops/email/message").status_code == 400            # no id at all


def test_no_path_is_ever_built_from_the_message_id(scene, monkeypatch):
    """The only paths handed to the file layer are <mail_dir>/<32 hex>.json|.msg, whatever the id looks like."""
    seen = []
    real_open = open

    def watch(file, *a, **k):
        seen.append(str(file))
        return real_open(file, *a, **k)
    monkeypatch.setattr("builtins.open", watch)
    for mid in ("../../users", scene.mails["alice_open"], "<x>/../../y"):
        message(scene.admin, mid)
        download(scene.admin, mid)
    mail_dir = str(scene.ops.mail_dir)
    ours = [p for p in seen if p.startswith(mail_dir)]
    assert ours and all(re.fullmatch(re.escape(mail_dir) + r"/[0-9a-f]{32}\.(json|msg)", p) for p in ours), ours


# ----------------------------------------------------------------------------- what the preview carries
def test_the_preview_of_a_ready_mail(scene):
    body = message(scene.alice, scene.mails["alice_open"]).get_json()
    assert body["state"] == "ready" and body["has_msg"] is True and body["source"] == "copy"
    assert body["subject"] == "Alice open mail" and body["from_name"] == "Customer One"
    assert body["from_email"] == "cust@example.test" and body["to"] == ["Support Desk"] and body["cc"] == ["Team Box"]
    assert body["body_text"] == "Body of alice_open" and body["truncated"] is False
    assert body["attachments"] == [{"name": "report.pdf", "size": 12345}]
    assert body["received"] == "2026-10-11T08:00:00" and body["msg_bytes"] == 600
    assert body["mail"]["status"] == "Pending Reply" and body["mail"]["team"] == "Flex Cube"
    assert body["assignment"]["assigned_to_email"] == ALICE and body["assignment"]["assigned_by"] == "Mona Manager"
    assert body["assignment"]["done"] is False
    assert body["can"] == {"download": True, "prepare": False, "done": True, "reopen": False}
    # no internal ids leak into the assignment block
    assert "marked_by_id" not in json.dumps(body)


def test_a_done_assignment_shows_who_when_and_the_note(scene):
    body = message(scene.fadi, scene.mails["alice_done"]).get_json()
    assert body["assignment"]["done"] is True and body["assignment"]["done_by"] == "Alice Employee"
    assert body["assignment"]["done_at"] == "2026-10-11T08:50:00" and body["assignment"]["done_note"] == "all fine"
    assert body["can"]["done"] is False and body["can"]["reopen"] is True


def test_states_ready_preparing_too_large_and_unavailable(scene):
    states = {name: message(scene.admin, scene.mails[name]).get_json()["state"]
              for name in ("alice_open", "prep_alice", "big_alice", "replied_free", "free_flex")}
    assert states == {"alice_open": "ready", "prep_alice": "preparing", "big_alice": "too_large",
                      "replied_free": "unavailable", "free_flex": "ready"}
    prep = message(scene.alice, scene.mails["prep_alice"]).get_json()
    assert prep["has_msg"] is False and prep["source"] == "snapshot" and prep["subject"] == "Alice mail without copy"
    assert prep["can"]["prepare"] is True and prep["can"]["download"] is False
    big = message(scene.alice, scene.mails["big_alice"]).get_json()
    assert big["has_msg"] is False and big["msg_bytes"] == 0 and big["can"]["download"] is False
    assert "too large" in download(scene.alice, scene.mails["big_alice"]).get_json()["error"]


def test_a_pending_mail_nobody_holds_is_preparing_only_when_the_scope_says_so(scene):
    ops = scene.ops
    ghost = "<no-copy-yet@x>"
    from mailkit import email_data, email_row, put_snapshot
    rows = [email_row(ghost, subject="Brand new")]
    put_snapshot(ops, "email", email_data(rows))
    assert message(scene.fadi, ghost).get_json()["state"] == "preparing"
    assert scene.admin.put("/api/ops/settings", {"email": {"mail_copies": {"scope": "assigned"}}}).status_code == 200
    assert message(scene.fadi, ghost).get_json()["state"] == "unavailable"
    assert scene.admin.put("/api/ops/settings", {"email": {"mail_copies": {"scope": "off"}}}).status_code == 200
    assert message(scene.fadi, scene.mails["alice_open"]).get_json()["state"] == "ready"    # an existing copy still opens


def test_a_preview_whose_msg_is_missing_is_not_ready(scene):
    key = mailbox.mail_key(scene.mails["alice_open"])
    mailbox.msg_path(scene.ops.mail_dir, key).unlink()
    body = message(scene.alice, scene.mails["alice_open"]).get_json()
    assert body["state"] == "preparing" and body["has_msg"] is False
    assert download(scene.alice, scene.mails["alice_open"]).status_code == 404


def test_a_damaged_preview_is_treated_as_missing_not_as_a_crash(scene):
    key = mailbox.mail_key(scene.mails["alice_open"])
    mailbox.preview_path(scene.ops.mail_dir, key).write_text("{ not json", encoding="utf-8")
    r = message(scene.alice, scene.mails["alice_open"])
    assert r.status_code == 200 and r.get_json()["state"] == "preparing" and r.get_json()["source"] == "snapshot"


@pytest.mark.parametrize("bad", ["lots", -5, 1.5, True, None, [1], {"a": 1}])
def test_a_preview_with_a_nonsense_size_is_treated_as_damaged_not_as_a_crash(scene, bad):
    key = mailbox.mail_key(scene.mails["alice_open"])
    path = mailbox.preview_path(scene.ops.mail_dir, key)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["msg_bytes"] = bad
    path.write_text(json.dumps(doc), encoding="utf-8")
    r = message(scene.alice, scene.mails["alice_open"])
    assert r.status_code == 200
    assert r.get_json()["state"] in ("preparing", "ready") and download(scene.alice, scene.mails["alice_open"]).status_code in (200, 404)


def test_odd_values_for_the_view_as_switch_never_crash(scene):
    mid = scene.mails["alice_open"]
    for odd in (["x"], {"a": 1}, 5, True, "", None):
        r = scene.alice.post("/api/ops/email/done", {"message_id": mid, "as": odd})
        assert r.status_code < 500, odd
    assert scene.alice.get(f"/api/ops/email/message?message_id={quote(mid, safe='')}&as=%00").status_code < 500


def test_view_as_is_read_only_but_the_viewers_own_rights_decide_access(scene):
    mid = scene.mails["alice_open"]
    r = message(scene.admin, mid + "", ).get_json()
    assert r["can"]["done"] is True
    ro = scene.admin.get(f"/api/ops/email/message?message_id={quote(mid, safe='')}&as=1001").get_json()
    assert ro["state"] == "ready" and ro["can"]["done"] is False and ro["can"]["reopen"] is False and ro["can"]["download"]
    # the manager viewing as an employee keeps their own scope: Card Management's manager still gets nothing
    assert scene.cara.get(f"/api/ops/email/message?message_id={quote(mid, safe='')}&as=1001").status_code == 404
    # an employee naming another account does not become that account
    r = scene.bob.get(f"/api/ops/email/message?message_id={quote(mid, safe='')}&as=1001")
    assert r.status_code == 404
    assert scene.admin.post("/api/ops/email/done", {"message_id": mid, "as": "1001"}).status_code == 403
    assert scene.admin.api("DELETE", f"/api/ops/email/done?message_id={quote(mid, safe='')}&as=1001").status_code == 403


# ----------------------------------------------------------------------------- the download itself
def test_download_headers_and_bytes(scene):
    mid = scene.mails["alice_open"]
    r = download(scene.alice, mid)
    assert r.status_code == 200 and r.data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") and len(r.data) == 600
    assert r.headers["Content-Type"] == "application/vnd.ms-outlook"
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Cache-Control"] == "no-store"
    disposition = r.headers["Content-Disposition"]
    assert disposition.startswith("attachment;") and 'filename="Alice open mail.msg"' in disposition
    assert "filename*=UTF-8''Alice%20open%20mail.msg" in disposition


def test_download_name_for_arabic_unsafe_and_long_subjects(scene):
    ops = scene.ops
    cases = {
        "<ar@x>": ("طلب صلاحية على نظام فليكس كيوب", "طلب صلاحية على نظام فليكس كيوب.msg"),
        "<bad@x>": ('RE: a/b\\c:d*e?f"g<h>i|j;k', "RE a b c d e f g h i j k.msg"),
        "<long@x>": ("L" * 200, "L" * 60 + ".msg"),
        "<empty@x>": ("   ", "e-mail.msg"),
        "<dots@x>": ("..", "e-mail.msg"),
        "<dev@x>": ("CON", "_CON.msg"),
    }
    from mailkit import email_data, email_row, put_snapshot
    put_snapshot(ops, "email", email_data([email_row(m) for m in cases]))
    for mid, (subject, wanted) in cases.items():
        put_copy(ops, mid, subject=subject)
        d = download(scene.admin, mid).headers["Content-Disposition"]
        assert f"filename*=UTF-8''{quote(wanted, safe='')}" in d, (subject, d)
        ascii_part = re.search(r'filename="([^"]*)"', d).group(1)
        assert ascii_part.isascii() and ascii_part.endswith(".msg") and '"' not in ascii_part and "/" not in ascii_part
        assert len(wanted) <= 64


def test_download_of_a_file_removed_a_moment_ago_is_a_clean_404(scene, monkeypatch):
    import calllog.ops.api as api
    monkeypatch.setattr(api, "send_file", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("gone")))
    r = download(scene.alice, scene.mails["alice_open"])
    assert r.status_code == 404 and "not ready" in r.get_json()["error"]


# ----------------------------------------------------------------------------- audit trail
def test_one_audit_line_per_open_and_per_download(app, clock, scene):
    mid = scene.mails["alice_open"]
    message(scene.alice, mid)
    message(scene.alice, mid)
    download(scene.alice, mid)
    lines = audit_actions(app, clock)
    assert [a for a in lines if a[0] == "ops_mail_open"] == [("ops_mail_open", mid, "1001")] * 2
    assert [a for a in lines if a[0] == "ops_mail_download"] == [("ops_mail_download", mid, "1001")]
    entry = [e for e in app.extensions["store"].read_audit("2026-10-11", "2026-10-11") if e["action"] == "ops_mail_download"][0]
    assert entry["details"]["subject"] == "Alice open mail" and "Body of" not in json.dumps(entry)      # never a body


# ----------------------------------------------------------------------------- Prepare now
def test_prepare_now_queues_one_email_scan(scene):
    ops = scene.ops
    mid = scene.mails["prep_alice"]
    r = scene.alice.post("/api/ops/email/message/prepare", {"message_id": mid})
    assert r.get_json() == {"ok": True, "queued": True, "already_pending": False, "state": "preparing"}
    assert [q["job"] for q in ops.pending_run_requests()] == ["email"]
    assert ops.pending_run_requests()[0]["requested_by"] == "1001"
    again = scene.mona.post("/api/ops/email/message/prepare", {"message_id": mid}).get_json()
    assert again["queued"] is True and again["already_pending"] is True and len(ops.pending_run_requests()) == 1


def test_prepare_now_does_nothing_for_a_mail_that_is_ready_or_not_yours(scene):
    ops = scene.ops
    ready = scene.alice.post("/api/ops/email/message/prepare", {"message_id": scene.mails["alice_open"]}).get_json()
    assert ready["queued"] is False and ready["state"] == "ready"
    r = scene.bob.post("/api/ops/email/message/prepare", {"message_id": scene.mails["prep_alice"]})
    assert r.status_code == 404 and r.get_json() == NOT_AVAILABLE
    assert ops.pending_run_requests() == []


def test_prepare_now_is_audited(app, clock, scene):
    scene.alice.post("/api/ops/email/message/prepare", {"message_id": scene.mails["prep_alice"]})
    assert any(a[0] == "ops_mail_prepare" and a[2] == "1001" for a in audit_actions(app, clock))


# ----------------------------------------------------------------------------- the System page
def test_health_reports_the_saved_copies_and_the_notification_files(scene):
    report = scene.admin.get("/api/ops/health").get_json()
    assert report["mail_copies"]["mails"] == 9 and report["mail_copies"]["bytes"] > 0 and report["mail_copies"]["scope"] == "assigned_and_pending"
    assert report["notifications"] == {"people": 0}
    scene.fadi.post("/api/ops/email/assign", {"message_id": scene.mails["free_flex"], "assigned_to_email": ALICE})
    assert scene.admin.get("/api/ops/health").get_json()["notifications"] == {"people": 1}
    assert scene.fadi.get("/api/ops/health").status_code == 403
