"""B1 - saved copies of mails: the collector writes ``ops/mail/<key>.msg`` + ``<key>.json`` while it works through Outlook.

Everything runs against the fake Outlook in tests/fake_outlook.py (never the real one): a frozen clock, fake addresses
(example.test), a throw-away OpsStore. The access rules that read these files are in test_mail_access.py.
"""
import gc
import json
import os
import tempfile
import time
from datetime import datetime, timedelta

import pytest

from calllog.ops import collector_config, mailbox, runner, schemas, settings
from calllog.ops.engines import email_engine as eng
from calllog.ops.engines.common import FakeMailer
from test_email_engine import (ALICE, BOB, BUSINESS, CUSTOMER, NOW, archive, email_settings, mail, make_ctx,  # noqa: F401
                               reply, store)
from tests.fake_outlook import MSG_MAGIC, FakeItem


def copies(**over):
    return email_settings(mail_copies=over) if over else email_settings()


def scan(store, ns, email=None, **ctx_kw):
    ctx = make_ctx(store, email or copies(), **ctx_kw)
    result = eng.run_scan(ctx, ns)
    assert result.ok, result.error
    return ctx, result


def rows_of(result):
    return {r["message_id"]: r for r in result.snapshot_data["rows"]}


def key(item):
    return mailbox.mail_key(item.PropertyAccessor.GetProperty(eng.PR_INTERNET_MESSAGE_ID))


def files_of(store):
    return sorted(p.name for p in store.mail_dir.iterdir())


def pending(subject, minutes_ago, mid, **kw):
    return mail(subject=subject, minutes_ago=minutes_ago, message_id=mid, savable=True, **kw)


# ----------------------------------------------------------------------------- what is saved
def test_a_pending_mail_gets_a_msg_and_a_preview(store):
    item = pending("Status of my request", 90, "<p1@example.test>", cc=["cc.person@example.test"],
                   body="Please check the problem\nSecond line", attachments=[("report.pdf", 1200), ("scan.png", 3000)],
                   msg_bytes=1500, sender_name="Customer One")
    ns = archive(item)
    ctx, result = scan(store, ns)
    k = key(item)
    assert files_of(store) == [f"{k}.json", f"{k}.msg"]
    preview = json.loads((store.mail_dir / f"{k}.json").read_text(encoding="utf-8"))
    assert preview["message_id"] == "<p1@example.test>" and preview["subject"] == "Status of my request"
    assert preview["from_name"] == "Customer One" and preview["from_email"] == CUSTOMER
    assert preview["to"] == [BUSINESS] and preview["cc"] == ["cc.person@example.test"]
    assert preview["received"] == (NOW - timedelta(minutes=90)).isoformat(timespec="seconds")
    assert preview["body_text"] == "Please check the problem\nSecond line" and preview["truncated"] is False
    assert preview["attachments"] == [{"name": "report.pdf", "size": 1200}, {"name": "scan.png", "size": 3000}]
    assert preview["msg_bytes"] == 1500 and preview["too_large"] is False and preview["saved_at"]
    msg = (store.mail_dir / f"{k}.msg").read_bytes()
    assert msg.startswith(MSG_MAGIC) and len(msg) == 1500 and b"subject=Status of my request" in msg
    assert item.save_calls and item.save_calls[0][1] == 3                      # olMSG
    assert rows_of(result)["<p1@example.test>"]["mail_copy"] == "ready"
    assert "1 mail copies saved" in result.summary
    assert any("Mail copies: 1 saved" in line for line in ctx.logs)


def test_the_file_name_is_the_hash_of_the_message_id():
    import hashlib
    assert mailbox.mail_key("<a@x>") == hashlib.sha1(b"<a@x>").hexdigest()[:32]
    assert mailbox.mail_key("<a@x>") != mailbox.mail_key("<A@x>") and len(mailbox.mail_key("../../x")) == 32
    assert mailbox.mail_key("\ud800") and mailbox.mail_key("طلب")                     # any text, even a lone surrogate
    for bad in ("../x", "A" * 32, "g" * 32, "a" * 31, ""):
        with pytest.raises(ValueError):
            mailbox.msg_path("/tmp", bad)


