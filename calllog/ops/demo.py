"""Demo data for the Operations Hub: made-up accounts, calls, snapshots, history, heartbeats and logs.

Why: the pages (and their screenshots and tests) need something to show before a collector has ever run. Everything here
is invented - people, customers and mailboxes end in ``.example``, security users carry Arabic names, user numbers are
1001... - and it all passes ``schemas.validate_snapshot``. The same ``now`` always produces the same files.

* ``seed_users_and_calls(call_store, now)``  accounts + ~60 calls today and some yesterday in the Call Log storage
* ``seed(ops_store, now)``                   snapshots, history, overrides, events, heartbeats, collector info, logs, reports

Used by ``python manage.py seed-demo``, the tests and the documentation screenshots.
"""
import hashlib
import os
import random
import tempfile
from collections import Counter
from itertools import zip_longest
from datetime import datetime, timedelta
from pathlib import Path

from .. import security
from ..storage import StorageError, iso_now
from . import mailbox, notifications, overlay, schemas
from . import settings as hub_settings

DEMO_PASSWORD = "demo-pass-2026"
DOMAIN = "demo.example"
HOST = "DEMO-PC-01"
TEAMS = list(hub_settings.DEFAULT_TEAMS)
SLA_TARGET = 60
JOB_NAMES = schemas.JOBS

# team -> [(mailbox, readable name)]; the routing prefix of the mailbox is the old "flexcube.jane@..." convention
TEAM_PEOPLE = {
    "Flex Cube": [("flexcube.alice.hassan", "Alice Hassan"), ("flexcube.omar.fathy", "Omar Fathy"),
                  ("flexcube.salma.nabil", "Salma Nabil")],
    "Card Management": [("cardmanagement.karim.adel", "Karim Adel"), ("cardmanagement.laila.saad", "Laila Saad")],
    "MFA Team": [("mfa.hany.mostafa", "Hany Mostafa"), ("mfa.nour.ezz", "Nour Ezz")],
    "Third Party": [("thirdparty.sara.gaber", "Sara Gaber")],
    "Open System": [("opensystem.tamer.wahba", "Tamer Wahba"), ("opensystem.rana.lotfy", "Rana Lotfy")],
}
MANAGER = ("flexcube.mona.adel", "Mona Adel")
BUSINESS_ADDRESSES = {"Flex Cube": "flexcube.support", "Card Management": "cards.support", "MFA Team": "mfa.support",
                      "Third Party": "thirdparty.support", "Open System": "opensystem.support"}
GENERAL_ADDRESS = "general.support"             # a business mailbox no rule maps to a team: its mails may stay Unclassified
ADDRESS_RULES = ("MFA Team", "Third Party")     # teams whose business mailbox is mapped to them in the demo settings (address_team_map)
CLASSIFIED_BY = ("system", "address", "default_team", "override", "none")
SECURITY_USERS = ["ياسمين فاروق", "هاني جمال", "مصطفى عبد الرحمن", "دينا مختار", "كريم البنا"]

EMPLOYEES = [   # user id, name, department, mailbox, security name, teams (1002 belongs to two teams)
    ("1001", "Alice Hassan", "Identity Management", f"flexcube.alice.hassan@{DOMAIN}", SECURITY_USERS[0], ["Flex Cube"]),
    ("1002", "Omar Fathy", "Identity Management", f"flexcube.omar.fathy@{DOMAIN}", SECURITY_USERS[1], ["Flex Cube", "Card Management"]),
    ("1003", "Tarek Mansour", "Application Support", "", "", []),
    ("1004", "Nada Ibrahim", "Application Support", "", "", []),
]
MANAGER_ID = "mgr.flex"

SUBJECTS = {   # team -> [(subject, application)]; "{n}" is a branch number
    "Flex Cube": [("Flexcube: cannot post journal entry for branch {n}", "Flexcube"),
                  ("طلب صلاحية على نظام فليكس كيوب - فرع {n}", "Flexcube"),
                  ("FCUBS end of day batch shows a warning", "Flexcube"),
                  ("GL reconciliation report is empty", "Flexcube"),
                  ("Flexcube user locked after password reset", "Flexcube")],
    "Card Management": [("Prepaid card activation fails at branch {n}", "Prepaid Cards"),
                        ("Instant card issuance printer error", "Instant Card Issuance"),
                        ("Essentis: card limit change not applied", "Essentis"),
                        ("طلب اصدار بطاقة فورية - فرع {n}", "Instant Card Issuance"),
                        ("DWP profile missing for a new joiner", "DWP")],
    "MFA Team": [("Veridium token not syncing for a user", "Veridium (MFA)"),
                 ("Hard token replacement request", "Hard Token"),
                 ("MFA enrolment for the new branch staff", "Veridium (MFA)"),
                 ("EIAM account unlock request", "EIAM")],
    "Third Party": [("ACH file rejected by the clearing house", "ACH"), ("CPS interface timeout", "CPS"),
                    ("TMS reconciliation question", "TMS"), ("Service provider SLA report", "Service Provider")],
    "Open System": [("FileNet scan error at branch {n}", "FileNet"), ("Platinum Mail quota request", "Platinum Mail"),
                    ("CRM page loads slowly", "CRM"), ("Signature Viewer is not loading", "Signature Viewer"),
                    ("E-Form submission fails", "E-Form")],
}
UNCLASSIFIED_SUBJECTS = [("SWIFT message stuck in the outbound queue", "swift; mt103"),
                         ("Question about the new treasury screen", "treasury"),
                         ("طلب غير واضح بخصوص تقرير شهري", "تقرير")]
CUSTOMERS = ["Hana Abdelrahman", "Nabil Mansour", "Dina Zaki", "Yara Hamed", "Mostafa Sherif", "Reem Anwar",
             "Adham Gad", "Mariam Fouad", "Ziad Barakat", "Heba Samir", "Ismail Reda", "Farah Younes"]
SPLIT_MAILS = [   # one mail sent to two teams: (teams and text), did the first team answer?
    (("Flex Cube", "Card Management", "Card limit and GL posting mismatch", "Flexcube; Essentis"), True),
    (("MFA Team", "Open System", "New joiners need tokens and CRM access", "Veridium (MFA); CRM"), False),
]
CYCLE = ["fast", "fast", "pending", "slow", "fast", "no_action", "late", "fast", "pending", "fast"]
QUALITY_NOTES = {"Excellent": "Clear answer with the exact steps.", "Good": "Correct, could be shorter.",
                 "Needs Improvement": "The customer had to ask twice."}
REPLY_TEXT = "Dear colleague, the request is done on our side. Please check and tell us if anything is missing."

JOB_ERRORS = {"email": "Cannot connect to Outlook after 5 tries", "email_report": "Could not save the Excel file",
              "sla": "The portal login page did not load within 60 s", "itsm": "Chrome could not be started"}


def _iso(moment):
    return moment.isoformat(timespec="seconds") if moment else None


def _pct(part, total):
    return round(part / total * 100.0, 2) if total else 0.0


def _fmt_minutes(minutes):
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} Min"
    h, m = divmod(minutes, 60)
    return f"{h} H {m} Min" if m else f"{h} H"


def _day_start(now):
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ======================================================================================== accounts and calls
def _user(uid, name, role, dept, now, **profile):
    return {"user_id": uid, "name": name, "department": dept, "role": role, "active": True,
            "must_change_password": False, "created_at": iso_now(now - timedelta(days=30)),
            "last_login": iso_now(now - timedelta(minutes=90)), "password_hash": security.hash_password(DEMO_PASSWORD),
            "email": "", "team": "", "teams": [], "security_name": "", **profile}


