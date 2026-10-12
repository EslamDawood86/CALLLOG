"""The SLA (/sla) and ITSM (/itsm) pages: the two shells, the front-end rules, and a real-browser run (Chromium through
Playwright) against the app with a snapshot written the way the collector writes it.

Fake data only (made-up Arabic names, user numbers 9000+, example.test addresses). The browser tests skip themselves
when Chromium cannot start; everything else runs anywhere. The server clock is the conftest `clock` (frozen, advanced by
the tests), so ages and "x min ago" are exact.

Opt-in screenshots for the docs:  CALLLOG_SCREENSHOTS=<folder> pytest tests/test_sla_itsm_pages.py -k screenshots
"""
import datetime as dt
import os
import re
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from calllog.ops import schemas

NOW = dt.datetime(2026, 10, 11, 9, 0, 0)                   # the conftest `clock`
STATIC = Path(__file__).resolve().parent.parent / "calllog" / "static"
PAGE_SCRIPTS = ("sla.js", "itsm.js")
USERS = ["أحمد سالم", "منى علي", "خالد حسن", "نور ابراهيم"]
IMPL, ROLLBACK = "Implementation Requests", "Rollback Requests"
ROLE_A, ROLE_B, ROLE_C, ROLE_D = "IMP User provisioning", "IMP Card Management", "IMP UAT I-Flex SMS", "IMP Open Systems"


# ---------------------------------------------------------------------------------------------- data builders
def iso(moment):
    return moment.isoformat(timespec="seconds")


def sla_snapshot(thresholds=(60, 120), now=NOW):
    """31 open requests: 4 breached, 7 reminder, 19 within, 1 without a sent date (ages are whole minutes before `now`)."""
    warn, breach = thresholds
    ages = [("breached", a) for a in (250, 200, 150, 125)] + [("reminder", a) for a in (118, 110, 100, 95, 90, 80, 70)]
    ages += [("within", a) for a in (58, 56, 54, 50, 47, 44, 41, 39, 36, 33, 30, 28, 25, 22, 19, 15, 11, 7, 3)] + [("no_date", None)]
    reqs = []
    for i, (status, age) in enumerate(ages):
        sent = iso(now - dt.timedelta(minutes=age)) if age is not None else None
        reqs.append({"key": f"k{i:03d}", "security_user": USERS[i % 4], "sent_at": sent, "age_minutes": age, "status": status,
                     "fields": {"اسم الموظف": f"موظف رقم {i}", "رقم المستخدم": str(9000 + i), "الفرع / الادارة": f"فرع {i + 1}",
                                "النظام": ["Flexcube", "T24"][i % 2], "نوع الطلب": "اضافة صلاحية", "موظف الاسناد": f"Support {i % 3}"}})
    return {"thresholds": {"warning_minutes": warn, "breach_minutes": breach}, "totals": totals_of(reqs), "by_user": by_user(reqs),
            "requests": sorted(reqs, key=lambda r: -(r["age_minutes"] if r["age_minutes"] is not None else -1))}


def totals_of(reqs):
    totals = {"open": len(reqs), "oldest_minutes": max((r["age_minutes"] or 0) for r in reqs) if reqs else 0}
    totals.update({s: sum(1 for r in reqs if r["status"] == s) for s in schemas.SLA_STATUSES})
    return totals


def by_user(reqs):
    rows = []
    for user in dict.fromkeys(r["security_user"] for r in reqs):
        mine = [r for r in reqs if r["security_user"] == user]
        rows.append({"user": user, "total": len(mine), **{s: sum(1 for r in mine if r["status"] == s) for s in schemas.SLA_STATUSES},
                     "oldest_minutes": max((r["age_minutes"] or 0) for r in mine)})
    return sorted(rows, key=lambda u: (-u["breached"], -u["reminder"], -u["total"]))


def write_sla(ops, data, at=NOW, history=True):
    ops.write_snapshot("sla", data, generated_at=iso(at), host="PC-TEST", duration_seconds=12.4)
    if history:
        for minutes, (opened, breached) in zip((0, 15, 30, 45, 60), ((20, 0), (22, 1), (25, 2), (27, 3), (31, 4))):
            ts = iso(dt.datetime(2026, 10, 11, 8, 0) + dt.timedelta(minutes=minutes))
            first = {"k000": iso(dt.datetime(2026, 10, 11, 8, 30)), "k001": iso(dt.datetime(2026, 10, 11, 8, 45)),
                     "k002": iso(dt.datetime(2026, 10, 11, 8, 45))} if minutes == 60 else {}
            ops.add_history_point("sla", "2026-10-11", {"ts": ts, "open": opened, "within": opened - breached - 5, "reminder": 5,
                                                         "breached": breached}, extra={"first_breach": first})


def itsm_snapshot(error_role=False, now=NOW):
    rows = lambda base, n: [{"number": str(base + i), "summary": s} for i, s in
                            zip(range(n), ["Add user to Flexcube group", "فتح صلاحية على النظام", "Reset profile limits", "Deploy patch to UAT", "Service account"])]
    cat = lambda count, rows_=(), warning=None, note=None: {"count": count, "rows": list(rows_), "warning": warning, "note": note}
    roles = [{"role": ROLE_A, "error": None, "categories": {IMPL: cat(3, rows(100100, 3)), ROLLBACK: cat(1, rows(100200, 1))}},
             {"role": ROLE_B, "error": None, "categories": {IMPL: cat(0), ROLLBACK: cat(0)}},
             {"role": ROLE_C, "error": None, "categories": {IMPL: cat(12, rows(100300, 5), note="Showing 5 of 12 requests (list is paged)."),
                                                              ROLLBACK: cat(None)}}]
    if error_role:
        roles.append({"role": ROLE_D, "error": "Set Role button not found", "categories": {}})
    return {"categories": [IMPL, ROLLBACK], "roles": roles, "totals": {IMPL: 15, ROLLBACK: 1},
            "changes": {"new": [{"role": ROLE_A, "category": ROLLBACK, "number": "100200", "summary": "Rollback of the patch"}],
                        "closed": [{"role": ROLE_C, "category": IMPL, "number": "099999", "summary": "طلب تم اغلاقه"}]}}


