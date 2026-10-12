"""E-mail report job: Excel, charts, HTML e-mails, .msg zips, notices, idempotence.

Runs the real scan against the fake Outlook (tests/fake_outlook.py) with a frozen clock (Sunday 2026-10-11 17:00), the
recording FakeMailer and a throw-away OpsStore. Fake addresses only (example.test). No Outlook, no Windows, no network.
"""
import itertools
import os
import re
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from calllog.ops import schemas, settings
from calllog.ops.engines import email_engine as eng
from calllog.ops.engines import email_report as er
from calllog.ops.engines.common import FakeMailer, JobContext, MailError
from calllog.ops.store import OpsStore
from tests.fake_outlook import FakeFolder, FakeItem, FakeNamespace

NOW = datetime(2026, 10, 11, 17, 0, 0)
BUSINESS, BOSS, CC = "support@example.test", "boss@example.test", "lead@example.test"
ALICE, BOB, CAROL, MANAGER, CUSTOMER = ("alice@example.test", "bob@example.test", "carol@example.test",
                                        "manager@example.test", "cust@example.test")
FLEX_TEAM, CARD_TEAM = "flex@example.test", "card@example.test"
_ids = itertools.count(1)


# --------------------------------------------------------------------------------------------------- builders
class SavingItem(FakeItem):
    """A fake mail that can do what an Outlook MailItem does: SaveAs(path, olMSG)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.saved = []

    def SaveAs(self, path, fmt):
        self.saved.append((path, fmt))
        Path(path).write_bytes(b"fake msg of " + self.Subject.encode("utf-8", "replace"))


class CheckingMailer(FakeMailer):
    """Proves at send time that attachments and inline images exist (the run deletes its temp folder afterwards) and
    records what is inside every zip."""

    def send(self, to, cc=(), subject="", html_body="", attachments=(), inline_images=None, display_only=None, bcc=()):
        for path in list(attachments) + list((inline_images or {}).values()):
            assert os.path.exists(path), f"missing at send time: {path}"
        zips = {}
        for path in attachments:
            if str(path).endswith(".zip"):
                with zipfile.ZipFile(path) as archive:
                    assert archive.testzip() is None
                    zips[Path(path).name] = archive.namelist()
        super().send(to, cc, subject, html_body, attachments, inline_images, display_only, bcc)
        self.sent[-1]["zips"] = zips


class FailingMailer(FakeMailer):
    """Raises for every subject that contains one of ``words`` (a team's bad recipient list, a COM hiccup)."""

    def __init__(self, *words):
        super().__init__()
        self.words = words

    def send(self, to, cc=(), subject="", html_body="", attachments=(), inline_images=None, display_only=None, bcc=()):
        if any(w in subject for w in self.words):
            raise MailError("simulated failure")
        super().send(to, cc, subject, html_body, attachments, inline_images, display_only, bcc)


def email_settings(report=None, **over):
    raw = {"email": {
        "business_emails": [BUSINESS], "team_members": [MANAGER],
        "team_members_by_team": {"Flex Cube": [ALICE, BOB], "MFA Team": [CAROL]},
        "manager_emails": [MANAGER], "target_archive_name": "2026_Part3", "sla_target_minutes": 60,
        "report": {"to": [BOSS], "send_times": ["16:30"], **(report or {})}}}
    clean, errors = settings.validate(settings.deep_merge(raw, {"email": over}))
    assert errors == [], errors
    return clean["email"]


def make_ctx(store, mailer=None, now=NOW, options=None, email=None, **flags):
    logs = []
    ctx = JobContext(job="email_report", settings=email or email_settings(), general={}, store=store, now=lambda: now,
                     log=logs.append, mailer=mailer if mailer is not None else FakeMailer(), options=dict(options or {}),
                     **flags)
    ctx.logs = logs
    return ctx


def mail(subject="Status", sender=CUSTOMER, to=(BUSINESS,), minutes_ago=30, body="Please check the problem", cls=FakeItem,
         **kw):
    n = next(_ids)
    item = cls(subject=subject, sender=sender, to=to, received=NOW - timedelta(minutes=minutes_ago), body=body,
               conversation_id=f"C{n}", message_id=f"<m{n}@example.test>", entry_id=f"E{n}", **kw)
    item.mid = f"<m{n}@example.test>"
    return item


def reply(original, sender=ALICE, after=10):
    n = next(_ids)
    return FakeItem(subject="RE: " + original.Subject, sender=sender, to=[original.SenderEmailAddress],
                    cc=[BUSINESS], received=original.ReceivedTime + timedelta(minutes=after), body="Done, fixed",
                    conversation_id=original.ConversationID, message_id=f"<r{n}@example.test>", entry_id=f"RE{n}")


def archive(*items):
    return FakeNamespace([FakeFolder("2026_Part3", children=[FakeFolder("Inbox", items)])])


def busy_day(cls=FakeItem):
    """Alice answers one Flex Cube mail; one Flex Cube and one MFA mail wait; one mail is a thank-you."""
    answered = mail(subject="Login fails", body="FCUBS login fails", minutes_ago=150, cls=cls)
    flex = mail(subject="Flex pending", body="flexcube is down", minutes_ago=120, cls=cls)
    mfa = mail(subject="Token pending", body="Veridium token problem", minutes_ago=100, cls=cls)
    thanks = mail(subject="Thanks", body="Thank you", minutes_ago=50, cls=cls)
    return [answered, reply(answered), flex, mfa, thanks], {"answered": answered, "flex": flex, "mfa": mfa}


@pytest.fixture
def store(tmp_path):
    return OpsStore(tmp_path / "data").init()


def run(ctx, *items, **kw):
    return er.run_report(ctx, archive(*items), **kw)


def sent_to(mailer, address):
    return [m for m in mailer.sent if address in m["to"]]


def subjects(mailer):
    return [m["subject"] for m in mailer.sent]


def report_path(store, day="2026-10-11"):
    return store.report_dir("email") / f"Team_Reply_Analytics_{day}.xlsx"


# ---------------------------------------------------------------------------------------------------- Excel
def test_workbook_has_the_expected_sheets_and_numbers(store):
    items, _ = busy_day()
    result = run(make_ctx(store), *items)
    assert result.ok, result.error
    wb = load_workbook(report_path(store))
    assert wb.sheetnames == ["Dashboard", "Detailed Report", "Team Replies", "Team Analysis"]

    dash = wb["Dashboard"]
    kpis = {dash.cell(r, c).value: dash.cell(r + 1, c).value for r in (4, 7, 10, 13) for c in (1, 4, 7)}
    assert kpis["Business Emails"] == 4 and kpis["Replied"] == 1 and kpis["Pending"] == 2
    assert kpis["No Action Needed"] == 1 and kpis["Reply Rate"] == "33.3%" and kpis["Team Replies"] == 1
    assert kpis["Fastest Response"] == "10 Min" and kpis["SLA Breached (>60m)"] == 2
    assert dash["B2"].value == "11/10/2026"
    assert len(dash._charts) == 2                                      # the native pie and bar chart
    team_rows = {dash.cell(r, 1).value: [dash.cell(r, c).value for c in range(2, 6)] for r in range(41, 48)}
    assert team_rows["Flex Cube"] == [2, 1, 1, 0] and team_rows["MFA Team"] == [1, 0, 1, 0]
    assert (dash["K3"].value, dash["L3"].value) == ("Alice", 1)

    detail = wb["Detailed Report"]
    assert [c.value for c in detail[1]] == er.DETAIL_HEADERS
    assert detail.max_row == 5
    by_subject = {detail.cell(r, 1).value: r for r in range(2, 6)}
    flex_row = by_subject["Flex pending"]
    assert detail.cell(flex_row, 3).value == "Flex Cube" and detail.cell(flex_row, 21).value == "Pending Reply"
    assert detail.cell(flex_row, 24).value == "Yes" and detail.cell(flex_row, 27).value == "Flex Cube"
    assert detail.cell(flex_row, 6).number_format == er.DATE_FORMAT
    assert detail.freeze_panes == "A2" and detail.auto_filter.ref
    assert sum(len(cf.rules) for cf in detail.conditional_formatting) == 4   # 3 status colours + escalation font

    assert [c.value for c in wb["Team Replies"][2]][:2] == ["Alice", ALICE]
    assert [c.value for c in wb["Team Analysis"][2]] == ["Alice", 1, "10 Min", "10 Min", "10 Min"]


def test_hostile_text_cannot_become_a_formula_or_break_the_workbook(store):
    items = [mail(subject='=HYPERLINK("http://evil.test","x")', body="flexcube down"),
             mail(subject="bell\x07 and\x00 nul", body="flexcube down"),
             mail(subject="مشكلة فلكس", body="flexcube down")]
    assert run(make_ctx(store), *items).ok
    detail = load_workbook(report_path(store))["Detailed Report"]
    texts = {detail.cell(r, 1).value: detail.cell(r, 1) for r in range(2, detail.max_row + 1)}
    formula = texts['=HYPERLINK("http://evil.test","x")']
    assert formula.data_type == "s"                                    # stored as text, never evaluated
    assert "bell and nul" in texts or any("bell" in t and "\x07" not in t for t in texts)
    assert "مشكلة فلكس" in texts


def test_old_workbooks_are_pruned_and_other_files_stay(store):
    folder = store.report_dir("email")
    for name in ("Team_Reply_Analytics_2026-07-01.xlsx", "Team_Reply_Analytics_2026-09-20.xlsx",
                 "Team_Reply_Analytics_2026-06-30_101500.xlsx", "notes.txt", "Other_2026-01-01.xlsx"):
        (folder / name).write_bytes(b"x")
    run(make_ctx(store), *busy_day()[0])
    names = {p.name for p in folder.iterdir()}
    assert names == {"Team_Reply_Analytics_2026-09-20.xlsx", "Team_Reply_Analytics_2026-10-11.xlsx", "notes.txt",
                     "Other_2026-01-01.xlsx"}
    assert er.prune_reports(folder, date(2026, 10, 11)) == 0


def test_a_workbook_open_in_excel_is_not_overwritten(store, monkeypatch):
    real, calls = os.replace, []

    def locked_once(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError("the file is open in Excel")
        return real(src, dst)
    monkeypatch.setattr(er.os, "replace", locked_once)
    result = run(make_ctx(store), *busy_day()[0])
    assert result.ok
    names = sorted(p.name for p in store.report_dir("email").iterdir())
    assert len(names) == 1 and re.match(r"Team_Reply_Analytics_2026-10-11_\d{6}\.xlsx$", names[0])   # no .tmp left behind


# ------------------------------------------------------------------------------------------------- main mail
def test_main_report_is_sent_with_workbook_logo_and_charts(store, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    mailer = CheckingMailer()
    ctx = make_ctx(store, mailer, email=email_settings(report={"cc": [CC]}))
    result = run(ctx, *busy_day()[0])
    main = mailer.sent[0]
    assert main["to"] == [BOSS] and main["cc"] == [CC]
    assert main["subject"] == "Daily Team Reply Analytics - 11/10/2026"
    assert main["attachments"] == [str(report_path(store))]
    assert set(main["inline_images"]) == {"nbe_logo", "reply_status_chart", "team_chart"}
    assert "cid:reply_status_chart" in main["html"] and "cid:team_chart" in main["html"] and "cid:nbe_logo" in main["html"]
    assert result.ok and result.summary.startswith("report sent: 4 mails, 2 pending")
    assert result.emails_sent == len(mailer.sent)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("nbe-email-report-")]    # temp folder deleted


def test_main_report_html_has_the_kpis_and_escapes_hostile_text(store):
    hostile = mail(subject='<script>alert(1)</script> & "quotes"', sender="evil@example.test",
                   sender_name='"><img src=x onerror=alert(2)>', body="flexcube is down", minutes_ago=300)
    ctx = make_ctx(store)
    outcome = eng.scan_and_analyze(ctx, archive(hostile, *busy_day()[0]))
    html = er.build_email_html(outcome.stats, outcome.today, include_charts=True)
    for label, value in (("Business Emails", 4), ("Replied", 1), ("Pending", 3), ("Reply Rate", "25.0%"),
                         ("Avg First Response", "10 Min"), ("SLA Breached (&gt;60m)", 3), ("Escalated (Pending)", 3)):
        assert re.search(rf"{re.escape(label)}</div><div[^>]*>{re.escape(str(value))}</div>", html), label
    assert "<script" not in html.lower() and "<img src=x" not in html and "onerror=alert(2)>" not in html.replace("&gt;", "")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "&amp; &quot;quotes&quot;" in html
    for forbidden in ("<svg", "display:flex", "display:grid", "<style", "linear-gradient", "<script"):
        assert forbidden not in html.lower()
    assert "Escalated - SLA breached" in html and "Top Slowest First Replies" in html
    assert "Performance by Team" in html and "Systems" in html and "Reply Quality" in html and "11/10/2026" in html


def test_trend_arrows_compare_with_yesterday(store):
    eng.save_today_stats_history(store, "2026-10-10", {"business_emails": 1, "replied": 3, "pending": 5,
                                                       "no_action_needed": 0, "reply_rate": 80, "average_response": 30,
                                                       "sla_breached": 0})
    mailer = FakeMailer()
    run(make_ctx(store, mailer), *busy_day()[0])
    html = mailer.sent[0]["html"]
    assert "&#9650; 2 vs yesterday" in html                       # business e-mails 1 -> 3
    assert "&#9660; 3 vs yesterday" in html                       # pending 5 -> 2 (down is good)
    assert f'color:{er.GREEN};margin-top:3px;">&#9660; 3 vs yesterday' in html
    assert f'color:{er.RED};margin-top:3px;">&#9660; 2 vs yesterday' in html                 # replied 3 -> 1
    assert er._trend_badge(None) == "" and er._trend_badge({"direction": "weird", "delta": 1}) == ""


def test_charts_fall_back_to_table_bars_without_matplotlib(store, monkeypatch):
    monkeypatch.setitem(sys.modules, "matplotlib", None)           # "import matplotlib" now raises ImportError
    with pytest.raises(ImportError):
        er.render_charts({}, date(2026, 10, 11))
    mailer = FakeMailer()
    ctx = make_ctx(store, mailer)
    result = run(ctx, *busy_day()[0])
    assert result.ok, result.error
    main = mailer.sent[0]
    assert set(main["inline_images"]) == {"nbe_logo"} and "cid:reply_status_chart" not in main["html"]
    assert "Reply Status" in main["html"] and f'bgcolor="{er.GREEN}"' in main["html"]       # CSS-table bars
    assert any("matplotlib is not installed" in line for line in ctx.logs)


def test_charts_are_png_images_and_survive_odd_names():
    stats = {"replied": 3, "pending": 1, "no_action_needed": 0,
             "first_reply_by": Counter({"$\\alpha$ <b>x</b>": 2, "Same label": 1, "Same label ": 1})}
    charts = er.render_charts(stats, date(2026, 10, 11))
    assert set(charts) == {"reply_status_chart", "team_chart"}
    assert all(png.startswith(b"\x89PNG") and len(png) > 1000 for png in charts.values())
    assert er.render_charts({"replied": 0, "pending": 0, "no_action_needed": 0, "first_reply_by": Counter()},
                            date(2026, 10, 11))["team_chart"].startswith(b"\x89PNG")


def test_a_chart_that_cannot_be_drawn_is_not_a_failure(store, monkeypatch):
    def boom(stats, today):
        raise RuntimeError("no display")
    monkeypatch.setattr(er, "render_charts", boom)
    mailer = FakeMailer()
    result = run(make_ctx(store, mailer), *busy_day()[0])
    assert result.ok and "cid:team_chart" not in mailer.sent[0]["html"]


# --------------------------------------------------------------------------------------- per-team routing
def team_settings(**report):
    return email_settings(report={"team_recipients": {"Flex Cube": {"to": [FLEX_TEAM], "cc": [CC]},
                                                      "Card Management": {"to": [CARD_TEAM]}}, **report})


def test_a_team_gets_its_own_followup_only_when_it_has_pending_mail(store):
    mailer = FakeMailer()
    items, _ = busy_day()
    result = run(make_ctx(store, mailer, email=team_settings()), *items)
    assert result.ok
    assert sent_to(mailer, CARD_TEAM) == []                        # Card Management has recipients but nothing pending
    flex = sent_to(mailer, FLEX_TEAM)
    assert len(flex) == 1 and flex[0]["cc"] == [CC]
    assert flex[0]["subject"] == "Daily Team Reply Analytics - 11/10/2026 - Pending: Flex Cube"
    assert "Flex pending" in flex[0]["html"] and "Token pending" not in flex[0]["html"]
    assert flex[0]["attachments"] == []                            # fake items cannot be saved as .msg
    # MFA has a pending mail but no recipients of its own and no zip: the general inbox is not spammed
    assert not [s for s in subjects(mailer) if "Pending: MFA Team" in s]


def test_a_team_without_recipients_falls_back_to_the_report_recipients_when_a_zip_exists(store, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    mailer = CheckingMailer()
    items, named = busy_day(cls=SavingItem)
    result = run(make_ctx(store, mailer, email=team_settings()), *items)
    assert result.ok, result.error
    mfa = [m for m in mailer.sent if "Pending: MFA Team" in m["subject"]]
    assert len(mfa) == 1 and mfa[0]["to"] == [BOSS]                # the old fallback to REPORT_TO
    (zip_name, members), = mfa[0]["zips"].items()
    assert zip_name == "MFA_Team_Pending.zip" and len(members) == 1 and members[0].endswith(".msg")
    assert "1</b> unanswered e-mail(s) for <b>MFA Team</b> are attached" in mfa[0]["html"]
    flex = sent_to(mailer, FLEX_TEAM)[0]
    assert list(flex["zips"]) == ["Flex_Cube_Pending.zip"]
    assert [fmt for item in (named["flex"], named["mfa"]) for _, fmt in item.saved] == [3, 3]    # olMSG
    assert "2</b> unanswered e-mail(s) are attached" in mailer.sent[0]["html"]


def test_a_big_team_list_is_split_into_numbered_parts(store, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    mailer = CheckingMailer()
    items = [mail(subject=f"Flex {i}", body="flexcube is down", minutes_ago=100 + i, cls=SavingItem) for i in range(5)]
    items.append(mail(subject="x", body="flexcube is down", cls=SavingItem))
    result = run(make_ctx(store, mailer, email=team_settings(max_pending_attachments=2)), *items)
    assert result.ok, result.error
    parts = [m for m in sent_to(mailer, FLEX_TEAM)]
    assert [p["subject"].rsplit(" - ", 2)[-2:] for p in parts] == [["Pending: Flex Cube", f"Part {n} of 3"] for n in (1, 2, 3)]
    assert [len(next(iter(p["zips"].values()))) for p in parts] == [2, 2, 2]
    assert [list(p["zips"]) for p in parts] == [["Flex_Cube_Pending_Part1of3.zip"], ["Flex_Cube_Pending_Part2of3.zip"],
                                                ["Flex_Cube_Pending_Part3of3.zip"]]


def test_msg_names_are_safe_and_the_step_skips_cleanly_without_outlook_items(store, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    hostile = mail(subject='../../evil: a/b*c?"d<e>f|', sender_name="..\\..\\CON", body="flexcube is down", cls=SavingItem)
    mailer = CheckingMailer()
    assert run(make_ctx(store, mailer, email=team_settings()), hostile).ok
    (names,) = [z for m in mailer.sent for z in m["zips"].values()]
    assert len(names) == 1 and re.match(r"^001_[^\\/:*?\"<>|]+\.msg$", names[0]) and ".." not in names[0]
    # plain fake items have no SaveAs: one log line, no failure, the follow-up still goes out without a zip
    plain, mailer = [mail(subject="plain", body="flexcube is down")], FakeMailer()
    ctx = make_ctx(store, mailer, email=team_settings(), options={"force": True})
    result = run(ctx, *plain)
    assert result.ok and not result.error
    assert sum("No Outlook items are available" in line for line in ctx.logs) == 1
    assert sent_to(mailer, FLEX_TEAM) and sent_to(mailer, FLEX_TEAM)[0]["attachments"] == []


def test_the_msg_step_can_be_switched_off(store):
    items, named = busy_day(cls=SavingItem)
    mailer = FakeMailer()
    assert run(make_ctx(store, mailer, email=team_settings(attach_pending_msg=False)), *items).ok
    assert named["flex"].saved == [] and sent_to(mailer, FLEX_TEAM)[0]["attachments"] == []


def test_safe_names_and_chunks():
    assert er.safe_name('a/b\\c:d*e?"f<g>h|i\r\nj') == "a_b_c_d_e__f_g_h_i__j"
    assert er.safe_name("  ..  ") == "email" and er.safe_name("", 10, "Unclassified") == "Unclassified"
    assert er.safe_name("CON") == "_CON" and er.safe_name("nul.txt") == "_nul.txt"
    assert er.safe_name("x" * 100, 25) == "x" * 25 and er.safe_name("مشكلة") == "مشكلة"
    assert er.chunk_list([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]] and er.chunk_list([1], 0) == [[1]]
    assert er.one_line("a\r\nb\n c") == "a b c"


def test_pending_rows_are_listed_under_every_team_that_still_owes_a_reply():
    rows = [{"status": "Pending Reply", "teams_pending": ["A", "B"], "subject": "1"},
            {"status": "Replied", "teams_pending": ["B"], "subject": "2"},
            {"status": "No Action Needed", "teams_pending": ["A"], "subject": "3"},
            {"status": "Replied", "teams_pending": [], "subject": "4"}]
    by_team = er.pending_rows_by_team(rows)
    assert {t: [r["subject"] for r in v] for t, v in by_team.items()} == {"A": ["1"], "B": ["1", "2"]}


def test_a_long_team_list_is_capped_and_says_how_many_more():
    rows = [{"subject": f"s{i}", "customer_name": "c", "received_time": NOW} for i in range(65)]
    html = er.build_team_pending_email_html(NOW.date(), "Flex <b>", 0, 1, 1, rows)
    assert "s59" in html and "s60" not in html and "and 5 more" in html and "Flex &lt;b&gt;" in html


# ------------------------------------------------------------------------------------------- assignments
def assign(store, item, to=ALICE, name="Alice", note="please look", by="Boss", **extra):
    store.set_override("assignments", item.mid, {"assigned_to_email": to, "assigned_to_name": name, "note": note,
                                                 "marked_by": by, "marked_at": "2026-10-11T16:00:00",
                                                 "subject": item.Subject, "notified": False, **extra})


def test_assignment_notice_is_sent_once_and_flagged_notified(store):
    items, named = busy_day()
    assign(store, named["flex"], name="Alice <b>", note="urgent <i>now</i>", by="Boss <script>")
    mailer = FakeMailer()
    ctx = make_ctx(store, mailer, email=email_settings(assignment_notifications={"cc": [CC]}))
    result = run(ctx, *items)
    notice, = sent_to(mailer, ALICE)
    assert notice["subject"] == "Action Required - Emails Assigned to You - 1 email(s) - 11/10/2026" and notice["cc"] == [CC]
    assert "Flex pending" in notice["html"] and "Pending Reply" in notice["html"] and "flexcube" in notice["html"].lower()
    assert "<script" not in notice["html"] and "&lt;script&gt;" in notice["html"] and "urgent &lt;i&gt;now&lt;/i&gt;" in notice["html"]
    assert store.overrides("assignments")[named["flex"].mid]["notified"] is True
    assert "1 assignment notice(s)" in result.summary

    before = len(mailer.sent)
    run(make_ctx(store, mailer, options={"force": True}), *items)         # even a forced re-run never repeats a notice
    assert len(sent_to(mailer, ALICE)) == 1 and len(mailer.sent) == before + 2        # report + end-of-day only


def test_one_mail_per_person_lists_all_their_new_assignments(store):
    items, named = busy_day()
    assign(store, named["flex"], note="first")
    assign(store, named["mfa"], note="second")
    assign(store, named["answered"], to=BOB, name="Bob", note="")
    mailer = FakeMailer()
    run(make_ctx(store, mailer), *items)
    alice, bob = sent_to(mailer, ALICE), sent_to(mailer, BOB)
    assert len(alice) == 1 and "2 email(s)" in alice[0]["subject"] and "first" in alice[0]["html"] and "second" in alice[0]["html"]
    assert len(bob) == 1 and "1 email(s)" in bob[0]["subject"]
    assert all(a["notified"] for a in store.overrides("assignments").values())


def test_a_failed_notice_is_retried_and_does_not_flag_the_assignment(store):
    items, named = busy_day()
    assign(store, named["flex"])
    result = run(make_ctx(store, FailingMailer("Assigned to You")), *items)
    assert not result.ok and "assignment notice" in result.error
    assert store.overrides("assignments")[named["flex"].mid]["notified"] is False
    mailer = FakeMailer()
    run(make_ctx(store, mailer, options={"force": True}), *items)
    assert len(sent_to(mailer, ALICE)) == 1 and store.overrides("assignments")[named["flex"].mid]["notified"] is True


def test_the_notified_flag_is_not_set_on_an_assignment_changed_meanwhile(store):
    item = SimpleNamespace(mid="<x@example.test>")
    assign(store, item, to=ALICE, note="old")
    sent_info = dict(store.overrides("assignments")["<x@example.test>"])
    assign(store, item, to=BOB, name="Bob", note="old")                  # re-assigned after we read it
    run_stub = SimpleNamespace(ctx=SimpleNamespace(store=store), errors=[], log=lambda text: None)
    er._mark_notified(run_stub, "<x@example.test>", sent_info)
    assert store.overrides("assignments")["<x@example.test>"]["notified"] is False
    er._mark_notified(run_stub, "<x@example.test>", dict(store.overrides("assignments")["<x@example.test>"]))
    assert store.overrides("assignments")["<x@example.test>"]["notified"] is True
    er._mark_notified(run_stub, "<gone@example.test>", sent_info)         # an assignment deleted meanwhile is not re-created
    assert "<gone@example.test>" not in store.overrides("assignments") and run_stub.errors == []


def test_assignment_notices_have_their_own_switch(store):
    items, named = busy_day()
    assign(store, named["flex"])
    mailer = FakeMailer()
    run(make_ctx(store, mailer, email=email_settings(assignment_notifications={"send": False})), *items)
    assert sent_to(mailer, ALICE) == [] and store.overrides("assignments")[named["flex"].mid]["notified"] is False


# ---------------------------------------------------------------------------------------- quality feedback
def rate(store, item, rating="Needs Improvement", at="2026-10-11T16:10:00", **extra):
    store.set_override("quality", item.mid, {"rating": rating, "note": "too brief <b>", "reasons": "Incomplete answer",
                                             "replied_by": "Alice", "replied_by_email": ALICE, "reviewed_by": "Boss",
                                             "reviewed_at": at, "subject": item.Subject, **extra})


def test_quality_feedback_goes_to_the_replier_once_per_review(store):
    items, named = busy_day()
    rate(store, named["answered"])
    mailer = FakeMailer()
    ctx = make_ctx(store, mailer, email=email_settings(quality_feedback={"cc": [CC]}))
    result = run(ctx, *items)
    fb, = sent_to(mailer, ALICE)
    assert fb["subject"] == "Reply Quality Feedback - 1 reply review(s) - 11/10/2026" and fb["cc"] == [CC]
    assert "Needs Improvement" in fb["html"] and "Incomplete answer" in fb["html"] and "too brief &lt;b&gt;" in fb["html"]
    assert store.read_state("email_quality_sent")[named["answered"].mid]["reviewed_at"] == "2026-10-11T16:10:00"
    assert "1 quality feedback" in result.summary

    mailer.sent.clear()
    run(make_ctx(store, mailer, options={"force": True}), *items)
    assert sent_to(mailer, ALICE) == []                                  # same review: never again
    rate(store, named["answered"], "Excellent", at="2026-10-11T17:30:00")   # a new rating is new feedback
    run(make_ctx(store, mailer, options={"force": True}), *items)
    assert len(sent_to(mailer, ALICE)) == 1 and "Excellent" in sent_to(mailer, ALICE)[0]["html"]


def test_withdrawn_legacy_flagged_and_switched_off_reviews_are_not_sent(store):
    items, named = busy_day()
    rate(store, named["answered"], rating_withdrawn=True)
    rate(store, named["flex"], feedback_sent=True)
    mailer = FakeMailer()
    run(make_ctx(store, mailer), *items)
    assert sent_to(mailer, ALICE) == []
    rate(store, named["mfa"])
    run(make_ctx(store, mailer, email=email_settings(quality_feedback={"send": False}), options={"force": True}), *items)
    assert sent_to(mailer, ALICE) == []


def test_quality_feedback_finds_the_replier_from_the_row_when_the_review_has_no_address(store):
    items, named = busy_day()
    rate(store, named["answered"], replied_by_email="")
    mailer = FakeMailer()
    run(make_ctx(store, mailer), *items)
    assert len(sent_to(mailer, ALICE)) == 1


def test_review_sample_is_small_random_and_stable_for_the_day(store):
    answered = [mail(subject=f"Q{i}", body="FCUBS login fails", minutes_ago=300 - i) for i in range(8)]
    items = [x for a in answered for x in (a, reply(a))]
    outcome = eng.scan_and_analyze(make_ctx(store), archive(*items))
    first = er.review_sample(outcome.rows, 3, outcome.today)
    assert len(first) == 3 and first == er.review_sample(outcome.rows, 3, outcome.today)
    assert er.review_sample(outcome.rows, 100, outcome.today) != [] and len(er.review_sample(outcome.rows, 100, outcome.today)) == 8
    assert er.review_sample(outcome.rows, 0, outcome.today) == []
    mailer = FakeMailer()
    run(make_ctx(store, mailer, email=email_settings(quality_feedback={"sample_size": 2})), *items)
    assert "Suggested Review Sample (2)" in mailer.sent[0]["html"]


# ------------------------------------------------------------------------------------------- end of day
def test_end_of_day_summary_goes_to_the_report_recipients_with_the_workbook(store):
    items, named = busy_day()
    assign(store, named["flex"], note="x")
    rate(store, named["answered"], "Excellent")
    mailer = FakeMailer()
    run(make_ctx(store, mailer, email=email_settings(report={"cc": [CC]})), *items)
    eod, = [m for m in mailer.sent if m["subject"].startswith("End of Day Summary")]
    assert eod["to"] == [BOSS] and eod["cc"] == [CC] and eod["attachments"] == [str(report_path(store))]
    for text in ("Performance by Team", "Who Replied Today", "Assignments Made", "Quality Scorecard by Person",
                 "Flex pending", "Alice"):
        assert text in eod["html"]
    mailer = FakeMailer()
    run(make_ctx(store, mailer, now=NOW + timedelta(days=1), email=email_settings(
        end_of_day={"to": ["eod@example.test"], "cc": []})), *items)
    eod, = [m for m in mailer.sent if m["subject"].startswith("End of Day Summary")]
    assert eod["to"] == ["eod@example.test"] and eod["cc"] == []        # its own recipients win over the report's


def test_end_of_day_can_be_switched_off(store):
    mailer = FakeMailer()
    run(make_ctx(store, mailer, email=email_settings(end_of_day={"send": False})), *busy_day()[0])
    assert not [s for s in subjects(mailer) if s.startswith("End of Day")] and len(mailer.sent) == 1


# -------------------------------------------------------------------------------------------- idempotence
def test_a_second_call_for_the_same_slot_sends_nothing_and_does_not_scan(store):
    items, _ = busy_day()
    mailer = FakeMailer()
    first = run(make_ctx(store, mailer), *items)
    assert first.ok and not first.skipped and len(mailer.sent) == 2        # report + end-of-day
    assert store.read_state("email_report")["day"] == "2026-10-11"
    again = er.run_report(make_ctx(store, mailer), None)                   # no Outlook at all: it must not even try
    assert again.ok and again.skipped and "already sent" in again.summary and again.emails_sent == 0
    assert len(mailer.sent) == 2

    forced = run(make_ctx(store, mailer, options={"force": True}), *items)
    assert forced.ok and not forced.skipped and len(mailer.sent) == 4
    assert not run(make_ctx(store, mailer, now=NOW + timedelta(days=1)), *items).skipped     # a new day sends again


def test_each_report_slot_of_the_day_sends_once(store):
    items, _ = busy_day()
    mailer = FakeMailer()
    email = email_settings(report={"send_times": ["09:00", "16:30"]})
    assert er.slot_name(er.ReportConfig(email), datetime(2026, 10, 11, 8, 0)) == "manual"
    assert er.slot_name(er.ReportConfig(email), NOW) == "16:30"
    assert er.slot_name(er.ReportConfig(email), NOW, {"slot": "x"}) == "x"
    run(make_ctx(store, mailer, now=datetime(2026, 10, 11, 9, 5), email=email), *items)
    assert len(mailer.sent) == 2
    assert er.run_report(make_ctx(store, mailer, now=datetime(2026, 10, 11, 9, 10), email=email), None).skipped
    run(make_ctx(store, mailer, email=email), *items)                      # the 16:30 slot is a different slot
    assert len(mailer.sent) == 4


def test_a_failed_step_is_retried_and_finished_steps_are_not_repeated(store):
    items, _ = busy_day()
    flaky = FailingMailer("End of Day")
    first = run(make_ctx(store, flaky), *items)
    assert not first.ok and first.emails_sent == 1 and "end-of-day summary" in first.error
    assert store.read_state("email_report")["slots"]["16:30"]["failed"] == ["end_of_day"]
    mailer = FakeMailer()
    second = run(make_ctx(store, mailer), *items)                          # not skipped: something failed last time
    assert second.ok and not second.skipped
    assert subjects(mailer) == ["End of Day Summary - Team Reply Analytics - 11/10/2026"]     # the report is not re-sent
    assert er.run_report(make_ctx(store, mailer), None).skipped


def test_a_new_assignment_after_the_report_is_still_delivered_without_force(store):
    items, named = busy_day()
    mailer = FakeMailer()
    run(make_ctx(store, mailer), *items)
    assert er.run_report(make_ctx(store, mailer), None).skipped
    assign(store, named["flex"])                                           # assigned after the report went out
    result = run(make_ctx(store, mailer), *items)
    assert result.ok and not result.skipped and len(sent_to(mailer, ALICE)) == 1
    assert len(mailer.sent) == 3                                           # the report and the summary were not repeated


# ------------------------------------------------------------------------------- isolation and no-send paths
def test_one_failing_step_does_not_stop_the_others(store):
    items, named = busy_day()
    assign(store, named["flex"])
    rate(store, named["answered"])
    mailer = FailingMailer("Pending:")
    result = run(make_ctx(store, mailer, email=team_settings()), *items)
    assert not result.ok and "team follow-up Flex Cube" in result.error and "1 step(s) failed" in result.summary
    assert len(sent_to(mailer, BOSS)) == 2 and len(sent_to(mailer, ALICE)) == 2       # report, summary, notice, feedback
    assert result.emails_sent == len(mailer.sent) == 4
    assert store.overrides("assignments")[named["flex"].mid]["notified"] is True
    assert store.read_state("email_report")["slots"]["16:30"]["failed"] == ["team:Flex Cube"]


def test_a_failing_excel_does_not_stop_the_mails(store, monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(er, "create_excel", boom)
    mailer = FakeMailer()
    result = run(make_ctx(store, mailer), *busy_day()[0])
    assert not result.ok and result.error.startswith("excel: OSError: disk full")
    assert mailer.sent[0]["attachments"] == [] and len(mailer.sent) == 2 and result.snapshot_data["rows"]


def test_a_failing_history_does_not_stop_the_report(store, monkeypatch):
    monkeypatch.setattr(eng, "save_today_stats_history", lambda *a, **kw: 1 / 0)
    mailer = FakeMailer()
    result = run(make_ctx(store, mailer), *busy_day()[0])
    assert not result.ok and "history: ZeroDivisionError" in result.error and len(mailer.sent) == 2
    assert "final" not in (store.read_history("email", "2026-10-11") or {})
    assert "quality" in store.read_history("email", "2026-10-11")           # the other history step still ran


def test_no_recipient_means_nothing_is_sent_but_the_report_is_built(store):
    items, _ = busy_day()
    mailer = FakeMailer()
    ctx = make_ctx(store, mailer, email=email_settings(report={"to": []}))
    result = run(ctx, *items)
    assert result.ok and mailer.sent == [] and result.emails_sent == 0
    assert result.summary == "report built (nothing sent): 4 mails, 2 pending"
    assert report_path(store).is_file() and any("no recipient" in line for line in ctx.logs)
    assert store.read_state("email_report") == {}                           # nothing sent, nothing remembered
    off = make_ctx(store, mailer, email=email_settings(report={"send": False}))
    assert run(off, *items).ok and mailer.sent == []


def test_no_email_builds_everything_and_sends_nothing(store):
    items, named = busy_day()
    assign(store, named["flex"])
    rate(store, named["answered"])
    mailer = FakeMailer()
    result = run(make_ctx(store, mailer, no_email=True), *items)
    assert result.ok and mailer.sent == [] and result.emails_sent == 0
    assert result.summary.startswith("report built (e-mail is off for this run)")
    assert report_path(store).is_file() and "final" in store.read_history("email", "2026-10-11")
    assert store.overrides("assignments")[named["flex"].mid]["notified"] is False
    assert store.read_state("email_report") == {} and store.read_state("email_quality_sent") == {}
    assert not run(make_ctx(store, mailer), *items).skipped                 # the later real run is not blocked


def test_dry_run_writes_nothing_into_the_data_folder(store):
    mailer = FakeMailer()
    result = run(make_ctx(store, mailer, dry_run=True), *busy_day()[0])
    assert result.ok and mailer.sent == [] and result.snapshot_data["rows"]
    assert list(store.report_dir("email").iterdir()) == [] and store.history_days("email") == []
    assert store.read_state("email_report") == {}


def test_the_job_never_raises(store):
    off_windows = er.run_report(make_ctx(store), None)                      # a real Outlook is needed: not on this machine
    assert not off_windows.ok and "Outlook" in off_windows.error
    no_business = er.run_report(make_ctx(store, email=email_settings(business_emails=[])), archive())
    assert not no_business.ok and "business" in no_business.error
    mailerless = JobContext(job="email_report", settings=email_settings(), general={}, store=store, now=lambda: NOW,
                            log=lambda text: None, mailer=None)
    result = er.run_report(mailerless, archive(*busy_day()[0]))              # no mailer -> Outlook COM -> not here
    assert not result.ok and "report e-mail" in result.error and result.snapshot_data


# ------------------------------------------------------------------------------------------ results and data
def test_the_result_carries_the_snapshot_history_and_summary(store):
    items, _ = busy_day()
    result = run(make_ctx(store), *items)
    envelope = schemas.make_envelope("email", result.snapshot_data, "2026-10-11T17:00:00", 1.0, "PC1", "2026-10-11")
    assert schemas.validate_snapshot(envelope) == []
    assert "mail_item" not in str(result.snapshot_data)
    assert result.snapshot_date == "2026-10-11"
    assert result.history_point["business_emails"] == 4 and result.history_point["pending"] == 2
    assert result.summary == "report sent: 4 mails, 2 pending, end-of-day summary"
    day = store.read_history("email", "2026-10-11")
    assert day["final"]["business_emails"] == 4 and "quality" in day
    # the saved trend is what the snapshot shows tomorrow: today's history is not part of today's comparison
    tomorrow = eng.compute_trend(date(2026, 10, 12), eng.load_stats_history(store), {"business_emails": 5})
    assert tomorrow["vs_yesterday"]["business_emails"]["value"] == 4


def test_the_run_log_line_summarises_the_day(store):
    ctx = make_ctx(store)
    run(ctx, *busy_day()[0])
    line = next(l for l in ctx.logs if l.startswith("Date=11/10/2026"))
    assert "Business=4" in line and "Replied=1" in line and "Pending=2" in line and "EmailsSent=2" in line
    assert "Team_Reply_Analytics_2026-10-11.xlsx" in line


def test_the_module_carries_no_secret_or_personal_data():
    source = Path(er.__file__).read_text(encoding="utf-8")
    assert "base64" not in source.lower() and "nbe.com.eg" not in source.lower() and "password" not in source.lower()
    assert re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", source) == []
    assert er.LOGO_PATH.is_file() and er.LOGO_PATH.read_bytes()[:4] == b"\x89PNG"


def test_a_missing_logo_falls_back_to_a_text_badge(store, monkeypatch):
    monkeypatch.setattr(er, "LOGO_PATH", Path("/nonexistent/logo.png"))
    mailer = FakeMailer()
    run(make_ctx(store, mailer), *busy_day()[0])
    assert "cid:nbe_logo" not in mailer.sent[0]["html"] and ">NBE<" in mailer.sent[0]["html"]
    assert "nbe_logo" not in mailer.sent[0]["inline_images"]
