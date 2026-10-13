"""Admin command line for emergencies (e.g. a forgotten administrator password).

    python manage.py list-users
    python manage.py reset-password 230966          (prints a new temporary password)
    python manage.py add-admin boss "Full Name"     (prints a temporary password)

IP phone directory (phone number -> branch / department; the best way to load a 40,000-row export):
    python manage.py import-ip-phones PATH.xlsx     replaces the directory; names of people in the file are never read into it

Operations Hub:
    python manage.py import-legacy --email OLD_EMAIL.py --sla SLA.PY --itsm itsm_status.py [--out-config PATH] [--no-passwords] [--force]
                                                    read the three old scripts (never run them): settings into the data
                                                    folder, users / passwords / archive name into collector_config.json
    python manage.py seed-demo [--data-dir PATH]    fill a data folder with demo data (to try the pages before a collector runs)

Works while the server is running; it uses the same data folder and file locking.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import bootstrap  # noqa: F401  (adds the 'libs' folder)
from calllog import security
from calllog.config import load_config
from calllog.storage import StorageError, Store, iso_now, norm_uid, valid_uid
from datetime import datetime


def open_store():
    store = Store(load_config()["data_dir"])
    store.init()
    return store


def cmd_list(args):
    for u in sorted(open_store().users().values(), key=lambda u: (u["role"], u["user_id"])):
        state = "active" if u.get("active", True) else "DISABLED"
        print(f"{u['user_id']:<14}{u['role']:<7}{state:<9}{u['name']}")


def cmd_reset(args):
    password = security.generate_password()
    result = open_store().update_user(args.user_id, {
        "password_hash": security.hash_password(password), "must_change_password": True, "active": True})
    if not result:
        sys.exit(f"No such user: {args.user_id}")
    print(f"New temporary password for {norm_uid(args.user_id)}: {password}")
    print("They must choose their own password at the next sign-in.")


def cmd_add_admin(args):
    uid = norm_uid(args.user_id)
    if not valid_uid(uid):
        sys.exit("User ID may use letters, digits, dot, dash and underscore.")
    password = security.generate_password()
    open_store().create_user({
        "user_id": uid, "name": args.name, "department": "", "role": "admin",
        "password_hash": security.hash_password(password), "active": True,
        "must_change_password": True, "created_at": iso_now(datetime.now()), "last_login": None})
    print(f"Administrator '{uid}' created. Temporary password: {password}")


def write_private_json(path, data):
    """collector_config.json holds passwords: write it atomically and, where the OS supports it, readable by the owner only."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def cmd_import_legacy(args):
    from calllog.ops import collector_config, legacy_import, settings as hub_settings
    from calllog.ops.store import OpsStore
    sources = {"email": args.email, "sla": args.sla, "itsm": args.itsm}
    if not any(sources.values()):
        sys.exit("Give at least one old script:  --email PATH  --sla PATH  --itsm PATH")
    for name, path in sources.items():
        if path and not Path(path).is_file():
            sys.exit(f"--{name}: file not found: {path}")
    target = Path(args.out_config) if args.out_config else Path(__file__).resolve().parent / collector_config.FILE_NAME
    if target.exists() and not args.force:
        sys.exit(f"{target} already exists - nothing was changed. Use --force to replace it, or --out-config PATH for another file.")

    patch, config, notes = legacy_import.import_legacy(args.email, args.sla, args.itsm, include_passwords=not args.no_passwords)
    print("Read: " + ", ".join(f"{name} script" for name, path in sources.items() if path))
    print("Found:")
    for label, count in legacy_import.describe(patch, config):
        print(f"  {label}: {count}")

    ops = OpsStore(load_config()["data_dir"]).init()
    try:
        hub_settings.patch(ops, patch)
    except hub_settings.SettingsError as exc:
        sys.exit(f"The settings could not be saved: {exc}")
    print(f"Settings saved: {ops.settings_path}")
    if config:
        write_private_json(target, config)
        print(f"{target.name} written: {target}")
        print("  It holds user names" + ("" if args.no_passwords else ", passwords") + " and the Outlook archive name - keep it private.")
    else:
        print("No user names or archive name were found, so no collector_config.json was written.")
    if notes:
        print("Notes:")
        for note in notes:
            print(f"  - {note}")
    print("Next: check the result with  python collector.py --check")


