"""Builders shared by the saved-copy, access, Done and notification tests (docs/FEATURES-V2.md, feature B).

Everything is made up: people are alice / bob / carol @example.test, mails are ``<...@x>`` ids. Import the fixtures you need
into a test module the way tests/test_ops_my.py does (``scene`` needs ``ops``, ``make_account`` and ``admin`` next to it).
"""
import tempfile
from pathlib import Path

import pytest

from calllog.ops import mailbox
from test_ops_api import beat, email_data, email_row, put_snapshot, replied_row  # noqa: F401  (re-exported for the tests)

ALICE, BOB, CAROL = "alice@example.test", "bob@example.test", "carol@example.test"
FADI, CARA, MONA = "fadi@example.test", "cara@example.test", "mona@example.test"
MSG_MARK = b"FAKE-MSG-FOR-TESTS"


def msg_bytes(subject, size=600):
    """Recognisable fake .msg bytes: the real OLE header, a marker and the subject."""
    head = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + MSG_MARK + b"\n" + subject.encode("utf-8") + b"\n"
    return head + b"\0" * max(0, size - len(head))


def put_copy(ops, message_id, subject="A subject", body="Body line one\nBody line two", size=600, too_large=False,
             attachments=(("report.pdf", 12345),), **fields):
    """Write the saved copy of one mail the way the collector does (preview + .msg, or preview only)."""
    key = mailbox.mail_key(message_id)
    preview = mailbox.make_preview(message_id, subject=subject, from_name="Customer One", from_email="cust@example.test",
                                   to=["Support Desk"], cc=["Team Box"], received="2026-10-11T08:00:00", body=body,
                                   attachments=[{"name": n, "size": z} for n, z in attachments], **fields)
    if too_large:
        mailbox.write_copy(ops.mail_dir, key, dict(preview, too_large=True))
        return key
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "x.msg"
        source.write_bytes(msg_bytes(subject, size))
        mailbox.write_copy(ops.mail_dir, key, preview, msg_file=source)
    return key


def assignment(to_email, to_name, by="Mona Manager", by_id="mona", at="2026-10-11T08:30:00", team="Flex Cube", **extra):
    return {"assigned_to_email": to_email, "assigned_to_name": to_name, "note": "please handle", "marked_by": by,
            "marked_by_id": by_id, "marked_at": at, "subject": "x", "team": team, "notified": True, **extra}


@pytest.fixture
def scene(ops, make_account, admin):
    """Accounts of every kind, one mail for every access situation, and the saved copies.

    employees: alice (1001), bob (1002), carol (1003, no mailbox);   managers: fadi (Flex Cube), cara (Card Management),
    mona (every team);   admin.   Mails (message ids are the keys of ``scene.mails``):
      alice_open   Flex Cube, pending, assigned to alice, copy ready      alice_done   assigned to alice, marked done
      bob_open     Flex Cube, pending, assigned to bob                    free_flex    Flex Cube, pending, nobody's
      free_card    Card Management, pending, nobody's                     free_uncl    Unclassified, pending, nobody's
      uncl_alice   Unclassified, assigned to alice                         replied_free Flex Cube, replied, no copy
      prep_alice   assigned to alice, NO copy yet (preparing)             big_alice    assigned to alice, preview only
      gone_alice   assigned to alice, NOT in the latest scan (copy exists)
    """
    class Scene:
        pass
    s = Scene()
    s.alice = make_account("1001", "user", "Alice Employee", email=ALICE, team="Flex Cube")
    s.bob = make_account("1002", "user", "Bob Employee", email=BOB, team="Flex Cube")
    s.carol = make_account("1003", "user", "Carol Employee")
    s.fadi = make_account("fadi", "manager", "Fadi Flex", email=FADI, team="Flex Cube")
    s.cara = make_account("cara", "manager", "Cara Card", email=CARA, team="Card Management")
    s.mona = make_account("mona", "manager", "Mona Manager", email=MONA, team="__all__")
    s.admin = admin
    s.ops = ops
    ids = {name: f"<{name.replace('_', '-')}@x>" for name in (
        "alice_open", "alice_done", "bob_open", "free_flex", "free_card", "free_uncl", "uncl_alice", "replied_free",
        "prep_alice", "big_alice", "gone_alice")}
    s.mails = ids
    uncl = dict(team="Unclassified", all_teams=["Unclassified"], teams_pending=["Unclassified"])
    card = dict(team="Card Management", all_teams=["Card Management"], teams_pending=["Card Management"])
    rows = [
        email_row(ids["alice_open"], subject="Alice open mail"),
        email_row(ids["alice_done"], subject="Alice done mail"),
        email_row(ids["bob_open"], subject="Bob open mail"),
        email_row(ids["free_flex"], subject="Free flex mail"),
        email_row(ids["free_card"], subject="Free card mail", **card),
        email_row(ids["free_uncl"], subject="Free unclassified mail", **uncl),
        email_row(ids["uncl_alice"], subject="Unclassified but Alice's", **uncl),
        replied_row(ids["replied_free"], BOB, 20, subject="Replied free mail"),
        email_row(ids["prep_alice"], subject="Alice mail without copy"),
        email_row(ids["big_alice"], subject="Alice big mail"),
    ]
    put_snapshot(ops, "email", email_data(rows))
    for job in ("email", "sla", "itsm"):
        beat(ops, job)
    for name in ("alice_open", "alice_done", "uncl_alice", "prep_alice", "big_alice", "gone_alice"):
        extra = {"done_at": "2026-10-11T08:50:00", "done_by": "Alice Employee", "done_by_id": "1001",
                 "done_note": "all fine"} if name == "alice_done" else {}
        team = "Unclassified" if name == "uncl_alice" else "Flex Cube"
        ops.set_override("assignments", ids[name], assignment(ALICE, "Alice", team=team, **extra))
    ops.set_override("assignments", ids["bob_open"], assignment(BOB, "Bob"))
    for name, subject in (("alice_open", "Alice open mail"), ("alice_done", "Alice done mail"), ("bob_open", "Bob open mail"),
                          ("free_flex", "Free flex mail"), ("free_card", "Free card mail"), ("free_uncl", "Free unclassified mail"),
                          ("uncl_alice", "Unclassified but Alice's"), ("gone_alice", "Gone from the scan")):
        put_copy(ops, ids[name], subject=subject, body=f"Body of {name}")
    put_copy(ops, ids["big_alice"], subject="Alice big mail", too_large=True)
    return s
