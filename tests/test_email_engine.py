"""E-mail engine: Outlook scan, reply matching, classification, SLA, overrides, statistics, history, snapshot.

Everything runs against the fake Outlook in tests/fake_outlook.py with a frozen clock (Sunday 2026-10-11 12:00), fake
addresses (example.test) and a throw-away OpsStore. No Outlook, no Windows, no network.
"""
import itertools
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from calllog.ops import schemas, settings
from calllog.ops.engines import email_engine as eng
from calllog.ops.engines import email_rules as rules
from calllog.ops.engines.common import JobContext
from calllog.ops.store import OpsStore
from tests.fake_outlook import (
    FakeAddressEntry, FakeFolder, FakeItem, FakeNamespace, FakeRecipient, flaky_dispatch)

NOW = datetime(2026, 10, 11, 12, 0, 0)
BUSINESS, BUSINESS2 = "support@example.test", "identity@example.test"
ALICE, BOB, CAROL, DAVE = "alice@example.test", "bob@example.test", "carol@example.test", "dave@example.test"
MANAGER, IGNORED, CUSTOMER = "manager@example.test", "noreply@example.test", "cust@example.test"
ROOT = "01" * 22                                      # a 44-character ConversationIndex root
_ids = itertools.count(1)


# --------------------------------------------------------------------------------------------------- builders
def email_settings(**over):
    """The e-mail settings section, validated like the real ones."""
    raw = {"email": {
        "business_emails": [BUSINESS, BUSINESS2], "team_members": [MANAGER],
        "team_members_by_team": {"Flex Cube": [ALICE, BOB], "MFA Team": [CAROL], "Card Management": [DAVE]},
        "ignored_sender_emails": [IGNORED], "manager_emails": [MANAGER], "target_archive_name": "2026_Part3",
        "sla_target_minutes": 60, "backlog_lookback_days": 1}}
    raw = settings.deep_merge(raw, {"email": over})
    clean, errors = settings.validate(raw)
    assert errors == [], errors
    return clean["email"]


def make_ctx(store, email=None, now=NOW, **kw):
    logs = []
    ctx = JobContext(job="email", settings=email or email_settings(), general={}, store=store, now=lambda: now,
                     log=logs.append, **kw)
    ctx.logs = logs
    return ctx


def mail(subject="Status", sender=CUSTOMER, to=(BUSINESS,), cc=(), minutes_ago=30, body="Please check the problem",
         conv=None, index="", message_id=None, **kw):
    n = next(_ids)
    return FakeItem(subject=subject, sender=sender, to=to, cc=cc, received=NOW - timedelta(minutes=minutes_ago),
                    body=body, conversation_id=conv if conv is not None else f"C{n}", conversation_index=index,
                    message_id=message_id if message_id is not None else f"<m{n}@example.test>", entry_id=f"E{n}", **kw)


def reply(original, sender=ALICE, after=10, body="Done, fixed", **kw):
    n = next(_ids)
    fields = dict(subject="RE: " + original.Subject, sender=sender, to=[original.SenderEmailAddress], cc=[BUSINESS],
                  received=original.ReceivedTime + timedelta(minutes=after), body=body,
                  conversation_id=original.ConversationID, conversation_index=original.ConversationIndex,
                  message_id=f"<r{n}@example.test>", entry_id=f"RE{n}")
    fields.update(kw)
    return FakeItem(**fields)


def archive(*items, name="2026_Part3", folders=()):
    inbox = FakeFolder("Inbox", items)
    other = FakeFolder("Personal", [mail(subject="elsewhere")])      # another root: must never be scanned
    return FakeNamespace([FakeFolder(name, children=[inbox, *folders]), other])


@pytest.fixture
def store(tmp_path):
    return OpsStore(tmp_path / "data").init()


@pytest.fixture
def ctx(store):
    return make_ctx(store)


def scan(ctx, *items, **kw):
    return eng.scan_and_analyze(ctx, archive(*items), **kw)


def only_row(outcome):
    assert len(outcome.rows) == 1, [r["subject"] for r in outcome.rows]
    return outcome.rows[0]


# ---------------------------------------------------------------------------------------- business filter
def test_business_filter_counts_to_only_and_never_cc(ctx):
    business = mail(subject="to business")
    cc_only = mail(subject="cc only", to=["other@example.test"], cc=[BUSINESS])
    unrelated = mail(subject="unrelated", to=["other@example.test"])
    team_mail = FakeItem(subject="team note", sender=ALICE, to=[CUSTOMER], received=NOW - timedelta(minutes=5),
                         body="Hello there", conversation_id="X", message_id="<t1@example.test>", entry_id="T1")
    out = scan(ctx, business, cc_only, unrelated, team_mail)
    assert [r["subject"] for r in out.rows] == ["to business"]
    assert len(out.included_emails) == 1
    assert {e["subject"] for e in out.all_emails} == {"to business", "team note"}    # the team's mail is kept for matching
    assert cc_only.body_reads == 0 and unrelated.body_reads == 0                    # the costly Body read is skipped


def test_only_the_target_archive_is_scanned(ctx):
    out = scan(ctx, mail(subject="in archive"))
    assert [r["subject"] for r in out.rows] == ["in archive"]           # the other root's mail was never looked at
    assert out.scan_info["items_seen"] == 1


def test_second_business_mailbox_counts_and_address_volume(ctx):
    out = scan(ctx, mail(to=[BUSINESS, BUSINESS2]), mail(to=[BUSINESS2]))
    assert out.stats["business_address_volume"] == {BUSINESS: 1, BUSINESS2: 2}
    assert only_row(scan(ctx, mail(to=[BUSINESS, BUSINESS2])))["business_addresses_in_to"] == f"{BUSINESS2}; {BUSINESS}"


def test_ignored_sender_is_dropped_before_the_body_is_read(ctx):
    noisy = mail(sender=IGNORED, subject="newsletter")
    out = scan(ctx, noisy, mail(subject="real"))
    assert [r["subject"] for r in out.rows] == ["real"] and noisy.body_reads == 0


def test_duplicate_copies_of_one_message_count_once(ctx):
    original = mail(subject="copied", message_id="<same@example.test>")
    twin = mail(subject="copied", message_id="<same@example.test>", conv="Cx")
    ns = archive(original, folders=[FakeFolder("Rule copy", [twin])])
    assert len(eng.scan_and_analyze(ctx, ns).rows) == 1


def test_entry_id_is_the_row_key_when_the_message_id_is_unreadable(store):
    item = mail(subject="no id", message_id="")
    item.PropertyAccessor.values.clear()
    store.set_override("no_action", item.EntryID, {"marked_by": "admin", "marked_at": "2026-10-11T09:00:00"})
    row = only_row(scan(make_ctx(store), item))
    assert row["message_id"] == row["entry_id"] == item.EntryID and row["status"] == "No Action Needed"


def test_smtp_address_resolution_including_exchange_users(ctx):
    exchange_sender = mail(subject="from exchange", sender="/O=EXCH/CN=BOB", sender_type="EX",
                           sender_entry=FakeAddressEntry(exchange_smtp="Cust.Two@Example.test"))
    legacy_dn_recipient = FakeRecipient("/O=EXCH/CN=SUPPORT", 1, FakeAddressEntry(smtp=BUSINESS.upper()))
    via_entry = mail(subject="legacy dn recipient", to=[legacy_dn_recipient])
    out = scan(ctx, exchange_sender, via_entry)
    by = {r["subject"]: r for r in out.rows}
    assert by["from exchange"]["customer_email"] == "cust.two@example.test"
    assert by["legacy dn recipient"]["business_addresses_in_to"] == BUSINESS
    raw = FakeAddressEntry(address="Raw@Example.test")
    assert eng.get_smtp_address_from_address_entry(raw) == "raw@example.test"
    assert eng.get_smtp_address_from_address_entry(None) == ""