def cmd_import_ip_phones(args):
    from calllog import ip_phones
    path = Path(args.path)
    if not path.is_file():
        sys.exit(f"File not found: {path}")
    store = open_store()
    started = time.perf_counter()
    try:
        data = ip_phones.build_from_bytes(path.read_bytes(), path.name, imported_by="manage.py", now=iso_now(datetime.now()))
    except ip_phones.IpPhoneError as exc:
        sys.exit(f"ERROR: {exc}")
    previous = store.ip_directory().status()["numbers"]
    store.replace_ip_phones(data)
    report = data["report"]
    store.audit({"ts": iso_now(datetime.now()), "actor": None, "actor_name": "manage.py", "action": "ip_phones_imported",
                 "target": data["source_name"], "ip": None, "details": {**report, "previous": previous}})
    print(f"IP phone directory imported in {time.perf_counter() - started:.1f} s: {store.ip_phones_path}")
    print(f"  rows read:           {report['rows']:,}")
    print(f"  numbers kept:        {report['numbers']:,}   (units: {report['units']:,}, numbers under more than one unit: {report['ambiguous']:,})")
    print(f"  rows skipped:        {report['no_number'] + report['bad_number'] + report['no_unit']:,}   "
          f"(no number {report['no_number']:,}, bad number {report['bad_number']:,}, no unit {report['no_unit']:,})")
    print(f"  units that are Call Log branches: {store.ip_directory().status()['branch_units']:,}")
    print("  The names of people in the file were not stored. The file itself was not copied.")


def cmd_seed_demo(args):
    try:
        from calllog.ops import demo
        seed_ops, seed_calls = demo.seed, demo.seed_users_and_calls
    except (ImportError, AttributeError):
        sys.exit("The demo data module (calllog/ops/demo.py) is not part of this copy of the program yet.")
    from calllog.ops.store import OpsStore
    data_dir = str(Path(args.data_dir).resolve()) if args.data_dir else load_config()["data_dir"]
    ops = OpsStore(data_dir).init()
    calls = Store(data_dir)
    calls.init()
    seed_ops(ops)
    seed_calls(calls)
    print(f"Demo data written to {data_dir}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-users").set_defaults(func=cmd_list)
    p = sub.add_parser("reset-password"); p.add_argument("user_id"); p.set_defaults(func=cmd_reset)
    p = sub.add_parser("add-admin"); p.add_argument("user_id"); p.add_argument("name"); p.set_defaults(func=cmd_add_admin)
    p = sub.add_parser("import-legacy", help="read the old e-mail / SLA / ITSM scripts (never runs them)")
    p.add_argument("--email", metavar="PATH", help="the old Daily_Team_Reply_Analytics script")
    p.add_argument("--sla", metavar="PATH", help="the old SLA.PY")
    p.add_argument("--itsm", metavar="PATH", help="the old itsm_status.py")
    p.add_argument("--out-config", metavar="PATH", help="where to write collector_config.json (default: next to manage.py)")
    p.add_argument("--no-passwords", action="store_true", help="do not copy passwords into collector_config.json")
    p.add_argument("--force", action="store_true", help="overwrite an existing collector_config.json")
    p.set_defaults(func=cmd_import_legacy)
    p = sub.add_parser("import-ip-phones", help="load the IP phone directory (Excel / CSV: phone, name, branch / department)")
    p.add_argument("path", metavar="PATH", help="the .xlsx or .csv export (columns telephoneNumber, displayName, msExchExtensionAttribute27)")
    p.set_defaults(func=cmd_import_ip_phones)
    p = sub.add_parser("seed-demo", help="write demo data (needs calllog/ops/demo.py)")
    p.add_argument("--data-dir", metavar="PATH", help="data folder to fill (default: the configured one)")
    p.set_defaults(func=cmd_seed_demo)
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except StorageError as exc:
        sys.exit(f"ERROR: {exc}")


if __name__ == "__main__":
    main()
