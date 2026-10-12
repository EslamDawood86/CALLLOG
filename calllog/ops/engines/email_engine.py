"""The "email" job: a read-only Outlook scan of today (plus still-open mail from earlier days) and the team-reply analysis.

Port of the scan + analysis half of the old Daily Team Reply Analytics script (Excel, charts, .msg files and e-mails are
email_report.py; the HTML dashboards and the HTTP server were replaced by the web app). Entry points (docs/HUB-DESIGN.md
section 12):

    run_scan(ctx, namespace=None) -> JobResult         what the runner calls every few minutes
    scan_and_analyze(ctx, namespace=None, keep_mail_items=False) -> ScanOutcome
                                                         the in-memory result email_report.py reuses (Excel, e-mails)
    to_snapshot_data(outcome, ctx) -> dict               JSON-safe snapshot data (contract 5.3)

``namespace`` is the Outlook MAPI namespace; tests pass a fake (tests/fake_outlook.py), production leaves it None.
Everything configurable comes from ``ctx.settings`` (the e-mail section) and the four override lists in
``ctx.store.overrides(...)``; the vocabulary and the matching rules are in email_rules.py (its docstring lists the
accepted ``classification`` and ``advanced`` keys).

Business filter: an e-mail enters the analysis only when one of ``business_emails`` is in its TO line. CC does NOT count.
(The old script's header docstring says so and its code does so; one old comment said "TO or CC" and was wrong.)

Performance, kept from the old "fast scan": only the target archive is walked, PGP folders are skipped before their
Items are touched, each folder is sorted newest-first and walked in Python, and the walk stops at the first item older
than the window. No Restrict() date filter by default: a short-date string is locale-unsafe (01/09 can read as 9 January
and silently return nothing) and the early stop already bounds the work. ``advanced.use_restrict_filter`` turns on the
legacy's ISO-8601 DASL variant for a mailbox so large that sorting a whole folder is slow. The expensive ``item.Body``
read happens only for mail that is a team reply or is addressed to a business mailbox.

Classification precedence (a row's ``classified_by``): manual team override ("override") > a business mailbox the mail was
sent TO that is in ``address_team_map`` ("address") > system words found in the text ("system"; the admin's
``system_team_map`` beats the built-in map) > ``unclassified_team`` ("default_team") > "Unclassified" ("none").

A row's ``message_id`` is the internet message id (identical for every copy of the mail and every scan); when Outlook
cannot give it, the entry id is used instead so every row has a stable key for the override lists. A mail addressed to
several teams becomes one row per team (same message_id, different ``team``; ``row_id`` tells them apart).
"""
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..settings import DEFAULT_TEAMS
from .common import OUTLOOK_LOCK, JobResult, fmt_minutes
from .email_rules import (
    UNCLASSIFIED, Directory, build_quality_leaderboard, build_quality_scorecard, build_rules,
    build_speed_quality_matrix, build_team_quality_index, candidate_words, clean_subject, format_duration,
    get_effective_body_text, norm_email, norm_set, safe_percent, strip_signature_block, to_int)

PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001E"
OL_MAIL_ITEM = 43
OL_FOLDER_INBOX = 6
CONVERSATION_INDEX_ROOT_LENGTH = 44          # 22 bytes, hex encoded: the part every message of a thread shares
BODY_KEEP_CHARS = 2500                       # the new text of a mail, kept for keyword scans and the body popup
THREAD_KEEP_CHARS = 6000                     # the whole text including the quoted history (system detection fallback)
PREVIEW_CHARS = 300                          # body text in the snapshot
NEAREST_REPLY_MAX_GAP_MINUTES = 240
TREND_METRICS = ("business_emails", "replied", "pending", "no_action_needed", "reply_rate", "average_response",
                 "sla_breached")
STATS_HISTORY_FILES = 10                     # newest history day files read per scan: the trend looks back 7 days...
QUALITY_HISTORY_FILES = 30                   # ...the quality trend 14 days that had a report


class OutlookUnavailable(RuntimeError):
    """Outlook cannot be used at all on this machine (not Windows / pywin32 missing): retrying will not help."""


class ConfigProblem(ValueError):
    """The e-mail settings are not enough to scan (no business mailbox, no team member)."""


# ---------------------------------------------------------------------------------------------------------------------
# Configuration derived from ctx.settings
# ---------------------------------------------------------------------------------------------------------------------
class EmailConfig:
    """Everything the analysis needs from the e-mail settings, prepared once per scan."""

    def __init__(self, settings):
        s = settings if isinstance(settings, dict) else {}
        adv = s.get("advanced") if isinstance(s.get("advanced"), dict) else {}
        cls = s.get("classification") if isinstance(s.get("classification"), dict) else {}
        self.business = norm_set(s.get("business_emails"))
        self.directory = Directory(s.get("team_members"), s.get("team_members_by_team"), s.get("team_link_groups"),
                                   s.get("manager_emails"))
        self.team_members = self.directory.members
        self.ignored_senders = norm_set(s.get("ignored_sender_emails"))
        teams = [str(t) for t in (s.get("teams") or DEFAULT_TEAMS)]
        self.teams = teams if UNCLASSIFIED in teams else teams + [UNCLASSIFIED]
        self.sla_target = max(1, to_int(s.get("sla_target_minutes"), 60))
        self.lookback_days = max(0, to_int(s.get("backlog_lookback_days"), 1))
        self.archive_name = str(s.get("target_archive_name") or "").strip()
        self.rules = build_rules(s)
        self.address_team_map = {norm_email(a): str(t).strip() for a, t in (cls.get("address_team_map") or {}).items()
                                 if norm_email(a) and str(t).strip()}
        default_team = str(cls.get("unclassified_team") or "").strip()
        self.unclassified_team = "" if default_team == UNCLASSIFIED else default_team
        self.ignored_folder_terms = [str(t).lower() for t in (adv.get("ignored_folder_terms") or ["pgp"]) if str(t)]
        self.end_buffer_minutes = max(0, to_int(adv.get("scan_end_buffer_minutes"), 0))
        self.forward_fallback_words = max(0, to_int(adv.get("forward_fallback_words"), 10))
        self.use_restrict = bool(adv.get("use_restrict_filter", False))
        self.repeat_threshold = max(1, to_int(adv.get("repeat_issue_threshold"), 3))
        self.repeat_lookback_days = max(1, to_int(adv.get("repeat_issue_lookback_days"), 7))
        self.outlook_retries = max(1, to_int(s.get("outlook_connect_retries"), 5))
        self.outlook_retry_seconds = max(0, to_int(s.get("outlook_connect_retry_seconds"), 15))


def team_member_people(email_settings):
    """[{name, email, team}] for the "Assign to..." list of the web page."""
    return EmailConfig(email_settings).directory.people()


# ---------------------------------------------------------------------------------------------------------------------
# Outlook property helpers. Every COM read can fail, so each one is wrapped and returns "" instead of raising.
# ---------------------------------------------------------------------------------------------------------------------
def to_python_datetime(value):
    if value is None:
        return None
    try:
        return value.replace(tzinfo=None)
    except Exception:
        return value


def get_smtp_address_from_address_entry(address_entry):
    """Real SMTP address of an AddressEntry, including Exchange users whose Address is an X.500 path."""
    if address_entry is None:
        return ""
    try:
        smtp = address_entry.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS)
        if smtp:
            return norm_email(smtp)
    except Exception:
        pass
    try:
        exchange_user = address_entry.GetExchangeUser()
        if exchange_user and exchange_user.PrimarySmtpAddress:
            return norm_email(exchange_user.PrimarySmtpAddress)
    except Exception:
        pass
    try:
        address = str(address_entry.Address or "")
        if "@" in address:
            return norm_email(address)
    except Exception:
        pass
    return ""


