"""ITSM engine: pure helpers, the scan flow against a scripted browser, e-mail slots, the WebSocket client, and an
end-to-end run of a real headless Chromium (through the engine's own DevTools client) against tests/fake_itsm.py.

Fake data only (user 1001, password "secret-pw", example.test addresses); the clock is frozen (Sunday 2026-10-11).
"""
import copy
import json
import os
import socket
import struct
import threading
from datetime import datetime

import pytest

from calllog.ops import schemas
from calllog.ops import settings as ops_settings
from calllog.ops.engines import itsm_engine as eng
from calllog.ops.engines.common import CredentialError, FakeMailer, JobContext
from calllog.ops.store import OpsStore
from tests.fake_itsm import DEFAULT_ROLES, IMPLEMENTATION, ROLLBACK, FakeITSM

PASSWORD = "secret-pw"
NOW = datetime(2026, 10, 11, 7, 0, 0)            # before the 08:30 mail slot
ROLE_A, ROLE_B, ROLE_EMPTY = "IMP User provisioning", "IMP UAT I-Flex SMS", "IMP Empty"
CATEGORIES = ops_settings.DEFAULTS["itsm"]["categories"]


# ------------------------------------------------------------------------------------------------------ helpers
class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def make_settings(**over):
    cfg = copy.deepcopy(ops_settings.DEFAULTS["itsm"])
    cfg.update({"username": "1001", "url": "http://itsm.example.test/CAisd/x", "roles": [ROLE_A, ROLE_B], **over})
    return cfg


def make_ctx(tmp_path, cfg=None, clock=None, mailer=None, password=PASSWORD, **kw):
    logs = []

    def credentials(kind, username):
        if isinstance(password, Exception):
            raise password
        return password

    ctx = JobContext(job="itsm", settings=cfg or make_settings(), general={}, store=OpsStore(tmp_path / "data").init(),
                     now=clock or Clock(), log=logs.append, mailer=mailer, data_dir=str(tmp_path / "data"),
                     credentials=credentials, **kw)
    ctx.logs = logs
    return ctx


@pytest.fixture
def fast_time(monkeypatch):
    """Waits cost nothing: sleeping just moves a fake clock forward."""
    t = [1000.0]
    monkeypatch.setattr(eng, "_clock", lambda: t[0])
    monkeypatch.setattr(eng, "_sleep", lambda s: t.__setitem__(0, t[0] + s))
    return t


def rows_of(role_data, cat):
    return [{"Change Order #": r[0], "Summary": r[2] if len(r) > 2 else r[1]} for r in role_data[cat]["rows"]]


class ScriptedBrowser:
    """call / eval / close like Browser. It recognises which JavaScript the engine sends and answers as the site would."""

    def __init__(self, roles=None, logged_in=False, mouse="works", js_click="works", dead_categories=(), no_set_role=False,
                 explode_roles=(), debug_text="", site_password=PASSWORD):
        self.roles = copy.deepcopy(DEFAULT_ROLES if roles is None else roles)
        self.logged_in, self.mouse, self.js_click = logged_in, mouse, js_click
        self.dead, self.no_set_role, self.explode = set(dead_categories), no_set_role, set(explode_roles)
        self.debug_text, self.site_password = debug_text, site_password
        self.typed, self.selected, self.role, self.shown, self.pending = {}, None, None, [], None
        self.calls, self.evals, self.closed = [], [], False

    # -- the Browser surface
    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed" and self.pending:
            self._open(self.pending)
        return {}

    def close(self):
        self.closed = True

    def eval(self, expr):
        self.evals.append(expr)
        j = json.dumps
        if "want.includes(o.text.trim().toLowerCase())" in expr:
            return self.logged_in
        if "el.value = " in expr:
            key = "pass" if j(eng.SEL["pass"]) in expr else "user"
            self.typed[key] = json.JSONDecoder().raw_decode(expr[expr.index("el.value = ") + len("el.value = "):])[0]
            return True
        if "el.form.submit()" in expr:
            return self._login()
        if "return !!Q(" + j(eng.SEL["pass"]) in expr:
            return not self.logged_in
        if "const PAT = " in expr:
            return self._mouse_position(expr)
        if "s.selectedIndex = i" in expr:
            return self._select(json.JSONDecoder().raw_decode(expr[expr.index("const want = ") + 13:])[0])
        if "__itsm_m = 1" in expr or "return !w.__itsm_m" in expr:
            return True
        if "'Change Order #'" in expr and "const NUM" in expr:
            return self.shown
        if "scoreboard_html" in expr:
            return [{"url": "http://x/y", "title": "t", "text": self.debug_text, "elements": [], "row_html": None,
                     "scoreboard_html": None}]
        if "return !!T(" + j(eng.SEL["scoreboard_text"]) in expr:
            return self.role is not None
        if "m.el.click()" in expr:
            return self._click(expr)
        if "return m ? m.t : null;" in expr:
            return self._label(expr)
        raise AssertionError(f"unexpected script: {expr[-200:]}")

    # -- behaviours
    def _login(self):
        self.logged_in = self.typed.get("pass") == self.site_password and self.typed.get("user") == "1001"
        return True

    def _select(self, want):
        for role in self.roles:
            if role.lower() == want.lower():
                self.selected = role
                return True
        return False

    def _category(self, expr):
        for cat, pattern in CATEGORIES.items():
            if json.dumps(pattern) in expr:
                return cat
        return None

    def _label(self, expr):
        cat = self._category(expr)
        d = (self.roles.get(self.role) or {}).get(cat)
        return f"{cat} ({d['count']})" if d else None

    def _open(self, cat):
        if cat not in self.dead:
            self.shown = rows_of(self.roles[self.role], cat)

    def _mouse_position(self, expr):
        if self.mouse == "missing":
            return None
        self.pending = None if self.mouse == "dead" else self._category(expr)      # "dead": the click lands but nothing happens
        return {"x": 10, "y": 20}

    def _click(self, expr):
        if json.dumps(eng.SEL["login_btn_text"]) in expr:
            return self._login()
        if json.dumps(eng.SEL["set_role_text"]) in expr:
            if self.selected in self.explode:
                raise RuntimeError("boom")
            if self.no_set_role:
                return False
            self.role, self.shown = self.selected, []
            return True
        cat = self._category(expr)
        if self.js_click == "works" and cat:
            self._open(cat)
        return True


def run_scripted(ctx, browser=None, **kw):
    browser = browser or ScriptedBrowser(**kw)
    return eng.run(ctx, browser_factory=lambda c: browser), browser


# ------------------------------------------------------------------------------------------- count / rows / numbers
@pytest.mark.parametrize("label, expected", [
    ("Implementation Requests (3)", 3), ("Rollback Requests (12)", 12), ("Rollback Requests (0)", 0),
    ("Implementation Requests (٤)", 4), ("Implementation Requests", None), ("Rollback Requests ()", None),
    ("", None), (None, None)])
def test_parse_count(label, expected):
    assert eng.parse_count(label) == expected


