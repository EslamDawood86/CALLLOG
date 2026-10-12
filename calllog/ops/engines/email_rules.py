"""Rules of the e-mail analysis: the domain vocabulary and the pure functions that use it.

Ported from the old Daily Team Reply Analytics script: Arabic normalisation,
"no action needed" phrases (English and Arabic), the short-reply safety net, system -> team detection, application
grouping, thread / signature stripping, the quality scorecard maths and the team directory. Nothing in here reads
Outlook, files or the clock, so every rule is testable on its own (tests/test_email_engine.py).

Business e-mails, team members, teams, managers, ignored senders, SLA minutes and the lookback days are NOT here: they
come from the e-mail settings (``ctx.settings``) and are handed to ``build_rules`` / ``Directory`` by email_engine.

Settings that tune the rules (all optional)::

    classification            (edited by the admin on System > Settings, docs/HUB-DESIGN.md 5.5)
        system_team_map           {"keyword or application": "Team"}   added to the built-in map, WINS over it
        address_team_map          {"business@mailbox": "Team"}         handled by email_engine (recipient rule)
        unclassified_team         "Team"   where mails with no detected team go instead of "Unclassified"
        extra_no_action_keywords  ["phrase", ...]   more "no reply needed" phrases, Arabic or English

    advanced                  (free-form; unknown keys are ignored)
        system_team_map           {"keyword": "Team"}      extends / overrides the built-in map (below classification)
        no_action_keywords        ["phrase", ...]          more "no reply needed" phrases
        action_signal_words       ["word", ...]            words that mean "a reply IS needed" (they block no-action)
        application_groups        {"alias": "Application"} extra aliases for the System(s) column and the volume chart
        noise_phrases             ["phrase", ...]          removed from the text before system detection
        team_priority             ["MFA Team", ...]        who wins a tie between teams (first = strongest)
        short_reply_limit         6    words; 0 turns the short-reply safety net off
        forward_fallback_words    10   a reply with fewer new words than this falls back to the quoted thread
        ignored_folder_terms      ["pgp"]                  folders whose name / path contains one of these are skipped
        scan_end_buffer_minutes   0    ignore the newest N minutes (grace period before a mail can be Pending)
        use_restrict_filter       false  ask Outlook for only the recent items first (see email_engine.scan_current_day)
        repeat_issue_threshold    3    "Needs Improvement" ratings that raise a repeat-issue alert
        repeat_issue_lookback_days 7

How a keyword is matched (same for the built-in map and for the admin's rules): Arabic letter forms are normalised first
(alef / yaa / taa marbuta variants, tatweel, diacritics, Arabic digits), several words tolerate any spacing, hyphen or
underscore between them, short Latin acronyms and every Latin keyword the admin adds are matched as WHOLE words (so "ACH"
does not fire inside "reach"), and Arabic words match anywhere because Arabic glues prefixes ("ال", "و", "ب") onto words.
"""
import json
import re
from collections import defaultdict

# ---------------------------------------------------------------------------------------------------------------------
# Domain vocabulary (plain data). Generated from the legacy script's lists; edit freely.
# ---------------------------------------------------------------------------------------------------------------------

NO_ACTION_KEYWORDS_EN = (
    "Confimed", "acknowledged", "all good now", "all set", "appreciate it", "appreciate your help",
    "appreciate your support", "case closed", "closing out", "closing the case", "closing the loop",
    "closing this thread", "confirmed, thanks", "disregard my previous email", "disregard this email", "duly noted",
    "for your information", "for your kind information", "fyi", "got it, thanks", "great, thanks",
    "greatly appreciated", "highly appreciated", "ignore my previous email", "ignore previous email",
    "issue resolved", "kindly noted", "many thanks", "many thanks for your support", "marking as resolved",
    "matter closed", "much appreciate", "much appreciated", "no action needed", "no action required",
    "no further action", "no need for further action", "noted with thanks", "noted, thank you",
    "nothing further needed", "nothing further required", "ok thanks", "okay thanks", "perfect, thanks",
    "please disregard", "received with thanks", "regards and thanks", "resolved, thank you", "thank you", "thanks",
    "thanks & regards", "thanks a lot", "thanks and regards", "understood, thank you", "we are all set",
    "we're all set", "well noted", "well received",
)

NO_ACTION_KEYWORDS_AR = (
    "الله يعطيك العافية", "برجاء التجاهل", "تجاهل الرسالة السابقة", "تسلم", "تم الإطلاع", "تم الإطلاع بالشكر",
    "تم الإغلاق", "تم الإنجاز", "تم الاستلام", "تم الاستلام بالشكر", "تم الاطلاع", "تم الاقفال", "تم الانجاز",
    "تم الحل", "تم بنجاح", "تم حل المشكلة", "تمام تسلم", "تمام شكرا", "تمام كده", "جاري الإغلاق", "جزاكم الله خيرا",
    "خلاص تمام", "شكرا", "شكرا جزيلا", "شكرا لكم", "شكراً", "لا داعي للرد", "لا يستدعي الرد", "لا يوجد إجراء مطلوب",
    "لا يوجد اجراء مطلوب", "للعلم", "للعلم فقط", "متشكرين", "مشكور", "مشكورين", "مفهوم شكرا", "يعطيك العافية",
)

