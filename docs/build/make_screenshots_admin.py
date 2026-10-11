#!/usr/bin/env python3
"""Regenerate the ADMINISTRATOR and SETUP images of the user guide  ->  docs/img/adm-*.png, setup-*.png

    python docs/build/make_screenshots_admin.py [--work-dir DIR] [--port 8098] [--console-port 8097]

Needs a Python with flask, waitress, openpyxl, playwright and requests (the project's dev environment) and the
pre-installed Chromium (/opt/pw-browsers/chromium, override with CHROMIUM=...).

What it does, by itself, from nothing:
  1. fills a brand-new demo data folder (fake accounts, five days of fake calls, ~80 % of them with a branch),
  2. starts the real program on 127.0.0.1:<port> (python run.py --port <port> --no-browser),
  3. drives the administrator pages with Playwright (one browser context, 1366x800, light mode) and takes the
     adm-*.png pictures; numbered orange callouts are injected just before each shot and removed right after,
  4. starts the real program once more on an EMPTY data folder (python run.py --share ...), copies what it prints
     and renders that text as the "Command Prompt" picture (setup-03),
  5. draws the two plain diagrams (setup-01, setup-02) from HTML/CSS,
  6. stops every server it started.

Nothing here creates or mentions a .bat/.cmd/.ps1/.vbs file: the program is only ever started with python run.py.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import datetime as dt
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # the project folder (run.py lives here)
IMG = ROOT / "docs" / "img"
CHROMIUM = os.environ.get("CHROMIUM", "/opt/pw-browsers/chromium")
MAX_W = 1300                                         # no picture is wider than this
UNC = r"\\10.104.46.34\calllog"

MANIFEST: list[dict] = []
NOTES: list[str] = []


# ====================================================================== demo data
def seed_demo_data(data_dir: str) -> None:
    """One admin, 9 employees (one must change password, one never calls), five days of fake calls."""
    import random
    sys.path.insert(0, str(ROOT))
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
                           "must_change_password": uid == "230973", "password_hash": security.hash_password("Employee123"),
                           "created_at": iso_now(now), "last_login": iso_now(now) if uid != "230972" else None})
    branches = json.load(open(ROOT / "calllog" / "default_branches.json", encoding="utf-8"))
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
                store.add_record({
                    "id": new_record_id(day.isoformat(), uid), "user_id": uid, "date": day.isoformat(),
                    "time": t.isoformat(), "ip_phone": str(random.randint(10000, 99999)), "reason": random.choice(reasons),
                    **(lambda b: {"branch": b["code"], "branch_name": b["name"], "region": b["region"]}
                       if random.random() < 0.8 else {"branch": "", "branch_name": "", "region": ""})(random.choice(branches)),
                    "note": random.choice(["", "", "", "", "رقم التذكرة 5521", "callback requested"]),
                    "created_at": iso_now(now), "updated_at": None, "updated_by": None})


# ====================================================================== servers
def port_is_free(port: int) -> bool:
    with socket.socket() as s:
        if os.name != "nt":                   # a port that was just released (TIME_WAIT) still counts as free
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def start_program(run_args: list[str], data_dir: Path, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ, PYTHONUNBUFFERED="1", CALLLOG_DATA_DIR=str(data_dir))
    return subprocess.Popen([sys.executable, "-u", str(ROOT / "run.py"), *run_args], env=env, cwd=str(log_path.parent),
                            stdout=open(log_path, "wb"), stderr=subprocess.STDOUT)


def stop_program(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def wait_for_http(url: str, proc: subprocess.Popen, seconds: float = 40) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError("the program exited early - see its log")
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"{url} did not answer within {seconds}s")


def capture_console_text(port: int, work: Path) -> str:
    """Start the program on an empty data folder, as the administrator would, and copy what it prints."""
    data = work / "console_data"
    shutil.rmtree(data, ignore_errors=True)
    log = work / "console_out.txt"
    proc = start_program(["--share", "--no-browser", "--port", str(port)], data, log)
    try:
        end = time.time() + 40
        while time.time() < end:
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            if text.count("=" * 66) >= 2:                  # banner printed completely
                time.sleep(2)                              # let the server start, like a real window would
                break
            if proc.poll() is not None:
                raise RuntimeError("run.py exited: " + text)
            time.sleep(0.3)
        else:
            raise RuntimeError("no banner within 40 s")
    finally:
        stop_program(proc)
    text = log.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    text = text.replace(str(data), UNC + r"\data")         # the only edit: the long scratch path
    shutil.rmtree(data, ignore_errors=True)
    return text.rstrip("\n")


# ====================================================================== picture helpers
def png_size(png: bytes) -> tuple[int, int]:
    return struct.unpack(">II", png[16:24])


def box_of(x) -> dict:
    if isinstance(x, dict):
        return x
    b = x.bounding_box()
    if not b:
        raise RuntimeError("target is not visible: " + repr(x))
    return b


def union(*items) -> dict:
    bs = [box_of(i) for i in items]
    x0, y0 = min(b["x"] for b in bs), min(b["y"] for b in bs)
    x1, y1 = max(b["x"] + b["width"] for b in bs), max(b["y"] + b["height"] for b in bs)
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def text_box(loc) -> dict:
    """The box of the text itself (not of the full-width block that holds it)."""
    return loc.evaluate("e => { const r = document.createRange(); r.selectNodeContents(e); const b = r.getBoundingClientRect();"
                        " return {x: b.x, y: b.y, width: b.width, height: b.height}; }")


def pad_box(b: dict, left=0, top=0, right=0, bottom=0) -> dict:
    return {"x": b["x"] - left, "y": b["y"] - top, "width": b["width"] + left + right, "height": b["height"] + top + bottom}


CALLOUT_JS = """
({items, host}) => {
  const root = host === 'dialog' ? document.querySelector('dialog[open]') : document.body;
  const pos = host === 'dialog' ? 'fixed' : 'absolute';
  for (const it of items) {
    if (it.outline) {
      const o = document.createElement('div');
      o.setAttribute('data-shot', '1');
      o.style.cssText = `position:${pos};left:${it.ox}px;top:${it.oy}px;width:${it.ow}px;height:${it.oh}px;` +
        'border:2px solid #ee7203;border-radius:8px;box-sizing:border-box;pointer-events:none;z-index:2147483646;' +
        'box-shadow:0 0 0 1px rgba(255,255,255,.85);';
      root.appendChild(o);
    }
    if (it.n == null) continue;
    const b = document.createElement('div');
    b.setAttribute('data-shot', '1');
    b.textContent = String(it.n);
    b.style.cssText = `position:${pos};left:${it.bx}px;top:${it.by}px;width:26px;height:26px;border-radius:50%;` +
      'background:#ee7203;color:#fff;font:700 14px/26px system-ui,"Segoe UI",Arial,sans-serif;text-align:center;' +
      'box-shadow:0 0 0 2px #fff,0 1px 4px rgba(0,0,0,.35);pointer-events:none;z-index:2147483647;';
    root.appendChild(b);
  }
}
"""
CLEAN_JS = "document.querySelectorAll('[data-shot]').forEach(e => e.remove())"
DIALOG_BACKDROP_CSS = "dialog.dlg::backdrop { background: #d6d5cd !important; }"


def T(n: int, target, pad: int = 3, off: tuple = (-15, -15), outline: bool = True, extra: tuple = ()) -> dict:
    """A numbered callout. off = badge position relative to the target's top-left corner (default: centred on it).
    extra = more boxes that get the same outline (but no badge), e.g. several filter boxes that belong together."""
    return {"n": n, "box": box_of(target), "pad": pad, "off": off, "outline": outline, "extra": [box_of(e) for e in extra]}


class Shooter:
    def __init__(self, ctx, page):
        self.ctx, self.page = ctx, page
        self.helper = ctx.new_page()          # a second page of the same context: used to rescale / to draw diagrams

    # ---- state of the admin page
    def settle(self):
        p = self.page
        p.evaluate("window.scrollTo(0, 0)")
        p.mouse.move(4, 797)                  # park the pointer on blank space (no hover colours, no tooltips)
        p.evaluate("document.fonts.ready.then(() => true)")
        p.wait_for_timeout(300)

    def no_toast(self):
        self.page.wait_for_function("document.querySelectorAll('.toast-msg').length === 0", timeout=8000)

    # ---- one picture
    def shot(self, name: str, clip: dict, targets: list | None = None, host: str = "body", page=None,
             description: str = "", legend: list | None = None):
        page = page or self.page
        items = []
        for t in targets or []:
            b, pad = t["box"], t["pad"]
            items.append({"n": t["n"], "outline": t["outline"], "ox": b["x"] - pad, "oy": b["y"] - pad,
                          "ow": b["width"] + 2 * pad, "oh": b["height"] + 2 * pad,
                          "bx": b["x"] + t["off"][0], "by": b["y"] + t["off"][1]})
            for e in t["extra"]:
                items.append({"n": None, "outline": True, "ox": e["x"] - pad, "oy": e["y"] - pad,
                              "ow": e["width"] + 2 * pad, "oh": e["height"] + 2 * pad, "bx": 0, "by": 0})
        for it in items:                      # every badge and outline must be inside the picture
            rects = [(it["ox"], it["oy"], it["ox"] + it["ow"], it["oy"] + it["oh"])]
            if it["n"] is not None:
                rects.append((it["bx"], it["by"], it["bx"] + 26, it["by"] + 26))
            for (x0, y0, x1, y1) in rects:
                if x0 < clip["x"] - 0.5 or y0 < clip["y"] - 0.5 or x1 > clip["x"] + clip["width"] + 0.5 or y1 > clip["y"] + clip["height"] + 0.5:
                    NOTES.append(f"{name}: callout {it['n']} sticks out of the clip ({x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}) vs {clip}")
        if items:
            page.evaluate(CALLOUT_JS, {"items": items, "host": host})
        c = {k: int(round(v)) for k, v in clip.items()}
        try:
            png = page.screenshot(clip=c, full_page=(host == "body"))
        finally:
            page.evaluate(CLEAN_JS)
        self.save(name, png, description, legend)

    def save(self, name: str, png: bytes, description: str, legend: list | None):
        w, h = png_size(png)
        if w > MAX_W:                         # only the full-bleed pictures (top bar / tabs): shrink by < 5 %
            png = self.rescale(png, MAX_W, round(h * MAX_W / w))
            w, h = png_size(png)
        IMG.mkdir(parents=True, exist_ok=True)
        (IMG / name).write_bytes(png)
        MANIFEST.append({"file": name, "width": w, "height": h, "description": description, "callouts": legend or []})
        print(f"  {name}: {w}x{h}")

    def rescale(self, png: bytes, w: int, h: int) -> bytes:
        p = self.helper
        p.set_viewport_size({"width": w, "height": h})
        uri = "data:image/png;base64," + base64.b64encode(png).decode()
        p.set_content(f'<body style="margin:0"><img id="i" style="display:block;width:{w}px;height:{h}px" src="{uri}"></body>')
        p.evaluate("document.getElementById('i').decode().then(() => true)")
        return p.locator("#i").screenshot()

    # ---- pictures drawn from HTML
    def render_html(self, name: str, doc: str, width: int, selector: str = "#shot", targets_fn=None,
                    description: str = "", legend: list | None = None):
        p = self.helper
        p.set_viewport_size({"width": width, "height": 700})
        p.set_content(doc)
        p.evaluate("document.fonts.ready.then(() => true)")
        p.wait_for_timeout(200)
        el = p.locator(selector)
        if targets_fn:
            b = box_of(el)
            targets = targets_fn(p)
            clip = {"x": b["x"], "y": b["y"], "width": b["width"], "height": b["height"]}
            self.shot(name, clip, targets, host="body", page=p, description=description, legend=legend)
        else:
            self.save(name, el.screenshot(), description, legend)


# ====================================================================== diagrams (HTML/CSS)
FONT = 'system-ui,"Segoe UI",Inter,Arial,sans-serif'
MONO = 'ui-monospace,Consolas,"Cascadia Mono","DejaVu Sans Mono",monospace'

PC_ICON = ('<svg width="54" height="48" viewBox="0 0 64 56" aria-hidden="true"><rect x="4" y="3" width="56" height="36" rx="5" '
           'fill="#fff" stroke="#006a4e" stroke-width="3"/><rect x="10" y="9" width="44" height="24" rx="2" fill="#e6f2ee"/>'
           '<path d="M22 52h20M32 39v13" stroke="#006a4e" stroke-width="3" stroke-linecap="round"/></svg>')
PC_ICON_BIG = PC_ICON.replace('width="54" height="48"', 'width="92" height="80"')
FOLDER_ICON = ('<svg width="24" height="20" viewBox="0 0 24 20" aria-hidden="true"><path d="M1.5 3.5a2 2 0 0 1 2-2h5.6l2.4 2.6h9a2 2 0 0 1 2 2V16.5'
               'a2 2 0 0 1-2 2h-17a2 2 0 0 1-2-2z" fill="#f6b46e" stroke="#ee7203" stroke-width="1.6" stroke-linejoin="round"/></svg>')
FILE_ICON = ('<svg width="24" height="24" viewBox="-2 0 24 24" aria-hidden="true"><path d="M2 1.5h10l6 6V21.5a1.5 1.5 0 0 1-1.5 1.5h-14.5z" fill="#fff" '
             'stroke="#898781" stroke-width="1.6" stroke-linejoin="round"/><path d="M12 1.5v6h6" fill="none" stroke="#898781" stroke-width="1.6" '
             'stroke-linejoin="round"/></svg>')

ARCH_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
body{margin:0;background:#f9f9f7;color:#0b0b0b;font:16px/1.35 __FONT__}
#shot{width:1100px;padding:44px 22px;display:grid;grid-template-columns:252px 1fr 262px 1fr 286px;align-items:center;background:#f9f9f7}
.card{background:#fff;border:2px solid #006a4e;border-radius:14px;overflow:hidden}
.hd{background:#006a4e;color:#fff;font-weight:700;font-size:17px;padding:12px 16px}
.hd small{display:block;margin-top:3px;font:500 13.5px __MONO__;color:#fff}
.bd{padding:18px 16px;display:grid;gap:14px;justify-items:center}
.clients{border-color:#b9b8ac}
.clients .hd{background:#e6f2ee;color:#006a4e}
.pcs{display:flex;gap:10px}
.url{font:700 14px __MONO__;color:#7a3d00;background:#fff3e6;border:1.5px solid #ee7203;border-radius:8px;padding:7px 10px;white-space:nowrap}
.cmd{font:600 14px __MONO__;color:#e8e8e8;background:#0c0c0c;border-radius:8px;padding:8px 12px;white-space:nowrap}
.chip{display:flex;align-items:center;gap:10px;width:100%;border:1px solid #d9d8d0;background:#fcfcfb;border-radius:10px;padding:9px 12px;white-space:nowrap}
.chip b{font-weight:700}.chip span{color:#52514e;font-size:14.5px}
.chips{display:grid;gap:10px;width:100%}
.arrow{display:grid;justify-items:center;gap:8px;padding:0 6px}
.lbl{font-weight:700;font-size:14.5px;color:#3d3c39;white-space:nowrap}
.line{position:relative;height:3px;background:#ee7203;width:calc(100% - 24px);margin:0 12px}
.line::before,.line::after{content:"";position:absolute;top:50%;transform:translateY(-50%);border-top:8px solid transparent;border-bottom:8px solid transparent}
.line::before{left:-11px;border-right:13px solid #ee7203}
.line::after{right:-11px;border-left:13px solid #ee7203}
</style></head><body><div id="shot">
 <div class="card clients"><div class="hd">Employees' browsers</div><div class="bd">
   <div class="pcs">__PC____PC____PC__</div><div class="url">http://&lt;server-pc&gt;:8080</div></div></div>
 <div class="arrow"><div class="lbl">pages</div><div class="line"></div></div>
 <div class="card"><div class="hd">PC that runs run.py</div><div class="bd">__PCBIG__<div class="cmd">python run.py --service</div></div></div>
 <div class="arrow"><div class="lbl">reads/writes files</div><div class="line"></div></div>
 <div class="card"><div class="hd">Shared folder<small>__UNC__</small></div><div class="bd"><div class="chips">
   <div class="chip">__FOLDER__<div><b>program</b> <span>(run.py, calllog/)</span></div></div>
   <div class="chip">__FOLDER__<div><b>libs</b> <span>(Flask)</span></div></div>
   <div class="chip">__FOLDER__<div><b>data</b> <span>(calls, users, log)</span></div></div>
 </div></div></div>
</div></body></html>"""