def test_default_category_patterns_match_scoreboard_lines():
    import re
    assert re.search(CATEGORIES[IMPLEMENTATION], "Implementation Requests (3)", re.I)
    assert re.search(CATEGORIES[IMPLEMENTATION], "  implementation request (1)", re.I)
    assert not re.search(CATEGORIES[IMPLEMENTATION], "Implementation Completed (3)", re.I)
    assert re.search(CATEGORIES[ROLLBACK], "Rollback Requests (0)", re.I)


def test_rows_from_extract_cleans_the_javascript_result():
    raw = [{"Change Order #": " 123456 ", "Summary": "  two   spaces\nand a newline "},
           {"Change Order #": "123456", "Summary": "duplicate number"},
           {"Change Order #": "", "Summary": "no number"},
           {"Change Order #": "777777"},
           "junk", None, {"Change Order #": "888888", "Summary": "x" * 900}]
    rows = eng.rows_from_extract(raw)
    assert rows[0] == {"number": "123456", "summary": "two spaces and a newline"}
    assert rows[1] == {"number": "777777", "summary": ""}
    assert [r["number"] for r in rows] == ["123456", "777777", "888888"]
    assert len(rows[2]["summary"]) == eng.SUMMARY_MAX
    assert eng.rows_from_extract(None) == [] and eng.rows_from_extract("x") == []


def test_totals_ignore_failed_roles_and_missing_counts():
    roles = [{"role": "a", "error": None, "categories": {"X": {"count": 2}, "Y": {"count": None}}},
             {"role": "b", "error": "boom", "categories": {}},
             {"role": "c", "error": None, "categories": {"X": {"count": 1}}}]
    assert eng.compute_totals(roles, ["X", "Y"]) == {"X": 3, "Y": 0}
    assert eng.per_role_counts(roles) == {"a": {"X": 2}, "c": {"X": 1}}


# --------------------------------------------------------------------------------------------------------- changes
def lst(role, cat, *numbers):
    return {"role": role, "category": cat, "rows": [{"number": n, "summary": f"s{n}"} for n in numbers]}


def role_entry(role, cat, count, *numbers, warning=None, error=None):
    rows = [{"number": n, "summary": f"s{n}"} for n in numbers]
    return {"role": role, "error": error, "categories": {} if error else
            {cat: {"count": count, "rows": rows, "warning": warning, "note": None}}}


def test_changes_report_new_and_closed_requests():
    prev = [lst("r", "X", "1", "2", "3")]
    current = eng.complete_lists([role_entry("r", "X", 3, "2", "3", "4")])
    changes = eng.compute_changes(prev, current)
    assert changes == {"new": [{"role": "r", "category": "X", "number": "4", "summary": "s4"}],
                       "closed": [{"role": "r", "category": "X", "number": "1", "summary": "s1"}]}


def test_first_scan_has_no_baseline_and_so_no_changes():
    current = eng.complete_lists([role_entry("r", "X", 2, "1", "2")])
    assert eng.compute_changes(None, current) == {"new": [], "closed": []}
    assert eng.compute_changes([], current) == {"new": [], "closed": []}
    assert eng.compute_changes([lst("other", "X", "9")], current) == {"new": [], "closed": []}     # new role: no baseline either


def test_unreadable_failed_or_paged_lists_never_look_like_changes():
    prev = [lst("r", "X", "1", "2"), lst("f", "X", "3"), lst("p", "X", "4", "5")]
    roles = [role_entry("r", "X", 2, warning="could not read"),            # count 2 but the list was not read
             role_entry("f", "X", 0, error="Set Role button not found"),   # the whole role failed
             role_entry("p", "X", 9, "4")]                                 # paged: 1 of 9 rows seen
    assert eng.complete_lists(roles) == {}
    assert eng.compute_changes(prev, eng.complete_lists(roles)) == {"new": [], "closed": []}


def test_an_empty_scoreboard_closes_everything_that_was_there():
    current = eng.complete_lists([role_entry("r", "X", 0)])
    assert current == {("r", "X"): []}
    changes = eng.compute_changes([lst("r", "X", "1", "2")], current)
    assert [c["number"] for c in changes["closed"]] == ["1", "2"] and changes["new"] == []


def test_remembered_lists_keep_what_could_not_be_read_and_drop_unconfigured_roles():
    prev = [lst("a", "X", "1"), lst("b", "X", "2"), lst("gone", "X", "3"), lst("a", "Old", "4")]
    current = {("a", "X"): [{"number": "9", "summary": ""}]}
    merged = eng.merge_lists(prev, current, {"a", "b"}, {"X"})
    by_key = {(l["role"], l["category"]): [r["number"] for r in l["rows"]] for l in merged}
    assert by_key == {("a", "X"): ["9"], ("b", "X"): ["2"]}


# ------------------------------------------------------------------------------------------------ subject and HTML
def sample_data():
    return {"categories": [IMPLEMENTATION, ROLLBACK], "totals": {}, "changes": {"new": [], "closed": []}, "roles": [
        {"role": "Role <b>A</b>", "error": None, "categories": {
            IMPLEMENTATION: {"count": 2, "rows": [{"number": "123456", "summary": "<script>alert(1)</script> & more"},
                                                  {"number": "123457", "summary": "طلب"}], "warning": None, "note": None},
            ROLLBACK: {"count": 0, "rows": [], "warning": None, "note": None}}},
        {"role": "Role B", "error": None, "categories": {
            IMPLEMENTATION: {"count": 5, "rows": [{"number": "200001", "summary": "one"}], "warning": None,
                             "note": "Showing 1 of 5 requests (list is paged)."},
            ROLLBACK: {"count": None, "rows": [], "warning": None, "note": None}}},
    ]}


def test_subject_has_the_totals_and_flags_errors():
    data = sample_data()
    assert eng.build_subject(data, NOW) == "ITSM Status - 2026-10-11 07:00 | Implementation: 7 | Rollback: 0"
    data["roles"].append({"role": "C", "error": "boom <x>", "categories": {}})
    assert eng.build_subject(data, NOW).endswith("| CHECK REQUIRED")


def test_html_escapes_everything_that_came_from_the_portal():
    data = sample_data()
    data["roles"].append({"role": "Role <i>C</i>", "error": "<img src=x onerror=alert(1)>", "categories": {}})
    html = eng.build_html(data, NOW, source="itsm.example.test")
    assert "<script>" not in html and "<b>A</b>" not in html and "<i>C</i>" not in html and "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    for needle in ("123456", "طلب", "Showing 1 of 5 requests (list is paged).", "Could not be scanned", "Source: itsm.example.test",
                   "Sunday, 11 October 2026"):
        assert needle in html
    assert html.count("Not available") == 0         # a category that is not on the role's scoreboard is simply left out
    assert "No requests" in html                    # Rollback 0 for Role <b>A</b>