def test_replied_mails_are_not_copied_and_scope_assigned_copies_only_assigned(store):
    waiting = pending("Waiting", 40, "<w@example.test>")
    answered = pending("Answered", 100, "<a@example.test>", conv="CA")
    ns = archive(waiting, answered, reply(answered, ALICE, after=10))
    _, result = scan(store, ns)
    assert files_of(store) == [f"{key(waiting)}.json", f"{key(waiting)}.msg"]
    assert rows_of(result)["<a@example.test>"]["mail_copy"] == ""
    only = pending("Only assigned", 50, "<o@example.test>")
    other = pending("Pending but not assigned", 60, "<n@example.test>")
    store.set_override("assignments", "<o@example.test>", {"assigned_to_email": BOB, "assigned_to_name": "Bob", "marked_by": "m",
                                                           "marked_at": "2026-10-11T11:00:00", "note": "", "subject": "x"})
    ns2 = archive(only, other)
    scan(store.__class__(store.root.parent), ns2, copies(scope="assigned"))
    names = files_of(store)
    assert f"{key(only)}.msg" in names and f"{key(other)}.msg" not in names


def test_an_assigned_mail_is_copied_even_when_it_was_already_answered(store):
    answered = pending("Answered", 100, "<a@example.test>", conv="CA")
    store.set_override("assignments", "<a@example.test>", {"assigned_to_email": BOB, "assigned_to_name": "Bob", "marked_by": "m",
                                                           "marked_at": "2026-10-11T11:00:00", "note": "", "subject": "x"})
    _, result = scan(store, archive(answered, reply(answered, ALICE, after=10)))
    assert f"{key(answered)}.msg" in files_of(store) and rows_of(result)["<a@example.test>"]["mail_copy"] == "ready"


def test_scope_off_touches_nothing(store):
    item = pending("Waiting", 40, "<w@example.test>")
    ns = archive(item)
    _, result = scan(store, ns, copies(scope="off"))
    assert files_of(store) == [] and ns.fetched == [] and item.save_calls == []
    assert rows_of(result)["<w@example.test>"]["mail_copy"] == ""
    assert "mail copies" not in result.summary


def test_a_dry_run_writes_and_fetches_nothing(store):
    ns = archive(pending("Waiting", 40, "<w@example.test>"))
    scan(store, ns, dry_run=True)
    assert files_of(store) == [] and ns.fetched == []


def test_mail_that_is_not_for_the_business_mailboxes_is_never_copied(store):
    stray = mail(subject="Private", to=["somebody.else@example.test"], savable=True, message_id="<s@example.test>")
    ns = archive(stray)
    scan(store, ns)
    assert files_of(store) == [] and ns.fetched == []


# ----------------------------------------------------------------------------- incremental, ordered, capped
def test_the_second_scan_skips_what_exists(store):
    items = [pending(f"Mail {n}", 30 + n * 10, f"<m{n}@example.test>") for n in range(3)]
    ns = archive(*items)
    scan(store, ns)
    first_fetches, first_files = list(ns.fetched), files_of(store)
    mtimes = {n: (store.mail_dir / n).stat().st_mtime_ns for n in first_files}
    ctx, result = scan(store, ns)
    assert ns.fetched == first_fetches and all(len(i.save_calls) == 1 for i in items)      # nothing fetched, nothing saved again
    assert files_of(store) == first_files and {n: (store.mail_dir / n).stat().st_mtime_ns for n in first_files} == mtimes
    assert all(r["mail_copy"] == "ready" for r in rows_of(result).values())               # but the rows still say a copy exists
    assert not any(line.startswith("Mail copies:") for line in ctx.logs)                  # nothing to do, nothing to report


def test_assigned_first_then_the_oldest_pending_and_at_most_max_per_scan(store):
    oldest = pending("Oldest", 300, "<oldest@example.test>")
    older = pending("Older", 200, "<older@example.test>")
    newer = pending("Newer", 60, "<newer@example.test>")
    assigned = pending("Assigned and new", 20, "<assigned@example.test>")
    store.set_override("assignments", "<assigned@example.test>", {
        "assigned_to_email": BOB, "assigned_to_name": "Bob", "marked_by": "m", "marked_at": "2026-10-11T11:00:00", "note": "", "subject": "x"})
    ns = archive(oldest, older, newer, assigned)
    ctx, _ = scan(store, ns, copies(max_per_scan=2))
    order = lambda: [eid for eid, _ in ns.fetched]            # noqa: E731
    assert order() == [assigned.EntryID, oldest.EntryID]      # the assigned mail first, then the oldest pending one
    assert any("2 waiting for the next scan" in line for line in ctx.logs)
    scan(store, ns, copies(max_per_scan=2))                   # the rest follows on the next scan, oldest first
    assert order()[2:] == [older.EntryID, newer.EntryID]
    scan(store, ns, copies(max_per_scan=2))                   # and then there is nothing left to do
    assert len(order()) == 4 and all(len(i.save_calls) == 1 for i in (oldest, older, newer, assigned))


