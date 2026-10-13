"""The "sla" job: how long has every security request been with its security user (port of the old SLA.PY monitor).

It logs in to the SSO portal with Playwright, opens the "User Provisioning Portal - موظف اسناد - مستخدم السرية" package,
jumps to the Re-Assign page, reads the requests table (plain tables with paging and DataTables through their API), and
measures the age of every request since "تاريخ الارسال للسرية". Entry point (docs/HUB-DESIGN.md sections 5.1, 6, 12):

    run(ctx, scraper=None) -> JobResult     what the runner calls; ``scraper(ctx) -> (headers, rows)`` replaces the browser in tests

SLA is two hours: from ``warning_minutes`` a request is a *reminder*, from ``breach_minutes`` it is *breached*. The result
is a snapshot (``requests`` with a stable ``key`` hashed from the identifying columns, ``by_user``, ``totals``), a history
point and ``first_breach`` for the day file. The runner writes snapshot, history and heartbeat; this module writes only the
Excel report (``SLA_latest.xlsx`` always, a timestamped copy with every e-mail, newest 30 kept) and its private alert state
(``ctx.store`` state "sla": ``{"alerted": {key: level}, "last_sent": iso}``).

E-mail rule (unchanged from the old script): send when a request reaches a higher level than the one already alerted
(first reminder, then breach), or every ``repeat_minutes`` while anything is still overdue (0 = only on escalation).
"Run now" with ``options["send_mail"]`` sends regardless. Nothing is sent when ``ctx.can_email()`` is false, ``mail.send`` is
off or ``mail.to`` is empty; in that case the alerts stay pending, so they go out as soon as mail is possible. Mail goes
through ``ctx.mailer`` (an Outlook sender is created lazily when the runner gave none). A failed scan, or a failed mail,
never changes the snapshot; a failed scan never changes the alert state either, and a failed mail leaves the alerts pending
so the next scan retries it.

The account comes from ``ctx.settings["username"]`` and the password only from ``ctx.password("sla", username)``. The
password is never logged, put in an exception text, a file name or a saved page: every error text and debug HTML is
scrubbed of it, and debug HTML is written without any input values.

Only the standard library and openpyxl are needed to import this module; the browser is driven over the DevTools
protocol (``calllog.ops.engines.cdp``) - no Playwright, no Node.js and no browser download.
"""
import hashlib
import os
import re
import subprocess
import sys
import time
from collections import Counter
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import json

from .. import schemas
from . import cdp
from ..settings import DEFAULTS, deep_merge
from .common import (
    FONT_STACK, NBE_GOLD, NBE_GREEN, NBE_GREEN_LIGHT, CredentialError, JobResult, OutlookMailer, esc, norm_arabic)

STATE_NAME = "sla"
LATEST_REPORT = "SLA_latest.xlsx"
REPORT_PREFIX = "SLA_Report_"
KEEP_REPORT_COPIES = 30
COPY_NAME = re.compile(r"^SLA_Report_\d{8}_\d{4}\.xlsx$")
MAX_EMAIL_ROWS = 200

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
LOGO_FILE = ASSETS_DIR / "nbe_logo.png"
LOGO_CID = "nbe_logo"

SETTLE_MS = 1500            # wait after a page opens / a search runs, before the tables are read
PAGE_MS = 700               # wait after clicking "next" on a plain paged table
MAX_PAGES = 500
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
UNASSIGNED = "(Unassigned)"

LEVEL_REMINDER, LEVEL_BREACHED = 1, 2
OVERDUE = (schemas.SLA_REMINDER, schemas.SLA_BREACHED)

# The old script's English labels (Excel and e-mail); the snapshot carries the stable codes instead.
LABELS = {schemas.SLA_WITHIN: "WITHIN SLA", schemas.SLA_REMINDER: "REMINDER",
          schemas.SLA_BREACHED: "SLA BREACHED", schemas.SLA_NO_DATE: "NO SENT DATE"}
COLORS = {  # (text, background)
    schemas.SLA_BREACHED: ("#B71C1C", "#FDECEA"),
    schemas.SLA_REMINDER: ("#8A4B00", "#FFF3E0"),
    schemas.SLA_WITHIN: ("#1B5E20", "#E8F5E9"),
    schemas.SLA_NO_DATE: ("#455A64", "#ECEFF1"),
}


class PortalError(RuntimeError):
    pass


class LoginError(PortalError):
    """Wrong / expired account: retrying only risks locking it, so the job result says error_kind "auth"."""


# --------------------------------------------------------------------------- text and dates
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
DATE_DMY = re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})")
DATE_YMD = re.compile(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|ص|م)?", re.I)
NOT_IDENTIFYING = re.compile("اسناد|ملاحظات|تفاصيل")      # reassigning / adding a note must not make a "new" request

norm = norm_arabic


def scrub(text, secrets=()):
    """``text`` with every secret (and its URL-encoded form) replaced by ***."""
    text = str(text)
    for secret in secrets:
        if secret:
            for form in {secret, quote(secret, safe=""), quote(secret)}:
                text = text.replace(form, "***")
    return text


def clean_error(exc, secrets=()):
    """One short line for the heartbeat / System page: the first lines of the exception, scrubbed of secrets."""
    text = str(exc).strip() or type(exc).__name__
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:6]
    return scrub(" | ".join(lines)[:600], secrets)


def fmt_age(minutes):
    if minutes is None:
        return "-"
    minutes = int(minutes)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def fmt_hours(minutes):
    return f"{minutes / 60:g}h"