# ---------------------------------------------------------------------------------------------------- mail slots
def test_plan_mail_before_and_after_a_slot():
    times = ["08:30", "16:00"]
    assert eng.plan_mail(times, {}, datetime(2026, 10, 11, 8, 29), True, False) == (None, [])
    assert eng.plan_mail(times, {}, datetime(2026, 10, 11, 8, 30), True, False) == ("report", ["2026-10-11 08:30"])
    both = eng.plan_mail(times, {}, datetime(2026, 10, 11, 17, 0), True, False)
    assert both == ("report", ["2026-10-11 08:30", "2026-10-11 16:00"])           # collector was down: one mail, both slots
    sent = {"2026-10-11 08:30": {"report": "x"}}
    assert eng.plan_mail(times, sent, datetime(2026, 10, 11, 17, 0), True, False) == ("report", ["2026-10-11 16:00"])
    assert eng.plan_mail(times, sent, datetime(2026, 10, 11, 9, 0), True, False) == (None, [])
    assert eng.plan_mail(times, sent, datetime(2026, 10, 12, 8, 30), True, False) == ("report", ["2026-10-12 08:30"])


def test_plan_mail_failure_once_per_slot_and_manual():
    times = ["08:30"]
    at = datetime(2026, 10, 11, 8, 40)
    assert eng.plan_mail(times, {}, at, False, False) == ("failure", ["2026-10-11 08:30"])
    assert eng.plan_mail(times, {"2026-10-11 08:30": {"failure": "x"}}, at, False, False) == (None, [])
    assert eng.plan_mail(times, {"2026-10-11 08:30": {"failure": "x"}}, at, True, False) == ("report", ["2026-10-11 08:30"])
    assert eng.plan_mail(times, {}, datetime(2026, 10, 11, 8, 0), False, False) == (None, [])
    assert eng.plan_mail(times, {}, datetime(2026, 10, 11, 8, 0), True, True) == ("report", [])
    assert eng.plan_mail(times, {}, datetime(2026, 10, 11, 8, 0), False, True) == ("failure", [])
    assert eng.plan_mail([], {}, at, True, False) == (None, [])


# ------------------------------------------------------------------------------------------------- the scan, scripted
def test_good_scan_produces_the_contract_5_2_snapshot(tmp_path, fast_time):
    ctx = make_ctx(tmp_path, make_settings(roles=[ROLE_A, ROLE_B, ROLE_EMPTY]))
    result, browser = run_scripted(ctx)
    assert result.ok and result.error is None and result.error_kind is None
    data = result.snapshot_data
    assert data["categories"] == [IMPLEMENTATION, ROLLBACK]
    assert data["totals"] == {IMPLEMENTATION: 5, ROLLBACK: 1}
    assert data["changes"] == {"new": [], "closed": []}
    role_a, role_b, role_empty = data["roles"]
    assert role_a == {"role": ROLE_A, "error": None, "categories": {
        IMPLEMENTATION: {"count": 3, "warning": None, "note": None, "rows": [
            {"number": "111111", "summary": "Create user for branch 12"},
            {"number": "222222", "summary": "طلب تفعيل مستخدم جديد"},
            {"number": "333333", "summary": "Cut short in the cell, but the full text is in the title"}]},
        ROLLBACK: {"count": 1, "warning": None, "note": None,
                   "rows": [{"number": "444444", "summary": "<script>alert(1)</script> & rollback"}]}}}
    assert role_b["categories"][ROLLBACK] == {"count": None, "rows": [], "warning": None, "note": None}   # not on this scoreboard
    assert role_empty["categories"][IMPLEMENTATION]["count"] == 0
    assert schemas.validate_snapshot(schemas.make_envelope("itsm", data, "2026-10-11T07:00:00")) == []
    assert result.history_point == {"ts": "2026-10-11T07:00:00", "totals": {IMPLEMENTATION: 5, ROLLBACK: 1},
                                    "per_role": {ROLE_A: {IMPLEMENTATION: 3, ROLLBACK: 1}, ROLE_B: {IMPLEMENTATION: 2},
                                                 ROLE_EMPTY: {IMPLEMENTATION: 0, ROLLBACK: 0}}}
    assert result.snapshot_date == "2026-10-11" and result.emails_sent == 0
    assert result.summary == "5 implementation, 1 rollback"
    assert browser.closed
    assert browser.typed == {"user": "1001", "pass": PASSWORD}
    assert ctx.store.read_snapshot("itsm") is None                     # the engine never writes snapshots
    report = ctx.store.report_dir("itsm") / "itsm_latest.html"
    assert report.is_file() and "Create user for branch 12" in report.read_text(encoding="utf-8")


def test_changes_are_found_against_the_previous_good_scan_even_on_the_next_day(tmp_path, fast_time):
    clock = Clock()
    ctx = make_ctx(tmp_path, clock=clock)
    first, _ = run_scripted(ctx)
    assert first.snapshot_data["changes"] == {"new": [], "closed": []}
    remembered = ctx.store.read_state("itsm")
    assert {(l["role"], l["category"]) for l in remembered["lists"]} == {
        (ROLE_A, IMPLEMENTATION), (ROLE_A, ROLLBACK), (ROLE_B, IMPLEMENTATION)}

    clock.value = datetime(2026, 10, 12, 8, 0)                         # first scan of the next day
    roles = copy.deepcopy(DEFAULT_ROLES)
    roles[ROLE_A][IMPLEMENTATION]["rows"][1] = ("999999", "brand new request")
    roles[ROLE_A][ROLLBACK] = {"count": 0, "rows": []}
    second, _ = run_scripted(ctx, roles=roles)
    changes = second.snapshot_data["changes"]
    assert changes["new"] == [{"role": ROLE_A, "category": IMPLEMENTATION, "number": "999999", "summary": "brand new request"}]
    assert sorted((c["category"], c["number"]) for c in changes["closed"]) == [
        (IMPLEMENTATION, "222222"), (ROLLBACK, "444444")]
    assert second.summary == "5 implementation, 0 rollback (1 new, 2 closed)"
    assert second.snapshot_date == "2026-10-12"


def test_a_failed_role_keeps_its_old_baseline_and_never_reports_closures(tmp_path, fast_time):
    ctx = make_ctx(tmp_path)
    run_scripted(ctx)
    result, _ = run_scripted(ctx, explode_roles={ROLE_A})
    assert result.ok
    assert result.snapshot_data["changes"] == {"new": [], "closed": []}
    third, _ = run_scripted(ctx)                                       # role A is back, unchanged
    assert third.snapshot_data["changes"] == {"new": [], "closed": []}


def test_a_failing_role_is_recorded_and_the_others_still_run(tmp_path, fast_time):
    ctx = make_ctx(tmp_path, make_settings(roles=[ROLE_A, "IMP No Such Role", ROLE_B]))
    result, browser = run_scripted(ctx)
    assert result.ok
    roles = result.snapshot_data["roles"]
    assert roles[1] == {"role": "IMP No Such Role", "error": "Role 'IMP No Such Role' not found in the dropdown",
                        "categories": {}}
    assert roles[0]["error"] is None and roles[2]["error"] is None
    assert result.snapshot_data["totals"] == {IMPLEMENTATION: 5, ROLLBACK: 1}
    assert result.summary == "5 implementation, 1 rollback, 1 role failed"
    assert browser.closed