ACTION_SIGNAL_WORDS = (
    "?", "any update", "any updates", "asap", "blocked", "broken", "can you", "can't", "cannot", "complaint",
    "confirm", "could you", "crash", "crashed", "does not work", "doesn't work", "down", "error", "escalate",
    "escalation", "failed", "failure", "follow up", "followup", "help", "how", "issue", "kindly", "kindly support",
    "need", "needed", "no response", "not resolved", "not working", "outage", "please", "problem", "remind",
    "reminder", "request", "required", "still waiting", "stuck", "unable", "urgent", "waiting for a response",
    "waiting for response", "when", "why", "won't work", "your support", "؟", "استفسار", "برجاء", "برجاء الافادة",
    "برجاء المتابعة", "تأكيد", "تاكيد", "تذكير", "تصعيد", "توقف", "خطأ", "شكوى", "عاجل", "عايز", "فشل", "في انتظار",
    "لا يعمل", "لسه معلق", "لسه من غير رد", "للتذكير", "لو سمحت", "متابعة", "متعطل", "متوقف", "محتاج", "محدش رد",
    "مش شغال", "مشكلة", "مطلوب", "معطل", "معلق", "معندناش رد", "من فضلك", "منتظر رد", "يرجى",
)

SYSTEM_KEYWORDS = {
    "MFA Team": [
        "Veridium", "Identity", "دعوة", "المصادقة البيومترية", "المصادقه", "المصادقة", "البيومترية", "EIAM",
        "SailPoint",
    ],
    "Flex Cube": [
        "Flex Cube", "فولت", "UAT V12", "UAT V14", "FCUB UAT V12", "FCUB", "FCUBS", "صناديق الإستثمار FCIS", "فليكس",
        "GL", "صناديق الإستثمار", "FC12", "user not authenticated", "FLEX", "FLX", "OBMA", "OBCL", "FCPROD", "FC",
        "User Authentication Failed", "UAT 14", "OBBRN",
    ],
    "Card Management": [
        "Prepaid", "DWP", "Essentis Issuer", "XCS", "TMS", "Easy Cash", "Orange DWP", "Acquirer", "ACI", "XPRIME",
        "XS PRIME", "PRIME",
    ],
    "Third Party": [
        "أنظمة البنك المركزى المصرى", "SERVICE PROVIDER", "ACH Corporate", "Deal Tracker", "Kondor", "SFTP", "DMMS",
        "RTGS", "استعراض التوقيعات المعتمدة", "corpay", "معاشات القوات المسلحة", "Efinance", "CPS", "ACH", "CCR",
        "CSD", "نظام المدفوعات", "ECSD", "CBE mortgage initiatives", "المنصة لخدمة العاملين بالخارج",
        "نظام طلعت مصطفى", "Go Green", "Metastock", "Redi", "Ivanti", "FX Platform", "Meeza", "MFS",
        "استعراض التوقيعات", "E-finance", "معاشات القوات المسلحة خاص بفرع عمارات العبور  فقط", "360T", "Gas Loan",
        "ThirdParty Security Assurance Portal", "FX Platforms", "GOV ORDER", "cheque wallet",
        "المنصة لخدمة العاملين بالخارج من خلال شركة GET", "Mezza MFS", "الغرفة المركزية لإحصاءات النقد الأجنبى",
        "IScore Cards Misuse", "Visa on Arrival", "Agent Portal",
    ],
    "Open System": [
        "Windows", "السرية الموحد", "cem", "Email Creation", "CASH MANAGMENT", "Create Sector Mail",
        "Quality MIS & Analysis", "Quality Exam and Survey", "قوائم الحظر", "السرية", "النماذج المميكنة",
        "Hard Token", "FileNet Branches", "CRM", "ارشفة", "E form", "الأهلي نت OBDX", "ميل الفرع", "ايميل الفرع",
        "الايميل", "BRANCH EMAIL", "SBX", "File Scan FFF", "الاستدعاء الالي", "File Trail", "Credit Lens",
        "Platinum Mail", "Instant Card Issuance", "ميكنة خطابات حظر السيارات", "CBE REGULATIONS",
        "Oracle Finance Access", "CXM", "ERP orcale", "ERP", "Vasco", "Taleo", "BAW LG", "نموذج 4",
        "البريد الالكترونى", "الاستدعاء", "الاوراكل", "نظام الاوراكل", "SSO", "للبريد الالكترونى",
        "البريد الاليكتروني", "الميل", "FT_Admin", "FT-Admin", "FT Admin", "Loan Calculator", "AHLY BUSINESS",
        "AHLY BUSINESS LOAN CALCULATOR", "BAW", "ECM", "IT panel",
    ],
}

