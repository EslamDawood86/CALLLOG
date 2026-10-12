"""A small fake of the Outlook object model, just what calllog/ops/engines/email_engine.py reads.

FakeNamespace -> FakeFolder (sub-folders, Items) -> FakeItems (Sort / Restrict / iteration) -> FakeItem (mail) with
FakeRecipient and FakeAddressEntry. Nothing here needs Windows or pywin32. All addresses are fake (example.test).

Handy for other tests too: FakeItems counts how many items the engine pulled out of it (``yielded``) and FakeItem counts
reads of the expensive ``Body`` (``body_reads``), so a test can prove the early stop and the "no body read for
irrelevant mail" optimisations.
"""
from datetime import datetime

PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001E"


class FakePropertyAccessor:
    def __init__(self, values):
        self.values = values

    def GetProperty(self, url):
        value = self.values.get(url)
        if value is None:
            raise RuntimeError(f"property {url} not available")
        return value


class FakeExchangeUser:
    def __init__(self, smtp):
        self.PrimarySmtpAddress = smtp


class FakeAddressEntry:
    """``smtp`` answers PR_SMTP_ADDRESS, ``exchange_smtp`` answers GetExchangeUser(), ``address`` is the raw Address."""

    def __init__(self, smtp=None, exchange_smtp=None, address=""):
        self.PropertyAccessor = FakePropertyAccessor({PR_SMTP_ADDRESS: smtp} if smtp else {})
        self._exchange_smtp = exchange_smtp
        self.Address = address

    def GetExchangeUser(self):
        if not self._exchange_smtp:
            raise RuntimeError("not an Exchange user")
        return FakeExchangeUser(self._exchange_smtp)


class FakeRecipient:
    TO, CC, BCC = 1, 2, 3

    def __init__(self, address, kind=1, address_entry=None):
        self.Address = address
        self.Type = kind
        self.AddressEntry = address_entry or FakeAddressEntry(address=address)


class FakeItem:
    """One mail. ``to`` / ``cc`` are address strings (or FakeRecipient objects)."""

    def __init__(self, subject="", sender="", sender_name=None, to=(), cc=(), received=None, body="", conversation_id="",
                 conversation_index="", message_id="", entry_id="", klass=43, sender_type="SMTP", sender_entry=None):
        self.Subject = subject
        self.SenderEmailAddress = sender
        self.SenderEmailType = sender_type
        self.SenderName = sender_name if sender_name is not None else (sender.split("@")[0].title() if sender else "")
        self.ReceivedTime = received
        self.ConversationID = conversation_id
        self.ConversationIndex = conversation_index
        self.EntryID = entry_id
        self.Class = klass
        self.Sender = sender_entry
        self.Recipients = ([r if isinstance(r, FakeRecipient) else FakeRecipient(r, FakeRecipient.TO) for r in to]
                           + [r if isinstance(r, FakeRecipient) else FakeRecipient(r, FakeRecipient.CC) for r in cc])
        self.PropertyAccessor = FakePropertyAccessor({PR_INTERNET_MESSAGE_ID: message_id} if message_id else {})
        self._body = body
        self.body_reads = 0

    @property
    def Body(self):
        self.body_reads += 1
        return self._body


class FakeItems:
    """Outlook's Items collection: Sort() orders in place, Restrict() filters, iteration counts what it hands out."""

    def __init__(self, items=(), fail_sort=False):
        self._items = list(items)
        self.fail_sort = fail_sort
        self.yielded = 0
        self.restrict_calls = []

    @property
    def Count(self):
        return len(self._items)

    def Sort(self, prop, descending=False):
        if self.fail_sort:
            raise RuntimeError("Sort is not supported on this folder")
        self._items.sort(key=lambda i: i.ReceivedTime or datetime.min, reverse=bool(descending))

    def Restrict(self, dasl):
        self.restrict_calls.append(dasl)
        start = datetime.strptime(dasl.split("'")[1], "%Y-%m-%d %H:%M:%S")
        kept = FakeItems([i for i in self._items if i.ReceivedTime and i.ReceivedTime >= start])
        kept.fail_sort = self.fail_sort
        return kept

    def __iter__(self):
        for item in list(self._items):
            self.yielded += 1
            yield item


class FakeFolders(list):
    @property
    def Count(self):
        return len(self)


class FakeFolder:
    def __init__(self, name, items=(), children=(), fail_sort=False):
        self.Name = name
        self.parent = None
        self.Parent = None
        self.items = FakeItems(items, fail_sort)
        self.Folders = FakeFolders(children)
        for child in self.Folders:
            child.parent = child.Parent = self

    @property
    def FolderPath(self):
        return self.parent.FolderPath + "\\" + self.Name if self.parent else "\\\\" + self.Name

    @property
    def Items(self):
        return self.items


class FakeNamespace:
    def __init__(self, roots):
        self.Folders = FakeFolders(roots)

    def GetDefaultFolder(self, which):
        for root in self.Folders:
            for child in root.Folders:
                if child.Name.lower() == "inbox":
                    return child
        raise RuntimeError("no default folder")


class FakeOutlook:
    def __init__(self, namespace):
        self._namespace = namespace

    def GetNamespace(self, name):
        assert name == "MAPI"
        return self._namespace


def flaky_dispatch(namespace, failures):
    """A ``dispatch()`` that raises ``failures`` times (Outlook still starting) and then works."""
    state = {"calls": 0}

    def dispatch():
        state["calls"] += 1
        if state["calls"] <= failures:
            raise RuntimeError("Outlook is still starting")
        return FakeOutlook(namespace)

    dispatch.state = state
    return dispatch