def write_itsm(ops, data, at=NOW):
    ops.write_snapshot("itsm", data, generated_at=iso(at), host="PC-TEST", duration_seconds=52)
    for minutes, (impl, rollback) in zip((0, 30, 60), ((10, 0), (12, 1), (15, 1))):
        ts = iso(dt.datetime(2026, 10, 11, 8, 0) + dt.timedelta(minutes=minutes))
        ops.add_history_point("itsm", "2026-10-11", {"ts": ts, "totals": {IMPL: impl, ROLLBACK: rollback}, "per_role": {}})


def beat(ops, job, at=NOW, **fields):
    ops.write_json(ops.heartbeat_dir / f"{job}.json", {
        "job": job, "host": "PC-TEST", "running": False, "enabled": True, "last_status": "ok", "last_error": None,
        "last_ok_at": iso(at), "last_run_at": iso(at), "next_run_at": iso(at + dt.timedelta(minutes=15)),
        "updated_at": iso(at), "updated_ts": at.timestamp(), **fields})


# ---------------------------------------------------------------------------------------------- fixtures
@pytest.fixture
def ops(app):
    return app.extensions["ops"]


@pytest.fixture
def hub(ops):
    """Both modules scanned a moment ago, collectors alive."""
    sla = sla_snapshot()
    write_sla(ops, sla)
    write_itsm(ops, itsm_snapshot())
    for job in schemas.JOBS:
        beat(ops, job)
    return {"sla": sla}


@pytest.fixture
def accounts(app, admin):
    """uid -> password of a signed-in-able administrator, manager and employee (no forced password change)."""
    store = app.extensions["store"]
    for uid, role in (("mona", "manager"), ("1001", "user")):
        r = admin.post("/api/admin/users", {"user_id": uid, "name": uid.title(), "department": "IT", "role": role,
                                            "password": "TempPass123", "team": "__all__" if role == "manager" else ""})
        assert r.status_code == 201, r.get_json()
        store.update_user(uid, {"must_change_password": False})
    return {"admin": "AdminPass123", "mona": "TempPass123", "1001": "TempPass123"}


def signed_in(app, uid, password):
    from conftest import Browser
    b = Browser(app)
    assert b.login(uid, password).status_code == 302
    return b


# ---------------------------------------------------------------------------------------------- shells and files
@pytest.mark.parametrize("path,script", [("/sla", "sla.js"), ("/itsm", "itsm.js")])
def test_shell_for_admin_and_manager_not_for_employee(app, admin, accounts, hub, path, script):
    for uid in ("admin", "mona"):
        page = admin if uid == "admin" else signed_in(app, uid, accounts[uid])
        resp = page.get(path)
        assert resp.status_code == 200, uid
        html = resp.get_data(as_text=True)
        assert f"/static/{script}" in html and "/static/common.js" in html
        assert 'name="csrf-token"' in html
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline script would break the CSP"
        assert not re.search(r"\sstyle=", html), "inline style attribute would break the CSP"
    employee = signed_in(app, "1001", accounts["1001"])
    resp = employee.get(path)
    assert resp.status_code in (302, 403) and script not in resp.get_data(as_text=True)
    anonymous = app.test_client().get(path)
    assert anonymous.status_code in (302, 401) and script not in anonymous.get_data(as_text=True)


@pytest.mark.parametrize("name", ["sla.js", "itsm.js", "ops-sla.css"])
def test_assets_are_served_and_self_contained(admin, name):
    resp = admin.get(f"/static/{name}")
    assert resp.status_code == 200 and len(resp.get_data()) > 2000
    text = resp.get_data(as_text=True)
    assert "http://" not in text and "https://" not in text, "no CDN or external address (the bank network is isolated)"
    if name.endswith(".css"):
        assert "@import" not in text and "url(" not in text


def code_of(name):
    """The script without its comments (a comment may say "never innerHTML")."""
    text = (STATIC / name).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)(^|\s)//[^\n]*$", "", text)


@pytest.mark.parametrize("name", PAGE_SCRIPTS)
def test_scripts_never_build_html_from_data(name):
    """Scraped text is untrusted: the pages only use textContent / text nodes (CL.h), never markup strings."""
    code = code_of(name)
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setAttribute(\"style\"",
                   "setAttribute('style'", ".srcdoc"):
        assert banned not in code, f"{name} uses {banned}"
    assert 'dir: "auto"' in code or "bidi(" in code          # Arabic text is shown with dir=auto


@pytest.mark.parametrize("name", PAGE_SCRIPTS)
def test_scripts_hold_no_secrets_or_personal_data(name):
    code = code_of(name).lower()
    assert "password" not in code and "@nbe." not in code and "token" not in code.replace("csrf", "")


def test_history_answer_has_what_the_timeline_reads(admin, hub):
    day = admin.get("/api/ops/sla/history?days=1").get_json()["days"][-1]
    assert day["day"] == "2026-10-11" and len(day["points"]) == 5 and set(day["points"][0]) >= {"ts", "open", "within", "reminder", "breached"}
    assert len(day["first_breach"]) == 3
    itsm_day = admin.get("/api/ops/itsm/history?days=1").get_json()["days"][-1]
    assert itsm_day["points"][-1]["totals"] == {IMPL: 15, ROLLBACK: 1}


