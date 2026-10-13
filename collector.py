"""NBE Operations Hub - the collector. Runs on the Windows PC that has Outlook and Chrome (or Edge) and feeds the shared folder.

    python collector.py                    run every enabled job in this window (Ctrl+C stops it)
    python collector.py --jobs sla,itsm    only these jobs (email, email_report, sla, itsm)
    python collector.py --once sla         run one job now, print the result, exit (0 = good, 1 = failed)
                                           add --dry-run (write/send nothing) --no-email --debug --headed --force --send-mail
    python collector.py --check            what is configured, what is missing (never prints a password)
    python collector.py --set-password sla|itsm     ask for the password (hidden) and keep it in Windows Credential Manager

    python collector.py --start            run in the background (no window), restarted automatically if it ever stops
    python collector.py --stop | --status
    python collector.py --install-autostart | --remove-autostart       start it in the background at every Windows log-in
    python collector.py --service          the same supervisor in this window (for Task Scheduler)

Users, passwords and the Outlook archive name are written in collector_config.json next to this file (see
collector_config.example.json and docs/HUB-DESIGN.md, section 10). Settings changed on the web page apply without a restart.
The first start installs what the collector needs (requirements-collector.txt) and restarts itself once - internet needed.
Outlook and the browser need a Windows session that stays logged in (a locked screen is fine).
"""
import bootstrap  # noqa: F401  (must come first: adds the 'libs' folder to the import path)

import argparse
import atexit
import getpass
import importlib
import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

HERE = bootstrap.HERE
SCRIPT = Path(__file__).resolve()
REQUIREMENTS = HERE / "requirements-collector.txt"
OPTIONAL_PACKAGES = (("matplotlib", "matplotlib>=3.7"),)      # charts in the e-mail report; the report works without them
JOB_NAMES = ("email", "email_report", "sla", "itsm")             # same as calllog.ops.schemas.JOBS (a test keeps them equal)
ACCOUNTS = {"sla": "SLA portal", "itsm": "ITSM"}
PACKAGES = [("flask", "Flask"), ("openpyxl", "openpyxl"), ("keyring", "keyring")] + (
    [("win32com", "pywin32")] if os.name == "nt" else [])

RESTART_FLAG = "NBE_COLLECTOR_RESTARTED"       # set after the one-time install, so a failed install cannot loop
SUPERVISED_FLAG = "NBE_COLLECTOR_SUPERVISED"   # set for the runner started by --service (the supervisor owns the PC-wide lock)
SINGLE_INSTANCE_PORT = 47831                   # held on 127.0.0.1 while a collector runs on this PC
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
WATCH_SECONDS = float(os.environ.get("CALLLOG_WATCH_SECONDS", "30"))
STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
AUTOSTART_LNK = "NBE_Operations_Collector.lnk"
BROWSER_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
BROWSER_COMMANDS = ("google-chrome", "chrome", "chromium", "chromium-browser", "msedge", "microsoft-edge")

_instance_socket = None


def say(text=""):
    print(text, flush=True)