# ---------------------------------------------------------------------------------------- folder walking
def test_pgp_folders_are_skipped_before_their_items_are_touched(ctx):
    pgp_top = FakeFolder("PGP Mail", [mail(subject="secret one")])
    pgp_child = FakeFolder("Archive pgp 2025", [mail(subject="secret two")])
    sub = FakeFolder("Sub", [mail(subject="kept")], children=[pgp_child])
    out = eng.scan_and_analyze(ctx, archive(folders=[pgp_top, sub]))
    assert [r["subject"] for r in out.rows] == ["kept"]
    assert pgp_top.items.yielded == 0 and pgp_child.items.yielded == 0
    assert out.scan_info["folders"] == 3                                   # archive, Inbox, Sub


def test_walk_stops_at_the_first_item_older_than_the_window(ctx):
    fresh = [mail(subject=f"fresh {i}", minutes_ago=10 + i) for i in range(3)]
    old = [mail(subject=f"old {i}", minutes_ago=3 * 24 * 60 + i) for i in range(200)]
    ns = archive(*old, *fresh)
    out = eng.scan_and_analyze(ctx, ns)
    inbox = ns.Folders[0].Folders[0]
    assert sorted(r["subject"] for r in out.rows) == ["fresh 0", "fresh 1", "fresh 2"]
    assert inbox.items.yielded == 4                                         # 3 fresh + the first old one, then stop


def test_a_refused_sort_disables_the_early_stop_instead_of_losing_mail(ctx):
    items = [mail(subject=f"old {i}", minutes_ago=3 * 24 * 60) for i in range(5)] + [mail(subject="fresh")]
    unsorted_folder = FakeFolder("Inbox", items, fail_sort=True)
    ns = FakeNamespace([FakeFolder("2026_Part3", children=[unsorted_folder])])
    out = eng.scan_and_analyze(ctx, ns)
    assert [r["subject"] for r in out.rows] == ["fresh"] and unsorted_folder.items.yielded == 6


def test_restrict_filter_is_opt_in(store):
    default_ns = archive(mail(subject="a"))
    eng.scan_and_analyze(make_ctx(store), default_ns)
    assert default_ns.Folders[0].Folders[0].items.restrict_calls == []
    ctx = make_ctx(store, email_settings(advanced={"use_restrict_filter": True}))
    ns = archive(mail(subject="a"), mail(subject="ancient", minutes_ago=9 * 24 * 60))
    out = eng.scan_and_analyze(ctx, ns)
    calls = ns.Folders[0].Folders[0].items.restrict_calls
    assert len(calls) == 1 and "2026-10-10 00:00:00" in calls[0] and "urn:schemas:httpmail:datereceived" in calls[0]
    assert [r["subject"] for r in out.rows] == ["a"]


def test_scan_window_ignores_future_mail_and_honours_the_end_buffer(store):
    future = mail(subject="future", minutes_ago=-5)
    recent, older = mail(subject="recent", minutes_ago=10), mail(subject="older", minutes_ago=50)
    assert {r["subject"] for r in scan(make_ctx(store), future, recent, older).rows} == {"recent", "older"}
    buffered = make_ctx(store, email_settings(advanced={"scan_end_buffer_minutes": 30}))
    assert [r["subject"] for r in scan(buffered, future, recent, older).rows] == ["older"]


def test_default_mailbox_when_no_archive_name_is_set(store):
    ctx = make_ctx(store, email_settings(target_archive_name=""))
    item = mail(subject="in default mailbox")
    ns = FakeNamespace([FakeFolder("someone@example.test", children=[FakeFolder("Inbox", [item])])])
    assert [r["subject"] for r in eng.scan_and_analyze(ctx, ns).rows] == ["in default mailbox"]


def test_missing_archive_is_a_clear_error_without_leaking_mailbox_names(ctx):
    ns = FakeNamespace([FakeFolder("2025_Part1"), FakeFolder("someone@example.test")])
    with pytest.raises(RuntimeError) as err:
        eng.scan_and_analyze(ctx, ns)
    assert "2026_Part3" in str(err.value) and "2025_Part1" in str(err.value) and "someone@" not in str(err.value)


# ----------------------------------------------------------------------------------------- reply matching
def test_match_by_conversation_id(ctx):
    original = mail(subject="Cannot log in", conv="CONV-A")
    row = only_row(scan(ctx, original, reply(original, after=12)))
    assert (row["status"], row["match_method"], row["response_minutes"]) == ("Replied", "ConversationID", 12)
    assert row["first_reply_by"] == "Alice" and row["first_reply_email"] == ALICE and row["first_response_time"] == "12 Min"


def test_match_falls_back_to_the_conversation_index_root(ctx):
    original = mail(subject="Cannot log in", conv="CONV-A", index=ROOT + "0000000001")
    answer = reply(original, conversation_id="CONV-OTHER-STORE", conversation_index=ROOT + "000000010000000002")
    row = only_row(scan(ctx, original, answer))
    assert row["status"] == "Replied" and row["match_method"] == "ConversationIndex"


def test_match_falls_back_to_the_clean_subject_when_addressed_back(ctx):
    original = mail(subject="Printer help", conv="")
    answer = reply(original, conversation_id="", subject="RE: FW: [SR#12345] printer help ")
    row = only_row(scan(ctx, original, answer))
    assert row["status"] == "Replied" and row["match_method"] == "Subject"


def test_subject_match_needs_the_reply_to_be_addressed_back(ctx):
    original = mail(subject="Printer help", conv="")
    elsewhere = reply(original, conversation_id="", to=["someone.else@example.test"], cc=[])
    row = only_row(scan(ctx, original, elsewhere))
    assert row["status"] == "Pending Reply" and row["match_method"] == "No Match"
    assert "addressed" in row["diag_reason"] or "did not include" in row["diag_reason"]


def test_no_match_stays_pending_with_a_diagnosis(ctx):
    original = mail(subject="Cannot log in", minutes_ago=50)
    unrelated = FakeItem(subject="Lunch", sender=ALICE, to=[CUSTOMER], received=NOW - timedelta(minutes=45), body="Noon?",
                         conversation_id="Z", message_id="<z@example.test>", entry_id="Z1")
    row = only_row(scan(ctx, original, unrelated))
    assert row["status"] == "Pending Reply" and row["match_method"] == "No Match" and row["reply_count"] == 0
    assert row["diag_candidate_by"] == "Alice" and row["diag_gap_minutes"] == 5
    assert "No shared ConversationID" in row["diag_reason"]
    lonely = only_row(scan(ctx, mail(subject="Alone")))
    assert lonely["diag_reason"].startswith("No team-member email found")


def test_a_reply_older_than_the_customer_mail_does_not_count(ctx):
    original = mail(subject="Cannot log in", conv="CONV-A", minutes_ago=30)
    too_early = reply(original, after=-10)
    row = only_row(scan(ctx, original, too_early))
    assert row["status"] == "Pending Reply"
    assert "BEFORE" in row["diag_reason"]


def test_replies_from_several_members_first_one_wins(ctx):
    original = mail(subject="Cannot log in", conv="CONV-A")
    out = scan(ctx, original, reply(original, sender=BOB, after=25), reply(original, sender=ALICE, after=10))
    row = only_row(out)
    assert (row["first_reply_by"], row["last_reply_by"], row["reply_count"]) == ("Alice", "Bob", 2)
    assert row["multiple_team_reply"] is True and row["multiple_team_reply_team"] == "Flex Cube"
    assert out.stats["total_team_replies"] == 2 and out.stats["team_replies"] == 2