# ---------------------------------------------------------------------------------------------- real browser plumbing
@pytest.fixture(scope="module")
def chromium():
    sync = pytest.importorskip("playwright.sync_api")
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "")
    found = list(root.glob("chromium-*/chrome-linux/chrome")) + list(root.glob("chromium_headless_shell-*/chrome-linux/headless_shell"))
    candidates = [""] + [str(p) for p in sorted(found, reverse=True)]
    pw = sync.sync_playwright().start()
    errors, browser = [], None
    for exe in candidates:
        try:
            browser = pw.chromium.launch(**({"executable_path": exe} if exe else {}))
            break
        except Exception as exc:                          # no binary, no sandbox permission, ...
            errors.append((str(exc).strip().splitlines() or [repr(exc)])[0])
    if browser is None:
        pw.stop()
        pytest.skip("Chromium cannot start here: " + " | ".join(errors))
    yield browser
    browser.close()
    pw.stop()


@pytest.fixture
def server(app):
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    thread.join(5)


class Tab:
    """One signed-in browser window that remembers everything that went wrong in it (console errors, CSP, failed requests)."""

    def __init__(self, browser, base, scheme="light", width=1440, height=900, **context):
        self.base = base
        self.context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, **context)
        self.page = self.context.new_page()
        self.problems = []

    def watch(self):
        page = self.page
        page.on("console", lambda m: self.problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.problems.append(f"page error: {e}"))
        page.on("requestfailed", lambda r: self.problems.append(f"request failed: {r.url}"))
        page.on("response", lambda r: self.problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)

    def login(self, uid, password):
        page = self.page
        page.goto(f"{self.base}/login")
        page.fill("input[name=user_id]", uid)
        page.fill("input[name=password]", password)
        page.click("button[type=submit]")
        page.wait_for_load_state()
        self.watch()
        return self

    def open(self, path, ready=".sx-page"):
        self.page.goto(f"{self.base}{path}")
        self.page.wait_for_selector(ready)
        return self.page

    def poll_now(self):
        """What the browser does when the tab becomes visible again: ask for news at once."""
        self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

    def clean(self):
        assert self.problems == []

    def close(self):
        self.context.close()


@pytest.fixture
def tab(chromium, server, accounts):
    tabs = []

    def make(uid="admin", **kw):
        t = Tab(chromium, server, **kw).login(uid, accounts[uid])
        tabs.append(t)
        return t
    yield make
    for t in tabs:
        t.close()


def expect(locator):
    """Playwright's retrying assertion (it does not use eval, which the page's CSP would refuse)."""
    from playwright.sync_api import expect as playwright_expect
    return playwright_expect(locator)


def text(page, selector):
    return page.locator(selector).first.inner_text().strip()


def svg_text(page, selector):
    return page.locator(selector).first.text_content()


def kpi(page, key):
    return text(page, f'[data-kpi="{key}"] [data-kpi-value]')


def rows(page):
    return page.locator(".sx-requests tbody tr")


def statuses(page):
    return [r.get_attribute("data-status") for r in rows(page).all()]


# ---------------------------------------------------------------------------------------------- SLA page
def test_sla_tiles_equal_the_scan(tab, hub):
    t = tab()
    page = t.open("/sla", '[data-kpi="open"]')
    totals = hub["sla"]["totals"]
    assert totals["open"] == 31 and totals["breached"] == 4
    for key in ("open", "within", "reminder", "breached", "no_date"):
        assert kpi(page, key) == str(totals[key]), key
    assert "Reminder ≥ 1 h" in text(page, '[data-kpi="reminder"]') and "Breached ≥ 2 h" in text(page, '[data-kpi="breached"]')
    assert abs(int(page.locator('[data-kpi="oldest"]').get_attribute("data-minutes")) - totals["oldest_minutes"]) <= 1
    assert kpi(page, "oldest") == "4 h 10 min" and USERS[0] in text(page, '[data-kpi="oldest"]')
    assert "Live" in text(page, ".sx-fresh") and "09:00" in text(page, ".sx-fresh") and "PC-TEST" in text(page, ".sx-fresh")
    t.clean()


def test_sla_thresholds_come_from_the_scan(ops, tab):
    write_sla(ops, sla_snapshot(thresholds=(30, 90)))
    for job in schemas.JOBS:
        beat(ops, job)
    page = tab().open("/sla", '[data-kpi="open"]')
    assert "Reminder ≥ 30 min" in text(page, '[data-kpi="reminder"]') and "Breached ≥ 90 min" in text(page, '[data-kpi="breached"]')


def test_sla_user_table_matches_and_sorts(tab, hub):
    t = tab()
    page = t.open("/sla", ".sx-users tbody tr")
    expected = hub["sla"]["by_user"]
    table = page.locator(".sx-users tbody tr")
    assert table.count() == len(expected)
    for row, user in zip(table.all(), expected):
        cells = [c.inner_text().strip() for c in row.locator("td").all()]
        assert row.get_attribute("data-user") == user["user"]
        assert cells[:4] == [str(user["total"]), str(user["within"]), str(user["reminder"]), str(user["breached"])]
    page.click('.sx-users button.sx-sort:has-text("Total")')
    assert page.locator('.sx-users th[aria-sort="descending"]').inner_text().strip().startswith("Total")
    totals = [int(r.locator("td").first.inner_text()) for r in table.all()]
    assert totals == sorted(totals, reverse=True)
    page.click('.sx-users button.sx-sort:has-text("Security user")')
    names = [r.get_attribute("data-user") for r in table.all()]
    assert page.locator('.sx-users th[aria-sort="ascending"]').count() == 1 and len(names) == 4
    t.clean()