def parse_datetime(text, date_order="dmy"):
    """dd/mm/yyyy (or mm/dd with date_order "mdy", or yyyy-mm-dd) with optional time; Arabic digits; AM/PM or ص/م."""
    s = str(text or "").translate(ARABIC_DIGITS)
    m = DATE_YMD.search(s)
    if m:
        year, month, day = map(int, m.groups())
    else:
        m = DATE_DMY.search(s)
        if not m:
            return None
        a, b, year = map(int, m.groups())
        day, month = (b, a) if date_order == "mdy" else (a, b)
    rest = s[:m.start()] + " " + s[m.end():]
    hh = mm = ss = 0
    t = TIME_RE.search(rest)
    if t:
        hh, mm = int(t.group(1)), int(t.group(2))
        ss = int(t.group(3) or 0)
        ampm = (t.group(4) or "").upper()
        if ampm in ("PM", "م") and hh < 12:
            hh += 12
        elif ampm in ("AM", "ص") and hh == 12:
            hh = 0
    try:
        return datetime(year, month, day, hh, mm, ss)
    except ValueError:
        return None


def find_col(columns, wanted):
    """The column whose normalised name equals ``wanted``, else the first that contains it."""
    w = norm(wanted)
    for c in columns:
        if norm(c) == w:
            return c
    for c in columns:
        if w in norm(c):
            return c
    return None


# --------------------------------------------------------------------------- browser (Playwright)
# \u escapes = tatweel, the diacritics range, alef / taa marbuta / alef maqsura variants (same as common.norm_arabic)
JS_NORM = r"""
  const norm = s => (s || "").replace(/ـ/g, "").replace(/[ً-ْ]/g, "")
      .replace(/[إأآ]/g, "ا").replace(/ة/g, "ه").replace(/ى/g, "ي")
      .replace(/\s+/g, " ").trim().toLowerCase();
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const textOf = el => norm(el.tagName === "INPUT" ? (el.value || "") : (el.innerText || el.textContent));
"""

# Reads every table on the page. DataTables are read through their API so all pages are included.
JS_READ_TABLES = "() => {" + r"""
  const cellText = n => {
    if (!n) return "";
    const c = n.cloneNode(true);
    c.querySelectorAll("br").forEach(b => b.replaceWith(" "));
    c.querySelectorAll("script, style").forEach(s => s.remove());
    return (c.textContent || "").replace(/\s+/g, " ").trim();
  };
  const strip = v => {
    if (v === null || v === undefined || typeof v === "object") return "";
    const d = document.createElement("div");
    d.innerHTML = String(v).replace(/<br\s*\/?>/gi, " ");
    return (d.textContent || "").replace(/\s+/g, " ").trim();
  };
  const out = [], seen = new Set();
  const DT = (window.jQuery && window.jQuery.fn && window.jQuery.fn.dataTable) || window.DataTable;
  if (DT && DT.tables && DT.Api) {
    for (const node of DT.tables()) {
      const api = new DT.Api(node);
      const cols = api.columns().indexes().toArray();
      const headers = cols.map(c => cellText(api.column(c).header()));
      const rows = api.rows().indexes().toArray().map(r => cols.map(c => {
        const n = api.cell(r, c).node();
        return n ? cellText(n) : strip(api.cell(r, c).data());
      }));
      out.push({source: "datatables", headers, rows});
      seen.add(node);
    }
  }
  for (const t of document.querySelectorAll("table")) {
    if (seen.has(t)) continue;
    const headRows = t.tHead ? [...t.tHead.rows] : [];
    let head = headRows.length ? [...headRows[headRows.length - 1].cells] : [];
    if (!head.length && t.rows.length && [...t.rows[0].cells].every(c => c.tagName === "TH"))
      head = [...t.rows[0].cells];
    const body = [...t.tBodies].flatMap(b => [...b.rows]).filter(tr => tr.cells.length > 1);
    out.push({source: "dom", headers: head.map(cellText), rows: body.map(tr => [...tr.cells].map(cellText))});
  }
  return out;
}"""

# The page without any typed or stored input value (buttons keep theirs: their text helps to find the right selector).
JS_REDACTED_HTML = "() => {" + r"""
  const root = document.documentElement.cloneNode(true);
  root.querySelectorAll("input, textarea").forEach(el => {
    const type = (el.getAttribute("type") || "text").toLowerCase();
    if (["submit", "button", "image", "reset"].includes(type)) return;
    el.removeAttribute("value");
    if (el.tagName === "TEXTAREA") el.textContent = "";
  });
  return "<!DOCTYPE html>\n" + root.outerHTML;
}"""


# The table-reading and page-dump scripts are arrow functions; CDP evaluates a complete expression, so they are called.
READ_TABLES = "(" + JS_READ_TABLES + ")()"
REDACTED_HTML = "(" + JS_REDACTED_HTML + ")()"



def js_click_text(text):
    """Find the best clickable element whose text contains ``text``; return its centre {x, y}, or null."""
    return ("(() => {" + JS_NORM
            + "const R = el => { try{el.scrollIntoView({block:'center'});}catch(e){} const r = el.getBoundingClientRect(); return {x: r.left + r.width/2, y: r.top + r.height/2}; };"
            + "const t = norm(" + json.dumps(text) + ");"
            + "const sel = \"a, button, input[type=submit], input[type=button], input[type=image], [onclick], [role=button], li, span\";"
            + "const cands = [...document.querySelectorAll(sel)].filter(visible).filter(el => textOf(el).includes(t));"
            + "if (!cands.length) return null;"
            + "const score = el => (/^(A|BUTTON|INPUT)$/.test(el.tagName) ? 0 : 10) + (textOf(el) === t ? 0 : 5) + Math.min(textOf(el).length/100, 4);"
            + "cands.sort((a,b) => score(a)-score(b));"
            + "return R(cands[0]); })()")


def js_click_selector(selector):
    """Centre {x, y} of the first visible element matching ``selector``, or null."""
    return ("(() => {" + JS_NORM
            + "const R = el => { try{el.scrollIntoView({block:'center'});}catch(e){} const r = el.getBoundingClientRect(); return {x: r.left + r.width/2, y: r.top + r.height/2}; };"
            + "const el = [...document.querySelectorAll(" + json.dumps(selector) + ")].filter(visible)[0];"
            + "return el ? R(el) : null; })()")