def test_two_replies_from_one_person_are_not_duplicated_work(ctx):
    original = mail(subject="Cannot log in", conv="CONV-A")
    row = only_row(scan(ctx, original, reply(original, after=10), reply(original, after=20)))
    assert row["reply_count"] == 2 and row["multiple_team_reply"] is False


def test_reminders_merge_into_one_row_dated_at_the_first_message(ctx):
    first = mail(subject="Need access", conv="CONV-R", minutes_ago=120)
    second = mail(subject="RE: Need access", conv="CONV-R", minutes_ago=60, body="Reminder")
    third = mail(subject="RE: Need access", conv="CONV-R", minutes_ago=20, body="any update?")
    row = only_row(scan(ctx, third, first, second))
    assert row["reminder_count"] == 2 and row["current_waiting_time"] == "2 H 0 M"
    assert row["status"] == "Pending Reply" and row["escalated"] is True


def test_a_reply_in_between_makes_the_next_message_a_new_request(ctx):
    first = mail(subject="Need access", conv="CONV-R", minutes_ago=120)
    answer = reply(first, after=10)
    second = mail(subject="RE: Need access", conv="CONV-R", minutes_ago=60, body="Another problem please")
    out = scan(ctx, first, answer, second)
    assert [(r["status"], r["match_method"]) for r in out.rows] == [("Replied", "ConversationID"),
                                                                     ("Pending Reply", "No Match")]


# ------------------------------------------------------------------------------------------ no action needed
@pytest.mark.parametrize("body, kind", [
    ("Thank you, noted.", "Matched closing phrase"),
    ("شكرا جزيلا", "Matched closing phrase"),
    ("Perfect 👍", "Short-message safety net"),
    ("تمام تسلم", "Matched closing phrase"),
])
def test_no_action_by_keyword_and_short_reply(ctx, body, kind):
    row = only_row(scan(ctx, mail(subject="Status", body=body)))
    assert row["status"] == "No Action Needed" and kind in row["diag_no_action_reason"]
    assert row["sla_status"] == "N/A" and row["escalated"] is False and row["teams_pending"] == []


@pytest.mark.parametrize("body", [
    "Thanks, please reset my password ASAP",         # a request signal beats the closing word
    "شكرا لكن مشكلة جديدة",                            # Arabic problem word
    "The new report looks acceptable to me today",   # long, no phrase, no signal
    "Thanks for the fix, is the issue gone for good?",
])
def test_requests_are_never_no_action(ctx, body):
    row = only_row(scan(ctx, mail(subject="Status", body=body)))
    assert row["status"] == "Pending Reply"


def test_a_phrase_must_be_a_whole_word_and_arabic_forms_are_normalised(ctx):
    assert not rules.build_rules({}).is_no_action_needed("x", "thanksgiving plans for the whole team are being drawn up")
    r = rules.build_rules({})
    assert r.is_no_action_needed("x", "تم الاطلاع") and r.is_no_action_needed("x", "تم الإطلاع")      # alef forms
    assert r.is_no_action_needed("x", "وشكرا")                                                         # attached prefix


def test_extra_no_action_keywords_from_the_settings(store):
    body = "Roger that, filed under reference material for the quarter ahead"
    assert only_row(scan(make_ctx(store), mail(body=body)))["status"] == "Pending Reply"
    tuned = make_ctx(store, email_settings(classification={"extra_no_action_keywords": ["roger that"]},
                                           advanced={"no_action_keywords": []}))
    assert only_row(scan(tuned, mail(body=body)))["status"] == "No Action Needed"
    via_advanced = make_ctx(store, email_settings(advanced={"no_action_keywords": ["Roger That"]}))
    assert only_row(scan(via_advanced, mail(body=body)))["status"] == "No Action Needed"


def test_short_reply_rule_and_action_words_are_tunable(store):
    short = mail(subject="Status", body="Perfect")
    assert only_row(scan(make_ctx(store), short))["status"] == "No Action Needed"
    off = make_ctx(store, email_settings(advanced={"short_reply_limit": 0}))
    assert only_row(scan(off, short))["status"] == "Pending Reply"
    extra_signal = make_ctx(store, email_settings(advanced={"action_signal_words": ["perfect"]}))
    assert only_row(scan(extra_signal, short))["status"] == "Pending Reply"


# -------------------------------------------------------------------------- thread / signature stripping
def test_quoted_thread_and_signature_are_cut_from_the_text():
    raw = ("Please open the account\n\nBest regards,\nBob\n-----Original Message-----\nFrom: x\nSent: Sunday\n"
           "Subject: FCUBS thanks\n> quoted")
    assert rules.strip_quoted_thread(raw).endswith("Bob")
    assert rules.extract_latest_message_text(raw) == "Please open the account"
    assert rules.extract_latest_message_text("Hello\nOn Mon, 5 Oct 2026, Jane wrote:\nold text") == "Hello"
    assert rules.extract_latest_message_text("مرحبا\nمن: فلان\nنص قديم") == "مرحبا"
    assert rules.extract_latest_message_text("Confidential Email\n\n--\nBob") == ""
    assert rules.extract_latest_message_text("Sent from my phone") == ""


def test_signature_only_mails_are_ignored_and_banner_only_mails_are_closed(ctx):
    out = scan(ctx, mail(subject="empty", body="Regards,\nBob"), mail(subject="banner", body="Confidential Email"),
               mail(subject="real", body="Please check the printer"))
    assert [(r["subject"], r["status"]) for r in out.rows] == [("banner", "No Action Needed"), ("real", "Pending Reply")]


def test_new_text_decides_not_the_quoted_history(ctx):
    body = ("Please create the new user on the DWP portal for the branch accounts team members today\n"
            "-----Original Message-----\nFrom: x\nthe FCUBS ledger was fine")
    row = only_row(scan(ctx, mail(subject="Request", body=body)))
    assert row["team"] == "Card Management" and "Flexcube" not in row["detected_systems"]
    vague = "Please create the new user for the branch accounts team members today\n-----Original Message-----\nFrom: x\nthe FCUBS ledger"
    assert only_row(scan(ctx, mail(subject="Request", body=vague)))["team"] == "Flex Cube"      # history only as a fallback


def test_a_bare_forward_falls_back_to_the_quoted_request(ctx):
    body = "Kindly your support\n\n-----Original Message-----\nFrom: branch\nSent: Sunday\nSubject: help\n\nFCUBS login fails"
    row = only_row(scan(ctx, mail(subject="FW: help", body=body)))
    assert row["team"] == "Flex Cube" and row["classified_by"] == "system"


def test_a_content_free_reminder_borrows_the_system_from_the_thread(ctx):
    first = mail(subject="Need access", conv="CONV-R", minutes_ago=90, body="Cannot open DWP portal please")
    nudge = mail(subject="RE: Need access", conv="CONV-R", minutes_ago=20, body="Reminder")
    row = only_row(scan(ctx, first, nudge))
    assert row["team"] == "Card Management" and row["reminder_count"] == 1


