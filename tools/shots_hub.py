"""Screenshots of every page of the program on demo data - to LOOK at the design, and for the guides.

    python tools/shots_hub.py                       # all pages, light theme, 1440 x 900, into ./shots
    python tools/shots_hub.py --out DIR --theme dark --width 1280 --height 800
    python tools/shots_hub.py --only my email --role employee manager

It seeds a temporary data folder with the demo data (made-up people and mails), starts the real server on a free port,
signs in as an administrator, a manager and an employee with the demo password and saves one PNG per page and role into
``DIR/<theme>-<role>-<page>.png``. Needs playwright and a Chromium (CHROMIUM_PATH, default /opt/pw-browsers/chromium).
Prints any console error or failed request it saw, so a page with a JavaScript problem cannot slip through.
"""
import argparse
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGES = {                                     # name -> (path, role, selector that means "the page has finished drawing")
    "admin": [("login", "/login", None), ("hub", "/hub", "#hub-page, main"), ("calls", "/admin", "main"),
              ("email", "/email", "#email-page .op-kpi, #email-page"), ("sla", "/sla", "main"), ("itsm", "/itsm", "main"),
              ("system", "/system", "main"), ("my-as", "/my?as=1001", "main")],
    "manager": [("hub", "/hub", "main"), ("email", "/email", "#email-page .op-kpi, #email-page"), ("sla", "/sla", "main"),
                ("itsm", "/itsm", "main")],
    "employee": [("my", "/my", "main"), ("log-calls", "/dashboard", "main")],
}


def start_server(data_dir):
    from waitress import create_server
    from calllog import create_app, security
    security.PBKDF2_ITERATIONS = 1000
    app = create_app({"data_dir": data_dir, "secret_key": "shots", "user_edit_days": None})
    server = create_server(app, host="127.0.0.1", port=0, threads=8)
    threading.Thread(target=server.run, daemon=True).start()
    return app, server, server.effective_port


def seed(app):
    from calllog import security
    from calllog.ops import demo
    ops, calls = app.extensions["ops"], app.extensions["store"]
    demo.seed(ops)
    info = demo.seed_users_and_calls(calls)
    calls.update_user("admin", {"password_hash": security.hash_password(info["password"]), "must_change_password": False})
    return info


def sign_in(page, base, uid, password):
    page.goto(base + "/login")
    page.fill('input[name="user_id"]', uid)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="shots")
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--full", action="store_true", help="whole page height, not just the first screen")
    parser.add_argument("--role", nargs="*", choices=tuple(PAGES), default=list(PAGES))
    parser.add_argument("--only", nargs="*", default=[], help="page names to take (default: all)")
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    folder = tempfile.mkdtemp(prefix="hub-shots-")
    app, server, port = start_server(folder)
    base = f"http://127.0.0.1:{port}"
    info = seed(app)
    logins = {"admin": ("admin", info["password"]), "manager": (info["manager"], info["password"]),
              "employee": (info["employees"][0], info["password"])}
    problems = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium"),
                                         args=["--no-sandbox"])
            for role in args.role:
                ctx = browser.new_context(viewport={"width": args.width, "height": args.height}, color_scheme=args.theme,
                                          locale="en-GB")
                ctx.add_init_script(f"try{{localStorage.setItem('nbe-theme','{args.theme}')}}catch(e){{}}")
                page = ctx.new_page()
                page.on("console", lambda m, role=role: problems.append(f"[{role}] console {m.type}: {m.text}")
                        if m.type in ("error", "warning") else None)
                page.on("pageerror", lambda e, role=role: problems.append(f"[{role}] page error: {e}"))
                page.on("requestfailed", lambda r, role=role: problems.append(f"[{role}] request failed: {r.url}"))
                uid, password = logins[role]
                for name, path, ready in PAGES[role]:
                    if args.only and name not in args.only:
                        continue
                    if name == "login":
                        page.goto(base + path)
                    else:
                        if "/login" in page.url or page.url == "about:blank":
                            sign_in(page, base, uid, password)
                        page.goto(base + path)
                    if ready:
                        try:
                            page.wait_for_selector(ready, timeout=8000)
                        except Exception:
                            problems.append(f"[{role}] {name}: '{ready}' never appeared")
                    page.wait_for_load_state("networkidle")
                    time.sleep(0.4)
                    target = out / f"{args.theme}-{role}-{name}.png"
                    page.screenshot(path=str(target), full_page=args.full)
                    print("saved", target)
                ctx.close()
            browser.close()
    finally:
        server.close()
        shutil.rmtree(folder, ignore_errors=True)
    if problems:
        print("\nPROBLEMS SEEN:")
        for p in dict.fromkeys(problems):
            print("  -", p)
        return 1
    print("\nno console errors, no failed requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