def fix_console():
    """pythonw has no console (sys.stdout is None) and Arabic text must never crash a print."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            continue
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def pause():
    """Keep a double-clicked console window open so the message can be read."""
    if os.name == "nt":
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, RuntimeError, OSError):
            pass


# ----------------------------------------------------------------------------------------- command line
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="NBE Operations Hub collector", epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", help="only these jobs, comma separated (email, email_report, sla, itsm)")
    ap.add_argument("--once", metavar="JOB", choices=JOB_NAMES, help="run one job now, print the result and exit")
    ap.add_argument("--no-email", action="store_true", help="do not send e-mail")
    ap.add_argument("--dry-run", action="store_true", help="scan, but write and send nothing")
    ap.add_argument("--debug", action="store_true", help="save screenshots / page structure of every step")
    ap.add_argument("--headed", action="store_true", help="show the browser window (SLA / ITSM)")
    ap.add_argument("--force", action="store_true", help="with --once: run even if the job decides there is nothing to do")
    ap.add_argument("--send-mail", action="store_true", help="with --once: send the e-mail even if nothing new")
    ap.add_argument("--set-password", choices=sorted(ACCOUNTS), metavar="sla|itsm",
                    help="save the portal / ITSM password in Windows Credential Manager")
    ap.add_argument("--check", action="store_true", help="show what is configured and what is missing")
    ap.add_argument("--start", action="store_true", help="run in the background (no window)")
    ap.add_argument("--stop", action="store_true", help="stop the background collector")
    ap.add_argument("--status", action="store_true", help="is it running? auto-start? what do the jobs say?")
    ap.add_argument("--install-autostart", action="store_true", help="start in the background at every Windows log-in")
    ap.add_argument("--remove-autostart", action="store_true", help="undo --install-autostart")
    ap.add_argument("--service", action="store_true", help="supervisor: keep the collector running, restart it if it stops")
    ap.add_argument("--config", metavar="PATH", help="collector_config.json to use (default: next to collector.py)")
    ap.add_argument("--data-dir", metavar="PATH", help="the shared data folder (default: from the config files)")
    ap.add_argument("--no-isolate", action="store_true", help="run jobs inside this process instead of one process per run")
    ap.add_argument("--run-job", metavar="JOB", choices=JOB_NAMES, help=argparse.SUPPRESS)
    ap.add_argument("--job-spec", help=argparse.SUPPRESS)
    ap.add_argument("--job-result", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    args.job_list = parse_jobs(ap, args.jobs)
    return args


def parse_jobs(ap, text):
    if not text:
        return list(JOB_NAMES)
    jobs = [j.strip() for j in text.split(",") if j.strip()]
    bad = [j for j in jobs if j not in JOB_NAMES]
    if bad or not jobs:
        ap.error(f"--jobs: unknown job {', '.join(bad) or '(none)'}; choose from {', '.join(JOB_NAMES)}")
    return jobs


def passthrough(args):
    """The options a background copy needs (not the one-shot ones)."""
    out = []
    if args.config:
        out += ["--config", str(Path(args.config).resolve())]
    if args.data_dir:
        out += ["--data-dir", str(Path(args.data_dir).resolve())]
    if args.jobs:
        out += ["--jobs", args.jobs]
    out += [flag for flag, on in (("--no-email", args.no_email), ("--debug", args.debug), ("--no-isolate", args.no_isolate)) if on]
    return out


# ------------------------------------------------------------------------------------- packages (first run)
def missing_packages():
    missing = []
    for module, pip_name in PACKAGES:
        try:
            if module == "win32com":                    # installed but unusable (no DLLs) must count as missing
                importlib.import_module("win32com.client")
            elif importlib.util.find_spec(module) is None:
                missing.append(pip_name)
        except Exception:
            missing.append(pip_name)
    return missing


def run_pip(*pip_args):
    return subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *pip_args],
                           creationflags=NO_WINDOW)


def ensure_packages():
    """Install what is missing (first run only) and start again in a fresh Python. None = nothing to do, else an exit code."""
    missing = missing_packages()
    if not missing:
        return None
    if os.environ.get(RESTART_FLAG):
        say(f"ERROR: still missing after the install: {', '.join(missing)}. Run  {sys.executable} -m pip install -r "
            f"\"{REQUIREMENTS}\"  and read the messages.")
        return 1
    say(f"First run: installing the Python packages the collector needs ({', '.join(missing)}) - one time, internet needed...")
    if run_pip("-r", str(REQUIREMENTS)) != 0:
        say("ERROR: the install failed - see the messages above. If the internet is blocked, ask IT for the proxy address and run:")
        say(f'  "{sys.executable}" -m pip install --proxy http://PROXY:PORT -r "{REQUIREMENTS}"')
        return 1
    for module, spec in OPTIONAL_PACKAGES:
        if importlib.util.find_spec(module) is None and run_pip(spec) != 0:
            say(f"Note: {spec} could not be installed - the e-mail report will be sent without charts.")
    say("Packages installed - starting again.")
    return restart_self()


def restart_self():
    """New packages such as pywin32 load only in a fresh Python: run this command again, once, and return its exit code."""
    env = dict(os.environ)
    env[RESTART_FLAG] = "1"
    return subprocess.call([sys.executable, str(SCRIPT)] + sys.argv[1:], env=env)


# --------------------------------------------------------------------------------------------- context
def load_context(args):
    """(collector config, data folder, OpsStore). Raises CollectorConfigError with a readable message."""
    from calllog.ops import collector_config
    from calllog.ops.store import OpsStore
    cc = collector_config.load(args.config)
    data_dir = str(Path(args.data_dir).resolve()) if args.data_dir else cc.data_dir()
    return cc, data_dir, OpsStore(data_dir)


def effective(store, cc):
    from calllog.ops import runner
    settings, _ = runner.effective_settings(store, cc)
    return settings


def find_browser(settings):
    wanted = [str(settings.get(k, {}).get("browser_executable") or "") for k in ("sla", "itsm")]
    for path in [w for w in wanted if w] + list(BROWSER_PATHS):
        if os.path.isfile(path):
            return path
    for name in BROWSER_COMMANDS:
        found = shutil.which(name)
        if found:
            return found
    return None


# --------------------------------------------------------------------------------------------- --check
def cmd_check(args):
    problems = []

    def line(label, text, bad=False):
        say(f"  {label:<20}{text}")
        if bad:
            problems.append(label)

    say("NBE Operations Hub - collector check")
    say()
    missing = missing_packages()
    for module, pip_name in PACKAGES:
        line(f"package {module}", "MISSING (run python collector.py once to install)" if pip_name in missing else "ok", pip_name in missing)
    if importlib.util.find_spec("matplotlib") is None:
        line("package matplotlib", "not installed (optional: the report is sent without charts)")
    if "Flask" in missing:
        say()
        say("The shared program code needs Flask, which is not installed yet - the rest cannot be checked.")
        say("Run  python collector.py  once in a window (internet needed), then  python collector.py --check.")
        return 1

    from calllog.ops import collector_config, runner
    try:
        cc, data_dir, store = load_context(args)
    except collector_config.CollectorConfigError as exc:
        line("collector_config", f"PROBLEM - {exc}", True)
        return 1
    where = cc.path if cc.path else "(none)"
    if cc.exists:
        line("collector_config", f"found: {where}")
    else:
        line("collector_config", f"NOT FOUND ({where}) - copy collector_config.example.json to collector_config.json and fill it in", True)
    folder = Path(data_dir)
    if not folder.is_dir():
        line("data folder", f"NOT FOUND: {folder} - is the shared folder reachable? (data_dir in collector_config.json / config.json)", True)
    else:
        from calllog.ops import health
        probe = health.probe_folder(folder)
        line("data folder", f"{folder} - writable: {'yes' if probe['writable'] else 'NO (' + str(probe['error']) + ')'}", not probe["writable"])
    settings = effective(store, cc)

    if os.name == "nt":
        try:
            importlib.import_module("win32com.client")
            line("Outlook (pywin32)", "importable: yes (Outlook itself is opened only when a job runs)")
        except Exception as exc:
            line("Outlook (pywin32)", f"importable: NO ({type(exc).__name__})", True)
    else:
        line("Outlook (pywin32)", "not Windows - the e-mail jobs and every e-mail send need a Windows PC with Outlook")
    archive = str(settings["email"].get("target_archive_name") or "")
    line("Outlook archive", archive if archive else "not set - the default mailbox is scanned (set email.target_archive_name)")
    browser = find_browser(settings)
    line("Chrome / Edge", browser if browser else "NOT FOUND - install Microsoft Edge or Google Chrome, or set sla.browser_executable / itsm.browser_executable", not browser)

    for kind, label in ACCOUNTS.items():
        username = str(settings[kind].get("username") or "")
        source = runner.password_source(cc, kind, username) if username else None
        text = f"user: {username or 'NOT SET'}   password: " + (f"yes ({source})" if source else "NO")
        if not source and username:
            text += f" - run  python collector.py --set-password {kind}"
        line(f"{label} account", text, not username or not source)

    email_ = settings["email"]
    line("e-mail settings", f"{len(email_['business_emails'])} business mailbox(es), {len(email_['team_members'])} team member(s), "
                           f"teams: {len(email_['teams'])}", not (email_["business_emails"] and email_["team_members"]))
    report = email_["report"]
    line("recipients", f"report to {len(report['to'])} / cc {len(report['cc'])}; SLA mail to {len(settings['sla']['mail']['to'])}; "
                       f"ITSM mail to {len(settings['itsm']['mail']['to'])} (empty = your own address)")
    pinned = cc.pinned_keys()
    line("pinned settings", f"{len(pinned)} key(s) in collector_config.json override the web page")
    say()
    say("Everything needed is in place." if not problems else f"{len(problems)} thing(s) to fix: {', '.join(problems)}")
    return 1 if problems else 0


# ----------------------------------------------------------------------------------------- --set-password
def cmd_set_password(args):
    from calllog.ops.engines import common
    cc, data_dir, store = load_context(args)
    kind = args.set_password
    username = str(effective(store, cc)[kind].get("username") or "").strip()
    if not username:
        say(f"No user name for the {ACCOUNTS[kind]} account yet. Write it in collector_config.json:  \"{kind}\": {{\"username\": \"...\"}}")
        return 1
    password = getpass.getpass(f"{ACCOUNTS[kind]} password for {username}: ")
    if not password or password != getpass.getpass("Type it again: "):
        say("The two entries are empty or different - nothing was saved.")
        return 1
    try:
        common.set_password(kind, username, password)
    except Exception as exc:
        say(f"The password could not be saved ({type(exc).__name__}): {exc}")
        return 1
    say(f"Password saved in Windows Credential Manager for {username}.")
    if cc.file_password(kind) or os.environ.get(common.PASSWORD_ENV[kind]):
        say("Note: a password is also written in collector_config.json / an environment variable - that one is used first.")
    return 0


# --------------------------------------------------------------------------------- single instance, pid file
def monitor_running():
    """True while some collector on this PC holds the single-instance port."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def claim_instance():
    global _instance_socket
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        held.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        held.close()
        return False
    _instance_socket = held
    return True


