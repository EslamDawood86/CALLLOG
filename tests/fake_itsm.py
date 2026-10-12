"""A fake CA SDM (ITSM) site for the ITSM engine tests: plain http on 127.0.0.1, served from a thread.

It imitates what the engine depends on, not the real product:

* a login form (``USERNAME`` + password input + a "Log In" button); a wrong password shows the form again,
* after login a frameset: a header frame with the roles ``<select>`` and a "Set Role" button, a scoreboard frame and a list frame,
* the scoreboard says "Scoreboard as of ..." and shows one line per category ("Implementation Requests (3)"); what it shows
  depends on the role chosen with Set Role, and it is rendered again (a new document) after Set Role,
* the category lines react only to a TRUSTED click (``event.isTrusted``), like the real scoreboard, so a plain ``el.click()``
  does nothing and only the engine's real mouse click opens the list,
* the list frame has a "Change Order #" column and a "Summary" column (plus a far-away "Ticket" column holding a number the
  engine must ignore); a summary can be cut short in the cell with the full text in its ``title``.

``FakeITSM.roles`` can be changed between scans: ``{role: {category: {"count": n, "rows": [row, ...]}}}`` where a row is
``(number, summary)`` or ``(number, summary, full_title)``. A category missing from a role is not on its scoreboard; a count larger
than the rows imitates a paged list.
"""
import copy
import html
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

IMPLEMENTATION = "Implementation Requests"
ROLLBACK = "Rollback Requests"

DEFAULT_ROLES = {
    "IMP User provisioning": {
        IMPLEMENTATION: {"count": 3, "rows": [
            ("111111", "Create user for branch 12"),
            ("222222", "طلب تفعيل مستخدم جديد"),
            ("333333", "Cut short in the cell...", "Cut short in the cell, but the full text is in the title"),
        ]},
        ROLLBACK: {"count": 1, "rows": [("444444", "<script>alert(1)</script> & rollback")]},
    },
    "IMP UAT I-Flex SMS": {
        IMPLEMENTATION: {"count": 2, "rows": [("555555", "UAT SMS template"), ("666666", "UAT SMS sender id")]},
    },
    "IMP Empty": {
        IMPLEMENTATION: {"count": 0, "rows": []},
        ROLLBACK: {"count": 0, "rows": []},
    },
}

LOGIN_PAGE = """<html><head><title>CA Service Desk Manager</title></head><body>
<h3>Welcome to the service desk</h3>{error}
<form method="post" action="/login">
<table><tr><td>User Name</td><td><input type="text" name="USERNAME"></td></tr>
<tr><td>Password</td><td><input type="password" name="PIN"></td></tr></table>
<input type="submit" value="Log In">
</form></body></html>"""

FRAMESET = """<html><head><title>Service Desk</title></head>
<frameset rows="70,*"><frame name="hdr" src="/hdr">
<frameset cols="320,*"><frame name="sb" src="/scoreboard"><frame name="list" src="/list"></frameset></frameset></html>"""

HEADER_PAGE = """<html><body style="margin:8px;font:14px sans-serif">
<form onsubmit="return false">Role: <select id="role" name="role">{options}</select>
<input type="button" value="Set Role" onclick="setRole()"></form>
<script>
function setRole() {{
  var role = document.getElementById('role').value;
  fetch('/api/role', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{role: role}})}})
    .then(function () {{ parent.frames['list'].location.href = '/list'; parent.frames['sb'].location.reload(); }});
}}
</script></body></html>"""

SCOREBOARD_PAGE = """<html><body style="margin:8px;font:14px sans-serif">
<div>Scoreboard as of 10/12/2026 9:00 AM</div><div>Role: {role}</div>
<table cellpadding="6">{lines}</table>
<script>
function openCat(ev, name) {{
  if (!ev.isTrusted) {{ fetch('/untrusted', {{method: 'POST'}}); return false; }}
  parent.frames['list'].location.href = '/list?cat=' + encodeURIComponent(name);
  return false;
}}
</script></body></html>"""

