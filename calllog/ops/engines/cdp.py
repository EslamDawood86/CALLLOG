"""Drive the Chrome / Edge that is already installed on the PC over the DevTools protocol (CDP) - no Playwright, no
browser download, no Node.js.

The ITSM job has driven a real site this way from the start (its own copy of this transport); the SLA job now uses the
same technique instead of Playwright, because Playwright needs its own downloaded Chromium and spawns a Node.js helper -
both of which a locked-down bank PC often blocks. Everything here is the Python standard library only.

Surface used by an engine::

    b = CdpBrowser(cfg, log=..., headless=True)   # launches the browser, attaches to its first page
    b.navigate(url)                               # go to a URL and wait for load
    b.eval("location.href")                       # run JavaScript, get the value back (returnByValue)
    b.attach("request_reassign")                  # follow the window/tab whose URL contains this text (new window or same tab)
    b.screenshot(path) / b.redacted_html()        # for the debug dumps
    b.close()                                     # always ends the browser it started (never raises)

``cfg`` keys: ``browser_executable`` (full path, wins), ``cdp_port`` (default 0 = a free port), ``profile_dir`` (a LOCAL
folder; never the share - Chrome keeps lock files there). Tests replace the whole object, so the engines take a factory.
"""
import base64
import glob
import json
import os
import signal
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Where Edge / Chrome normally live on a Windows PC (what the bank already has - used directly, no download).
WINDOWS_BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
)


class CdpError(RuntimeError):
    """The browser could not be started or driven (shown to people)."""


def _clock():
    return time.time()


def _sleep(seconds):
    time.sleep(seconds)


# ------------------------------------------------------------------------------- finding a browser
def playwright_chromiums(environ=None):
    """Chromium builds that Playwright downloaded (newest first). Lets a PC that already has a Playwright Chromium, and
    the test sandbox, run without a system Chrome / Edge. Never downloads anything."""
    environ = os.environ if environ is None else environ
    root = environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
    found = []
    for pattern in ("chromium-*/chrome-linux/chrome", "chromium_headless_shell-*/chrome-linux/headless_shell",
                    "chromium-*/chrome-win/chrome.exe", "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"):
        found += sorted(glob.glob(os.path.join(root, pattern)), reverse=True)
    return found


def browser_candidates(configured="", environ=None):
    environ = os.environ if environ is None else environ
    out = [configured] if configured else []
    out += [os.path.expandvars(p) for p in WINDOWS_BROWSERS]
    if environ.get("CHROME_PATH"):
        out.append(environ["CHROME_PATH"])
    out += playwright_chromiums(environ)
    return [p for p in out if p]


def find_browser(configured="", environ=None):
    """Full path of a Chrome / Edge / Chromium that exists, or None. The configured path wins, then the standard Windows
    locations, then CHROME_PATH, then a Playwright Chromium already on the PC. No download ever happens."""
    seen = set()
    for path in browser_candidates(configured, environ):
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if os.path.isfile(path):
                return path
        except OSError:
            pass
    return None


def profile_dir(leaf):
    """A LOCAL Chrome profile folder for a job (never the share: Chrome keeps lock files / a database there)."""
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    return os.path.join(base or tempfile.gettempdir(), "NBE-Ops", leaf)


def kill_profile_browsers(profile):
    """Close any Chrome / Edge started with this job's profile (never the user's normal browser). Windows only."""
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