def test_set_role_button_missing_and_script_errors_are_isolated_per_role(tmp_path, fast_time):
    ctx = make_ctx(tmp_path)
    result, _ = run_scripted(ctx, explode_roles={ROLE_A})
    assert result.ok
    assert result.snapshot_data["roles"][0]["error"] == "boom"
    assert result.snapshot_data["roles"][1]["error"] is None
    broken, _ = run_scripted(make_ctx(tmp_path / "b"), no_set_role=True)
    assert not broken.ok and broken.snapshot_data is None
    assert "Set Role button not found" in broken.error


def test_when_no_role_could_be_read_the_scan_fails_and_returns_no_snapshot(tmp_path, fast_time):
    ctx = make_ctx(tmp_path, make_settings(roles=["Nope 1", "Nope 2"]))
    result, _ = run_scripted(ctx)
    assert not result.ok and result.snapshot_data is None and result.history_point is None
    assert result.error.startswith("No role could be read (2 failed)")
    assert ctx.store.read_state("itsm") == {}                          # nothing remembered from a failed scan


def test_browser_that_cannot_start_is_a_clean_failure(tmp_path, fast_time):
    def factory(ctx):
        raise eng.ScanError("The browser did not answer on its remote-debugging port (a bank policy may block it).")

    ctx = make_ctx(tmp_path)
    result = eng.run(ctx, browser_factory=factory)
    assert not result.ok and result.snapshot_data is None and result.error_kind is None
    assert "remote-debugging port" in result.error
    assert any("FATAL" in line for line in ctx.logs)


def test_wrong_password_is_an_auth_failure_and_the_browser_is_closed(tmp_path, fast_time):
    browser = ScriptedBrowser(site_password="another-pw")
    ctx = make_ctx(tmp_path)
    result, _ = run_scripted(ctx, browser)
    assert not result.ok and result.error_kind == "auth" and result.snapshot_data is None
    assert "not accepted" in result.error and PASSWORD not in result.error
    assert browser.closed
    assert sum(1 for m, _ in browser.calls if m == "Page.navigate") == 1        # one login attempt, no retries


def test_already_signed_in_profile_skips_the_login(tmp_path, fast_time):
    result, browser = run_scripted(make_ctx(tmp_path), logged_in=True)
    assert result.ok and browser.typed == {}


def test_a_dead_browser_fails_every_role_quickly_instead_of_waiting_for_timeouts(tmp_path, fast_time):
    class Dead(ScriptedBrowser):
        def eval(self, expr):
            if self.logged_in:
                raise ConnectionError("websocket closed by browser")
            return super().eval(expr)

        def _login(self):
            self.logged_in = True
            return True

    browser = Dead()
    result, _ = run_scripted(make_ctx(tmp_path), browser)
    assert not result.ok and result.snapshot_data is None
    assert "websocket closed by browser" in result.error
    assert len(browser.evals) < 40 and fast_time[0] < 1100          # no 45 s waits per role
    assert browser.closed


def test_missing_password_is_a_clean_error_and_no_browser_is_started(tmp_path, fast_time):
    started = []
    ctx = make_ctx(tmp_path, password=CredentialError("No saved password for the ITSM account '1001'."))
    result = eng.run(ctx, browser_factory=lambda c: started.append(1))
    assert not result.ok and result.error_kind == "auth" and "No saved password" in result.error
    assert started == [] and result.snapshot_data is None


def test_missing_username_or_roles_fail_before_any_browser(tmp_path, fast_time):
    started = []
    cfg = make_settings()
    cfg["username"] = ""
    result = eng.run(make_ctx(tmp_path, cfg), browser_factory=lambda c: started.append(1))
    assert not result.ok and result.error_kind == "auth" and "user name is not set" in result.error
    result = eng.run(make_ctx(tmp_path / "b", make_settings(roles=[])), browser_factory=lambda c: started.append(1))
    assert not result.ok and "roles" in result.error
    assert started == []


def test_the_password_is_requested_for_itsm_and_the_configured_username(tmp_path, fast_time):
    asked = []
    ctx = make_ctx(tmp_path)
    ctx.credentials = lambda kind, username: asked.append((kind, username)) or PASSWORD
    run_scripted(ctx)
    assert asked == [("itsm", "1001")]


def test_unexpected_exceptions_never_escape_and_never_show_the_password(tmp_path, fast_time, monkeypatch):
    class Exploding(ScriptedBrowser):
        def eval(self, expr):
            raise ValueError(f"cannot talk to the page {PASSWORD}")

    ctx = make_ctx(tmp_path)
    result = eng.run(ctx, browser_factory=lambda c: Exploding())
    assert not result.ok
    assert PASSWORD not in (result.error or "") and not any(PASSWORD in line for line in ctx.logs)
    monkeypatch.setattr(eng, "compute_changes", lambda *a: 1 / 0)          # a bug outside the browser part
    broken = eng.run(make_ctx(tmp_path / "b"), browser_factory=lambda c: ScriptedBrowser())
    assert not broken.ok and broken.error.startswith("Unexpected error") and broken.snapshot_data is None


def test_settings_roles_categories_and_regexes_drive_the_scan(tmp_path, fast_time):
    cats = {IMPLEMENTATION: CATEGORIES[IMPLEMENTATION]}
    ctx = make_ctx(tmp_path, make_settings(roles=[ROLE_B], categories=cats))
    result, _ = run_scripted(ctx)
    assert result.snapshot_data["categories"] == [IMPLEMENTATION]
    assert list(result.snapshot_data["roles"][0]["categories"]) == [IMPLEMENTATION]
    assert result.snapshot_data["totals"] == {IMPLEMENTATION: 2}


# ------------------------------------------------------------------------------------------------- list reading
def test_mouse_click_is_tried_first_and_the_js_click_is_the_fallback(tmp_path, fast_time):
    result, browser = run_scripted(make_ctx(tmp_path))
    assert result.ok
    clicks = [p for m, p in browser.calls if m == "Input.dispatchMouseEvent"]
    assert [c["type"] for c in clicks[:3]] == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert not any("m.el.click()" in e and json.dumps(CATEGORIES[IMPLEMENTATION]) in e for e in browser.evals)

    result, browser = run_scripted(make_ctx(tmp_path / "b"), mouse="missing")
    assert result.ok and result.snapshot_data["totals"][IMPLEMENTATION] == 5
    assert any("m.el.click()" in e and json.dumps(CATEGORIES[IMPLEMENTATION]) in e for e in browser.evals)

    ctx = make_ctx(tmp_path / "c")
    result, _ = run_scripted(ctx, mouse="dead", js_click="works")           # the mouse click does nothing, JS click opens the list
    assert result.ok and len(result.snapshot_data["roles"][0]["categories"][IMPLEMENTATION]["rows"]) == 3
    assert any("list did not load after mouse click" in line for line in ctx.logs)