def _call_times(rng, count, start, end):
    span = max(60, int((end - start).total_seconds()))
    return sorted(start + timedelta(seconds=rng.randrange(span)) for _ in range(count))


def _make_calls(call_store, rng, day, uid, count, start, end, reasons, branches):
    """Append ``count`` made-up calls of one employee; skipped when that employee already has calls on that day."""
    if call_store.day_records(day, uid):
        return 0
    weights = [(5, 4, 2, 3)[i % 4] for i in range(len(reasons))] or [1]
    for when in _call_times(rng, count, start, end):
        reason = rng.choices(reasons, weights=weights)[0]["key"]
        branch = rng.choice(branches) if branches and rng.random() < 0.7 else None
        call_store.add_record({
            "id": f"{day}_{uid}_{rng.randrange(16 ** 6):06x}", "user_id": uid, "date": day, "time": when.time().isoformat(timespec="seconds"),
            "ip_phone": str(rng.randrange(70000, 80000)), "reason": reason,
            "note": rng.choice(["", "", "", "Called twice", "طلب متابعة", "Waiting for the branch"]),
            "branch": branch["code"] if branch else "", "branch_name": branch["name"] if branch else "",
            "region": branch["region"] if branch else "", "created_at": iso_now(when), "updated_at": None, "updated_by": None})
    return count


def seed_users_and_calls(call_store, now=None):
    """Create the demo accounts (4 employees - one of them in two teams - and 1 manager of Flex Cube) and about 60 calls today
    plus some yesterday.

    Existing accounts and days that already have calls are left alone, so running it twice adds nothing. Returns the
    credentials for tests: ``{"password", "users": {uid: {...}}, "employees": [uid], "manager": uid, "records": {...}}``."""
    now = (now or datetime.now()).replace(microsecond=0)
    users = {}
    for uid, name, dept, mailbox, secname, teams in EMPLOYEES:
        users[uid] = _user(uid, name, "user", dept, now, email=mailbox, team=teams[0] if teams else "", teams=list(teams),
                           security_name=secname)
    users[MANAGER_ID] = _user(MANAGER_ID, MANAGER[1], "manager", "IT Operations", now,
                              email=f"{MANAGER[0]}@{DOMAIN}", team="Flex Cube", teams=["Flex Cube"])
    for user in users.values():
        try:
            call_store.create_user(user)
        except StorageError:
            pass                                            # already there
    reasons = call_store.reasons()
    branches = call_store.branches()
    rng = random.Random(f"calls-{now.date().isoformat()}")
    today, yesterday = now.date().isoformat(), (now - timedelta(days=1)).date().isoformat()
    start = max(_day_start(now), min(now.replace(hour=8, minute=0, second=0), now - timedelta(minutes=90)))
    counts = {"1001": 21, "1002": 17, "1003": 13, "1004": 9}                       # 60 today
    spread = {"1001": 12, "1002": 10, "1003": 8, "1004": 6}                        # 36 yesterday
    made_today = made_yesterday = 0
    for uid in counts:
        made_today += _make_calls(call_store, rng, today, uid, counts[uid], start, now - timedelta(minutes=1), reasons, branches)
        y0 = _day_start(now) - timedelta(days=1) + timedelta(hours=8)
        made_yesterday += _make_calls(call_store, rng, yesterday, uid, spread[uid], y0, y0 + timedelta(hours=8), reasons, branches)
    return {"password": DEMO_PASSWORD, "employees": [e[0] for e in EMPLOYEES], "manager": MANAGER_ID,
            "users": {uid: {"name": u["name"], "role": u["role"], "email": u["email"], "team": u["team"], "teams": u["teams"],
                            "security_name": u["security_name"], "password": DEMO_PASSWORD} for uid, u in users.items()},
            "records": {"today": made_today, "yesterday": made_yesterday}}


# ================================================================================================ e-mail
def _address(local):
    return f"{local}@{DOMAIN}"


def _mail_row(n, subject, system, team, received, now, hint=None, classified_by="system"):
    customer = CUSTOMERS[n % len(CUSTOMERS)]
    arabic = any("؀" <= c <= "ۿ" for c in subject)
    message_id = f"<demo-{n:04d}@mail.{DOMAIN}>"
    return {
        "subject": subject, "entry_id": f"00000000DEMO{n:04d}", "message_id": message_id,
        "row_id": message_id, "detected_systems": system, "team": team, "all_teams": [team],
        "teams_pending": [], "teams_replied": [], "classified_by": classified_by,
        "classification_hint": hint if hint is not None else system.lower(), "customer_name": customer,
        "customer_email": _address(customer.lower().replace(" ", ".")), "received_time": _iso(received),
        "received_time_display": f"{received:%H:%M}", "mailbox": "IT Support shared mailbox", "folder": "Inbox",
        "business_addresses_in_to": _address(BUSINESS_ADDRESSES.get(team, GENERAL_ADDRESS)),
        "match_method": "No Match", "first_reply_by": "", "first_reply_email": "", "first_reply_time": None,
        "last_reply_by": "", "last_reply_email": "", "last_reply_time": None, "mail_copy": "",
        "first_response_time": "", "response_minutes": None, "duration_minutes": None, "reply_count": 0,
        "multiple_team_reply": False, "multiple_team_reply_team": "", "current_waiting_time": "",
        "status": schemas.EMAIL_PENDING, "sla_minutes": SLA_TARGET, "sla_status": "Within SLA", "escalated": False,
        "reminder_count": 0, "is_carryover": False, "age_days": 0, "is_split_child": False, "split_sibling_teams": [],
        "no_action_reason": "", "reply_by_manager": False, "assigned_to_email": "", "assigned_to_name": "",
        "assigned_note": "", "assigned_by": "", "assigned_at": "", "quality_reviewed_by": "", "quality_reviewed_at": "",
        "quality_rating": "", "quality_note": "", "quality_reasons": "", "quality_appeal": "", "appeal_decision": "",
        "rating_withdrawn": False,
        "body_preview": ("السلام عليكم، نرجو المساعدة في الموضوع الموضح أعلاه." if arabic
                         else f"Hello team, we need your help with: {subject}")[:300],
        "reply_body_preview": "",
    }


