"""The Operations Hub pages and their shared front-end: who may open what, the shells and the top bar, the strict-CSP rules,
and a real-browser run (Chromium through Playwright) of Home and System against the demo data.

Server-side checks run anywhere; the browser tests skip themselves when Chromium cannot start. The server clock is the
conftest ``clock`` (frozen at 2026-10-11 09:00), so the demo data and every "x min ago" are exact.

Opt-in screenshots for the documentation:  CALLLOG_SCREENSHOTS=<folder> pytest tests/test_ops_views.py -k screenshots
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from html.parser import HTMLParser
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from calllog.ops import schemas
from conftest import Browser

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "calllog" / "static"
OWN_SCRIPTS = ("ops-common.js", "hub.js", "system.js")
PAGES = ("/hub", "/email", "/sla", "/itsm", "/system", "/my")
NOW = dt.datetime(2026, 10, 11, 9, 0, 0)                      # the conftest `clock`


class _LazyDemo:
    """``calllog.ops.demo`` imported on first use and forgotten after this module's tests: an import at collection time would leave
    ``calllog.ops.demo`` on the package for the whole session and hide a missing module from tests/test_legacy_import.py."""

    def __getattr__(self, name):
        if name.startswith("_") or name == "pytestmark":          # pytest probes every module attribute while collecting
            raise AttributeError(name)
        import calllog.ops.demo as module
        return getattr(module, name)


demo = _LazyDemo()


@pytest.fixture(scope="module", autouse=True)
def _forget_the_demo_module():
    yield
    import calllog.ops
    sys.modules.pop("calllog.ops.demo", None)
    calllog.ops.__dict__.pop("demo", None)


# ------------------------------------------------------------------------------------------------ helpers
class Shell(HTMLParser):
    """What a rendered page contains that the strict CSP cares about, and its top-bar links."""

    def __init__(self, html):
        super().__init__()
        self.scripts, self.styles, self.style_attrs, self.handlers, self.nav, self.ids, self.links = [], [], [], [], [], set(), []
        self._script = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        if "style" in a:
            self.style_attrs.append((tag, a["style"]))
        self.handlers += [(tag, k) for k in a if k.startswith("on")]
        if tag == "script":
            self._script = {"src": a.get("src"), "body": ""}
            self.scripts.append(self._script)
        elif tag == "style":
            self.styles.append(tag)
        elif tag == "link" and a.get("rel") == "stylesheet":
            self.links.append(a.get("href"))
        elif tag == "a" and "op-nav-link" in (a.get("class") or "").split():
            self.nav.append((a.get("href"), "aria-current" in a))
            self._nav_index = len(self.nav) - 1
            self._nav_text = ""
        self._in_nav = tag == "a" and "op-nav-link" in (a.get("class") or "").split()

    def handle_data(self, data):
        if self._script is not None:
            self._script["body"] += data
        if getattr(self, "_in_nav", False):
            self._nav_text += data

    def handle_endtag(self, tag):
        if tag == "script":
            self._script = None
        if tag == "a" and getattr(self, "_in_nav", False):
            href, current = self.nav[self._nav_index]
            self.nav[self._nav_index] = (href, current, self._nav_text.strip())
            self._in_nav = False


def shell(browser, url="/hub"):
    resp = browser.get(url)
    assert resp.status_code == 200, (url, resp.status_code)
    return Shell(resp.get_data(as_text=True)), resp


@pytest.fixture
def ops(app):
    return app.extensions["ops"]


@pytest.fixture
def make_account(app, admin):
    def factory(uid, role="user", name=None, **profile):
        body = {"user_id": uid, "name": name or uid.title(), "department": "IT", "role": role, "password": "TempPass123", **profile}
        r = admin.post("/api/admin/users", body)
        assert r.status_code == 201, r.get_json()
        b = Browser(app)
        b.login(uid, "TempPass123")
        resp = b.client.post("/account/password", data={"current_password": "TempPass123", "new_password": "MyOwnPass123",
                                                         "confirm_password": "MyOwnPass123", "csrf_token": b.csrf})
        assert resp.status_code == 302
        return b
    return factory


@pytest.fixture
def mgr(make_account):
    return make_account("fadi", "manager", "Fadi Flex", email="fadi@example.test", team="Flex Cube")


@pytest.fixture
def emp(make_account):
    return make_account("2001", "user", "Alice Employee", email="alice@example.test")


# ------------------------------------------------------------------------------------------- who opens what
@pytest.mark.parametrize("path", PAGES + ("/calls",))
def test_anonymous_goes_to_sign_in(app, path):
    resp = app.test_client().get(path)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")


@pytest.mark.parametrize("path", ("/hub", "/email", "/sla", "/itsm", "/system", "/calls"))
def test_employee_is_sent_to_my_work(emp, path):
    resp = emp.get(path)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/my")
    assert emp.get("/my").status_code == 200


@pytest.mark.parametrize("path,code", [("/hub", 200), ("/email", 200), ("/sla", 200), ("/itsm", 200), ("/system", 302), ("/calls", 302), ("/my", 302)])
def test_manager_pages(mgr, path, code):
    resp = mgr.get(path)
    assert resp.status_code == code
    if code == 302:
        assert resp.headers["Location"].endswith("/hub")           # back to the person's own start page, never a bare error


@pytest.mark.parametrize("path", ("/hub", "/email", "/sla", "/itsm", "/system"))
def test_admin_pages(admin, path):
    assert admin.get(path).status_code == 200


def test_admin_calls_is_the_call_log_dashboard_and_my_needs_a_person(admin):
    assert admin.get("/calls").headers["Location"].endswith("/admin#overview")
    assert admin.get("/my").headers["Location"].endswith("/hub")


def test_view_as_employee(admin, mgr, emp, make_account):
    page, _ = shell(admin, "/my?as=2001")
    html = admin.get("/my?as=2001").get_data(as_text=True)
    assert 'data-view-as="2001"' in html and "read only" in html and "Alice Employee" in html and 'href="/hub"' in html
    assert 'data-view-as="2001"' in mgr.get("/my?as=2001").get_data(as_text=True)                  # a manager may look too
    missing = admin.get("/my?as=nobody")
    assert missing.status_code == 404 and "Person not found" in missing.get_data(as_text=True)
    other = make_account("2002", "user", "Bob Employee")
    assert emp.get("/my?as=2002").headers["Location"].endswith("/my")                               # an employee only ever sees their own
    own = emp.get("/my?as=2001")
    assert own.status_code == 200 and "data-view-as" not in own.get_data(as_text=True)
    assert other.get("/my").status_code == 200


def test_picker_lists_active_employees_and_escapes_names(admin, make_account, app):
    make_account("2003", "user", '<img src=x onerror=alert(1)> Eve')
    make_account("2004", "user", "Dormant Dan")
    assert admin.put("/api/admin/users/2004", {"active": False}).status_code == 200
    html = admin.get("/hub").get_data(as_text=True)
    picker = html[html.index('id="viewAs"'):html.index("</select>")]
    assert "2003" in picker and "2004" not in picker and "admin" not in picker.replace("Choose", "")
    assert "<img src=x" not in html and "&lt;img src=x onerror=alert(1)&gt;" in picker


# ------------------------------------------------------------------------------------- top bar and shells
def nav_of(browser, url):
    page, _ = shell(browser, url)
    return [(href, text, current) for href, current, text in page.nav]


def test_top_bar_links_per_role(admin, mgr, emp):
    assert [(h, t) for h, t, _ in nav_of(admin, "/hub")] == [("/hub", "Home"), ("/calls", "Calls"), ("/email", "E-mail"), ("/sla", "SLA"),
                                                              ("/itsm", "ITSM"), ("/system", "System")]
    assert [(h, t) for h, t, _ in nav_of(mgr, "/hub")] == [("/hub", "Home"), ("/email", "E-mail"), ("/sla", "SLA"), ("/itsm", "ITSM")]
    assert [(h, t) for h, t, _ in nav_of(emp, "/my")] == [("/dashboard", "Log calls"), ("/my", "My work")]


@pytest.mark.parametrize("path,label", [("/hub", "Home"), ("/email", "E-mail"), ("/sla", "SLA"), ("/itsm", "ITSM"), ("/system", "System"), ("/admin", "Calls")])
def test_current_page_is_marked(admin, path, label):
    marked = [t for _, t, current in nav_of(admin, path) if current]
    assert marked == [label]


def test_the_older_pages_keep_their_top_bar(admin, emp):
    html = admin.get("/admin").get_data(as_text=True)
    assert "Operations Hub" in html and 'aria-current="page"' in html and "Sign out" in html and "ops.css" in html
    mine = nav_of(emp, "/dashboard")
    assert [(h, t, c) for h, t, c in mine] == [("/dashboard", "Log calls", True), ("/my", "My work", False)]


@pytest.mark.parametrize("path,main,script", [("/hub", "hub-page", "hub.js"), ("/email", "email-page", "email.js"), ("/sla", "sla-page", "sla.js"),
                                              ("/itsm", "itsm-page", "itsm.js"), ("/system", "system-page", "system.js")])
def test_page_shells(admin, path, main, script):
    page, resp = shell(admin, path)
    assert main in page.ids and "toast" in page.ids
    srcs = [s["src"] for s in page.scripts]
    assert all(srcs) and srcs[-1].endswith(f"/static/{script}") and any(s.endswith("/static/ops-common.js") for s in srcs)
    assert any(s.endswith("/static/common.js") for s in srcs) and any(l.endswith("/static/ops.css") for l in page.links)
    assert resp.headers["Cache-Control"] == "no-store"


def test_system_page_has_every_tab_and_home_has_every_section(admin):
    system, _ = shell(admin, "/system")
    assert {"tab-health", "tab-collectors", "tab-settings", "tab-logs", "tab-reports", "sysTabs", "live"} <= system.ids
    hub, _ = shell(admin, "/hub")
    assert {"hubStrip", "hubAttention", "hubCollectors", "hubCards", "hubTimeline", "hubTeams", "viewAs", "viewAsForm"} <= hub.ids


@pytest.mark.parametrize("path", PAGES)
def test_no_inline_script_style_or_handler_in_any_page(admin, path):
    html = admin.get(path if path != "/my" else "/my?as=admin").get_data(as_text=True)
    page = Shell(html)
    assert page.scripts and all(s["src"] and not s["body"].strip() for s in page.scripts), "inline script"
    assert not page.styles and not page.style_attrs and not page.handlers
    assert "javascript:" not in html.lower()


def test_csp_and_security_headers_stay_strict(admin):
    resp = admin.get("/hub")
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp
    assert resp.headers["X-Content-Type-Options"] == "nosniff" and resp.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("name,mime", [("ops.css", "text/css"), ("ops-common.js", "javascript"), ("hub.js", "javascript"), ("system.js", "javascript")])
def test_static_files_are_served(app, name, mime):
    resp = app.test_client().get(f"/static/{name}")
    assert resp.status_code == 200 and mime in resp.headers["Content-Type"] and resp.headers["X-Content-Type-Options"] == "nosniff"
    assert len(resp.get_data()) > 2000


def test_every_script_a_page_loads_exists_or_belongs_to_a_page_still_being_written(admin):
    """email.js and my.js are written by the page agents after the foundation; everything else must exist now."""
    later = {"email.js", "my.js"}
    for path in ("/hub", "/email", "/sla", "/itsm", "/system", "/my?as=admin"):
        for s in Shell(admin.get(path).get_data(as_text=True)).scripts:
            name = s["src"].rsplit("/", 1)[-1]
            assert (STATIC / name).is_file() or name in later, (path, name)


# ------------------------------------------------------------------------------- the front-end source rules
FORBIDDEN_JS = [r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write", r"\beval\(", r"new Function", r"setAttribute\(\s*[\"']style",
                r"\.cssText", r"\bstyle\s*=", r"javascript:", r"<script", r"setTimeout\(\s*[\"'`]", r"localStorage", r"sessionStorage"]


@pytest.mark.parametrize("name", OWN_SCRIPTS)
def test_script_source_follows_the_csp_and_textcontent_rules(name):
    text = (STATIC / name).read_text(encoding="utf-8")
    assert text.startswith('"use strict";')
    for pattern in FORBIDDEN_JS:
        assert not re.search(pattern, text), f"{name}: {pattern}"
    assert not re.findall(r"https?://", text.replace("http://www.w3.org/2000/svg", "")), "no external address"


@pytest.mark.parametrize("name", OWN_SCRIPTS)
def test_script_syntax(name):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "--check", str(STATIC / name)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


def test_stylesheet_is_self_contained_light_and_dark():
    css = (STATIC / "ops.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}")
    assert "@import" not in css and not re.search(r"url\(\s*[\"']?https?:", css) and "@font-face" not in css
    assert "prefers-color-scheme: dark" in css and "prefers-reduced-motion" in css
    for tone in ("danger", "warn", "ok", "info", "na"):
        assert f".op-t-{tone}" in css
    for token in ("--op-danger-text", "--op-warn-text", "--tone-text"):
        assert token in css


def test_ops_common_documents_its_api():
    text = (STATIC / "ops-common.js").read_text(encoding="utf-8")
    header = text[:text.index("(function")]
    for name in ("loader", "freshnessBanner", "sharedStrip", "table", "kpi", "chip", "statusChip", "jobChip", "donut", "hbars", "stackbar",
                 "sparkline", "emptyState", "fmtDuration", "copyText", "tip"):
        assert name in header, name
    exported = re.search(r"window\.OPS = \{(.*?)\};", text, re.S).group(1)
    for name in ("loader", "sharedStrip", "table", "kpi", "jobChip", "freshnessBanner", "fmtWhen", "donut"):
        assert re.search(rf"\b{name}\b", exported), name


# ============================================================================================ real browser
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
        except Exception as exc:
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


@pytest.fixture
def demo_data(app, clock):
    """The whole demo (accounts, calls, snapshots, history, heartbeats) for the frozen clock."""
    creds = demo.seed_users_and_calls(app.extensions["store"], clock())
    demo.seed(app.extensions["ops"], clock())
    return creds


def not_yet_written():
    """Messages to ignore while my.js (the employee page, written after the foundation) does not exist yet."""
    return () if (STATIC / "my.js").exists() else ("/static/my.js", "Failed to load resource", "MIME type")


class Tab:
    """One signed-in browser window that remembers everything that went wrong in it (console, CSP, failed requests)."""

    def __init__(self, browser, base, scheme="light", width=1440, height=900):
        self.base = base
        self.context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)
        self.problems = []
        page = self.page
        page.on("console", lambda m: self.problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.problems.append(f"page error: {e}"))
        page.on("requestfailed", lambda r: self.problems.append(f"request failed: {r.url}") if "ABORTED" not in str(r.failure).upper() else None)
        page.on("response", lambda r: self.problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)

    def login(self, uid, password):
        page = self.page
        page.goto(f"{self.base}/login")
        page.fill("input[name=user_id]", uid)
        page.fill("input[name=password]", password)
        page.click("button[type=submit]")
        page.wait_for_load_state()
        return self

    def open(self, path, ready):
        self.page.goto(f"{self.base}{path}")
        self.page.wait_for_selector(ready, timeout=10000)
        return self.page

    def later_problems(self, *ignore):
        return [p for p in self.problems if not any(i in p for i in ignore)]

    def close(self):
        self.context.close()


@pytest.fixture
def tab(chromium, server, admin):
    tabs = []

    def make(uid="admin", password="AdminPass123", **kw):
        t = Tab(chromium, server, **kw).login(uid, password)
        tabs.append(t)
        return t
    yield make
    for t in tabs:
        t.close()


def text_of(page, selector):
    return " ".join(page.inner_text(selector).split())


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_home_shows_the_sector_and_agrees_with_the_api(admin, demo_data, tab, scheme):
    t = tab(scheme=scheme)
    page = t.open("/hub", "#hubCards .op-module")
    page.wait_for_selector("#tlBody .op-tl-item")
    shared = admin.get("/api/ops/shared").get_json()
    overview = admin.get("/api/ops/overview").get_json()
    # the same Team status strip numbers as the API says
    groups = page.locator("#hubStrip .op-strip-group")
    assert groups.count() == 5
    assert shared["calls"]["total"] == 60 and page.inner_text("#hubStrip .op-strip-group:nth-child(1) .op-strip-value") == "60"
    assert page.inner_text("#hubStrip .op-strip-group:nth-child(2) .op-strip-value") == str(shared["email"]["pending"])
    assert page.inner_text("#hubStrip .op-strip-group:nth-child(3) .op-strip-value") == str(shared["sla"]["open"])
    assert "Collectors" in text_of(page, "#hubStrip .op-strip-group:nth-child(5)")
    # four module cards, the attention list in the order of the API, the collectors, the timeline and the team table
    assert page.locator("#hubCards .op-module").count() == 4
    items = page.locator("#attnBody .op-attn-item")
    assert items.count() == len(overview["attention"]) >= 5
    levels = [re.search(r"op-t-(\w+)", c).group(1) for c in items.evaluate_all("els => els.map(e => e.className)")]
    assert levels == sorted(levels, key=["danger", "warn", "info"].index)
    assert page.locator("#colBody .op-job").count() == 4 and "Offline" in text_of(page, "#colBody")           # the demo's stale ITSM collector
    assert page.locator("#tlBody .op-tl-item").count() == len(admin.get(f"/api/ops/events?day={NOW.date()}").get_json()["events"])
    assert page.locator("#teamBody tbody tr").count() == len([t for t in overview["email"]["teams"]])
    assert "not classified" in text_of(page, "#teamBody")
    assert page.inner_text("#hubCards .op-module-calls .op-big") == "60"
    assert t.later_problems() == [], t.problems


def test_home_links_jump_to_the_pages(admin, demo_data, tab):
    t = tab()
    page = t.open("/hub", "#hubCards .op-module")
    page.wait_for_selector("#tlBody .op-tl-item")
    hrefs = set(page.locator("#hubPage a, main a").evaluate_all("els => els.map(e => e.getAttribute('href'))"))
    assert {"/email", "/sla", "/itsm", "/calls", "/system#collectors", "/email#unclassified"} <= hrefs
    assert any(h and h.startswith("/sla?q=") for h in hrefs) and any(h and h.startswith("/email?q=") for h in hrefs)
    assert any(h and h.startswith("/email?team=") for h in hrefs)
    assert t.later_problems() == [], t.problems


def test_view_as_employee_picker_opens_that_page(demo_data, tab):
    t = tab()
    page = t.open("/hub", "#viewAs")
    options = page.locator("#viewAs option").all_inner_texts()
    assert any("Alice Hassan" in o for o in options) and not any("Administrator" in o or "Mona Adel" in o for o in options)
    page.select_option("#viewAs", "1001")
    page.wait_for_url(re.compile(r"/my\?as=1001"))
    assert "Alice Hassan" in text_of(page, ".op-viewing") and "read only" in text_of(page, ".op-viewing")
    page.click(".op-viewing a")
    page.wait_for_url(re.compile(r"/hub$"))
    assert t.later_problems(*not_yet_written()) == [], t.problems


def test_manager_home_is_scoped_to_their_team(admin, demo_data, tab):
    t = tab(demo.MANAGER_ID, demo_data["password"])
    page = t.open("/hub", "#hubCards .op-module")
    page.wait_for_selector("#tlBody .op-tl-item, #tlBody .op-empty")
    assert page.locator('a[href^="/system"]:visible, a[href="/calls"]:visible').count() == 0 and page.locator("#colLink").is_hidden()
    assert "Flex Cube" in text_of(page, "#hubSub")
    teams = page.locator("#teamBody tbody tr td:first-child").all_inner_texts()
    assert teams == ["Flex Cube"]
    pending = page.inner_text("#hubStrip .op-strip-group:nth-child(2) .op-strip-value")
    everybody = admin.get("/api/ops/shared").get_json()["email"]["pending"]
    assert pending != str(everybody) and int(pending) < everybody
    assert t.later_problems() == [], t.problems


def test_home_degrades_calmly_with_no_collector_and_with_failures(app, ops, tab, clock):
    t = tab()
    page = t.open("/hub", "#hubCards .op-module")
    assert page.locator("#hubCards .op-empty").count() >= 3 and "No e-mail data yet" in text_of(page, "#hubCards")
    assert "Not started" in text_of(page, "#colBody")
    demo.seed(ops, clock(), failed_job="sla", paused_job="itsm", itsm_error=True, stale_job="")
    page.reload()
    page.wait_for_selector("#hubCards .op-module-itsm .op-note")
    assert "could not be read" in text_of(page, "#hubCards .op-module-itsm")
    assert "Last run failed" in text_of(page, "#colBody")
    assert t.later_problems() == [], t.problems


# --------------------------------------------------------------------------------------------- polling rules
POLL_PROBE = """
async () => {
  const realSet = window.setTimeout, delays = [], urls = [];
  window.setTimeout = (fn, ms, ...a) => { delays.push(ms); return realSet(fn, ms, ...a); };
  const realFetch = window.fetch;
  window.fetch = (u, o) => { urls.push(String(u)); return realFetch(u, o); };
  const wait = (ms) => new Promise((r) => realSet(r, ms));
  const idle = async (loader) => { for (let i = 0; i < 300 && loader.busy; i++) await wait(10); await wait(10); };
  const I = 10000, out = {};
  // random start offset: twelve browsers that open the page together do not poll together
  const starts = [];
  for (let i = 0; i < 12; i++) { const l = OPS.loader({ url: "/api/ops/email", interval: I }); const before = delays.length; l.start(); await idle(l); starts.push(delays[before]); l.stop(); }
  out.starts = starts;
  // the following polls: interval + 0..20 %, doubled by every failure (at most x8), reset by one good answer
  let target = "/api/ops/email";
  const L = OPS.loader({ url: () => target, interval: I });
  const last = () => delays[delays.length - 1];
  L.start(); await idle(L);
  await L.refresh(); await idle(L); out.steady = last();
  target = "/api/ops/there-is-nothing-here";
  const failed = [];
  for (let i = 0; i < 5; i++) { await L.refresh(); await idle(L); failed.push(last()); }
  out.failed = failed;
  target = "/api/ops/email";
  await L.refresh(); await idle(L); out.recovered = last();
  // nothing while the tab is hidden
  Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
  const timers = delays.length, requests = urls.length;
  await L.refresh(); await idle(L);
  out.hiddenTimers = delays.length - timers; out.hiddenRequests = urls.length - requests;
  Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
  const before = urls.length;
  document.dispatchEvent(new Event("visibilitychange"));
  for (let i = 0; i < 300 && urls.length === before; i++) await wait(10);
  out.resumed = urls.length - before;
  L.stop();
  return out;
}
"""

SINCE_PROBE = """
async () => {
  const urls = [], realFetch = window.fetch;
  window.fetch = (u, o) => { urls.push(String(u)); return realFetch(u, o); };
  let unchanged = 0, data = 0;
  const L = OPS.loader({ url: "/api/ops/email", interval: 150, onData: () => { data++; }, onUnchanged: () => { unchanged++; } });
  L.start();
  for (let i = 0; i < 300 && unchanged < 1; i++) await new Promise((r) => setTimeout(r, 20));
  L.stop();
  return { urls, unchanged, data };
}
"""


def blank_page(tab_obj):
    """A signed-in page with no polling of its own (the change-password page) that has OPS loaded into it."""
    page = tab_obj.page
    page.goto(f"{tab_obj.base}/account/password")
    for name in ("common.js", "ops-common.js"):
        page.add_script_tag(url=f"{tab_obj.base}/static/{name}")
    return page


def test_polling_rules(demo_data, tab):
    page = blank_page(tab())
    r = page.evaluate(POLL_PROBE)
    I = 10000
    assert all(0.5 * I <= d < 1.5 * I for d in r["starts"]) and max(r["starts"]) - min(r["starts"]) > 0.2 * I      # random start offset
    assert I <= r["steady"] <= 1.2 * I
    for delay, factor in zip(r["failed"], (2, 4, 8, 8, 8)):                                                         # back-off, capped at x8
        assert factor * I <= delay <= factor * 1.2 * I, (delay, factor)
    assert I <= r["recovered"] <= 1.2 * I                                                                          # one good answer resets it
    assert r["hiddenTimers"] == 0 and r["hiddenRequests"] == 1 and r["resumed"] >= 1                               # silent while hidden, wakes when shown


def test_polls_send_since_and_rev_and_get_the_cheap_answer(demo_data, tab):
    page = blank_page(tab())
    r = page.evaluate(SINCE_PROBE)
    assert r["data"] == 1 and r["unchanged"] >= 1
    later = [u for u in r["urls"][1:] if "since=" in u]
    assert later and "rev=" in later[0] and "since=2026-10-11T08%3A58%3A00" in later[0]
    assert "since" not in r["urls"][0]


STRIP_PROBE = """
async (selector) => {
  const host = document.createElement("section"); document.body.append(host);
  const strip = OPS.sharedStrip(host, { poll: true });
  for (let i = 0; i < 300 && !host.querySelector(".op-strip-group"); i++) await new Promise((r) => setTimeout(r, 20));
  const groups = [...host.querySelectorAll(".op-strip-group")].map((g) => ({
    label: g.querySelector(".op-strip-label").textContent, value: g.querySelector(".op-strip-value").textContent, sub: g.querySelector(".op-strip-sub").textContent, title: g.title }));
  strip.stop();
  return groups;
}
"""


@pytest.mark.parametrize("uid,label", [("1001", "Team e-mails pending"), ("1002", "Team e-mails pending"), ("1003", "Team e-mails pending")])
def test_shared_strip_numbers_are_the_viewers_own(app, demo_data, tab, uid, label):
    t = tab(uid, demo.DEMO_PASSWORD)
    me = Browser(app)
    me.login(uid, demo.DEMO_PASSWORD)
    api = me.get("/api/ops/shared").get_json()
    groups = blank_page(t).evaluate(STRIP_PROBE, "")
    mail = groups[1]
    assert [g["label"] for g in groups] == ["Calls today", label, "Security requests open", "ITSM requests", "Collectors"]
    assert groups[0]["value"] == str(api["calls"]["total"]) == "60"
    assert groups[2]["value"] == str(api["sla"]["open"]) and groups[4]["value"] in ("Online", "2 of 3 online")
    if uid == "1003":                                                      # an employee with no team sees no team's mail
        assert mail["value"] == "—" and "No team" in mail["sub"] and api["email"]["teams"] == []
    else:
        assert mail["value"] == str(api["email"]["pending"]) and api["email"]["scope"] == "teams"
        assert ", ".join(api["email"]["teams"]) in mail["title"]
        assert "Unclassified" not in mail["title"]
    assert t.later_problems(*not_yet_written()) == [], t.problems


def test_employee_numbers_differ_from_the_administrators(app, admin, demo_data, tab):
    everybody = admin.get("/api/ops/shared").get_json()["email"]["pending"]
    one, two = (Browser(app) for _ in range(2))
    one.login("1001", demo.DEMO_PASSWORD)
    two.login("1002", demo.DEMO_PASSWORD)
    a, b = (x.get("/api/ops/shared").get_json()["email"]["pending"] for x in (one, two))
    assert a < everybody and b < everybody and b >= a                      # 1002 has a second team, so never fewer


# ---------------------------------------------------------------------------------------------------- System
def open_system(t, tab_name):
    page = t.open(f"/system#{tab_name}", f"#tab-{tab_name} .card, #tab-{tab_name} .op-form, #tab-{tab_name} .op-sys")
    return page


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_system_health_tab(admin, demo_data, tab, scheme):
    t = tab(scheme=scheme)
    page = open_system(t, "health")
    page.wait_for_selector("#tab-health .op-kpis")
    health = admin.get("/api/ops/health").get_json()
    assert page.locator("#tab-health .op-kpi").count() == 6
    assert "Writable" in text_of(page, '[data-kpi="folder"]') and str(health["users"]["active"]) == page.inner_text('[data-kpi="users"] .op-kpi-value')
    assert page.inner_text('[data-kpi="signins"] .op-kpi-value') == str(health["audit_today"]["failed_sign_ins"])
    assert page.locator("#tab-health .op-problems li").count() == len(health["problems"])
    assert "3 of 3" in text_of(page, '[data-kpi="snapshots"]')
    assert page.locator("#tab-health table tbody tr").count() == 3 + 4                   # snapshots + heartbeats
    assert "Disk free" in text_of(page, "#tab-health") and page.locator("#tab-health .op-stackbar").count() == 1
    assert page.get_attribute('#sysTabs [data-tab="health"]', "aria-selected") == "true"
    assert t.later_problems() == [], t.problems


def test_system_tab_follows_the_url_hash(demo_data, tab):
    t = tab()
    page = open_system(t, "reports")
    assert page.get_attribute('#sysTabs [data-tab="reports"]', "aria-selected") == "true" and page.is_hidden("#tab-health")
    page.click('#sysTabs [data-tab="logs"]')
    page.wait_for_selector("#tab-logs .op-log")
    assert page.url.endswith("#logs") and page.is_visible("#tab-logs") and page.is_hidden("#tab-reports")
    page.evaluate("() => { location.hash = '#reports'; }")                  # a link or the address bar changes the tab too
    page.wait_for_selector("#tab-reports .op-table-box, #tab-reports .op-sys")
    assert page.is_visible("#tab-reports") and page.is_hidden("#tab-logs")
    page.evaluate("() => { location.hash = '#nonsense'; }")
    page.wait_for_selector("#tab-health .op-kpis")                             # an unknown name falls back to Health
    assert t.later_problems() == [], t.problems


def test_system_collectors_tab_and_run_now(app, ops, demo_data, tab):
    t = tab()
    page = open_system(t, "collectors")
    page.wait_for_selector("#tab-collectors .op-table-box tbody tr")
    assert page.locator("#tab-collectors .op-table-box tbody tr").count() == 4
    assert "Offline" in text_of(page, "#tab-collectors") and "Online" in text_of(page, "#tab-collectors")
    assert "demo.sla.user" in text_of(page, "#tab-collectors") and "email.target_archive_name" in text_of(page, "#tab-collectors")
    assert "password" not in text_of(page, "#tab-collectors").lower().replace("password set", "").replace("a job without a password", "")
    page.click('button[aria-label="Run E-mail scan now"]')
    page.wait_for_selector("dialog.dlg")
    assert "Run" in text_of(page, "dialog.dlg") and ops.pending_run_requests() == []                         # nothing happens before the confirmation
    page.click("dialog.dlg button[type=submit]")
    page.wait_for_selector("#tab-collectors .op-chip:has-text('Waiting for the collector')")
    assert [r["job"] for r in ops.pending_run_requests()] == ["email"]
    assert "Requested" in text_of(page, "#toast")
    page.click('button[aria-label="Run ITSM now"]')
    page.wait_for_selector("dialog.dlg")
    page.click("dialog.dlg button:has-text('Cancel')")
    assert [r["job"] for r in ops.pending_run_requests()] == ["email"]
    # the report job warns that it SENDS e-mails
    page.click('button[aria-label="Run E-mail daily report now"]')
    page.wait_for_selector("dialog.dlg")
    assert "SENDS" in text_of(page, "dialog.dlg")
    page.click("dialog.dlg button:has-text('Cancel')")
    assert t.later_problems() == [], t.problems


def patch_json(page, pattern, patcher):
    """Let the page ask the real server, then change what it gets back (to try a state the demo data cannot produce)."""
    def handle(route):
        response = route.fetch()
        body = response.json()
        patcher(body)
        route.fulfill(response=response, json=body)
    page.route(pattern, handle)


PAUSED = {"paused": "auth", "paused_since": "2026-10-11T08:40:00", "paused_reason": "login failed - fix the password, retries paused",
          "last_status": "error", "last_error": "The login failed"}


def test_collectors_tab_and_home_show_a_paused_login(demo_data, tab):
    """The pages read `paused` / `paused_reason` of a job (the runner writes them into the heartbeat)."""
    t = tab()
    page = t.context.new_page()
    page.set_default_timeout(10000)
    patch_json(page, "**/api/ops/jobs", lambda body: body["jobs"]["sla"].update(PAUSED))
    patch_json(page, "**/api/ops/overview", lambda body: body["collectors"]["sla"].update(PAUSED))
    page.goto(f"{t.base}/system#collectors")
    page.wait_for_selector("#tab-collectors .op-table-box tbody tr")
    text = text_of(page, "#tab-collectors .op-table-box")
    assert "Login failed - paused" in text and "fix the password" in text
    page.goto(f"{t.base}/hub")
    page.wait_for_selector("#colBody .op-job")
    assert "Login failed - paused" in text_of(page, "#colBody")


@pytest.mark.xfail(strict=False, reason="calllog/ops/health.py job_state() does not forward paused / paused_reason from the heartbeat yet (backend file)")
def test_the_api_forwards_a_paused_job_to_the_pages(app, ops, admin, clock):
    demo.seed(ops, clock(), paused_job="sla", stale_job="")
    state = admin.get("/api/ops/jobs").get_json()["jobs"]["sla"]
    assert state["paused"] == "auth" and "retries paused" in state["paused_reason"]


def test_system_settings_pinned_keys_are_read_only(demo_data, tab):
    t = tab()
    page = open_system(t, "settings")
    page.click('#tab-settings .op-subtabs button:has-text("E-mail")')
    pinned = page.locator('.op-field[data-path="email.target_archive_name"]')
    assert pinned.locator("input").is_disabled() and "collector_config.json" in pinned.inner_text()
    assert page.locator('.op-field[data-path="email.interval_minutes"] input').is_enabled()
    page.click('#tab-settings .op-subtabs button:has-text("Security requests")')
    assert page.locator('.op-field[data-path="sla.username"] input').is_disabled()
    assert page.locator('.op-field[data-path="sla.base_url"] input').is_enabled()
    assert t.later_problems() == [], t.problems


def test_system_settings_edit_save_and_server_errors(app, ops, demo_data, tab):
    from calllog.ops import settings as hub_settings
    t = tab()
    page = open_system(t, "settings")
    page.click('#tab-settings .op-subtabs button:has-text("E-mail")')
    save, bar = page.locator(".op-savebar button.btn-primary"), page.locator(".op-savebar .op-dirty")
    assert save.is_disabled() and "No unsaved" in bar.inner_text()
    field = page.locator('.op-field[data-path="email.interval_minutes"]')
    field.locator("input").fill("0")                                                      # the server refuses this one
    assert "1 unsaved change" in bar.inner_text() and save.is_enabled()
    assert page.locator('#sysTabs [data-tab="settings"] .op-count').is_visible()          # the tab says so too
    save.click()
    field.locator(".op-field-error").wait_for()
    assert "between 1 and 1440" in field.locator(".op-field-error").inner_text() and "has-error" in field.get_attribute("class")
    assert "were not saved" in text_of(page, ".op-banner.op-t-danger")
    assert hub_settings.load(ops)["email"]["interval_minutes"] == 10                      # nothing was written
    field.locator("input").fill("20")
    assert field.locator(".op-field-error").is_hidden()                                   # the message goes when the field is edited
    page.locator('.op-field[data-path="email.sla_target_minutes"] input').fill("45")
    page.locator('.op-field[data-path="email.business_emails"] textarea').fill("a@x.example\nB@X.example\n\na@x.example")
    assert "3 unsaved changes" in bar.inner_text()
    save.click()
    page.wait_for_selector("#toast .toast-msg")
    assert "Settings saved" in text_of(page, "#toast")
    saved = hub_settings.load(ops)["email"]
    assert saved["interval_minutes"] == 20 and saved["sla_target_minutes"] == 45 and saved["business_emails"] == ["a@x.example", "b@x.example"]
    assert page.locator('.op-field[data-path="email.business_emails"] textarea').input_value() == "a@x.example\nb@x.example"   # normalised copy shown
    assert "No unsaved" in bar.inner_text() and save.is_disabled()
    assert any(a == "ops_settings_saved" for a, *_ in audit(app, t))
    assert t.later_problems("HTTP 400", "400 (BAD REQUEST)") == [], t.problems          # the one refused save is the only thing the console may say


def audit(app, t):
    day = NOW.date().isoformat()
    return [(e["action"], e.get("target")) for e in app.extensions["store"].read_audit(day, day)]


def test_system_settings_unsaved_changes_are_guarded(demo_data, tab):
    t = tab()
    page = open_system(t, "settings")
    page.locator('.op-field[data-path="general.active_hours"] input').fill("07:00-18:00")
    page.click('#sysTabs [data-tab="health"]')
    page.click('#sysTabs [data-tab="settings"]')
    assert page.locator('.op-field[data-path="general.active_hours"] input').input_value() == "07:00-18:00"      # a tab switch loses nothing
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.type), d.dismiss()))
    page.evaluate("() => { window.addEventListener('beforeunload', () => {}); }")
    prevented = page.evaluate("() => { const e = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(e); return e.defaultPrevented; }")
    assert prevented is True                                                                                   # the browser will ask before leaving
    page.click('.op-savebar button:has-text("Discard")')
    page.click("dialog.dlg button[type=submit]")
    page.wait_for_function("() => document.querySelector('.op-savebar .op-dirty').textContent.startsWith('No unsaved')")
    assert page.locator('.op-field[data-path="general.active_hours"] input').input_value() == "08:00-17:00"
    prevented = page.evaluate("() => { const e = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(e); return e.defaultPrevented; }")
    assert prevented is False
    assert t.later_problems() == [], t.problems


def test_system_classification_editor(app, ops, demo_data, tab):
    from calllog.ops import settings as hub_settings
    t = tab()
    page = open_system(t, "settings")
    page.click('#tab-settings .op-subtabs button:has-text("Classification")')
    text = text_of(page, "#tab-settings")
    assert "“Unclassified” is where an e-mail goes" in text and page.locator('a[href="/email#unclassified"]').count() == 1
    assert page.locator('.op-field[data-path="email.classification.address_team_map"] .op-row').count() == 2        # the demo's two mailbox rules
    # keyword rule: keyword -> team (never to Unclassified)
    rules = page.locator('.op-field[data-path="email.classification.system_team_map"]')
    rules.locator("button:has-text('Add a keyword rule')").click()
    row = rules.locator(".op-row").first
    assert "Unclassified" not in row.locator("select").inner_text()
    row.locator("input").fill("swift")
    row.locator("select").select_option("Third Party")
    page.locator('.op-field[data-path="email.classification.unclassified_team"] select').select_option("Flex Cube")
    page.locator('.op-field[data-path="email.classification.extra_no_action_keywords"] textarea').fill("للعلم فقط\nFYI only")
    # a keyword used twice is caught here, before the server sees it
    rules.locator("button:has-text('Add a keyword rule')").click()
    rules.locator(".op-row").nth(1).locator("input").fill("SWIFT")
    assert "appears twice" in rules.locator(".op-field-error").inner_text()
    assert page.locator(".op-savebar button.btn-primary").is_disabled()
    rules.locator(".op-row").nth(1).locator("button").click()
    assert rules.locator(".op-field-error").is_hidden()
    page.locator(".op-savebar button.btn-primary").click()
    page.wait_for_selector("#toast .toast-msg")
    cls = hub_settings.load(ops)["email"]["classification"]
    assert cls["system_team_map"] == {"swift": "Third Party"} and cls["unclassified_team"] == "Flex Cube"
    assert cls["extra_no_action_keywords"] == ["للعلم فقط", "FYI only"] and len(cls["address_team_map"]) == 2
    # removing the rule again
    page.locator('.op-field[data-path="email.classification.system_team_map"] .op-row button[aria-label^="Remove"]').click()
    page.locator(".op-savebar button.btn-primary").click()
    page.wait_for_function("() => document.querySelector('.op-savebar .op-dirty').textContent.startsWith('No unsaved')")
    assert hub_settings.load(ops)["email"]["classification"]["system_team_map"] == {}
    assert t.later_problems() == [], t.problems


def test_system_settings_team_lists_feed_the_team_selects(ops, demo_data, tab):
    t = tab()
    page = open_system(t, "settings")
    page.click('#tab-settings .op-subtabs button:has-text("Classification")')
    select = page.locator('.op-field[data-path="email.classification.unclassified_team"] select')
    assert "Rapid Response" not in select.inner_text()
    page.click('#tab-settings .op-subtabs button:has-text("E-mail")')
    teams = page.locator('.op-field[data-path="email.teams"] textarea')
    teams.fill(teams.input_value() + "\nRapid Response")
    page.click('#tab-settings .op-subtabs button:has-text("Classification")')
    assert "Rapid Response" in select.inner_text()                                    # the new team can be chosen at once
    assert t.later_problems() == [], t.problems


def test_system_settings_schedule_and_reports_sections(ops, demo_data, tab):
    from calllog.ops import settings as hub_settings
    t = tab()
    page = open_system(t, "settings")
    page.click('#tab-settings .op-subtabs button:has-text("SLA") , #tab-settings .op-subtabs button:has-text("Security requests")')
    own = page.locator('.op-field[data-path="sla.schedule"]')
    assert own.locator("input[role=switch]").is_checked() and own.locator(".op-days").count() == 0
    own.locator(".op-switch").click()                                              # give the module its own window
    assert own.locator(".op-days input:checked").count() == 5
    own.locator("input[type=text]").fill("06:00-22:00")
    page.locator(".op-savebar button.btn-primary").click()
    page.wait_for_selector("#toast .toast-msg")
    sched = hub_settings.load(ops)["sla"]["schedule"]
    assert sched["active_hours"] == "06:00-22:00" and sched["work_days"] == ["Sun", "Mon", "Tue", "Wed", "Thu"]
    page.click('#tab-settings .op-subtabs button:has-text("E-mail reports")')
    times = page.locator('.op-field[data-path="email.report.send_times"] input')
    assert times.input_value() == "16:30"
    times.fill("16:30, 07:45")
    page.locator('.op-field[data-path="email.report.team_recipients"] button:has-text("Add a team")').click()
    page.locator('.op-field[data-path="email.report.team_recipients"] .op-row textarea').first.fill("lead@x.example")
    page.locator(".op-savebar button.btn-primary").click()
    page.wait_for_selector("#toast .toast-msg:nth-of-type(2), #toast .toast-msg")
    page.wait_for_function("() => document.querySelector('.op-savebar .op-dirty').textContent.startsWith('No unsaved')")
    report = hub_settings.load(ops)["email"]["report"]
    assert report["send_times"] == ["07:45", "16:30"] and list(report["team_recipients"].values())[0]["to"] == ["lead@x.example"]
    assert t.later_problems() == [], t.problems


def test_system_settings_advanced_json_and_server_side_list_errors(ops, demo_data, tab):
    from calllog.ops import settings as hub_settings
    t = tab()
    page = open_system(t, "settings")
    page.click('#tab-settings .op-subtabs button:has-text("E-mail")')
    page.locator("#tab-settings details summary:has-text('Advanced')").click()
    box = page.locator('.op-field[data-path="email.advanced"]')
    box.locator("textarea").fill("{ not json")
    assert "Not valid JSON" in box.locator(".op-field-error").inner_text()
    assert page.locator(".op-savebar button.btn-primary").is_disabled()                 # a typo never reaches the server
    box.locator("textarea").fill('{"extra_keywords": ["fyi"]}')
    assert box.locator(".op-field-error").is_hidden()
    page.locator('.op-field[data-path="email.manager_emails"] textarea').fill("not-an-address")
    page.locator(".op-savebar button.btn-primary").click()
    page.locator('.op-field[data-path="email.manager_emails"] .op-field-error').wait_for()
    assert "not an e-mail address" in page.locator('.op-field[data-path="email.manager_emails"] .op-field-error').inner_text()
    assert hub_settings.load(ops)["email"]["advanced"] == {}                              # all or nothing: the valid part was not saved either
    page.locator('.op-field[data-path="email.manager_emails"] textarea').fill("boss@x.example")
    page.locator(".op-savebar button.btn-primary").click()
    page.wait_for_function("() => document.querySelector('.op-savebar .op-dirty').textContent.startsWith('No unsaved')")
    saved = hub_settings.load(ops)["email"]
    assert saved["advanced"] == {"extra_keywords": ["fyi"]} and saved["manager_emails"] == ["boss@x.example"]
    assert t.later_problems("HTTP 400", "400 (BAD REQUEST)") == [], t.problems


def test_system_logs_and_reports_tabs(admin, ops, demo_data, tab):
    t = tab()
    page = open_system(t, "logs")
    page.wait_for_selector("#tab-logs .op-log")
    page.wait_for_function("() => document.querySelector('#tab-logs .op-log').textContent.includes('Finished OK')")
    assert page.inner_text("#tab-logs .op-log").count("\n") == 2                            # three lines, as seeded
    page.click('#tab-logs button:has-text("Security requests")')
    page.wait_for_function("() => document.querySelector('#tab-logs .op-log').textContent.includes('portal scan')")
    assert page.get_attribute('#tab-logs button:has-text("Security requests")', "aria-pressed") == "true"
    page = open_system(t, "reports")
    page.wait_for_selector("#tab-reports tbody tr")
    assert page.locator("#tab-reports tbody tr").count() == 3
    link = page.locator('#tab-reports a[href^="/export/ops/report/email/"]')
    assert link.get_attribute("download").endswith(".xlsx")
    with page.expect_download() as info:
        link.click()
    assert info.value.suggested_filename.endswith(".xlsx")
    assert t.later_problems() == [], t.problems


def test_system_is_calm_without_any_collector_data(tab, ops):
    t = tab()
    page = open_system(t, "health")
    page.wait_for_selector("#tab-health .op-kpis")
    assert "No email data yet" in text_of(page, "#tab-health .op-problems")           # an empty folder is said plainly, not as a failure
    assert page.locator("#tab-health .op-problems li.op-t-danger").count() == 0
    page = open_system(t, "collectors")
    page.wait_for_selector("#tab-collectors .op-table-box tbody tr")
    assert "Not started" in text_of(page, "#tab-collectors") and "No collector has introduced itself" in text_of(page, "#tab-collectors")
    page = open_system(t, "reports")
    page.wait_for_selector("#tab-reports .op-empty")
    assert "No reports yet" in text_of(page, "#tab-reports")
    page = open_system(t, "logs")
    page.wait_for_function("() => document.querySelector('#tab-logs p.hint').textContent.includes('empty')")
    assert t.later_problems() == [], t.problems


def test_pages_work_on_a_1280_laptop_and_a_tablet(demo_data, tab):
    for width, height in ((1280, 720), (820, 1100)):
        t = tab(width=width, height=height)
        for path, ready in (("/hub", "#hubCards .op-module"), ("/system#health", "#tab-health .op-kpis"), ("/system#collectors", "#tab-collectors .op-table-box tbody tr"),
                            ("/system#settings", "#tab-settings .op-form")):
            page = t.open(path, ready)
            page.wait_for_timeout(200)
            overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 1, (width, path, overflow)                               # no sideways scrolling of the page
        assert t.later_problems() == [], t.problems


@pytest.mark.skipif(not os.environ.get("CALLLOG_SCREENSHOTS"), reason="set CALLLOG_SCREENSHOTS=<folder> to write the documentation screenshots")
def test_documentation_screenshots(demo_data, tab):
    folder = Path(os.environ["CALLLOG_SCREENSHOTS"])
    folder.mkdir(parents=True, exist_ok=True)
    t = tab()
    page = t.open("/hub", "#hubCards .op-module")
    page.wait_for_selector("#tlBody .op-tl-item")
    page.wait_for_timeout(600)
    page.screenshot(path=str(folder / "ops-hub-home.png"))
    page = t.open("/system#collectors", "#tab-collectors .op-table-box tbody tr")
    page.wait_for_timeout(600)
    page.screenshot(path=str(folder / "ops-system.png"))
    assert t.later_problems() == [], t.problems