def test_a_list_that_never_loads_is_a_warning_not_the_previous_categories_rows(tmp_path, fast_time):
    ctx = make_ctx(tmp_path, make_settings(roles=[ROLE_A]))
    result, _ = run_scripted(ctx, dead_categories={ROLLBACK})
    cats = result.snapshot_data["roles"][0]["categories"]
    assert len(cats[IMPLEMENTATION]["rows"]) == 3
    assert cats[ROLLBACK]["count"] == 1 and cats[ROLLBACK]["rows"] == []                  # not the 3 rows of Implementation
    assert cats[ROLLBACK]["warning"] == "The scoreboard shows requests, but the list could not be read."
    assert result.ok and result.snapshot_data["totals"][ROLLBACK] == 1
    # a warning list is not a baseline: nothing is remembered for it
    assert {(l["role"], l["category"]) for l in ctx.store.read_state("itsm")["lists"]} == {(ROLE_A, IMPLEMENTATION)}


def test_a_paged_list_gets_a_note(tmp_path, fast_time):
    roles = copy.deepcopy(DEFAULT_ROLES)
    roles[ROLE_B][IMPLEMENTATION]["count"] = 7
    result, _ = run_scripted(make_ctx(tmp_path), roles=roles)
    entry = result.snapshot_data["roles"][1]["categories"][IMPLEMENTATION]
    assert entry["count"] == 7 and len(entry["rows"]) == 2
    assert entry["note"] == "Showing 2 of 7 requests (list is paged)."


def test_scoreboard_line_without_a_number_is_a_warning(tmp_path, fast_time):
    class NoNumber(ScriptedBrowser):
        def _label(self, expr):
            label = super()._label(expr)
            return label.split(" (")[0] if label else label

    result, _ = run_scripted(make_ctx(tmp_path, make_settings(roles=[ROLE_A])), NoNumber())
    entry = result.snapshot_data["roles"][0]["categories"][IMPLEMENTATION]
    assert entry["count"] is None and "no number in brackets" in entry["warning"]


# ---------------------------------------------------------------------------------------------------- debug dumps
def test_debug_files_only_with_debug_and_never_with_the_password(tmp_path, fast_time):
    leaky = f"page text that happens to contain {PASSWORD} and more"
    ctx = make_ctx(tmp_path, make_settings(roles=["IMP No Such Role", ROLE_A]))
    run_scripted(ctx, debug_text=leaky)
    assert not (tmp_path / "data" / "ops" / "debug").exists()

    ctx = make_ctx(tmp_path / "d", make_settings(roles=["IMP No Such Role", ROLE_A]), debug=True)
    result, _ = run_scripted(ctx, debug_text=leaky, dead_categories={ROLLBACK})
    folder = tmp_path / "d" / "data" / "ops" / "debug" / "itsm"
    files = sorted(p.name for p in folder.iterdir())
    assert files == ["itsm_debug_IMP_No_Such_Role.json", "itsm_debug_IMP_User_provisioning_Rollback_Requests.json"]
    for p in folder.iterdir():
        text = p.read_text(encoding="utf-8")
        assert PASSWORD not in text and "***" in text
        assert json.loads(text)["frames"][0]["url"] == "http://x/y"
    assert result.ok


def test_the_debug_script_reads_only_button_labels_never_typed_values():
    assert "type !== 'password'" not in eng.DEBUG_JS            # the old rule (everything but passwords) is gone
    assert "/^(button|submit|reset)$/i.test(e.type) ? e.value : ''" in eng.DEBUG_JS


def test_a_fatal_failure_dumps_the_page_when_debug_is_on(tmp_path, fast_time):
    browser = ScriptedBrowser(site_password="other")
    ctx = make_ctx(tmp_path, debug=True)
    result, _ = run_scripted(ctx, browser)
    assert not result.ok
    assert (tmp_path / "data" / "ops" / "debug" / "itsm" / "itsm_debug_fatal.json").is_file()


# ------------------------------------------------------------------------------------------------------------ e-mail
def mail_ctx(tmp_path, clock=None, mailer=None, **mail):
    cfg = make_settings()
    cfg["mail"] = {"send": True, "send_times": ["08:30"], "to": ["boss@example.test"], "cc": ["cc@example.test"],
                   "display_only": False, **mail}
    return make_ctx(tmp_path, cfg, clock=clock, mailer=mailer or FakeMailer())


def test_no_mail_before_the_slot_then_exactly_one_after_it(tmp_path, fast_time):
    clock, mailer = Clock(datetime(2026, 10, 11, 8, 29)), FakeMailer()
    ctx = mail_ctx(tmp_path, clock, mailer)
    assert run_scripted(ctx)[0].emails_sent == 0 and mailer.sent == []

    clock.value = datetime(2026, 10, 11, 8, 31)
    result, _ = run_scripted(ctx)
    assert result.emails_sent == 1 and len(mailer.sent) == 1
    mail = mailer.sent[0]
    assert mail["to"] == ["boss@example.test"] and mail["cc"] == ["cc@example.test"]
    assert mail["subject"] == "ITSM Status - 2026-10-11 08:31 | Implementation: 5 | Rollback: 1"
    assert "Create user for branch 12" in mail["html"] and "&lt;script&gt;" in mail["html"] and "<script>" not in mail["html"]
    assert ctx.store.read_state("itsm")["mail_sent"] == {"2026-10-11 08:30": {"report": "2026-10-11T08:31:00"}}

    clock.value = datetime(2026, 10, 11, 9, 1)
    assert run_scripted(ctx)[0].emails_sent == 0 and len(mailer.sent) == 1        # same slot: never twice

    clock.value = datetime(2026, 10, 12, 8, 45)
    assert run_scripted(ctx)[0].emails_sent == 1 and len(mailer.sent) == 2        # next day, next slot


def test_a_failed_scan_sends_the_failure_mail_once_and_the_report_follows_later(tmp_path, fast_time):
    clock, mailer = Clock(datetime(2026, 10, 11, 8, 35)), FakeMailer()
    ctx = mail_ctx(tmp_path, clock, mailer)
    failed, _ = run_scripted(ctx, ScriptedBrowser(site_password="other"))
    assert not failed.ok and failed.emails_sent == 1
    mail = mailer.sent[0]
    assert mail["subject"].endswith("| CHECK REQUIRED") and "Implementation: 0 | Rollback: 0" in mail["subject"]
    assert "not accepted" in mail["html"] and ROLE_A in mail["html"] and PASSWORD not in mail["html"]

    clock.value = datetime(2026, 10, 11, 9, 5)
    assert run_scripted(ctx, ScriptedBrowser(site_password="other"))[0].emails_sent == 0     # still the same slot
    assert len(mailer.sent) == 1

    clock.value = datetime(2026, 10, 11, 9, 35)
    good, _ = run_scripted(ctx)
    assert good.ok and good.emails_sent == 1 and "CHECK REQUIRED" not in mailer.sent[1]["subject"]
    assert ctx.store.read_state("itsm")["mail_sent"]["2026-10-11 08:30"].keys() == {"failure", "report"}