def build_args(exe, port, profile, headless):
    args = [
        exe, f"--remote-debugging-port={port}", "--remote-allow-origins=*",
        f"--user-data-dir={profile}", "--ignore-certificate-errors",
        "--no-first-run", "--no-default-browser-check", "--disable-default-apps", "--log-level=3", "about:blank",
    ]
    if headless:
        args[1:1] = ["--headless=new", "--window-size=1600,1000", "--disable-gpu", "--mute-audio"]
    if os.name != "nt":
        args[1:1] = ["--disable-dev-shm-usage"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:        # Chrome refuses its sandbox as root
            args[1:1] = ["--no-sandbox"]
    return args


def popen_kwargs(headless):
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW if headless else 0
    else:
        kwargs["start_new_session"] = True          # own process group, so close() can end Chrome's helpers too
    return kwargs


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ------------------------------------------------------------------------------- WebSocket (stdlib only)
class WS:
    def __init__(self, url, timeout=60):
        u = urlparse(url)
        self.s = socket.create_connection((u.hostname, u.port), timeout=timeout)
        self.s.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        self.s.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
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


# ------------------------------------------------------------------------------- the browser
class CdpBrowser:
    """A real Chrome / Edge driven over the DevTools protocol. ``navigate`` / ``eval`` / ``attach`` / ``screenshot`` /
    ``redacted_html`` / ``close`` is the surface the SLA engine uses (tests replace the whole object)."""

    def __init__(self, cfg, log=None, headless=True, profile_leaf="sla_profile"):
        self.log = log or (lambda message: None)
        self.proc = None
        self.ws = None
        self.id = 0
        self.port = int(cfg.get("cdp_port") or 0) or free_port()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # never a proxy for localhost
        exe = find_browser(str(cfg.get("browser_executable") or "").strip())
        if not exe:
            raise CdpError("Chrome or Edge was not found on this PC. Install Microsoft Edge (or Google Chrome), or set "
                           "sla.browser_executable in collector_config.json to the full path of msedge.exe / chrome.exe.")
        self.profile = str(cfg.get("profile_dir") or profile_dir(profile_leaf))
        os.makedirs(self.profile, exist_ok=True)
        kill_profile_browsers(self.profile)
        if os.name == "nt":
            _sleep(1)
        self.log(f"browser: {os.path.basename(exe)} (headless={headless}) on port {self.port}")
        try:
            self.proc = subprocess.Popen(build_args(exe, self.port, self.profile, headless), **popen_kwargs(headless))
            self.ws = WS(self._wait_target())
            self.call("Page.enable")
            try:   # drop "Headless" from the user agent so a picky server does not refuse us
                ua = self.eval("navigator.userAgent").replace("HeadlessChrome", "Chrome").replace("Headless", "")
                self.call("Network.setUserAgentOverride", userAgent=ua)
            except Exception:
                pass
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------- targets
    def _targets(self):
        with self._opener.open(f"http://127.0.0.1:{self.port}/json", timeout=5) as r:
            return json.load(r)

    def _wait_target(self):
        end = _clock() + 30
        last = ""
        while _clock() < end:
            rc = self.proc.poll()
            if rc not in (None, 0):        # exit code 0 can mean Chrome handed over to a running instance
                raise CdpError(f"The browser exited at start-up (code {rc}).")
            try:
                for t in self._targets():
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
            except Exception as exc:
                last = str(exc)
            _sleep(0.3)
        raise CdpError("The browser did not answer on its debugging port - a bank policy may block it. " + last)

    def attach(self, fragment, timeout=20):
        """Make the page whose URL contains ``fragment`` the current one (the package may open a new window, or load in
        the same tab). Returns the matched URL, or raises if none appears in time."""
        want = (fragment or "").lower()
        end = _clock() + timeout
        while _clock() < end:
            try:
                if want in (self.current_url() or "").lower():      # the current page already got there
                    return self.current_url()
            except Exception:
                pass
            try:
                pages = [t for t in self._targets() if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            except Exception:
                pages = []
            for t in pages:
                if want in (t.get("url") or "").lower():
                    if self.ws is not None:
                        self.ws.close()
                    self.ws = WS(t["webSocketDebuggerUrl"])
                    self.call("Page.enable")
                    return t.get("url")
            _sleep(0.3)
        raise CdpError(f"The expected page did not open (no window whose address contains '{fragment}').")

    def page_urls(self):
        try:
            return [t.get("url", "") for t in self._targets() if t.get("type") == "page"]
        except Exception:
            return []

    # ------------------------------------------------------------- CDP calls
    def call(self, method, **params):
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.id:
                if "error" in m:
                    raise CdpError(m["error"].get("message", "cdp error"))
                return m.get("result", {})

    def eval(self, expression, timeout_ms=30000):
        r = self.call("Runtime.evaluate", expression=f"(()=>{{{expression}}})()" if "return" in expression else expression,
                      returnByValue=True, awaitPromise=True, timeout=timeout_ms)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise CdpError((d.get("exception") or {}).get("description") or d.get("text", "js error"))
        return r["result"].get("value")

    def current_url(self):
        return self.eval("location.href")

    # ------------------------------------------------------------- navigation
    def navigate(self, url, timeout=30):
        self.call("Page.navigate", url=url)
        self.wait_ready(timeout)

    def wait_ready(self, timeout=30):
        end = _clock() + timeout
        while _clock() < end:
            try:
                if self.eval("document.readyState") in ("interactive", "complete"):
                    return
            except Exception:
                pass
            _sleep(0.2)

    def wait_url(self, predicate, timeout=30):
        """Wait until ``predicate(current_url)`` is true. Returns the URL, or raises on timeout."""
        end = _clock() + timeout
        last = ""
        while _clock() < end:
            try:
                last = self.current_url() or ""
                if predicate(last):
                    return last
            except Exception:
                pass
            _sleep(0.3)
        return None

    # ------------------------------------------------------------- debug dumps
    def screenshot(self, path):
        data = self.call("Page.captureScreenshot", format="png").get("data")
        if data:
            with open(path, "wb") as f:
                f.write(base64.b64decode(data))

    # ------------------------------------------------------------- shutdown
    def close(self):
        """Never raises; always ends the browser it started (and its helper processes)."""
        if self.ws is not None:
            self.ws.close()
            self.ws = None
        if self.proc is not None:
            self._stop(self.proc)
            self.proc = None
        kill_profile_browsers(self.profile) if self.profile else None

    @staticmethod
    def _stop(proc):
        group = os.name != "nt" and hasattr(os, "killpg")
        for step in ("term", "kill"):
            try:
                if group:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM if step == "term" else signal.SIGKILL)
                else:
                    proc.terminate() if step == "term" else proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
                return
            except Exception:
                pass
