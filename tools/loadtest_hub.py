"""Load test for the NBE Operations Hub: 60 people on the web app at the same time.

    python tools/loadtest_hub.py                      # 60 users for 60 seconds on made-up data (about a minute to set up)
    python tools/loadtest_hub.py --seconds 120 --users 80 --delay-ms 10
    python tools/loadtest_hub.py --keep               # keep the temporary data folder for a look afterwards

What it does (everything in a temporary folder - your real data is never touched):

1. fills a data folder with demo data scaled up to a busy day: about 500 e-mails, 300 SLA requests and 142,000 calls;
2. starts the real web server (waitress, same number of threads as the program) on a free local port;
3. adds a small delay to every file read/write inside the program (default 5 ms) to imitate the shared folder;
4. starts the users: 5 of 6 are employees (the "My work" page), 1 of 6 are admins/managers (Hub, E-mail, SLA, ITSM);
   each page asks for fresh data on its own interval (employees 30 s, admins 15 s - shortened by --interval-scale so a
   short test is heavier than real life), exactly as the pages do (``?since=`` / ``rev`` -> "unchanged");
5. replaces the e-mail and SLA snapshots every 20 seconds, as a collector does, so the "new data" path is measured too;
6. prints the response times and checks them against the targets in docs/HUB-DESIGN.md section 14. Exit code 1 = missed.

No password of any real account is used: the accounts are made up and live only in the temporary folder.
"""
import argparse
import copy
import http.client
import json
import random
import re
import shutil
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# (name, p95 target in ms)
TARGETS = {"email unchanged": 5.0, "email full": 150.0, "my": 100.0, "shared": 20.0}
PASSWORD = "load-test-pass-1"