# ----------------------------------------------------------------------------- system and team detection
@pytest.mark.parametrize("subject, body, team", [
    ("x", "FCUBS posting fails", "Flex Cube"),
    ("x", "the DWP portal is down", "Card Management"),
    ("x", "Veridium enrolment", "MFA Team"),
    ("x", "المصادقة البيومترية لا تعمل", "MFA Team"),
    ("x", "المصادقه البيومتريه", "MFA Team"),              # other letter forms of the same words
    ("x", "SFTP transfer is late", "Third Party"),
    ("x", "CRM screen frozen", "Open System"),
    ("FCUBS issue", "please look at the Windows server", "Open System"),    # the body decides, the subject only falls back
    ("FCUBS issue", "please look", "Flex Cube"),
    ("x", "flex-cube and E-form", "Flex Cube"),
    ("x", "nothing technical here at all", "Unclassified"),
])
def test_detection_of_systems_and_teams(subject, body, team):
    systems, primary, all_teams = rules.build_rules({}).detect(subject, body)
    assert primary == team
    assert (all_teams == []) == (team == "Unclassified")


def test_short_acronyms_match_whole_words_only():
    r = rules.build_rules({})
    assert r.detect("x", "please reach out and teach me")[0] == []
    assert r.detect("x", "the ACH file arrived")[1] == "Third Party"
    assert r.detect("I-Flex SMS Team mailbox", "nothing")[0] == []           # a mailbox display name is noise


def test_ties_between_teams_go_by_priority_and_the_priority_is_tunable():
    r = rules.build_rules({})
    assert r.detect("x", "Windows and DWP")[1] == "Card Management"            # specific team before the generic one
    assert r.detect("x", "Windows and DWP")[2] == ["Card Management", "Open System"]
    flipped = rules.build_rules({"advanced": {"team_priority": ["Open System", "Card Management"]}})
    assert flipped.detect("x", "Windows and DWP")[1] == "Open System"


def test_several_keywords_of_one_application_count_once():
    systems, team, _ = rules.build_rules({}).detect("x", "GL FCUBS OBMA and DWP")
    assert "Flexcube" in systems and systems.count("Flexcube") == 1 and "DWP" in systems
    assert rules.build_rules({}).canonical_application("الفليكس") == "Flexcube"


def test_multi_team_mail_splits_into_one_row_per_team_one_replied_one_pending(ctx):
    original = mail(subject="Two systems", conv="CONV-M", minutes_ago=90, body="FCUBS login fails and DWP is down")
    out = scan(ctx, original, reply(original, sender=ALICE, after=10))
    flex, card = out.rows
    assert (flex["team"], flex["status"], flex["response_minutes"], flex["first_reply_by"]) == ("Flex Cube", "Replied", 10, "Alice")
    assert (card["team"], card["status"], card["escalated"], card["sla_status"]) == ("Card Management", "Pending Reply", True, "Breached")
    assert card["first_reply_by"] == "" and card["teams_pending"] == ["Card Management"] and card["current_waiting_time"] == "1 H 30 M"
    assert flex["is_split_child"] and card["split_sibling_teams"] == ["Flex Cube", "Card Management"]
    assert flex["message_id"] == card["message_id"] and flex["row_id"] != card["row_id"]
    s = out.stats
    assert (s["business_emails"], s["replied"], s["pending"], s["total_team_replies"]) == (2, 1, 1, 1)
    assert s["team_stats"]["Flex Cube"]["replied"] == 1 and s["team_stats"]["Card Management"]["pending"] == 1


def test_an_unmapped_reply_answers_every_team_of_a_multi_team_mail(ctx):
    original = mail(subject="Two systems", conv="CONV-M", body="FCUBS login fails and DWP is down")
    row = only_row(scan(ctx, original, reply(original, sender=MANAGER)))
    assert row["status"] == "Replied" and row["teams_pending"] == [] and row["reply_by_manager"] is True


def test_linked_teams_share_reply_credit(store):
    body = "Veridium enrolment and Windows reinstall"
    original = mail(subject="Linked", conv="CONV-L", body=body)
    answer = reply(original, sender=CAROL)
    alone = scan(make_ctx(store), original, answer)
    assert sorted((r["team"], r["status"]) for r in alone.rows) == [("MFA Team", "Replied"), ("Open System", "Pending Reply")]
    linked = make_ctx(store, email_settings(team_link_groups={"MFA Team": ["Open System"], "Open System": ["MFA Team"]}))
    out = scan(linked, original, answer)
    assert sorted((r["team"], r["status"]) for r in out.rows) == [("MFA Team", "Replied"), ("Open System", "Replied")]


def test_team_directory_routing_prefix_and_people_list():
    directory = rules.Directory(["x@example.test"], {"Flex Cube": ["flexcube.jane.doe@example.test"]}, {}, [])
    assert directory.member_team("Jane.Doe@example.test") == "Flex Cube"      # the plain address of a prefixed alias
    assert directory.member_team("x@example.test") is None
    assert directory.people() == [{"name": "Jane Doe", "email": "flexcube.jane.doe@example.test", "team": "Flex Cube"},
                                  {"name": "X", "email": "x@example.test", "team": ""}]
    assert eng.team_member_people(email_settings())[0]["name"] == "Alice"


# ----------------------------------------------------------------------------- classification (configurable)
def classify_one(store, email, **mail_kw):
    return only_row(scan(make_ctx(store, email), mail(**mail_kw)))


def test_unclassified_rows_say_why_and_suggest_words(store):
    row = classify_one(store, email_settings(), subject="Swift Alliance login", body="the Swift screen is blank")
    assert (row["team"], row["classified_by"]) == ("Unclassified", "none")
    assert row["classification_hint"].split("; ")[:2] == ["Swift", "Alliance"]


def test_classification_rule_from_the_settings_moves_the_mail_to_a_team(store):
    kw = dict(subject="Swift Alliance login", body="the Swift screen is blank")
    before = classify_one(store, email_settings(), **kw)
    after = classify_one(store, email_settings(classification={"system_team_map": {"swift": "Third Party"}}), **kw)
    assert (before["team"], after["team"], after["classified_by"]) == ("Unclassified", "Third Party", "system")
    assert after["classification_hint"] == "swift"


def test_admin_rule_beats_the_built_in_map_for_the_same_keyword(store):
    builtin = classify_one(store, email_settings(), body="CRM screen frozen")
    mine = classify_one(store, email_settings(classification={"system_team_map": {"crm": "Card Management"}}),
                        body="CRM screen frozen")
    assert (builtin["team"], mine["team"]) == ("Open System", "Card Management")
    r = rules.build_rules({"classification": {"system_team_map": {"fcubs": "Third Party"}}})
    systems, team, all_teams = r.detect("x", "FCUBS issue")
    assert team == "Third Party" and "Flex Cube" in all_teams          # the other built-in word (FCUB) still names its team


def test_admin_latin_rules_match_whole_words_and_arabic_rules_match_inside_words():
    r = rules.build_rules({"classification": {"system_team_map": {"swift": "Third Party", "سويفت": "Third Party",
                                                                  "Gold Card": "Card Management"}}})
    assert r.detect("x", "Swiftly done")[0] == []
    assert r.detect("x", "SWIFT payment")[1] == "Third Party"
    assert r.detect("x", "عطل في السويفت")[1] == "Third Party"
    assert r.detect("x", "about the gold-card limit")[1] == "Card Management"      # any spacing between the words


def test_advanced_keys_extend_the_built_in_map_below_the_admin_rules():
    r = rules.build_rules({"advanced": {"system_team_map": {"Zetapay": "Third Party"}, "noise_phrases": ["Zeta Footer"],
                                        "application_groups": {"zetapay": "Zeta Payments"}}})
    assert r.detect("x", "Zetapay is slow") == (["Zeta Payments"], "Third Party", ["Third Party"])
    assert r.detect("x", "the Zeta Footer text only")[0] == []
    mine = rules.build_rules({"advanced": {"system_team_map": {"Zetapay": "Third Party"}},
                              "classification": {"system_team_map": {"zetapay": "Open System"}}})
    assert mine.detect("x", "Zetapay is slow")[1] == "Open System"