def pid_path(store):
    return store.state_dir / f"collector-{socket.gethostname()}.pid"


def write_pid(store):
    try:
        store.state_dir.mkdir(parents=True, exist_ok=True)
        pid_path(store).write_text(str(os.getpid()), encoding="ascii")
        atexit.register(remove_pid, store, os.getpid())
    except OSError:
        pass


def read_pid(store):
    try:
        return int(pid_path(store).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def remove_pid(store, only_if=None):
    if only_if is not None and read_pid(store) != only_if:
        return
    try:
        pid_path(store).unlink()
    except OSError:
        pass


def process_name(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return "python"
        except OSError:
            return None
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
    except Exception:
        return None
    start = (out or "").strip().strip('"')
    return start.split('"')[0].lower() if start else None


def pythonw_exe():
    """pythonw.exe runs Python without a console window."""
    if os.name == "nt":
        candidate = Path(sys.executable).with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return sys.executable


# ---------------------------------------------------------------------------------------------- foreground run
def cmd_once(args):
    from calllog.ops import runner
    cc, data_dir, store = load_context(args)
    if not args.dry_run:
        store.init()
    options = {k: True for k, on in (("force", args.force), ("send_mail", args.send_mail)) if on}
    engine = runner.Runner(store, cc, jobs=[args.once], isolate=False, dry_run=args.dry_run, no_email=args.no_email,
                           debug=args.debug, headed=args.headed, console=say)
    status, result = engine.run_once(args.once, options)
    if status == "busy":
        say(f"The {args.once} job is already running (another collector holds its lock). Try again when it has finished.")
        return 1
    ok = result.ok or result.skipped
    word = "SKIPPED" if result.skipped else "OK" if result.ok else "FAILED"
    say()
    say(f"{args.once}: {word}" + (f" - {result.summary}" if result.summary else ""))
    if result.error:
        say(f"  error: {result.error}")
    if result.emails_sent:
        say(f"  e-mails sent: {result.emails_sent}")
    say("  (dry run: nothing was written or sent)" if args.dry_run else
        f"  data folder: {data_dir}" + ("  - snapshot written" if result.ok and result.snapshot_data is not None else ""))
    return 0 if ok else 1


def account_warnings(settings, cc):
    from calllog.ops import runner
    out = []
    for kind, label in ACCOUNTS.items():
        if not settings[kind].get("enabled", True):
            continue
        username = str(settings[kind].get("username") or "")
        if not username:
            out.append(f"{label}: no user name (collector_config.json) - the {kind} job will report an error")
        elif not runner.password_source(cc, kind, username):
            out.append(f"{label}: no password for {username} - run  python collector.py --set-password {kind}")
    return out


def on_stop_signal(handler):
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), handler)
            except (ValueError, OSError):
                pass