def test_an_open_assignment_is_copied_before_one_that_is_done(store):
    open_one = pending("Open", 10, "<open@example.test>")
    done_one = pending("Done", 500, "<done@example.test>")
    for mid, extra in (("<open@example.test>", {}), ("<done@example.test>", {"done_at": "2026-10-11T11:30:00"})):
        store.set_override("assignments", mid, {"assigned_to_email": BOB, "assigned_to_name": "Bob", "marked_by": "m",
                                                "marked_at": "2026-10-11T11:00:00", "note": "", "subject": "x", **extra})
    ns = archive(done_one, open_one)
    scan(store, ns, copies(max_per_scan=1))
    assert open_one.save_calls and not done_one.save_calls


def test_wanted_mails_is_a_pure_function_with_a_stable_order():
    rows = [{"message_id": "<p2>", "status": "Pending Reply", "received_time": "2026-10-11T10:00:00"},
            {"message_id": "<p1>", "status": "Pending Reply", "received_time": "2026-10-11T08:00:00"},
            {"message_id": "<r>", "status": "Replied", "received_time": "2026-10-11T07:00:00"},
            {"message_id": "<a-done>", "status": "Pending Reply", "received_time": "2026-10-11T06:00:00"},
            {"message_id": "<a-open>", "status": "Replied", "received_time": "2026-10-11T09:00:00"},
            {"message_id": "<p1>", "status": "Pending Reply", "received_time": "2026-10-11T08:00:00"},      # a split row
            {"message_id": "", "status": "Pending Reply"}, "junk"]
    assignments = {"<a-done>": {"done_at": "x"}, "<a-open>": {}, "<gone>": {"x": 1}}
    both = [(m, why) for m, why, _ in mailbox.wanted_mails(rows, assignments, "assigned_and_pending")]
    assert both == [("<a-open>", "assigned"), ("<a-done>", "assigned"), ("<p1>", "pending"), ("<p2>", "pending")]
    only = [(m, why) for m, why, _ in mailbox.wanted_mails(rows, assignments, "assigned")]
    assert only == [("<a-open>", "assigned"), ("<a-done>", "assigned")]
    assert mailbox.wanted_mails(rows, assignments, "off") == [] and mailbox.wanted_mails(rows, assignments, "nonsense") == []
    assert mailbox.wanted_mails(None, None, "assigned_and_pending") == []


# ----------------------------------------------------------------------------- big mails, bad mails
def test_a_mail_over_the_limit_gets_a_preview_only(store):
    big = pending("Huge attachment", 60, "<big@example.test>", msg_bytes=3 * 1024 * 1024, attachments=[("movie.mp4", 3_000_000)])
    small = pending("Small", 50, "<small@example.test>")
    ns = archive(big, small)
    _, result = scan(store, ns, copies(max_msg_mb=1))
    k = key(big)
    assert f"{k}.msg" not in files_of(store) and f"{k}.json" in files_of(store) and big.save_calls == []
    preview = json.loads((store.mail_dir / f"{k}.json").read_text(encoding="utf-8"))
    assert preview["msg_bytes"] == 0 and preview["too_large"] is True and preview["attachments"][0]["name"] == "movie.mp4"
    assert rows_of(result)["<big@example.test>"]["mail_copy"] == "too_large" and rows_of(result)["<small@example.test>"]["mail_copy"] == "ready"
    fetched = len(ns.fetched)
    _, again = scan(store, ns, copies(max_msg_mb=1))                      # a too-large mail is "done": not fetched again
    assert len(ns.fetched) == fetched and rows_of(again)["<big@example.test>"]["mail_copy"] == "too_large"


