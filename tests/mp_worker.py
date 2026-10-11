"""Run in separate processes by test_app.py: simulates several PCs sharing one data folder."""
from calllog.storage import Store


def hammer(root, worker, rounds):
    store = Store(root)
    for i in range(rounds):
        store.add_record({"id": f"2026-10-11_shared_{worker}{i:05x}", "user_id": "shared", "date": "2026-10-11",
                          "time": "09:00:00", "ip_phone": "1", "reason": "solved", "note": ""})
        store.audit({"ts": "2026-10-11 09:00:00", "actor": str(worker), "action": "x", "details": {"i": i}})
        store.update_user("admin", {"last_login": f"{worker}-{i}"})