def get_sender_smtp_address(item):
    """Fast path first: SenderEmailAddress is already SMTP for mail that came in over SMTP. Only Exchange legacy paths
    ("/O=...") need the slower AddressEntry resolution."""
    try:
        fast = str(item.SenderEmailAddress or "")
    except Exception:
        fast = ""
    if fast and "@" in fast and not fast.startswith("/"):
        return norm_email(fast)
    try:
        sender_type = str(item.SenderEmailType or "")
    except Exception:
        sender_type = ""
    if fast and "@" in fast and sender_type.upper() == "SMTP":
        return norm_email(fast)
    try:
        return get_smtp_address_from_address_entry(item.Sender)
    except Exception:
        return ""


def get_recipient_smtp_address(recipient):
    try:
        fast = str(recipient.Address or "")
    except Exception:
        fast = ""
    if fast and "@" in fast and not fast.startswith("/"):
        return norm_email(fast)
    try:
        return get_smtp_address_from_address_entry(recipient.AddressEntry)
    except Exception:
        return ""


def get_to_cc_and_business_addresses(item, business_addresses):
    """One pass over the recipients: (every To + Cc address, the TO-only addresses that are business mailboxes)."""
    all_to_cc, business_to = set(), set()
    try:
        for recipient in item.Recipients:
            try:
                recipient_type = int(recipient.Type)         # 1 = To, 2 = Cc, 3 = Bcc
            except Exception:
                continue
            if recipient_type not in (1, 2):
                continue
            address = get_recipient_smtp_address(recipient)
            if not address:
                continue
            all_to_cc.add(address)
            if recipient_type == 1 and address in business_addresses:
                business_to.add(address)
    except Exception:
        pass
    return all_to_cc, business_to


def _text_property(item, name):
    try:
        return str(getattr(item, name) or "").strip()
    except Exception:
        return ""


def get_conversation_id(item):
    return _text_property(item, "ConversationID")


def get_conversation_index(item):
    return _text_property(item, "ConversationIndex")


def conversation_index_root(conversation_index):
    """ConversationID is computed per store; the first 22 bytes of the ConversationIndex are the same for the whole
    thread whatever store carries a message, so they identify a thread across mailboxes / archives."""
    if not conversation_index or len(conversation_index) < CONVERSATION_INDEX_ROOT_LENGTH:
        return ""
    return conversation_index[:CONVERSATION_INDEX_ROOT_LENGTH]


def get_internet_message_id(item):
    """Assigned once by the sending server: identical in every copy of the mail and on every scan (EntryID is not)."""
    try:
        return str(item.PropertyAccessor.GetProperty(PR_INTERNET_MESSAGE_ID) or "").strip()
    except Exception:
        return ""


def get_entry_id(item):
    return _text_property(item, "EntryID")


# ---------------------------------------------------------------------------------------------------------------------
# Outlook connection and folder walking
# ---------------------------------------------------------------------------------------------------------------------
def _dispatch_outlook():
    if os.name != "nt":
        raise OutlookUnavailable("Reading Outlook needs Windows with Outlook installed (this collector runs on "
                                 f"{os.name}).")
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise OutlookUnavailable(f"pywin32 is not installed ({exc}); see requirements-collector.txt.") from exc
    pythoncom.CoInitialize()                       # every thread needs its own COM apartment
    return win32com.client.Dispatch("Outlook.Application")


def connect_outlook(ctx, dispatch=None, sleep=time.sleep):
    """``(outlook, namespace)``. Outlook may still be starting (a task that fires right after login), so a failed
    attempt is retried ``outlook_connect_retries`` times, ``outlook_connect_retry_seconds`` apart."""
    cfg = EmailConfig(ctx.settings)
    dispatch = dispatch or _dispatch_outlook
    last_error = None
    for attempt in range(1, cfg.outlook_retries + 1):
        try:
            outlook = dispatch()
            namespace = outlook.GetNamespace("MAPI")
            _ = namespace.Folders.Count            # a half-started COM object fails here, not in the middle of the scan
            return outlook, namespace
        except OutlookUnavailable:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < cfg.outlook_retries:
                ctx.log(f"Outlook not ready yet (attempt {attempt}/{cfg.outlook_retries}): {exc}; "
                        f"retrying in {cfg.outlook_retry_seconds}s")
                sleep(cfg.outlook_retry_seconds)
    raise RuntimeError(f"Could not connect to Outlook after {cfg.outlook_retries} attempt(s): {last_error}")


def folder_is_ignored(folder, terms):
    """True for folders that must never be touched (PGP folders prompt for a passphrase)."""
    try:
        name = str(folder.Name or "").strip().lower()
    except Exception:
        name = ""
    try:
        path = str(folder.FolderPath or "").strip().lower()
    except Exception:
        path = ""
    return any(term in name or term in path for term in terms)


def get_all_folders_fast(parent_folder, terms):
    """``parent_folder`` and every sub-folder, depth first. Ignored folders are dropped before their Items are touched."""
    if folder_is_ignored(parent_folder, terms):
        return []
    folders = [parent_folder]
    try:
        for child in parent_folder.Folders:
            if not folder_is_ignored(child, terms):
                folders.extend(get_all_folders_fast(child, terms))
    except Exception:
        pass
    return folders


def find_target_archive(namespace, archive_name):
    """The top-level Outlook root with exactly this name (only roots are searched: no walk through every mailbox)."""
    wanted = norm_email(archive_name)
    for root in namespace.Folders:
        try:
            if norm_email(root.Name) == wanted:
                return root
        except Exception:
            continue
    return None


def default_mailbox_root(namespace):
    """No archive name set: the store of the default mailbox (parent of its Inbox)."""
    try:
        return namespace.GetDefaultFolder(OL_FOLDER_INBOX).Parent
    except Exception:
        pass
    for root in namespace.Folders:
        return root
    return None


def _open_target(namespace, cfg):
    if not cfg.archive_name:
        root = default_mailbox_root(namespace)
        if root is None:
            raise RuntimeError("The default Outlook mailbox could not be found.")
        return root
    root = find_target_archive(namespace, cfg.archive_name)
    if root is None:
        names = []
        try:
            names = [str(r.Name) for r in namespace.Folders if "@" not in str(r.Name)]
        except Exception:
            pass
        raise RuntimeError(f"Target archive '{cfg.archive_name}' was not found as an Outlook top-level folder"
                           + (f" (archives seen: {', '.join(names[:12])})" if names else "")
                           + ". Check email.target_archive_name in Settings or collector_config.json.")
    return root


def _recent_first_items(folder, start_dt, use_restrict):
    """``(items, ordered)``: the folder's items sorted newest-first. ``ordered`` is False when Outlook refused the sort,
    so the walk must not stop early (it would stop at an arbitrary old item and lose newer ones)."""
    items = folder.Items
    if use_restrict:
        dasl = ('@SQL=' + chr(34) + "urn:schemas:httpmail:datereceived" + chr(34)
                + " >= '" + start_dt.strftime("%Y-%m-%d %H:%M:%S") + "'")
        try:
            items = items.Restrict(dasl)
        except Exception:
            items = folder.Items
    try:
        items.Sort("[ReceivedTime]", True)
        return items, True
    except Exception:
        return items, False