def test_a_file_bigger_than_the_item_claimed_is_still_refused(store):
    class Liar(FakeItem):
        Size = 100                                        # Outlook said 100 bytes, the saved file turns out much bigger

    liar = Liar(subject="Liar", sender=CUSTOMER, to=[BUSINESS], received=NOW - timedelta(minutes=30), body="Please check the problem",
                message_id="<liar@example.test>", entry_id="ELIAR", conversation_id="CL", savable=True, msg_bytes=2 * 1024 * 1024)
    scan(store, archive(liar), copies(max_msg_mb=1))
    assert liar.save_calls and f"{key(liar)}.msg" not in files_of(store)
    assert json.loads((store.mail_dir / f"{key(liar)}.json").read_text(encoding="utf-8"))["too_large"] is True
    assert not [n for n in os.listdir(store.mail_dir) if n.endswith(".tmp")]


def test_one_mail_failing_is_logged_and_skipped_and_retried_next_scan(store, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    bad_save = pending("SaveAs fails", 100, "<bad1@example.test>", save_error="Outlook is busy")
    bad_fetch = pending("Cannot be fetched", 90, "<bad2@example.test>")
    good = pending("Fine", 80, "<good@example.test>")
    ns = archive(bad_save, bad_fetch, good)
    real = ns.GetItemFromID
    ns.GetItemFromID = lambda entry_id, store_id=None: (_ for _ in ()).throw(RuntimeError("MAPI_E_NOT_FOUND")) \
        if entry_id == bad_fetch.EntryID else real(entry_id, store_id)
    ctx, result = scan(store, ns)
    assert result.ok and f"{key(good)}.msg" in files_of(store)
    assert f"{key(bad_save)}.json" not in files_of(store) and f"{key(bad_fetch)}.json" not in files_of(store)
    failures = [line for line in ctx.logs if line.startswith("Could not save a copy")]
    assert len(failures) == 2 and "Outlook is busy" in failures[0] + failures[1] and "MAPI_E_NOT_FOUND" in failures[0] + failures[1]
    assert not any("SaveAs fails" in line or "Cannot be fetched" in line for line in ctx.logs)          # a subject is never logged
    assert os.listdir(tmp_path / "tmp") == [] and not [n for n in os.listdir(store.mail_dir) if n.endswith(".tmp")]
    assert {r["mail_copy"] for m, r in rows_of(result).items() if m != "<good@example.test>"} == {""}
    bad_save._save_error = None                           # Outlook recovered
    ns.GetItemFromID = real
    scan(store, ns)
    assert f"{key(bad_save)}.msg" in files_of(store) and f"{key(bad_fetch)}.msg" in files_of(store)


def test_a_failure_that_hits_every_mail_does_not_flood_the_log(store):
    items = [pending(f"Mail {n}", 30 + n, f"<m{n}@example.test>", save_error="Outlook is busy") for n in range(12)]
    ctx, result = scan(store, archive(*items), copies(max_per_scan=12))
    lines = [line for line in ctx.logs if line.startswith("Could not save a copy")]
    assert len(lines) == eng.COPY_FAILURES_LOGGED == 5
    assert any("12 failed" in line for line in ctx.logs) and files_of(store) == [] and result.ok


def test_a_damaged_override_file_fails_the_scan_before_anything_is_copied(store):
    """As before this feature: a scan that cannot read the manual decisions fails (the last good snapshot stays) rather than
    forgetting them. No copy is written on the way."""
    ns = archive(pending("Waiting", 40, "<w@example.test>"))
    store.overrides_dir.mkdir(parents=True, exist_ok=True)
    (store.overrides_dir / "assignments.json").write_text("{ broken", encoding="utf-8")
    result = eng.run_scan(make_ctx(store, copies()), ns)
    assert result.ok is False and "damaged" in result.error and files_of(store) == [] and ns.fetched == []


def test_a_problem_while_choosing_the_copies_never_costs_the_scan(store, monkeypatch):
    ns = archive(pending("Waiting", 40, "<w@example.test>"))
    monkeypatch.setattr(mailbox, "wanted_mails", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("share unavailable")))
    ctx, result = scan(store, ns)
    assert result.ok and result.snapshot_data["rows"] and files_of(store) == []
    assert any(line.startswith("Mail copies skipped: RuntimeError") for line in ctx.logs)
    monkeypatch.undo()
    monkeypatch.setattr(eng, "save_mail_copies", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ctx, result = scan(store, ns)
    assert result.ok and any(line.startswith("Mail copies failed: RuntimeError: boom") for line in ctx.logs)
    assert all(r["mail_copy"] == "" for r in result.snapshot_data["rows"])


def test_the_body_is_cut_at_30000_characters_and_cleaned(store):
    long_body = "Line\n" * 10_000 + "\x00\x07bell\r\nwindows line"
    item = pending("Long mail", 40, "<long@example.test>", body=long_body)
    scan(store, archive(item))
    preview = json.loads((store.mail_dir / f"{key(item)}.json").read_text(encoding="utf-8"))
    assert len(preview["body_text"]) == schemas.MAIL_BODY_CHARS == 30000 and preview["truncated"] is True
    clean = mailbox.make_preview("<m>", body="a\x00b\x07c\r\nd\re\tf")
    assert clean["body_text"] == "abc\nd\ne\tf" and clean["truncated"] is False


def test_previews_limit_people_attachments_and_text_lengths():
    p = mailbox.make_preview("<m>" * 300, subject="S" * 900, from_name="N" * 500, from_email="UPPER@Example.TEST",
                             to=[f"p{i}@example.test" for i in range(80)] + ["p1@example.test"], cc="a@x.test; b@x.test;;",
                             attachments=[{"name": "n" * 400, "size": "12"}, {"name": "", "size": "bad"}, ("tuple.txt", 5)] * 60,
                             received=datetime(2026, 10, 11, 8, 0, 0), saved_at=datetime(2026, 10, 11, 9, 0, 0), msg_bytes="7")
    assert len(p["message_id"]) == 500 and len(p["subject"]) == 300 and len(p["from_name"]) == 200
    assert p["from_email"] == "upper@example.test" and len(p["to"]) == 50 and p["cc"] == ["a@x.test", "b@x.test"]
    assert len(p["attachments"]) == 100 and p["attachments"][0] == {"name": "n" * 200, "size": 12}
    assert p["attachments"][1] == {"name": "attachment", "size": 0} and p["attachments"][2] == {"name": "tuple.txt", "size": 5}
    assert p["received"] == "2026-10-11T08:00:00" and p["saved_at"] == "2026-10-11T09:00:00" and p["msg_bytes"] == 7


# ----------------------------------------------------------------------------- COM discipline
def test_mails_are_fetched_one_at_a_time_and_none_survives_the_scan(store):
    items = [pending(f"Mail {n}", 30 + n, f"<m{n}@example.test>") for n in range(6)]
    ns = archive(*items)
    outcome = eng.scan_and_analyze(make_ctx(store, copies()), ns, save_copies=True)
    gc.collect()
    assert len(ns.fetched) == 6 and ns.live_handles == 0 and ns.max_live_handles == 1
    assert all(entry_store == "STORE:2026_Part3" for _, entry_store in ns.fetched)           # the store of the archive was used
    assert len(outcome.rows) == 6 and all(r.get("mail_item") is None for r in outcome.rows)
    assert outcome.copies["saved"] == 6 and "mail_item" not in json.dumps(eng.to_snapshot_data(outcome, make_ctx(store, copies())))


def test_a_store_id_that_does_not_fit_falls_back_to_the_plain_lookup(store, monkeypatch):
    item = pending("Waiting", 40, "<w@example.test>")
    ns = archive(item)
    monkeypatch.setattr(eng, "_folder_store_id", lambda folder: "WRONG-STORE")
    scan(store, ns)
    assert [s for _, s in ns.fetched] == ["WRONG-STORE", None] and f"{key(item)}.msg" in files_of(store)


def test_the_scan_result_is_json_and_carries_the_copy_state_and_last_reply_fields(store):
    original = pending("Answered", 100, "<a@example.test>", conv="CA")
    answer = reply(original, ALICE, after=10)
    _, result = scan(store, archive(original, answer))
    text = json.dumps(result.snapshot_data)
    row = rows_of(result)["<a@example.test>"]
    assert row["last_reply_email"] == ALICE and row["last_reply_time"] and row["last_reply_by"] and row["mail_copy"] == ""
    assert "mail_copy" in text


def test_ctx_less_scan_paths_still_work_without_copies(store):
    """scan_and_analyze (used by the daily report) never saves anything unless asked to."""
    item = pending("Waiting", 40, "<w@example.test>")
    ns = archive(item)
    outcome = eng.scan_and_analyze(make_ctx(store, copies()), ns)
    assert outcome.copies == {} and ns.fetched == [] and files_of(store) == []


# ----------------------------------------------------------------------------- a copy that is half there
def test_a_preview_without_its_msg_is_saved_again_but_a_too_large_preview_is_not(store):
    item = pending("Waiting", 40, "<w@example.test>")
    ns = archive(item)
    scan(store, ns)
    (store.mail_dir / f"{key(item)}.msg").unlink()
    assert not mailbox.copy_complete(mailbox.list_files(store.mail_dir), key(item), store.mail_dir)
    scan(store, ns)
    assert (store.mail_dir / f"{key(item)}.msg").is_file() and len(item.save_calls) == 2
    (store.mail_dir / f"{key(item)}.msg").unlink()
    mailbox.write_copy(store.mail_dir, key(item), mailbox.make_preview("<w@example.test>", subject="x"))      # msg_bytes 0
    assert mailbox.copy_complete(mailbox.list_files(store.mail_dir), key(item), store.mail_dir)


def test_a_msg_without_a_preview_is_saved_again(store):
    item = pending("Waiting", 40, "<w@example.test>")
    ns = archive(item)
    scan(store, ns)
    (store.mail_dir / f"{key(item)}.json").unlink()
    scan(store, ns)
    assert (store.mail_dir / f"{key(item)}.json").is_file() and len(item.save_calls) == 2


def test_write_copy_is_atomic_and_leaves_no_temp_files(store, tmp_path):
    source = tmp_path / "x.msg"
    source.write_bytes(b"abc" * 100)
    k = mailbox.mail_key("<m>")
    out = mailbox.write_copy(store.mail_dir, k, mailbox.make_preview("<m>", subject="s"), msg_file=source)
    assert out["msg_bytes"] == 300 and sorted(os.listdir(store.mail_dir)) == [f"{k}.json", f"{k}.msg"]
    again = mailbox.write_copy(store.mail_dir, k, mailbox.make_preview("<m>", subject="s"))           # now too large: the old .msg goes
    assert again["msg_bytes"] == 0 and os.listdir(store.mail_dir) == [f"{k}.json"]
    with pytest.raises(OSError):
        mailbox.write_copy(store.mail_dir, k, mailbox.make_preview("<m>"), msg_file=tmp_path / "missing.msg")
    assert not [n for n in os.listdir(store.mail_dir) if n.endswith(".tmp")]


# ----------------------------------------------------------------------------- settings
def test_mail_copies_settings_defaults_and_validation():
    clean, errors = settings.validate({})
    assert errors == [] and clean["email"]["mail_copies"] == {"scope": "assigned_and_pending", "max_per_scan": 40,
                                                                "max_msg_mb": 25, "keep_days": 21}
    clean, errors = settings.validate({"email": {"mail_copies": {"scope": "assigned", "max_per_scan": "7", "keep_days": 3}}})
    assert errors == [] and clean["email"]["mail_copies"]["scope"] == "assigned" and clean["email"]["mail_copies"]["max_per_scan"] == 7
    for bad, path in (({"scope": "everything"}, "scope"), ({"max_per_scan": 0}, "max_per_scan"), ({"max_per_scan": 501}, "max_per_scan"),
                      ({"max_msg_mb": 0}, "max_msg_mb"), ({"keep_days": 400}, "keep_days"), ({"keep_days": "soon"}, "keep_days"),
                      ({"keep_days": True}, "keep_days")):
        _, errors = settings.validate({"email": {"mail_copies": bad}})
        assert [e["path"] for e in errors] == [f"email.mail_copies.{path}"], bad
    _, errors = settings.validate({"email": {"mail_copies": "all"}})
    assert errors and errors[0]["path"] == "email.mail_copies"


def test_a_pinned_value_in_collector_config_wins(tmp_path):
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps({"email": {"mail_copies": {"scope": "off"}}}), encoding="utf-8")
    cc = collector_config.load(path)
    assert "email.mail_copies.scope" in cc.pinned_keys()
    merged, errors = settings.validate(cc.apply(settings.deep_merge(settings.DEFAULTS, {"email": {"mail_copies": {"max_per_scan": 9}}})))
    assert errors == [] and merged["email"]["mail_copies"] == {"scope": "off", "max_per_scan": 9, "max_msg_mb": 25, "keep_days": 21}


