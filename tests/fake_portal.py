"""A fake of the SSO portal for the SLA engine's end-to-end tests (real Chromium against a real HTTP server).

``FakePortal`` serves, on 127.0.0.1 and a free port, the pages the SLA engine walks through:

    /App_Security_Login/Login.aspx      Arabic login page (user + password inputs, a hidden decoy input, a login button);
                                        a wrong password shows the same page again
    /Portal/Groups.aspx                 table of packages; the right row has a "تحويل" link that opens the application
                                        in a new window (``open_mode="blank"``) or in the same tab (``"same"``)
    /SecurityCodeRequest/Home.aspx      the application home
    /SecurityCodeRequest/Security_Assign_Maker/Request_ReAssign.aspx
                                        the requests table, in one of the ``variant`` shapes below

``variant`` of the Re-Assign page:
    plain        one table with every row
    paged        a plain table showing ``page_size`` rows with a DataTables-style "next" button (class paginate_button next)
    datatables   only ``page_size`` rows are in the DOM; a small DataTables-like API (window.jQuery.fn.dataTable with
                 tables() and Api) hands out all of them, the hidden ones as markup (no external JS needed)
    search       the table is empty until the "بحث" button is pressed
    empty        the right headers and no rows (an empty queue)
    no_table     a table whose headers lack the sent-date column

Everything is fake: names, numbers and the password are made up by the tests. The server never stores a password, it only
records whether each login attempt was right (``logins``).
"""
import html
import http.server
import json
import threading
import urllib.parse
from datetime import timedelta

HEADERS = ["", "اسم الموظف", "رقم المستخدم", "الفرع / الادارة", "النظام", "نوع الطلب", "موظف الاسناد",
           "تاريخ الارسال للسرية", "مستخدم السرية"]
OWNERS = ["مستخدم اول", "مستخدم ثان", "مستخدم ثالث", ""]            # "" = not assigned yet
AGES = [5, 30, 59, 60, 61, 90, 119, 120, 121, 200, 400, None]      # minutes before "now"; None = no sent date

LOGIN_PATH = "/App_Security_Login/Login.aspx"
GROUPS_PATH = "/Portal/Groups.aspx"
HOME_PATH = "/SecurityCodeRequest/Home.aspx"
REASSIGN_PATH = "/SecurityCodeRequest/Security_Assign_Maker/Request_ReAssign.aspx"
PACKAGE_TEXT = "User Provisioning Portal - موظف اسناد - مستخدم السرية"

_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def format_sent(moment, style):
    """The portal shows dates in several ways: 12 h with AM/PM, Arabic digits with ص/م, plain 24 h."""
    if moment is None:
        return ""
    if style == 0:
        return f"{moment:%d/%m/%Y %I:%M:%S} {'AM' if moment.hour < 12 else 'PM'}"
    if style == 1:
        text = f"{moment:%d/%m/%Y %I:%M} {'ص' if moment.hour < 12 else 'م'}"
        return text.translate(_ARABIC_DIGITS)
    return f"{moment:%d/%m/%Y %H:%M}"


def sample_requests(now, ages=AGES):
    """Requests whose sent time is ``age`` minutes before ``now`` (dict per request, dates cycle through the 3 styles)."""
    out = []
    for i, age in enumerate(ages):
        sent = None if age is None else now - timedelta(minutes=age)
        out.append({
            "employee": f"موظف اختبار {i + 1}", "user_no": f"10{i:02d}", "branch": "فرع تجريبي",
            "system": f"نظام تجريبي {i % 3}", "kind": "فتح مستخدم", "assignee": "مسند اختبار",
            "age": age, "sent": sent, "sent_text": format_sent(sent, i % 3), "owner": OWNERS[i % len(OWNERS)],
        })
    return out


def table_cells(req):
    """The text of one table row, in HEADERS order (the first cell is a checkbox)."""
    return ["", req["employee"], req["user_no"], req["branch"], req["system"], req["kind"], req["assignee"],
            req["sent_text"], req["owner"]]


def _row_html(cells):
    first = "<td><input type='checkbox'></td>"
    return "<tr>" + first + "".join(f"<td>{html.escape(c)}</td>" for c in cells[1:]) + "</tr>"


def _script_json(value):
    return json.dumps(value).replace("</", "<\\/")