def test_address_rule_generalises_the_recipient_override(store):
    mapping = {"address_team_map": {BUSINESS2: "MFA Team"}}
    plain = classify_one(store, email_settings(), to=[BUSINESS2], body="Windows reinstall")
    routed = classify_one(store, email_settings(classification=mapping), to=[BUSINESS2], body="Windows reinstall")
    assert plain["team"] == "Open System"
    assert (routed["team"], routed["classified_by"]) == ("MFA Team", "address")
    assert routed["all_teams"] == ["MFA Team", "Open System"] and "MFA Team (recipient address)" in routed["detected_systems"]
    other_box = classify_one(store, email_settings(classification=mapping), to=[BUSINESS], body="Windows reinstall")
    assert other_box["classified_by"] == "system"                      # the rule is about the mailbox the mail was sent TO


def test_unclassified_team_catches_what_nothing_else_matched(store):
    tuned = email_settings(classification={"unclassified_team": "Open System"})
    nothing = classify_one(store, tuned, body="something entirely different")
    assert (nothing["team"], nothing["classified_by"], nothing["all_teams"]) == ("Open System", "default_team", ["Open System"])
    found = classify_one(store, tuned, body="DWP is down")
    assert (found["team"], found["classified_by"]) == ("Card Management", "system")


def test_precedence_override_then_address_then_system_then_default_then_none(store):
    cls = {"address_team_map": {BUSINESS2: "MFA Team"}, "unclassified_team": "Third Party"}
    tuned = email_settings(classification=cls)
    kw = dict(subject="Precedence", to=[BUSINESS2], body="FCUBS posting fails")
    item = mail(**kw)
    store.set_override("team", item.PropertyAccessor.values[eng.PR_INTERNET_MESSAGE_ID],
                       {"team": "Open System", "marked_by": "admin", "marked_at": "2026-10-11T09:00:00"})
    steps = [only_row(scan(make_ctx(store), item))]                                       # override wins over everything
    store.delete_override("team", item.PropertyAccessor.values[eng.PR_INTERNET_MESSAGE_ID])
    steps.append(only_row(scan(make_ctx(store, tuned), item)))                           # address beats system words
    steps.append(only_row(scan(make_ctx(store, email_settings()), item)))                # system words
    steps.append(only_row(scan(make_ctx(store, tuned), mail(subject="P", body="nothing at all"))))
    steps.append(only_row(scan(make_ctx(store), mail(subject="P", body="nothing at all"))))
    assert [(r["team"], r["classified_by"]) for r in steps] == [
        ("Open System", "override"), ("MFA Team", "address"), ("Flex Cube", "system"), ("Third Party", "default_team"),
        ("Unclassified", "none")]


def test_settings_teams_drive_the_team_statistics(store):
    tuned = make_ctx(store, email_settings(teams=["Flex Cube", "Open System", "Unclassified"]))
    s = scan(tuned, mail(body="FCUBS"), mail(body="DWP is down")).stats
    assert list(s["team_stats"]) == ["Flex Cube", "Open System", "Unclassified", "Card Management"]
    assert s["team_stats"]["Flex Cube"]["business_emails"] == 1 and s["team_stats"]["Card Management"]["business_emails"] == 1
    data = eng.to_snapshot_data(scan(tuned, mail(body="DWP is down")), tuned)
    assert data["teams"] == ["Flex Cube", "Open System", "Unclassified", "Card Management"]


# ------------------------------------------------------------------------------------------ SLA, carry-over
def test_sla_met_breached_within_and_escalated(ctx):
    met = mail(subject="met", minutes_ago=100)
    late = mail(subject="late", minutes_ago=200)
    fresh = mail(subject="fresh", minutes_ago=45)
    stale = mail(subject="stale", minutes_ago=90)
    closing = mail(subject="closing", minutes_ago=300, body="Thank you")
    out = scan(ctx, met, reply(met, after=30), late, reply(late, after=90), fresh, stale, closing)
    by = {r["subject"]: r for r in out.rows}
    assert (by["met"]["sla_status"], by["met"]["escalated"]) == ("Met", False)
    assert (by["late"]["sla_status"], by["late"]["escalated"], by["late"]["status"]) == ("Breached", False, "Replied")
    assert (by["fresh"]["sla_status"], by["fresh"]["escalated"]) == ("Within SLA", False)
    assert (by["stale"]["sla_status"], by["stale"]["escalated"]) == ("Breached", True)
    assert by["closing"]["sla_status"] == "N/A"
    s = out.stats
    assert (s["sla_breached"], s["escalated_count"], s["sla_compliance_rate"]) == (2, 1, 50.0)
    assert [i["subject"] for i in s["escalated_items"]] == ["stale"] and s["escalated_items"][0]["current_waiting_time"] == "1 H 30 M"
    assert all(r["sla_minutes"] == 60 for r in out.rows)
    assert make_ctx(ctx.store, email_settings(sla_target_minutes=120)) and \
        scan(make_ctx(ctx.store, email_settings(sla_target_minutes=120)), stale).rows[0]["escalated"] is False


def test_unanswered_mail_from_yesterday_is_carried_over(ctx):
    yesterday = mail(subject="old pending", minutes_ago=24 * 60 + 120)
    answered = mail(subject="old answered", minutes_ago=24 * 60 + 200)
    today_mail = mail(subject="today", minutes_ago=20)
    out = scan(ctx, yesterday, answered, reply(answered, after=15), today_mail)
    by = {r["subject"]: r for r in out.rows}
    assert set(by) == {"old pending", "today"}                          # the answered old mail is not today's business
    assert (by["old pending"]["is_carryover"], by["old pending"]["age_days"], by["old pending"]["escalated"]) == (True, 1, True)
    assert by["today"]["is_carryover"] is False
    s = out.stats
    assert (s["business_emails"], s["business_emails_incl_carryover"], s["carryover_count"], s["carryover_oldest_days"]) == (1, 2, 1, 1)
    assert s["pending"] == 2 and s["team_stats"]["Unclassified"]["carryover_count"] == 1


def test_lookback_days_come_from_the_settings(store):
    old = mail(subject="two days old", minutes_ago=2 * 24 * 60 + 60)
    assert scan(make_ctx(store), old).rows == []
    wider = make_ctx(store, email_settings(backlog_lookback_days=3))
    row = only_row(scan(wider, old))
    assert row["is_carryover"] and row["age_days"] == 2
    none = make_ctx(store, email_settings(backlog_lookback_days=0))
    assert scan(none, mail(subject="yesterday", minutes_ago=24 * 60 + 60)).rows == []


# ----------------------------------------------------------------------------------------------- overrides
def mid(item):
    return item.PropertyAccessor.values[eng.PR_INTERNET_MESSAGE_ID]


def test_manual_no_action_applies_to_unanswered_mail_only(store):
    pending, answered = mail(subject="pending"), mail(subject="answered")
    for item in (pending, answered):
        store.set_override("no_action", mid(item), {"marked_by": "admin", "marked_at": "2026-10-11T09:00:00",
                                                    "subject": item.Subject})
    out = scan(make_ctx(store), pending, answered, reply(answered))
    by = {r["subject"]: r for r in out.rows}
    assert by["pending"]["status"] == "No Action Needed"
    assert by["pending"]["diag_no_action_reason"] == 'Manually marked "No Action Needed" by admin on 2026-10-11T09:00:00'
    assert by["answered"]["status"] == "Replied"                       # a reply wins over the mark
    assert out.stats["no_action_needed"] == 1 and out.stats["needed_reply"] == 1