def test_the_engine_reads_the_settings(store):
    cfg = eng.EmailConfig(copies(scope="assigned", max_per_scan=3, max_msg_mb=2))
    assert (cfg.copies_scope, cfg.copies_per_scan, cfg.copies_max_bytes) == ("assigned", 3, 2 * 1024 * 1024)
    for odd in ({}, {"mail_copies": None}, {"mail_copies": {"scope": "bogus", "max_per_scan": "x"}}):
        cfg = eng.EmailConfig(odd)
        assert cfg.copies_scope == "assigned_and_pending" and cfg.copies_per_scan == 40


# ----------------------------------------------------------------------------- housekeeping
def age(path, days=0, hours=0):
    t = time.time() - days * 86400 - hours * 3600
    os.utime(path, (t, t))


def seed_copy(store, mid, msg=True, old_hours=0, bytes_=100):
    k = mailbox.mail_key(mid)
    preview = mailbox.make_preview(mid, subject="s", msg_bytes=bytes_ if msg else 0)
    (store.mail_dir).mkdir(parents=True, exist_ok=True)
    store.write_json(store.mail_dir / f"{k}.json", preview)
    if msg:
        (store.mail_dir / f"{k}.msg").write_bytes(b"x" * bytes_)
    for p in store.mail_dir.glob(f"{k}.*"):
        age(p, hours=old_hours)
    return k