# ---------------------------------------------------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------------------------------------------------
def scan_current_day(ctx, namespace, cfg, now=None, keep_mail_items=False, lookback_days=None, info=None):
    """Read today's mail (and ``backlog_lookback_days`` earlier days) from the target archive.

    Returns ``(all_emails, included_emails, today)``:
      all_emails       every mail from a team member or addressed (TO) to a business mailbox - reply matching needs the
                       team's mails even when they do not mention a business mailbox
      included_emails  the business mails (not from a team member), each with ``business_addresses_in_to``
    ``info`` (a dict) receives scan counters: folders, items_seen, item_errors. A mail that cannot be read is skipped,
    never fatal.
    """
    now = now or ctx.now()
    info = info if info is not None else {}
    today = now.date()
    lookback = cfg.lookback_days if lookback_days is None else max(0, lookback_days)
    start_dt = datetime.combine(today, datetime.min.time()) - timedelta(days=lookback)
    end_dt = max(start_dt, now - timedelta(minutes=cfg.end_buffer_minutes))
    ctx.log(f"Scan window: {start_dt:%d/%m/%Y %H:%M} -> {end_dt:%d/%m/%Y %H:%M}"
            + (f" (includes {lookback} earlier day(s) for carry-over)" if lookback else ""))

    root = _open_target(namespace, cfg)
    folders = get_all_folders_fast(root, cfg.ignored_folder_terms)
    info.update(folders=len(folders), items_seen=0, item_errors=0)
    ctx.log(f"Target archive: {root.Name}; folders to scan: {len(folders)}")

    all_emails, included, seen_ids = [], [], set()
    for folder in folders:
        try:
            if folder_is_ignored(folder, cfg.ignored_folder_terms):
                continue
            items, ordered = _recent_first_items(folder, start_dt, cfg.use_restrict)
            for item in items:
                try:
                    if item.Class != OL_MAIL_ITEM:
                        continue
                    received = to_python_datetime(item.ReceivedTime)
                    if received is None:
                        continue
                    if received < start_dt:
                        if ordered:
                            break               # newest-first: everything after this is older too
                        continue
                    if received >= end_dt:
                        continue
                    info["items_seen"] += 1
                    email = _read_item(item, cfg, root, folder, received, seen_ids, keep_mail_items)
                    if email is None:
                        continue
                    all_emails.append(email)
                    if email["is_team_member"]:
                        continue                # a team member's mail is a potential reply, never a pending case
                    included.append(dict(email))
                except Exception as exc:
                    info["item_errors"] += 1
                    if ctx.debug and info["item_errors"] <= 3:
                        ctx.log(f"Skipped an unreadable item: {type(exc).__name__}: {exc}")
        except Exception as exc:
            info["item_errors"] += 1
            if ctx.debug:
                ctx.log(f"Skipped an unreadable folder: {type(exc).__name__}: {exc}")
    return all_emails, included, today


def _read_item(item, cfg, root, folder, received, seen_ids, keep_mail_items):
    """The analysis dict of one Outlook mail, or None when it is irrelevant (duplicate copy, ignored sender, neither a
    team reply nor a business mail, nothing but a signature). Cheap checks come first: ``item.Body`` is the single most
    expensive COM read."""
    sender_email = get_sender_smtp_address(item)
    internet_id = get_internet_message_id(item)
    if internet_id:                              # a mail rule can copy one mail into several folders: count it once
        if internet_id in seen_ids:
            return None
        seen_ids.add(internet_id)
    if norm_email(sender_email) in cfg.ignored_senders:
        return None
    all_to_cc, business_to = get_to_cc_and_business_addresses(item, cfg.business)
    is_team_member = norm_email(sender_email) in cfg.team_members
    if not is_team_member and not business_to:
        return None
    try:
        raw_body = str(item.Body or "")
    except Exception:
        raw_body = ""
    effective = get_effective_body_text(raw_body, cfg.forward_fallback_words)
    if not effective:
        return None
    subject = str(item.Subject or "")
    entry_id = get_entry_id(item)
    return {
        "subject": subject,
        "clean_subject": clean_subject(subject),
        "body": effective[:BODY_KEEP_CHARS],
        # the quoted history too: detection falls back to it when the newest text names no system
        "full_thread_body": strip_signature_block(raw_body).strip()[:THREAD_KEEP_CHARS],
        "sender_name": str(item.SenderName or ""),
        "sender_email": sender_email,
        "received_time": received,
        "conversation_id": get_conversation_id(item),
        "conversation_index_root": conversation_index_root(get_conversation_index(item)),
        "entry_id": entry_id,
        "message_id": internet_id or entry_id,
        "mailbox": str(root.Name),
        "folder": str(folder.FolderPath),
        "to_cc": all_to_cc,
        "business_addresses_in_to": business_to,
        "is_team_member": is_team_member,
        # a live COM object per mail makes Outlook unresponsive: only the daily report (it saves .msg files) keeps them
        "mail_item": item if keep_mail_items else None,
    }


# ---------------------------------------------------------------------------------------------------------------------
# Reply matching
# ---------------------------------------------------------------------------------------------------------------------
def build_team_reply_index(all_emails, team_members):
    """Team members' mails indexed by ConversationID, ConversationIndex root and clean subject, oldest first.
    Returns ``(by_conversation, by_conv_root, by_subject, team_replies)``."""
    by_conversation, by_root, by_subject = defaultdict(list), defaultdict(list), defaultdict(list)
    team_replies = []
    for email in all_emails:
        if norm_email(email["sender_email"]) not in team_members:
            continue
        team_replies.append(email)
        if email["conversation_id"]:
            by_conversation[email["conversation_id"]].append(email)
        if email.get("conversation_index_root"):
            by_root[email["conversation_index_root"]].append(email)
        if email["clean_subject"]:
            by_subject[email["clean_subject"]].append(email)
    for index in (by_conversation, by_root, by_subject):
        for key in index:
            index[key].sort(key=lambda x: x["received_time"])
    return by_conversation, by_root, by_subject, team_replies


def _subject_candidates(customer, by_subject):
    """Subject-fallback replies: addressed (To/Cc) back to the customer or to a business mailbox they wrote to. Team
    members often reply-all to the shared mailbox, and without this check two customers who send the same generic
    subject ("Password Reset") would be matched to each other's reply."""
    candidates = by_subject.get(customer["clean_subject"], [])
    wanted = norm_set(customer.get("business_addresses_in_to"))
    sender = norm_email(customer.get("sender_email", ""))
    if sender:
        wanted.add(sender)
    if not wanted:
        return list(candidates)                  # nothing to check against: keep the unfiltered match
    return [c for c in candidates if wanted & c.get("to_cc", set())]


def find_team_replies(customer, by_conversation, by_conv_root, by_subject):
    """``(replies, match_method)``. Priority: ConversationID, ConversationIndex root, then clean subject. Only replies
    received at or after the customer's mail count."""
    received = customer["received_time"]
    conv_id = customer["conversation_id"]
    if conv_id:
        matches = [x for x in by_conversation.get(conv_id, []) if x["received_time"] >= received]
        if matches:
            return matches, "ConversationID"
    root = customer.get("conversation_index_root", "")
    if root:
        matches = [x for x in by_conv_root.get(root, []) if x["received_time"] >= received]
        if matches:
            return matches, "ConversationIndex"
    if customer["clean_subject"]:
        matches = [x for x in _subject_candidates(customer, by_subject) if x["received_time"] >= received]
        if matches:
            return matches, "Subject"
    return [], "No Match"


def _group_customer_emails(customer_emails, by_conversation, by_conv_root, by_subject):
    """Merge repeated messages of one unanswered request (periodic "Reminder" / "تذكير") into ONE row dated at the
    EARLIEST message, so the waiting time is the real one and a short reminder is not judged "no action" on its own.
    A team reply in between makes the next message a new request. Falls back to sender + subject without a ConversationID."""
    buckets, order = defaultdict(list), []
    for email in customer_emails:
        if email.get("conversation_id"):
            key = ("conv", email["conversation_id"])
        else:
            key = ("subj", norm_email(email.get("sender_email", "")), email.get("clean_subject", ""))
        if key not in buckets:
            order.append(key)
        buckets[key].append(email)

    grouped = []
    for key in order:
        bucket = sorted(buckets[key], key=lambda e: e["received_time"])
        if key[0] == "conv":
            times = {r["received_time"] for r in by_conversation.get(key[1], [])}
            for root in {e.get("conversation_index_root") for e in bucket if e.get("conversation_index_root")}:
                times.update(r["received_time"] for r in by_conv_root.get(root, []))
            reply_times = sorted(times)
        else:
            probe = dict(bucket[0])
            probe["business_addresses_in_to"] = set().union(*(e.get("business_addresses_in_to") or set()
                                                              for e in bucket))
            reply_times = sorted(r["received_time"] for r in _subject_candidates(probe, by_subject))
        current = None
        for email in bucket:
            reply_between = current is not None and any(
                current["received_time"] < t <= email["received_time"] for t in reply_times)
            if current is None or reply_between:
                if current is not None:
                    grouped.append(current)
                current = dict(email)
                current["reminder_count"] = 0
                current["thread_messages"] = [email]      # oldest first: which one names a system?
            else:
                current["reminder_count"] += 1
                current["thread_messages"].append(email)
        if current is not None:
            grouped.append(current)
    return grouped


