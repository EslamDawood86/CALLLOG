"""Start the Call Log web server.  Usage:  python run.py"""
import socket
import sys

from waitress import serve

from calllog import create_app
from calllog.config import load_config
from calllog.storage import StorageError


def lan_address():
    """The address colleagues type in their browser (best effort; no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


def main():
    cfg = load_config()
    try:
        app = create_app()
    except StorageError as exc:
        print(f"\nERROR: {exc}\n")
        sys.exit(1)

    first_password = app.config["INITIAL_ADMIN_PASSWORD"]
    port = cfg["port"]
    print("=" * 66)
    print(" NBE Call Log is running")
    print(f"   Data folder : {cfg['data_dir']}")
    print(f"   Open in a browser:  http://{lan_address()}:{port}")
    print(f"   (on this computer:  http://localhost:{port})")
    if first_password:
        print()
        print("   FIRST RUN - administrator account created:")
        print("     User ID : admin")
        print(f"     Password: {first_password}")
        print("   You will be asked to choose a new password when you sign in.")
        print("   This password is shown only once - write it down now.")
    print()
    print(" Keep this window open. Close it (or press Ctrl+C) to stop the server.")
    print("=" * 66)
    serve(app, host=cfg["host"], port=port, threads=cfg["threads"], ident="calllog")


if __name__ == "__main__":
    main()