def test_run_now_with_send_mail_sends_regardless_of_the_slots(tmp_path, fast_time):
    mailer = FakeMailer()
    ctx = mail_ctx(tmp_path, Clock(datetime(2026, 10, 11, 7, 0)), mailer)
    ctx.options = {"send_mail": True}
    assert run_scripted(ctx)[0].emails_sent == 1 and len(mailer.sent) == 1
    ctx.options = {}
    assert run_scripted(ctx)[0].emails_sent == 0                 # a plain scheduled run before the slot sends nothing


@pytest.mark.parametrize("kw", [{"no_email": True}, {"dry_run": True}])
def test_no_mail_and_no_files_when_the_run_forbids_it(tmp_path, fast_time, kw):
    mailer = FakeMailer()
    ctx = mail_ctx(tmp_path, Clock(datetime(2026, 10, 11, 9, 0)), mailer)
    for k, v in kw.items():
        setattr(ctx, k, v)
    ctx.options = {"send_mail": True}
    result, _ = run_scripted(ctx)
    assert result.ok and result.emails_sent == 0 and mailer.sent == []
    if "dry_run" in kw:
        assert ctx.store.read_state("itsm") == {}
        assert not (ctx.store.reports_dir / "itsm" / "itsm_latest.html").exists()


def test_mail_switch_off_or_no_mailer_means_no_mail(tmp_path, fast_time):
    mailer = FakeMailer()
    ctx = mail_ctx(tmp_path, Clock(datetime(2026, 10, 11, 9, 0)), mailer, send=False)
    ctx.options = {"send_mail": True}
    assert run_scripted(ctx)[0].emails_sent == 0 and mailer.sent == []
    ctx = mail_ctx(tmp_path / "b", Clock(datetime(2026, 10, 11, 9, 0)))
    ctx.mailer = None
    result, _ = run_scripted(ctx)
    assert result.ok and result.emails_sent == 0
    assert "mail_sent" not in ctx.store.read_state("itsm")


def test_without_recipients_the_mail_goes_to_the_outlook_account_like_the_old_script(tmp_path, fast_time):
    mailer = FakeMailer()
    ctx = mail_ctx(tmp_path, Clock(datetime(2026, 10, 11, 9, 0)), mailer, to=[], cc=[])
    result, _ = run_scripted(ctx)
    assert result.emails_sent == 1 and mailer.sent[0]["to"] == [mailer.my_address()] and mailer.sent[0]["cc"] == []


def test_a_mail_failure_does_not_fail_the_scan_and_the_slot_stays_open(tmp_path, fast_time):
    clock, mailer = Clock(datetime(2026, 10, 11, 9, 0)), FakeMailer(fail=True)
    ctx = mail_ctx(tmp_path, clock, mailer)
    result, _ = run_scripted(ctx)
    assert result.ok and result.emails_sent == 0 and result.snapshot_data
    assert any("e-mail failed" in line for line in ctx.logs)
    mailer.fail = False
    assert run_scripted(ctx)[0].emails_sent == 1                  # retried on the next scan


def test_display_only_is_passed_to_the_mailer(tmp_path, fast_time):
    class Spy(FakeMailer):
        def send(self, *a, **kw):
            self.kwargs = kw
            return super().send(*a, **kw)

    mailer = Spy()
    run_scripted(mail_ctx(tmp_path, Clock(datetime(2026, 10, 11, 9, 0)), mailer, display_only=True))
    assert mailer.kwargs["display_only"] is True


def test_debug_files_are_attached_only_when_something_went_wrong(tmp_path, fast_time):
    mailer = FakeMailer()
    cfg = mail_ctx(tmp_path).settings
    cfg["roles"] = ["IMP No Such Role", ROLE_B]
    ctx = make_ctx(tmp_path, cfg, clock=Clock(datetime(2026, 10, 11, 9, 0)), mailer=mailer, debug=True)
    run_scripted(ctx)
    assert [os.path.basename(p) for p in mailer.sent[0]["attachments"]] == ["itsm_debug_IMP_No_Such_Role.json"]
    mailer2 = FakeMailer()
    ctx2 = mail_ctx(tmp_path / "b", Clock(datetime(2026, 10, 11, 9, 0)), mailer2)
    ctx2.debug = True
    run_scripted(ctx2)
    assert mailer2.sent[0]["attachments"] == []