def cmd_run(args):
    from calllog.ops import runner
    cc, data_dir, store = load_context(args)
    store.init()
    supervised = bool(os.environ.get(SUPERVISED_FLAG))
    if not supervised:
        if not claim_instance():
            say("A collector is already running on this PC. Use  python collector.py --stop  first, or --status to see it.")
            return 0
        write_pid(store)
    engine = runner.Runner(store, cc, jobs=args.job_list, isolate=not args.no_isolate, dry_run=args.dry_run, no_email=args.no_email,
                           debug=args.debug, headed=args.headed, console=None if supervised else say)
    settings = engine.reload()
    if not supervised:
        say("=" * 66)
        say(" NBE Operations Hub collector")
        say(f"   Jobs        : {', '.join(args.job_list)}" + ("  (each run in its own process)" if engine.isolate else ""))
        say(f"   Data folder : {data_dir}")
        say(f"   Config file : {cc.path if cc.exists else 'none found - using the saved settings only'}")
        for warning in account_warnings(settings, cc):
            say(f"   WARNING     : {warning}")
        say(" Keep this window open (or use --start). Press Ctrl+C to stop.")
        say("=" * 66)
    on_stop_signal(lambda *_: engine.request_stop())
    engine.start()
    try:
        engine.wait()
    except KeyboardInterrupt:
        pass
    if not supervised:
        say("Stopping - waiting for running jobs...")
    engine.stop()
    return 0


