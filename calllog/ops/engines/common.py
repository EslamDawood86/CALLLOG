"""Shared pieces for the collector engines: job context/result, credentials, Outlook mail sender, small helpers.

Nothing here may import win32com / playwright / keyring at module level (they are imported inside the functions
that need them), so engines stay importable and testable on any OS.
"""
import html
import os
import re
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime

# One Outlook COM conversation at a time per process (Outlook automation is not thread-safe).
OUTLOOK_LOCK = threading.RLock()

# NBE brand colours used by the e-mails and Excel files.
NBE_GREEN = "#00573F"
NBE_GREEN_LIGHT = "#E6F0EC"
NBE_GOLD = "#C8A45D"
FONT_STACK = "Segoe UI,Calibri,Arial,sans-serif"

KEYRING_SERVICES = {"sla": "nbe-sso-sla-monitor", "itsm": "nbe-itsm"}
PASSWORD_ENV = {"sla": "NBE_SSO_PASSWORD", "itsm": "ITSM_PASS"}


class MailError(RuntimeError):
    pass


class CredentialError(RuntimeError):
    pass


@dataclass
class JobResult:
    """What a job hands back to the runner. The runner writes the snapshot, the history point and the heartbeat."""
    ok: bool = True
    snapshot_data: dict = None        # the "data" part of the snapshot (None = do not write a snapshot)
    snapshot_date: str = None         # YYYY-MM-DD the data is about (default: today)
    summary: str = ""                 # one line for the System page, e.g. "31 open, 4 breached"
    error: str = None
    error_kind: str = None            # "auth" = wrong/expired password: the runner stops retrying (account lock-out protection)
    emails_sent: int = 0
    history_point: dict = None        # appended to ops/history/<module>/<day>.json
    history_extra: dict = None        # merged into that day document
    skipped: bool = False             # nothing to do (e.g. report already sent today)


@dataclass
class JobContext:
    job: str                          # email | email_report | sla | itsm
    settings: dict                    # this module's section of the settings
    general: dict                     # settings["general"]
    store: object                     # OpsStore
    now: callable = datetime.now
    log: callable = print
    mailer: object = None             # OutlookMailer (or a fake in tests)
    debug: bool = False
    dry_run: bool = False             # scan but write/send nothing
    no_email: bool = False
    host: str = field(default_factory=socket.gethostname)
    data_dir: str = ""
    headed: bool = False              # show the browser (SLA / ITSM) for troubleshooting
    options: dict = field(default_factory=dict)   # per-run extras, e.g. {"send_mail": True} from "Run now", {"requested_by": "admin"}
    credentials: callable = None      # credentials(kind) -> password; default get_password

    def password(self, kind, username):
        fn = self.credentials or get_password
        return fn(kind, username)

    def can_email(self):
        return not (self.no_email or self.dry_run)


# ------------------------------------------------------------------ credentials
def get_password(kind, username):
    """Password for 'sla' or 'itsm': environment variable first, then Windows Credential Manager. Never prompts."""
    env = os.environ.get(PASSWORD_ENV[kind])
    if env:
        return env
    try:
        import keyring
        pw = keyring.get_password(KEYRING_SERVICES[kind], str(username))
        if pw:
            return pw
    except Exception:
        pass
    raise CredentialError(
        f"No saved password for the {kind.upper()} account '{username}'. On the collector PC run:  "
        f"python collector.py --set-password {kind}")


def set_password(kind, username, password):
    import keyring
    keyring.set_password(KEYRING_SERVICES[kind], str(username), password)


# ---------------------------------------------------------------------- helpers
def esc(value):
    return "" if value is None else html.escape(str(value))


def fmt_minutes(minutes):
    """90 -> '1 H 30 Min' (the format the old e-mail script used in its tables)."""
    if minutes is None:
        return "-"
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} Min"
    h, m = divmod(minutes, 60)
    return f"{h} H {m} Min" if m else f"{h} H"


def norm_email(value):
    return (value or "").strip().lower()


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def norm_arabic(text):
    """Normalise Arabic text for matching (tatweel, diacritics, alef/yaa/taa marbuta variants, digits, spaces)."""
    s = str(text or "").translate(_ARABIC_DIGITS).replace("ـ", "")
    s = re.sub(r"[ً-ْ]", "", s)
    s = re.sub("[إأآ]", "ا", s).replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", s).strip().lower()


# --------------------------------------------------------------------- Outlook
class OutlookMailer:
    """Sends HTML mail through Outlook desktop (COM). Safe to call from any collector thread."""

    def __init__(self, display_only=False):
        self.display_only = display_only

    def _outlook(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()          # every thread needs its own COM apartment
        return win32com.client.Dispatch("Outlook.Application")

    def my_address(self):
        with OUTLOOK_LOCK:
            outlook = self._outlook()
            ns = outlook.GetNamespace("MAPI")
            for getter in (lambda: ns.CurrentUser.AddressEntry.GetExchangeUser().PrimarySmtpAddress,
                           lambda: ns.Accounts.Item(1).SmtpAddress):
                try:
                    value = getter()
                    if value:
                        return value
                except Exception:
                    pass
        raise MailError("Could not find your own e-mail address in Outlook - put a recipient in the settings.")

    def send(self, to, cc=(), subject="", html_body="", attachments=(), inline_images=None, display_only=None,
             bcc=()):
        """``inline_images``: {content_id: path} referenced in the body as <img src="cid:content_id">."""
        to, cc, bcc = [a for a in to if a], [a for a in cc if a], [a for a in bcc if a]
        if not (to or cc or bcc):
            raise MailError("No recipients.")
        display = self.display_only if display_only is None else display_only
        with OUTLOOK_LOCK:
            outlook = self._outlook()
            mail = outlook.CreateItem(0)
            mail.To = "; ".join(to)
            if cc:
                mail.CC = "; ".join(cc)
            if bcc:
                mail.BCC = "; ".join(bcc)
            mail.Subject = subject
            for cid, path in (inline_images or {}).items():
                if path and os.path.exists(path):
                    att = mail.Attachments.Add(path, 1, 0)
                    pa = att.PropertyAccessor
                    pa.SetProperty("http://schemas.microsoft.com/mapi/proptag/0x3712001F", cid)
                    pa.SetProperty("http://schemas.microsoft.com/mapi/proptag/0x7FFE000B", True)
            mail.HTMLBody = html_body
            for path in attachments:
                if path and os.path.exists(path):
                    mail.Attachments.Add(str(path))
            if display:
                mail.Display()
            else:
                mail.Send()


class FakeMailer:
    """Records mails instead of sending them (tests, --dry-run)."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def my_address(self):
        return "me@nbe.com.eg"

    def send(self, to, cc=(), subject="", html_body="", attachments=(), inline_images=None, display_only=None, bcc=()):
        if self.fail:
            raise MailError("simulated Outlook failure")
        self.sent.append({"to": list(to), "cc": list(cc), "bcc": list(bcc), "subject": subject, "html": html_body,
                          "attachments": list(attachments), "inline_images": dict(inline_images or {})})