def test_sla_breached_first_then_filter_by_status_and_user(tab, hub):
    t = tab()
    page = t.open("/sla", ".sx-requests tbody tr")
    assert statuses(page)[:4] == ["breached"] * 4 and statuses(page)[4] == "reminder"
    assert text(page, ".sx-count") == "31 open requests"

    page.click('[data-kpi="breached"]')                                      # a tile filters like the drop-down
    assert statuses(page) == ["breached"] * 4 and text(page, ".sx-count") == "4 of 31 requests"
    assert page.locator('[data-kpi="breached"]').get_attribute("aria-pressed") == "true"
    page.click('[data-kpi="open"]')
    assert rows(page).count() == 25
    page.select_option('select[aria-label="Status"]', "reminder")
    assert statuses(page) == ["reminder"] * 7

    page.select_option('select[aria-label="Status"]', "")
    page.click(f'.sx-users tr[data-user="{USERS[0]}"] button.sx-link')       # click a user in the table
    mine = [r for r in hub["sla"]["requests"] if r["security_user"] == USERS[0]]
    assert text(page, ".sx-count") == f"{len(mine)} of 31 requests" and rows(page).count() == len(mine)
    assert all(USERS[0] in r.locator("td").nth(1).inner_text() for r in rows(page).all())
    assert page.locator(".sx-users tr.is-selected").count() == 1
    page.select_option('select[aria-label="Status"]', "breached")             # both filters together
    assert rows(page).count() == sum(1 for r in mine if r["status"] == "breached")
    page.click("text=Clear filters >> nth=0")
    assert text(page, ".sx-count") == "31 open requests" and page.locator(".sx-users tr.is-selected").count() == 0
    t.clean()


def test_sla_search_ignores_arabic_spelling_and_says_when_nothing_matches(tab, hub):
    t = tab()
    page = t.open("/sla", ".sx-requests tbody tr")
    page.fill('input[aria-label="Search requests"]', "9007")
    expect(rows(page)).to_have_count(1)
    assert "9007" in rows(page).first.inner_text()
    page.fill('input[aria-label="Search requests"]', "احمد")                   # data has the hamza spelling "أحمد"
    expect(rows(page)).to_have_count(8)                                            # the 8 requests of that user
    assert all(USERS[0] in r.locator("td").nth(1).inner_text() for r in rows(page).all())
    page.fill('input[aria-label="Search requests"]', "no such request")
    page.wait_for_selector("text=No requests match these filters.")
    assert rows(page).count() == 0
    page.click("div.empty >> text=Clear filters")
    expect(rows(page)).to_have_count(25)
    t.clean()


def test_sla_sorting_and_paging(tab, hub):
    t = tab()
    page = t.open("/sla", ".sx-requests tbody tr")
    age = lambda i: rows(page).nth(i).locator("[data-age]").inner_text()
    page.click('.sx-requests button.sx-sort:has-text("Age")')                 # oldest first
    assert age(0) == "4 h 10 min" and page.locator('.sx-requests th[aria-sort="descending"]').inner_text().strip().startswith("Age")
    page.click('.sx-requests button.sx-sort:has-text("Age")')                 # youngest first, the undated one last
    assert age(0) == "3 min"
    assert text(page, ".sx-pager").startswith("1–25 of 31")
    page.click('.sx-pager button:has-text("Next")')
    assert rows(page).count() == 6 and "26–31 of 31" in text(page, ".sx-pager")
    assert page.locator('.sx-pager button:has-text("Next")').is_disabled()
    page.select_option('select[aria-label="Rows per page"]', "50")
    assert rows(page).count() == 31 and page.locator('.sx-pager button:has-text("Next")').is_disabled()
    page.click('.sx-requests button.sx-sort:has-text("Security user")')
    users = [r.locator("td").nth(1).inner_text().strip() for r in rows(page).all()]
    assert users == sorted(users, key=lambda u: u.replace("أ", "ا"))
    t.clean()


def test_sla_keeps_filters_sort_and_scroll_when_a_new_scan_arrives(tab, hub, ops):
    t = tab()
    page = t.open("/sla", ".sx-requests tbody tr")
    page.click('[data-kpi="reminder"]')
    page.fill('input[aria-label="Search requests"]', "T24")
    expect(rows(page)).to_have_count(3)                                            # 3 of the 7 reminders are on T24
    page.click('.sx-requests button.sx-sort:has-text("Age")')
    before = rows(page).count()
    page.evaluate("window.scrollTo(0, 120)")

    newer = sla_snapshot(now=NOW + dt.timedelta(minutes=15))                    # a later scan, one more reminder request
    newer["requests"].append({"key": "extra", "security_user": USERS[1], "sent_at": iso(NOW - dt.timedelta(minutes=64)), "age_minutes": 79,
                              "status": "reminder", "fields": {"النظام": "T24", "رقم المستخدم": "9999"}})
    newer["totals"] = totals_of(newer["requests"])
    newer["by_user"] = by_user(newer["requests"])
    write_sla(ops, newer, at=NOW + dt.timedelta(minutes=15), history=False)
    t.poll_now()
    expect(rows(page)).to_have_count(before + 1)
    assert page.locator('[data-kpi="reminder"]').get_attribute("aria-pressed") == "true"
    assert page.locator('input[aria-label="Search requests"]').input_value() == "T24"
    assert page.locator('.sx-requests th[aria-sort="descending"]').inner_text().strip().startswith("Age")
    assert page.evaluate("window.scrollY") > 0
    assert "Scan of 2026-10-11 09:15" in text(page, ".sx-scan")
    t.clean()


def test_sla_filters_survive_a_page_reload(tab, hub):
    t = tab()
    page = t.open("/sla", ".sx-requests tbody tr")
    page.click('[data-kpi="breached"]')
    page.click('.sx-requests button.sx-sort:has-text("Age")')
    page.reload()
    page.wait_for_selector(".sx-requests tbody tr")
    assert statuses(page) == ["breached"] * 4 and page.locator('.sx-requests th[aria-sort="descending"]').count() == 1