def _make_replied(row, person, minutes, now, by_manager=False):
    received = datetime.fromisoformat(row["received_time"])
    minutes = max(1, min(minutes, int((now - received).total_seconds() // 60) - 1))
    mailbox, name = person
    row.update(
        status=schemas.EMAIL_REPLIED, first_reply_by=name, first_reply_email=_address(mailbox),
        first_reply_time=_iso(received + timedelta(minutes=minutes)), response_minutes=minutes,
        last_reply_by=name, last_reply_email=_address(mailbox), last_reply_time=_iso(received + timedelta(minutes=minutes)),
        first_response_time=_fmt_minutes(minutes), duration_minutes=minutes, reply_count=1 + minutes % 2,
        sla_status="Met" if minutes <= SLA_TARGET else "Breached", teams_replied=list(row["all_teams"]),
        teams_pending=[], match_method=("ConversationID", "ConversationIndex", "Subject")[minutes % 3],
        reply_by_manager=by_manager, reply_body_preview=REPLY_TEXT)
    return row


def _make_pending(row, now, carry_days=0):
    received = datetime.fromisoformat(row["received_time"])
    waiting = max(0, int((now - received).total_seconds() // 60))
    late = waiting > SLA_TARGET
    row.update(teams_pending=list(row["all_teams"]), current_waiting_time=_fmt_minutes(waiting), duration_minutes=waiting,
               sla_status="Breached" if late else "Within SLA", escalated=late, reminder_count=min(3, waiting // 60),
               is_carryover=carry_days > 0, age_days=carry_days)
    return row


def _make_no_action(row, reason="Auto: acknowledgement / thank-you message"):
    row.update(status=schemas.EMAIL_NO_ACTION, no_action_reason=reason, sla_status="N/A")
    return row


def _received(now, rng, low, high):
    """A receive time ``low``..``high`` minutes ago, never before midnight (the snapshot is about today)."""
    moment = now - timedelta(minutes=rng.randint(low, high))
    return max(moment, _day_start(now) + timedelta(minutes=rng.randint(1, 20)))


def _generated_rows(now, rng):
    rows, n = [], 0
    counts = {"Flex Cube": 12, "Card Management": 9, "MFA Team": 7, "Third Party": 5, "Open System": 7}
    for team, count in counts.items():
        people, pool = TEAM_PEOPLE[team], SUBJECTS[team]
        for i in range(count):
            n += 1
            subject, system = pool[i % len(pool)]
            subject = subject.format(n=100 + (n * 7) % 400)
            kind = CYCLE[(n + len(team)) % len(CYCLE)]
            if kind == "fast":
                row = _mail_row(n, subject, system, team, _received(now, rng, 60, 400), now)
                _make_replied(row, people[i % len(people)], rng.randint(4, 50), now)
            elif kind == "slow":
                row = _mail_row(n, subject, system, team, _received(now, rng, 150, 420), now)
                _make_replied(row, people[i % len(people)], rng.randint(65, 140), now)
            elif kind in ("pending", "late"):
                low, high = (8, 55) if kind == "pending" else (75, 260)
                row = _make_pending(_mail_row(n, subject, system, team, _received(now, rng, low, high), now), now)
            else:
                row = _make_no_action(_mail_row(n, subject, system, team, _received(now, rng, 30, 300), now))
            rows.append(row)
    return rows, n


def _special_rows(now, rng, n):
    """Carry-over mails, unclassified mails, mails sent to two teams, replies written by the manager."""
    rows = []
    for team, days in (("Flex Cube", 1), ("Flex Cube", 2), ("Card Management", 1), ("Third Party", 3)):
        n += 1
        subject, system = SUBJECTS[team][n % len(SUBJECTS[team])]
        received = now - timedelta(days=days, minutes=rng.randint(30, 300))
        rows.append(_make_pending(_mail_row(n, subject.format(n=210 + n), system, team, received, now), now, days))
    for subject, hint in UNCLASSIFIED_SUBJECTS:
        n += 1
        row = _mail_row(n, subject, "", "Unclassified", _received(now, rng, 20, 200), now, hint=hint, classified_by="none")
        rows.append(_make_pending(row, now))
    for (first, second, subject, system), replied_first in SPLIT_MAILS:
        n += 1
        received = _received(now, rng, 90, 240)
        for team, answered in ((first, replied_first), (second, not replied_first)):
            row = _mail_row(n, subject, system, team, received, now)
            row.update(row_id=f"{row['message_id']}|{team}", all_teams=[team], is_split_child=True,
                       split_sibling_teams=[second if team == first else first])
            if answered:
                _make_replied(row, TEAM_PEOPLE[team][0], 35, now)
                row.update(teams_replied=[team], teams_pending=[second if team == first else first])
            else:
                _make_pending(row, now)
                row["teams_pending"] = [team]
            rows.append(row)
    n += 1
    both = _make_replied(_mail_row(n, "Token and card request answered twice", "Hard Token; Prepaid Cards", "MFA Team",
                                   _received(now, rng, 120, 300), now), TEAM_PEOPLE["MFA Team"][0], 22, now)
    both.update(multiple_team_reply=True, multiple_team_reply_team="Card Management", reply_count=2)
    rows.append(both)
    for k in range(2):                                  # a manager answered: never quality-reviewed
        n += 1
        subject, system = SUBJECTS["Flex Cube"][k + 1]
        rows.append(_make_replied(_mail_row(n, subject.format(n=330 + k), system, "Flex Cube", _received(now, rng, 100, 300), now),
                                  MANAGER, 18 + k * 6, now, by_manager=True))
    return rows


def _build_overrides(rows, now):
    """Manual actions of the day: two no-action marks, one team change, three assignments, ratings and appeals."""
    by_id = lambda r: r["message_id"]
    pending = [r for r in rows if r["status"] == schemas.EMAIL_PENDING and not r["is_split_child"] and not r["is_carryover"]
               and r["team"] != "Unclassified"]
    stamp = lambda minutes: _iso(now - timedelta(minutes=minutes))
    overrides = {"no_action": {}, "team": {}, "assignments": {}, "quality": {}}
    for r in [r for r in pending if r["team"] in ("Third Party", "Open System")][:2]:
        overrides["no_action"][by_id(r)] = {"marked_by": MANAGER[1], "marked_at": stamp(40), "subject": r["subject"]}
    unclassified = [r for r in rows if r["team"] == "Unclassified"]
    if unclassified:
        overrides["team"][by_id(unclassified[0])] = {"team": "Third Party", "marked_by": MANAGER[1], "marked_at": stamp(25),
                                                      "subject": unclassified[0]["subject"]}
    flex = [r for r in pending if r["team"] == "Flex Cube" and by_id(r) not in overrides["no_action"]]
    alice, omar = TEAM_PEOPLE["Flex Cube"][0], TEAM_PEOPLE["Flex Cube"][1]

    def assign(row, person, note, minutes, **extra):
        mailbox_, name = person
        overrides["assignments"][by_id(row)] = {
            "assigned_to_email": _address(mailbox_), "assigned_to_name": name, "note": note, "marked_by": MANAGER[1],
            "marked_by_id": MANAGER_ID, "marked_at": stamp(minutes), "subject": row["subject"], "team": row["team"],
            "notified": True, **extra}
    # two open mails for the first employee, one open mail for the second (it has no saved copy yet: "preparing")
    for r, person, note, minutes in zip(flex[:3], (alice, omar, alice), ("Please call the branch first", "", "Customer is waiting"),
                                        (55, 52, 50)):
        assign(r, person, note, minutes)
    # one the employee already marked done (still waiting for a reply in Outlook: Done is not a reply), an older mail
    carry = [r for r in rows if r["team"] == "Flex Cube" and r["is_carryover"] and not r["is_split_child"]]
    if carry:
        assign(carry[-1], alice, "Branch confirmed by phone - waiting for their e-mail", 180,
               done_at=stamp(20), done_by=alice[1], done_by_id="1001", done_note="Called the branch, they will confirm today")
    # one the employee answered in Outlook (shows as done by itself)
    answered = [r for r in rows if r["team"] == "Flex Cube" and r["status"] == schemas.EMAIL_REPLIED
                and r["first_reply_email"] == _address(alice[0]) and not r["is_split_child"]]
    if answered:
        assign(answered[-1], alice, "Please answer today", 0, replied_notified=_iso(now - timedelta(minutes=10)))
        overrides["assignments"][by_id(answered[-1])]["marked_at"] = _iso(
            datetime.fromisoformat(answered[-1]["received_time"]) + timedelta(minutes=2))
    return overrides


def _rate(rows, overrides, now):
    """Ratings on replied mails of ordinary team members (managers' replies are not reviewed)."""
    replied = [r for r in rows if r["status"] == schemas.EMAIL_REPLIED and not r["reply_by_manager"] and not r["is_split_child"]
               and not r["multiple_team_reply"]]
    by_team = {}
    for r in replied:
        by_team.setdefault(r["team"], []).append(r)
    replied = [r for group in zip_longest(*by_team.values()) for r in group if r]       # round robin: every team gets ratings
    ratings = ["Excellent", "Good", "Needs Improvement", "Excellent", "Good", "Needs Improvement", "Good", "Excellent",
               "Needs Improvement", "Excellent", "Good", "Good", "Needs Improvement", "Excellent"]
    for i, (row, rating) in enumerate(zip(replied, ratings)):
        entry = {"rating": rating, "note": QUALITY_NOTES[rating], "reasons": "Incomplete answer" if rating == "Needs Improvement" else "",
                 "replied_by": row["first_reply_by"], "replied_by_email": row["first_reply_email"], "reviewed_by": MANAGER[1],
                 "reviewed_at": _iso(now - timedelta(minutes=30 + i * 3)), "subject": row["subject"], "team": row["team"],
                 "feedback_sent": False, "appeal": "", "appeal_by": "", "appeal_at": "", "appeal_decision": "", "decided_by": "",
                 "decided_at": "", "rating_withdrawn": False}
        overrides["quality"][row["message_id"]] = entry
    needs = [e for e in overrides["quality"].values() if e["rating"] == "Needs Improvement"]
    if len(needs) >= 3:
        needs[0].update(appeal="The customer changed the request after my reply.", appeal_by=needs[0]["replied_by"],
                        appeal_at=_iso(now - timedelta(minutes=12)))
        needs[1].update(appeal="Reply was sent within the agreed time.", appeal_by=needs[1]["replied_by"],
                        appeal_at=_iso(now - timedelta(minutes=20)), appeal_decision="Rejected", decided_by=MANAGER[1],
                        decided_at=_iso(now - timedelta(minutes=8)))
        needs[2].update(appeal="Wrong mail was rated.", appeal_by=needs[2]["replied_by"], appeal_at=_iso(now - timedelta(minutes=18)),
                        appeal_decision="Accepted", decided_by=MANAGER[1], decided_at=_iso(now - timedelta(minutes=6)),
                        rating_withdrawn=True)


def _quality_block(rows):
    people = {}
    for r in rows:
        if r["quality_rating"] and not r["rating_withdrawn"] and not r["reply_by_manager"]:
            p = people.setdefault(r["first_reply_by"], {"name": r["first_reply_by"], "team": r["team"], "reviewed": 0,
                                                        "excellent": 0, "good": 0, "needs": 0, "minutes": []})
            p["reviewed"] += 1
            p["excellent" if r["quality_rating"] == "Excellent" else "good" if r["quality_rating"] == "Good" else "needs"] += 1
            p["minutes"].append(r["response_minutes"] or 0)
    scorecard = []
    for p in people.values():
        score = round((p["excellent"] * 100 + p["good"] * 75 + p["needs"] * 25) / p["reviewed"], 1)
        scorecard.append({k: v for k, v in p.items() if k != "minutes"} | {"score": score, "_minutes": p["minutes"]})
    scorecard.sort(key=lambda e: (e["score"], -e["reviewed"], e["name"]))
    matrix = []
    for e in scorecard:
        avg = sum(e["_minutes"]) / len(e["_minutes"])
        fast, good = avg <= SLA_TARGET, e["score"] >= 75
        matrix.append({"name": e["name"], "team": e["team"], "avg_minutes": round(avg), "avg_score": e["score"],
                       "quadrant": "Fast & High Quality" if fast and good else "Fast but Low Quality" if fast
                       else "Slow but High Quality" if good else "Slow & Low Quality"})
    for e in scorecard:
        e.pop("_minutes")
    index = {}
    for e in scorecard:
        t = index.setdefault(e["team"], {"weighted": 0.0, "reviewed": 0, "excellent": 0, "good": 0, "needs": 0})
        t["weighted"] += e["score"] * e["reviewed"]
        for k in ("reviewed", "excellent", "good", "needs"):
            t[k] += e[k]
    team_index = {team: {"index": round(t.pop("weighted") / t["reviewed"], 1), **t} for team, t in index.items()}
    ranked = sorted((e for e in scorecard if e["reviewed"] >= 2), key=lambda e: (-e["score"], -e["reviewed"], e["name"]))
    top = ranked[:2]
    bottom = [e for e in reversed(ranked) if e not in top][:2]
    trends = [{"name": e["name"], "points": [max(25, e["score"] - 12), e["score"]], "latest": e["score"],
               "first": max(25, e["score"] - 12), "direction": "improving", "days": 2} for e in scorecard[:3]]
    return {"scorecard": scorecard, "team_index": team_index, "leaderboard_top": top, "leaderboard_bottom": bottom,
            "speed_quality_matrix": matrix, "person_trends": trends,
            "repeat_issue_alerts": [{"name": e["name"], "needs": 3, "team": e["team"], "days": 2}
                                    for e in scorecard if e["needs"] >= 2][:1]}


def _trend(stats):
    """Today against invented 'yesterday' and 'last 7 days' figures, in the shape of the engine's compute_trend()."""
    out = {"vs_yesterday": {}, "vs_last7": {}}
    for metric in ("business_emails", "replied", "pending", "no_action_needed", "reply_rate", "average_response", "sla_breached"):
        value = stats.get(metric, 0) or 0
        for key, factor in (("vs_yesterday", 0.9), ("vs_last7", 1.05)):
            ref = round(value * factor, 2)
            delta = round(value - ref, 2)
            out[key][metric] = {"value": ref, "delta": delta, "direction": "up" if delta > 0.001 else "down" if delta < -0.001 else "flat"}
    return out


def _email_stats(rows, now):
    base = {"sla_target_minutes": SLA_TARGET, "team_stats": {t: {"team_emails_sent": 2 + i} for i, t in enumerate(TEAMS)}}
    stats = overlay.recount(rows, base)
    replied = [r for r in rows if r["status"] == schemas.EMAIL_REPLIED]
    stats.update(
        team_replies=len(replied), total_team_replies=sum(r["reply_count"] for r in replied),
        business_emails_incl_carryover=len(rows),
        conversation_matches=sum(1 for r in rows if r["match_method"] == "ConversationID"),
        conversation_index_matches=sum(1 for r in rows if r["match_method"] == "ConversationIndex"),
        subject_matches=sum(1 for r in rows if r["match_method"] == "Subject"),
        no_matches=sum(1 for r in rows if r["match_method"] == "No Match"),
        system_volume=dict(Counter(s for r in rows for s in r["detected_systems"].split("; ") if s).most_common()),
        business_address_volume=dict(Counter(r["business_addresses_in_to"] for r in rows).most_common()),
        reply_breakdown=[{"name": name, "team": team, "count": c} for (name, team), c in
                         Counter((r["first_reply_by"], r["team"]) for r in replied).most_common()],
        trend=_trend(stats))
    return stats


def _tag_classification(rows, overrides):
    """How each row got its team, as the engine writes it: a mail sent to a mapped business mailbox is 'address' (every other
    one of those teams' mails here), a team set by hand is 'override'; the rest keep 'system' / 'none'."""
    by_address = {}
    for row in rows:
        if row["classified_by"] == "system" and row["team"] in ADDRESS_RULES and not row.get("is_split_child"):
            count = by_address[row["team"]] = by_address.get(row["team"], 0) + 1
            if count % 2:
                row.update(classified_by="address", detected_systems=f"{row['team']} (recipient address)",
                           classification_hint=f"{row['team']} (recipient address)")
    for row in rows:
        if row["message_id"] in overrides["team"]:
            row["classified_by"] = "override"


def email_snapshot_data(now):
    """(data, overrides) of the e-mail snapshot: the mails of 'today' with the manual actions already folded in."""
    rng = random.Random("email")
    rows, n = _generated_rows(now, rng)
    rows += _special_rows(now, rng, n)
    overrides = _build_overrides(rows, now)
    _rate(rows, overrides, now)
    rows = overlay.apply_overrides(rows, overrides, now)
    _tag_classification(rows, overrides)
    rows.sort(key=lambda r: (r["received_time"], r["row_id"]))
    _plan_mail_copies(rows, overrides)
    stats = _email_stats(rows, now)
    counts = Counter(r["classified_by"] for r in rows)
    data = {"date": now.date().isoformat(), "sla_target_minutes": SLA_TARGET, "teams": list(TEAMS), "stats": stats, "rows": rows,
            "quality": _quality_block(rows),
            "classification": {"counts": {k: counts.get(k, 0) for k in CLASSIFIED_BY},
                               "unclassified": sum(1 for r in rows if r["team"] == "Unclassified"), "unclassified_team": ""}}
    return data, overrides


# ================================================================================ saved copies and notifications
MSG_DEMO_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1DEMO-MSG (not a real Outlook message)\n"
OMAR_OPEN_NOTE = "the second employee's open mail has no saved copy yet, so the viewer shows 'preparing'"
BODY_EN = ["Hello team,", "", "{subject}.", "",
           "We tried again this morning and the problem is still there. Could you please check and tell us what we should do?",
           "", "Steps we followed:", "  1. Signed out and signed in again", "  2. Cleared the cache and restarted the application",
           "  3. Tried from another PC in the same branch", "", "This is blocking the branch's customers, so any help today is appreciated.",
           "", "Best regards,", "{customer}", "Branch operations", "", "-----Original Message-----", "From: {customer}",
           "Sent: yesterday 16:42", "Subject: {subject}", "", "Please see the screenshot attached."]
BODY_AR = ["السلام عليكم،", "", "{subject}", "", "حاولنا مرة أخرى هذا الصباح والمشكلة ما زالت قائمة، برجاء الفحص والإفادة.", "",
           "الخطوات التي اتبعناها:", "  ١. تسجيل الخروج ثم الدخول مرة أخرى", "  ٢. إعادة تشغيل البرنامج",
           "  ٣. التجربة من جهاز آخر في نفس الفرع", "", "نرجو سرعة الرد لأن العملاء في انتظار الخدمة.", "", "مع خالص التحية،", "{customer}"]


def _plan_mail_copies(rows, overrides):
    """Which mails have a saved copy in the demo (the collector's rule: assigned and pending), written as ``mail_copy`` on the
    row the way the engine does. One assigned mail is left without a copy on purpose (state 'preparing'), one pending mail has
    a preview only ('too large')."""
    assigned = overrides["assignments"]
    left_out = next((m for m, e in assigned.items() if e["assigned_to_email"] == _address(TEAM_PEOPLE["Flex Cube"][1][0])), None)
    plan = {m: "ready" for m in assigned if m != left_out}
    pending = [r for r in rows if r["status"] == schemas.EMAIL_PENDING and r["message_id"] not in assigned
               and not r.get("is_split_child") and r["team"] != "Unclassified"]
    for i, row in enumerate(pending[:6]):
        plan[row["message_id"]] = "too_large" if i == 2 else "ready"
    for row in rows:
        row["mail_copy"] = plan.get(row["message_id"], "")


def _demo_preview(row, now, index):
    arabic = any("؀" <= c <= "ۿ" for c in row["subject"])
    text = "\n".join((BODY_AR if arabic else BODY_EN)).format(subject=row["subject"], customer=row["customer_name"])
    attachments = [("screenshot.png", 184_320), ("error-log.txt", 5_120)][: index % 3]
    if row.get("mail_copy") == "too_large":
        attachments = [("scanned-documents.zip", 31_457_280)]              # the reason this mail is preview-only
    return mailbox.make_preview(
        row["message_id"], subject=row["subject"], from_name=row["customer_name"], from_email=row["customer_email"],
        to=[row["business_addresses_in_to"]], cc=[_address("branch.operations")] if index % 2 else [],
        received=row["received_time"], body=text, attachments=[{"name": n, "size": z} for n, z in attachments],
        saved_at=now - timedelta(minutes=5))


def _write_mail_copies(ops, rows, now):
    """Fake .msg bytes + preview for every row the plan marked; the previous demo copies are cleared first, so seeding twice
    gives the same files. The .msg is NOT a real Outlook message - it only proves the download works."""
    ops.mail_dir.mkdir(parents=True, exist_ok=True)
    for old in ops.mail_dir.glob("*"):
        if old.is_file():
            old.unlink()
    written = 0
    for index, row in enumerate(r for r in rows if r.get("mail_copy")):
        key = mailbox.mail_key(row["message_id"])
        preview = _demo_preview(row, now, index)
        if row["mail_copy"] == "too_large":
            mailbox.write_copy(ops.mail_dir, key, dict(preview, too_large=True))
        else:
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / f"{key}.msg"
                source.write_bytes(MSG_DEMO_MAGIC + f"subject={row['subject']}\n".encode("utf-8") + preview["body_text"].encode("utf-8")
                                   + b"\0" * (2048 + 512 * index))
                mailbox.write_copy(ops.mail_dir, key, preview, msg_file=source)
        written += 1
    return written


def _demo_notifications(ops, rows, overrides, now):
    """Three to six notifications for the first employee and for the Flex Cube manager, telling the story of the demo
    assignments (assigned, taken away, done, answered in Outlook, reopened). Fixed ids, so seeding twice is the same."""
    by_assignee = {}
    for message_id, entry in overrides["assignments"].items():
        by_assignee.setdefault(entry["assigned_to_email"], []).append((message_id, entry))
    alice = _address(TEAM_PEOPLE["Flex Cube"][0][0])
    mine = sorted(by_assignee.get(alice, []), key=lambda t: t[1]["marked_at"])
    omar_mail = next((m for m, e in overrides["assignments"].items()
                      if e["assigned_to_email"] == _address(TEAM_PEOPLE["Flex Cube"][1][0])), None)
    done = next(((m, e) for m, e in mine if e.get("done_at")), None)
    answered = next(((m, e) for m, e in mine if not e.get("done_at") and any(
        r["message_id"] == m and r["status"] == schemas.EMAIL_REPLIED for r in rows)), None)
    open_mails = [(m, e) for m, e in mine if not e.get("done_at") and (answered is None or m != answered[0])]
    subject = {m: e["subject"] for m, e in overrides["assignments"].items()}
    user_role, boss_role = {"role": "user"}, {"role": "manager"}

    def at(stamp, minutes=0):
        return datetime.fromisoformat(stamp) + timedelta(minutes=minutes)

    def item(n, moment, kind, title, text, message_id, role, actor, read=False):
        return {"id": f"demo-{n:02d}", "ts": _iso(moment), "type": kind, "title": title,
                "text": text[:200], "link": notifications.link_for(role, message_id), "actor": actor,
                "ref": {"message_id": message_id}, "read": read}

    employee_items, manager_items = [], []
    for k, (message_id, entry) in enumerate(open_mails[:2]):
        employee_items.append(item(10 + k, at(entry["marked_at"]), schemas.NOTIFY_ASSIGNED, "New e-mail assigned to you",
                                   f"{subject[message_id]} - by {MANAGER[1]}", message_id, user_role, MANAGER[1]))
    if omar_mail:       # the mail was Alice's first and went to the second employee: she is told it was taken away
        employee_items.append(item(12, at(overrides["assignments"][omar_mail]["marked_at"]), schemas.NOTIFY_UNASSIGNED,
                                   "Assignment removed", f"{subject[omar_mail]} - by {MANAGER[1]}", omar_mail, user_role,
                                   MANAGER[1], read=True))
    if done:
        employee_items.append(item(13, at(done[1]["marked_at"]), schemas.NOTIFY_ASSIGNED, "New e-mail assigned to you",
                                   f"{subject[done[0]]} - by {MANAGER[1]}", done[0], user_role, MANAGER[1], read=True))
        manager_items.append(item(20, at(done[1]["done_at"]), schemas.NOTIFY_DONE, f"{EMPLOYEES[0][1]} marked an e-mail as done",
                                  f"{subject[done[0]]} - {done[1].get('done_note', '')}", done[0], boss_role, EMPLOYEES[0][1]))
    if answered:
        reply_at = next(r["first_reply_time"] for r in rows if r["message_id"] == answered[0])
        manager_items.append(item(21, at(reply_at, 1), schemas.NOTIFY_REPLIED, f"{EMPLOYEES[0][1]} replied to an assigned e-mail",
                                  subject[answered[0]], answered[0], boss_role, EMPLOYEES[0][1]))
    if open_mails:      # she marked the first open mail done and then took it back
        first_id, first = open_mails[0]
        manager_items.append(item(22, at(first["marked_at"], 12), schemas.NOTIFY_REOPENED, "E-mail reopened",
                                  f"{subject[first_id]} - by {EMPLOYEES[0][1]}", first_id, boss_role, EMPLOYEES[0][1], read=True))
        manager_items.append(item(23, at(first["marked_at"], 6), schemas.NOTIFY_DONE, f"{EMPLOYEES[0][1]} marked an e-mail as done",
                                  f"{subject[first_id]} - Waiting for the branch", first_id, boss_role, EMPLOYEES[0][1], read=True))
    ops.notifications_dir.mkdir(parents=True, exist_ok=True)
    for uid, items in ((EMPLOYEES[0][0], employee_items), (MANAGER_ID, manager_items)):
        items.sort(key=lambda i: i["ts"], reverse=True)
        doc = {"items": items, "ts": _iso(now) + ".000000", "seen_ts": ""}
        ops.write_json(notifications.path_for(ops, uid), doc)
    return len(employee_items) + len(manager_items)


# ================================================================================================== SLA
REQUESTERS = ["أحمد سمير", "منى إبراهيم", "خالد عادل", "نورا حسن", "طارق فتحي", "سلمى رضا", "عمرو شوقي", "هبة ناجي"]
BRANCHES = ["فرع المعادي", "فرع مدينة نصر", "فرع الدقي", "فرع الإسكندرية", "فرع أسيوط", "إدارة الائتمان", "إدارة الخزانة"]
SYSTEMS = ["Flexcube", "T24", "DWP", "Prime", "EIAM"]
REQUEST_TYPES = ["اضافة صلاحية", "سحب صلاحية", "اعادة تعيين كلمة المرور", "نقل مستخدم"]
SUPPORT_STAFF = ["Support Desk 1", "Support Desk 2", "Support Desk 3"]
# (status, age in minutes) of the 33 open requests; ages are before the scan
OWNER_PATTERN = [0, 1, 2, 0, 3, 1, 0, 4, 2, 1, 0, 3]      # who owns the i-th request: uneven, like a real queue
SLA_AGES = ([("breached", a) for a in (310, 245, 180, 150, 128)] + [("reminder", a) for a in (118, 105, 97, 88, 76, 72, 64)]
            + [("within", a) for a in (58, 55, 52, 49, 46, 43, 40, 37, 34, 31, 28, 25, 22, 19, 16, 13, 10, 7, 4)]
            + [("no_date", None), ("no_date", None)])


def _sla_totals(requests):
    totals = {"open": len(requests), "oldest_minutes": max((r["age_minutes"] or 0 for r in requests), default=0)}
    totals.update({s: sum(1 for r in requests if r["status"] == s) for s in schemas.SLA_STATUSES})
    return totals


def _sla_by_user(requests):
    out = []
    for user in dict.fromkeys(r["security_user"] for r in requests):
        mine = [r for r in requests if r["security_user"] == user]
        out.append({"user": user, "total": len(mine), **{s: sum(1 for r in mine if r["status"] == s) for s in schemas.SLA_STATUSES},
                    "oldest_minutes": max(r["age_minutes"] or 0 for r in mine)})
    return sorted(out, key=lambda u: (-u["breached"], -u["reminder"], -u["total"], u["user"]))


def sla_snapshot_data(scan_at):
    rng = random.Random("sla")
    requests = []
    for i, (status, age) in enumerate(SLA_AGES):
        user = SECURITY_USERS[OWNER_PATTERN[i % len(OWNER_PATTERN)]]
        fields = {"اسم الموظف": REQUESTERS[i % len(REQUESTERS)], "رقم المستخدم": str(9000 + i),
                  "الفرع / الادارة": BRANCHES[rng.randrange(len(BRANCHES))], "النظام": SYSTEMS[i % len(SYSTEMS)],
                  "نوع الطلب": REQUEST_TYPES[i % len(REQUEST_TYPES)], "موظف الاسناد": SUPPORT_STAFF[i % len(SUPPORT_STAFF)]}
        key = hashlib.sha1(f"{user}|{sorted(fields.items())}".encode()).hexdigest()[:16]
        sent = scan_at - timedelta(minutes=age) if age is not None else None
        requests.append({"key": key, "security_user": user, "sent_at": _iso(sent), "age_minutes": age, "status": status, "fields": fields})
    requests.sort(key=lambda r: (r["age_minutes"] is None, -(r["age_minutes"] or 0)))
    return {"thresholds": {"warning_minutes": 60, "breach_minutes": 120}, "totals": _sla_totals(requests),
            "by_user": _sla_by_user(requests), "requests": requests}


# ================================================================================================= ITSM
ITSM_ROLES = ["IMP User provisioning", "IMP UAT I-Flex SMS"]
IMPL, ROLLBACK = "Implementation Requests", "Rollback Requests"
ITSM_SUMMARIES = ["Add user to the Flexcube group", "فتح صلاحية على النظام", "Reset profile limits", "Deploy patch to UAT",
                  "Create a service account", "Move the batch window"]


def _itsm_rows(base, count):
    return [{"number": str(base + i), "summary": ITSM_SUMMARIES[i % len(ITSM_SUMMARIES)]} for i in range(count)]


def itsm_snapshot_data(error_role=False):
    cat = lambda rows, count=None, warning=None: {"count": len(rows) if count is None else count, "rows": rows,
                                                   "warning": warning, "note": None}
    first = {"role": ITSM_ROLES[0], "error": None, "categories": {
        IMPL: cat(_itsm_rows(512300, 3)), ROLLBACK: cat(_itsm_rows(512400, 1))}}
    if error_role:
        second = {"role": ITSM_ROLES[1], "error": "Set Role button not found", "categories": {}}
    else:
        second = {"role": ITSM_ROLES[1], "error": None, "categories": {
            IMPL: cat(_itsm_rows(512500, 3), 4, "The scoreboard says 4 but only 3 rows could be read"),
            ROLLBACK: {"count": None, "rows": [], "warning": None, "note": "Not on this role's scoreboard"}}}
    roles = [first, second]
    totals = {c: sum(r["categories"][c]["count"] or 0 for r in roles if r["categories"].get(c)) for c in (IMPL, ROLLBACK)}
    changes = {"new": [{"role": ITSM_ROLES[0], "category": ROLLBACK, "number": "512400", "summary": ITSM_SUMMARIES[0]}],
               "closed": [{"role": ITSM_ROLES[0], "category": IMPL, "number": "512099", "summary": ITSM_SUMMARIES[2]}]}
    return {"categories": [IMPL, ROLLBACK], "roles": roles, "totals": totals, "changes": changes}


# ============================================================================================ history
def _grid(now, minutes, hours=8):
    """Scan times every ``minutes`` for the last ``hours`` hours, oldest first, ending at the last grid step before ``now``."""
    steps = hours * 60 // minutes
    last = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % minutes)
    return [last - timedelta(minutes=minutes * k) for k in range(steps, -1, -1)]


def _replace_history(ops, module, points, extra=None):
    """Write the points into their day files (replacing what was there, so seeding twice gives the same files)."""
    by_day = {}
    for point in points:
        by_day.setdefault(point["ts"][:10], []).append(point)
    for day, items in by_day.items():
        def change(doc, items=items):
            doc["points"] = items
            for key, value in (extra or {}).items():
                doc[key] = value if day == max(by_day) else {}
        ops.update_history(module, day, change)


def _email_points(now, stats):
    grid = _grid(now, 10)
    out = []
    for i, ts in enumerate(grid):
        share = (i + 1) / len(grid)
        business = round(stats["business_emails"] * (0.25 + 0.75 * share))
        replied = round(stats["replied"] * share)
        out.append({"ts": _iso(ts), "business_emails": business, "replied": replied, "pending": max(0, business - replied - round(stats["no_action_needed"] * share)),
                    "no_action_needed": round(stats["no_action_needed"] * share), "sla_breached": round(stats["sla_breached"] * share),
                    "escalated": round(stats["escalated_count"] * share)})
    return out


def _sla_points(now, data):
    grid = _grid(now, 15)
    final = data["totals"]
    out = []
    for i, ts in enumerate(grid):
        share = (i + 1) / len(grid)
        breached = round(final["breached"] * share)
        reminder = round(final["reminder"] * (0.5 + 0.5 * share))
        within = round(final["within"] * (0.6 + 0.4 * share))
        out.append({"ts": _iso(ts), "open": within + reminder + breached + final["no_date"], "within": within,
                    "reminder": reminder, "breached": breached})
    first = {r["key"]: _iso(datetime.fromisoformat(r["sent_at"]) + timedelta(minutes=120))
             for r in data["requests"] if r["status"] == "breached" and r["sent_at"]}
    return out, first


def _itsm_points(now, data):
    out = []
    for i, ts in enumerate(_grid(now, 30)):
        wobble = i % 3 - 1
        totals = {c: max(0, n + wobble if c == IMPL else n) for c, n in data["totals"].items()}
        per_role = {r["role"]: {c: (v["count"] or 0) for c, v in r["categories"].items()} for r in data["roles"] if not r["error"]}
        out.append({"ts": _iso(ts), "totals": totals, "per_role": per_role})
    return out


def _previous_finals(ops, now, stats):
    """The headline numbers of the 7 days before today (the e-mail trend compares with them)."""
    for back in range(1, 8):
        day = (now - timedelta(days=back)).date().isoformat()
        factor = 0.85 + (back % 4) * 0.07
        final = {m: round(stats.get(m, 0) * factor, 2) for m in
                 ("business_emails", "replied", "pending", "no_action_needed", "reply_rate", "average_response", "sla_breached")}
        def change(doc, final=final):
            doc["final"] = final
        ops.update_history("email", day, change)


# ============================================================================================ timeline
def _events(now, failed_job, sla_data, email_data):
    at = lambda minutes: _iso(now - timedelta(minutes=minutes))
    breached = [r for r in sla_data["requests"] if r["status"] == "breached"][:3]
    reminder = [r for r in sla_data["requests"] if r["status"] == "reminder"][:3]
    escalated = [r for r in email_data["rows"] if r["escalated"] and r["status"] == schemas.EMAIL_PENDING][:3]
    events = [
        {"ts": at(3), "module": "sla", "level": "danger", "kind": "sla_breached", "count": len(breached),
         "text": f"{len(breached)} security requests passed the 2 h SLA",
         "items": [{"user": r["security_user"], "key": r["key"]} for r in breached]},
        {"ts": at(14), "module": "email", "level": "danger", "kind": "email_escalated", "count": len(escalated),
         "text": f"{len(escalated)} e-mails waiting past the SLA with no team reply",
         "items": [{"subject": r["subject"][:80], "team": r["team"]} for r in escalated]},
        {"ts": at(29), "module": "sla", "level": "warn", "kind": "sla_reminder", "count": len(reminder),
         "text": f"{len(reminder)} security requests reached the reminder time",
         "items": [{"user": r["security_user"], "key": r["key"]} for r in reminder]},
        {"ts": at(44), "module": "itsm", "level": "warn", "kind": "itsm_new", "count": 1, "text": f"1 new {ROLLBACK}",
         "items": [{"number": "512400", "summary": ITSM_SUMMARIES[0], "role": ITSM_ROLES[0]}]},
        {"ts": at(75), "module": "sla", "level": "ok", "kind": "sla_closed", "count": 4, "text": "4 security requests completed"},
        {"ts": at(120), "module": "itsm", "level": "ok", "kind": "itsm_closed", "count": 1, "text": f"1 {IMPL} closed",
         "items": [{"number": "512099", "summary": ITSM_SUMMARIES[2], "role": ITSM_ROLES[0]}]},
    ]
    if failed_job:
        events.append({"ts": at(9), "module": schemas.JOB_MODULE[failed_job], "level": "danger", "kind": "scan_failed",
                       "text": f"{failed_job} scan failed: {JOB_ERRORS[failed_job]}"})
    start = _day_start(now)
    return sorted((e for e in events if datetime.fromisoformat(e["ts"]) >= start), key=lambda e: e["ts"])


def _write_events(ops, now, events):
    import json
    ops.events_dir.mkdir(parents=True, exist_ok=True)
    path = ops.events_dir / f"{now.date().isoformat()}.jsonl"
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")


# ======================================================================================== collector state
def _heartbeats(ops, now, stale_job, failed_job, stats, paused_job=None):
    summaries = {"email": f"{stats['business_emails']} mails, {stats['pending']} pending",
                 "email_report": "Report sent: 2 e-mails, 3 attachments", "sla": "33 open, 5 breached",
                 "itsm": "7 open requests"}
    report_time = now.replace(hour=16, minute=30, second=0)
    report_next = report_time if now < report_time else report_time + timedelta(days=1)
    for job in JOB_NAMES:
        module = schemas.JOB_MODULE[job]
        interval = hub_settings.DEFAULTS[module]["interval_minutes"]
        beat_at = now - timedelta(seconds=40)
        last_run = now - timedelta(minutes=interval // 2 + 1)
        if job == stale_job:
            beat_at = last_run = now - timedelta(hours=3)
        paused = job == paused_job
        failed = job == failed_job or paused
        doc = {
            "job": job, "host": HOST, "pid": 4120, "running": False, "enabled": True, "state": "idle",
            "last_status": "error" if failed else "ok",
            "last_error": "The login failed: wrong or missing user name / password" if paused else JOB_ERRORS[job] if failed else None,
            "last_summary": "" if failed else summaries[job], "last_run_at": _iso(last_run),
            "last_ok_at": _iso(last_run - timedelta(minutes=interval if failed else 0)),
            "last_duration_seconds": 41.5 if job == "email" else 12.4,
            "next_run_at": _iso(report_next if job == "email_report" else last_run + timedelta(minutes=interval)),
            "runs_ok": 18 if not failed else 17, "runs_failed": 1 if failed else 0, "consecutive_failures": 3 if failed else 0,
            "paused": "auth" if paused else None, "paused_since": _iso(last_run) if paused else None,
            "paused_reason": "login failed - fix the password, retries paused" if paused else None, "version": "NBE Operations Hub 1.0", "updated_at": _iso(beat_at), "updated_ts": round(beat_at.timestamp(), 1)}
        if job == "email_report":
            doc["last_run_at"] = _iso(report_time - timedelta(days=1))
            doc["last_ok_at"] = doc["last_run_at"]
        ops.write_json(ops.heartbeat_dir / f"{job}.json", doc)


def _collector_info(ops, now):
    ops.write_state("collector_info", {
        "host": HOST, "pid": 4120, "started_at": _iso(now - timedelta(hours=5)), "version": "NBE Operations Hub 1.0",
        "config_path": "C:\\NBE-Hub\\collector_config.json", "config_found": True,
        "pinned_keys": ["email.target_archive_name", "itsm.username", "sla.username"],
        "accounts": {"sla": {"username": "demo.sla.user", "has_password": True, "password_source": "collector_config.json"},
                     "itsm": {"username": "demo.itsm.user", "has_password": True, "password_source": "collector_config.json"}},
        "jobs": list(JOB_NAMES), "isolated": True, "python": "3.12.4", "updated_at": _iso(now - timedelta(minutes=2))})


def _logs(ops, now, failed_job, stats, paused_job=None):
    lines = {
        "email": ["Started: read-only scan of today", f"Read {stats['business_emails'] + 20} mails, {stats['business_emails']} business mails",
                  f"Finished OK - {stats['business_emails']} mails, {stats['pending']} pending (41.5 s)"],
        "email_report": ["Started: daily report", "Excel workbook written (12 sheets), 3 attachments", "Finished OK - Report sent: 2 e-mails, 3 attachments (88.0 s)"],
        "sla": ["Started: portal scan", "Signed in, 33 open requests read", "Finished OK - 33 open, 5 breached (12.4 s)"],
        "itsm": ["Started: scoreboard scan", "2 roles read", "Finished OK - 7 open requests (35.9 s)"],
    }
    if failed_job:
        lines[failed_job][-1] = f"FAILED - {JOB_ERRORS[failed_job]} (9.1 s)"
    if paused_job:
        lines[paused_job][-1] = "FAILED - the login failed (wrong or missing user name / password); scheduled runs are paused (9.1 s)"
    ops.logs_dir.mkdir(parents=True, exist_ok=True)
    for job, items in lines.items():
        stamps = [now - timedelta(minutes=12 - 5 * i, seconds=40 - 4 * i) for i in range(len(items))]
        ops.log_path(job).write_text("".join(f"[{t:%Y-%m-%d %H:%M:%S}] {line}\n" for t, line in zip(stamps, items)), encoding="utf-8")


def _reports(ops, now):
    """Three small real files so the Reports list and its download link can be tried."""
    from openpyxl import Workbook
    yesterday = (now - timedelta(days=1)).date().isoformat()
    stamp = (now - timedelta(days=1)).replace(hour=16, minute=31).timestamp()
    for module, name in (("email", f"Daily_Team_Reply_Analytics_{yesterday}.xlsx"), ("sla", f"Security_Requests_SLA_{yesterday}.xlsx")):
        path = ops.report_dir(module) / name
        wb = Workbook()
        ws = wb.active
        ws.append(["Demo report", yesterday])
        ws.append(["This file only shows the Reports list; real reports are written by the collector."])
        wb.save(path)
        os.utime(path, (stamp, stamp))
    html = ops.report_dir("itsm") / f"ITSM_Status_{yesterday}.html"
    html.write_text("<!doctype html><title>Demo ITSM report</title><p>Demo report for the Reports list.</p>", encoding="utf-8")
    os.utime(html, (stamp, stamp))


def _demo_settings(ops):
    """Team directory and business mailboxes, only when nobody has saved settings yet (a real configuration is never touched)."""
    if ops.read_settings_raw():
        return False
    members = {team: [_address(m) for m, _ in people] for team, people in TEAM_PEOPLE.items()}
    hub_settings.patch(ops, {"email": {
        "business_emails": [_address(a) for a in (*BUSINESS_ADDRESSES.values(), GENERAL_ADDRESS)], "team_members_by_team": members,
        "team_members": sorted(a for addrs in members.values() for a in addrs), "manager_emails": [_address(MANAGER[0])],
        "classification": {"address_team_map": {_address(BUSINESS_ADDRESSES[t]): t for t in ADDRESS_RULES}}},
        "sla": {"owner_emails": {SECURITY_USERS[0]: _address("yasmin.farouk"), SECURITY_USERS[1]: _address("hany.gamal")}}})
    return True


# ===================================================================================================== seed
def seed(ops, now=None, with_history=True, itsm_error=False, failed_job=None, stale_job="itsm", with_settings=True, paused_job=None):
    """Write the demo files into ``ops`` (an OpsStore) as if collectors had been running all morning.

    ``itsm_error``  one ITSM role could not be read (Set Role button not found)
    ``failed_job``  that job's last run failed (email | email_report | sla | itsm), its snapshot stays the last good one
    ``stale_job``   that job's collector stopped answering three hours ago ("" for none); its snapshot is that old too
    ``paused_job``  that job (sla | itsm) stopped retrying after a wrong password ("login failed - paused")
    ``with_history`` history points of the last 8 hours, the finals of the last week and today's timeline events
    Returns a few counts for the caller. Deterministic for a given ``now``; seeding twice leaves the same files."""
    now = (now or datetime.now()).replace(microsecond=0)
    for job in (failed_job, stale_job, paused_job):
        if job and job not in JOB_NAMES:
            raise ValueError(f"unknown job {job!r}")
    day = now.date().isoformat()
    stale = lambda module: stale_job and schemas.JOB_MODULE[stale_job] == module
    email_at, sla_at = now - timedelta(minutes=2), now - timedelta(minutes=4)
    itsm_at = now - timedelta(hours=3) if stale("itsm") else now - timedelta(minutes=8)
    if stale("email"):
        email_at = now - timedelta(hours=3)
    if stale("sla"):
        sla_at = now - timedelta(hours=3)

    email, overrides = email_snapshot_data(email_at)
    sla = sla_snapshot_data(sla_at)
    itsm = itsm_snapshot_data(itsm_error)
    ops.write_snapshot("email", email, generated_at=_iso(email_at), duration_seconds=41.5, host=HOST, date=day)
    ops.write_snapshot("sla", sla, generated_at=_iso(sla_at), duration_seconds=12.4, host=HOST, date=day)
    ops.write_snapshot("itsm", itsm, generated_at=_iso(itsm_at), duration_seconds=35.9, host=HOST, date=day)
    for name, entries in overrides.items():
        ops.write_json(ops.overrides_dir / f"{name}.json", entries)
    copies = _write_mail_copies(ops, email["rows"], now)
    notified = _demo_notifications(ops, email["rows"], overrides, now)

    if with_history:
        _replace_history(ops, "email", _email_points(email_at, email["stats"]))
        points, first = _sla_points(sla_at, sla)
        _replace_history(ops, "sla", points, {"first_breach": first})
        _replace_history(ops, "itsm", _itsm_points(itsm_at, itsm))
        _previous_finals(ops, now, email["stats"])
        _write_events(ops, now, _events(now, failed_job, sla, email))
    _heartbeats(ops, now, stale_job, failed_job, email["stats"], paused_job)
    _collector_info(ops, now)
    _logs(ops, now, failed_job, email["stats"], paused_job)
    _reports(ops, now)
    configured = _demo_settings(ops) if with_settings else False
    return {"email_rows": len(email["rows"]), "sla_requests": len(sla["requests"]), "itsm_roles": len(itsm["roles"]),
            "history": with_history, "settings_written": configured, "day": day, "mail_copies": copies,
            "notifications": notified}
