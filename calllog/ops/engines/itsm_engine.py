"""The "itsm" job: reads the CA SDM (ITSM) scoreboard for every configured role and reports the open requests.

Port of the old itsm_status.py script. The browser technique that proved itself on the real site is kept as it was:
Chrome / Edge is started with a remote-debugging port and driven over the DevTools protocol by a tiny stdlib WebSocket
client; the page is read with small JavaScript helpers that search every same-origin frame; the scoreboard only reacts
to a real (trusted) mouse click, so the click goes through ``Input.dispatchMouseEvent`` first and ``el.click()`` second.

Entry point (docs/HUB-DESIGN.md section 12)::

    run(ctx, browser_factory=None) -> JobResult

``browser_factory(ctx)`` returns an object with ``call(method, **params)``, ``eval(expression)`` and ``close()`` (the
same surface as ``Browser``); tests pass a scripted fake or a factory for a headless Chromium. Everything configurable
comes from ``ctx.settings`` (the itsm section); the password only from ``ctx.password("itsm", username)``.

What a run produces (contract 5.2): per role, per category, the scoreboard count and the rows of the list behind it; the
totals; and ``changes`` = requests that appeared / disappeared since the previous good scan (kept in
``ctx.store.read_state("itsm")``, so the first scan of a day still compares with the last known state). A role that
fails is recorded as ``{"role", "error"}`` and the other roles still run; when the browser cannot start, the login is
refused, or no role could be read at all, the result is ``ok=False`` and no snapshot data is returned, so the last good
snapshot stays on screen.

E-mail: the old script always sent a mail. Now it is sent only when the settings allow it and either "Run now" asked for
it (``ctx.options["send_mail"]``) or a ``mail.send_times`` slot of today has passed and was not sent yet. A failed scan
sends the failure mail once per slot, and a later good scan still sends the report for that slot.

The Chrome profile lives on the local disk (``%LOCALAPPDATA%\\NBE-Ops\\itsm_profile``), never on the shared folder.
Windows-only parts (the powershell clean-up, ``CREATE_NO_WINDOW``) are guarded by ``os.name``.
"""
import base64
import copy
import glob
import json
import os
import re
import signal
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from ...storage import StorageError
from ..settings import DEFAULTS, deep_merge
from .common import CredentialError, JobResult, esc

CREATE_NO_WINDOW = 0x08000000
PROFILE_LEAF = "itsm_profile"

# Where the old script looked for a browser (Windows); settings["browser_executable"] is tried first.
LEGACY_BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# Page vocabulary of CA SDM: regexes matched against the visible text (case-insensitive, in every frame).
SEL = {
    "user": "input[name='USERNAME'], input[name='username'], input[type='text']",
    "pass": "input[type='password']",
    "login_btn_text": r"^\s*log\s*in\s*$",
    "set_role_text": r"^\s*set\s*role\s*$",
    "scoreboard_text": r"scoreboard\s*as\s*of",
}

# Waits, in seconds. Module level so tests can shorten them.
LOGIN_TIMEOUT = 60
PAGE_TIMEOUT = 60
FRESH_TIMEOUT = 20
SCOREBOARD_TIMEOUT = 45
CATEGORY_TIMEOUT = 4
LIST_TIMEOUT = 12
ROLE_SETTLE = 2          # let the page finish loading after Set Role
LIST_SETTLE = 1          # let a freshly appeared list finish rendering
POLL = 0.5
LIST_POLL = 0.7
SUMMARY_MAX = 500
MAX_CHANGES = 200
MAIL_SENT_KEEP_DAYS = 7


def _sleep(seconds):
    time.sleep(seconds)


def _clock():
    return time.time()


class ScanError(RuntimeError):
    """A role (or the whole scan) could not be read; the message is shown to people."""


class LoginError(ScanError):
    """The portal did not accept the account (wrong / expired password): the runner must not retry."""


