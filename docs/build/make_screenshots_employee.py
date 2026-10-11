#!/usr/bin/env python3
"""Employee screenshots for the user guide  ->  docs/img/emp-01 ... emp-10 (PNG).

Re-runnable: it seeds a fresh demo data folder, starts its own server, takes the pictures with
headless Chromium (Playwright) and stops the server again.

    python docs/build/make_screenshots_employee.py

Needs the packages from requirements.txt plus playwright, and a Chromium browser.
Settings (all optional, environment variables):
    CALLLOG_SHOT_PORT   port of the temporary server              (default 8099)
    CALLLOG_SHOT_DATA   demo data folder; it is deleted and re-created   (default: a temporary folder)
    CHROMIUM_PATH       Chromium executable                        (default /opt/pw-browsers/chromium)
"""
import base64
import datetime as dt
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
OUT = PROJECT / "docs" / "img"
PORT = int(os.environ.get("CALLLOG_SHOT_PORT", "8099"))
BASE = f"http://127.0.0.1:{PORT}"
CHROMIUM = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium")
ORANGE = "#ee7203"
MAX_W = 1300
VIEW5 = 520          # window height used for the "saved" picture (the toast is shown 20 px above the bottom edge)
BACKDROP = "dialog::backdrop { background: #898988 !important; }"     # plain grey = the dimmed blank page


# --------------------------------------------------------------------------- demo data
def seed(data_dir):
    """One admin, nine employees (230966..230974), five days of calls (about 80 % with a branch)."""
    sys.path.insert(0, str(PROJECT))
    from calllog import create_app, security
    from calllog.storage import new_record_id, iso_now
    security.PBKDF2_ITERATIONS = 20000
    app = create_app({"data_dir": data_dir, "secret_key": "demo"})
    store = app.extensions["store"]
    now = dt.datetime.now().replace(microsecond=0)
    store.update_user("admin", {"password_hash": security.hash_password("AdminPass123"), "must_change_password": False})
    people = [("230966", "خالد محمد فكرى", "إدارة اعمال الهوية"), ("230967", "سارة أحمد", "إدارة اعمال الهوية"),
              ("230968", "Omar Hassan", "Cards Support"), ("230969", "منى عبد الله", "Cards Support"),
              ("230970", "Youssef Adel", "Digital Banking"), ("230971", "هدى سمير", "Digital Banking"),
              ("230972", "Karim Nabil", "Digital Banking"), ("230973", "ليلى إبراهيم", "Cards Support"),
              ("230974", "Mona Idle", "Cards Support")]
    for uid, name, dept in people:
        store.create_user({"user_id": uid, "name": name, "department": dept, "role": "user", "active": True,
                           "must_change_password": uid == "230973",
                           "password_hash": security.hash_password("Employee123"),
                           "created_at": iso_now(now), "last_login": iso_now(now) if uid != "230972" else None})
    branches = json.load(open(PROJECT / "calllog" / "default_branches.json", encoding="utf-8"))
    random.seed(7)
    reasons = ["answered", "solved", "solved", "answered", "transferred", "followup"]
    for back in range(0, 5):
        day = (now - dt.timedelta(days=back)).date()
        for uid, name, dept in people[:-1]:
            n = random.randint(14, 34) if back else random.randint(10, 26)
            for _ in range(n):
                hour = random.choice([8, 9, 9, 10, 10, 10, 11, 11, 12, 13, 13, 14, 14, 15])
                t = dt.time(hour, random.randint(0, 59), random.randint(0, 59))
                if back == 0 and dt.datetime.combine(day, t) > now:
                    t = (now - dt.timedelta(minutes=random.randint(1, 120))).time()
                b = random.choice(branches)
                store.add_record({
                    "id": new_record_id(day.isoformat(), uid), "user_id": uid, "date": day.isoformat(),
                    "time": t.isoformat(), "ip_phone": str(random.randint(10000, 99999)), "reason": random.choice(reasons),
                    **({"branch": b["code"], "branch_name": b["name"], "region": b["region"]} if random.random() < 0.8
                       else {"branch": "", "branch_name": "", "region": ""}),
                    "note": random.choice(["", "", "", "", "رقم التذكرة 5521", "callback requested"]),
                    "created_at": iso_now(now), "updated_at": None, "updated_by": None})
    print("seeded", data_dir)