def cmd_run_job(args):
    from calllog.ops import runner
    if not (args.job_spec and args.job_result):
        say("--run-job is used by the collector itself (it needs --job-spec and --job-result).")
        return 2
    return runner.run_child(args.run_job, args.job_spec, args.job_result)


# --------------------------------------------------------------------------- background run, supervisor, autostart
def cmd_start(args):
    cc, data_dir, store = load_context(args)
    if monitor_running():
        pid = read_pid(store)
        say(f"The collector is already running{f' (pid {pid})' if pid else ''}.")
        return 0
    for warning in account_warnings(effective(store, cc), cc):
        say(f"WARNING: {warning}")
    kw = {"cwd": str(HERE), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([pythonw_exe(), str(SCRIPT), "--service"] + passthrough(args), **kw)
    for _ in range(60):
        if monitor_running():
            time.sleep(0.5)
            say(f"The collector started in the background (pid {read_pid(store)}).")
            say(f"Check on it with  python collector.py --status   Stop it with  python collector.py --stop")
            return 0
        time.sleep(0.25)
    say(f"The collector did not start - see {store.logs_dir / 'collector-console.log'}")
    return 1


def cmd_stop(args):
    cc, data_dir, store = load_context(args)
    pid = read_pid(store)
    if not monitor_running():
        remove_pid(store)
        say("The collector is not running.")
        return 0
    name = process_name(pid) if pid else None
    if not name or not name.startswith("python"):
        say("The collector is running but its process id is unknown. End the python / pythonw process in Task Manager.")
        return 1
    if os.name == "nt":                                   # /T also ends the job processes (Chrome / Edge and their helpers)
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        os.kill(pid, signal.SIGTERM)
    for _ in range(80):
        if not monitor_running():
            break
        time.sleep(0.25)
    else:
        if os.name != "nt":
            os.kill(pid, signal.SIGKILL)
    remove_pid(store)
    say(f"The collector was stopped (pid {pid}).")
    return 0


def autostart_file():
    return STARTUP_DIR / AUTOSTART_LNK


def cmd_status(args):
    from calllog.ops import health
    cc, data_dir, store = load_context(args)
    pid = read_pid(store)
    running = monitor_running()
    say("Collector:  " + ((f"running (pid {pid})" if pid else "running") if running else "not running"))
    say("Auto-start: " + (f"on - {autostart_file()}" if autostart_file().exists() else "off"))
    settings = effective(store, cc)
    now = datetime.now()
    say(f"Data folder: {data_dir}")
    say("Jobs (from the heartbeat files):")
    states = health.job_states(store, settings, now)
    for job in JOB_NAMES:
        s = states[job]
        if not s["present"]:
            say(f"  {job:<13}no heartbeat yet")
            continue
        say(f"  {job:<13}{'online' if s['online'] else 'OFFLINE':<8}last: {s['last_status'] or '-':<8}ok at {s['last_ok_at'] or '-'}   "
            f"next {s['next_run_at'] or '-'}   {s['last_summary'] or ''}"[:200])
        if s["last_error"]:
            say(f"               error: {s['last_error'][:160]}")
    log_path = store.logs_dir / "collector.log"
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]
        say(f"Last lines of {log_path.name}:")
        for text in lines:
            say("  " + text)
    return 0


