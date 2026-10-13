"""Start the NBE Call Log - run it straight from the shared folder.

    python run.py             this PC only: opens your browser, nothing else to set up
    python run.py --share     also let other PCs open it:  http://<this-pc>:8080
    python run.py --service   for Windows Task Scheduler: keeps the server running, restarts it if it
                              stops, and restarts it when the program files are updated (see README)

Double-clicking run.py does the same as "python run.py".
The first start installs Flask etc. into the 'libs' folder next to this file (internet needed once).
"""
import bootstrap  # noqa: F401  (must come first: adds the 'libs' folder to the import path)

import argparse
import hashlib
import importlib
import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

REQUIREMENTS = bootstrap.HERE / "requirements.txt"


STAMP = bootstrap.LIBS / ".requirements.sha256"


def requirements_hash():
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def needs_install():
    if not all(importlib.util.find_spec(name) for name in ("flask", "waitress", "openpyxl")):
        return True
    try:  # packages were installed into 'libs' for an older requirements.txt
        return STAMP.read_text().strip() != requirements_hash()
    except OSError:
        return False


def ensure_dependencies():
    if not needs_install():
        return
    print("Installing the required packages (internet needed; first start or after an update)...")
    base = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
            "-r", str(REQUIREMENTS)]
    # First choice: the shared 'libs' folder, so other PCs need nothing but Python.
    # Fallback: this PC's own Python (e.g. when the share is read-only for this user).
    for extra in (["--target", str(bootstrap.LIBS), "--upgrade"], []):
        try:
            subprocess.check_call(base + extra)
            if extra:
                STAMP.write_text(requirements_hash())
            break
        except (subprocess.CalledProcessError, OSError):
            continue
    else:
        sys.exit("Could not install the packages automatically.\n"
                 "Check the internet connection, delete the 'libs' folder if it exists, and start again.")
    importlib.invalidate_caches()
    if bootstrap.LIBS.is_dir() and str(bootstrap.LIBS) not in sys.path:
        sys.path.insert(0, str(bootstrap.LIBS))


def pause():
    """Keep a double-clicked console window open so the message can be read."""
    if os.name == "nt":
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, RuntimeError, OSError):  # no console (e.g. started by Task Scheduler)
            pass


