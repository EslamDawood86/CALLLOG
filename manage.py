"""Admin command line for emergencies (e.g. a forgotten administrator password).

    python manage.py list-users
    python manage.py reset-password 230966          (prints a new temporary password)
    python manage.py add-admin boss "Full Name"     (prints a temporary password)

Works while the server is running; it uses the same data folder and file locking.
"""
import argparse
import sys

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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-users").set_defaults(func=cmd_list)
    p = sub.add_parser("reset-password"); p.add_argument("user_id"); p.set_defaults(func=cmd_reset)
    p = sub.add_parser("add-admin"); p.add_argument("user_id"); p.add_argument("name"); p.set_defaults(func=cmd_add_admin)
    args = parser.parse_args()
    try:
        args.func(args)
    except StorageError as exc:
        sys.exit(f"ERROR: {exc}")


if __name__ == "__main__":
    main()
