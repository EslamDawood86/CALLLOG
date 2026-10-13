"""The E-mail page (calllog/static/email.js): who may open it, the strict-CSP / textContent source rules, and - when
Chromium is available - a real-browser run over the demo data (open it, read the KPI tiles, switch tabs, mark an e-mail
"no action" through the UI and see the override file appear). The browser parts skip cleanly when Chromium cannot start.

The browser fixtures (chromium, server, demo_data, tab, Tab) and a few helpers are shared with test_ops_views.py."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# fixtures/helpers shared with the front-end foundation tests
from test_ops_views import (Tab, chromium, demo, demo_data, emp, make_account, mgr, not_yet_written,  # noqa: F401
                            server, tab, text_of)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "calllog" / "static"


# --------------------------------------------------------------------- gating and shell
def test_email_renders_for_admin_and_manager(admin, mgr):
    for who in (admin, mgr):
        html = who.get("/email").get_data(as_text=True)
        assert 'id="email-page"' in html and html.count("<main") == 1
        assert "/static/email.js" in html and "/static/ops-common.js" in html and "/static/common.js" in html


def test_employee_never_reaches_the_email_page(emp):
    resp = emp.get("/email")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/my")


# --------------------------------------------------------------------- the source follows the CSP rules
FORBIDDEN = [r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write", r"\beval\(", r"new Function",
             r"setAttribute\(\s*[\"']style", r"\.cssText", r"\bstyle\s*=", r"javascript:", r"<script"]


def test_email_js_uses_textcontent_and_no_inline_style_or_eval():
    text = (STATIC / "email.js").read_text(encoding="utf-8")
    assert text.startswith('"use strict";')
    for pattern in FORBIDDEN:
        assert not re.search(pattern, text), f"email.js: {pattern}"
    assert not re.findall(r"https?://", text.replace("http://www.w3.org/2000/svg", "")), "no external address"


def test_email_css_is_self_contained():
    css = (STATIC / "ops-email.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}")
    assert "@import" not in css and not re.search(r"url\(\s*[\"']?https?:", css) and "@font-face" not in css


def test_email_js_syntax():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "--check", str(STATIC / "email.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


# ============================================================================================ real browser
def panel(page):
    return ".em-panel:not([hidden])"


def test_email_overview_and_tabs_in_a_browser(admin, demo_data, tab):
    t = tab()
    page = t.open("/email", "#email-page .em-kpis .op-kpi")
    api = admin.get("/api/ops/email").get_json()["data"]["stats"]
    # KPI tiles carry the demo snapshot's numbers (business e-mails, replied, pending)
    assert page.locator("#email-page .em-kpis .op-kpi").count() >= 8
    kpis = text_of(page, "#email-page .em-kpis")
    assert str(api["business_emails"]) in kpis and str(api["replied"]) in kpis
    # the Pending and Unclassified tabs carry a count badge
    assert page.inner_text('.em-tab:has-text("Pending") .em-badge') == str(api["pending"])
    # every tab opens without a console error and shows its panel
    for label, ready in (("Pending", ".op-table tbody tr"), ("Replied", ".op-table tbody tr"),
                         ("Quality", ".em-kpis .op-kpi"), ("Trends", ".card"), ("Unclassified", ".card"),
                         ("Overview", ".em-kpis")):
        page.click(f'.em-tab:has-text("{label}")')
        page.wait_for_selector(f"{panel(page)} {ready}")
    assert t.later_problems(*not_yet_written()) == [], t.problems


def test_email_pending_action_writes_an_override(app, admin, ops_store, demo_data, tab):
    t = tab()
    page = t.open("/email", '.em-tab:has-text("Pending")')
    page.click('.em-tab:has-text("Pending")')
    page.wait_for_selector(f"{panel(page)} .op-table tbody tr")
    before = int(page.inner_text('.em-tab:has-text("Pending") .em-badge'))
    marked_before = len(ops_store.overrides("no_action"))
    page.click(f"{panel(page)} .op-table tbody tr:first-child button:has-text('No action')")
    page.wait_for_selector("dialog.dlg")
    page.click("dialog.dlg button:has-text('Mark no action')")
    page.wait_for_selector("#toast .toast-msg")
    assert "no action" in text_of(page, "#toast").lower()
    # the override reached the store and the pending badge dropped by one
    page.wait_for_function("(n) => { const b = document.querySelector('.em-tab .em-badge'); return b && +b.textContent === n; }", arg=before - 1)
    assert len(ops_store.overrides("no_action")) == marked_before + 1
    assert t.later_problems(*not_yet_written()) == [], t.problems


def test_email_is_scoped_for_a_manager(app, demo_data, tab):
    t = tab(demo.MANAGER_ID, demo_data["password"])
    page = t.open("/email", "#email-page .em-kpis .op-kpi")
    page.click('.em-tab:has-text("Pending")')
    page.wait_for_selector(f"{panel(page)} .op-table tbody tr, {panel(page)} .op-empty")
    teams = set(page.locator(f"{panel(page)} .op-table tbody tr td:nth-child(4)").all_inner_texts())
    assert teams <= {"Flex Cube", ""}                                  # the Flex Cube manager sees only their team
    # a manager of one team never sees the Unclassified bucket
    page.click('.em-tab:has-text("Unclassified")')
    page.wait_for_selector(f"{panel(page)} .op-empty, {panel(page)} .op-table")
    assert "Nothing unclassified" in text_of(page, panel(page)) or page.locator(f"{panel(page)} .op-table tbody tr").count() == 0
    assert t.later_problems(*not_yet_written()) == [], t.problems


@pytest.fixture
def ops_store(app):
    return app.extensions["ops"]