APPLICATION_ALIASES = {
    "Flexcube": [
        "flexcube", "flex cube", "flex", "fcubs", "fc", "gl", "obma", "obcl", "fcprod", "flx", "oracle", "فليكس",
        "فلیکس", "الفليكس", "صناديق الاستثمار", "صناديق الاستثمار fcis", "fcis",
    ],
    "DWP": [
        "dwp", "orange dwp",
    ],
    "Essentis": [
        "essentis issuer", "essentis",
    ],
    "Prepaid Cards": [
        "prepaid",
    ],
    "PRIME": [
        "xprime", "prime",
    ],
    "XCS": [
        "xcs",
    ],
    "Easy Cash": [
        "easy cash",
    ],
    "Instant Card Issuance": [
        "instant card issuance",
    ],
    "Acquirer": [
        "acquirer",
    ],
    "Unified Secrecy": [
        "السرية", "السريه", "سرية", "سريه", "السرية الموحد", "السريه الموحد", "السرية الموحدة", "السريه الموحده",
        "سرية موحد", "سريه موحده", "نظام السرية", "نظام السريه",
    ],
    "Automation (Mekana)": [
        "ميكنة", "ميكنه", "الميكنة", "الميكنه", "نظام الميكنة", "نظام الميكنه", "المكينة", "المكينه",
        "النماذج المميكنة", "النماذج المميكنه", "نماذج مميكنة", "ميكنة خطابات حظر السيارات",
    ],
    "AhlyNet (OBDX)": [
        "obdx", "الأهلي نت obdx", "الاهلي نت obdx", "الأهلي نت", "الاهلي نت",
    ],
    "EIAM": [
        "eiam",
    ],
    "Veridium (MFA)": [
        "veridium", "veridium mobile", "المصادقة", "المصادقه", "المصادقة الثنائية", "المصادقة البيومترية", "mfa",
    ],
    "Signature Viewer": [
        "استعراض التوقيعات المعتمدة", "استعراض التوقيعات", "استعراض التوقيعات المعتمده",
    ],
    "Hard Token": [
        "hard token", "hardtoken",
    ],
    "E-Form": [
        "e form", "eform",
    ],
    "FileNet": [
        "filenet branches", "filenet",
    ],
    "ECM": [
        "ecm", "docs",
    ],
    "CRM": [
        "crm",
    ],
    "Taleo": [
        "taleo",
    ],
    "Credit Lens": [
        "credit lens",
    ],
    "Platinum Mail": [
        "platinum mail",
    ],
    "File Trail": [
        "file trail",
    ],
    "File Scan": [
        "file scan fff", "file scan",
    ],
    "Auto Retrieval": [
        "الاستدعاء الالي", "الاستدعاء الآلي",
    ],
    "CBE Systems": [
        "أنظمة البنك المركزى المصرى", "انظمة البنك المركزي المصري",
    ],
    "SBX": [
        "sbx",
    ],
    "CPS": [
        "cps",
    ],
    "ACH": [
        "ach",
    ],
    "ACI": [
        "aci",
    ],
    "TMS": [
        "tms",
    ],
    "Redi": [
        "redi",
    ],
    "BAW LG": [
        "baw lg",
    ],
    "Form 4": [
        "نموذج 4",
    ],
    "Service Provider": [
        "service provider",
    ],
}

SHORT_LATIN_ACRONYMS = frozenset((
    "ACH", "ACI", "CCR", "CPS", "CRM", "CSD", "DMMS", "DWP", "ECM", "ECSD", "EIAM", "FC", "FCPROD", "FCUBS", "FLEX",
    "FLX", "GL", "MFS", "OBCL", "OBDX", "OBMA", "PRIME", "REDI", "RTGS", "SBX", "SFTP", "TMS", "XCS", "XPRIME",
))

TEXT_NOISE_PHRASES = ("I-Flex SMS Team",)

DEFAULT_TEAM_PRIORITY = ("MFA Team", "Flex Cube", "Card Management", "Third Party", "Open System")

QUALITY_REASONS = (
    "Incomplete answer", "Wrong information", "Tone / wording", "Too slow", "Missing follow-up",
    "Not escalated properly",
)

# Keywords that need a hand-written pattern because senders type them in many ways ("flexcube", "Flex-Cube", "E form").
CUSTOM_PATTERNS = {
    "flex cube": re.compile(r"\bflex[\s\-_]*cube\b", re.IGNORECASE),
    "e form": re.compile(r"\be[\s\-_]?form\b", re.IGNORECASE),
}

SHORT_REPLY_WORD_LIMIT = 6
FORWARD_FALLBACK_WORD_LIMIT = 10
UNCLASSIFIED = "Unclassified"
RATINGS = ("Excellent", "Good", "Needs Improvement")
RATING_POINTS = {"Excellent": 100, "Good": 75, "Needs Improvement": 25}

# The mail address of some team members carries a routing prefix ("flexcube.jane@..."); the same person often replies
# from the plain address, so the prefix-less form is mapped to the team too.
TEAM_PREFIX_RE = re.compile(r"^(flexcube|cardmanagement|mfa|opensystem|thirdparty)\.+", re.IGNORECASE)