# ================================================================================================ data
def build_data(data_dir, employees, admins, calls_total, mails, sla_requests, log):
    from calllog import security
    from calllog.ops import demo
    from calllog.ops.store import OpsStore
    from calllog.storage import Store, iso_now

    security.PBKDF2_ITERATIONS = 1000                      # sign-in speed only matters for the set-up here
    ops = OpsStore(data_dir).init()
    calls = Store(data_dir)
    calls.init()
    now = datetime.now().replace(microsecond=0)
    demo.seed(ops, now=now)
    demo.seed_users_and_calls(calls, now=now)

    teams = list(demo.TEAMS)
    pw_hash = security.hash_password(PASSWORD)
    users = []
    for i in range(employees):
        team = teams[i % len(teams)]
        users.append({"user_id": f"e{i:03d}", "name": f"Employee {i:03d}", "department": "Load test", "role": "user",
                      "password_hash": pw_hash, "active": True, "must_change_password": False,
                      "created_at": iso_now(now), "last_login": None, "email": f"employee{i:03d}@{demo.DOMAIN}",
                      "team": team, "teams": [team] + ([teams[(i + 1) % len(teams)]] if i % 7 == 0 else []),
                      "security_name": ""})
    for i in range(admins):
        users.append({"user_id": f"a{i:03d}", "name": f"Admin {i:03d}", "department": "Load test",
                      "role": "admin" if i % 2 == 0 else "manager", "password_hash": pw_hash, "active": True,
                      "must_change_password": False, "created_at": iso_now(now), "last_login": None,
                      "email": "", "team": "", "teams": [], "security_name": ""})
    calls.create_users(users)

    # calls: spread the total over the last 20 working days, one file per person per day (the real layout)
    reasons = [r["key"] for r in calls.reasons()]
    rng = random.Random(7)
    uids = [u["user_id"] for u in users if u["role"] == "user"]
    days = [(now - timedelta(days=d)).date().isoformat() for d in range(1, 21)]
    per_file = max(1, calls_total // (len(uids) * len(days)))
    root = Path(data_dir) / "records"
    written = 0
    for day in days:
        (root / day).mkdir(parents=True, exist_ok=True)
        for uid in uids:
            items = [{"id": f"{day}_{uid}_{n:05d}", "user_id": uid, "date": day,
                      "time": f"{8 + n * 8 // per_file:02d}:{(n * 7) % 60:02d}:00", "ip_phone": "70001",
                      "reason": rng.choice(reasons), "note": "", "branch": "", "branch_name": "", "region": "",
                      "created_at": iso_now(now), "updated_at": None, "updated_by": None} for n in range(per_file)]
            (root / day / f"{uid}.json").write_text(json.dumps(items), encoding="utf-8")
            written += len(items)
    log(f"  calls written: {written:,} in {len(days)} days")

    # scale the e-mail and SLA snapshots up to a busy day
    snap = ops.read_snapshot("email")
    rows = snap["data"]["rows"]
    base = len(rows)
    scaled = list(rows)
    n = 0
    while len(scaled) < mails:
        src = copy.deepcopy(rows[n % base])
        n += 1
        src["row_id"] = f"{src.get('row_id', 'row')}#x{n}"
        src["message_id"] = f"{src.get('message_id', 'msg')}#x{n}"
        scaled.append(src)
    snap["data"]["rows"] = scaled
    ops.write_snapshot("email", snap["data"], date=snap.get("date"), host=snap.get("host", ""))
    snap = ops.read_snapshot("sla")
    reqs = snap["data"]["requests"]
    base = len(reqs)
    scaled = list(reqs)
    n = 0
    while len(scaled) < sla_requests:
        src = copy.deepcopy(reqs[n % base])
        n += 1
        src["key"] = f"{src['key']}x{n}"
        scaled.append(src)
    snap["data"]["requests"] = scaled
    ops.write_snapshot("sla", snap["data"], date=snap.get("date"), host=snap.get("host", ""))
    return ops


def add_file_delay(delay_ms, data_dir):
    """Every operation on a file inside ``data_dir`` waits ``delay_ms`` first (a shared folder is slower than a disk).
    Answers served from the program's own memory do not touch a file, so they are not slowed down - as in real life."""
    if delay_ms <= 0:
        return
    import builtins
    import io
    import os
    pause = delay_ms / 1000.0
    prefix = os.path.abspath(data_dir)

    def inside(path):
        try:
            return os.path.abspath(os.fspath(path)).startswith(prefix)
        except TypeError:                           # a file handle number, not a path
            return False

    def slow(fn):
        def wrapper(path, *args, **kwargs):
            if inside(path):
                time.sleep(pause)
            return fn(path, *args, **kwargs)
        return wrapper
    builtins.open = io.open = slow(io.open)
    for name in ("stat", "scandir", "listdir", "replace", "open", "unlink", "remove"):
        setattr(os, name, slow(getattr(os, name)))


# ================================================================================================ server
def serve(data_dir, threads, delay_ms, refresher=True):
    """Runs inside the server process (``--serve``): the web app on a free port, file operations slowed down."""
    from waitress import create_server
    from calllog import create_app, security
    from calllog.app import start_refresher
    security.PBKDF2_ITERATIONS = 1000
    add_file_delay(delay_ms, data_dir)
    app = create_app({"data_dir": data_dir, "secret_key": "load-test-secret", "user_edit_days": None})
    if refresher:
        start_refresher(app)                               # as run.py does
    server = create_server(app, host="127.0.0.1", port=0, threads=threads, ident="calllog")
    print(f"PORT {server.effective_port}", flush=True)
    server.run()


def start_server(data_dir, threads, delay_ms, refresher=True):
    """The server gets its own process, so the 60 simulated users do not share its interpreter lock."""
    import subprocess
    proc = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "--serve", data_dir, str(threads),
                             str(delay_ms)] + ([] if refresher else ["--no-refresher"]), stdout=subprocess.PIPE, text=True, cwd=str(ROOT))
    line = proc.stdout.readline()
    if not line.startswith("PORT "):
        proc.kill()
        raise RuntimeError(f"the test server did not start: {line!r}")
    threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()     # keep the pipe drained
    return proc, int(line.split()[1])