class FakePortal:
    """``with FakePortal(...) as portal: portal.url``. Public knobs can be changed between runs."""

    def __init__(self, requests, username="1001", password="secret-pw", variant="plain", open_mode="blank",
                 login_style="arabic", package=True, page_size=5, reassign_redirect=False, echo_password=False):
        self.requests = requests
        self.username = username
        self.password = password
        self.variant = variant
        self.open_mode = open_mode
        self.login_style = login_style          # "arabic": button text دخول; "english": no matching text (Enter key is used)
        self.package = package
        self.page_size = page_size
        self.reassign_redirect = reassign_redirect
        self.echo_password = echo_password      # a sloppy portal that prints the wrong password back on the login page
        self.logins = []                        # True / False per login attempt, never the password
        self.hits = []
        self._server = None
        self._thread = None
        self.url = ""

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        portal = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                portal._handle(self, "GET")

            def do_POST(self):
                portal._handle(self, "POST")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------------ routing
    def _handle(self, h, method):
        parts = urllib.parse.urlsplit(h.path)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        self.hits.append((method, path))
        signed_in = "sid=ok" in h.headers.get("Cookie", "")
        form = {}
        if method == "POST":
            body = h.rfile.read(int(h.headers.get("Content-Length") or 0)).decode("utf-8")
            form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}

        if path.lower() == LOGIN_PATH.lower():
            return self._login(h, method, form)
        if path == "/favicon.ico":
            return self._send(h, 404, "")
        if not signed_in:
            return self._redirect(h, LOGIN_PATH)
        if path == GROUPS_PATH:
            return self._send(h, 200, self._groups_page())
        if path == HOME_PATH:
            return self._send(h, 200, self._page("الصفحة الرئيسية للتطبيق", "<p>تطبيق طلبات السرية</p>"))
        if path == REASSIGN_PATH:
            if self.reassign_redirect:
                return self._redirect(h, GROUPS_PATH)
            return self._send(h, 200, self._reassign_page("btnSearch" in query))
        return self._send(h, 404, self._page("غير موجود", ""))

    @staticmethod
    def _send(h, code, body, headers=()):
        data = body.encode("utf-8")
        h.send_response(code)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def _redirect(self, h, location, headers=()):
        h.send_response(302)
        h.send_header("Location", location)
        h.send_header("Content-Length", "0")
        for k, v in headers:
            h.send_header(k, v)
        h.end_headers()

    # ------------------------------------------------------------------ pages
    @staticmethod
    def _page(title, body, head=""):
        return (f"<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
                f"{head}</head><body>{body}</body></html>")

    def _login(self, h, method, form):
        message = ""
        if method == "POST":
            ok = (form.get("txtUserName") == self.username and form.get("txtPassword") == self.password)
            self.logins.append(ok)
            if ok:
                return self._redirect(h, GROUPS_PATH, [("Set-Cookie", "sid=ok; Path=/; HttpOnly")])
            message = "<div class='err'>بيانات الدخول غير صحيحة</div>"
            if self.echo_password:
                message += f"<div class='err'>{html.escape(form.get('txtPassword', ''))}</div>"
        button = ("<input type='submit' id='btnLogin' name='btnLogin' value='دخول'>" if self.login_style == "arabic"
                  else "<input type='submit' id='btnLogin' name='btnLogin' value='Sign in'>")
        body = (f"<h1>بوابة اختبار</h1>{message}<form method='post' action='{LOGIN_PATH}'>"
                "<input type='hidden' name='__VIEWSTATE' value='state'>"
                "<input type='text' name='decoy' style='display:none'>"
                "<label>المستخدم</label><input type='text' id='txtUserName' name='txtUserName'>"
                "<label>كلمة المرور</label><input type='password' id='txtPassword' name='txtPassword'>"
                f"{button}</form>")
        return self._send(h, 200, self._page("تسجيل", body))

    def _groups_page(self):
        target = "target='_blank'" if self.open_mode == "blank" else ""
        rows = ("<tr><td>User Provisioning Portal - موظف اسناد - مستخدم اخر</td>"
                "<td><a href='/Other/Home.aspx'>تحويل</a></td></tr>")
        if self.package:
            rows += (f"<tr><td>{html.escape(PACKAGE_TEXT)}</td><td><a href='/help'>مساعدة</a> "
                     f"<a href='{HOME_PATH}' {target}>تحويل</a></td></tr>")
        rows += "<tr><td>Another package</td><td><a href='/Other/Two.aspx'>تحويل</a></td></tr>"
        return self._page("المجموعات", f"<table id='groups'><tr><th>الحزمة</th><th>اجراء</th></tr>{rows}</table>")

    def _reassign_page(self, searched):
        head_row = "".join(f"<th>{html.escape(h)}</th>" for h in HEADERS)
        cells = [table_cells(r) for r in self.requests]
        v = self.variant
        if v == "no_table":
            body = ("<table><thead><tr><th>الرقم</th><th>الحالة</th></tr></thead>"
                    "<tbody><tr><td>1</td><td>جديد</td></tr></tbody></table>")
            return self._page("Re-Assign", body)
        if v == "empty":
            return self._page("Re-Assign", f"<table><thead><tr>{head_row}</tr></thead><tbody></tbody></table>")
        if v == "search":
            rows = "".join(_row_html(c) for c in cells) if searched else ""
            body = ("<form method='get'><input type='submit' name='btnSearch' value='بحث'></form>"
                    f"<table><thead><tr>{head_row}</tr></thead><tbody>{rows}</tbody></table>")
            return self._page("Re-Assign", body)
        if v == "plain":
            rows = "".join(_row_html(c) for c in cells)
            return self._page("Re-Assign", f"<table><thead><tr>{head_row}</tr></thead><tbody>{rows}</tbody></table>")
        if v == "paged":
            return self._page("Re-Assign", self._paged_body(head_row, cells))
        if v == "datatables":
            return self._page("Re-Assign", self._datatables_body(head_row, cells))
        raise ValueError(f"unknown variant {v!r}")

    def _paged_body(self, head_row, cells):
        return (f"<table id='grid'><thead><tr>{head_row}</tr></thead><tbody></tbody></table>"
                "<div class='dataTables_paginate'><a class='paginate_button previous disabled'>Previous</a>"
                "<a id='nx' class='paginate_button next' href='#'>Next</a></div>"
                f"<script>const ALL = {_script_json(cells)}, SIZE = {int(self.page_size)};"
                "let page = 0; const body = document.querySelector('#grid tbody'), nx = document.getElementById('nx');"
                "const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;');"
                "function render(){ body.innerHTML = ALL.slice(page*SIZE, (page+1)*SIZE).map(r => '<tr><td><input type=checkbox></td>'"
                " + r.slice(1).map(c => '<td>' + esc(c) + '</td>').join('') + '</tr>').join('');"
                " nx.className = 'paginate_button next' + ((page+1)*SIZE >= ALL.length ? ' disabled' : ''); }"
                "nx.onclick = e => { e.preventDefault(); if (!nx.classList.contains('disabled')) { page++; render(); } };"
                "render();</script>")

    def _datatables_body(self, head_row, cells):
        shown = "".join(_row_html(c) for c in cells[:self.page_size])
        # rows that are not in the DOM come back from the API as markup, like a real DataTable's cell().data()
        data = [[c if i == 0 else f"<span class='cell'>{html.escape(c)}</span>" for i, c in enumerate(row)]
                for row in cells]
        return (f"<table id='grid'><thead><tr>{head_row}</tr></thead><tbody>{shown}</tbody></table>"
                f"<script>(function(){{ const DATA = {_script_json(data)}, SHOWN = {int(self.page_size)};"
                "const table = document.getElementById('grid');"
                "function Api(node){ this.node = node; }"
                "Api.prototype.columns = function(){ return {indexes: () => ({toArray: () => DATA[0].map((_, i) => i)})}; };"
                "Api.prototype.column = function(c){ return {header: () => table.tHead.rows[0].cells[c]}; };"
                "Api.prototype.rows = function(){ return {indexes: () => ({toArray: () => DATA.map((_, i) => i)})}; };"
                "Api.prototype.cell = function(r, c){ return {"
                " node: () => r < SHOWN ? table.tBodies[0].rows[r].cells[c] : null,"
                " data: () => DATA[r][c]}; };"
                "const dt = function(){}; dt.tables = () => [table]; dt.Api = Api;"
                "window.jQuery = {fn: {dataTable: dt}}; })();</script>")


if __name__ == "__main__":          # python -m tests.fake_portal  -> browse it by hand
    from datetime import datetime
    with FakePortal(sample_requests(datetime.now().replace(second=0, microsecond=0))) as fake:
        print(f"Fake portal on {fake.url}{LOGIN_PATH}  (user {fake.username}, password {fake.password}) - Ctrl+C to stop")
        threading.Event().wait()