def js_open_package(target, link_text):
    """Centre {x, y} of the package row's open control (prefer one whose text has ``link_text``). 'notfound' if no row."""
    return ("(() => {" + JS_NORM
            + "const R = el => { try{el.scrollIntoView({block:'center'});}catch(e){} const r = el.getBoundingClientRect(); return {x: r.left + r.width/2, y: r.top + r.height/2}; };"
            + "const loose = s => s.replace(/[\\s\\-\u2013_]/g,'');"
            + "const t = loose(norm(" + json.dumps(target) + ")), lt = norm(" + json.dumps(link_text or "") + ");"
            + "const rows = [...document.querySelectorAll('tr')].filter(visible).filter(r => loose(textOf(r)).includes(t));"
            + "if (!rows.length) return 'notfound';"
            + "rows.sort((a,b) => textOf(a).length - textOf(b).length);"
            + "const row = rows[0];"
            + "const ctls = [...row.querySelectorAll(\"a, button, input[type=submit], input[type=button], input[type=image], [onclick]\")].filter(visible);"
            + "const ctl = ctls.find(c => lt && textOf(c).includes(lt)) || ctls[ctls.length-1] || row;"
            + "return R(ctl); })()")


def js_next_page():
    """Centre {x, y} of the DataTables 'next' button when it is enabled, else null."""
    return ("(() => {" + 
            "const el = el => el; const R = e => { try{e.scrollIntoView({block:'center'});}catch(x){} const r = e.getBoundingClientRect(); return {x: r.left + r.width/2, y: r.top + r.height/2}; };"
            + "const n = document.querySelector('.dataTables_paginate .next:not(.disabled), .paginate_button.next:not(.disabled)');"
            + "return n ? R(n) : null; })()")


def js_fill_login(user_sel, pass_sel, username, password):
    """Set the user and password inputs (by selector, or the first visible text / password input) and fire input/change."""
    return ("(() => {" + JS_NORM
            + "const uSel=" + json.dumps(user_sel or "") + ", pSel=" + json.dumps(pass_sel or "") + ";"
            + "const u = uSel ? document.querySelector(uSel) : [...document.querySelectorAll(\"input[type=text], input:not([type])\")].filter(visible)[0];"
            + "const p = pSel ? document.querySelector(pSel) : [...document.querySelectorAll(\"input[type=password]\")].filter(visible)[0];"
            + "const set=(el,val)=>{if(!el)return false; try{el.focus();}catch(e){} el.value=val; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true})); return true;};"
            + "return {user:set(u," + json.dumps(str(username)) + "), pass:set(p," + json.dumps(password) + ")}; })()")


def _click(browser, finder_expr):
    """Run ``finder_expr`` (returns a point {x, y} or null); do a trusted mouse click there. Returns True if clicked."""
    point = browser.eval(finder_expr)
    if not isinstance(point, dict):
        return False
    browser.click_at(point["x"], point["y"])
    return True


class Debug:
    """Screenshot + page HTML of every step, only when ``ctx.debug``. The HTML has no input values and no password."""

    def __init__(self, enabled, folder, secrets=()):
        self.enabled = enabled
        self.folder = Path(folder)
        self.secrets = list(secrets)
        self.n = 0

    def snap(self, browser, name):
        if not self.enabled:
            return
        with suppress(OSError):
            self.folder.mkdir(parents=True, exist_ok=True)
        self.n += 1
        base = self.folder / f"{self.n:02d}_{name}_0"
        try:
            browser.screenshot(f"{base}.png")
        except Exception:
            pass
        try:
            page_html = scrub(browser.eval(REDACTED_HTML), self.secrets)
            Path(f"{base}.html").write_text(page_html, encoding="utf-8")
        except Exception:
            pass


def debug_folder(ctx):
    root = Path(ctx.data_dir) / "ops" if ctx.data_dir else Path(ctx.store.root)
    return root / "debug" / "sla"


def open_browser(ctx, cfg, headless):
    """The real browser for a scan: the PC's Edge / Chrome driven over the DevTools protocol (no Playwright). Tests
    monkeypatch this to inject a scripted browser."""
    bcfg = {"browser_executable": cfg.get("browser_executable") or "",
            "cdp_port": cfg.get("cdp_port") or 0,
            "profile_dir": cfg.get("profile_dir") or ""}
    return cdp.CdpBrowser(bcfg, log=ctx.log, headless=headless, profile_leaf="sla_profile")