def test_sla_ages_follow_the_server_clock_between_scans(tab, hub, clock):
    t = tab()
    page = t.open("/sla", ".sx-requests tbody tr")
    row = lambda key: page.locator(f'.sx-requests tr[data-key="{key}"]')
    assert row("k003").locator("[data-age]").inner_text() == "2 h 05 min" and row("k003").locator("[data-cue]").inner_text() == ""
    assert row("k004").locator("[data-age]").inner_text() == "1 h 58 min"
    clock.advance(minutes=3)                                                    # no new scan, only time passes
    t.poll_now()
    expect(row("k003").locator("[data-age]")).to_have_text("2 h 08 min")
    assert row("k004").locator("[data-age]").inner_text() == "2 h 01 min"
    assert row("k004").get_attribute("data-status") == "reminder"               # the status is the scan's ...
    assert row("k004").locator("[data-cue]").inner_text() == "now over 2 h"      # ... and the page says the clock has passed the limit
    assert page.locator('[data-kpi="oldest"]').get_attribute("data-minutes") == "253"
    assert kpi(page, "breached") == "4"                                         # tiles stay the scan's numbers
    assert "13 min ago" not in text(page, ".sx-fresh") and "3 min ago" in text(page, ".sx-fresh")
    t.clean()


def test_sla_timeline_marks_where_requests_first_breached(tab, hub):
    t = tab()
    page = t.open("/sla", ".sx-chart svg")
    assert page.locator(".sx-chart path.ln-open").count() == 1 and page.locator(".sx-chart path.ln-breached").count() == 1
    assert page.locator(".sx-chart .mark-break").count() == 2                   # 08:30 and 08:45 (two requests share the second)
    assert "Open 31" in svg_text(page, ".sx-chart svg") and "Breached 4" in svg_text(page, ".sx-chart svg")
    assert "First went over the limit" in text(page, ".sx-legend")
    box = page.locator(".sx-chart rect.hit").bounding_box()
    page.mouse.move(box["x"] + box["width"] - 2, box["y"] + 20)
    page.wait_for_selector(".sx-tip:not([hidden])")
    assert "Scan at 09:00" in text(page, ".sx-tip")
    page.mouse.move(box["x"] + box["width"] * 0.62, box["y"] + 20)
    expect(page.locator(".sx-tip")).to_contain_text("passed 2 h")
    page.click("text=View as table")                                            # the same numbers without hovering
    assert page.locator(".sx-chart-table tbody tr").count() == 5 and not page.locator(".sx-chart").is_visible()
    page.click("text=View as chart")
    assert page.locator(".sx-chart").is_visible()
    t.clean()


def test_sla_run_now_queues_a_request_and_waits_for_the_new_scan(tab, hub, ops, clock):
    t = tab()
    page = t.open("/sla", '[data-kpi="open"]')
    button = page.locator("button.btn-primary")
    assert button.inner_text().strip() == "Run scan now" and ops.pending_run_requests("sla") == []
    button.click()
    page.wait_for_selector("button.btn-primary:disabled")
    assert button.inner_text().strip() == "Scanning…" and "Waiting for the collector" in text(page, ".sx-runline")
    pending = ops.pending_run_requests("sla")
    assert len(pending) == 1 and pending[0]["requested_by"] == "admin" and pending[0]["file"].startswith("sla-")
    assert ops.pending_run_requests("itsm") == []

    beat(ops, "sla", running=True)                                              # the collector picks it up ...
    page.wait_for_selector("text=is scanning now", timeout=15000)
    ops.consume_run_requests("sla")
    clock.advance(minutes=5)
    done = sla_snapshot(now=NOW + dt.timedelta(minutes=5))
    done["requests"] = done["requests"][2:]                                     # ... and finds two requests less
    done["totals"], done["by_user"] = totals_of(done["requests"]), by_user(done["requests"])
    write_sla(ops, done, at=NOW + dt.timedelta(minutes=5), history=False)
    beat(ops, "sla", at=NOW + dt.timedelta(minutes=5))
    page.wait_for_selector("text=Scan finished at 09:05: 29 open, 2 breached.", timeout=15000)
    assert kpi(page, "open") == "29" and kpi(page, "breached") == "2"
    assert button.inner_text().strip() == "Run scan now" and not button.is_disabled()
    t.clean()


def test_sla_run_now_reports_a_failed_scan(tab, hub, ops, clock):
    t = tab()
    page = t.open("/sla", '[data-kpi="open"]')
    page.click("button.btn-primary")
    page.wait_for_selector("button.btn-primary:disabled")
    clock.advance(minutes=1)
    beat(ops, "sla", at=NOW + dt.timedelta(minutes=1), last_status="error", last_error="Login failed - check the password")
    page.wait_for_selector("text=The scan failed: Login failed - check the password", timeout=15000)
    assert not page.locator("button.btn-primary").is_disabled()
    assert "The last scan failed" in text(page, ".sx-fresh") and kpi(page, "open") == "31"   # the last good scan stays on screen
    t.problems.clear()


def test_sla_run_now_gives_up_waiting_after_its_time_limit(chromium, server, accounts, hub, ops):
    t = Tab(chromium, server)
    t.page.clock.install(time=NOW)                                               # fake timers: the 4 minutes pass at once
    t.login("admin", accounts["admin"])
    page = t.open("/sla", '[data-kpi="open"]')
    page.click("button.btn-primary")
    page.wait_for_selector("button.btn-primary:disabled")
    page.clock.run_for(5 * 60 * 1000)
    page.wait_for_selector("text=has not delivered a new scan yet", timeout=15000)
    assert not page.locator("button.btn-primary").is_disabled() and len(ops.pending_run_requests("sla")) == 1
    t.close()