# ================================================================================================ client
class Client:
    """One signed-in browser tab: cookies + CSRF, keep-alive connection."""

    def __init__(self, port):
        self.port = port
        self.cookies = {}
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)

    def _request(self, method, path, body=None, headers=None):
        headers = dict(headers or {})
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        headers.setdefault("Accept-Encoding", "gzip")
        try:
            self.conn.request(method, path, body=body, headers=headers)
            resp = self.conn.getresponse()
            data = resp.read()
        except (http.client.HTTPException, OSError):
            self.conn.close()
            self.conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
            raise
        for header, value in resp.getheaders():
            if header.lower() == "set-cookie":
                name, _, rest = value.partition("=")
                self.cookies[name.strip()] = rest.split(";", 1)[0]
        return resp.status, data

    def login(self, uid):
        status, body = self._request("GET", "/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', body.decode("utf-8", "replace"))
        form = f"user_id={uid}&password={PASSWORD}&csrf_token={token.group(1) if token else ''}"
        status, _ = self._request("POST", "/login", body=form,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        if status not in (302, 303):
            raise RuntimeError(f"sign-in of {uid} failed ({status})")

    def get(self, path):
        started = time.perf_counter()
        status, data = self._request("GET", path)
        return status, (time.perf_counter() - started) * 1000.0, data


class Results:
    def __init__(self):
        self.lock = threading.Lock()
        self.times = {}
        self.errors = {}
        self.samples = []                           # (ms, request, who, seconds since start) - to look at the slowest ones
        self.t0 = time.perf_counter()

    def add(self, name, ms, ok=True, who=""):
        with self.lock:
            self.times.setdefault(name, []).append(ms)
            self.samples.append((ms, name, who, round(time.perf_counter() - self.t0, 1)))
            if not ok:
                self.errors[name] = self.errors.get(name, 0) + 1


def employee(port, uid, results, stop, interval, offset, ready, go):
    """The "My work" page: /my every interval (+ /shared with it), first call after a random start offset."""
    client = Client(port)
    try:
        client.login(uid)
    except Exception as exc:                        # noqa: BLE001 - a failed sign-in is a result, not a crash
        results.add("sign-in", 0.0, ok=False)
        print(f"  ! {exc}")
        ready.release()
        return
    ready.release()
    go.wait()                                       # everybody is signed in: the measuring starts now
    if stop.wait(offset):
        return
    while not stop.is_set():
        for name, path in (("my", "/api/ops/my"), ("shared", "/api/ops/shared")):
            try:
                status, ms, _ = client.get(path)
                results.add(name, ms, ok=status == 200, who=uid)
            except Exception:                       # noqa: BLE001
                results.add(name, 0.0, ok=False)
        if stop.wait(interval):
            return


def admin(port, uid, results, stop, interval, offset, ready, go):
    """Hub overview + the e-mail, SLA and ITSM pages; every poll sends ``since``/``rev`` of the last answer."""
    client = Client(port)
    try:
        client.login(uid)
    except Exception as exc:                        # noqa: BLE001
        results.add("sign-in", 0.0, ok=False)
        print(f"  ! {exc}")
        ready.release()
        return
    ready.release()
    go.wait()
    if stop.wait(offset):
        return
    last = {}
    while not stop.is_set():
        for module in ("overview", "email", "sla", "itsm", "shared"):
            path = "/api/ops/overview" if module == "overview" else f"/api/ops/{module}"
            if module == "shared":
                path = "/api/ops/shared"
            elif module in last:
                path += f"?since={last[module]['since']}" + (f"&rev={last[module]['rev']}" if last[module].get("rev") else "")
            try:
                status, ms, data = client.get(path)
            except Exception:                       # noqa: BLE001
                results.add(module, 0.0, ok=False)
                continue
            body = {}
            if status == 200:
                try:
                    body = json.loads(data or b"{}")
                except ValueError:
                    pass
            unchanged = bool(body.get("unchanged"))
            name = module if module in ("overview", "shared") else (f"{module} unchanged" if unchanged else f"{module} full")
            results.add(name, ms, ok=status == 200)
            if module in ("email", "sla", "itsm") and status == 200 and not unchanged:
                snap_time = (body.get("snapshot") or {}).get("generated_at") or body.get("generated_at") \
                    or (body.get("meta") or {}).get("generated_at")
                if snap_time:
                    last[module] = {"since": snap_time, "rev": (body.get("meta") or {}).get("rev", "")}
        if stop.wait(interval):
            return


def collector(ops, stop, every):
    """Pretends to be the collector: replaces the snapshots now and then so clients have to fetch new data."""
    while not stop.wait(every):
        for module in ("email", "sla"):
            snap = ops.read_snapshot(module)
            if snap:
                ops.write_snapshot(module, snap["data"], date=snap.get("date"), host=snap.get("host", ""))


# ================================================================================================ report
def percentile(values, pct):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(pct / 100.0 * len(ordered) + 0.5)) - 1))
    return ordered[index]


