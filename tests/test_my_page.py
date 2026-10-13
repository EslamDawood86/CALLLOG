"""The My work page (calllog/static/my.js): who may open it, the strict-CSP / textContent source rules, and - when
Chromium is available - a real-browser run over the demo data (an employee sees their own numbers, never another
person's rows; an administrator can open it read-only with ?as=<id>). The browser parts skip without Chromium.

The browser fixtures and helpers are shared with test_ops_views.py."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import Browser
from test_ops_views import (Tab, chromium, demo, demo_data, emp, make_account, mgr, not_yet_written,  # noqa: F401
                            server, tab, text_of)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "calllog" / "static"


# --------------------------------------------------------------------- gating and shell
def test_my_renders_for_an_employee_and_references_my_js(emp):
    html = emp.get("/my").get_data(as_text=True)
    assert 'id="my-page"' in html and "/static/my.js" in html and "/static/ops-common.js" in html


def test_admin_needs_a_person_to_view(admin):
    assert admin.get("/my").headers["Location"].endswith("/hub")             # an admin must say whose page
    assert admin.get("/my?as=nobody").status_code == 404


# --------------------------------------------------------------------- the source follows the CSP rules
FORBIDDEN = [r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write", r"\beval\(", r"new Function",
             r"setAttribute\(\s*[\"']style", r"\.cssText", r"\bstyle\s*=", r"javascript:", r"<script"]


def test_my_js_uses_textcontent_and_no_inline_style_or_eval():
    text = (STATIC / "my.js").read_text(encoding="utf-8")
    assert text.startswith('"use strict";')
    for pattern in FORBIDDEN:
        assert not re.search(pattern, text), f"my.js: {pattern}"
    assert not re.findall(r"https?://", text.replace("http://www.w3.org/2000/svg", "")), "no external address"


def test_my_css_is_self_contained():
    css = (STATIC / "ops-my.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}")
    assert "@import" not in css and not re.search(r"url\(\s*[\"']?https?:", css) and "@font-face" not in css


def test_my_js_syntax():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "--check", str(STATIC / "my.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


# ============================================================================================ real browser
def test_my_work_shows_the_employees_own_numbers(app, demo_data, tab):
    uid = demo_data["employees"][0]
    me = Browser(app)
    me.login(uid, demo_data["password"])
    api = me.get("/api/ops/my").get_json()
    t = tab(uid, demo_data["password"])
    page = t.open("/my", ".my-stack .card")
    page.wait_for_selector("#myStrip .op-strip-group")
    # my calls today equal the API, and the Team status strip is present (the same component as Home)
    assert api["calls"]["today"]["total"] > 0
    assert str(api["calls"]["today"]["total"]) in text_of(page, ".my-stack .card:first-child")
    assert page.locator("#myStrip .op-strip-group").count() == 5
    # the "Log a call" quick link is there for the employee
    assert page.locator('a[href="/dashboard"]').count() == 1
    assert t.later_problems(*not_yet_written()) == [], t.problems


def test_my_work_never_shows_another_employees_rows(app, demo_data, tab):
    one, two = demo_data["employees"][0], demo_data["employees"][1]
    mine = Browser(app)
    mine.login(one, demo_data["password"])
    other = Browser(app)
    other.login(two, demo_data["password"])
    # a customer name that is in the other person's assigned mail must not appear on my page
    other_names = {i.get("customer_name") for i in other.get("/api/ops/my").get_json()["email"]["assigned"]["items"]}
    my_names = {i.get("customer_name") for i in mine.get("/api/ops/my").get_json()["email"]["assigned"]["items"]}
    unique_to_other = [n for n in other_names - my_names if n]
    t = tab(one, demo_data["password"])
    page = t.open("/my", ".my-stack .card")
    page.wait_for_selector("#myStrip .op-strip-group")
    body = text_of(page, "#my-page")
    for name in unique_to_other:
        assert name not in body, name
    assert t.later_problems(*not_yet_written()) == [], t.problems


def test_admin_view_as_banner_and_read_only(admin, demo_data, tab):
    uid = demo_data["employees"][0]
    t = tab()                                                                 # signed in as admin
    page = t.open(f"/my?as={uid}", ".my-stack .card")
    assert "read only" in text_of(page, ".op-viewing").lower()
    assert page.locator('a[href="/dashboard"]').count() == 0                  # no "Log a call" when viewing someone else
    assert t.later_problems(*not_yet_written()) == [], t.problems