def test_sla_banners_say_what_is_wrong(ops, tab, hub, clock):
    t = tab()
    page = t.open("/sla", '[data-kpi="open"]')
    old = NOW - dt.timedelta(minutes=90)
    for job in schemas.JOBS:
        beat(ops, job, at=old)
    t.poll_now()
    expect(page.locator(".sx-fresh")).to_contain_text("offline")
    assert "collector is offline" in text(page, ".sx-fresh") and "09:00" in text(page, ".sx-fresh")
    for job in schemas.JOBS:
        beat(ops, job, last_status="error", last_error="SSO portal did not answer")
    t.poll_now()
    expect(page.locator(".sx-fresh")).to_contain_text("SSO portal did not answer")
    assert "The last scan failed" in text(page, ".sx-fresh") and "Showing the scan from 09:00" in text(page, ".sx-fresh")
    assert kpi(page, "open") == "31"
    t.problems.clear()


@pytest.mark.parametrize("path,api,ready", [("/sla", "sla", '[data-kpi="open"]'), ("/itsm", "itsm", ".sx-role")])
def test_pages_keep_the_last_scan_and_say_so_when_the_server_fails(tab, hub, path, api, ready):
    t = tab()
    page = t.open(path, ready)
    answer = {"status": 500, "content_type": "application/json", "body": '{"error": "Data folder problem"}'}
    page.route(f"**/api/ops/{api}*", lambda route: route.fulfill(**answer))
    t.poll_now()
    expect(page.locator(".sx-fresh")).to_contain_text("The server reported a problem (Data folder problem)")
    assert page.locator(ready).count() >= 1                                    # the numbers stay on screen
    page.unroute(f"**/api/ops/{api}*")
    page.route(f"**/api/ops/{api}*", lambda route: route.abort())
    t.poll_now()
    expect(page.locator(".sx-fresh")).to_contain_text("Cannot reach the server - retrying. Showing the scan from 09:00.")
    page.unroute(f"**/api/ops/{api}*")
    t.poll_now()
    expect(page.locator(".sx-fresh")).to_contain_text("Live")
    t.problems.clear()


def test_sla_calm_empty_states(ops, tab):
    page = tab().open("/sla", ".sx-page")
    assert "No scan yet" in text(page, ".sx-page") and page.locator("[data-kpi]").count() == 0
    assert page.locator('a:has-text("Export Excel")').is_hidden()
    write_sla(ops, sla_snapshot(now=NOW) | {"totals": totals_of([]), "by_user": [], "requests": []}, history=False)
    for job in schemas.JOBS:
        beat(ops, job)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_selector("text=No open requests")
    assert "Nothing is waiting for the security users." in text(page, ".sx-page") and kpi(page, "open") == "0"
    assert page.locator(".sx-requests tbody tr").count() == 0 and page.locator('[data-kpi="oldest"] [data-kpi-value]').inner_text() == "—"


def test_sla_export_link_serves_the_workbook(tab, hub):
    t = tab()
    page = t.open("/sla", '[data-kpi="open"]')
    link = page.locator('a:has-text("Export Excel")')
    assert link.get_attribute("href") == "/export/ops/sla.xlsx"
    with page.expect_download() as info:
        link.click()
    assert info.value.suggested_filename.endswith(".xlsx")
    t.clean()


def test_sla_text_from_the_portal_is_never_run_as_html(ops, tab):
    data = sla_snapshot()
    evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>'
    data["requests"][0]["fields"]["اسم الموظف"] = evil
    data["requests"][0]["security_user"] = evil
    data["by_user"][0]["user"] = evil
    write_sla(ops, data)
    for job in schemas.JOBS:
        beat(ops, job)
    t = tab()
    page = t.open("/sla", ".sx-requests tbody tr")
    assert page.evaluate("window.__pwned") is None and page.locator(".sx-page img").count() == 0
    assert evil in page.locator(".sx-requests").inner_text()
    t.clean()


# ---------------------------------------------------------------------------------------------- ITSM page
def role_card(page, role):
    return page.locator(f'.sx-role[data-role="{role}"]')


def category(page, role, name):
    return role_card(page, role).locator(f'.sx-cat[data-category="{name}"]')


def test_itsm_cards_show_counts_tones_and_lists(tab, hub):
    t = tab()
    page = t.open("/itsm", ".sx-role")
    assert page.locator(".sx-role").count() == 3
    assert [kpi(page, k) for k in (IMPL, ROLLBACK)] == ["15", "1"]
    assert page.locator(f'[data-kpi="{IMPL}"]').get_attribute("class").endswith("tone-warn")
    assert page.locator(f'[data-kpi="{ROLLBACK}"]').get_attribute("class").endswith("tone-danger")
    assert kpi(page, "roles") == "3 of 3" and page.locator(".sx-banner.danger:visible").count() == 0

    expect = {(ROLE_A, IMPL): ("3", "warn"), (ROLE_A, ROLLBACK): ("1", "danger"), (ROLE_B, IMPL): ("0", "ok"),
              (ROLE_B, ROLLBACK): ("0", "ok"), (ROLE_C, IMPL): ("12", "warn"), (ROLE_C, ROLLBACK): (None, "na")}
    for (role, name), (count, tone) in expect.items():
        cat = category(page, role, name)
        assert cat.get_attribute("data-tone") == tone, (role, name)
        if count is None:
            assert "not on this scoreboard" in cat.inner_text() and cat.locator(".sx-count-chip").count() == 0
        else:
            assert cat.locator(".sx-count-chip").inner_text().strip() == count and cat.locator(".sx-count-chip").get_attribute("class").endswith(f"tone-{tone}")

    a = category(page, ROLE_A, IMPL)
    assert [li.get_attribute("data-number") for li in a.locator("li").all()] == ["100100", "100101", "100102"]
    assert "Add user to Flexcube group" in a.locator("li").first.inner_text() and "فتح صلاحية على النظام" in a.locator("li").nth(1).inner_text()
    assert page.locator(".sx-reqs li span.bidi[dir=auto]").count() >= 5                      # summaries carry dir=auto
    c = category(page, ROLE_C, IMPL)
    assert "Showing 5 of 12 requests (list is paged)." in c.inner_text() and c.locator("li").count() == 5
    assert category(page, ROLE_B, IMPL).locator("button.sx-cat-toggle").count() == 0      # nothing to open behind a zero
    t.clean()