# ---------------------------------------------------------------------------------------------------------------------
# Arabic normalisation and small helpers
# ---------------------------------------------------------------------------------------------------------------------
_ARABIC_MAP = {ord(a): b for a, b in {
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي", "ی": "ي", "ک": "ك"}.items()}
_ARABIC_MAP.update({0x0640: None, 0x0670: None})                                   # tatweel, dagger alef
_ARABIC_MAP.update({cp: None for cp in range(0x064B, 0x0653)})                      # diacritics
_ARABIC_MAP.update({0x0660 + i: str(i) for i in range(10)})                         # Arabic-Indic digits
_ARABIC_MAP.update({0x06F0 + i: str(i) for i in range(10)})                         # Persian digits


def normalize_arabic(text):
    return str(text or "").translate(_ARABIC_MAP)


def norm_email(value):
    return str(value or "").strip().lower()


def norm_set(values):
    return {norm_email(v) for v in (values or ()) if norm_email(v)}


def _key(word):
    """Comparison key of a keyword: lower-case, single spaces, Arabic-normalised."""
    return normalize_arabic(" ".join(str(word or "").lower().split()))


def to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_percent(numerator, denominator):
    return 0.0 if denominator == 0 else numerator / denominator * 100.0


def format_duration(minutes):
    """90 -> '1 H 30 M', 1500 -> '1 D 1 H', 5 -> '5 Min' (the wording the old reports used)."""
    minutes = max(0, int(minutes))
    days, hours, mins = minutes // 1440, (minutes % 1440) // 60, minutes % 60
    if days:
        return f"{days} D {hours} H"
    if hours:
        return f"{hours} H {mins} M"
    return f"{mins} Min"


# ---------------------------------------------------------------------------------------------------------------------
# Subject helpers
# ---------------------------------------------------------------------------------------------------------------------
# Help-desk systems stamp a new reference number on every message of the same request; it is removed for MATCHING only.
_SUBJECT_REFERENCE_NOISE = [
    re.compile(r"[\[\(]\s*(sr|ticket|case|ref|reference|incident|req|request)\s*[:#\-]?\s*\d+\s*[\]\)]", re.IGNORECASE),
    re.compile(r"\b(sr|ticket|case|ref|reference|incident|req|request)\s*(no\.?|number)?\s*[:#\-]?\s*\d{3,}\b",
               re.IGNORECASE),
    re.compile(r"#\d{3,}\b"),
]
# RE:, RE :, RE[2]:, FW:, FWD:, TR:, and the Arabic equivalents, in any stack / order.
_SUBJECT_PREFIX = re.compile(r"^\s*(re|fw|fwd|tr|رد|تحويل|اعاده توجيه|إعادة توجيه)\s*(\[\s*\d+\s*\])?\s*:\s*",
                             re.IGNORECASE)


def clean_subject(subject):
    """Subject reduced to what identifies the request: no RE:/FW: stack, no ticket numbers, Arabic-normalised."""
    text = str(subject or "").strip().lower()
    for pattern in _SUBJECT_REFERENCE_NOISE:
        text = pattern.sub(" ", text)
    text = text.strip()
    while True:
        shorter = _SUBJECT_PREFIX.sub("", text).strip()
        if shorter == text:
            break
        text = shorter
    text = re.sub(r"[\[\]\(\)]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—:.,!؟?")
    return normalize_arabic(text)


# ---------------------------------------------------------------------------------------------------------------------
# Latest-message extraction: only the text typed in THIS e-mail, not the quoted history or the signature
# ---------------------------------------------------------------------------------------------------------------------
_THREAD_BOUNDARY = [
    re.compile(r"^-{3,}\s*(original message|forwarded message)\s*-{3,}", re.IGNORECASE),
    re.compile(r"^_{10,}\s*$"),
    re.compile(r"^from:\s.+", re.IGNORECASE),
    re.compile(r"^sent:\s.+", re.IGNORECASE),
    re.compile(r"^on\s.{3,120}wrote:\s*$", re.IGNORECASE),
    re.compile(r"^من:\s.+"),
    re.compile(r"^أرسل:\s.+"),
    re.compile(r"^إلى:\s.+"),
    re.compile(r"^بتاريخ:\s.+"),
    re.compile(r"^في\s.{3,120}كتب:\s*$"),
    re.compile(r"^>"),
]
_SIGNATURE_START = [
    re.compile(r"^--\s*$"),
    re.compile(r"^(best|kind|warm|warmest)\s+regards[,.]?\s*$", re.IGNORECASE),
    re.compile(r"^regards[,.]?\s*$", re.IGNORECASE),
    re.compile(r"^thanks\s*(&|and)\s*regards[,.]?\s*$", re.IGNORECASE),
    re.compile(r"^(many\s+)?thanks[,.]?\s*$", re.IGNORECASE),
    re.compile(r"^best[,.]?\s*$", re.IGNORECASE),
    re.compile(r"^(yours\s+)?(sincerely|faithfully)[,.]?\s*$", re.IGNORECASE),
    re.compile(r"^sent from my\s.+", re.IGNORECASE),
    re.compile(r"^مع\s+(خالص\s+)?(تحياتي|التحية|تحياتنا)[،,]?\s*$"),
    re.compile(r"^وتفضلوا بقبول (فائق|خالص) الاحترام.*$"),
    re.compile(r"^أطيب\s+التحيات[،,]?\s*$"),
    re.compile(r"^مع\s+تحيات[،,]?\s*$"),
]
# Mail-gateway banners that can be the whole "body" of an otherwise empty e-mail.
_PLACEHOLDER_LINE = [
    re.compile(r"^confidential(\s+email)?[.!]?\s*$", re.IGNORECASE),
    re.compile(r"^this (email|message) is confidential.*$", re.IGNORECASE),
    re.compile(r"^بريد سري[.!]?\s*$"),
    re.compile(r"^رسالة سرية[.!]?\s*$"),
    re.compile(r"^classification:.*$", re.IGNORECASE),
    re.compile(r"^\[?(internal|public|restricted)\]?\s*$", re.IGNORECASE),
]


def _cut_at_first(text, patterns):
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and any(p.match(stripped) for p in patterns):
            return "\n".join(lines[:index])
    return str(text or "")


def strip_quoted_thread(raw_body):
    """Everything before the first quoted-thread marker ("-----Original Message-----", "From:", "> ...")."""
    return _cut_at_first(raw_body, _THREAD_BOUNDARY)


def strip_signature_block(text):
    """Everything before the first line that starts a signature ("Regards,", "--", "مع تحياتي", ...)."""
    return _cut_at_first(text, _SIGNATURE_START)


def strip_placeholder_lines(text):
    kept = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped and any(p.match(stripped) for p in _PLACEHOLDER_LINE):
            continue
        kept.append(line)
    return "\n".join(kept)


def extract_latest_message_text(raw_body):
    new_message = strip_signature_block(strip_quoted_thread(raw_body))
    return strip_placeholder_lines(new_message).strip()


def get_effective_body_text(raw_body, fallback_words=FORWARD_FALLBACK_WORD_LIMIT):
    """The text to analyse: this e-mail's own new text; when that is too short to mean anything (a bare forward,
    "kindly your support"), the fuller body with the quoted request underneath. "" = nothing usable at all."""
    latest = extract_latest_message_text(raw_body)
    if len(latest.split()) > fallback_words:
        return latest
    fuller = strip_signature_block(str(raw_body or "")).strip()
    return fuller if len(fuller) > len(latest) else latest


# ---------------------------------------------------------------------------------------------------------------------
# Phrase matching
# ---------------------------------------------------------------------------------------------------------------------
_BOUNDARY_CHARS = r"\s\.,!؟?\-:;\(\)\[\]\"'،؛"
_ATTACHED_PREFIXES = "وفبلك"      # Arabic proclitics that may be glued to a word with no space ("وشكرا")


def compile_exact_phrase(phrase):
    """A phrase counts only as a standalone word / phrase, never as letters hiding inside a longer word."""
    return re.compile(r"(?:^|[" + _BOUNDARY_CHARS + r"])(?:[" + _ATTACHED_PREFIXES + r"])?" + re.escape(phrase)
                      + r"(?:$|[" + _BOUNDARY_CHARS + r"])")


_WHOLE_WORD_BEFORE = r"(?<![A-Za-z0-9_])"
_WHOLE_WORD_AFTER = r"(?![A-Za-z0-9_])"


def _system_pattern(keyword, strict=False):
    """Regex for one system keyword. ``strict`` (admin rules): Latin edges must be whole-word edges."""
    custom = CUSTOM_PATTERNS.get(_key(keyword))
    if custom is not None:
        return custom
    words = normalize_arabic(keyword).split()
    body = r"[\s\-_]*".join(re.escape(w) for w in words)
    if len(words) == 1 and keyword.isascii() and (strict or keyword.upper() in SHORT_LATIN_ACRONYMS):
        edge_before, edge_after = (_WHOLE_WORD_BEFORE, _WHOLE_WORD_AFTER) if strict else (r"\b", r"\b")
        return re.compile(edge_before + body + edge_after, re.IGNORECASE)
    if strict:
        first, last = words[0][0], words[-1][-1]
        body = (_WHOLE_WORD_BEFORE if first.isascii() and first.isalnum() else "") + body \
            + (_WHOLE_WORD_AFTER if last.isascii() and last.isalnum() else "")
    return re.compile(body, re.IGNORECASE)


# Words too generic to suggest as a rule keyword when a mail stays unclassified.
_HINT_STOPWORDS = frozenset((
    "re", "fw", "fwd", "tr", "the", "and", "for", "with", "from", "this", "that", "please", "kindly", "request",
    "issue", "problem", "error", "urgent", "help", "need", "access", "user", "users", "new", "update", "reply", "mail",
    "email", "team", "dear", "all", "any", "not", "can", "could", "you", "your", "our", "has", "have", "was", "are",
    "check", "regarding", "about", "working", "unable", "cannot", "fix", "open",
    "برجاء", "يرجى", "من", "في", "على", "الى", "إلى", "عن", "هذا", "هذه", "مع", "لو", "سمحت", "ممكن", "مشكلة", "طلب",
    "عاجل", "السادة", "تحية", "الفريق", "الخاص", "الخاصة", "بخصوص", "موضوع"))


def candidate_words(subject, limit=6):
    """Words of a subject an admin could turn into a rule (acronyms, names, Arabic nouns). A hint, not a decision."""
    text = _SUBJECT_PREFIX.sub("", str(subject or ""))
    seen, out = set(), []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_.\-]+|[؀-ۿ]{3,}", text):
        token = token.strip(".-_")
        low = token.lower()
        if len(token) < 2 or low in _HINT_STOPWORDS or low in seen:
            continue
        seen.add(low)
        out.append(token)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The compiled rules
# ---------------------------------------------------------------------------------------------------------------------
class Rules:
    """Compiled no-action, action-signal and system / team rules for one set of e-mail settings."""

    def __init__(self, classification=None, advanced=None):
        classification = classification if isinstance(classification, dict) else {}
        advanced = advanced if isinstance(advanced, dict) else {}
        self.team_priority = tuple(advanced.get("team_priority") or DEFAULT_TEAM_PRIORITY)
        self.short_reply_limit = max(0, to_int(advanced.get("short_reply_limit"), SHORT_REPLY_WORD_LIMIT))
        self._build_no_action(classification, advanced)
        self._build_applications(advanced)
        self._build_systems(classification, advanced)
        noise = list(TEXT_NOISE_PHRASES) + [str(p) for p in (advanced.get("noise_phrases") or []) if str(p).strip()]
        self._noise = re.compile("|".join(re.escape(p) for p in noise), re.IGNORECASE) if noise else None

    # ------------------------------------------------------------------ no action needed
    def _build_no_action(self, classification, advanced):
        phrases = set(NO_ACTION_KEYWORDS_EN) | set(NO_ACTION_KEYWORDS_AR)
        phrases |= {str(p) for p in classification.get("extra_no_action_keywords") or []}
        phrases |= {str(p) for p in advanced.get("no_action_keywords") or []}
        keys = {normalize_arabic(p.lower().strip()) for p in phrases if p and p.strip()}
        # longest first, then alphabetical: deterministic, and the reason names the most specific phrase
        self._no_action = [(k, compile_exact_phrase(k)) for k in sorted(keys, key=lambda k: (-len(k), k))]
        signals = {normalize_arabic(w.lower()) for w in ACTION_SIGNAL_WORDS}
        signals |= {normalize_arabic(str(w).lower().strip()) for w in advanced.get("action_signal_words") or []
                    if str(w).strip()}
        self._signal_symbols = {w for w in signals if not any(c.isalpha() for c in w)}
        self._signal_patterns = [compile_exact_phrase(w) for w in sorted(signals - self._signal_symbols)]

    def has_action_signal(self, text):
        """``text`` already lower-cased and Arabic-normalised."""
        if any(symbol in text for symbol in self._signal_symbols):
            return True
        return any(p.search(text) for p in self._signal_patterns)

    def is_short_no_action_reply(self, subject, body):
        """A very short message with no request / question signal in it."""
        text = f"{subject or ''} {body or ''}".strip()
        if not text or not self.short_reply_limit:
            return False
        if self.has_action_signal(normalize_arabic(text.lower())):
            return False
        return len(text.split()) <= self.short_reply_limit

    def explain_no_action(self, subject, body):
        """(is_no_action, reason). A closing phrase counts only when no request / question signal is in the mail."""
        combined = normalize_arabic(f"{subject or ''} {body or ''}".lower())
        signal = self.has_action_signal(combined)
        for keyword, pattern in self._no_action:
            if pattern.search(combined):
                if signal:
                    return False, (f'Matched closing phrase "{keyword}", but an action-request signal was also '
                                   "present - kept as Pending instead.")
                return True, f'Matched closing phrase "{keyword}"'
        if self.is_short_no_action_reply(subject, body):
            words = len(f"{subject or ''} {body or ''}".strip().split())
            return True, f"Short-message safety net ({words} word(s), no request/question signal detected)"
        return False, ""

    def is_no_action_needed(self, subject, body):
        return self.explain_no_action(subject, body)[0]

    # ------------------------------------------------------------------ applications
    def _build_applications(self, advanced):
        self._app_exact, self._app_norm, self._app_stripped = {}, {}, {}
        aliases = [(a, app) for app, group in APPLICATION_ALIASES.items() for a in group]
        aliases += [(str(a), str(app)) for a, app in (advanced.get("application_groups") or {}).items()]
        for alias, app in aliases:
            low = alias.strip().lower()
            norm = normalize_arabic(low)
            self._app_exact.setdefault(low, app)
            self._app_norm.setdefault(norm, app)
            self._app_stripped.setdefault(norm[2:] if norm.startswith("ال") else norm, app)
        self._app_cache = {}

    def canonical_application(self, system_name):
        """One detected keyword -> the application it belongs to ("FCUBS", "GL" -> "Flexcube"); unknown ones as typed."""
        raw = str(system_name or "").strip()
        if raw in self._app_cache:
            return self._app_cache[raw]
        low = raw.lower()
        norm = normalize_arabic(low)
        stripped = norm[2:] if norm.startswith("ال") else norm
        app = (self._app_exact.get(low) or self._app_norm.get(norm) or self._app_stripped.get(stripped) or raw)
        self._app_cache[raw] = app
        return app

    # ------------------------------------------------------------------ systems and teams
    def _build_systems(self, classification, advanced):
        builtin = {}
        for team, words in SYSTEM_KEYWORDS.items():
            for word in words:
                builtin[_key(word)] = (word, team)
        for word, team in (advanced.get("system_team_map") or {}).items():
            if str(word).strip() and team:
                builtin[_key(word)] = (str(word).strip(), team)
        mine = {}
        for word, team in (classification.get("system_team_map") or {}).items():
            if str(word).strip() and team:
                mine[_key(word)] = (str(word).strip(), team)
        for key in mine:                 # an admin rule replaces the built-in rule for the same keyword
            builtin.pop(key, None)
        self._builtin = [(kw, team, _system_pattern(kw)) for kw, team in builtin.values()]
        self._mine = [(kw, team, _system_pattern(kw, strict=True)) for kw, team in mine.values()]

    def _strip_noise(self, text):
        text = str(text or "")
        return self._noise.sub(" ", text) if self._noise else text

    @staticmethod
    def _scan(patterns, text):
        return [(kw, team) for kw, team, pattern in patterns if pattern.search(text)]

    def _pick_team(self, matches):
        """Owner of a set of matches: the team with the most DISTINCT applications; a tie goes by team priority."""
        if not matches:
            return None
        apps = defaultdict(set)
        for keyword, team in matches:
            apps[team].add(self.canonical_application(keyword))
        best = max(len(v) for v in apps.values())
        leaders = [t for t, v in apps.items() if len(v) == best]
        if len(leaders) == 1:
            return leaders[0]
        for team in self.team_priority:
            if team in leaders:
                return team
        return sorted(leaders)[0]

    def detect(self, subject, body):
        """Scan Subject + Body for system names. Returns ``(systems, primary_team, all_teams)``.

        The BODY decides the team (the subject is often stale or generic); the subject is used only when the body names
        nothing. An admin rule beats a built-in one. ``all_teams`` is every team named anywhere, strongest first.
        """
        subject_text = normalize_arabic(self._strip_noise(subject))
        body_text = normalize_arabic(self._strip_noise(body))
        mine_body, mine_subject = self._scan(self._mine, body_text), self._scan(self._mine, subject_text)
        built_body, built_subject = self._scan(self._builtin, body_text), self._scan(self._builtin, subject_text)
        everything = mine_body + built_body + mine_subject + built_subject
        if not everything:
            return [], UNCLASSIFIED, []
        systems, seen = [], set()
        for keyword, _team in everything:
            app = self.canonical_application(keyword)
            if app not in seen:
                seen.add(app)
                systems.append(app)
        primary = (self._pick_team(mine_body) or self._pick_team(mine_subject)
                   or self._pick_team(built_body) or self._pick_team(built_subject))
        named = {team for _kw, team in everything}
        all_teams = [t for t in self.team_priority if t in named] + sorted(named - set(self.team_priority))
        return systems, primary, all_teams

    def detect_for_thread(self, thread_messages):
        """A reminder like "any update?" names nothing, but an earlier message of the same unanswered thread does:
        try each message's own text first (oldest first), then the quoted history underneath them."""
        if not thread_messages:
            return [], UNCLASSIFIED, []
        last = ([], UNCLASSIFIED, [])
        for message in thread_messages:
            last = self.detect(message.get("subject", ""), message.get("body", ""))
            if last[0]:
                return last
        for message in thread_messages:
            quoted = message.get("full_thread_body", "")
            if quoted:
                result = self.detect(message.get("subject", ""), quoted)
                if result[0]:
                    return result
        return last


def build_rules(email_settings):
    """Rules for an e-mail settings section (``ctx.settings``). Cached: compiling ~250 patterns once per setting set."""
    settings = email_settings or {}
    key = json.dumps([settings.get("classification"), settings.get("advanced")], sort_keys=True, default=str)
    rules = _RULES_CACHE.get(key)
    if rules is None:
        if len(_RULES_CACHE) > 8:
            _RULES_CACHE.clear()
        rules = _RULES_CACHE[key] = Rules(settings.get("classification"), settings.get("advanced"))
    return rules


_RULES_CACHE = {}


# ---------------------------------------------------------------------------------------------------------------------
# Team directory: who belongs to which team
# ---------------------------------------------------------------------------------------------------------------------
class Directory:
    """Team members, their teams, linked teams and managers (from the e-mail settings)."""

    def __init__(self, team_members=(), members_by_team=None, link_groups=None, managers=()):
        self.members_by_team = {team: norm_set(members) for team, members in (members_by_team or {}).items()}
        self.members = norm_set(team_members)
        for members in self.members_by_team.values():
            self.members |= members
        self.managers = norm_set(managers)
        self.link_groups = {team: set(group or ()) for team, group in (link_groups or {}).items()}
        self._lookup = self._build_lookup()

    def _build_lookup(self):
        lookup, stripped = {}, []
        for team, members in self.members_by_team.items():       # an address a team lists itself always wins
            for member in sorted(members):
                lookup.setdefault(member, team)
                plain = norm_email(TEAM_PREFIX_RE.sub("", member))
                if plain != member:
                    stripped.append((plain, team))
        for plain, team in stripped:
            lookup.setdefault(plain, team)
        return lookup

    def member_team(self, email):
        """Team of a member, or None when the address is not mapped to a specific team (a "generic" reply)."""
        return self._lookup.get(norm_email(email))

    def linked_teams(self, team):
        """Teams that share reply credit with ``team`` (always includes ``team`` itself)."""
        return {team} | self.link_groups.get(team, set())

    def is_manager(self, email):
        return norm_email(email) in self.managers

    def people(self):
        """[{name, email, team}] sorted by name - the "Assign to..." list."""
        seen, people = set(), []
        for team, members in self.members_by_team.items():
            for raw in sorted(members):
                if raw not in seen:
                    seen.add(raw)
                    people.append({"name": display_name_from_email(raw), "email": raw, "team": team})
        for raw in sorted(self.members - seen):
            people.append({"name": display_name_from_email(raw), "email": raw, "team": ""})
        people.sort(key=lambda p: (p["name"].lower(), p["email"]))
        return people


def display_name_from_email(raw_email):
    """'flexcube.jane.doe@x' -> 'Jane Doe' (routing prefix removed, local part made readable)."""
    local = str(raw_email or "").split("@")[0]
    local = TEAM_PREFIX_RE.sub("", local).strip(" .")
    name = " ".join(w.capitalize() for w in re.sub(r"[._\-]+", " ", local).split())
    return name or str(raw_email or "")


# ---------------------------------------------------------------------------------------------------------------------
# Quality scorecard maths (rows -> numbers). Ratings are read from the quality overrides by email_engine.
# ---------------------------------------------------------------------------------------------------------------------
def _reviewed(row):
    """A rating counts unless its appeal was accepted (withdrawn) or a manager wrote the reply."""
    return bool(row.get("quality_rating")) and not row.get("rating_withdrawn") and not row.get("reply_by_manager")


def _person_team(row, directory):
    return directory.member_team(row.get("first_reply_email", "")) or row.get("team", "-")


def _points(rating):
    return RATING_POINTS.get(rating, RATING_POINTS["Needs Improvement"])


def build_quality_scorecard(rows, directory):
    """Per-person reviewed counts and score (Excellent 100, Good 75, Needs Improvement 25), worst first."""
    people = {}
    for row in rows:
        if not _reviewed(row):
            continue
        person = row.get("first_reply_by", "") or "Unknown"
        entry = people.setdefault(person, {"name": person, "team": "", "reviewed": 0, "excellent": 0, "good": 0,
                                           "needs": 0})
        entry["team"] = _person_team(row, directory)
        entry["reviewed"] += 1
        rating = row["quality_rating"]
        entry["excellent" if rating == "Excellent" else "good" if rating == "Good" else "needs"] += 1
    scorecard = []
    for entry in people.values():
        total = entry["excellent"] * 100 + entry["good"] * 75 + entry["needs"] * 25
        entry["score"] = round(total / (entry["reviewed"] or 1), 1)
        scorecard.append(entry)
    scorecard.sort(key=lambda e: (e["score"], -e["reviewed"], e["name"]))
    return scorecard


def build_team_quality_index(scorecard):
    """0-100 quality index per team, weighted by review count, best team first."""
    teams = {}
    for e in scorecard:
        t = teams.setdefault(e["team"], {"weighted": 0.0, "reviewed": 0, "excellent": 0, "good": 0, "needs": 0})
        t["weighted"] += e["score"] * e["reviewed"]
        for k in ("reviewed", "excellent", "good", "needs"):
            t[k] += e[k]
    index = [{"team": team, "index": round(d["weighted"] / d["reviewed"], 1), "reviewed": d["reviewed"],
              "excellent": d["excellent"], "good": d["good"], "needs": d["needs"]}
             for team, d in teams.items() if d["reviewed"]]
    index.sort(key=lambda x: (-x["index"], x["team"]))
    return index


def build_quality_leaderboard(scorecard, min_reviews=2):
    """(top, bottom): people with at least ``min_reviews`` reviews, so nobody ranks off one lucky review."""
    eligible = [e for e in scorecard if e["reviewed"] >= min_reviews]
    if not eligible:
        return [], []
    ranked = sorted(eligible, key=lambda e: (-e["score"], -e["reviewed"], e["name"]))
    n = len(ranked)
    if n <= 1:
        return ranked, []
    top_n = min(3, n // 2) if n < 6 else 3          # with few people "top" must not swallow everyone
    top = ranked[:top_n]
    bottom = [e for e in reversed(ranked) if e not in top][:3]
    return top, bottom


def build_speed_quality_matrix(rows, directory, sla_target_minutes):
    """Each reviewed person as fast / slow (average first reply vs the SLA) and high / low quality (>= 75)."""
    people = {}
    for row in rows:
        if not _reviewed(row) or row.get("response_minutes") is None:
            continue
        person = row.get("first_reply_by", "") or "Unknown"
        a = people.setdefault(person, {"name": person, "team": "", "minutes": [], "score": 0, "n": 0})
        a["team"] = _person_team(row, directory)
        a["minutes"].append(row["response_minutes"])
        a["score"] += _points(row["quality_rating"])
        a["n"] += 1
    out = []
    for a in people.values():
        avg_minutes = sum(a["minutes"]) / len(a["minutes"])
        avg_score = a["score"] / a["n"]
        fast, good = avg_minutes <= sla_target_minutes, avg_score >= 75
        quadrant = ("Fast & High Quality" if fast and good else "Fast but Low Quality" if fast
                    else "Slow but High Quality" if good else "Slow & Low Quality")
        out.append({"name": a["name"], "team": a["team"], "avg_minutes": round(avg_minutes),
                    "avg_score": round(avg_score, 1), "quadrant": quadrant})
    out.sort(key=lambda x: (x["quadrant"] != "Slow & Low Quality", x["quadrant"] != "Fast but Low Quality", x["name"]))
    return out