def make_shortcut(lnk, target, arguments, workdir, description):
    """Startup-folder shortcut: with pywin32, or - if it is not available yet - with PowerShell."""
    lnk.parent.mkdir(parents=True, exist_ok=True)
    try:
        import win32com.client
        shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(lnk))
        shortcut.TargetPath, shortcut.Arguments = target, arguments
        shortcut.WorkingDirectory, shortcut.Description = workdir, description
        shortcut.Save()
        return lnk.exists()
    except Exception:
        quote = lambda text: str(text).replace("'", "''")
        script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{quote(lnk)}');$s.TargetPath='{quote(target)}';"
                  f"$s.Arguments='{quote(arguments)}';$s.WorkingDirectory='{quote(workdir)}';$s.Description='{quote(description)}';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, creationflags=NO_WINDOW)
        return lnk.exists()


def service_arguments(args):
    return " ".join([f'"{SCRIPT}"', "--service"] + [f'"{a}"' if " " in a else a for a in passthrough(args)])


def cmd_install_autostart(args):
    if os.name != "nt":
        say("Auto-start is only available on Windows.")
        return 1
    if make_shortcut(autostart_file(), pythonw_exe(), service_arguments(args), str(HERE), "NBE Operations Hub collector"):
        say(f"Auto-start installed: {autostart_file()}")
        say("The collector starts in the background at your next Windows log-in. To start it now:  python collector.py --start")
        return 0
    say("The shortcut could not be created. Run  python collector.py  once in a window first (it installs pywin32).")
    return 1


def cmd_remove_autostart(args):
    try:
        autostart_file().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        say(f"Could not remove {autostart_file()}: {exc}")
        return 1
    say("Auto-start removed.")
    return 0


def program_fingerprint():
    """(path, mtime, size) of the program files - changes when someone copies a new version onto the share."""
    files = [HERE / n for n in ("collector.py", "bootstrap.py", "requirements-collector.txt")]
    for root, dirs, names in os.walk(HERE / "calllog"):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        files += [Path(root) / n for n in names if n.endswith((".py", ".json"))]
    out = []
    for f in sorted(files):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append((str(f), st.st_mtime_ns, st.st_size))
    return tuple(out)


