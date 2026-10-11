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