def test_housekeeping_keeps_what_is_wanted_and_removes_the_rest_after_keep_days(store):
    keep, drop, back = (seed_copy(store, m) for m in ("<keep@x>", "<drop@x>", "<back@x>"))
    day = datetime(2026, 10, 1)
    first = mailbox.housekeeping(store, {keep, back}, 21, today=day.date(), now=day)
    assert first["removed"] == 0 and first["copies"] == 3 and first["bytes"] == 3 * (100 + os.path.getsize(store.mail_dir / f"{keep}.json"))
    assert store.read_state("mail-copies")["left"] == {drop: "2026-10-01"}
    soon = day + timedelta(days=20)
    assert mailbox.housekeeping(store, {keep, back}, 21, today=soon.date(), now=soon)["removed"] == 0
    assert (store.mail_dir / f"{drop}.msg").exists()
    # the mail comes back before the 21 days are over (re-assigned): the countdown is forgotten
    again = day + timedelta(days=21)
    result = mailbox.housekeeping(store, {keep, back, drop}, 21, today=again.date(), now=again)
    assert result["removed"] == 0 and store.read_state("mail-copies")["left"] == {}
    later = again + timedelta(days=1)
    mailbox.housekeeping(store, {keep, back}, 21, today=later.date(), now=later)             # left the set again: a new countdown
    end = later + timedelta(days=21)
    gone = mailbox.housekeeping(store, {keep, back}, 21, today=end.date(), now=end)
    assert gone["removed"] == 2 and not (store.mail_dir / f"{drop}.msg").exists() and not (store.mail_dir / f"{drop}.json").exists()
    assert (store.mail_dir / f"{keep}.msg").exists() and (store.mail_dir / f"{back}.json").exists()
    assert store.read_state("mail-copies")["left"] == {}