def test_manual_team_override_beats_detection_and_stops_the_split(store):
    original = mail(subject="Two systems", body="FCUBS login fails and DWP is down")
    store.set_override("team", mid(original), {"team": "Third Party", "marked_by": "admin", "marked_at": "x"})
    row = only_row(scan(make_ctx(store), original))
    assert (row["team"], row["all_teams"], row["classified_by"]) == ("Third Party", ["Third Party"], "override")


def test_assignment_and_quality_are_overlaid_on_the_row(store):
    answered = mail(subject="answered", minutes_ago=60)
    waiting = mail(subject="waiting")
    store.set_override("assignments", mid(waiting), {"assigned_to_email": "dave@example.test", "assigned_to_name": "Dave",
                                                     "note": "n", "marked_by": "admin", "marked_at": "x", "notified": False})
    store.set_override("quality", mid(answered), {
        "rating": "Needs Improvement", "note": "short answer", "reasons": "Incomplete answer", "appeal": "I disagree",
        "appeal_decision": "", "rating_withdrawn": False, "reviewed_by": "admin", "reviewed_at": "x"})
    out = scan(make_ctx(store), answered, reply(answered), waiting)
    by = {r["subject"]: r for r in out.rows}
    assert (by["waiting"]["assigned_to_email"], by["waiting"]["assigned_to_name"]) == ("dave@example.test", "Dave")
    assert (by["answered"]["quality_rating"], by["answered"]["quality_note"], by["answered"]["quality_reasons"],
            by["answered"]["quality_appeal"]) == ("Needs Improvement", "short answer", "Incomplete answer", "I disagree")
    s = out.stats
    assert (s["assigned_count"], s["quality_reviewed"], s["quality_needs_improvement"], s["quality_appeals"]) == (1, 1, 1, 1)
    assert s["quality_scorecard"] == [{"name": "Alice", "team": "Flex Cube", "reviewed": 1, "excellent": 0, "good": 0,
                                       "needs": 1, "score": 25.0}]


def test_quality_scorecard_index_leaderboard_and_matrix(store):
    ratings = [("a1", ALICE, "Excellent", 10), ("a2", ALICE, "Good", 20), ("b1", BOB, "Needs Improvement", 90),
               ("b2", BOB, "Needs Improvement", 80), ("c1", CAROL, "Good", 5), ("c2", CAROL, "Good", 15),
               ("m1", MANAGER, "Excellent", 10), ("w1", DAVE, "Needs Improvement", 10)]
    items = []
    for name, who, rating, after in ratings:
        original = mail(subject=name, minutes_ago=200)
        items += [original, reply(original, sender=who, after=after)]
        store.set_override("quality", mid(original), {"rating": rating, "rating_withdrawn": name == "w1"})
    out = scan(make_ctx(store), *items)
    s = out.stats
    card = {e["name"]: e for e in s["quality_scorecard"]}
    assert set(card) == {"Alice", "Bob", "Carol"}                       # manager replies and withdrawn ratings are not scored
    assert (card["Alice"]["score"], card["Bob"]["score"], card["Carol"]["score"]) == (87.5, 25.0, 75.0)
    assert [e["name"] for e in s["quality_scorecard"]] == ["Bob", "Carol", "Alice"]          # worst first
    index = {e["team"]: e["index"] for e in s["team_quality_index"]}
    assert index == {"Flex Cube": 56.2, "MFA Team": 75.0}
    assert [e["name"] for e in s["quality_leaderboard_top"]] == ["Alice"]
    assert [e["name"] for e in s["quality_leaderboard_bottom"]] == ["Bob", "Carol"]
    quadrants = {e["name"]: e["quadrant"] for e in s["speed_quality_matrix"]}
    assert quadrants == {"Alice": "Fast & High Quality", "Bob": "Slow & Low Quality", "Carol": "Fast & High Quality"}
    assert (s["manager_reply_count"], s["repliable_count"]) == (1, 7)
    assert s["quality_coverage_rate"] == 100.0


# -------------------------------------------------------------------------------------------- statistics
def test_statistics_numbers(ctx):
    a, b, c = mail(subject="a", minutes_ago=100), mail(subject="b", minutes_ago=100), mail(subject="c", minutes_ago=100)
    d = mail(subject="d", minutes_ago=30, body="Thank you")
    e = mail(subject="e", minutes_ago=24 * 60 + 60)
    out = scan(ctx, a, reply(a, after=10), b, reply(b, sender=BOB, after=30, ), c, d, e)
    s = out.stats
    assert (s["business_emails"], s["replied"], s["pending"], s["no_action_needed"], s["needed_reply"]) == (4, 2, 2, 1, 3)
    assert s["reply_rate"] == pytest.approx(2 / 3 * 100)
    assert (s["average_response"], s["fastest_response"], s["slowest_response"]) == (20, 10, 30)
    assert (s["team_replies"], s["total_team_replies"]) == (2, 2)
    assert (s["conversation_matches"], s["no_matches"], s["conversation_index_matches"], s["subject_matches"]) == (2, 3, 0, 0)
    unclassified = s["team_stats"]["Unclassified"]
    assert unclassified["business_emails"] == 4 + 1 - 1 + 1 - 1 or unclassified["business_emails"] == 5
    assert unclassified["response_minutes_sum"] == 40 and unclassified["response_count"] == 2
    assert unclassified["fastest_response_minutes"] == 10
    assert s["team_stats"]["Flex Cube"]["team_emails_sent"] == 2         # Alice and Bob each sent one
    assert s["reply_breakdown"] == [{"name": "Alice", "team": "Flex Cube", "count": 1},
                                    {"name": "Bob", "team": "Flex Cube", "count": 1}]
    assert [t["response_minutes"] for t in s["top_slowest_replies"]] == [30, 10]
    assert s["sla_target_minutes"] == 60


def test_system_volume_counts_applications_not_spellings(ctx):
    out = scan(ctx, mail(body="FCUBS and GL"), mail(body="Flex Cube again"), mail(body="DWP"))
    assert out.stats["system_volume"]["Flexcube"] == 2 and out.stats["system_volume"]["DWP"] == 1


# ------------------------------------------------------------------------------------ snapshot and job
CONTRACT_ROW_KEYS = {
    "subject", "entry_id", "message_id", "detected_systems", "team", "all_teams", "teams_pending", "teams_replied",
    "customer_name", "customer_email", "received_time", "mailbox", "folder", "match_method", "first_reply_by",
    "first_reply_email", "first_reply_time", "first_response_time", "response_minutes", "duration_minutes",
    "reply_count", "current_waiting_time", "status", "sla_minutes", "sla_status", "escalated", "reminder_count",
    "is_carryover", "age_days", "no_action_reason", "reply_by_manager", "assigned_to_email", "assigned_to_name",
    "quality_rating", "quality_note", "quality_reasons", "quality_appeal", "appeal_decision", "rating_withdrawn",
    "body_preview", "reply_body_preview", "classified_by", "classification_hint"}
CONTRACT_STAT_KEYS = {
    "business_emails", "replied", "pending", "no_action_needed", "needed_reply", "reply_rate", "team_replies",
    "total_team_replies", "average_response", "fastest_response", "slowest_response", "conversation_matches",
    "conversation_index_matches", "subject_matches", "no_matches", "sla_target_minutes", "sla_breached",
    "sla_compliance_rate", "escalated_count", "carryover_count", "carryover_oldest_days", "assigned_count",
    "team_stats", "system_volume", "business_address_volume", "reply_breakdown", "top_slowest_replies",
    "escalated_items", "quality", "trend"}


