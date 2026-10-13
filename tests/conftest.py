import datetime as dt

import pytest

from calllog import create_app, security


class Clock:
    def __init__(self, start):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, **kw):
        self.value += dt.timedelta(**kw)


class Browser:
    """Tiny helper that behaves like a signed-in browser tab (cookies + CSRF header)."""

    def __init__(self, app):
        self.client = app.test_client()
        self.csrf = None

    def _token(self):
        self.client.get("/login")
        with self.client.session_transaction() as s:
            return s["csrf"]

    def login(self, uid, password, follow=False):
        resp = self.client.post("/login", data={"user_id": uid, "password": password,
                                                "csrf_token": self._token()}, follow_redirects=follow)
        if resp.status_code in (302, 303):
            with self.client.session_transaction() as s:
                self.csrf = s.get("csrf")
        return resp

    def api(self, method, url, json=None, headers=None, **kw):
        headers = {**({"X-CSRF-Token": self.csrf} if self.csrf else {}), **(headers or {})}
        return self.client.open(url, method=method, json=json, headers=headers, **kw)

    def get(self, url, **kw):
        return self.api("GET", url, **kw)

    def post(self, url, json=None, **kw):
        return self.api("POST", url, json=json, **kw)

    def put(self, url, json=None, **kw):
        return self.api("PUT", url, json=json, **kw)

    def delete(self, url, **kw):
        return self.api("DELETE", url, **kw)


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    monkeypatch.setattr(security, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture
def clock():
    return Clock(dt.datetime(2026, 10, 11, 9, 0, 0))


@pytest.fixture
def make_app(tmp_path, clock):
    def factory(**overrides):
        return create_app({"data_dir": str(tmp_path / "data"), "clock": clock,
                           "secret_key": "test-secret", "user_edit_days": None, **overrides})
    return factory


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def admin(app):
    """Signed-in administrator (initial password already changed)."""
    b = Browser(app)
    b.login("admin", app.config["INITIAL_ADMIN_PASSWORD"])
    resp = b.client.post("/account/password", data={
        "current_password": app.config["INITIAL_ADMIN_PASSWORD"], "new_password": "AdminPass123",
        "confirm_password": "AdminPass123", "csrf_token": b.csrf})
    assert resp.status_code == 302
    return b


@pytest.fixture
def make_employee(app, admin):
    """Create an employee through the API and return a signed-in Browser for them."""
    def factory(uid, name="Employee", dept="Identity Management"):
        r = admin.post("/api/admin/users", {"user_id": uid, "name": name, "department": dept,
                                            "password": "TempPass123"})
        assert r.status_code == 201, r.get_json()
        b = Browser(app)
        b.login(uid, "TempPass123")
        b.client.post("/account/password", data={
            "current_password": "TempPass123", "new_password": "MyOwnPass123",
            "confirm_password": "MyOwnPass123", "csrf_token": b.csrf})
        return b
    return factory


# ---------------------------------------------------------------------------------------------------------------
# IP phone directory test data. Everything here is made up: the "people" are placeholders that must never show up
# anywhere (the importer has to drop the displayName column completely), the units are branch / department names.
IP_HEADER = ["telephoneNumber", "displayName", "msExchExtensionAttribute27"]
IP_NAME_SENTINELS = ("Zed Quartz Testperson", "Maya Placeholder", "سينتينل ألفا")
IP_ROWS = [
    [75942, "Zed Quartz Testperson", "فرع مصر الجديدة"],          # a unit that is a Call Log branch (after dropping "فرع")
    ["٨٢٨٧٧", "Maya Placeholder", "جامعة الازهر#"],               # Arabic-Indic digits, a '#' left behind by the export
    [73550, "Person C", "فرع روكسي"],                             # one number under two units: the more frequent one first
    [73550, "Person D", "فرع روكسي"],
    [73550, "Person E", "إدارة الموارد البشرية"],                 # a department, not a branch
    [None, "Section header row", None],                            # header-ish rows: no number
    [None, "Maya Placeholder", "فرع روكسي"],
    [61234, "Person F", None],                                     # no unit
    ["abc", "Person G", "فرع روكسي"],                              # not a number
    ["12", "Person H", "فرع روكسي"],                               # too short
    [75943, "Person I", "فرع المطرية"],                            # two branches have this name: the branch stays undecided
    ["77 123", "سينتينل ألفا", "إدارة الموارد البشرية"],           # spaces inside the number
    ["01001234567", "Person K", "مكتب الاعلام"],                   # a 11-digit number, a department
    [75942, "Person L", "فرع مصر الجديدة"],                        # the same number and unit again
    [88001, "Person M", "فرع الدقي2000"],                          # digits glued to the name
    [88002, "Person N", "فرع طيبة"],
    [88002, "Person O", "فرع طيبه"],                               # the same name spelled another way: one unit, not two
    [None, None, None],                                            # an empty line
]
# What the rows above must give: 17 data rows read; kept: 75942 x2, 82877, 73550 x3, 75943, 77123, 01001234567, 88001, 88002 x2 = 12;
# skipped: 2 without number, 1 without unit, 2 bad numbers.
IP_EXPECTED = {"rows": 17, "kept": 12, "numbers": 8, "ambiguous": 1, "no_number": 2, "no_unit": 1, "bad_number": 2}


def ip_workbook(rows=None, header=IP_HEADER, sheet="Sheet1"):
    """The bytes of a small .xlsx like the Active Directory export (three columns)."""
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    if header:
        ws.append(list(header))
    for row in IP_ROWS if rows is None else rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def import_ip_phones(admin):
    """``import_ip_phones(rows=None, name="ip_phones.xlsx")`` -> the answer of the import (asserts it worked)."""
    import io

    def run(rows=None, name="ip_phones.xlsx", raw=None, expect=200):
        data = raw if raw is not None else ip_workbook(rows)
        resp = admin.post("/api/admin/ip-phones/import", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")
        assert resp.status_code == expect, resp.get_json()
        return resp.get_json()
    return run