# --------------------------------------------------------------------------- callout overlay
OVERLAY_JS = """
({items, host}) => {
  const hostEl = host === 'body' ? document.body : document.querySelector(host);
  const r = hostEl.getBoundingClientRect();
  const ox = host === 'body' ? window.scrollX : -(r.left + hostEl.clientLeft);
  const oy = host === 'body' ? window.scrollY : -(r.top + hostEl.clientTop);
  const add = (css, text) => {
    const d = document.createElement('div');
    d.setAttribute('data-shot-overlay', '');
    d.style.cssText = css;
    if (text) d.textContent = text;
    hostEl.appendChild(d);
  };
  for (const it of items) {
    const b = it.box, x = b.x + ox, y = b.y + oy, inf = it.inflate === undefined ? 3 : it.inflate;
    if (it.outline !== false)
      add(`position:absolute;box-sizing:border-box;pointer-events:none;z-index:2147483646;border:2px solid #ee7203;` +
          `border-radius:8px;left:${x - inf}px;top:${y - inf}px;width:${b.width + 2 * inf}px;height:${b.height + 2 * inf}px`);
    let left = x - inf - 13, top = y - inf - 13;                   // default: centred on the top-left corner
    if (it.pos === 'above') { left = x + b.width / 2 - 13; top = y - inf - (it.gap === undefined ? 4 : it.gap) - 26; }
    if (it.pos === 'right') { left = x + b.width - 26 - 12; top = y + b.height / 2 - 13; }
    left += it.dx || 0; top += it.dy || 0;
    add(`position:absolute;pointer-events:none;z-index:2147483647;width:26px;height:26px;border-radius:50%;` +
        `background:#ee7203;color:#fff;font:700 14px/26px Arial,Helvetica,sans-serif;text-align:center;` +
        `outline:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.35);left:${left}px;top:${top}px`, String(it.n));
  }
}
"""


def overlay(page, items, host="body"):
    page.evaluate(OVERLAY_JS, {"items": items, "host": host})


def clear_overlay(page):
    page.evaluate("document.querySelectorAll('[data-shot-overlay]').forEach(e => e.remove())")


def box(loc):
    b = loc.bounding_box()
    assert b, "element is not visible"
    return b


def union(*boxes):
    x0 = min(b["x"] for b in boxes); y0 = min(b["y"] for b in boxes)
    x1 = max(b["x"] + b["width"] for b in boxes); y1 = max(b["y"] + b["height"] for b in boxes)
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


MANIFEST = []


def record(name, desc, callouts=""):
    w, h = png_size(OUT / name)
    assert w <= MAX_W, f"{name} is {w}px wide"
    MANIFEST.append({"file": name, "width": w, "height": h, "description": desc, "callouts": callouts})
    print(f"  {name}: {w}x{h}")


def crop(page, name, b, pad=(0, 0, 0, 0)):
    """Screenshot of a box given in viewport coordinates (+ padding left, top, right, bottom).
    Playwright reads the clip relative to the visible viewport, so the box must lie inside it."""
    vp = page.viewport_size
    l, t, r, btm = pad
    x0 = max(0, b["x"] - l); y0 = max(0, b["y"] - t)
    x1 = min(vp["width"], b["x"] + b["width"] + r); y1 = min(vp["height"], b["y"] + b["height"] + btm)
    page.screenshot(path=str(OUT / name), clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})