# --------------------------------------------------------------------------------- browser start-up (no browser run)
def test_browser_is_found_from_settings_then_the_environment_then_playwright(tmp_path, monkeypatch):
    exe = tmp_path / "my-chrome"
    exe.write_text("x")
    assert eng.find_browser(str(exe), {}) == str(exe)
    assert eng.find_browser("", {"CHROME_PATH": str(exe)}) == str(exe) or os.name == "nt"
    pw = tmp_path / "pw" / "chromium-1100" / "chrome-linux"
    pw.mkdir(parents=True)
    (pw / "chrome").write_text("x")
    (tmp_path / "pw" / "chromium_headless_shell-1100" / "chrome-linux").mkdir(parents=True)
    assert eng.playwright_chromiums({"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "pw")}) == [str(pw / "chrome")]
    if os.name != "nt":
        assert eng.find_browser("", {"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "pw")}) == str(pw / "chrome")
        assert eng.find_browser("", {"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "empty")}) is None
    legacy = eng.browser_candidates("", {})
    assert any(p.endswith(r"Google\Chrome\Application\chrome.exe") for p in legacy)
    assert any(p.endswith(r"Microsoft\Edge\Application\msedge.exe") for p in legacy)


def test_launch_arguments_follow_the_old_script():
    args = eng.build_args("/x/chrome", 9333, "/p", True)
    assert args[0] == "/x/chrome" and args[-1] == "about:blank"
    for flag in ("--headless=new", "--remote-debugging-port=9333", "--remote-allow-origins=*", "--user-data-dir=/p",
                 "--ignore-certificate-errors", "--no-first-run", "--window-size=1920,1080", "--mute-audio"):
        assert flag in args
    shown = eng.build_args("/x/chrome", 9333, "/p", False)
    assert not any(a.startswith("--headless") for a in shown) and "--window-size=1920,1080" not in shown


def test_windows_only_parts_are_guarded_by_os_name(tmp_path, monkeypatch):
    class OsAs:
        def __init__(self, name):
            self.name = name

        def __getattr__(self, attr):
            return getattr(os, attr)

    monkeypatch.setattr(eng, "os", OsAs("nt"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    profile = eng.profile_dir()
    assert profile.startswith(str(tmp_path / "local")) and profile.endswith("NBE-Ops" + os.sep + "itsm_profile")
    assert eng.popen_kwargs(True)["creationflags"] == eng.CREATE_NO_WINDOW
    assert eng.popen_kwargs(False)["creationflags"] == 0
    assert "start_new_session" not in eng.popen_kwargs(True)
    assert not any(a in ("--no-sandbox", "--disable-dev-shm-usage") for a in eng.build_args("c", 1, "p", True))
    seen = []
    monkeypatch.setattr(eng.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)))
    eng.kill_profile_browsers(profile)
    assert seen and seen[0][0][0] == "powershell" and "*itsm_profile*" in seen[0][0][-1]
    assert seen[0][1]["creationflags"] == eng.CREATE_NO_WINDOW

    monkeypatch.setattr(eng, "os", OsAs("posix"))
    seen.clear()
    eng.kill_profile_browsers(profile)
    assert seen == []                                              # nothing is run outside Windows
    assert "creationflags" not in eng.popen_kwargs(True) and eng.popen_kwargs(True)["start_new_session"] is True
    assert eng.profile_dir().startswith(__import__("tempfile").gettempdir())


@pytest.mark.skipif(os.name == "nt" or not os.path.exists("/bin/sleep"), reason="needs POSIX sleep / false")
@pytest.mark.parametrize("command, message", [(["/bin/sleep", "60"], "did not answer"), (["/bin/false"], r"exited at start-up \(code 1\)")])
def test_browser_that_never_answers_or_dies_is_reported_and_not_left_running(tmp_path, monkeypatch, fast_time, command, message):
    monkeypatch.setattr(eng, "profile_dir", lambda: str(tmp_path / "p"))
    monkeypatch.setattr(eng, "find_browser", lambda configured="", environ=None: command[0])
    monkeypatch.setattr(eng, "build_args", lambda exe, port, profile, headless: command)
    started, real_popen = [], eng.subprocess.Popen
    monkeypatch.setattr(eng.subprocess, "Popen", lambda *a, **kw: started.append(real_popen(*a, **kw)) or started[-1])
    with pytest.raises(eng.ScanError, match=message):
        eng.Browser({"cdp_port": free_port()}, log=lambda m: None)
    assert started and started[0].poll() is not None


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
def test_closing_the_browser_ends_helper_processes_too():
    import subprocess
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & sleep 60"], **eng.popen_kwargs(True))
    eng.Browser._stop(proc)
    assert proc.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)                                 # the whole group is gone
    eng.Browser._stop(proc)                                    # closing twice is harmless


def test_the_profile_is_never_inside_the_data_folder(tmp_path):
    assert not eng.profile_dir().startswith(str(tmp_path))
    assert "itsm_profile" in eng.profile_dir()


# ------------------------------------------------------------------------------------------------ WebSocket client
class MiniWebSocketServer:
    """Accepts one client, completes the handshake, echoes every text message back and also sends a ping, a fragmented message
    and a 70 kB message when asked."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.received = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @staticmethod
    def frame(payload, op=1, fin=True):
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | op])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        return head + payload

    def _read(self, conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _serve(self):
        conn, _ = self.srv.accept()
        with conn:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(4096)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            try:
                while True:
                    b1, b2 = self._read(conn, 2)
                    n = b2 & 0x7F
                    assert b2 & 0x80, "client frames must be masked"
                    if n == 126:
                        n = struct.unpack(">H", self._read(conn, 2))[0]
                    elif n == 127:
                        n = struct.unpack(">Q", self._read(conn, 8))[0]
                    mask = self._read(conn, 4)
                    text = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(conn, n))).decode("utf-8")
                    self.received.append(text)
                    if text == "ping-then-echo":
                        conn.sendall(self.frame(b"", op=9) + self.frame(b"pong-after-ping"))
                    elif text == "fragment":
                        conn.sendall(self.frame(b"frag", op=1, fin=False) + self.frame(b"ment", op=0, fin=True))
                    elif text == "big":
                        conn.sendall(self.frame(("é" * 40000).encode("utf-8")))
                    elif text == "close":
                        conn.sendall(self.frame(b"", op=8))
                    else:
                        conn.sendall(self.frame(text.encode("utf-8")))
            except (ConnectionError, OSError):
                pass

    def close(self):
        self.srv.close()


def test_websocket_client_handles_sizes_pings_fragments_and_close():
    server = MiniWebSocketServer()
    ws = eng.WS(f"ws://127.0.0.1:{server.port}/devtools/page/X", timeout=10)
    try:
        for text in ("hi", "x" * 200, "ü" * 40000):                  # <126, 16-bit length, 64-bit length (client side)
            ws.send(text)
            assert ws.recv() == text
        ws.send("ping-then-echo")
        assert ws.recv() == "pong-after-ping"
        ws.send("fragment")
        assert ws.recv() == "fragment"
        ws.send("big")
        assert ws.recv() == "é" * 40000
        ws.send("close")
        with pytest.raises(ConnectionError):
            ws.recv()
    finally:
        ws.close()
        server.close()
    assert server.received[0] == "hi" and len(server.received[2]) == 40000


def test_websocket_handshake_refusal_is_an_error():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def refuse():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        conn.close()

    threading.Thread(target=refuse, daemon=True).start()
    try:
        with pytest.raises(ConnectionError, match="refused"):
            eng.WS(f"ws://127.0.0.1:{srv.getsockname()[1]}/x", timeout=5)
    finally:
        srv.close()


def test_browser_call_and_eval_speak_the_devtools_protocol():
    """Browser.call matches replies by id (skipping events) and eval turns script exceptions into errors."""
    sent = []
    replies = [{"method": "Some.event", "params": {}}, {"id": 1, "result": {"result": {"value": 42}}},
               {"id": 2, "result": {"exceptionDetails": {"text": "x", "exception": {"description": "ReferenceError: nope"}}}},
               {"id": 3, "error": {"message": "no such method"}}]

    class Wire:
        def send(self, text):
            sent.append(json.loads(text))

        def recv(self):
            return json.dumps(replies.pop(0))

    b = eng.Browser.__new__(eng.Browser)
    b.ws, b.id = Wire(), 0
    assert b.eval("6*7") == 42
    assert sent[0] == {"id": 1, "method": "Runtime.evaluate", "params": {"expression": "6*7", "returnByValue": True}}
    with pytest.raises(RuntimeError, match="ReferenceError"):
        b.eval("nope")
    with pytest.raises(RuntimeError, match="no such method"):
        b.call("Bad.method")


# ------------------------------------------------------------------------------------------- end to end, real Chromium
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def chrome_processes_using(profile):
    """pids of any process whose command line mentions this profile (Linux /proc); [] when /proc is not there."""
    found = []
    try:
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        if str(profile).encode() in f.read():
                            found.append(int(pid))
                except OSError:
                    pass
    except OSError:
        pass
    return found


class RealBrowsers:
    """browser_factory for the end-to-end tests: real Chromium, remembered so it can be killed whatever happens."""

    def __init__(self):
        self.created, self.failure = [], None

    def __call__(self, ctx):
        try:
            browser = eng.open_browser(ctx)
        except Exception as exc:
            self.failure = exc
            raise
        self.created.append(browser)
        return browser

    def kill_all(self):
        for browser in self.created:
            browser.close()


@pytest.fixture
def e2e(tmp_path, monkeypatch):
    if eng.find_browser("") is None:
        pytest.skip("no Chrome / Chromium on this machine")
    profile = tmp_path / "profile"
    monkeypatch.setattr(eng, "profile_dir", lambda: str(profile))
    for name, value in (("ROLE_SETTLE", 0.3), ("LIST_SETTLE", 0.3), ("POLL", 0.2), ("LIST_POLL", 0.3), ("SCOREBOARD_TIMEOUT", 15),
                        ("FRESH_TIMEOUT", 8), ("LOGIN_TIMEOUT", 8), ("PAGE_TIMEOUT", 20), ("LIST_TIMEOUT", 8)):
        monkeypatch.setattr(eng, name, value)
    browsers = RealBrowsers()
    with FakeITSM() as site:
        try:
            yield site, browsers, profile
        finally:
            browsers.kill_all()
            for b in browsers.created:
                assert b.proc.poll() is not None, "Chromium was left running"
    assert chrome_processes_using(profile) == [], "Chromium helper processes were left running"


def e2e_ctx(tmp_path, site, roles, **kw):
    cfg = make_settings(roles=roles)
    cfg.update(url=site.url, cdp_port=free_port())
    return make_ctx(tmp_path, cfg, **kw)


def skip_if_browser_failed(browsers, result):
    if browsers.failure is not None:
        pytest.skip(f"Chromium cannot be started here: {browsers.failure}")
    assert result.error is None or "Chromium" not in result.error


def test_end_to_end_real_chromium_against_the_fake_site(tmp_path, e2e):
    site, browsers, profile = e2e
    roles = [ROLE_A, ROLE_B, "IMP No Such Role", ROLE_EMPTY]
    clock = Clock(datetime(2026, 10, 11, 9, 0))
    mailer = FakeMailer()
    ctx = e2e_ctx(tmp_path, site, roles, clock=clock, mailer=mailer, debug=True)
    ctx.settings["mail"] = {"send": True, "send_times": ["08:30"], "to": ["boss@example.test"], "cc": [], "display_only": False}
    try:
        result = eng.run(ctx, browser_factory=browsers)
    finally:
        browsers.kill_all()
    skip_if_browser_failed(browsers, result)

    assert result.ok, (result.error, ctx.logs)
    assert site.login_attempts == [("1001", True)]
    assert site.untrusted_clicks == 0                       # the real mouse click opened every list, not el.click()
    data = result.snapshot_data
    role_a, role_b, missing, empty = data["roles"]
    assert role_a["categories"][IMPLEMENTATION] == {"count": 3, "warning": None, "note": None, "rows": [
        {"number": "111111", "summary": "Create user for branch 12"},
        {"number": "222222", "summary": "طلب تفعيل مستخدم جديد"},
        {"number": "333333", "summary": "Cut short in the cell, but the full text is in the title"}]}
    assert role_a["categories"][ROLLBACK]["rows"] == [{"number": "444444", "summary": "<script>alert(1)</script> & rollback"}]
    assert [r["number"] for r in role_b["categories"][IMPLEMENTATION]["rows"]] == ["555555", "666666"]
    assert role_b["categories"][ROLLBACK]["count"] is None
    assert missing == {"role": "IMP No Such Role", "error": "Role 'IMP No Such Role' not found in the dropdown", "categories": {}}
    assert empty["categories"][IMPLEMENTATION] == {"count": 0, "rows": [], "warning": None, "note": None}
    assert data["totals"] == {IMPLEMENTATION: 5, ROLLBACK: 1}
    assert data["changes"] == {"new": [], "closed": []}
    assert schemas.validate_snapshot(schemas.make_envelope("itsm", data, "2026-10-11T09:00:00")) == []
    # the slot (08:30) had passed: one mail, flagged because a role failed, with the portal text escaped
    assert result.emails_sent == 1 and "CHECK REQUIRED" in mailer.sent[0]["subject"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; rollback" in mailer.sent[0]["html"]
    assert "<script>alert(1)" not in mailer.sent[0]["html"]
    # debug dump of the failed role, produced by the page script in the real browser: all frames, button labels, no secrets
    dump = tmp_path / "data" / "ops" / "debug" / "itsm" / "itsm_debug_IMP_No_Such_Role.json"
    doc = json.loads(dump.read_text(encoding="utf-8"))
    assert len(doc["frames"]) == 4 and any("Scoreboard as of" in f.get("text", "") for f in doc["frames"])
    labels = [e["text"] for f in doc["frames"] for e in f.get("elements", [])]
    assert "Set Role" in labels and PASSWORD not in dump.read_text(encoding="utf-8")
    assert mailer.sent[0]["attachments"] == [str(dump)]
    # the password reached only the login form of the site
    everything = json.dumps([result.snapshot_data, result.history_point, ctx.logs, mailer.sent, ctx.store.read_state("itsm")])
    assert PASSWORD not in everything
    assert PASSWORD not in (ctx.store.report_dir("itsm") / "itsm_latest.html").read_text(encoding="utf-8")

    # a second scan, a little later: 222222 was closed, 999999 is new (the state of the first scan is the baseline)
    site.roles[ROLE_A][IMPLEMENTATION]["rows"][1] = ("999999", "brand new request")
    clock.value = datetime(2026, 10, 11, 9, 30)
    ctx2 = e2e_ctx(tmp_path, site, roles, clock=clock, mailer=mailer)
    try:
        second = eng.run(ctx2, browser_factory=browsers)
    finally:
        browsers.kill_all()
    assert second.ok, (second.error, ctx2.logs)
    assert second.snapshot_data["changes"]["new"] == [
        {"role": ROLE_A, "category": IMPLEMENTATION, "number": "999999", "summary": "brand new request"}]
    assert second.snapshot_data["changes"]["closed"] == [
        {"role": ROLE_A, "category": IMPLEMENTATION, "number": "222222", "summary": "طلب تفعيل مستخدم جديد"}]
    assert second.emails_sent == 0                          # that slot was already mailed
    assert len(browsers.created) == 2


def test_end_to_end_wrong_password_is_reported_as_auth_and_chromium_is_closed(tmp_path, e2e):
    site, browsers, profile = e2e
    ctx = e2e_ctx(tmp_path, site, [ROLE_A], password="not-the-password")
    try:
        result = eng.run(ctx, browser_factory=browsers)
    finally:
        browsers.kill_all()
    skip_if_browser_failed(browsers, result)
    assert not result.ok and result.error_kind == "auth" and result.snapshot_data is None
    assert "not accepted" in result.error and "not-the-password" not in result.error
    assert site.login_attempts == [("1001", False)]          # exactly one attempt
    assert ctx.store.read_state("itsm") == {}


def test_end_to_end_browser_is_killed_when_the_scan_blows_up(tmp_path, e2e, monkeypatch):
    site, browsers, profile = e2e
    ctx = e2e_ctx(tmp_path, site, [ROLE_A])

    def explode(self):
        raise RuntimeError("simulated crash after the browser started")

    monkeypatch.setattr(eng.Scanner, "login", explode)
    try:
        result = eng.run(ctx, browser_factory=browsers)
    finally:
        browsers.kill_all()
    skip_if_browser_failed(browsers, result)
    assert not result.ok and "simulated crash" in result.error
    assert browsers.created and browsers.created[0].proc.poll() is not None