def stop_child(child):
    """Graceful where Windows allows it (SIGTERM elsewhere); after 30 s - or at once on Windows - the whole process tree is killed."""
    from calllog.ops import runner
    if os.name != "nt":
        child.terminate()
        try:
            child.wait(30)
            return
        except subprocess.TimeoutExpired:
            pass
    runner.kill_process_tree(child.pid)
    child.wait()


def cmd_service(args):
    """Keep the collector running for months: restart it when it stops or when the program files are updated."""
    from calllog.ops import runner
    cc, data_dir, store = load_context(args)
    store.init()
    if not claim_instance():
        say("A collector is already running on this PC.")
        return 0
    write_pid(store)
    console = store.logs_dir / "collector-console.log"

    def note(text):
        runner.append_collector_log(store, f"[supervisor] {text}")
        say(text)

    def open_console():
        try:
            if console.exists() and console.stat().st_size > 1_000_000:
                os.replace(console, console.with_suffix(".log.1"))
            return open(console, "ab", buffering=0)
        except OSError:
            return None

    cmd = [sys.executable, "-u", str(SCRIPT)] + passthrough(args)
    env = dict(os.environ, **{SUPERVISED_FLAG: "1"})
    stopping = []
    on_stop_signal(lambda *_: stopping.append(1))
    delay = 10
    note("Supervisor started: keeps the collector running and restarts it when the program files change.")
    while not stopping:
        seen, began = program_fingerprint(), time.monotonic()
        out = open_console()
        child = subprocess.Popen(cmd, env=env, cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=out or subprocess.DEVNULL,
                                 stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        note(f"Collector started (process {child.pid}).")
        reason, checked = None, time.monotonic()
        try:
            while child.poll() is None and not stopping:
                time.sleep(1)
                if time.monotonic() - checked >= WATCH_SECONDS:
                    checked = time.monotonic()
                    if program_fingerprint() != seen:
                        reason = "the program files were updated"
                        time.sleep(WATCH_SECONDS)             # a copy onto the share may still be running
                        break
        except KeyboardInterrupt:
            stopping.append(1)
        reason = reason or ("stop requested" if stopping else f"the collector stopped (exit code {child.returncode})")
        if child.poll() is None:
            stop_child(child)
        if out:
            out.close()
        if stopping:
            break
        note(f"Restarting because {reason}.")
        delay = 10 if time.monotonic() - began > 300 or reason.startswith("the program") else min(delay * 2, 120)
        time.sleep(delay)
    note("Supervisor stopped.")
    return 0


# --------------------------------------------------------------------------------------------------- main
def main(argv=None):
    fix_console()
    args = parse_args(argv)
    if args.run_job:
        return cmd_run_job(args)
    if args.remove_autostart:
        return cmd_remove_autostart(args)
    if args.check:
        return cmd_check(args)
    needs_packages = args.once or args.set_password or args.start or args.install_autostart or not (args.stop or args.status or args.service)
    if needs_packages:
        code = ensure_packages()
        if code is not None:
            return code
    if "Flask" in missing_packages():
        say("The collector's shared code needs Flask, which is not installed yet. Run  python collector.py  once in a window.")
        return 1
    from calllog.ops import collector_config
    from calllog.storage import StorageError
    try:
        if args.stop:
            return cmd_stop(args)
        if args.status:
            return cmd_status(args)
        if args.set_password:
            return cmd_set_password(args)
        if args.install_autostart:
            return cmd_install_autostart(args)
        if args.start:
            return cmd_start(args)
        if args.service:
            return cmd_service(args)
        if args.once:
            return cmd_once(args)
        return cmd_run(args)
    except collector_config.CollectorConfigError as exc:
        say(f"ERROR: {exc}")
        return 1
    except StorageError as exc:
        say(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit as exc:
        if exc.code not in (0, None) and isinstance(exc.code, str):
            print(exc.code)
            pause()
            sys.exit(1)
        raise
    except Exception:
        traceback.print_exc()
        pause()
        sys.exit(1)