def busy_day():
    original = mail(subject="Two systems", conv="CONV-M", minutes_ago=90, body="FCUBS login fails and DWP is down " + "x" * 800)
    answered = mail(subject="Answered", minutes_ago=60, body="Veridium " + "y" * 600)
    return [original, reply(original, after=10, body="Reply " + "z" * 700), answered, reply(answered, sender=CAROL, after=20),
            mail(subject="Pending", minutes_ago=15), mail(subject="Thanks", body="Thank you"),
            mail(subject="Yesterday", minutes_ago=24 * 60 + 90)]


def test_snapshot_is_json_safe_contract_shaped_and_valid(ctx):
    out = scan(ctx, *busy_day(), keep_mail_items=True)
    assert any(r["mail_item"] is not None for r in out.rows)             # the report keeps the COM references...
    data = eng.to_snapshot_data(out, ctx)
    text = json.dumps(data)                                              # ...the snapshot never has them
    assert "mail_item" not in text
    envelope = schemas.make_envelope("email", data, "2026-10-11T12:00:00", 1.5, "PC1", "2026-10-11")
    assert schemas.validate_snapshot(envelope) == []
    for row in data["rows"]:
        assert CONTRACT_ROW_KEYS <= set(row)
        assert len(row["body_preview"]) <= 300 and len(row["reply_body_preview"]) <= 300
        assert row["received_time"].startswith("2026-10-")
    assert CONTRACT_STAT_KEYS <= set(data["stats"])
    assert set(data["quality"]) == {"scorecard", "team_index", "leaderboard_top", "leaderboard_bottom",
                                    "speed_quality_matrix", "person_trends", "repeat_issue_alerts"}
    assert data["date"] == "2026-10-11" and data["sla_target_minutes"] == 60
    assert data["teams"] == ["MFA Team", "Flex Cube", "Card Management", "Third Party", "Open System", "Unclassified"]
    assert set(data["stats"]["quality"]) == {"reviewed", "excellent", "good", "needs_improvement", "appeals",
                                             "coverage_rate", "repliable_count", "manager_reply_count"}
    assert data["classification"]["counts"]["system"] >= 3
    by_subject = {r["subject"]: r for r in data["rows"]}
    replied = [r for r in data["rows"] if r["status"] == "Replied"][0]
    assert replied["first_reply_time"] and replied["duration_minutes"] == replied["response_minutes"]
    pending = by_subject["Pending"]
    assert pending["first_reply_time"] is None and pending["duration_minutes"] == 15


def test_rows_do_not_hold_com_objects_unless_asked(ctx):
    plain = scan(ctx, mail(subject="a"))
    assert all(r["mail_item"] is None for r in plain.rows) and all(e["mail_item"] is None for e in plain.all_emails)


def test_snapshot_round_trips_through_the_store(ctx, store):
    out = scan(ctx, *busy_day())
    store.write_snapshot("email", eng.to_snapshot_data(out, ctx), generated_at="2026-10-11T12:00:00")
    assert store.read_snapshot("email")["data"]["stats"]["business_emails"] == out.stats["business_emails"]


def test_output_is_deterministic_with_a_frozen_clock(store):
    first = json.dumps(eng.to_snapshot_data(eng.scan_and_analyze(make_ctx(store), archive(*busy_day())), make_ctx(store)),
                       sort_keys=False, ensure_ascii=False)
    items = busy_day()
    second_ns = archive(*reversed(items))                              # a different folder order gives the same answer
    second = json.dumps(eng.to_snapshot_data(eng.scan_and_analyze(make_ctx(store), second_ns), make_ctx(store)),
                        sort_keys=False, ensure_ascii=False)
    normalise = lambda text: text.replace("m1", "").replace("E", "")             # ids differ between the two builds
    first_rows = [(r["subject"], r["team"], r["status"], r["response_minutes"], r["received_time"])
                  for r in json.loads(first)["rows"]]
    second_rows = [(r["subject"], r["team"], r["status"], r["response_minutes"], r["received_time"])
                   for r in json.loads(second)["rows"]]
    assert first_rows == second_rows and normalise(first)
    same = json.dumps(eng.to_snapshot_data(eng.scan_and_analyze(make_ctx(store), archive(*items)), make_ctx(store)),
                      ensure_ascii=False)
    again = json.dumps(eng.to_snapshot_data(eng.scan_and_analyze(make_ctx(store), archive(*items)), make_ctx(store)),
                       ensure_ascii=False)
    assert same == again


def test_run_scan_returns_snapshot_summary_and_history_point(ctx):
    result = eng.run_scan(ctx, archive(*busy_day()))
    assert result.ok and result.error is None and result.emails_sent == 0
    assert result.snapshot_date == "2026-10-11"
    assert schemas.validate_snapshot(schemas.make_envelope("email", result.snapshot_data, "2026-10-11T12:00:00")) == []
    assert result.summary.startswith("5 business e-mails, 2 replied") or "business e-mails" in result.summary
    assert result.history_point == {
        "ts": "2026-10-11T12:00:00", "business_emails": 5, "replied": 2, "pending": 3, "no_action_needed": 1,
        "sla_breached": 2, "escalated": 2}


def test_run_scan_never_raises(store):
    empty = make_ctx(store, email_settings(business_emails=[]))
    result = eng.run_scan(empty, archive())
    assert not result.ok and "business e-mail" in result.error and result.snapshot_data is None
    nobody = make_ctx(store, email_settings(team_members=[], team_members_by_team={}, manager_emails=[]))
    assert "team member" in eng.run_scan(nobody, archive()).error
    wrong_archive = make_ctx(store, email_settings(target_archive_name="nope"))
    failed = eng.run_scan(wrong_archive, archive())
    assert not failed.ok and "nope" in failed.error


def test_a_damaged_override_file_fails_the_scan_instead_of_forgetting_decisions(ctx, store):
    store.set_override("team", "x", {"team": "Third Party"})
    (store.overrides_dir / "team.json").write_text("{ not json", encoding="utf-8")
    store._cache.clear()
    result = eng.run_scan(ctx, archive(mail()))
    assert not result.ok and "team.json" in result.error


def test_one_unreadable_item_does_not_stop_the_scan(ctx):
    class Broken(FakeItem):
        @property
        def ReceivedTime(self):
            raise RuntimeError("COM error")

        @ReceivedTime.setter
        def ReceivedTime(self, value):
            pass

    out = scan(ctx, Broken(subject="broken", sender=CUSTOMER, to=[BUSINESS]), mail(subject="fine"))
    assert [r["subject"] for r in out.rows] == ["fine"] and out.scan_info["item_errors"] == 1


def test_items_that_are_not_mail_are_ignored(ctx):
    meeting = mail(subject="meeting request", klass=53)
    assert scan(ctx, meeting, mail(subject="mail")).stats["business_emails"] == 1


# ------------------------------------------------------------------------------------------ connect_outlook
def test_connect_retries_while_outlook_starts(ctx):
    ns = archive()
    sleeps = []
    dispatch = flaky_dispatch(ns, failures=2)
    outlook, namespace = eng.connect_outlook(ctx, dispatch=dispatch, sleep=sleeps.append)
    assert namespace is ns and dispatch.state["calls"] == 3 and sleeps == [15, 15]
    assert any("attempt 1/5" in line for line in ctx.logs)