def report(results, seconds, verbose=False):
    print()
    print(f"{'request':<18}{'count':>8}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>10}{'errors':>8}   target p95")
    failed = []
    for name in sorted(results.times):
        values = results.times[name]
        p50, p95 = statistics.median(values), percentile(values, 95)
        target = TARGETS.get(name)
        verdict = ""
        if target is not None:
            verdict = f"<{target:g} ms  " + ("OK" if p95 < target else "MISSED")
            if p95 >= target:
                failed.append(name)
        print(f"{name:<18}{len(values):>8}{p50:>10.1f}{p95:>10.1f}{max(values):>10.1f}{results.errors.get(name, 0):>8}   {verdict}")
    if verbose:
        print("\nslowest requests (ms, request, who, second):")
        for sample in sorted(results.samples, reverse=True)[:8]:
            print("  ", *sample)
    total = sum(len(v) for v in results.times.values())
    errors = sum(results.errors.values())
    print(f"\n{total:,} requests in {seconds:.0f} s = {total / seconds:.1f} per second, {errors} errors")
    return failed, errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--users", type=int, default=60, help="people on the program at the same time (default 60)")
    parser.add_argument("--seconds", type=int, default=60, help="how long to run (default 60)")
    parser.add_argument("--delay-ms", type=float, default=5.0, help="extra time per file operation, like a shared folder (default 5)")
    parser.add_argument("--calls", type=int, default=142_000, help="calls in the data folder (default 142000)")
    parser.add_argument("--mails", type=int, default=500)
    parser.add_argument("--sla", type=int, default=300)
    parser.add_argument("--threads", type=int, default=16, help="server threads (the program's default is 16)")
    parser.add_argument("--interval-scale", type=float, default=0.2,
                        help="1 = the pages' real intervals (30 s / 15 s); 0.2 = five times as often (default)")
    parser.add_argument("--verbose", action="store_true", help="also list the slowest requests")
    parser.add_argument("--keep", action="store_true", help="keep the temporary data folder")
    parser.add_argument("--no-refresher", action="store_true", help="compare: without the background refresher")
    parser.add_argument("--serve", nargs=3, metavar=("DATA_DIR", "THREADS", "DELAY_MS"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.serve:
        serve(args.serve[0], int(args.serve[1]), float(args.serve[2]), not args.no_refresher)
        return 0

    admins = max(1, args.users // 6)
    employees = max(1, args.users - admins)
    folder = tempfile.mkdtemp(prefix="hub-loadtest-")
    print(f"Temporary data folder: {folder}")
    try:
        print("Setting up the data ...")
        started = time.perf_counter()
        ops = build_data(folder, employees, admins, args.calls, args.mails, args.sla, print)
        print(f"  done in {time.perf_counter() - started:.0f} s")
        server, port = start_server(folder, args.threads, args.delay_ms, not args.no_refresher)
        print(f"Server on port {port}: {employees} employees + {admins} admins/managers for {args.seconds} s "
              f"(file delay {args.delay_ms:g} ms, intervals x{args.interval_scale:g})")

        results, stop = Results(), threading.Event()
        ready, go = threading.Semaphore(0), threading.Event()
        rng = random.Random(1)
        workers = []
        emp_interval, adm_interval = 30 * args.interval_scale, 15 * args.interval_scale
        for i in range(employees):
            workers.append(threading.Thread(target=employee, args=(port, f"e{i:03d}", results, stop, emp_interval,
                                                                    rng.uniform(0, emp_interval), ready, go), daemon=True))
        for i in range(admins):
            workers.append(threading.Thread(target=admin, args=(port, f"a{i:03d}", results, stop, adm_interval,
                                                                 rng.uniform(0, adm_interval), ready, go), daemon=True))
        workers.append(threading.Thread(target=collector, args=(ops, stop, 20.0), daemon=True))
        for w in workers:
            w.start()
        for _ in range(employees + admins):         # wait until everybody has signed in
            ready.acquire(timeout=120)
        results.t0 = t0 = time.perf_counter()
        go.set()
        time.sleep(args.seconds)
        stop.set()
        for w in workers:
            w.join(timeout=30)
        elapsed = time.perf_counter() - t0
        failed, errors = report(results, elapsed, args.verbose)
        server.terminate()
        if failed or errors:
            print("\nRESULT: MISSED - " + (", ".join(failed) if failed else f"{errors} requests failed"))
            return 1
        print("\nRESULT: all targets met")
        return 0
    finally:
        if args.keep:
            print(f"Data folder kept: {folder}")
        else:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
