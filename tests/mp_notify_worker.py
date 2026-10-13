"""Run in separate processes by test_notifications.py: simulates the collector (another PC, another process) adding
notifications to the same person's file while the web app writes to it too."""
from datetime import datetime

from calllog.ops import notifications
from calllog.ops.store import OpsStore


def hammer(root, worker, rounds, user_id):
    store = OpsStore(root)
    for i in range(rounds):
        item = notifications.make_item("replied", f"worker {worker}", f"note {worker}-{i}", "/email", "collector",
                                       f"<w{worker}-{i}@x>", datetime(2026, 10, 11, 9, 0, 0))
        notifications.add(store, user_id, [item], datetime(2026, 10, 11, 9, 0, 0))