def test_itsm_shows_the_message_of_a_role_that_could_not_be_read(ops, tab, hub):
    write_itsm(ops, itsm_snapshot(error_role=True))
    t = tab()
    page = t.open("/itsm", ".sx-role")
    assert page.locator(".sx-role").count() == 4
    card = page.locator(f'.sx-role[data-error="1"]')
    assert card.count() == 1 and card.get_attribute("data-role") == ROLE_D
    assert "This role could not be read" in card.inner_text() and "Set Role button not found" in card.inner_text()
    assert page.locator(".sx-role").first.get_attribute("data-error") == "1"               # errors come first
    assert "1 of 4 roles could not be read" in text(page, ".sx-banner.danger")
    assert kpi(page, "roles") == "3 of 4" and "1 could not be read" in text(page, '[data-kpi="roles"]')
    assert page.locator('[data-kpi="roles"]').get_attribute("class").endswith("tone-danger")
    t.clean()


def test_itsm_expand_collapse_search_and_copy(tab, hub):
    t = tab(permissions=["clipboard-read", "clipboard-write"])
    page = t.open("/itsm", ".sx-role")
    assert page.locator(".sx-reqs li").count() == 9
    toggle = category(page, ROLE_A, IMPL).locator("button.sx-cat-toggle")
    assert toggle.get_attribute("aria-expanded") == "true"
    toggle.focus()
    page.keyboard.press("Enter")                                                          # keyboard works, focus stays
    assert toggle.get_attribute("aria-expanded") == "false" and page.evaluate("document.activeElement.className") == "sx-cat-toggle"
    assert page.locator(".sx-reqs li").count() == 6
    page.click("text=Collapse all")
    assert page.locator(".sx-reqs li").count() == 0
    page.click("text=Expand all")
    assert page.locator(".sx-reqs li").count() == 9

    page.fill('input[aria-label="Search the requests"]', "100303")
    expect(page.locator(".sx-reqs li")).to_have_count(1)
    assert page.locator(".sx-role").count() == 1 and role_card(page, ROLE_C).count() == 1
    page.fill('input[aria-label="Search the requests"]', "صلاحية")
    expect(page.locator(".sx-reqs li")).to_have_count(2)
    page.fill('input[aria-label="Search the requests"]', "zzz")
    page.wait_for_selector("text=No request matches the search.")
    page.fill('input[aria-label="Search the requests"]', "")
    expect(page.locator(".sx-role")).to_have_count(3)

    page.click('button[aria-label="Copy request number 100101"]')
    page.wait_for_selector("text=Copied 100101")
    assert page.evaluate("navigator.clipboard.readText()") == "100101"
    t.clean()


def test_itsm_long_lists_start_closed_and_open_on_demand(ops, tab, hub):
    data = itsm_snapshot()
    many = [{"number": str(500000 + i), "summary": f"Request {i}"} for i in range(12)]
    data["roles"][0]["categories"][IMPL] = {"count": 12, "rows": many, "warning": None, "note": None}
    write_itsm(ops, data)
    t = tab()
    page = t.open("/itsm", ".sx-role")
    toggle = category(page, ROLE_A, IMPL).locator("button.sx-cat-toggle")
    assert toggle.get_attribute("aria-expanded") == "false" and category(page, ROLE_A, IMPL).locator("li").count() == 0
    toggle.click()
    assert category(page, ROLE_A, IMPL).locator("li").count() == 12
    page.reload()                                                                          # the choice is remembered for this tab
    page.wait_for_selector(".sx-role")
    assert category(page, ROLE_A, IMPL).locator("li").count() == 12


def test_itsm_lists_what_changed_since_the_previous_scan(ops, tab, hub):
    t = tab()
    page = t.open("/itsm", ".sx-role")
    new, closed = page.locator('[data-changes="new"]'), page.locator('[data-changes="closed"]')
    assert "100200" in new.inner_text() and "Rollback of the patch" in new.inner_text() and ROLE_A in new.inner_text()
    assert "099999" in closed.inner_text() and "طلب تم اغلاقه" in closed.inner_text() and ROLE_C in closed.inner_text()
    assert "1 new · 1 closed" in text(page, ".sx-page")
    data = itsm_snapshot()
    data["changes"] = {"new": [], "closed": []}
    write_itsm(ops, data, at=NOW + dt.timedelta(minutes=30))
    t.poll_now()
    page.wait_for_selector("text=Nothing new and nothing closed since the previous scan.")
    t.clean()


def test_itsm_history_chart_and_table(tab, hub):
    t = tab()
    page = t.open("/itsm", ".sx-chart svg")
    assert page.locator(".sx-chart path.ln").count() == 2
    legend = text(page, ".sx-legend")
    assert IMPL in legend and ROLLBACK in legend
    assert "Implementation 15" in svg_text(page, ".sx-chart svg") and "Rollback 1" in svg_text(page, ".sx-chart svg")
    box = page.locator(".sx-chart rect.hit").bounding_box()
    page.mouse.move(box["x"] + box["width"] - 2, box["y"] + 10)
    page.wait_for_selector(".sx-tip:not([hidden])")
    assert "Scan at 09:00" in text(page, ".sx-tip") or "Scan at 10:00" in text(page, ".sx-tip")
    page.click("text=View as table")
    assert page.locator(".sx-chart-table tbody tr").count() == 3
    t.clean()