def test_housekeeping_removes_orphans_but_not_a_copy_that_is_being_written(store):
    old_msg_only = mailbox.mail_key("<o1@x>")
    (store.mail_dir / f"{old_msg_only}.msg").write_bytes(b"x")
    age(store.mail_dir / f"{old_msg_only}.msg", hours=3)
    fresh_msg_only = mailbox.mail_key("<o2@x>")
    (store.mail_dir / f"{fresh_msg_only}.msg").write_bytes(b"x")                 # the collector is writing it right now
    broken = seed_copy(store, "<o3@x>", msg=True, old_hours=3)
    (store.mail_dir / f"{broken}.msg").unlink()                                 # promises a .msg that is gone
    preview_only = seed_copy(store, "<o4@x>", msg=False, old_hours=3)           # "too large": complete without a .msg
    old_tmp = store.mail_dir / f".{old_msg_only}.msg.abcd1234.tmp"
    old_tmp.write_bytes(b"x")
    age(old_tmp, hours=3)
    new_tmp = store.mail_dir / f".{fresh_msg_only}.msg.ef567890.tmp"
    new_tmp.write_bytes(b"x")
    stranger = store.mail_dir / "README.txt"
    stranger.write_text("not ours", encoding="utf-8")
    wanted = {broken, preview_only, fresh_msg_only}
    result = mailbox.housekeeping(store, wanted, 21, today=datetime.now().date(), now=datetime.now())
    assert result["orphans"] == 2 and result["temp"] == 1
    assert not (store.mail_dir / f"{old_msg_only}.msg").exists() and not (store.mail_dir / f"{broken}.json").exists()
    assert (store.mail_dir / f"{fresh_msg_only}.msg").exists() and (store.mail_dir / f"{preview_only}.json").exists()
    assert not old_tmp.exists() and new_tmp.exists() and stranger.exists()