def _press_enter(browser):
    """Press Enter in the focused password field (a real, trusted key event), to submit a login form that has no button
    whose text we recognise."""
    try:
        browser.eval("(() => { const p=[...document.querySelectorAll('input[type=password]')]"
                     ".filter(e => e.offsetParent !== null)[0]; if (p) p.focus(); return !!p; })()")
    except Exception:
        pass
    for kind in ("rawKeyDown", "char", "keyUp"):
        try:
            browser.call("Input.dispatchKeyEvent", type=kind, key="Enter", code="Enter", text="\r",
                         windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
        except Exception:
            pass


def _sleep_ms(ms):
    time.sleep(max(0, int(ms)) / 1000.0)


def pick_table(tables, cfg):
    """The table that has both the sent-date and the owner column; a DataTable (all pages) beats a plain one."""
    best = None
    for t in tables:
        if find_col(t["headers"], cfg["sent_date_header"]) and find_col(t["headers"], cfg["owner_header"]):
            key = (t["source"] == "datatables", len(t["rows"]))
            if best is None or key > best[0]:
                best = (key, t)
    return best[1] if best else None


def read_requests(browser, cfg, debug):
    """(headers, rows) of the requests table on the Re-Assign page."""
    _sleep_ms(SETTLE_MS)
    table = pick_table(browser.eval(READ_TABLES), cfg)
    if table is None or not table["rows"]:
        # results not loaded yet -> press the search button
        if _click(browser, js_click_text(cfg["search_button_text"])):
            _sleep_ms(SETTLE_MS)
            browser.wait_ready(int(cfg["timeout_seconds"]))
            _sleep_ms(SETTLE_MS)
            table = pick_table(browser.eval(READ_TABLES), cfg)
    debug.snap(browser, "results")
    if table is None:
        seen = [t["headers"] for t in browser.eval(READ_TABLES) if t["headers"]]
        raise PortalError("Requests table not found. Headers seen on the page: "
                          + " | ".join(", ".join(h) for h in seen))

    rows = list(table["rows"])
    if table["source"] == "dom":
        # plain paging: click "next" until it is disabled or nothing new arrives
        for _ in range(MAX_PAGES):
            if not _click(browser, js_next_page()):
                break
            _sleep_ms(PAGE_MS)
            t = pick_table(browser.eval(READ_TABLES), cfg)
            new = [r for r in (t["rows"] if t else []) if r not in rows]
            if not new:
                break
            rows.extend(new)
    return table["headers"], rows


def url_marker(path, default, keep_extension=False):
    """The file name of a portal path, lower-case: what must (not) appear in the URL once we are there."""
    name = urlparse(path or "").path.rstrip("/").rsplit("/", 1)[-1]
    return (name if keep_extension else os.path.splitext(name)[0]).lower() or default


def scrape(ctx, username, password):
    """Log in, open the package, open the Re-Assign page, read the table. Returns (headers, rows).

    Driven over the DevTools protocol (no Playwright): the PC's Edge / Chrome is launched with a debugging port and
    walked through the pages with small JavaScript snippets. The debug dumps hold no input value and no password."""
    cfg = _config(ctx)
    timeout_s = int(cfg["timeout_seconds"])
    base = cfg["base_url"].rstrip("/")
    sel = cfg.get("selectors") or {}
    login_marker = url_marker(cfg["login_path"], "login.aspx", keep_extension=True)
    reassign_marker = url_marker(cfg["reassign_path"], "request_reassign")
    debug = Debug(ctx.debug, debug_folder(ctx), [password])

    browser = open_browser(ctx, cfg, headless=not ctx.headed)
    try:
        # 1) Login. The page is dumped BEFORE the password is typed.
        browser.navigate(base + cfg["login_path"], timeout=timeout_s)
        debug.snap(browser, "login")
        browser.eval(js_fill_login(sel.get("username"), sel.get("password"), username, password))
        if sel.get("login_button"):
            _click(browser, js_click_selector(sel["login_button"]))
        elif not _click(browser, js_click_text(cfg["login_button_text"])):
            _press_enter(browser)
        if browser.wait_url(lambda u: login_marker not in u.lower(), timeout=timeout_s) is None:
            raise LoginError("Login failed - still on the login page. Check the SLA user and password "
                             "in collector_config.json (sla.username / sla.password).")
        browser.wait_ready(timeout_s)
        debug.snap(browser, "groups")

        # 2) Open the package (may open in a new window, or load in the same tab)
        pkg = browser.eval(js_open_package(cfg["package_text"], cfg["package_link_text"]))
        if pkg == "notfound" or not isinstance(pkg, dict):
            raise PortalError(f"Package not found on the groups page: {cfg['package_text']}")
        browser.click_at(pkg["x"], pkg["y"])
        browser.attach(cfg["app_url_fragment"].lower(), timeout=timeout_s)
        debug.snap(browser, "app_home")

        # 3) Re-Assign page
        browser.navigate(base + cfg["reassign_path"], timeout=timeout_s)
        if reassign_marker not in (browser.current_url() or "").lower():
            raise PortalError(f"Could not open the Re-Assign page (ended on {scrub(browser.current_url(), [password])})")
        return read_requests(browser, cfg, debug)
    except Exception:
        try:
            debug.snap(browser, "error")
        except Exception:
            pass
        raise
    finally:
        browser.close()


# --------------------------------------------------------------------------- analysis
def _text(value):
    return "" if value is None else str(value)


def build_table(headers, rows):
    """Page table -> (columns, rows): unique non-empty column names, rows padded / cut to the width, and empty helper
    columns (checkboxes, icons: nameless and blank in every row) dropped."""
    cols, used = [], set()
    for i, h in enumerate(headers):
        name = _text(h).strip() or f"col_{i + 1}"
        while name in used:
            name += "_"
        used.add(name)
        cols.append(name)
    width = len(cols)
    body = [(list(map(_text, r)) + [""] * width)[:width] for r in rows]
    keep = [i for i, c in enumerate(cols)
            if not (c.startswith("col_") and all(not row[i].strip() for row in body))]
    return [cols[i] for i in keep], [[row[i] for i in keep] for row in body]


def status_of(age, warn, breach):
    if age is None:
        return schemas.SLA_NO_DATE
    return schemas.SLA_BREACHED if age >= breach else schemas.SLA_REMINDER if age >= warn else schemas.SLA_WITHIN


def request_key(columns, cells, owner_col):
    """Stable id of a request: a hash of the identifying columns (not the owner, notes or assignee, so reassigning a
    request does not make it a "new" one)."""
    idx = [i for i, c in enumerate(columns) if c != owner_col and not NOT_IDENTIFYING.search(norm(c))
           and not c.startswith("col_")]
    if not idx:
        idx = [i for i, c in enumerate(columns) if not c.startswith("col_")] or list(range(len(columns)))
    raw = "|".join(cells[i] for i in idx)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def analyze(columns, rows, cfg, now):
    """One record per request, oldest first (no date last; equal ages keep the page order).

    Record: key, security_user, sent_at (iso | None), age_minutes (| None), status, fields {detail header: text},
    plus private ``_sent`` (datetime) and ``_cells`` (the page row) used by the Excel report."""
    sent_col = find_col(columns, cfg["sent_date_header"])
    owner_col = find_col(columns, cfg["owner_header"])
    if sent_col is None or owner_col is None:
        raise PortalError("Requests table not found. Headers seen on the page: " + ", ".join(columns))
    sent_i, owner_i = columns.index(sent_col), columns.index(owner_col)
    warn, breach = int(cfg["warning_minutes"]), int(cfg["breach_minutes"])

    details, taken = [], set()
    for wanted in cfg["detail_headers"]:
        col = find_col(columns, wanted)
        if col is not None and col not in taken:
            taken.add(col)
            details.append((wanted, columns.index(col)))

    out = []
    for cells in rows:
        sent = parse_datetime(cells[sent_i], cfg["date_order"])
        age = None if sent is None else max(0, round((now - sent).total_seconds() / 60))
        out.append({
            "key": request_key(columns, cells, owner_col),
            "security_user": cells[owner_i].strip() or UNASSIGNED,
            "sent_at": sent.isoformat(timespec="seconds") if sent else None,
            "age_minutes": age,
            "status": status_of(age, warn, breach),
            "fields": {name: cells[i] for name, i in details},
            "_sent": sent, "_cells": cells,
        })
    out.sort(key=lambda r: (r["age_minutes"] is None, -(r["age_minutes"] or 0)))
    return out


def _counts(requests):
    by_status = Counter(r["status"] for r in requests)
    ages = [r["age_minutes"] for r in requests if r["age_minutes"] is not None]
    return {"within": by_status[schemas.SLA_WITHIN], "reminder": by_status[schemas.SLA_REMINDER],
            "breached": by_status[schemas.SLA_BREACHED], "no_date": by_status[schemas.SLA_NO_DATE],
            "oldest_minutes": max(ages, default=0)}


def summarize(requests):
    """Per security user, worst first: breached desc, reminder desc, total desc (then name)."""
    groups = {}
    for r in requests:
        groups.setdefault(r["security_user"], []).append(r)
    rows = [{"user": user, "total": len(items), **_counts(items)} for user, items in sorted(groups.items())]
    rows.sort(key=lambda s: (-s["breached"], -s["reminder"], -s["total"]))
    return rows


def totals_of(requests):
    return {"open": len(requests), **_counts(requests)}


def snapshot_data(requests, by_user, cfg):
    """The ``data`` of the sla snapshot (HUB-DESIGN 5.1)."""
    return {
        "thresholds": {"warning_minutes": int(cfg["warning_minutes"]), "breach_minutes": int(cfg["breach_minutes"])},
        "totals": totals_of(requests),
        "by_user": by_user,
        "requests": [{k: v for k, v in r.items() if not k.startswith("_")} for r in requests],
    }


# --------------------------------------------------------------------------- alert state and decision
def load_state(ctx):
    """{"alerted": {key: level}, "last_sent": datetime | None}; a missing or damaged file is a fresh state."""
    try:
        raw = ctx.store.read_state(STATE_NAME) or {}
    except Exception as exc:
        ctx.log(f"SLA alert state could not be read ({type(exc).__name__}); starting fresh")
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    alerted = {str(k): int(v) for k, v in (raw.get("alerted") or {}).items()
               if isinstance(v, int) and not isinstance(v, bool)} if isinstance(raw.get("alerted"), dict) else {}
    try:
        last_sent = datetime.fromisoformat(raw["last_sent"]) if raw.get("last_sent") else None
    except (TypeError, ValueError):
        last_sent = None
    return {"alerted": alerted, "last_sent": last_sent}


def alert_levels(requests):
    return {r["key"]: LEVEL_BREACHED if r["status"] == schemas.SLA_BREACHED else LEVEL_REMINDER
            for r in requests if r["status"] in OVERDUE}


def decide_alert(state, levels, now, repeat_minutes, force=False):
    """Should an e-mail go out now? Returns {"send", "escalated": [keys], "repeat": bool}."""
    escalated = [k for k, lv in levels.items() if lv > state["alerted"].get(k, 0)]
    since = None
    if state["last_sent"]:
        since = (now - state["last_sent"]).total_seconds() / 60
    repeat = int(repeat_minutes or 0)
    # a negative gap means the clock moved back (daylight saving, another PC): do not wait for it
    due_repeat = bool(levels) and repeat > 0 and (since is None or since < 0 or since >= repeat)
    return {"send": bool(force or escalated or due_repeat), "escalated": escalated, "repeat": due_repeat}


def next_state(state, levels, sent, now):
    """After a sent mail the levels are raised (never lowered); otherwise only closed / reassigned requests are forgotten."""
    if sent:
        alerted = {k: max(lv, state["alerted"].get(k, 0)) for k, lv in levels.items()}
        last_sent = now
    else:
        alerted = {k: v for k, v in state["alerted"].items() if k in levels}
        last_sent = state["last_sent"]
    return {"alerted": alerted, "last_sent": last_sent.isoformat(timespec="seconds") if last_sent else None}


# --------------------------------------------------------------------------- Excel
def _excel_text(value):
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    return ILLEGAL_CHARACTERS_RE.sub("", value)


def _append(ws, values):
    """Append a row; text is always stored as text, so a scraped '=HYPERLINK(...)' can never run as a formula."""
    ws.append([None] * len(values))
    for cell, value in zip(ws[ws.max_row], values):
        if isinstance(value, str):
            cell.value = _excel_text(value)
            cell.data_type = "s"
        else:
            cell.value = value


def build_workbook(columns, requests, by_user):
    """Workbook with the three sheets of the old report: By Security User / Over 1 Hour / All Requests."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    first = ["SECURITY_USER", "SENT_AT", "AGE_MIN", "AGE", "SLA_STATUS"]

    def request_row(r):
        sent = r["_sent"].strftime("%d/%m/%Y %H:%M") if r["_sent"] else ""
        return [r["security_user"], sent, r["age_minutes"], fmt_age(r["age_minutes"]), LABELS[r["status"]], *r["_cells"]]

    wb = Workbook()
    sheets = [
        ("By Security User", ["SECURITY_USER", "TOTAL", "WITHIN_SLA", "REMINDER", "BREACHED", "NO_DATE", "OLDEST"],
         [[s["user"], s["total"], s["within"], s["reminder"], s["breached"], s["no_date"],
           fmt_age(s["oldest_minutes"]) if s["total"] > s["no_date"] else "-"] for s in by_user]),
        ("Over 1 Hour", first + columns, [request_row(r) for r in requests if r["status"] in OVERDUE]),
        ("All Requests", first + columns, [request_row(r) for r in requests]),
    ]
    thin = Side(style="thin", color="D0D7D4")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    label_status = {label: status for status, label in LABELS.items()}
    for n, (title, headers, rows) in enumerate(sheets):
        ws = wb.active if n == 0 else wb.create_sheet()
        ws.title = title
        _append(ws, headers)
        for row in rows:
            _append(ws, row)
        ws.sheet_view.rightToLeft = False
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for c in ws[1]:
            c.fill = PatternFill("solid", fgColor=NBE_GREEN[1:])
            c.font = Font(bold=True, color="FFFFFF")
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        names = [c.value for c in ws[1]]
        st = names.index("SLA_STATUS") if "SLA_STATUS" in names else None
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.border = border
            if st is not None and row[st].value in label_status:
                fg, bg = COLORS[label_status[row[st].value]]
                row[st].fill = PatternFill("solid", fgColor=bg[1:])
                row[st].font = Font(bold=True, color=fg[1:])
            for name, status in (("BREACHED", schemas.SLA_BREACHED), ("REMINDER", schemas.SLA_REMINDER)):
                if name in names and (row[names.index(name)].value or 0) > 0:
                    fg, bg = COLORS[status]
                    row[names.index(name)].fill = PatternFill("solid", fgColor=bg[1:])
                    row[names.index(name)].font = Font(bold=True, color=fg[1:])
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(width + 3, 12), 45)
    return wb


def save_workbook(wb, path):
    """Write next to the target and rename, so a reader never sees a half-written file."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        wb.save(tmp)
        os.replace(tmp, path)
    finally:
        with suppress(OSError):
            tmp.unlink()
    return path


def prune_report_copies(folder, keep=KEEP_REPORT_COPIES):
    try:
        names = sorted(p.name for p in Path(folder).iterdir() if COPY_NAME.match(p.name))
    except OSError:
        return
    for name in names[:-keep] if keep else names:
        with suppress(OSError):
            (Path(folder) / name).unlink()


# --------------------------------------------------------------------------- e-mail
def _badge(status):
    fg, bg = COLORS.get(status, ("#333333", "#EEEEEE"))
    return (f'<span style="background:{bg};color:{fg};font-weight:bold;padding:3px 8px;'
            f'border-radius:10px;font-size:9pt;white-space:nowrap">{esc(LABELS.get(status, status))}</span>')


def _kpi(label, value, color):
    return (f'<td width="25%" style="padding:6px">'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
            f'background:#FFFFFF;border:1px solid #DDE3E0;border-top:4px solid {color}">'
            f'<tr><td style="padding:12px 14px">'
            f'<div style="font-size:9pt;color:#5F6B66;text-transform:uppercase">{esc(label)}</div>'
            f'<div style="font-size:22pt;font-weight:bold;color:{color}">{esc(value)}</div>'
            f'</td></tr></table></td>')


def html_body(requests, by_user, cfg, now, has_logo=False, max_rows=MAX_EMAIL_ROWS):
    """The reminder e-mail. Every scraped value goes through ``esc``."""
    warn, breach = int(cfg["warning_minutes"]), int(cfg["breach_minutes"])
    counts = _counts(requests)
    n_total, n_breach, n_warn, n_ok = len(requests), counts["breached"], counts["reminder"], counts["within"]
    font = f"font-family:{FONT_STACK}"
    th = (f'style="background:{NBE_GREEN};color:#FFFFFF;padding:8px 10px;text-align:center;'
          f'font-size:9.5pt;border:1px solid {NBE_GREEN}"')
    td = 'style="padding:7px 10px;border:1px solid #E1E6E4;font-size:9.5pt;vertical-align:top"'
    tdc = 'style="padding:7px 10px;border:1px solid #E1E6E4;font-size:9.5pt;text-align:center;white-space:nowrap"'
    logo = (f'<img src="cid:{LOGO_CID}" height="56" alt="NBE" style="display:block">' if has_logo else
            '<div style="color:#FFFFFF;font-size:20pt;font-weight:bold">NBE</div>')

    def num(v, status):
        if not v:
            return '<span style="color:#9AA5A0">0</span>'
        fg, bg = COLORS[status]
        return (f'<span style="background:{bg};color:{fg};font-weight:bold;padding:2px 10px;'
                f'border-radius:10px">{int(v)}</span>')

    # --- per security user
    user_rows = []
    for i, s in enumerate(by_user):
        zebra = "#FFFFFF" if i % 2 == 0 else "#F7FAF9"
        oldest = fmt_age(s["oldest_minutes"]) if s["total"] > s["no_date"] else "-"
        user_rows.append(
            f'<tr style="background:{zebra}"><td {td} dir="auto"><b>{esc(s["user"])}</b></td>'
            f'<td {tdc}><b>{int(s["total"])}</b></td><td {tdc}>{num(s["within"], schemas.SLA_WITHIN)}</td>'
            f'<td {tdc}>{num(s["reminder"], schemas.SLA_REMINDER)}</td>'
            f'<td {tdc}>{num(s["breached"], schemas.SLA_BREACHED)}</td>'
            f'<td {tdc}>{esc(oldest)}</td></tr>')

    # --- overdue requests grouped by security user
    over = [r for r in requests if r["status"] in OVERDUE]
    detail_names = list(over[0]["fields"]) if over else []
    ncols = len(detail_names) + 3
    det_rows, shown = [], 0
    for s in by_user:
        grp = [r for r in over if r["security_user"] == s["user"]]
        if not grp or shown >= max_rows:
            continue
        nb = sum(1 for r in grp if r["status"] == schemas.SLA_BREACHED)
        grp_style = (f'background:{NBE_GREEN_LIGHT};padding:8px 10px;border:1px solid #D5E2DC;'
                     f'font-size:10pt;color:{NBE_GREEN}')
        breached_txt = f' &middot; <b style="color:#B71C1C">{nb} breached</b>' if nb else ""
        det_rows.append(
            f'<tr><td colspan="3" style="{grp_style}" dir="ltr">{len(grp)} request(s) over '
            f'{fmt_hours(warn)}{breached_txt}</td>'
            f'<td colspan="{ncols - 3}" style="{grp_style};text-align:right" dir="rtl">'
            f'<b>{esc(s["user"])}</b></td></tr>')
        for r in grp[:max_rows - shown]:
            shown += 1
            cells = "".join(f'<td {td} dir="auto">{esc(r["fields"].get(n))}</td>' for n in detail_names)
            sent = r["_sent"].strftime("%d/%m %H:%M") if r["_sent"] else "-"
            det_rows.append(
                f'<tr>{cells}<td {tdc}>{esc(sent)}</td>'
                f'<td {tdc}><b>{esc(fmt_age(r["age_minutes"]))}</b></td><td {tdc}>{_badge(r["status"])}</td></tr>')
    more = (f'<p style="font-size:9pt;color:#5F6B66">Showing {shown} of {len(over)} requests. '
            f'Full list in the attached Excel.</p>' if len(over) > shown else "")
    head = "".join(f"<th {th}>{esc(n)}</th>" for n in detail_names)
    details = (
        f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
        f'<tr>{head}<th {th}>Sent to Security</th><th {th}>Age</th><th {th}>Status</th></tr>'
        f'{"".join(det_rows)}</table>{more}'
        if det_rows else
        f'<p style="color:#2E7D32;font-weight:bold">&#10004; No request is older than {fmt_hours(warn)}.</p>')

    def section(title):
        return (f'<tr><td style="padding:22px 24px 8px 24px"><div style="font-size:13pt;font-weight:bold;'
                f'color:{NBE_GREEN};border-left:4px solid {NBE_GOLD};padding-left:10px">{title}</div></td></tr>')

    return f"""<html><body style="margin:0;padding:0;background:#EEF2F0;{font}">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#EEF2F0"><tr><td align="center" style="padding:20px 10px">
<table width="900" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#FFFFFF;{font};color:#1F2A26">
<tr><td style="background:{NBE_GREEN};padding:16px 24px">
  <table width="100%" cellpadding="0" cellspacing="0"><tr>
    <td valign="middle">{logo}</td>
    <td valign="middle" align="right" style="color:#FFFFFF">
      <div style="font-size:16pt;font-weight:bold">Security Requests SLA Reminder</div>
      <div style="font-size:9.5pt;color:#DCE9E4">Re-Assign queue &middot; {now:%d %b %Y, %H:%M}</div>
    </td></tr></table>
</td></tr>
<tr><td style="background:{NBE_GOLD};height:4px;font-size:1px">&nbsp;</td></tr>
<tr><td style="padding:20px 24px 4px 24px;font-size:10.5pt">
  Dear Team,<br><br>
  Security requests must be completed within <b>{fmt_hours(breach)}</b> of being sent to security.
  Below are the open requests per security user; requests over <b>{fmt_hours(warn)}</b> need action now.
</td></tr>
<tr><td style="padding:10px 18px"><table width="100%" cellpadding="0" cellspacing="0"><tr>
  {_kpi("Open Requests", n_total, NBE_GREEN)}
  {_kpi(f"Within {fmt_hours(warn)}", n_ok, "#2E7D32")}
  {_kpi(f"{fmt_hours(warn)} - {fmt_hours(breach)}", n_warn, "#EF6C00")}
  {_kpi(f"Over {fmt_hours(breach)} (Breached)", n_breach, "#C62828")}
</tr></table></td></tr>
{section("Requests per Security User")}
<tr><td style="padding:0 24px">
  <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
  <tr><th {th}>Security User</th><th {th}>Total</th>
      <th {th}>Under {fmt_hours(warn)}</th><th {th}>{fmt_hours(warn)} - {fmt_hours(breach)}</th>
      <th {th}>Over {fmt_hours(breach)}</th><th {th}>Oldest</th></tr>
  {"".join(user_rows)}
  </table>
</td></tr>
{section(f"Requests Over {fmt_hours(warn)} ({len(over)})")}
<tr><td style="padding:0 24px">{details}</td></tr>
<tr><td style="padding:22px 24px;font-size:10.5pt">Please action the requests above. Full details are attached.<br><br>Regards,</td></tr>
<tr><td style="background:{NBE_GREEN_LIGHT};padding:10px 24px;font-size:8.5pt;color:#5F6B66;border-top:1px solid #D5E2DC">
  National Bank of Egypt &middot; Automated SLA monitor &middot; Internal use only
</td></tr>
</table></td></tr></table>
</body></html>"""


def mail_subject(mail_cfg, cfg, counts, now):
    subject = (f"{mail_cfg.get('subject') or 'Security Requests SLA'} - {counts['breached']} breached, "
               f"{counts['reminder']} over {fmt_hours(int(cfg['warning_minutes']))} - {now:%d-%b %H:%M}")
    return re.sub(r"[\r\n]+", " ", subject)


def mail_cc(mail_cfg, owner_emails, requests):
    """The configured cc list, plus (cc_security_users) the owners of overdue requests that have an address."""
    to = set(mail_cfg.get("to") or [])
    cc = [a for a in (mail_cfg.get("cc") or []) if a]
    if mail_cfg.get("cc_security_users"):
        emails = {norm(k): v for k, v in (owner_emails or {}).items()}
        for r in requests:
            addr = emails.get(norm(r["security_user"])) if r["status"] in OVERDUE else None
            if addr and addr not in cc and addr not in to:
                cc.append(addr)
    return cc


# --------------------------------------------------------------------------- the job
def _config(ctx):
    return deep_merge(DEFAULTS["sla"], ctx.settings or {})


def _fail(ctx, message, kind=None):
    ctx.log(f"SLA scan failed: {message}")
    return JobResult(ok=False, error=message, error_kind=kind)


def _mailer(ctx):
    return ctx.mailer or OutlookMailer()


def _write_reports(ctx, wb, now, send):
    """SLA_latest.xlsx always, a timestamped copy when a mail goes out. Returns the copy's path (or None).
    A report that cannot be written (share down, file open in Excel) is logged, never fatal."""
    copy = None
    try:
        folder = ctx.store.report_dir("sla")
        save_workbook(wb, folder / LATEST_REPORT)
    except OSError as exc:
        ctx.log(f"Excel {LATEST_REPORT} could not be saved ({type(exc).__name__}: {exc})")
    if send:
        try:
            copy = save_workbook(wb, ctx.store.report_dir("sla") / f"{REPORT_PREFIX}{now:%Y%m%d_%H%M}.xlsx")
            prune_report_copies(ctx.store.report_dir("sla"))
        except OSError as exc:
            ctx.log(f"Excel copy could not be saved, the e-mail goes without it ({type(exc).__name__}: {exc})")
    return copy


def _send_mail(ctx, cfg, requests, by_user, now, attachment):
    mail_cfg = cfg["mail"]
    cc = mail_cc(mail_cfg, cfg.get("owner_emails"), requests)
    has_logo = LOGO_FILE.is_file()
    body = html_body(requests, by_user, cfg, now, has_logo=has_logo)
    _mailer(ctx).send(
        to=list(mail_cfg["to"]), cc=cc, subject=mail_subject(mail_cfg, cfg, _counts(requests), now), html_body=body,
        attachments=[str(attachment)] if attachment else [], inline_images={LOGO_CID: str(LOGO_FILE)} if has_logo else None,
        display_only=bool(mail_cfg.get("display_only")))
    return cc


def _notify(ctx, cfg, requests, by_user, columns, now):
    """Decide, write the Excel files, send, update the alert state. Returns (sent: bool, note: str)."""
    state = load_state(ctx)
    levels = alert_levels(requests)
    mail_cfg = cfg.get("mail") or {}
    force = bool((ctx.options or {}).get("send_mail"))
    decision = decide_alert(state, levels, now, cfg.get("repeat_minutes"), force)

    if not ctx.can_email():
        ctx.log("E-mail skipped (this run does not send e-mail)")
        can_mail = False
    elif not mail_cfg.get("send", True):
        ctx.log("E-mail skipped (switched off in the SLA settings)")
        can_mail = False
    elif not mail_cfg.get("to"):
        ctx.log("E-mail skipped (no recipients in the SLA settings)")
        can_mail = False
    else:
        can_mail = True
        if not decision["send"]:
            ctx.log("No new request crossed a threshold - no e-mail")
    send = can_mail and decision["send"]

    sent, note = False, ""
    attachment = None if ctx.dry_run else _write_reports(ctx, build_workbook(columns, requests, by_user), now, send)
    if send:
        try:
            cc = _send_mail(ctx, cfg, requests, by_user, now, attachment)
            sent = True
            ctx.log(f"E-mail sent to {', '.join(mail_cfg['to'])}" + (f" (cc {', '.join(cc)})" if cc else ""))
        except Exception as exc:
            note = f"e-mail FAILED: {clean_error(exc)}"
            ctx.log(f"SLA e-mail failed, it will be tried again on the next scan: {clean_error(exc)}")
    if not ctx.dry_run:
        try:
            ctx.store.write_state(STATE_NAME, next_state(state, levels, sent, now))
        except Exception as exc:
            ctx.log(f"SLA alert state could not be saved ({type(exc).__name__}: {exc})")
    return sent, note


def run(ctx, scraper=None):
    """One scan: read the portal, analyse, e-mail on escalation, return the snapshot data."""
    cfg = _config(ctx)
    username = str(cfg.get("username") or "").strip()
    if not username:
        return _fail(ctx, "No SLA account is set. Put the portal user in collector_config.json "
                          "(\"sla\": {\"username\": ...}) or in System > Settings.", "auth")
    try:
        password = ctx.password("sla", username)
        if not password:
            raise CredentialError("empty password")
    except CredentialError:
        return _fail(ctx, f"No password for the SLA portal account '{username}'. Fill sla.password in "
                          "collector_config.json (next to collector.py), or set the NBE_SSO_PASSWORD environment "
                          "variable, or run:  python collector.py --set-password sla", "auth")
    secrets = [password]

    try:
        headers, rows = scraper(ctx) if scraper else scrape(ctx, username, password)
        now = ctx.now()
        columns, table = build_table(headers, rows)
        requests = analyze(columns, table, cfg, now)
    except Exception as exc:
        if ctx.debug:
            import traceback
            ctx.log(scrub(traceback.format_exc(), secrets))
        return _fail(ctx, clean_error(exc, secrets), "auth" if isinstance(exc, LoginError) else None)

    by_user = summarize(requests)
    data = snapshot_data(requests, by_user, cfg)
    totals = data["totals"]
    ctx.log(f"{totals['open']} open requests - {totals['reminder']} reminder, {totals['breached']} over "
            f"{fmt_hours(int(cfg['breach_minutes']))}")

    try:
        sent, note = _notify(ctx, cfg, requests, by_user, columns, now)
    except Exception as exc:                  # the scan itself is good: keep its snapshot
        sent, note = False, f"report / e-mail step failed: {clean_error(exc, secrets)}"
        ctx.log(f"SLA {note}")
    ts = now.isoformat(timespec="seconds")
    summary = f"{totals['open']} open, {totals['reminder']} reminder, {totals['breached']} breached"
    return JobResult(
        ok=True, snapshot_data=data, summary=summary + (f" - {note}" if note else ""), emails_sent=int(sent),
        history_point={"ts": ts, "open": totals["open"], "within": totals["within"],
                       "reminder": totals["reminder"], "breached": totals["breached"]},
        history_extra={"first_breach": {r["key"]: ts for r in requests if r["status"] == schemas.SLA_BREACHED}})