LIST_PAGE = """<html><head><style>td.s {{ width: 560px; }} td, th {{ padding: 6px; text-align: left; }}</style></head>
<body style="margin:8px;font:14px sans-serif">{body}</body></html>"""


class FakeITSM:
    def __init__(self, users=None, roles=None):
        self.users = dict(users or {"1001": "secret-pw"})
        self.roles = copy.deepcopy(roles if roles is not None else DEFAULT_ROLES)
        self.sessions = {}
        self.login_attempts = []        # (user name, accepted)
        self.requests = []              # (method, path) of everything the browser asked for
        self.untrusted_clicks = 0       # how often the scoreboard saw a click that was not a real mouse click
        self._server = None
        self._thread = None

    # ----------------------------------------------------------------- control
    def start(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._server = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}/CAisd/nbeitsm5"

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------------- pages
    def scoreboard(self, role):
        lines = []
        for cat, d in (self.roles.get(role) or {}).items():
            lines.append(f'<tr><td><a href="#" onclick="return openCat(event, {html.escape(json.dumps(cat))})">'
                         f'{html.escape(cat)} ({d["count"]})</a></td></tr>')
        return SCOREBOARD_PAGE.format(role=html.escape(role or ""), lines="".join(lines))

    def listing(self, role, cat):
        d = (self.roles.get(role) or {}).get(cat)
        if not d:
            return LIST_PAGE.format(body="<p>Select a category on the scoreboard.</p>")
        rows = []
        for row in d["rows"]:
            number, summary = row[0], row[1]
            title = f' title="{html.escape(row[2])}"' if len(row) > 2 else ""
            rows.append(f'<tr><td><a href="#">{html.escape(number)}</a></td>'
                        f'<td class="s"{title}>{html.escape(summary)}</td><td>77777</td></tr>')
        table = ('<table border="1" cellspacing="0"><tr><th>Change Order #</th><th>Summary</th><th>Ticket</th></tr>'
                 + "".join(rows) + "</table>")
        return LIST_PAGE.format(body=f"<div>{html.escape(cat)}</div>{table}")


def _handler(site):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body, status=200, headers=()):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _session(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "sid" and value in site.sessions:
                    return site.sessions[value]
            return None

        def _body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")

        def do_GET(self):
            url = urlparse(self.path)
            site.requests.append(("GET", url.path))
            session = self._session()
            if url.path.startswith("/CAisd"):
                return self._send(FRAMESET if session else LOGIN_PAGE.format(error=""))
            if session is None:
                return self._send("not signed in", 403)
            if url.path == "/hdr":
                options = "".join(f"<option>{html.escape(r)}</option>" for r in site.roles)
                return self._send(HEADER_PAGE.format(options=options))
            if url.path == "/scoreboard":
                return self._send(site.scoreboard(session["role"]))
            if url.path == "/list":
                cat = (parse_qs(url.query).get("cat") or [""])[0]
                return self._send(site.listing(session["role"], cat))
            self._send("not found", 404)

        def do_POST(self):
            url = urlparse(self.path)
            site.requests.append(("POST", url.path))
            if url.path == "/login":
                form = parse_qs(self._body())
                user, pin = (form.get("USERNAME") or [""])[0], (form.get("PIN") or [""])[0]
                ok = bool(user) and site.users.get(user) == pin
                site.login_attempts.append((user, ok))
                if not ok:
                    return self._send(LOGIN_PAGE.format(error='<p style="color:red">Invalid user name or password</p>'))
                sid = secrets.token_hex(8)
                site.sessions[sid] = {"user": user, "role": next(iter(site.roles), "")}
                return self._send("", 302, [("Location", "/CAisd/nbeitsm5"), ("Set-Cookie", f"sid={sid}; Path=/")])
            if url.path == "/untrusted":
                site.untrusted_clicks += 1
                return self._send("{}")
            session = self._session()
            if session is None:
                return self._send("not signed in", 403)
            if url.path == "/api/role":
                session["role"] = json.loads(self._body()).get("role", "")
                return self._send("{}")
            self._send("not found", 404)

    return Handler