FOLDER_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
body{margin:0;background:#f9f9f7;color:#0b0b0b;font:16px/1.35 __FONT__}
#shot{width:760px;padding:30px 32px 26px;background:#f9f9f7}
.sheet{background:#fff;border:1px solid #bdbcb0;border-radius:12px;overflow:hidden}
.title{display:flex;align-items:center;gap:10px;padding:14px 18px;background:#f1f0ec;border-bottom:1px solid #dddcd3;font:700 17px __MONO__}
.row{display:flex;align-items:center;gap:14px;padding:11px 18px;border-bottom:1px solid #eeede8;font:16px __MONO__}
.row:last-child{border-bottom:0}
.row svg{flex:none}.row .tag{margin-left:auto;font:13px __FONT__;color:#898781}
.note{margin-top:18px;border:1.5px dashed #ee7203;border-radius:10px;background:#fff7ee;padding:13px 16px;display:flex;flex-wrap:wrap;align-items:center;gap:10px 12px;color:#3d3c39}
.note .t{font-weight:600}
.pill{display:inline-flex;align-items:center;gap:7px;font:15px __MONO__;color:#0b0b0b;background:#fff;border:1px solid #e3c8a8;border-radius:8px;padding:4px 10px}
.pill svg{height:17px;width:auto}
.cap{margin-top:14px;color:#52514e;font-size:14px;font-style:italic}
</style></head><body><div id="shot">
 <div class="sheet"><div class="title">__FOLDER__<span>__UNC__</span></div>
  <div class="row">__FILE__run.py</div>
  <div class="row">__FILE__bootstrap.py</div>
  <div class="row">__FILE__manage.py</div>
  <div class="row">__FILE__requirements.txt</div>
  <div class="row">__FILE__config.example.json</div>
  <div class="row">__FILE__README.md</div>
  <div class="row">__FOLDER__calllog<span class="tag">folder</span></div>
 </div>
 <div class="note"><span class="t">created automatically on first start:</span>
  <span class="pill">__FOLDER__libs</span><span class="pill">__FOLDER__data</span><span class="pill">__FILE__server.log</span></div>
 <div class="cap">What you should see</div>
</div></body></html>"""

CONSOLE_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
body{margin:0;background:#f0efec}
#shot{padding:26px 28px 30px;background:#f0efec;display:inline-block}
.win{width:790px;border-radius:9px;overflow:hidden;background:#0c0c0c;box-shadow:0 10px 30px rgba(0,0,0,.28),0 0 0 1px rgba(0,0,0,.45)}
.tb{display:flex;align-items:center;gap:10px;height:38px;padding:0 0 0 12px;background:#1f1f1f;color:#e8e8e8;font:13.5px __FONT__}
.tb .ic{display:inline-grid;place-items:center;width:20px;height:16px;border:1px solid #cfcfcf;border-radius:2px;font:700 10px __MONO__;color:#e8e8e8}
.tb .ctl{margin-left:auto;display:flex}
.tb .ctl span{width:46px;height:38px;display:grid;place-items:center;font:15px __FONT__;color:#e8e8e8}
.tb .ctl span:last-child{font-size:17px}
.body{margin:0;padding:18px 26px 22px 28px;color:#cccccc;font:15px/1.5 __MONO__}
.ln{white-space:pre;min-height:1.5em}
.t{display:inline-block;padding:0 6px;margin:0 -6px}
</style></head><body><div id="shot"><div class="win">
 <div class="tb"><span class="ic">C:\</span><span>Command Prompt</span><div class="ctl"><span>&#8212;</span><span>&#9633;</span><span>&#10005;</span></div></div>
 <div class="body">__LINES__</div></div></div></body></html>"""


def fill(tpl: str, **kw) -> str:
    out = tpl.replace("__FONT__", FONT).replace("__MONO__", MONO)
    for k, v in kw.items():
        out = out.replace(f"__{k}__", v)
    return out


def console_html(text: str) -> str:
    rows = []
    for i, line in enumerate(text.split("\n")):
        esc = html.escape(line) or " "
        m = re.match(r"^(\s*)(Password:.*)$", line)
        if m:
            esc = html.escape(m.group(1)) + f'<span class="t" id="pw">{html.escape(m.group(2))}</span>'
        m = re.match(r"^(\s*)(Other PCs open:.*)$", line)
        if m:
            esc = html.escape(m.group(1)) + f'<span class="t" id="addr">{html.escape(m.group(2))}</span>'
        rows.append(f'<div class="ln">{esc}</div>')
    return fill(CONSOLE_HTML, LINES="".join(rows))


# ====================================================================== the administrator walk-through
def run_admin_shots(sh: Shooter, base: str, port: int):
    from playwright.sync_api import expect
    import requests

    page = sh.page
    problems = []
    page.on("console", lambda m: m.type in ("error", "warning") and problems.append(f"console {m.type}: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("response", lambda r: r.status >= 400 and problems.append(f"HTTP {r.status}: {r.url}"))

    # a little earlier activity for the audit log: an employee signs in, somebody mistypes a password
    s = requests.Session()
    token = re.search(r'name="csrf_token" value="([^"]+)"', s.get(base + "/login", timeout=10).text).group(1)
    s.post(base + "/login", data={"user_id": "230968", "password": "wrong-password", "csrf_token": token}, timeout=15)
    s = requests.Session()
    token = re.search(r'name="csrf_token" value="([^"]+)"', s.get(base + "/login", timeout=10).text).group(1)
    s.post(base + "/login", data={"user_id": "230967", "password": "Employee123", "csrf_token": token}, timeout=15)
    time.sleep(1.1)

    page.goto(base + "/login")
    page.fill("input[name=user_id]", "admin")
    page.fill("input[name=password]", "AdminPass123")
    page.click("button[type=submit]")
    page.wait_for_url("**/admin")
    tabs = lambda: union(*page.locator("#tabs button[role=tab]").all())
    page.add_style_tag(content=".live-dot { animation: none !important; opacity: 1 !important; }")   # no half-faded dot

    def open_tab(name):
        page.click(f"#tabs button[data-tab={name}]")

    # ---------------------------------------------------------------- 1. overview, top
    expect(page.locator("#live")).to_have_class(re.compile("ok"))
    expect(page.locator(".tile.hero strong")).not_to_have_text("0")
    expect(page.locator(".chart svg")).to_be_visible()
    sh.settle()
    chart = box_of(page.locator(".chart-card"))
    sh.shot("adm-01-overview.png",
            {"x": 0, "y": 0, "width": 1366, "height": chart["y"] + chart["height"] + 3},
            [T(1, tabs(), pad=3, off=(-19, -7)),
             T(2, page.locator("#presets"), pad=3, off=(-15, -21)),
             T(3, page.locator("#live"), pad=6, off=(-40, -7)),
             T(4, page.locator(".kpis"), pad=1, off=(-16, -16)),
             T(5, page.locator(".chart-card"), pad=1, off=(-15, -15))],
            description="Overview tab, top: top bar with the Admin badge, tabs, date-range buttons with From/To, the five number tiles and the Calls by hour chart.",
            legend=["1 = tabs row", "2 = date-range buttons (Today ... Custom, From/To)", "3 = Live indicator (top right)",
                    "4 = number tiles", "5 = Calls by hour chart"])

    # ---------------------------------------------------------------- 2. overview, people and sources
    # The side column is ~1700 px tall (11 regions, 10 branches, 10 latest calls). To get all five cards
    # into one picture the three long lists are cut after their first rows (a "···" marks the cut).
    page.evaluate("""() => {
      const more = () => { const d = document.createElement('div'); d.className = 'shot-more'; d.setAttribute('data-shot', '1');
        d.style.cssText = 'text-align:center;color:#898781;letter-spacing:.35em;line-height:1;padding:2px 0 0'; d.textContent = '···'; return d; };
      const cut = (container, itemsSel, keep, host) => { const items = [...container.querySelectorAll(itemsSel)];
        items.slice(keep).forEach(e => { e.setAttribute('data-trimmed', '1'); e.style.display = 'none'; });
        container.after(more()); };
      const cards = document.querySelectorAll('.side-stack > .card');
      cut(cards[1].querySelector('.reason-rows'), ':scope > .rrow', 3);
      cut(cards[1].querySelector('.top-list'), ':scope > li', 3);
      cut(cards[2].querySelector('.feed'), ':scope > li', 3);
    }""")
    sh.settle()
    employees = page.locator(".two-col > .card").first
    reason, region_card, latest = (page.locator(".side-stack > .card").nth(i) for i in range(3))
    region_sec = union(region_card.locator(":scope > .card-head:not(.spaced)"), region_card.locator(":scope > .reason-rows"),
                       region_card.locator(":scope > .shot-more").nth(0))
    top_sec = union(region_card.locator(":scope > .card-head.spaced"), region_card.locator(":scope > .top-list"),
                    region_card.locator(":scope > .shot-more").nth(1), region_card.locator(":scope > p.hint"))
    e, l = box_of(employees), box_of(latest)
    top = e["y"] - 5                      # the cards are only ~6 px below the chart card: do not show a sliver of it
    sh.shot("adm-02-overview-people.png",
            {"x": 28, "y": top, "width": 1300, "height": l["y"] + l["height"] + 12 - top},
            [T(1, employees, pad=3, off=(-10, -3)),
             T(2, reason, pad=3, off=(-10, -3)),
             T(3, pad_box(region_sec, 9, 9, 9, 5), pad=0, off=(-20, -8)),
             T(4, pad_box(top_sec, 9, 9, 9, 5), pad=0, off=(-20, -8)),
             T(5, latest, pad=3, off=(-10, -3))],
            description="Overview tab, lower part: Employees table (click a row to see their calls) and, on the right, Calls by reason, Calls by region, Top branches and Latest calls (long lists shortened with ··· so all cards fit).",
            legend=["1 = Employees table", "2 = Calls by reason", "3 = Calls by region", "4 = Top branches", "5 = Latest calls"])
    page.evaluate("document.querySelectorAll('[data-trimmed]').forEach(e => e.style.display = '')")

    # ---------------------------------------------------------------- 3. records
    open_tab("records")
    expect(page.locator("#tab-records tbody tr").first).to_be_visible()
    sh.settle()
    rows = page.locator("#tab-records tbody tr")
    N = 7
    first_rows = [rows.nth(i) for i in range(N)]
    th_branch = page.locator("#tab-records thead th", has_text="Branch")
    head = page.locator("#tab-records .card-head")
    t0 = tabs()
    last = box_of(first_rows[-1])
    sh.shot("adm-03-records.png",
            {"x": 0, "y": t0["y"] - 1, "width": 1366, "height": last["y"] + last["height"] + 7 - (t0["y"] - 1)},
            [T(1, page.locator("#fUser"), pad=3, off=(-15, -15),
               extra=(page.locator("#fDept"), page.locator("#fRegion"), page.locator("#fReason"), page.locator("#fQuery"))),
             T(2, union(head.get_by_role("link", name="Excel"), head.get_by_role("link", name="CSV")), pad=4, off=(-17, -17)),
             T(3, union(th_branch, *[r.locator("td.branch-cell") for r in first_rows]), pad=3, off=(-15, -15)),
             T(4, union(*[r.locator("td.actions-col .actions") for r in first_rows]), pad=3, off=(-15, -15))],
            description="Records tab: filter row (employee, department, region, reason, search), Excel/CSV buttons, the first calls with the Branch column and Edit/delete buttons.",
            legend=["1 = filter row", "2 = Excel / CSV buttons", "3 = Branch column", "4 = Edit / delete buttons"])

    # ... then do the things the audit log should show: edit one call, delete another
    first = rows.first
    first.get_by_role("button", name=re.compile("^Edit call")).click()
    expect(page.locator("dialog")).to_be_visible()
    cur = page.eval_on_selector_all("dialog input[name=reason]", "els => els.findIndex(e => e.checked)")
    count = page.locator("dialog input[name=reason]").count()
    page.locator("dialog .radio-list label").nth((cur + 1) % count).click()
    page.fill("dialog textarea", "Reason corrected after review")
    page.click("dialog button[type=submit]")
    expect(page.locator("dialog")).to_have_count(0)
    page.wait_for_timeout(500)
    victim = page.locator("#tab-records tbody tr").nth(1)
    victim.get_by_role("button", name=re.compile("^Delete call")).click()
    expect(page.locator("dialog")).to_be_visible()
    page.click("dialog button[type=submit]")
    expect(page.locator("dialog")).to_have_count(0)
    page.wait_for_timeout(500)

    # ---------------------------------------------------------------- 4. employees
    open_tab("employees")
    expect(page.locator("#tab-employees tbody tr").first).to_be_visible()
    page.wait_for_timeout(300)
    sh.no_toast()
    sh.settle()
    emp_rows = page.locator("#tab-employees tbody tr")
    # the first row that has a Disable button (the admin's own row has none)
    target = next(emp_rows.nth(i) for i in range(emp_rows.count()) if emp_rows.nth(i).get_by_role("button", name="Disable").count())
    card = box_of(page.locator("#tab-employees .card"))
    t0 = tabs()
    sh.shot("adm-04-employees.png",
            {"x": 0, "y": t0["y"] - 1, "width": 1366, "height": card["y"] + card["height"] + 12 - (t0["y"] - 1)},
            [T(1, page.get_by_role("button", name="Add employee"), pad=3, off=(-6, -22)),
             T(2, page.get_by_role("button", name="Import from Excel / CSV"), pad=3, off=(-6, -22)),
             T(3, target.get_by_role("button", name="Reset password"), pad=3, off=(-6, -22)),
             T(4, target.get_by_role("button", name="Disable"), pad=3, off=(-6, -22))],
            description="Employees tab: Import from Excel / CSV and Add employee buttons and the accounts table with Edit / Reset password / Disable on every row.",
            legend=["1 = Add employee", "2 = Import from Excel / CSV", "3 = Reset password", "4 = Disable"])

    # ---------------------------------------------------------------- 5. add employee dialog
    page.get_by_role("button", name="Add employee").click()
    dlg = page.locator("dialog[open]")
    expect(dlg).to_be_visible()
    page.fill("dialog input[aria-label='User ID']", "240001")
    page.fill("dialog input[aria-label='Name']", "نورا حسن")
    page.fill("dialog input[aria-label='Department']", "Cards Support")
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.add_style_tag(content=DIALOG_BACKDROP_CSS)
    page.mouse.move(4, 797)
    page.wait_for_timeout(250)
    d = box_of(dlg)
    sh.shot("adm-05-add-employee.png", pad_box(d, 26, 26, 26, 30), host="dialog",
            description="The Add employee dialog filled in: User ID 240001, name, department Cards Support.")

    # ---------------------------------------------------------------- 6. temporary password
    page.click("dialog button[type=submit]")
    page.wait_for_selector(".cred")
    page.mouse.move(4, 797)
    page.wait_for_timeout(250)
    dlg = page.locator("dialog[open]")
    d = box_of(dlg)
    sh.shot("adm-06-temp-password.png", pad_box(d, 26, 26, 26, 30),
            [T(1, page.locator("dialog .cred").nth(1), pad=3, off=(-19, -4))], host="dialog",
            description="'Account created' dialog: User ID and the one-time Temporary password.",
            legend=["1 = the temporary password"])
    page.click("dialog button:has-text('Close')")
    expect(page.locator("dialog")).to_have_count(0)

    # ---------------------------------------------------------------- 7. import employees dialog
    page.get_by_role("button", name="Import from Excel / CSV").click()
    expect(page.locator("dialog[open]")).to_be_visible()
    page.mouse.move(4, 797)
    page.wait_for_timeout(250)
    d = box_of(page.locator("dialog[open]"))
    sh.shot("adm-07-import-employees.png", pad_box(d, 26, 26, 26, 30), host="dialog",
            description="The Import employees dialog before a file is chosen (columns user_id, name, department).")
    page.click("dialog button:has-text('Cancel')")
    expect(page.locator("dialog")).to_have_count(0)

    # ---------------------------------------------------------------- 8. branches
    open_tab("branches")
    expect(page.locator("#tab-branches h2")).to_contain_text("597 branches")
    expect(page.locator("#tab-branches tbody tr").first).to_be_visible()
    sh.settle()
    brows = page.locator("#tab-branches tbody tr")
    t0 = tabs()
    bb = box_of(brows.nth(7))
    sh.shot("adm-08-branches.png",
            {"x": 0, "y": t0["y"] - 1, "width": 1366, "height": bb["y"] + bb["height"] + 12 - (t0["y"] - 1)},
            [T(1, page.locator("#tab-branches input[type=search]"), pad=3, off=(-4, -21)),
             T(2, page.get_by_role("button", name="Import from Excel"), pad=3, off=(-4, -21))],
            description="Branches tab: heading with the branch/region count, search box, Import from Excel button and the branch table.",
            legend=["1 = search box", "2 = Import from Excel button"])

    # ---------------------------------------------------------------- 9. import branches dialog
    page.get_by_role("button", name="Import from Excel").click()
    expect(page.locator("dialog[open]")).to_be_visible()
    page.mouse.move(4, 797)
    page.wait_for_timeout(250)
    d = box_of(page.locator("dialog[open]"))
    sh.shot("adm-09-import-branches.png", pad_box(d, 26, 26, 26, 30), host="dialog",
            description="The 'Import the branch list' dialog before a file is chosen.")
    page.click("dialog button:has-text('Cancel')")
    expect(page.locator("dialog")).to_have_count(0)

    # ---------------------------------------------------------------- 10. audit log
    open_tab("audit")
    expect(page.locator("#tab-audit tbody tr").first).to_be_visible()
    page.wait_for_timeout(300)
    sh.settle()
    arows = page.locator("#tab-audit tbody tr")
    edited = arows.filter(has_text="Edited a call").first
    t0 = tabs()
    acard = box_of(page.locator("#tab-audit .card"))
    head = page.locator("#tab-audit .card-head")
    sh.shot("adm-10-audit.png",
            {"x": 0, "y": t0["y"] - 1, "width": 1366, "height": acard["y"] + acard["height"] + 12 - (t0["y"] - 1)},
            [T(1, page.locator("#aActor"), pad=3, off=(-15, -15), extra=(page.locator("#aAction"), page.locator("#aQuery"))),
             T(2, edited, pad=0, off=(-14, -2)),
             T(3, union(head.get_by_role("link", name="Excel"), head.get_by_role("link", name="CSV")), pad=4, off=(-17, -17))],
            description="Audit log tab after a few actions (sign-ins, a failed sign-in, an edited and a deleted call, a created account): filters, Excel/CSV buttons and the log rows.",
            legend=["1 = filters (user, action, search)", "2 = row of an edited call", "3 = Excel / CSV buttons"])

    # ---------------------------------------------------------------- 11. export
    open_tab("export")
    expect(page.locator("#tab-export .export-grid")).to_be_visible()
    sh.settle()
    t0 = tabs()
    grid = box_of(page.locator("#tab-export .export-grid"))
    sentence = page.locator("#tab-export > p").first
    sh.shot("adm-11-export.png",
            {"x": 0, "y": t0["y"] - 1, "width": 1366, "height": grid["y"] + grid["height"] + 18 - (t0["y"] - 1)},
            [T(1, text_box(sentence), pad=4, off=(-34, -7)),
             T(2, page.locator("#tab-export .export-grid .card").first.get_by_role("link", name="Excel"), pad=3, off=(-6, -22))],
            description="Export tab: the date-range sentence and the three cards (Full report, All calls, Audit log) with their Excel/CSV buttons.",
            legend=["1 = the date range the exports cover", "2 = Full report Excel button"])
    if problems:
        NOTES.append("browser problems while driving the admin pages: " + json.dumps(problems, ensure_ascii=False))


# ====================================================================== main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work-dir", help="scratch folder (default: a new temporary folder, removed at the end)")
    ap.add_argument("--data-dir", help="demo data folder; must not exist (default: <work-dir>/demo_data_adm)")
    ap.add_argument("--port", type=int, default=8098, help="port of the demo server (default 8098)")
    ap.add_argument("--console-port", type=int, default=8097, help="port used to capture the console text (default 8097)")
    ap.add_argument("--only", choices=("admin", "setup"), help="make only the adm-* or only the setup-* pictures")
    args = ap.parse_args()

    own_work = args.work_dir is None
    work = Path(args.work_dir or tempfile.mkdtemp(prefix="calllog-shots-")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    data = Path(args.data_dir) if args.data_dir else work / "demo_data_adm"
    base = f"http://127.0.0.1:{args.port}"
    for port in (args.port, args.console_port):
        if not port_is_free(port):
            sys.exit(f"port {port} is already in use - choose another with --port / --console-port")

    procs: list[subprocess.Popen] = []
    from playwright.sync_api import sync_playwright
    try:
        console_text = None
        if args.only != "admin":
            print("capturing the real console output of run.py ...")
            console_text = capture_console_text(args.console_port, work)
        if args.only != "setup":
            if data.exists():
                if data.parent != work:                                   # never delete something we did not make
                    sys.exit(f"{data} exists - give --data-dir a folder that does not exist yet")
                shutil.rmtree(data)
            print("seeding demo data ...")
            seed_demo_data(str(data))
            prog = start_program(["--port", str(args.port), "--no-browser"], data, work / "demo_server.log")
            procs.append(prog)
            wait_for_http(base + "/login", prog)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1366, "height": 800}, device_scale_factor=1, color_scheme="light",
                                      bypass_csp=True, locale="en-GB")
            # the admin page polls every 5 s and would redraw cards (and our cut lists) in the middle of a shot
            ctx.add_init_script("const _si = window.setInterval; window.setInterval = (f, ms, ...a) => ms === 5000 ? 0 : _si(f, ms, ...a);")
            page = ctx.new_page()
            sh = Shooter(ctx, page)
            if args.only != "setup":
                print("administrator pictures ...")
                run_admin_shots(sh, base, args.port)
            if args.only != "admin":
                print("setup pictures ...")
                svg = dict(PC=PC_ICON, PCBIG=PC_ICON_BIG, FOLDER=FOLDER_ICON, FILE=FILE_ICON, UNC=html.escape(UNC))
                sh.render_html("setup-01-architecture.png", fill(ARCH_HTML, **svg), 1100,
                               description="Architecture diagram: employees' browsers -> PC that runs run.py -> shared folder (program, libs, data).",
                               legend=[])
                sh.render_html("setup-02-folder-contents.png", fill(FOLDER_HTML, **svg), 760,
                               description="What the shared folder must contain after step 1 (run.py, bootstrap.py, manage.py, requirements.txt, config.example.json, README.md, calllog folder) and what is created on first start.",
                               legend=[])
                sh.render_html("setup-03-first-run-window.png", console_html(console_text), 880,
                               targets_fn=lambda pg: [T(1, pg.locator("#pw"), pad=0, off=(-36, -2)),
                                                      T(2, pg.locator("#addr"), pad=0, off=(-36, -2))],
                               description="The real first-run output of python run.py --share in a dark Command Prompt window (long scratch path replaced by the share path).",
                               legend=["1 = administrator password line", "2 = address line (Other PCs open)"])
            browser.close()
    finally:
        for pr in procs:
            stop_program(pr)
        if own_work:
            shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({"images": MANIFEST, "notes": NOTES}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