def _find_nearest_team_reply(customer, team_replies, max_gap_minutes=NEAREST_REPLY_MAX_GAP_MINUTES):
    """The team mail closest in time (matched or not), only when within ``max_gap_minutes`` - a diagnostic candidate."""
    if not team_replies:
        return None, None
    at = customer["received_time"]
    nearest = min(team_replies, key=lambda r: abs((r["received_time"] - at).total_seconds()))
    gap = int((nearest["received_time"] - at).total_seconds() // 60)
    return (nearest, gap) if abs(gap) <= max_gap_minutes else (None, None)


def _diagnose_unmatched_reply(customer, candidate, gap_minutes):
    """Why was the nearest team mail not accepted as the reply? Re-runs the three match checks against it alone."""
    if candidate is None:
        return "No team-member email found within 4 hours of this email."
    if gap_minutes < 0:
        return (f"Nearest team-member email is {abs(gap_minutes)} min BEFORE this customer email - unrelated, "
                "likely a different conversation, not a real reply.")
    reasons = []
    if customer.get("conversation_id") and candidate.get("conversation_id") == customer["conversation_id"]:
        reasons.append("Same ConversationID")
    root = customer.get("conversation_index_root", "")
    if root and candidate.get("conversation_index_root") == root:
        reasons.append("Same ConversationIndex")
    subject = customer.get("clean_subject", "")
    subject_reason = ""
    if subject and candidate.get("clean_subject") == subject:
        wanted = norm_set(customer.get("business_addresses_in_to"))
        if norm_email(customer.get("sender_email", "")):
            wanted.add(norm_email(customer["sender_email"]))
        if wanted & candidate.get("to_cc", set()):
            reasons.append("Subject matched AND addressed correctly")
        else:
            subject_reason = ("Subject matched, but reply's To/Cc did not include the customer's address or the shared "
                              "business address")
            reasons.append(subject_reason)
    if not reasons:
        return ("No shared ConversationID / ConversationIndex / Subject with this candidate - likely a genuinely "
                "different email, or the reply's own ConversationID/Index differs (check subject wording too, e.g. a "
                "typo).")
    if subject_reason:
        return subject_reason
    if len(reasons) == 1 and reasons[0] in ("Same ConversationID", "Same ConversationIndex"):
        return (f"{reasons[0]} - this SHOULD have matched. If it still shows Pending, re-run the report; this may be "
                "a timing edge case from the moment the scan ran.")
    return "; ".join(reasons)


# ---------------------------------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------------------------------
def classify(cfg, customer, team_override=None):
    """Team of one customer mail. Returns ``{systems, team, all_teams, classified_by, hint}``.

    Precedence: manual team override > address_team_map > system words (built-in + admin rules) > unclassified_team >
    "Unclassified". ``hint`` is what was found (the systems) or, when nothing was, a few words of the subject an admin
    could turn into a rule.
    """
    systems, team, all_teams = cfg.rules.detect_for_thread(customer.get("thread_messages") or [customer])
    classified_by = "system" if systems else "none"
    sent_to = norm_set(customer.get("business_addresses_in_to"))
    address_teams = list(dict.fromkeys(t for addr, t in cfg.address_team_map.items() if addr in sent_to))
    if address_teams:
        classified_by = "address"
        team = address_teams[0]
        systems = systems + [f"{t} (recipient address)" for t in address_teams]
        all_teams = address_teams + [t for t in all_teams if t not in address_teams]
    elif not systems and cfg.unclassified_team:
        classified_by = "default_team"
        team, all_teams = cfg.unclassified_team, [cfg.unclassified_team]
    hint = "; ".join(systems) if systems else "; ".join(candidate_words(customer.get("subject", "")))
    if team_override:
        classified_by = "override"
        team, all_teams = str(team_override), [str(team_override)]
    return {"systems": systems, "team": team, "all_teams": all_teams or [team], "classified_by": classified_by,
            "hint": hint}


def _attribute_replies(directory, replies, all_teams):
    """Which of the named teams have been answered? A reply from a member of team T covers T and the teams linked to
    it. A reply from someone not mapped to a team (or to a team that was not named) answers EVERYTHING - the old,
    simple behaviour - and is reported through the second value."""
    replied, unattributed = set(), False
    for reply in replies:
        member_team = directory.member_team(reply.get("sender_email", ""))
        covered = directory.linked_teams(member_team) & set(all_teams) if member_team else set()
        if covered:
            replied |= covered
        else:
            unattributed = True
    return replied, unattributed


# ---------------------------------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------------------------------
def _load_overrides(ctx):
    """The four manual override lists, {} when there is no store. A damaged file raises: better a failed scan (the last
    good snapshot stays) than a snapshot that silently forgets every manual decision."""
    store = getattr(ctx, "store", None)
    names = ("no_action", "team", "assignments", "quality")
    return {name: (store.overrides(name) if store else {}) for name in names}


def _minutes_between(later, earlier):
    return int((later - earlier).total_seconds() // 60)


def _pending_status(cfg, waiting_minutes):
    breached = waiting_minutes > cfg.sla_target
    return ("Breached" if breached else "Within SLA"), breached


def _new_row(cfg, customer, cls, ovr, status_info):
    key = customer.get("message_id", "")
    # a rating belongs to a real reply: a mail nobody answered carries none (the web overlay applies the same rule)
    review = (ovr["quality"].get(key) or {}) if status_info["status"] == "Replied" else {}
    assignment = ovr["assignments"].get(key) or {}
    first, last, replies = status_info["first_reply"], status_info["last_reply"], status_info["replies"]
    people_by_team = defaultdict(set)
    for reply in replies:
        team = cfg.directory.member_team(reply.get("sender_email", ""))
        if team:
            people_by_team[team].add(norm_email(reply.get("sender_email", "")))
    multi_team = next((t for t, people in people_by_team.items() if len(people) >= 2), "")
    return {
        "subject": customer["subject"],
        "customer_name": customer["sender_name"],
        "customer_email": customer["sender_email"],
        "received_time": customer["received_time"],
        "mailbox": customer["mailbox"],
        "folder": customer["folder"],
        "conversation_id": customer["conversation_id"],
        "entry_id": customer.get("entry_id", ""),
        "message_id": key,
        "row_id": key,
        "mail_item": customer.get("mail_item"),
        "detected_systems": "; ".join(cls["systems"]),
        "team": cls["team"],
        "classified_by": cls["classified_by"],
        "classification_hint": cls["hint"],
        "business_addresses_in_to": "; ".join(sorted(customer["business_addresses_in_to"])),
        "match_method": status_info["match_method"],
        "first_reply_by": first["sender_name"] if first else "",
        "first_reply_email": first["sender_email"] if first else "",
        "first_reply_time": first["received_time"] if first else None,
        # a manager's reply is counted as Replied but is never offered for quality review
        "reply_by_manager": bool(first and cfg.directory.is_manager(first["sender_email"])),
        "first_response_time": status_info["first_response"],
        "response_minutes": status_info["response_minutes"],
        "last_reply_by": last["sender_name"] if last else "",
        "last_reply_email": last["sender_email"] if last else "",
        "last_reply_time": last["received_time"] if last else None,
        "reply_count": len(replies),
        "multiple_team_reply": bool(multi_team),          # 2+ different people of one team both replied
        "multiple_team_reply_team": multi_team,
        "body_preview": customer.get("body", ""),
        "reply_body_preview": first.get("body", "") if first else "",
        "current_waiting_time": status_info["waiting"],
        "status": status_info["status"],
        "sla_minutes": cfg.sla_target,
        "sla_status": status_info["sla_status"],
        "escalated": status_info["escalated"],
        "is_carryover": status_info["is_carryover"],
        "age_days": status_info["age_days"],
        "reminder_count": customer.get("reminder_count", 0),
        "all_teams": cls["all_teams"],
        "teams_pending": status_info["teams_pending"],
        "teams_replied": sorted(status_info["teams_replied"]),
        "is_split_child": False,
        "split_sibling_teams": [],
        "diag_candidate_by": status_info["diag"]["by"],
        "diag_candidate_email": status_info["diag"]["email"],
        "diag_candidate_time": status_info["diag"]["time"],
        "diag_candidate_subject": status_info["diag"]["subject"],
        "diag_gap_minutes": status_info["diag"]["gap"],
        "diag_reason": status_info["diag"]["reason"],
        "diag_no_action_reason": status_info["no_action_reason"],
        "assigned_to_email": assignment.get("assigned_to_email", ""),
        "assigned_to_name": assignment.get("assigned_to_name", ""),
        "assigned_note": assignment.get("note", ""),
        "assigned_by": assignment.get("marked_by", ""),
        "assigned_at": assignment.get("marked_at", ""),
        "quality_reviewed_by": review.get("reviewed_by", ""),
        "quality_reviewed_at": review.get("reviewed_at", ""),
        "quality_rating": review.get("rating", ""),
        "quality_note": review.get("note", ""),
        "quality_reasons": review.get("reasons", ""),
        "quality_appeal": review.get("appeal", ""),
        "appeal_decision": review.get("appeal_decision", ""),
        "rating_withdrawn": bool(review.get("rating_withdrawn", False)),
    }


def _status_of(cfg, customer, replies, teams_replied, teams_pending, ovr, team_replies, now):
    """Status, SLA and waiting data of one customer mail (everything but the classification)."""
    first = replies[0] if replies else None
    received = customer["received_time"]
    info = {"first_reply": first, "last_reply": replies[-1] if replies else None, "replies": replies,
            "response_minutes": None, "first_response": "", "waiting": "", "no_action_reason": "", "escalated": False,
            "teams_pending": teams_pending, "teams_replied": teams_replied,
            "diag": {"by": "", "email": "", "time": None, "subject": "", "gap": None, "reason": ""}}
    if first:
        info["response_minutes"] = _minutes_between(first["received_time"], received)
        info["status"] = "Replied"
        info["first_response"] = format_duration(info["response_minutes"])
        info["sla_status"] = "Breached" if info["response_minutes"] > cfg.sla_target else "Met"
    else:
        waiting_minutes = _minutes_between(now, received)
        manual = ovr["no_action"].get(customer.get("message_id", ""))
        if manual is not None:
            info["status"] = "No Action Needed"
            info["no_action_reason"] = (f'Manually marked "No Action Needed" by {manual.get("marked_by", "someone")} '
                                        f'on {manual.get("marked_at", "")}')
        else:
            no_action, reason = cfg.rules.explain_no_action(customer.get("subject", ""), customer.get("body", ""))
            info["status"] = "No Action Needed" if no_action else "Pending Reply"
            info["no_action_reason"] = reason if no_action else ""
        info["waiting"] = format_duration(waiting_minutes)
        if info["status"] == "Pending Reply":
            info["sla_status"], info["escalated"] = _pending_status(cfg, waiting_minutes)
            candidate, gap = _find_nearest_team_reply(customer, team_replies)
            info["diag"]["reason"] = _diagnose_unmatched_reply(customer, candidate, gap)
            if candidate is not None:
                info["diag"].update(by=candidate.get("sender_name", ""), email=candidate.get("sender_email", ""),
                                    time=candidate.get("received_time"), subject=candidate.get("subject", ""), gap=gap)
        else:
            info["sla_status"] = "N/A"          # never really pending: no SLA and no team to chase
            info["teams_pending"] = []
    return info


def _split_rows(cfg, base_row, customer, replies, teams_replied, split_teams, now):
    """One row per owning team: team X can be Replied while teams Y and Z are still Pending on the SAME mail."""
    age_days = base_row["age_days"]
    children = []
    for team_name in split_teams:
        child = dict(base_row)
        child.update(team=team_name, all_teams=[team_name], is_split_child=True, split_sibling_teams=list(split_teams),
                     row_id=f"{base_row['message_id']}|{team_name}")
        if team_name in teams_replied:
            _fill_replied_child(cfg, child, customer, replies, team_name, base_row)
        else:
            waiting = _minutes_between(now, customer["received_time"])
            sla_status, escalated = _pending_status(cfg, waiting)
            child.update(
                status="Pending Reply", teams_pending=[team_name], teams_replied=[], first_reply_by="",
                first_reply_email="", first_reply_time=None, first_response_time="", response_minutes=None,
                reply_by_manager=False, current_waiting_time=format_duration(waiting), sla_status=sla_status,
                escalated=escalated, is_carryover=age_days > 0,
                # quality belongs to the reply that was given, not to a team that has not answered yet
                quality_rating="", quality_note="", quality_reasons="", quality_appeal="", appeal_decision="",
                rating_withdrawn=False, quality_reviewed_by="", quality_reviewed_at="")
        children.append(child)
    return children


def _fill_replied_child(cfg, child, customer, replies, team_name, base_row):
    child.update(status="Replied", teams_pending=[], teams_replied=[team_name], is_carryover=False, escalated=False)
    # the earliest reply that covers THIS team: two teams on one mail can be answered by two people at different times
    team_reply = next((r for r in replies
                       if team_name in cfg.directory.linked_teams(cfg.directory.member_team(r.get("sender_email", "")))),
                      None)
    if team_reply is None:
        child["sla_status"] = base_row["sla_status"] if base_row["first_reply_time"] else "Met"
        return
    minutes = _minutes_between(team_reply["received_time"], customer["received_time"])
    child.update(
        first_reply_by=team_reply.get("sender_name", ""), first_reply_email=team_reply.get("sender_email", ""),
        first_reply_time=team_reply.get("received_time"), first_response_time=format_duration(minutes),
        response_minutes=minutes, reply_body_preview=team_reply.get("body", ""),
        reply_by_manager=cfg.directory.is_manager(team_reply.get("sender_email", "")),
        sla_status="Breached" if minutes > cfg.sla_target else "Met")


def _rows_for_customer(cfg, customer, indexes, team_replies, ovr, now):
    by_conversation, by_root, by_subject = indexes
    replies, match_method = find_team_replies(customer, by_conversation, by_root, by_subject)
    key = customer.get("message_id", "")
    override = (ovr["team"].get(key) or {}).get("team") if key else None
    cls = classify(cfg, customer, override)
    teams_replied, unattributed = _attribute_replies(cfg.directory, replies, cls["all_teams"])
    teams_pending = [] if unattributed else [t for t in cls["all_teams"] if t not in teams_replied]

    info = _status_of(cfg, customer, replies, teams_replied, teams_pending, ovr, team_replies, now)
    info["match_method"] = match_method
    age_days = (now.date() - customer["received_time"].date()).days
    is_carryover = age_days > 0 and info["status"] == "Pending Reply"
    if age_days > 0 and not is_carryover:
        return []                                 # old AND resolved: not today's business
    info["is_carryover"], info["age_days"] = is_carryover, age_days
    base = _new_row(cfg, customer, cls, ovr, info)

    split_teams = [t for t in cls["all_teams"] if t and t != UNCLASSIFIED]
    can_split = info["status"] in ("Replied", "Pending Reply") and len(split_teams) >= 2 and not unattributed
    return _split_rows(cfg, base, customer, replies, teams_replied, split_teams, now) if can_split else [base]


def build_report(ctx, cfg, included_emails, all_emails, now=None):
    """Rows (one per customer mail, or per team for a mail addressed to several) and the team's own mails.

    Overrides are applied like the legacy did: a manual team beats every detection, a manual "no action" applies to a mail
    nobody replied to, assignments and quality ratings are overlaid on the row. Mails from earlier days that were
    answered are dropped; the ones still pending are flagged ``is_carryover``. Rows come oldest first.
    """
    now = now or ctx.now()
    ovr = _load_overrides(ctx)
    by_conversation, by_root, by_subject, team_replies = build_team_reply_index(all_emails, cfg.team_members)
    customers = [x for x in included_emails if norm_email(x["sender_email"]) not in cfg.team_members]
    customers = _group_customer_emails(customers, by_conversation, by_root, by_subject)
    rows = []
    for customer in customers:
        rows.extend(_rows_for_customer(cfg, customer, (by_conversation, by_root, by_subject), team_replies, ovr, now))
    rows.sort(key=lambda r: (r["received_time"], r["message_id"]))        # stable: a split mail keeps its team order
    return rows, team_replies


# ---------------------------------------------------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------------------------------------------------
def _new_team_stats():
    return {"business_emails": 0, "replied": 0, "pending": 0, "no_action_needed": 0, "escalated_count": 0,
            "carryover_count": 0, "carryover_oldest_days": 0, "response_minutes_sum": 0, "response_count": 0,
            "fastest_response_minutes": None, "sla_breached_count": 0, "team_emails_sent": 0}


def _team_stats(cfg, rows, team_replies):
    stats = {team: _new_team_stats() for team in cfg.teams}
    for row in rows:
        entry = stats.setdefault(row.get("team", UNCLASSIFIED), _new_team_stats())
        entry["business_emails"] += 1
        key = {"Replied": "replied", "Pending Reply": "pending", "No Action Needed": "no_action_needed"}.get(row["status"])
        if key:
            entry[key] += 1
        entry["escalated_count"] += bool(row.get("escalated"))
        entry["sla_breached_count"] += row.get("sla_status") == "Breached"
        if row.get("is_carryover"):
            entry["carryover_count"] += 1
            entry["carryover_oldest_days"] = max(entry["carryover_oldest_days"], row.get("age_days", 0))
        if row.get("first_reply_time"):
            minutes = _minutes_between(row["first_reply_time"], row["received_time"])
            entry["response_minutes_sum"] += minutes
            entry["response_count"] += 1
            fastest = entry["fastest_response_minutes"]
            entry["fastest_response_minutes"] = minutes if fastest is None else min(fastest, minutes)
    # "Team Emails Sent": every mail a team member sent, not only the replies matched to a row
    for reply in team_replies:
        team = cfg.directory.member_team(reply.get("sender_email", ""))
        if team:
            stats.setdefault(team, _new_team_stats())["team_emails_sent"] += 1
    return stats


def calculate_statistics(cfg, rows, team_replies, included_emails):
    """The numbers of the day. ``business_emails`` is today's intake only; carry-over is counted apart so old open
    items do not inflate it. ``reply_rate`` is a percentage of the mails that needed a reply."""
    business = sum(1 for r in rows if not r.get("is_carryover"))
    replied = sum(r["status"] == "Replied" for r in rows)
    pending = sum(r["status"] == "Pending Reply" for r in rows)
    no_action = sum(r["status"] == "No Action Needed" for r in rows)
    needed_reply = business - no_action
    # a split mail is several rows, but reply_count / match_method describe the whole mail: count each mail once
    unique_mails = list({r.get("entry_id") or r.get("message_id") or id(r): r for r in rows}.values())
    minutes = [_minutes_between(r["first_reply_time"], r["received_time"]) for r in rows if r["first_reply_time"]]

    sla_rows = [r for r in rows if r.get("sla_status") != "N/A"]
    sla_breached = sum(r.get("sla_status") == "Breached" for r in sla_rows)
    escalated = sorted((r for r in rows if r.get("escalated")), key=lambda r: r["received_time"])
    replied_rows = [r for r in rows if r["status"] == "Replied" and r["first_reply_time"]]
    slowest = sorted(replied_rows, key=lambda r: r["first_reply_time"] - r["received_time"], reverse=True)[:5]
    canonical = cfg.rules.canonical_application
    scorecard = build_quality_scorecard(rows, cfg.directory)
    top, bottom = build_quality_leaderboard(scorecard)
    repliable = sum(1 for r in rows if r["status"] == "Replied" and not r.get("reply_by_manager"))
    reviewed_repliable = sum(1 for r in rows if r.get("quality_rating") and not r.get("reply_by_manager"))

    return {
        "business_emails": business,
        "business_emails_incl_carryover": len(rows),
        "team_replies": len(team_replies),
        "replied": replied,
        "pending": pending,
        "no_action_needed": no_action,
        "needed_reply": needed_reply,
        "reply_rate": safe_percent(replied, needed_reply),
        "total_team_replies": sum(r["reply_count"] for r in unique_mails),
        "conversation_matches": sum(r["match_method"] == "ConversationID" for r in unique_mails),
        "conversation_index_matches": sum(r["match_method"] == "ConversationIndex" for r in unique_mails),
        "subject_matches": sum(r["match_method"] == "Subject" for r in unique_mails),
        "no_matches": sum(r["match_method"] == "No Match" for r in unique_mails),
        "average_response": sum(minutes) / len(minutes) if minutes else 0,
        "fastest_response": min(minutes) if minutes else 0,
        "slowest_response": max(minutes) if minutes else 0,
        "team_stats": _team_stats(cfg, rows, team_replies),
        "system_volume": Counter(canonical(s) for r in rows for s in r.get("detected_systems", "").split("; ") if s),
        "reply_breakdown": sorted(
            ({"name": name, "team": cfg.directory.member_team(email) or "Unassigned", "count": count}
             for (name, email), count in Counter(
                 (r["first_reply_by"], r.get("first_reply_email", "")) for r in rows
                 if r["status"] == "Replied" and r.get("first_reply_by")).items()),
            key=lambda d: (-d["count"], d["name"])),
        "quality_reviewed": sum(1 for r in rows if r.get("quality_rating")),
        "quality_excellent": sum(1 for r in rows if r.get("quality_rating") == "Excellent"),
        "quality_good": sum(1 for r in rows if r.get("quality_rating") == "Good"),
        "quality_needs_improvement": sum(1 for r in rows if r.get("quality_rating") == "Needs Improvement"),
        "quality_appeals": sum(1 for r in rows if r.get("quality_appeal")),
        "quality_scorecard": scorecard,
        "team_quality_index": build_team_quality_index(scorecard),
        "quality_leaderboard_top": top,
        "quality_leaderboard_bottom": bottom,
        "speed_quality_matrix": build_speed_quality_matrix(rows, cfg.directory, cfg.sla_target),
        "quality_coverage_rate": safe_percent(reviewed_repliable, repliable or 1),
        "repliable_count": repliable,
        "manager_reply_count": sum(1 for r in rows if r.get("reply_by_manager")),
        "assigned_count": sum(1 for r in rows if r.get("assigned_to_email")),
        "carryover_count": sum(1 for r in rows if r.get("is_carryover")),
        "carryover_oldest_days": max((r.get("age_days", 0) for r in rows if r.get("is_carryover")), default=0),
        "first_reply_by": Counter(r["first_reply_by"] for r in rows if r["first_reply_by"]),
        "business_address_volume": Counter(a for e in included_emails for a in e["business_addresses_in_to"]),
        "sla_target_minutes": cfg.sla_target,
        "sla_breached": sla_breached,
        "sla_compliance_rate": safe_percent(len(sla_rows) - sla_breached, len(sla_rows)),
        "escalated_count": sum(bool(r.get("escalated")) for r in rows),
        "escalated_items": [{"subject": r["subject"], "customer_name": r["customer_name"], "team": r["team"],
                             "current_waiting_time": r["current_waiting_time"]} for r in escalated[:10]],
        "top_slowest_replies": [
            {"subject": r["subject"], "customer_name": r["customer_name"], "team": r["team"],
             "first_reply_by": r["first_reply_by"], "entry_id": r.get("entry_id", ""),
             "response_minutes": _minutes_between(r["first_reply_time"], r["received_time"])} for r in slowest],
    }


# ---------------------------------------------------------------------------------------------------------------------
# History and trends (ops/history/email/<day>.json: "final" = the day's headline numbers, "quality" = its scorecard)
# ---------------------------------------------------------------------------------------------------------------------
def _day_key(day):
    return day if isinstance(day, str) else day.isoformat()


def _day_date(day):
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    try:
        return datetime.strptime(str(day), "%Y-%m-%d").date()
    except ValueError:
        return None


def _history_docs(store, key, limit):
    """{day: doc[key]} from the newest ``limit`` history day files that have ``key``. Never raises: trends are a
    nice-to-have, not worth failing a scan over."""
    out = {}
    try:
        for day in store.history_days("email")[-limit:]:
            doc = store.read_history("email", day)
            if isinstance(doc, dict) and isinstance(doc.get(key), dict):
                out[day] = doc[key]
    except Exception:
        return out
    return out


def load_stats_history(store, limit=STATS_HISTORY_FILES):
    """{day: {metric: value}} - the daily finals saved by the report."""
    return _history_docs(store, "final", limit)


def load_quality_history(store, limit=QUALITY_HISTORY_FILES):
    """{day: {person: {score, reviewed, needs, team}}}."""
    return _history_docs(store, "quality", limit)


def _extract_trend_snapshot(stats):
    return {metric: round(stats.get(metric, 0) or 0, 2) for metric in TREND_METRICS}


def _set_history_key(store, day, key, value):
    def change(doc):
        doc[key] = value
    store.update_history("email", _day_key(day), change)


def save_today_stats_history(store, day, stats):
    """Write the day's headline numbers (the trend metrics) into the history day document; returns the full history.
    Safe to call every run: the day is overwritten. Old days are pruned by ``store.prune_history`` (the runner)."""
    _set_history_key(store, day, "final", _extract_trend_snapshot(stats))
    return load_stats_history(store)


def save_today_quality_history(store, day, scorecard):
    """Write the day's per-person quality numbers; returns the full quality history."""
    _set_history_key(store, day, "quality", {
        e["name"]: {"score": e["score"], "reviewed": e["reviewed"], "needs": e["needs"], "team": e["team"]}
        for e in scorecard})
    return load_quality_history(store)


def compute_trend(today, history, stats):
    """Today against yesterday's saved numbers and the trailing 7-day average (both exclude today). A metric is None
    when there is not enough history for that comparison."""
    today = _day_date(today)
    current = _extract_trend_snapshot(stats)
    yesterday = history.get((today - timedelta(days=1)).isoformat())
    last7 = [history[d] for d in ((today - timedelta(days=n)).isoformat() for n in range(1, 8)) if d in history]

    def average(metric):
        values = [snap[metric] for snap in last7 if metric in snap]
        return sum(values) / len(values) if values else None

    def compare(value, reference):
        if reference is None:
            return None
        delta = value - reference
        return {"value": reference, "delta": delta,
                "direction": "up" if delta > 0.001 else "down" if delta < -0.001 else "flat"}

    return {"vs_yesterday": {m: compare(current[m], (yesterday or {}).get(m)) for m in TREND_METRICS},
            "vs_last7": {m: compare(current[m], average(m)) for m in TREND_METRICS}}


def build_person_quality_trends(history, days=14):
    """A per-person series of daily scores over the last ``days`` days with a direction (improving / declining /
    steady) from first to last reviewed day. One day alone never implies a trend."""
    dates = sorted(d for d in history if _day_date(d) is not None)[-days:]
    series = defaultdict(list)
    for d in dates:
        for person, info in history.get(d, {}).items():
            if info.get("reviewed"):
                series[person].append(info["score"])
    trends = []
    for person, scores in series.items():
        direction = "steady"
        if len(scores) >= 2:
            delta = scores[-1] - scores[0]
            direction = "improving" if delta >= 8 else "declining" if delta <= -8 else "steady"
        trends.append({"name": person, "points": scores, "latest": scores[-1], "first": scores[0],
                       "direction": direction, "days": len(scores)})
    order = {"declining": 0, "steady": 1, "improving": 2}
    trends.sort(key=lambda t: (order[t["direction"]], t["latest"], t["name"]))
    return trends


def build_repeat_issue_alerts(history, today, threshold=3, lookback_days=7):
    """Anyone with ``threshold``+ "Needs Improvement" ratings within the lookback window."""
    today = _day_date(today)
    cutoff = today - timedelta(days=lookback_days)
    totals = defaultdict(lambda: {"needs": 0, "team": "-", "days": 0})
    for d, day in history.items():
        when = _day_date(d)
        if when is None or when < cutoff:
            continue
        for person, info in day.items():
            if info.get("needs"):
                totals[person]["needs"] += info["needs"]
                totals[person]["team"] = info.get("team", "-")
                totals[person]["days"] += 1
    alerts = [{"name": p, "needs": v["needs"], "team": v["team"], "days": v["days"]}
              for p, v in totals.items() if v["needs"] >= threshold]
    alerts.sort(key=lambda a: (-a["needs"], a["name"]))
    return alerts


def _attach_history(ctx, cfg, stats, today):
    """Trend and quality trends from the saved history. Read-only: the live scan never writes history (the report does),
    but today's scorecard is merged in memory so the dashboard shows today too."""
    store = getattr(ctx, "store", None)
    stats["trend"], stats["quality_trends"], stats["repeat_issue_alerts"] = {}, [], []
    if store is None:
        return
    try:
        stats["trend"] = compute_trend(today, load_stats_history(store), stats)
        quality = load_quality_history(store)
        quality[today.isoformat()] = {e["name"]: {"score": e["score"], "reviewed": e["reviewed"], "needs": e["needs"],
                                                  "team": e["team"]} for e in stats["quality_scorecard"]}
        stats["quality_trends"] = build_person_quality_trends(quality)
        stats["repeat_issue_alerts"] = build_repeat_issue_alerts(quality, today, cfg.repeat_threshold,
                                                                 cfg.repeat_lookback_days)
    except Exception as exc:
        ctx.log(f"Trend history could not be used: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class ScanOutcome:
    """The in-memory result of one scan. Rows keep datetimes and (with ``keep_mail_items``) live COM references, so it
    must not be serialised: use ``to_snapshot_data``."""
    rows: list
    team_replies: list
    stats: dict
    today: date
    included_emails: list
    all_emails: list
    now: datetime = None
    config: EmailConfig = None
    scan_info: dict = field(default_factory=dict)


def scan_and_analyze(ctx, namespace=None, keep_mail_items=False):
    """Scan Outlook and analyse it. ``namespace`` None = connect to Outlook (retrying while it starts)."""
    cfg = EmailConfig(ctx.settings)
    if not cfg.business:
        raise ConfigProblem("No business e-mail address is set (Settings > E-mail > business_emails).")
    if not cfg.team_members:
        raise ConfigProblem("No team member is set (Settings > E-mail > team_members).")
    now = ctx.now()
    info = {}
    unknown = sorted(set(cfg.address_team_map) - cfg.business)
    if unknown:
        ctx.log(f"{len(unknown)} address_team_map mailbox(es) are not in business_emails, so their mail is not scanned: "
                + ", ".join(unknown))
    with OUTLOOK_LOCK:                              # one Outlook COM conversation at a time in this process
        if namespace is None:
            _outlook, namespace = connect_outlook(ctx)
        all_emails, included, today = scan_current_day(ctx, namespace, cfg, now, keep_mail_items, info=info)
    ctx.log(f"Scanned {info['items_seen']} mail(s) in {info['folders']} folder(s): {len(all_emails)} relevant, "
            f"{len(included)} business")
    rows, team_replies = build_report(ctx, cfg, included, all_emails, now)
    stats = calculate_statistics(cfg, rows, team_replies, included)
    _attach_history(ctx, cfg, stats, today)
    return ScanOutcome(rows, team_replies, stats, today, included, all_emails, now, cfg, info)


def _preview(text):
    return str(text or "")[:PREVIEW_CHARS]


def _iso(value):
    return value.isoformat(timespec="seconds") if value else None


def snapshot_row(row, now):
    """One analysis row as plain JSON: ISO strings for datetimes, no COM object, bodies cut to 300 characters."""
    received, replied_at, status = row.get("received_time"), row.get("first_reply_time"), row.get("status", "")
    duration = None
    if status == "Replied" and replied_at and received:
        duration = _minutes_between(replied_at, received)
    elif status == "Pending Reply" and received:
        duration = _minutes_between(now, received)
    # the web wording ("1 H 5 Min"); the in-memory rows keep the old reports' "1 H 5 M" for Excel and e-mails
    first_response = fmt_minutes(row.get("response_minutes")) if row.get("response_minutes") is not None else ""
    waiting = (fmt_minutes(max(0, _minutes_between(now, received))) if received and not replied_at else "")
    return {
        "subject": row.get("subject", ""),
        "entry_id": row.get("entry_id", ""),
        "message_id": row.get("message_id", ""),
        "row_id": row.get("row_id", ""),
        "detected_systems": row.get("detected_systems", ""),
        "team": row.get("team", ""),
        "all_teams": list(row.get("all_teams") or []),
        "teams_pending": list(row.get("teams_pending") or []),
        "teams_replied": list(row.get("teams_replied") or []),
        "classified_by": row.get("classified_by", "none"),
        "classification_hint": row.get("classification_hint", ""),
        "customer_name": row.get("customer_name", ""),
        "customer_email": row.get("customer_email", ""),
        "received_time": _iso(received),
        "received_time_display": received.strftime("%H:%M") if received else "",
        "mailbox": row.get("mailbox", ""),
        "folder": row.get("folder", ""),
        "business_addresses_in_to": row.get("business_addresses_in_to", ""),
        "match_method": row.get("match_method", ""),
        "first_reply_by": row.get("first_reply_by", ""),
        "first_reply_email": row.get("first_reply_email", ""),
        "first_reply_time": _iso(replied_at),
        "first_response_time": first_response,
        "response_minutes": row.get("response_minutes"),
        "duration_minutes": duration,
        "reply_count": row.get("reply_count", 0),
        "multiple_team_reply": bool(row.get("multiple_team_reply")),
        "multiple_team_reply_team": row.get("multiple_team_reply_team", ""),
        "current_waiting_time": waiting,
        "status": status,
        "sla_minutes": row.get("sla_minutes", 0),
        "sla_status": row.get("sla_status", ""),
        "escalated": bool(row.get("escalated")),
        "reminder_count": row.get("reminder_count", 0),
        "is_carryover": bool(row.get("is_carryover")),
        "age_days": row.get("age_days", 0),
        "is_split_child": bool(row.get("is_split_child")),
        "split_sibling_teams": list(row.get("split_sibling_teams") or []),
        "no_action_reason": row.get("diag_no_action_reason", ""),
        "reply_by_manager": bool(row.get("reply_by_manager")),
        "assigned_to_email": row.get("assigned_to_email", ""),
        "assigned_to_name": row.get("assigned_to_name", ""),
        "assigned_note": row.get("assigned_note", ""),
        "assigned_by": row.get("assigned_by", ""),
        "assigned_at": row.get("assigned_at", ""),
        "quality_reviewed_by": row.get("quality_reviewed_by", ""),
        "quality_reviewed_at": row.get("quality_reviewed_at", ""),
        "quality_rating": row.get("quality_rating", ""),
        "quality_note": row.get("quality_note", ""),
        "quality_reasons": row.get("quality_reasons", ""),
        "quality_appeal": row.get("quality_appeal", ""),
        "appeal_decision": row.get("appeal_decision", ""),
        "rating_withdrawn": bool(row.get("rating_withdrawn")),
        "diag_reason": row.get("diag_reason", ""),
        "diag_gap_minutes": row.get("diag_gap_minutes"),
        "diag_candidate_by": row.get("diag_candidate_by", ""),
        "diag_candidate_time": _iso(row.get("diag_candidate_time")),
        "body_preview": _preview(row.get("body_preview")),
        "reply_body_preview": _preview(row.get("reply_body_preview")),
    }


def _counter_dict(counter):
    return {str(k): v for k, v in sorted(dict(counter or {}).items(), key=lambda kv: (-kv[1], str(kv[0])))}


def _snapshot_stats(stats):
    out = {k: stats[k] for k in (
        "business_emails", "business_emails_incl_carryover", "replied", "pending", "no_action_needed", "needed_reply",
        "team_replies", "total_team_replies", "conversation_matches", "conversation_index_matches", "subject_matches",
        "no_matches", "fastest_response", "slowest_response", "sla_target_minutes", "sla_breached", "escalated_count",
        "carryover_count", "carryover_oldest_days", "assigned_count", "escalated_items", "top_slowest_replies",
        "reply_breakdown")}
    out.update(
        reply_rate=round(stats["reply_rate"], 2),
        average_response=round(stats["average_response"], 1),
        sla_compliance_rate=round(stats["sla_compliance_rate"], 2),
        team_stats={team: dict(data) for team, data in stats["team_stats"].items()},
        system_volume=_counter_dict(stats["system_volume"]),
        business_address_volume=_counter_dict(stats["business_address_volume"]),
        quality={"reviewed": stats["quality_reviewed"], "excellent": stats["quality_excellent"],
                 "good": stats["quality_good"], "needs_improvement": stats["quality_needs_improvement"],
                 "appeals": stats["quality_appeals"], "coverage_rate": round(stats["quality_coverage_rate"], 2),
                 "repliable_count": stats["repliable_count"], "manager_reply_count": stats["manager_reply_count"]},
        trend=stats.get("trend") or {})
    return out


def to_snapshot_data(outcome, ctx):
    """The ``data`` of the email snapshot (docs/HUB-DESIGN.md 5.3). JSON-safe: no COM objects, no sets, datetimes as ISO
    strings, bodies cut to 300 characters. Rows are oldest first; ``classification`` counts how mails were classified."""
    cfg = outcome.config or EmailConfig(ctx.settings)
    stats, rows = outcome.stats, [snapshot_row(r, outcome.now or ctx.now()) for r in outcome.rows]
    teams = list(cfg.teams) + sorted({r["team"] for r in rows if r["team"] and r["team"] not in cfg.teams})
    by = Counter(r["classified_by"] for r in rows)
    return {
        "date": outcome.today.isoformat(),
        "sla_target_minutes": cfg.sla_target,
        "teams": teams,
        "stats": _snapshot_stats(stats),
        "rows": rows,
        "quality": {
            "scorecard": stats["quality_scorecard"],
            "team_index": {e["team"]: {k: v for k, v in e.items() if k != "team"} for e in stats["team_quality_index"]},
            "leaderboard_top": stats["quality_leaderboard_top"],
            "leaderboard_bottom": stats["quality_leaderboard_bottom"],
            "speed_quality_matrix": stats["speed_quality_matrix"],
            "person_trends": stats.get("quality_trends") or [],
            "repeat_issue_alerts": stats.get("repeat_issue_alerts") or [],
        },
        "classification": {
            "counts": {k: by.get(k, 0) for k in ("system", "address", "default_team", "override", "none")},
            "unclassified": sum(1 for r in rows if r["team"] == UNCLASSIFIED),
            "unclassified_team": cfg.unclassified_team,
        },
    }


def history_point(outcome, ctx):
    s = outcome.stats
    return {"ts": ctx.now().isoformat(timespec="seconds"), "business_emails": s["business_emails"],
            "replied": s["replied"], "pending": s["pending"], "no_action_needed": s["no_action_needed"],
            "sla_breached": s["sla_breached"], "escalated": s["escalated_count"]}


def run_scan(ctx, namespace=None):
    """Job "email". Never raises: a failure becomes ``JobResult(ok=False, error=...)`` and the last good snapshot stays."""
    try:
        outcome = scan_and_analyze(ctx, namespace)
        data = to_snapshot_data(outcome, ctx)
    except (ConfigProblem, OutlookUnavailable) as exc:
        ctx.log(f"E-mail scan cannot run: {exc}")
        return JobResult(ok=False, error=str(exc))
    except Exception as exc:
        ctx.log(f"E-mail scan failed: {type(exc).__name__}: {exc}")
        return JobResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    s = outcome.stats
    summary = (f"{s['business_emails']} business e-mails, {s['replied']} replied, {s['pending']} pending"
               + (f", {s['escalated_count']} escalated" if s["escalated_count"] else "")
               + (f", {s['carryover_count']} carried over" if s["carryover_count"] else ""))
    return JobResult(ok=True, snapshot_data=data, snapshot_date=outcome.today.isoformat(), summary=summary,
                     history_point=history_point(outcome, ctx))