# ---------------------------------------------------------------------------------------------------------------------
# Minimal WebSocket client (stdlib only)
# ---------------------------------------------------------------------------------------------------------------------
class WS:
    def __init__(self, url, timeout=60):
        u = urlparse(url)
        self.s = socket.create_connection((u.hostname, u.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((
            f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake failed")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n")[0]:
            raise ConnectionError("websocket handshake refused: " + buf.split(b"\r\n")[0].decode())
        self.extra = buf.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self.extra) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.extra += chunk
        out, self.extra = self.extra[:n], self.extra[n:]
        return out

    def send(self, text):
        data = text.encode("utf-8")
        n = len(data)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        msg = b""
        while True:
            b1, b2 = self._read(2)
            fin, op, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if op == 8:
                raise ConnectionError("websocket closed by browser")
            if op in (9, 10):          # ping / pong
                continue
            msg += payload
            if fin:
                return msg.decode("utf-8")

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------------------------------
# Browser (Chrome DevTools Protocol)
# ---------------------------------------------------------------------------------------------------------------------
def profile_dir():
    """Chrome profile of the job. LOCAL on purpose: Chrome keeps lock files and a database there that a network share breaks."""
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    return os.path.join(base or tempfile.gettempdir(), "NBE-Ops", PROFILE_LEAF)


def kill_profile_browsers(profile):
    """Close any Chrome / Edge that uses the job's profile (never touches the user's normal browser). Windows only."""
    if os.name != "nt":
        return
    leaf = os.path.basename(str(profile).rstrip("\\/")).replace("'", "''")
    ps = ("Get-CimInstance Win32_Process | Where-Object { ($_.Name -in 'chrome.exe','msedge.exe') "
          "-and $_.CommandLine -like '*" + leaf + "*' } | ForEach-Object "
          "{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                       creationflags=CREATE_NO_WINDOW, timeout=30, capture_output=True)
    except Exception:
        pass


def playwright_chromiums(environ=None):
    """Chromium builds that Playwright downloaded (newest first); only used on Linux / macOS, mainly by the tests."""
    environ = os.environ if environ is None else environ
    root = environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
    found = []
    for pattern in ("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"):
        found += sorted(glob.glob(os.path.join(root, pattern)), reverse=True)
    return found


def browser_candidates(configured="", environ=None):
    environ = os.environ if environ is None else environ
    out = [configured] if configured else []
    out += [os.path.expandvars(p) for p in LEGACY_BROWSERS]
    if os.name != "nt":
        if environ.get("CHROME_PATH"):
            out.append(environ["CHROME_PATH"])
        out += playwright_chromiums(environ)
    return out


def find_browser(configured="", environ=None):
    return next((p for p in browser_candidates(configured, environ) if os.path.exists(p)), None)


def build_args(exe, port, profile, headless):
    args = [
        exe, f"--remote-debugging-port={port}", "--remote-allow-origins=*",
        f"--user-data-dir={profile}", "--ignore-certificate-errors",
        "--no-first-run", "--no-default-browser-check", "--disable-default-apps", "--log-level=3", "about:blank",
    ]
    if headless:
        args[1:1] = ["--headless=new", "--window-size=1920,1080", "--disable-gpu", "--mute-audio"]
    if os.name != "nt":
        args[1:1] = ["--disable-dev-shm-usage"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:        # Chrome refuses to start as root with its sandbox
            args[1:1] = ["--no-sandbox"]
    return args


def popen_kwargs(headless):
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW if headless else 0
    else:
        kwargs["start_new_session"] = True          # own process group, so close() can end Chrome's helper processes too
    return kwargs


class Browser:
    """Chrome / Edge driven over the DevTools protocol. ``call`` / ``eval`` / ``close`` is the surface tests replace."""

    def __init__(self, cfg, log=None, headless=True):
        log = log or (lambda message: None)
        self.proc = None
        self.ws = None
        self.id = 0
        port = int(cfg.get("cdp_port") or 9222)
        self.port = port
        configured = str(cfg.get("browser_executable") or "").strip()
        exe = find_browser(configured)
        if not exe:
            raise ScanError("Chrome or Edge was not found. Set itsm.browser_executable (collector_config.json) to its full path.")
        self.profile = profile_dir()
        os.makedirs(self.profile, exist_ok=True)
        kill_profile_browsers(self.profile)
        if os.name == "nt":
            _sleep(1)
        log(f"launching {os.path.basename(exe)} (headless={headless})")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # no proxy for localhost
        try:
            self.proc = subprocess.Popen(build_args(exe, port, self.profile, headless), **popen_kwargs(headless))
            self.ws = WS(self._wait_target())
            try:   # drop the word Headless from the user agent so the server does not refuse us
                ua = self.eval("navigator.userAgent").replace("HeadlessChrome", "Chrome")
                self.call("Emulation.setUserAgentOverride", userAgent=ua)
            except Exception:
                pass
        except BaseException:
            self.close()
            raise

    def _wait_target(self):
        end = _clock() + 30
        while _clock() < end:
            rc = self.proc.poll()
            if rc not in (None, 0):          # exit code 0 can be Chrome handing over to an instance that is already running
                raise ScanError(f"The browser exited at start-up (code {rc}).")
            try:
                with self._opener.open(f"http://127.0.0.1:{self.port}/json", timeout=3) as r:
                    for t in json.load(r):
                        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                            return t["webSocketDebuggerUrl"]
            except Exception:
                pass
            _sleep(0.5)
        raise ScanError("The browser did not answer on its remote-debugging port (a bank policy may block it).")

    def call(self, method, **params):
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.id:
                if "error" in m:
                    raise RuntimeError(m["error"].get("message", "cdp error"))
                return m.get("result", {})

    def eval(self, expression):
        r = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError((d.get("exception") or {}).get("description") or d.get("text", "js error"))
        return r["result"].get("value")

    def close(self):
        """Never raises; always ends the browser it started (and its helper processes)."""
        if self.ws is not None:
            self.ws.close()
        proc = self.proc
        if proc is not None:
            self._stop(proc)
        kill_profile_browsers(self.profile)

    @staticmethod
    def _stop(proc):
        group = os.name != "nt" and hasattr(os, "killpg")
        try:
            if group:
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        try:
            if group:
                os.killpg(proc.pid, signal.SIGKILL)          # whatever is left of the process group
            else:
                proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def open_browser(ctx):
    """Default ``browser_factory``: a real Chrome / Edge."""
    cfg = deep_merge(DEFAULTS["itsm"], ctx.settings or {})
    return Browser(cfg, ctx.log, headless=bool(cfg.get("headless", True)) and not ctx.headed)


# ---------------------------------------------------------------------------------------------------------------------
# JavaScript run inside the page. Shared helpers search every same-origin frame.
# ---------------------------------------------------------------------------------------------------------------------
HELPERS = r"""
const ALL = () => { const o = []; const walk = w => { o.push(w); try { for (let i = 0; i < w.frames.length; i++) walk(w.frames[i]); } catch (e) {} }; walk(window.top); return o; };
const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
const Q = css => { for (const w of ALL()) { try { for (const el of w.document.querySelectorAll(css)) if (vis(el)) return el; } catch (e) {} } return null; };
const SKIP = /^(SCRIPT|STYLE|HEAD|TITLE|META|LINK|OPTION|SELECT|HTML|BODY)$/;
const T = src => { let best = null; const re = new RegExp(src, 'i'); for (const w of ALL()) { try { for (const el of w.document.querySelectorAll('*')) { if (SKIP.test(el.tagName)) continue; const t = ((el.tagName === 'INPUT' ? el.value : el.textContent) || '').replace(/\u00a0/g, ' ').trim(); if (t && re.test(t) && vis(el) && (!best || t.length < best.t.length)) best = { el, t }; } } catch (e) {} } return best; };
"""

# The rows of the list that is on screen: a request number (4-8 digits) and the cell after it (the summary).
EXTRACT = r"""
const NUM = /^\d{4,8}$/;
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const out = [], seen = new Set();
for (const w of ALL()) { try {
  const d = w.document;
  const label = re => { for (const el of d.querySelectorAll('th,td,div,span,a,b,label')) { if (re.test(clean(el.textContent))) { const r = el.getBoundingClientRect(); if (r.width && r.height) return r; } } return null; };
  const hCO = label(/^Change Order\s*#$/i), hSum = label(/^Summary$/i);
  for (const el of d.querySelectorAll('a,span,td,div,font,b,u,i')) {
    if (el.children.length || !NUM.test(clean(el.textContent))) continue;
    const r0 = el.getBoundingClientRect();
    if (!r0.width || !r0.height) continue;
    if (hCO && Math.abs(r0.left - hCO.left) > 120) continue;      // only the Change Order # column
    const num = clean(el.textContent);
    if (seen.has(num)) continue;
    let txt = '', ttl = '';
    const cell = el.closest('td,th') || el;
    for (let s = cell.nextElementSibling; s; s = s.nextElementSibling) {   // the cell after the request number is the Summary
      const t = clean(s.innerText);
      if (t) { txt = t; const te = s.querySelector('[title]'); ttl = clean((te && te.getAttribute('title')) || s.getAttribute('title')); break; }
    }
    if (!txt && hSum) {                                              // fallback: whatever is under the Summary header on screen
      try {
        el.scrollIntoView({ block: 'center' });
        const r = el.getBoundingClientRect();
        for (const dx of [6, 20, 40]) {
          const q = d.elementFromPoint(hSum.left + dx, r.top + r.height / 2);
          if (q && !/^(TR|TABLE|TBODY|BODY|HTML)$/.test(q.tagName)) {
            const t = clean(q.innerText);
            if (t && t !== num) { txt = t; const te = q.closest('[title]'); ttl = clean(te && te.getAttribute('title')); break; }
          }
        }
      } catch (e) {}
    }
    seen.add(num);
    out.push({ 'Change Order #': num, 'Summary': ttl.length > txt.length ? ttl : txt });
  }
} catch (e) {} }
return out;
"""

ROLE_SELECT_JS = r"""
for (const w of ALL()) { try { for (const s of w.document.querySelectorAll('select')) {
  for (const o of s.options) { if (want.includes(o.text.trim().toLowerCase())) return true; } } } catch (e) {} }
return false;
"""

# Marks every window; a window that is loaded again after Set Role has no mark (= the scoreboard is fresh).
MARK_JS = r"ALL().forEach(w => { try { w.__itsm_m = 1; } catch (e) {} }); return true;"

FRESH_JS = r"""
for (const w of ALL()) { try { if (/scoreboard\s*as\s*of/i.test(w.document.body.innerText)) return !w.__itsm_m; } catch (e) {} }
return false;
"""

# Page structure for troubleshooting. Typed values are never read: only the label of buttons, never an input's text.
DEBUG_JS = r"""
const out = [];
for (const w of ALL()) { try {
  const d = w.document;
  let sb = null;
  for (const el of d.querySelectorAll('*')) { const t = (el.textContent || ''); if (/scoreboard/i.test(t) && t.length < 200 && (!sb || t.length < sb.t.length)) sb = { t, h: el.outerHTML.slice(0, 700) }; }
  let rowHtml = null;
  for (const el of d.querySelectorAll('a,span,td,div,u,b')) { if (!el.children.length && /^\d{4,8}$/.test((el.textContent || '').trim())) { const tr = el.closest('tr') || el.parentElement; rowHtml = tr.outerHTML.slice(0, 1500); break; } }
  out.push({
    url: w.location.href,
    title: d.title,
    row_html: rowHtml,
    scoreboard_html: sb ? sb.h : null,
    text: (d.body ? d.body.innerText : '').slice(0, 3000),
    elements: [...d.querySelectorAll('input,button,select,a')].slice(0, 80).map(e => ({
      tag: e.tagName, id: e.id, name: e.name, type: e.type,
      text: (e.tagName === 'INPUT' ? (/^(button|submit|reset)$/i.test(e.type) ? e.value : '') : (e.textContent || '')).trim().slice(0, 40),
      visible: vis(e)
    }))
  });
} catch (e) { out.push({ url: '(cross-origin frame)' }); } }
return out;
"""

MOUSE_POS_JS = r"""
const m = T(PAT); if (!m) return null;
const el = m.el;
try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
const r = el.getBoundingClientRect();
let x = r.left + r.width / 2, y = r.top + r.height / 2;
let w = el.ownerDocument.defaultView;
while (w && w !== w.top) { const fe = w.frameElement; if (!fe) break; const fr = fe.getBoundingClientRect(); x += fr.left + fe.clientLeft; y += fr.top + fe.clientTop; w = w.parent; }
return { x: x, y: y };
"""


# ---------------------------------------------------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------------------------------------------------
def scrub(value, secrets):
    """Text with every secret (the password) replaced; applied to everything that can reach a log, a file or a mail."""
    text = str(value)
    for secret in secrets or ():
        if secret:
            text = text.replace(secret, "***")
    return text


def _scrub_deep(value, secrets):
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, list):
        return [_scrub_deep(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_deep(v, secrets) for k, v in value.items()}
    return value


def parse_count(label):
    """'Implementation Requests (3)' -> 3; None when the line has no number in brackets."""
    m = re.search(r"\((\d+)\)", label or "")
    return int(m.group(1)) if m else None


def rows_from_extract(raw):
    """What EXTRACT returned -> [{"number", "summary"}] (contract 5.2). Junk entries and repeated numbers are dropped."""
    rows, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        number = str(item.get("Change Order #") or "").strip()
        if not number or number in seen:
            continue
        seen.add(number)
        rows.append({"number": number, "summary": " ".join(str(item.get("Summary") or "").split())[:SUMMARY_MAX]})
    return rows


def extract_key(rows):
    return tuple(r.get("Change Order #") for r in rows)


def compute_totals(roles, categories):
    totals = {c: 0 for c in categories}
    for role in roles:
        if role.get("error"):
            continue
        for c in categories:
            totals[c] += ((role.get("categories") or {}).get(c) or {}).get("count") or 0
    return totals


def per_role_counts(roles):
    out = {}
    for role in roles:
        if role.get("error"):
            continue
        out[role["role"]] = {c: d["count"] for c, d in (role.get("categories") or {}).items() if d.get("count") is not None}
    return out


def complete_lists(roles):
    """{(role, category): rows} for the lists that were read completely (all rows of the scoreboard count were seen)."""
    out = {}
    for role in roles:
        if role.get("error"):
            continue
        for cat, d in (role.get("categories") or {}).items():
            count = d.get("count")
            if count is None or d.get("warning") or len(d.get("rows") or []) < count:
                continue
            out[(role["role"], cat)] = list(d.get("rows") or [])
    return out


def compute_changes(prev_lists, current):
    """New / closed requests between the stored lists and the lists of this scan.

    Only (role, category) pairs that were read completely both times are compared; a failed role, an unreadable or paged
    list would otherwise look like "everything closed" or "everything new". No stored list at all (first scan ever) = no changes.
    """
    prev = {(l.get("role"), l.get("category")): l.get("rows") or [] for l in prev_lists or []}
    new, closed = [], []
    for (role, cat), rows in current.items():
        if (role, cat) not in prev:
            continue
        old_numbers = {r["number"] for r in prev[(role, cat)]}
        now_numbers = {r["number"] for r in rows}
        new += [{"role": role, "category": cat, "number": r["number"], "summary": r["summary"]}
                for r in rows if r["number"] not in old_numbers]
        closed += [{"role": role, "category": cat, "number": r["number"], "summary": r["summary"]}
                   for r in prev[(role, cat)] if r["number"] not in now_numbers]
    return {"new": new[:MAX_CHANGES], "closed": closed[:MAX_CHANGES]}


def merge_lists(prev_lists, current, roles, categories):
    """The lists to remember: this scan's complete lists, else what we knew before; only for configured roles / categories."""
    merged = {(l.get("role"), l.get("category")): l for l in prev_lists or []}
    for (role, cat), rows in current.items():
        merged[(role, cat)] = {"role": role, "category": cat, "rows": rows}
    return [l for (role, cat), l in merged.items() if role in roles and cat in categories]


def failure_data(roles, categories, error):
    return {"categories": list(categories),
            "roles": [{"role": r, "error": error, "categories": {}} for r in roles],
            "totals": {c: 0 for c in categories}, "changes": {"new": [], "closed": []}}


def has_issue(data):
    for role in data.get("roles") or []:
        if role.get("error") or any(d.get("warning") for d in (role.get("categories") or {}).values()):
            return True
    return False


# ---------------------------------------------------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------------------------------------------------
def wait_for(fn, timeout=20, what="timeout"):
    end, last = _clock() + timeout, None
    while _clock() < end:
        try:
            r = fn()
            if r:
                return r
        except ConnectionError:
            raise                    # the browser is gone: waiting longer cannot help
        except Exception as exc:
            last = exc
        _sleep(POLL)
    raise TimeoutError(what + (f" (last error: {last})" if last else ""))


class Scanner:
    """The flow of the old script (login, Set Role, scoreboard, lists) on top of one browser."""

    def __init__(self, browser, ctx, cfg, username, password):
        self.b = browser
        self.ctx = ctx
        self.url = str(cfg.get("url") or "")
        self.username = username
        self.password = password
        self.roles = [str(r) for r in cfg.get("roles") or []]
        self.categories = dict(cfg.get("categories") or {})
        self.debug_files = []

    # ------------------------------------------------------------------ plumbing
    def log(self, message):
        self.ctx.log(scrub(message, [self.password]))

    def scrub(self, value):
        return scrub(value, [self.password])

    def js(self, code, retries=3):
        last = None
        for _ in range(retries):
            try:
                return self.b.eval("(() => {" + HELPERS + code + "})()")
            except RuntimeError as exc:      # e.g. the page is reloading
                last = exc
                _sleep(1)
        raise last

    def has_css(self, css):
        return self.js(f"return !!Q({json.dumps(css)});")

    def has_text(self, pat):
        return self.js(f"return !!T({json.dumps(pat)});")

    def match_text(self, pat):
        return self.js(f"const m = T({json.dumps(pat)}); return m ? m.t : null;")

    def click_text(self, pat):
        return self.js(f"const m = T({json.dumps(pat)}); if (!m) return false; m.el.click(); return true;")

    def fill(self, css, value):
        return self.js(
            f"const el = Q({json.dumps(css)}); if (!el) return false; el.focus(); el.value = {json.dumps(value)};"
            "['input','change'].forEach(e => el.dispatchEvent(new Event(e, {bubbles: true}))); return true;")

    def submit_form(self, css):
        return self.js(f"const el = Q({json.dumps(css)}); if (el && el.form) {{ el.form.submit(); return true; }} return false;")

    def has_role_select(self):
        return self.js("const want = " + json.dumps([r.lower() for r in self.roles]) + ";" + ROLE_SELECT_JS)

    def select_option(self, label):
        return self.js(
            f"const want = {json.dumps(label)}.toLowerCase();"
            "for (const w of ALL()) { try { for (const s of w.document.querySelectorAll('select')) {"
            " for (let i = 0; i < s.options.length; i++) { if (s.options[i].text.trim().toLowerCase() === want) {"
            "  s.selectedIndex = i; s.dispatchEvent(new Event('change', {bubbles: true})); return true; } } } } catch (e) {} }"
            "return false;")

    def mouse_click(self, pat):
        """A real (trusted) mouse click: the CA SDM scoreboard ignores a plain el.click()."""
        pos = self.js("const PAT = " + json.dumps(pat) + ";" + MOUSE_POS_JS)
        if not pos:
            return False
        x, y = pos["x"], pos["y"]
        self.b.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        self.b.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
        self.b.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)
        return True

    # ---------------------------------------------------------------------- lists
    def read_list(self, prev_key, timeout=20):
        """Rows of the list on screen once it differs from ``prev_key`` (the previous category's list)."""
        end = _clock() + timeout
        while _clock() < end:
            try:
                rows = self.js(EXTRACT) or []
            except Exception:
                rows = []
            if rows and extract_key(rows) != prev_key:       # the new list has appeared
                _sleep(LIST_SETTLE)
                try:
                    rows = self.js(EXTRACT) or rows
                except Exception:
                    pass
                return rows
            _sleep(LIST_POLL)
        return []        # the old script returned the last rows here, i.e. the previous category's list under this name

    def open_category(self, pattern, prev_key):
        """Open a scoreboard category and read its list. Real mouse click first, JavaScript click second."""
        for name, fn in (("mouse", self.mouse_click), ("js", self.click_text)):
            try:
                if not fn(pattern):
                    continue
            except Exception as exc:
                self.log(f"  {name} click failed: {exc}")
                continue
            rows = self.read_list(prev_key, timeout=LIST_TIMEOUT)
            if rows:
                return rows
            self.log(f"  list did not load after {name} click")
        return []

    def dump_debug(self, tag):
        """Page structure (never a password or typed text) in <data_dir>/ops/debug/itsm/ - only when the job runs with debug."""
        if not self.ctx.debug:
            return None
        tag = re.sub(r"\W+", "_", str(tag))[:80]
        try:
            frames = _scrub_deep(self.js(DEBUG_JS), [self.password])
            doc = {"tag": tag, "ts": self.ctx.now().isoformat(timespec="seconds"), "frames": frames}
            folder = Path(self.ctx.data_dir or self.ctx.store.root.parent) / "ops" / "debug" / "itsm"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"itsm_debug_{tag}.json"
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            self.debug_files.append(str(path))
            return str(path)
        except Exception as exc:
            self.log(f"page dump failed: {exc}")
            return None

    # ----------------------------------------------------------------------- flow
    def login(self):
        self.log("opening ITSM ...")
        self.b.call("Page.navigate", url=self.url)
        state = wait_for(
            lambda: "in" if self.has_role_select() else ("login" if self.has_css(SEL["pass"]) else None),
            PAGE_TIMEOUT, "Neither the login page nor the roles page appeared (the page did not load)")
        self.log(f"page state: {state}")
        if state != "login":
            return
        self.fill(SEL["user"], self.username)
        self.fill(SEL["pass"], self.password)
        if not self.click_text(SEL["login_btn_text"]):
            self.log("Log In button not found, submitting form")
            self.submit_form(SEL["pass"])
        try:
            wait_for(self.has_role_select, LOGIN_TIMEOUT, "The roles page did not appear after login")
        except TimeoutError as exc:
            try:
                still_login = bool(self.has_css(SEL["pass"]))
            except Exception:
                still_login = False
            if still_login:      # the login form is shown again: the account was not accepted
                raise LoginError("Login failed - the ITSM user name or password was not accepted. "
                                 "Fix the account in collector_config.json.") from exc
            raise ScanError("Login did not succeed (check the user name / password, or the page may have changed).") from exc
        self.log("logged in")

    def scoreboard_ready(self):
        return self.has_text(SEL["scoreboard_text"]) or any(self.match_text(p) for p in self.categories.values())

    def collect_role(self, role):
        """{category: {count, rows, warning, note}} for one role."""
        self.log(f"role: {role}")
        if not self.select_option(role):
            raise ScanError(f"Role '{role}' not found in the dropdown")
        self.js(MARK_JS)
        if not self.click_text(SEL["set_role_text"]):
            raise ScanError("Set Role button not found")
        try:
            wait_for(lambda: self.js(FRESH_JS), FRESH_TIMEOUT, "scoreboard refresh")      # the page reloads with the new role
        except TimeoutError:
            self.log("warning: scoreboard refresh not detected, continuing")
        _sleep(ROLE_SETTLE)
        wait_for(self.scoreboard_ready, SCOREBOARD_TIMEOUT, "The scoreboard did not appear after Set Role")

        out, prev_key = {}, None
        for cat, pattern in self.categories.items():
            label = None
            try:
                label = wait_for(lambda: self.match_text(pattern), CATEGORY_TIMEOUT)
            except TimeoutError:
                pass
            entry = {"count": None, "rows": [], "warning": None, "note": None}
            out[cat] = entry
            if not label:
                self.log(f"  {cat}: not in scoreboard")           # this role's scoreboard does not have the category
                continue
            count = parse_count(label)
            if count is None:
                entry["warning"] = f"The scoreboard line '{str(label)[:60]}' has no number in brackets."
                continue
            entry["count"] = count
            raw = []
            if count:
                raw = self.open_category(pattern, prev_key)
                if raw:
                    prev_key = extract_key(raw)
            entry["rows"] = rows_from_extract(raw)
            self.log(f"  {cat}: count={count}, rows read={len(entry['rows'])}")
            if count and not entry["rows"]:
                entry["warning"] = "The scoreboard shows requests, but the list could not be read."
                self.dump_debug(f"{role}_{cat}")
            elif count and len(entry["rows"]) < count:
                entry["note"] = f"Showing {len(entry['rows'])} of {count} requests (list is paged)."
        return out

    def scan_role(self, role):
        """One role as a snapshot entry; a failure is recorded, never raised, so the other roles still run."""
        try:
            return {"role": role, "error": None, "categories": self.collect_role(role)}
        except Exception as exc:
            message = self.scrub(exc) or exc.__class__.__name__
            self.log(f"ERROR in role '{role}': {message}")
            self.dump_debug(role)
            return {"role": role, "error": message, "categories": {}}


def scan(ctx, cfg, username, password, browser_factory=None):
    """(roles, fatal_error, error_kind, debug_files). ``fatal_error`` = browser / login failed, no role was tried."""
    browser, scanner, roles = None, None, []
    fatal, kind = None, None
    try:
        browser = (browser_factory or open_browser)(ctx)
        scanner = Scanner(browser, ctx, cfg, username, password)
        scanner.login()
        roles = [scanner.scan_role(role) for role in scanner.roles]
    except Exception as exc:                 # browser could not start, or the login failed
        fatal = scrub(exc, [password]) or exc.__class__.__name__
        kind = "auth" if isinstance(exc, LoginError) else None
        ctx.log(f"FATAL: {fatal}")
        if scanner is not None:
            scanner.dump_debug("fatal")
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:
                ctx.log(f"browser close failed: {scrub(exc, [password])}")
    return roles, fatal, kind, (scanner.debug_files if scanner else [])


# ---------------------------------------------------------------------------------------------------------------------
# E-mail (HTML ported from the old script; every text from the portal is escaped)
# ---------------------------------------------------------------------------------------------------------------------
GREEN, GOLD, INK, MUTED, LINE = "#0B5D3B", "#E3A21A", "#1F2D3D", "#6B7780", "#E1E7EA"
CAT_COLORS = {                      # (number colour, background) when the count is above 0
    "Implementation Requests": ("#9A6700", "#FFF4D6"),
    "Rollback Requests": ("#B42318", "#FDE8E6"),
}
FONT = "font-family:'Segoe UI',Calibri,Arial,sans-serif;"


def _count_cell(d, cat):
    if d is None or d.get("count") is None:
        return (f'<td align="center" style="{FONT}padding:10px;font-size:15px;color:#B8C0C7;'
                f'border-bottom:1px solid {LINE};">&ndash;</td>')
    n = d["count"]
    fg, bg = ("#1E7B4F", "#E6F4EC") if n == 0 else CAT_COLORS.get(cat, ("#9A6700", "#FFF4D6"))
    return (f'<td align="center" bgcolor="{bg}" style="{FONT}padding:10px;font-size:18px;font-weight:bold;'
            f'color:{fg};border-bottom:1px solid {LINE};">{esc(n)}</td>')


def build_subject(data, now=None):
    now = now or datetime.now()
    cats = list(data.get("categories") or [])
    tot = compute_totals(data.get("roles") or [], cats)
    parts = " | ".join(f"{c.replace(' Requests', '')}: {n}" for c, n in tot.items())
    has_err = any(r.get("error") for r in data.get("roles") or [])
    return f"ITSM Status - {now:%Y-%m-%d %H:%M} | {parts}" + (" | CHECK REQUIRED" if has_err else "")


def build_html(data, now=None, source=""):
    now = now or datetime.now()
    cats = list(data.get("categories") or [])
    roles = data.get("roles") or []
    tot = compute_totals(roles, cats)

    stamp = now.strftime("%A, %d %B %Y &nbsp;&middot;&nbsp; %H:%M")
    h = []
    h.append(
        '<html><body style="margin:0;padding:0;background:#F3F5F7;">'
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#F3F5F7"><tr><td align="center" style="padding:24px 10px;">'
        '<table width="760" cellpadding="0" cellspacing="0" border="0" bgcolor="#FFFFFF" style="border:1px solid #DCE2E7;">'
        f'<tr><td bgcolor="{GREEN}" style="{FONT}padding:24px 30px;">'
        '<div style="font-size:24px;font-weight:bold;color:#FFFFFF;">NBE ITSM &ndash; Implementation Status</div>'
        f'<div style="font-size:13px;color:#F3D68A;padding-top:6px;">Automated daily report &nbsp;|&nbsp; {stamp}</div>'
        '</td></tr>'
        f'<tr><td bgcolor="{GOLD}" height="4" style="font-size:0;line-height:0;">&nbsp;</td></tr>'
        f'<tr><td style="padding:26px 30px 6px 30px;{FONT}">'
        f'<div style="font-size:15px;font-weight:bold;color:{INK};padding-bottom:10px;">Overview</div>'
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;border:1px solid {LINE};">'
        f'<tr bgcolor="{GREEN}"><td style="{FONT}padding:10px 12px;font-size:12px;font-weight:bold;color:#FFFFFF;">ROLE</td>'
    )
    for c in cats:
        h.append(f'<td align="center" style="{FONT}padding:10px 12px;font-size:12px;font-weight:bold;color:#FFFFFF;">{esc(c.upper())}</td>')
    h.append("</tr>")
    for r in roles:
        h.append(f'<tr><td style="{FONT}padding:10px 12px;font-size:13px;color:{INK};border-bottom:1px solid {LINE};">{esc(r.get("role"))}</td>')
        if r.get("error"):
            h.append(f'<td colspan="{max(1, len(cats))}" align="center" bgcolor="#FDE8E6" style="{FONT}padding:10px;font-size:13px;color:#B42318;'
                     f'border-bottom:1px solid {LINE};">Could not be scanned &ndash; see details below</td>')
        else:
            for c in cats:
                h.append(_count_cell((r.get("categories") or {}).get(c), c))
        h.append("</tr>")
    h.append(f'<tr bgcolor="#F3F5F7"><td style="{FONT}padding:10px 12px;font-size:13px;font-weight:bold;color:{INK};">Total</td>')
    for c in cats:
        h.append(f'<td align="center" style="{FONT}padding:10px 12px;font-size:15px;font-weight:bold;color:{INK};">{esc(tot[c])}</td>')
    h.append("</tr></table></td></tr>")

    for r in roles:
        h.append(f'<tr><td style="padding:26px 30px 0 30px;{FONT}">'
                 '<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
                 f'<td width="5" bgcolor="{GOLD}" style="font-size:0;line-height:0;">&nbsp;</td>'
                 f'<td style="padding:6px 12px;font-size:17px;font-weight:bold;color:{GREEN};">{esc(r.get("role"))}</td></tr></table></td></tr>')
        if r.get("error"):
            h.append(f'<tr><td style="padding:12px 30px 0 30px;{FONT}"><table width="100%" cellpadding="0" cellspacing="0" border="0">'
                     '<tr><td bgcolor="#FDE8E6" style="padding:12px 14px;font-size:13px;color:#B42318;border-left:4px solid #B42318;">'
                     f'<b>Error:</b> {esc(r["error"])}</td></tr></table></td></tr>')
            continue
        for c in cats:
            d = (r.get("categories") or {}).get(c)
            if d is None or d.get("count") is None:      # the category is not on this role's scoreboard
                continue
            fg = CAT_COLORS.get(c, ("#9A6700", ""))[0] if d["count"] else "#1E7B4F"
            h.append(f'<tr><td style="padding:16px 30px 6px 30px;{FONT}font-size:14px;font-weight:bold;color:{INK};">'
                     f'{esc(c)} <span style="color:{fg};">({esc(d["count"])})</span></td></tr>')
            h.append(f'<tr><td style="padding:0 30px;{FONT}"><table width="100%" cellpadding="0" cellspacing="0" border="0" '
                     f'style="border-collapse:collapse;border:1px solid {LINE};">')
            if d.get("rows"):
                h.append(f'<tr bgcolor="{GREEN}">'
                         f'<td width="44" align="center" style="{FONT}padding:9px 8px;font-size:12px;font-weight:bold;color:#FFFFFF;">#</td>'
                         f'<td width="120" style="{FONT}padding:9px 12px;font-size:12px;font-weight:bold;color:#FFFFFF;">REQUEST NO.</td>'
                         f'<td style="{FONT}padding:9px 12px;font-size:12px;font-weight:bold;color:#FFFFFF;">SUMMARY</td></tr>')
                for i, row in enumerate(d["rows"], 1):
                    bg = "#FFFFFF" if i % 2 else "#F4F8F6"
                    h.append(f'<tr bgcolor="{bg}">'
                             f'<td align="center" style="{FONT}padding:10px 8px;font-size:13px;color:{MUTED};border-bottom:1px solid {LINE};">{i}</td>'
                             f'<td style="{FONT}padding:10px 12px;font-size:13px;font-weight:bold;color:{GREEN};border-bottom:1px solid {LINE};">{esc(row.get("number"))}</td>'
                             f'<td style="{FONT}padding:10px 12px;font-size:13px;color:{INK};border-bottom:1px solid {LINE};">{esc(row.get("summary"))}</td></tr>')
            else:
                h.append(f'<tr><td align="center" style="{FONT}padding:14px;font-size:13px;color:{MUTED};font-style:italic;">No requests</td></tr>')
            h.append("</table></td></tr>")
            for kind, color, bg in (("warning", "#9A6700", "#FFF4D6"), ("note", MUTED, "#F3F5F7")):
                if d.get(kind):
                    h.append(f'<tr><td style="padding:6px 30px 0 30px;{FONT}"><table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
                             f'<td bgcolor="{bg}" style="padding:8px 12px;font-size:12px;color:{color};">{esc(d[kind])}</td></tr></table></td></tr>')

    where = f" &middot; Source: {esc(source)}" if source else ""
    h.append(
        f'<tr><td style="padding:30px 30px 24px 30px;{FONT}">'
        '<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="border-top:1px solid {LINE};padding-top:14px;font-size:11px;color:#98A2AB;">'
        f'Generated automatically by the NBE Operations Hub (ITSM collector){where} &middot; This is an automated message.'
        '</td></tr></table></td></tr></table></td></tr></table></body></html>'
    )
    return "\n".join(h)


# ---------------------------------------------------------------------------------------------------------------------
# When to send the mail
# ---------------------------------------------------------------------------------------------------------------------
def plan_mail(send_times, sent, now, ok, manual):
    """(kind, slot keys) - kind is "report", "failure" or None.

    A slot is "HH:MM" of today; its key is "YYYY-MM-DD HH:MM". ``sent`` maps a key to {"report": ts, "failure": ts}.
    A good scan sends the report for the slots that have passed and have no report yet (all of them in one mail).
    A failed scan sends the failure mail once per slot that has neither; the report for that slot still follows later.
    ``manual`` ("Run now" with send_mail) sends whatever the outcome is.
    """
    day, hm = now.date().isoformat(), f"{now:%H:%M}"
    due = [f"{day} {t}" for t in sorted(send_times or []) if t <= hm]
    if ok:
        slots = [k for k in due if not (sent.get(k) or {}).get("report")]
        return ("report", slots) if slots or manual else (None, [])
    slots = [k for k in due if not (sent.get(k) or {}).get("report") and not (sent.get(k) or {}).get("failure")]
    return ("failure", slots) if slots or manual else (None, [])


def _read_state(ctx):
    try:
        return copy.deepcopy(ctx.store.read_state("itsm") or {})
    except StorageError as exc:
        ctx.log(f"state file unreadable, starting fresh: {exc}")
        return {}


def _write_state(ctx, state):
    try:
        ctx.store.write_state("itsm", state)
        return True
    except (StorageError, OSError) as exc:
        ctx.log(f"cannot save state: {exc}")
        return False


def _prune_sent(sent, now):
    cutoff = (now - timedelta(days=MAIL_SENT_KEEP_DAYS)).date().isoformat()
    return {k: v for k, v in sent.items() if str(k)[:10] >= cutoff}


def send_mail_if_due(ctx, cfg, data, ok, attachments=()):
    """Send the report / failure mail when the settings and the clock say so. Returns how many mails were sent (0 or 1)."""
    mail = cfg.get("mail") or {}
    if not (ctx.can_email() and mail.get("send")):
        return 0
    now = ctx.now()
    state = _read_state(ctx)
    sent = state.get("mail_sent") if isinstance(state.get("mail_sent"), dict) else {}
    kind, slots = plan_mail(mail.get("send_times"), sent, now, ok, bool((ctx.options or {}).get("send_mail")))
    if not kind:
        return 0
    if ctx.mailer is None:
        ctx.log("e-mail not sent: no Outlook mailer is available in this run")
        return 0
    to, cc = list(mail.get("to") or []), list(mail.get("cc") or [])
    try:
        if not to:
            to = [ctx.mailer.my_address()]          # like the old script: no recipient = your own address
        source = urlparse(str(cfg.get("url") or "")).hostname or ""
        files = [p for p in attachments if p and os.path.exists(p)] if has_issue(data) else []
        ctx.mailer.send(to, cc=cc, subject=build_subject(data, now), html_body=build_html(data, now, source),
                        attachments=files, display_only=bool(mail.get("display_only")))
    except Exception as exc:
        ctx.log(f"e-mail failed: {exc}")
        return 0
    ctx.log(f"{kind} mail sent | To: {'; '.join(to)}" + (f" | CC: {'; '.join(cc)}" if cc else ""))
    for key in slots:
        sent.setdefault(key, {})[kind] = now.isoformat(timespec="seconds")
    state["mail_sent"] = _prune_sent(sent, now)
    _write_state(ctx, state)
    return 1


def save_report(ctx, html_text):
    """itsm_latest.html in the reports folder (what the System page lists); written atomically."""
    try:
        folder = ctx.store.report_dir("itsm")
        tmp = folder / ".itsm_latest.tmp"          # a leading dot keeps it out of the reports list
        tmp.write_text(html_text, encoding="utf-8")
        os.replace(tmp, folder / "itsm_latest.html")
    except OSError as exc:
        ctx.log(f"cannot save the HTML report: {exc}")


# ---------------------------------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------------------------------
def _summary(data, changes_note=""):
    parts = [f"{n} {c.replace(' Requests', '').lower()}" for c, n in data["totals"].items()]
    text = ", ".join(parts) if parts else "no categories"
    failed = sum(1 for r in data["roles"] if r.get("error"))
    if failed:
        text += f", {failed} role{'s' if failed != 1 else ''} failed"
    return text + changes_note


def _fail(ctx, cfg, roles, categories, error, kind=None, data=None, attachments=()):
    """A failed scan: nothing to write, but the failure mail may be due."""
    data = data or failure_data(roles, categories, error)
    sent = send_mail_if_due(ctx, cfg, data, False, attachments)
    return JobResult(ok=False, error=error, error_kind=kind, emails_sent=sent, summary="ITSM scan failed")


def _run(ctx, cfg, browser_factory, secrets):
    started = _clock()
    username = str(cfg.get("username") or "").strip()
    roles = [str(r).strip() for r in cfg.get("roles") or [] if str(r).strip()]
    categories = dict(cfg.get("categories") or {})
    cfg = {**cfg, "roles": roles, "categories": categories}

    if not username:
        return _fail(ctx, cfg, roles, categories, "The ITSM user name is not set (itsm.username in collector_config.json).", "auth")
    if not roles or not categories:
        return _fail(ctx, cfg, roles, categories, "No ITSM roles or categories are configured (System > Settings > ITSM).")
    try:
        password = ctx.password("itsm", username)
    except CredentialError as exc:
        return _fail(ctx, cfg, roles, categories, str(exc), "auth")
    secrets.append(password)

    results, fatal, kind, debug_files = scan(ctx, cfg, username, password, browser_factory)
    if fatal:
        results = [{"role": r, "error": fatal, "categories": {}} for r in roles]
    elif results and all(r.get("error") for r in results):
        fatal = f"No role could be read ({len(results)} failed). First error: {results[0]['error']}"
    data = {"categories": list(categories), "roles": results,
            "totals": compute_totals(results, categories), "changes": {"new": [], "closed": []}}
    if fatal:
        return _fail(ctx, cfg, roles, categories, fatal, kind, data, debug_files)

    state = _read_state(ctx)
    current = complete_lists(results)
    data["changes"] = compute_changes(state.get("lists"), current)
    ts = ctx.now().isoformat(timespec="seconds")
    if not ctx.dry_run:
        state.update(ts=ts, lists=merge_lists(state.get("lists"), current, set(roles), set(categories)))
        _write_state(ctx, state)
        save_report(ctx, build_html(data, ctx.now(), urlparse(str(cfg.get("url") or "")).hostname or ""))
    sent = send_mail_if_due(ctx, cfg, data, True, debug_files)

    n_new, n_closed = len(data["changes"]["new"]), len(data["changes"]["closed"])
    note = f" ({n_new} new, {n_closed} closed)" if n_new or n_closed else ""
    return JobResult(
        ok=True, snapshot_data=data, snapshot_date=ctx.now().date().isoformat(), summary=_summary(data, note),
        emails_sent=sent,
        history_point={"ts": ts, "totals": dict(data["totals"]), "per_role": per_role_counts(results)})


def run(ctx, browser_factory=None):
    """The "itsm" job. Never raises: every problem comes back as a JobResult with ok=False and a readable error."""
    secrets = []
    try:
        cfg = deep_merge(DEFAULTS["itsm"], ctx.settings or {})
        return _run(ctx, cfg, browser_factory, secrets)
    except Exception as exc:
        message = f"Unexpected error: {scrub(exc, secrets) or exc.__class__.__name__}"
        ctx.log(message)
        return JobResult(ok=False, error=message, summary="ITSM scan failed")