def lan_address():
    """The address colleagues type in their browser (best effort; no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


def port_is_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":   # like the server itself: a just-released port counts as free (never on Windows,
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # where it would report a busy port as free)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def choose_port(host, wanted, others_connect):
    if others_connect:                       # colleagues rely on a fixed address
        if not port_is_free(host, wanted):
            sys.exit(f"Port {wanted} is already in use on this PC. Close the other program, "
                     f"or choose another port with:  python run.py --share --port {wanted + 1}")
        return wanted
    for port in range(wanted, wanted + 20):  # personal use: any free port will do
        if port_is_free(host, port):
            return port
    sys.exit(f"No free port found between {wanted} and {wanted + 19}.")


# ------------------------------------------------------------------ service mode
WATCH_SECONDS = float(os.environ.get("CALLLOG_WATCH_SECONDS", "5"))
LAUNCHER_FILES = ("run.py", "bootstrap.py")
PROGRAM_FILES = LAUNCHER_FILES + ("manage.py", "config.json", "requirements.txt")


def fingerprint(names, folders=()):
    """(path, mtime, size) of the given files - changes whenever someone copies in a new version."""
    files = [bootstrap.HERE / n for n in names]
    for folder in folders:
        for root, dirs, found in os.walk(bootstrap.HERE / folder):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            files += [Path(root) / n for n in found]
    out = []
    for f in sorted(files):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append((str(f), st.st_mtime_ns, st.st_size))
    return tuple(out)


def program_fingerprint():
    return fingerprint(PROGRAM_FILES, ("calllog",))


def settled_fingerprint():
    """Wait until the files stop changing (a copy onto the share may still be in progress)."""
    previous = program_fingerprint()
    while True:
        time.sleep(max(WATCH_SECONDS / 2, 1))
        current = program_fingerprint()
        if current == previous:
            return current
        previous = current


def open_log():
    path = bootstrap.HERE / "server.log"
    try:
        if path.exists() and path.stat().st_size > 1_000_000:
            os.replace(path, path.with_name("server.log.1"))
        return open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return None  # no log file possible - carry on without


def stop(child):
    child.terminate()
    try:
        child.wait(10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def supervise(args):
    """Keep the server running; restart it if it stops or when the program files change."""
    log = open_log()

    def say(message):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}"
        print(line, flush=True)
        if log:
            log.write(line + "\n")

    child_cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--share", "--no-browser", "--hide-password"]
    if args.port:
        child_cmd += ["--port", str(args.port)]
    launcher = fingerprint(LAUNCHER_FILES)
    say("Service started: keeps the server running and restarts it when the program files change.")
    while True:
        seen = program_fingerprint()
        child = subprocess.Popen(child_cmd, stdout=log, stderr=subprocess.STDOUT if log else None)
        say(f"Server started (process {child.pid}).")
        reason = None
        try:
            while child.poll() is None:
                time.sleep(WATCH_SECONDS)
                if program_fingerprint() != seen:
                    settled_fingerprint()
                    reason = "the program files were updated"
                    break
            else:
                reason = f"the server stopped (exit code {child.returncode})"
        except KeyboardInterrupt:
            stop(child)
            say("Service stopped.")
            return
        if child.poll() is None:
            stop(child)
        say(f"Restarting because {reason}.")
        if fingerprint(LAUNCHER_FILES) != launcher:
            say("run.py itself was updated - exiting; Task Scheduler restarts it (see README).")
            sys.exit(3)
        if reason.startswith("the server stopped"):
            time.sleep(WATCH_SECONDS)   # e.g. the share is not reachable yet after a reboot: try again


def main():
    parser = argparse.ArgumentParser(description="NBE Call Log server")
    parser.add_argument("--share", action="store_true", help="let other PCs on the network open the site")
    parser.add_argument("--port", type=int, help="port number (default from config.json, 8080)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    parser.add_argument("--service", action="store_true",
                        help="for Task Scheduler: share with other PCs, keep running, restart on updates")
    parser.add_argument("--hide-password", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.service:
        return supervise(args)

    from waitress import serve
    from calllog import create_app
    from calllog.app import start_refresher
    from calllog.config import load_config
    from calllog.storage import StorageError

    cfg = load_config()
    host = "0.0.0.0" if args.share else cfg["host"]
    others_connect = host not in ("127.0.0.1", "localhost", "::1")
    port = choose_port(host, args.port or cfg["port"], others_connect)

    try:
        app = create_app()
    except StorageError as exc:
        sys.exit(f"\nERROR: {exc}\n")
    start_refresher(app)        # pages are answered from memory; this thread keeps it fresh (see calllog/app.py)

    first_password = app.config["INITIAL_ADMIN_PASSWORD"]
    local_url = f"http://localhost:{port}"
    print("=" * 66)
    print(" NBE Call Log is running")
    print(f"   Data folder : {cfg['data_dir']}")
    if others_connect:
        print(f"   Other PCs open:  http://{lan_address()}:{port}")
        print(f"   This PC opens :  {local_url}")
    else:
        print(f"   Open in a browser:  {local_url}   (this PC only)")
        print("   To let other PCs connect, start with:  python run.py --share")
    if first_password and args.hide_password:   # output goes to server.log - keep the password out of it
        print()
        print("   FIRST RUN - administrator account 'admin' was created, but its password is not")
        print("   written to the log. Set one with:  python manage.py reset-password admin")
    elif first_password:
        print()
        print("   FIRST RUN - administrator account created:")
        print("     User ID : admin")
        print(f"     Password: {first_password}")
        print("   You will be asked to choose a new password when you sign in.")
        print("   This password is shown only once - write it down now.")
    print()
    print(" Keep this window open while you work. Close it (or press Ctrl+C) to stop.")
    print("=" * 66)
    if not others_connect and not args.no_browser:
        threading.Timer(1.5, webbrowser.open, [local_url]).start()
    serve(app, host=host, port=port, threads=cfg["threads"], ident="calllog")


if __name__ == "__main__":
    try:
        ensure_dependencies()
        main()
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:
        if exc.code in (0, None):
            raise
        if isinstance(exc.code, str):
            print(exc.code)
        pause()
        sys.exit(exc.code if isinstance(exc.code, int) else 1)
    except Exception:
        traceback.print_exc()
        pause()
        sys.exit(1)