def test_housekeeping_survives_an_empty_or_missing_folder_and_a_damaged_state_file(store, tmp_path):
    other = type(store)(tmp_path / "nothing-here")                                # never initialised: no mail folder at all
    assert mailbox.housekeeping(other, set(), 21)["copies"] == 0
    seed_copy(store, "<x@x>")
    store.state_dir.joinpath("mail-copies.json").write_text("{ no", encoding="utf-8")
    result = mailbox.housekeeping(store, set(), 21, today=datetime(2026, 10, 1).date(), now=datetime(2026, 10, 1))
    assert result["copies"] == 1 and result["removed"] == 0


def test_wanted_keys_from_the_store(store):
    from test_ops_api import email_data, email_row, replied_row, put_snapshot
    put_snapshot(store, "email", email_data([email_row("<p@x>"), replied_row("<r@x>", ALICE), email_row("<n@x>", status="No Action Needed")]))
    store.set_override("assignments", "<a@x>", {"assigned_to_email": BOB})
    assert mailbox.wanted_keys_from(store) == {mailbox.mail_key("<p@x>"), mailbox.mail_key("<a@x>")}


def test_the_collector_runs_the_mail_housekeeping_daily(store):
    keep, drop = seed_copy(store, "<keep@x>"), seed_copy(store, "<drop@x>")
    store.set_override("assignments", "<keep@x>", {"assigned_to_email": BOB})
    notification_user = store.notifications_dir / "1001.json"
    from calllog.ops import notifications
    notifications.add(store, "1001", [notifications.make_item("done", "old", "t", "/x", "a", "<o>", datetime(2026, 8, 1))], datetime(2026, 8, 1))
    store.write_state("mail-copies", {"day": "2026-09-01", "left": {drop: "2026-09-01"}})
    clock = datetime(2026, 10, 12, 22, 0)
    r = runner.Runner(store, None, jobs=("email",), clock=lambda: clock, heartbeat_seconds=0, poll_seconds=0.02, mailer_factory=FakeMailer)
    removed = r.housekeeping(clock)
    assert removed["mail copies"] == 2 and removed["notifications"] == 1
    assert (store.mail_dir / f"{keep}.msg").exists() and not (store.mail_dir / f"{drop}.msg").exists()
    assert notification_user.exists() and notifications.read(store, "1001")["items"] == []


def test_folder_summary_counts_mails_not_files(store):
    assert mailbox.folder_summary(store.mail_dir) == {"mails": 0, "bytes": 0, "oldest": None}
    seed_copy(store, "<a@x>"), seed_copy(store, "<b@x>", msg=False)
    summary = mailbox.folder_summary(store.mail_dir)
    assert summary["mails"] == 2 and summary["bytes"] > 100 and summary["oldest"]


def test_download_names_are_safe():
    assert mailbox.download_name("Hello World") == ("Hello World.msg", "Hello World.msg")
    name, ascii_name = mailbox.download_name('RE: a/b\\c:d*e?f"g<h>i|j')
    assert name == "RE a b c d e f g h i j.msg" and ascii_name == name
    name, ascii_name = mailbox.download_name("طلب صلاحية - فرع 12")
    assert name == "طلب صلاحية - فرع 12.msg" and ascii_name.isascii() and ascii_name.endswith(".msg")
    assert mailbox.download_name("x" * 100)[0] == "x" * 60 + ".msg"
    assert mailbox.download_name(None)[0] == "e-mail.msg" and mailbox.download_name("...")[0] == "e-mail.msg"
    assert mailbox.download_name("aux")[0] == "_aux.msg" and mailbox.download_name("LPT1.txt")[0] == "_LPT1.txt.msg"