def crop_scaled(page, name, clip):
    """Chromium renders the clip again at a smaller scale (sharp text) so a 1366 px wide page fits 1300 px."""
    cdp = page.context.new_cdp_session(page)
    scale = (MAX_W - 0.5) / clip["width"]
    res = cdp.send("Page.captureScreenshot", {"format": "png", "clip": {**clip, "scale": scale}, "captureBeyondViewport": False})
    (OUT / name).write_bytes(base64.b64decode(res["data"]))
    cdp.detach()


def settle(page, ms=350):
    page.mouse.move(2, 2)          # no hover effects, no chart tooltip
    page.wait_for_timeout(ms)


# --------------------------------------------------------------------------- the pictures
def take_all(p):
    from playwright.sync_api import expect
    kw = {"executable_path": CHROMIUM} if Path(CHROMIUM).exists() else {}
    browser = p.chromium.launch(args=["--no-sandbox"], **kw)
    ctx = browser.new_context(viewport={"width": 1366, "height": 800}, bypass_csp=True, device_scale_factor=1,
                              color_scheme="light", locale="en-US")
    page = ctx.new_page()
    problems = []
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("response", lambda r: r.status >= 400 and problems.append(f"HTTP {r.status}: {r.url}"))

    # ---- 1. sign-in card
    page.goto(BASE + "/login")
    assert page.eval_on_selector("img.logo", "i => i.complete && i.naturalWidth > 0")
    settle(page)
    crop(page, "emp-01-login.png", box(page.locator(".auth-card")), pad=(50, 30, 50, 30))
    record("emp-01-login.png", "Sign-in card (User ID, Password, Sign in)")

    page.fill("input[name=user_id]", "230966"); page.fill("input[name=password]", "Employee123")
    page.click("button[type=submit]"); page.wait_for_url("**/dashboard")
    expect(page.locator("#recordsBody tr").first).to_be_visible()
    expect(page.locator("#pName")).to_have_text("خالد محمد فكرى")

    # ---- 2. dashboard after sign-in (top part, down to the start of "Today's calls")
    page.set_viewport_size({"width": 1366, "height": 1000})
    page.evaluate("window.scrollTo(0, 0)")
    settle(page, 700)
    top_h = box(page.locator("main > section.card").last)["y"] - 14          # just above the "Today's calls" card
    overlay(page, [
        {"n": 1, "box": box(page.locator(".profile"))},
        {"n": 2, "box": box(page.locator("#entryCard"))},
        {"n": 3, "box": box(page.locator(".kpis"))},
        {"n": 4, "box": box(page.locator(".chart-card"))},
    ])
    crop_scaled(page, "emp-02-dashboard.png", {"x": 0, "y": 0, "width": 1366, "height": round(top_h)})
    clear_overlay(page)
    record("emp-02-dashboard.png", "Employee dashboard after sign-in: top bar, profile and date controls, Log a call card, "
           "number tiles, Calls by hour chart, Calls by reason",
           "1=name/ID/department row with the date arrows; 2=Log a call card; "
           "3=number tiles (Calls today, Resolved, Transferred, Ticket Inquiry); 4=Calls by hour chart")
    page.set_viewport_size({"width": 1366, "height": 900})        # tall enough for the whole "Log a call" card
    page.evaluate("window.scrollTo(0, 0)")

    # ---- 3. IP phone typed, branch list open
    page.fill("#ipInput", "75942")
    page.press("#ipInput", "Enter")
    page.keyboard.type("100", delay=40)
    items = page.locator(".bp-list li")
    expect(items.first).to_be_visible()
    first_text = items.first.inner_text().replace("\n", " ")
    assert "100" in first_text and "مصر الجديدة" in first_text, first_text
    page.wait_for_timeout(300)
    settle(page, 100)
    page.focus(".bp-input")                       # moving the mouse away must not close the list
    card = box(page.locator("#entryCard"))
    overlay(page, [
        {"n": 1, "box": box(page.locator("#ipInput")), "dy": 8},
        {"n": 2, "box": box(page.locator(".bp-input")), "dy": 8},
        {"n": 3, "box": box(items.first), "pos": "right", "outline": False},
    ])
    crop(page, "emp-03-ip-and-branch.png", card, pad=(12, 12, 12, 12))
    clear_overlay(page)
    record("emp-03-ip-and-branch.png", "Log a call card with IP phone 75942 typed and the branch list open for \"100\" "
           f"(first suggestion: {first_text})", "1=IP phone box; 2=Branch box; 3=suggestion in the list (100 مصر الجديدة)")

    # ---- 4. branch chosen, reasons visible
    page.keyboard.press("Enter")
    expect(page.locator(".bp-list")).to_be_hidden()
    expect(page.locator(".bp-input")).to_have_value("100 · مصر الجديدة")
    page.evaluate("document.activeElement.blur()")
    settle(page, 200)
    overlay(page, [
        {"n": 1, "box": box(page.locator(".bp-input")), "dy": 8},
        {"n": 2, "box": box(page.locator("#reasons")), "dy": 8},
    ])
    crop(page, "emp-04-reasons.png", card, pad=(12, 12, 12, 12))
    clear_overlay(page)
    record("emp-04-reasons.png", "Same card after choosing the branch (100 · مصر الجديدة); the four reason buttons are visible",
           "1=chosen branch; 2=the four reason buttons (click one to save)")

    # ---- 5. saved: real save through the UI, then toast + top rows of "Today's calls"
    page.click(".reason-btn:has-text('Problem solved')")
    expect(page.locator("#recordsBody tr").first).to_contain_text("75942")
    expect(page.locator("#recordsBody tr.flash")).to_have_count(1)
    expect(page.locator(".toast-msg")).to_have_count(0, timeout=8000)           # the real toast is gone again
    page.wait_for_timeout(2000)
    page.set_viewport_size({"width": 1366, "height": VIEW5})
    page.evaluate("""() => {              // short list + room below it, so the toast sits under the rows instead of on top of them
        document.querySelector('main.page').style.paddingBottom = '400px';
        document.querySelectorAll('#recordsBody tr').forEach((tr, i) => { if (i >= 5) tr.style.display = 'none'; });
    }""")
    calls = page.locator("main > section.card").last
    page.evaluate("y => window.scrollTo(0, y)", box(calls)["y"] + page.evaluate("window.scrollY") - 10)
    page.evaluate("""() => {
        document.querySelectorAll('.toast-msg').forEach(e => e.remove());
        window.CL.toast('Saved · 75942 · Problem solved');
        const tr = document.querySelector('#recordsBody tr');           // show the highlight of a freshly saved row
        tr.classList.remove('flash'); void tr.offsetWidth; tr.classList.add('flash');
        tr.querySelectorAll('td').forEach(td => { td.style.animationDelay = '-0.35s'; td.style.animationPlayState = 'paused'; });
    }""")
    settle(page, 150)
    row1 = page.locator("#recordsBody tr").first
    toast = page.locator(".toast-msg")
    expect(toast).to_have_text("Saved · 75942 · Problem solved")
    cb = box(calls)
    overlay(page, [{"n": 1, "box": box(toast)}, {"n": 2, "box": box(row1)}])
    crop(page, "emp-05-saved.png", {"x": cb["x"], "y": 0, "width": cb["width"], "height": VIEW5}, pad=(10, 0, 10, 0))
    clear_overlay(page)
    record("emp-05-saved.png", "Toast \"Saved · 75942 · Problem solved\" with the newly saved call at the top of Today's calls",
           "1=the \"Saved\" toast; 2=the new row at the top of the list")
    page.set_viewport_size({"width": 1366, "height": 800})
    page.reload()
    expect(page.locator("#recordsBody tr").first).to_contain_text("75942")
    page.wait_for_timeout(500)

    # ---- 6. calls list header + first 4 rows
    page.evaluate("document.querySelectorAll('#recordsBody tr').forEach((tr, i) => { if (i >= 4) tr.style.display = 'none'; })")
    calls = page.locator("main > section.card").last
    page.evaluate("y => window.scrollTo(0, y)", box(calls)["y"] + page.evaluate("window.scrollY") - 20)
    settle(page, 300)
    row1 = page.locator("#recordsBody tr").first
    edit_btn = row1.get_by_role("button", name="Edit call")
    del_btn = row1.get_by_role("button", name="Delete call")
    overlay(page, [
        {"n": 1, "box": box(page.locator("#searchInput"))},
        {"n": 2, "box": union(box(page.locator("label.inline-field")), box(page.locator("#dlXlsx")), box(page.locator("#dlCsv")))},
        {"n": 3, "box": box(edit_btn), "pos": "above"},
        {"n": 4, "box": box(del_btn), "pos": "above"},
    ])
    crop(page, "emp-06-calls-list.png", box(calls), pad=(10, 10, 10, 10))
    clear_overlay(page)
    page.evaluate("document.querySelectorAll('#recordsBody tr').forEach(tr => { tr.style.display = ''; })")
    record("emp-06-calls-list.png", "Today's calls card: search box, Download (this day / this month) with Excel and CSV, "
           "first four rows with Edit and delete buttons",
           "1=search box; 2=Download Excel/CSV; 3=Edit button of the first row; 4=delete (trash) button of the first row")

    # ---- 7. edit dialog on a call that has a branch (the call saved above: IP 75942, branch 100)
    row1 = page.locator("#recordsBody tr").first
    expect(row1.locator(".branch-cell strong")).to_have_text("100")
    backdrop = page.add_style_tag(content=BACKDROP)
    row1.get_by_role("button", name="Edit call").click()
    dlg = page.locator("dialog[open]")
    expect(dlg).to_be_visible()
    expect(dlg.locator(".bp-input")).to_have_value("100 · مصر الجديدة")
    settle(page, 400)
    overlay(page, [
        {"n": 1, "box": box(dlg.locator("input[inputmode=numeric]")), "dy": 8},
        {"n": 2, "box": box(dlg.locator(".bp-input")), "dy": 8},
        {"n": 3, "box": box(dlg.locator(".radio-list")), "dy": 8},
        {"n": 4, "box": box(dlg.locator("button[type=submit]")), "pos": "above"},
    ], host="dialog[open]")
    crop(page, "emp-07-edit.png", box(dlg), pad=(22, 22, 22, 22))
    clear_overlay(page)
    record("emp-07-edit.png", "\"Edit call\" dialog on a call with a branch: IP phone, Branch, reason list, note, Save changes / Cancel",
           "1=IP phone; 2=Branch; 3=reason list; 4=Save changes")
    dlg.get_by_role("button", name="Cancel").click()
    expect(page.locator("dialog")).to_have_count(0)

    # ---- 8. delete confirmation (never confirmed)
    row1 = page.locator("#recordsBody tr").first
    row1.get_by_role("button", name="Delete call").click()
    dlg = page.locator("dialog[open]")
    expect(dlg).to_be_visible()
    expect(dlg).to_contain_text("Delete this call?")
    settle(page, 400)
    crop(page, "emp-08-delete.png", box(dlg), pad=(22, 22, 22, 22))
    record("emp-08-delete.png", "\"Delete this call?\" confirmation with Cancel and Delete call buttons (not confirmed)")
    dlg.get_by_role("button", name="Cancel").click()
    expect(page.locator("dialog")).to_have_count(0)
    expect(page.locator("#recordsBody tr").first).to_contain_text("75942")      # still there

    # ---- 9. date controls while viewing yesterday
    backdrop.evaluate("e => e.remove()")
    page.evaluate("window.scrollTo(0, 0)")
    page.click("#prevDay")
    expect(page.locator("#entryCard")).to_be_hidden()
    expect(page.locator("#todayBtn")).to_be_enabled()
    expect(page.locator("#totalLabel")).not_to_have_text("Calls today")
    page.wait_for_timeout(600)
    settle(page, 300)
    layout_bug = box(page.locator(".stats"))["width"] < 700
    if layout_bug:        # known app problem: with the entry card hidden, the tiles are squeezed into the 400 px column
        print("  NOTE: number tiles are squeezed into the narrow column on a past day; widening them for the picture only")
        fix = page.add_style_tag(content=".grid-main { grid-template-columns: minmax(0, 1fr) !important; }")
        page.wait_for_timeout(300)
    prof, kp = box(page.locator(".profile")), box(page.locator(".kpis"))
    pad = {"inflate": 1, "pos": "above", "gap": -3}
    overlay(page, [
        {"n": 1, "box": box(page.locator("#prevDay")), **pad},
        {"n": 2, "box": box(page.locator("#dayInput")), **pad},
        {"n": 1, "box": box(page.locator("#nextDay")), **pad},
        {"n": 3, "box": box(page.locator("#todayBtn")), **pad},
    ])
    crop(page, "emp-09-dates.png", union(prof, kp), pad=(20, 19, 20, 14))
    clear_overlay(page)
    if layout_bug:
        fix.evaluate("e => e.remove()")
        problems.append("APP LAYOUT BUG: on a past day (entry card hidden) .grid-main keeps two columns, so the stats section "
                        "(tiles, chart) is squeezed into the 400 px left column and the tiles overflow. emp-09 was shot with a "
                        "temporary CSS override (.grid-main one column); the script skips the override once the app is fixed.")
    yesterday = page.inner_text("#totalLabel") + " = " + page.inner_text("#kTotal")
    record("emp-09-dates.png", f"Profile row with the date controls while viewing yesterday ({yesterday}); Log a call card hidden, "
           "Today button enabled, number tiles below", "1=previous / next day arrows; 2=date box; 3=Today button")

    # ---- 10. forced password change for 230973
    ctx.clear_cookies()
    page.goto(BASE + "/login")
    page.fill("input[name=user_id]", "230973"); page.fill("input[name=password]", "Employee123")
    page.click("button[type=submit]"); page.wait_for_url("**/password")
    expect(page.locator(".auth-card")).to_contain_text("set by an administrator")
    settle(page, 400)
    crop(page, "emp-10-password.png", box(page.locator(".auth-card")), pad=(50, 30, 50, 30))
    record("emp-10-password.png", "Change password page shown at first sign-in (an administrator set the password)")

    left = page.evaluate("document.querySelectorAll('[data-shot-overlay]').length")
    assert left == 0, "leftover overlay elements"
    browser.close()
    return problems


def wait_for_server(proc, seconds=40):
    end = time.time() + seconds
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError("the server stopped early, see its log")
        try:
            urllib.request.urlopen(BASE + "/login", timeout=2).read()
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("the server did not start")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--seed":
        return seed(sys.argv[2])
    from playwright.sync_api import sync_playwright
    data = os.environ.get("CALLLOG_SHOT_DATA") or str(Path(tempfile.mkdtemp(prefix="calllog-shots-")) / "data")
    if Path(data).exists():
        shutil.rmtree(data)
    OUT.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "--seed", data], check=True, timeout=300)
    log = open(Path(data).parent / (Path(data).name + "-server.log"), "w")
    proc = subprocess.Popen([sys.executable, str(PROJECT / "run.py"), "--port", str(PORT), "--no-browser"],
                            env={**os.environ, "CALLLOG_DATA_DIR": data}, stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_for_server(proc)
        with sync_playwright() as p:
            problems = take_all(p)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    print(json.dumps(MANIFEST, ensure_ascii=False, indent=1))
    print("PROBLEMS:", problems or "none")


if __name__ == "__main__":
    main()