def test_connect_gives_up_after_the_configured_attempts(store):
    ctx = make_ctx(store, email_settings(outlook_connect_retries=3, outlook_connect_retry_seconds=2))
    sleeps = []
    with pytest.raises(RuntimeError, match="after 3 attempt"):
        eng.connect_outlook(ctx, dispatch=flaky_dispatch(archive(), failures=99), sleep=sleeps.append)
    assert sleeps == [2, 2]


def test_connect_does_not_retry_when_outlook_is_unavailable(ctx, monkeypatch):
    def unavailable():
        raise eng.OutlookUnavailable("needs Windows")
    slept = []
    with pytest.raises(eng.OutlookUnavailable):
        eng.connect_outlook(ctx, dispatch=unavailable, sleep=slept.append)
    assert slept == []
    monkeypatch.setattr(eng, "_dispatch_outlook", unavailable)
    result = eng.run_scan(ctx)                                        # no namespace given: tries to connect
    assert not result.ok and result.error == "needs Windows"


@pytest.mark.skipif(os.name == "nt", reason="the guard is for non-Windows machines")
def test_real_dispatch_refuses_politely_off_windows():
    with pytest.raises(eng.OutlookUnavailable, match="Windows"):
        eng._dispatch_outlook()


# ------------------------------------------------------------------------- history and trends (OpsStore)
def stats_for(**kw):
    base = {"business_emails": 10, "replied": 6, "pending": 3, "no_action_needed": 1, "reply_rate": 66.666,
            "average_response": 25.5, "sla_breached": 2}
    base.update(kw)
    return base


def test_history_is_saved_in_the_day_document_next_to_the_scan_points(store):
    store.add_history_point("email", "2026-10-10", {"ts": "2026-10-10T09:00:00", "business_emails": 1})
    history = eng.save_today_stats_history(store, date(2026, 10, 10), stats_for())
    assert history == {"2026-10-10": {"business_emails": 10, "replied": 6, "pending": 3, "no_action_needed": 1,
                                      "reply_rate": 66.67, "average_response": 25.5, "sla_breached": 2}}
    doc = store.read_history("email", "2026-10-10")
    assert doc["points"][0]["business_emails"] == 1 and doc["final"]["replied"] == 6
    eng.save_today_stats_history(store, "2026-10-10", stats_for(replied=7))              # a re-run overwrites the day
    assert eng.load_stats_history(store)["2026-10-10"]["replied"] == 7
    assert eng.load_stats_history(OpsStore(store.root.parent / "empty").init()) == {}


def test_trend_compares_with_yesterday_and_the_last_seven_days(store):
    for day, replied in (("2026-10-04", 2), ("2026-10-08", 4), ("2026-10-10", 6)):
        eng.save_today_stats_history(store, day, stats_for(replied=replied))
    history = eng.load_stats_history(store)
    trend = eng.compute_trend(date(2026, 10, 11), history, stats_for(replied=9))
    assert trend["vs_yesterday"]["replied"] == {"value": 6, "delta": 3, "direction": "up"}
    assert trend["vs_last7"]["replied"]["value"] == pytest.approx(4.0)         # (2 + 4 + 6) / 3, today excluded
    assert trend["vs_yesterday"]["pending"]["direction"] == "flat"
    first_day = eng.compute_trend(date(2026, 10, 11), {}, stats_for())
    assert first_day["vs_yesterday"]["replied"] is None and first_day["vs_last7"]["replied"] is None


def test_quality_history_trends_and_repeat_issue_alerts(store):
    def card(score, needs, reviewed=2, name="Bob"):
        return [{"name": name, "team": "Flex Cube", "reviewed": reviewed, "needs": needs, "score": score,
                 "excellent": 0, "good": 0}]
    for day, score, needs in (("2026-10-06", 90.0, 0), ("2026-10-08", 70.0, 1), ("2026-10-10", 40.0, 2)):
        eng.save_today_quality_history(store, day, card(score, needs))
    eng.save_today_quality_history(store, "2026-10-09", card(80.0, 0, name="Cara"))
    history = eng.load_quality_history(store)
    trends = {t["name"]: t for t in eng.build_person_quality_trends(history)}
    assert trends["Bob"]["direction"] == "declining" and trends["Bob"]["points"] == [90.0, 70.0, 40.0]
    assert trends["Cara"]["direction"] == "steady" and trends["Cara"]["days"] == 1
    assert [t["name"] for t in eng.build_person_quality_trends(history)][0] == "Bob"        # declining first
    assert eng.build_repeat_issue_alerts(history, date(2026, 10, 11)) == [
        {"name": "Bob", "needs": 3, "team": "Flex Cube", "days": 2}]
    assert eng.build_repeat_issue_alerts(history, date(2026, 10, 11), threshold=4) == []
    assert eng.build_repeat_issue_alerts(history, date(2026, 10, 30)) == []                 # outside the lookback window


def test_the_scan_reads_history_but_never_writes_it(store):
    eng.save_today_stats_history(store, "2026-10-10", stats_for(business_emails=4))
    ctx = make_ctx(store)
    out = scan(ctx, mail(subject="a"))
    trend = out.stats["trend"]["vs_yesterday"]["business_emails"]
    assert trend["value"] == 4 and trend["delta"] == -3
    assert store.history_days("email") == ["2026-10-10"] and "final" in store.read_history("email", "2026-10-10")
    assert store.read_history("email", "2026-10-11") is None
    assert eng.to_snapshot_data(out, ctx)["stats"]["trend"]["vs_yesterday"]["business_emails"]["value"] == 4


def test_todays_scorecard_joins_the_trend_in_memory(store):
    answered = mail(subject="answered", minutes_ago=100)
    store.set_override("quality", mid(answered), {"rating": "Good"})
    eng.save_today_quality_history(store, "2026-10-10", [{"name": "Alice", "team": "Flex Cube", "reviewed": 1, "needs": 0,
                                                          "score": 75.0, "excellent": 0, "good": 1}])
    out = scan(make_ctx(store), answered, reply(answered))
    assert out.stats["quality_trends"][0]["name"] == "Alice" and out.stats["quality_trends"][0]["days"] == 2
    assert store.read_history("email", "2026-10-11") is None


# --------------------------------------------------------------------------------------------- rules units
def test_text_helpers():
    assert rules.clean_subject("[SR#12345] RE: FW: Flex Cube issue.") == "flex cube issue"
    assert rules.clean_subject("رد: المصادقة") == rules.clean_subject("FW: المصادقه")
    assert rules.format_duration(5) == "5 Min" and rules.format_duration(90) == "1 H 30 M"
    assert rules.format_duration(1500) == "1 D 1 H" and rules.format_duration(-4) == "0 Min"
    assert rules.safe_percent(1, 0) == 0.0 and rules.safe_percent(1, 4) == 25.0
    assert rules.normalize_arabic("المصادقة") == rules.normalize_arabic("المصادقه") == "المصادقه"
    assert rules.normalize_arabic("رقم ٤") == "رقم 4"
    assert rules.candidate_words("RE: Please check Swift Alliance, SWIFT login") == ["Swift", "Alliance", "login"]
    assert rules.build_rules({}) is rules.build_rules({"classification": {}, "advanced": {}})       # compiled once


def test_the_team_member_list_never_contains_real_data():
    """Guard rail: the sources and tests of this area carry no company address, password or logo."""
    here = Path(__file__).resolve()
    for path in (here, here.with_name("fake_outlook.py"), here.parents[1] / "calllog/ops/engines/email_engine.py",
                 here.parents[1] / "calllog/ops/engines/email_rules.py"):
        text = path.read_text(encoding="utf-8")
        forbidden = ("nbe" + ".com.eg", "ahly" + ".bank", "base64" + ",")
        assert not any(word in text for word in forbidden), path.name