def test_itsm_run_now_and_export(tab, hub, ops, clock):
    t = tab()
    page = t.open("/itsm", ".sx-role")
    assert page.locator('a:has-text("Export Excel")').get_attribute("href") == "/export/ops/itsm.xlsx"
    assert page.locator("text=Send status e-mail").count() == 0          # the API has no such option, so the page has no such button
    page.click("button.btn-primary")
    page.wait_for_selector("button.btn-primary:disabled")
    pending = ops.pending_run_requests("itsm")
    assert len(pending) == 1 and pending[0]["requested_by"] == "admin" and ops.pending_run_requests("sla") == []
    ops.consume_run_requests("itsm")
    clock.advance(minutes=2)
    data = itsm_snapshot(error_role=True)
    write_itsm(ops, data, at=NOW + dt.timedelta(minutes=2))
    beat(ops, "itsm", at=NOW + dt.timedelta(minutes=2))
    page.wait_for_selector("text=Scan finished at 09:02: 16 open requests.", timeout=15000)
    assert page.locator(".sx-role").count() == 4
    t.clean()


def test_itsm_banners_and_empty_states(ops, tab):
    page = tab().open("/itsm", ".sx-page")
    assert "No scan yet" in text(page, ".sx-page") and page.locator("[data-kpi]").count() == 0
    write_itsm(ops, {"categories": [IMPL, ROLLBACK], "roles": [], "totals": {}, "changes": {"new": [], "closed": []}})
    for job in schemas.JOBS:
        beat(ops, job)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_selector("text=No roles in this scan")
    assert [kpi(page, k) for k in (IMPL, ROLLBACK)] == ["0", "0"] and "nothing waiting" in text(page, f'[data-kpi="{IMPL}"]')


def test_itsm_text_from_the_scoreboard_is_never_run_as_html(ops, tab, hub):
    data = itsm_snapshot()
    evil = '<img src=x onerror="window.__pwned=1">'
    data["roles"][0]["role"] = evil
    data["roles"][0]["categories"][IMPL]["rows"][0]["summary"] = evil
    data["roles"][1]["error"] = evil
    data["roles"][1]["categories"] = {}
    write_itsm(ops, data)
    t = tab()
    page = t.open("/itsm", ".sx-role")
    assert page.evaluate("window.__pwned") is None and page.locator(".sx-page img").count() == 0
    assert evil in page.locator(".sx-page").inner_text()
    t.clean()


# ---------------------------------------------------------------------------------------------- roles, themes, size
@pytest.mark.parametrize("path,ready", [("/sla", '[data-kpi="open"]'), ("/itsm", ".sx-role")])
def test_manager_sees_both_pages_and_runs_a_scan(tab, hub, ops, path, ready):
    t = tab("mona")
    page = t.open(path, ready)
    page.click("button.btn-primary")
    page.wait_for_selector("button.btn-primary:disabled")
    job = path[1:]
    assert [r["requested_by"] for r in ops.pending_run_requests(job)] == ["mona"]
    t.clean()


@pytest.mark.parametrize("path", ["/sla", "/itsm"])
def test_employee_is_kept_out_in_the_browser_too(tab, hub, path):
    t = tab("1001")
    page = t.page
    page.goto(f"{t.base}{path}")
    assert page.locator(".sx-page").count() == 0 and page.locator("script[src$='sla.js'], script[src$='itsm.js']").count() == 0
    t.problems.clear()


@pytest.mark.parametrize("path,ready", [("/sla", '[data-kpi="open"]'), ("/itsm", ".sx-role")])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_pages_are_readable_at_1280_in_both_themes(tab, hub, path, ready, scheme):
    t = tab(scheme=scheme, width=1280, height=800)
    page = t.open(path, ready)
    page.wait_for_timeout(300)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "no horizontal page scroll"
    surface = page.evaluate("getComputedStyle(document.body).backgroundColor")
    ink = page.evaluate("getComputedStyle(document.querySelector('.sx-banner-text')).color")
    luminance = lambda rgb: sum(int(v) * w for v, w in zip(re.findall(r"\d+", rgb)[:3], (0.2126, 0.7152, 0.0722)))
    assert (luminance(surface) < luminance(ink)) == (scheme == "dark")
    t.clean()


@pytest.mark.parametrize("path,ready", [("/sla", '[data-kpi="open"]'), ("/itsm", ".sx-role")])
def test_pages_work_by_keyboard(tab, hub, path, ready):
    t = tab()
    page = t.open(path, ready)
    seen = set()
    for _ in range(40):
        page.keyboard.press("Tab")
        seen.add(page.evaluate("(document.activeElement.dataset && document.activeElement.dataset.focusId) || document.activeElement.tagName"))
    assert page.evaluate("document.activeElement !== document.body")
    assert len(seen) >= 6
    t.clean()


def test_screenshots_for_the_docs(chromium, server, accounts, hub, ops):
    folder = os.environ.get("CALLLOG_SCREENSHOTS")
    if not folder:
        pytest.skip("set CALLLOG_SCREENSHOTS=<folder> to write the screenshots")
    write_itsm(ops, itsm_snapshot(error_role=True))
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    for scheme in ("light", "dark"):
        t = Tab(chromium, server, scheme=scheme).login("admin", accounts["admin"])
        for path, ready, name in (("/sla", ".sx-requests tbody tr", "sla"), ("/itsm", ".sx-role", "itsm")):
            page = t.open(path, ready)
            page.wait_for_timeout(500)
            page.screenshot(path=str(out / f"ops-{name}-{scheme}.png"))
        t.close()
