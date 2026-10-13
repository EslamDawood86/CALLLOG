"""A small fake of the Outlook object model, just what calllog/ops/engines/email_engine.py reads.

FakeNamespace -> FakeFolder (sub-folders, Items) -> FakeItems (Sort / Restrict / iteration) -> FakeItem (mail) with
FakeRecipient and FakeAddressEntry. Nothing here needs Windows or pywin32. All addresses are fake (example.test).

Handy for other tests too: FakeItems counts how many items the engine pulled out of it (``yielded``) and FakeItem counts
reads of the expensive ``Body`` (``body_reads``), so a test can prove the early stop and the "no body read for
irrelevant mail" optimisations.

Saved copies of mails (docs/FEATURES-V2.md B1): a FakeItem has ``To`` / ``CC`` (the display strings), ``Attachments``,
``Size`` and - for items made with ``savable=True`` only, so the plain items of the older tests still "cannot SaveAs" -
``SaveAs(path, format)`` which writes recognisable bytes (``MSG_MAGIC`` + the subject, the body and padding up to
``msg_bytes``). ``FakeNamespace.GetItemFromID(entry_id, store_id=None)`` hands out a ``FakeItemRef`` - a thin handle on
the real fake item - and counts the handles that are alive (``live_handles``, ``max_live_handles``), so a test can prove the
engine fetched mails one at a time and kept none after the scan.
"""
import weakref
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


# What a fake .msg file starts with: the real OLE2 signature, then a marker a test can look for
MSG_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1FAKE-MSG\n"


class FakeAttachment:
    def __init__(self, name, size=0):
        self.FileName = name
        self.Size = size


class FakeItem:
    """One mail. ``to`` / ``cc`` are address strings (or FakeRecipient objects). ``attachments`` = [(name, size)];
    ``msg_bytes`` = how big the saved .msg is (``Size`` answers the same number); ``savable=True`` gives the item a
    ``SaveAs``; ``save_error`` makes that SaveAs fail."""

    def __init__(self, subject="", sender="", sender_name=None, to=(), cc=(), received=None, body="", conversation_id="",
                 conversation_index="", message_id="", entry_id="", klass=43, sender_type="SMTP", sender_entry=None,
                 attachments=(), msg_bytes=None, save_error=None, to_text=None, cc_text=None, savable=False):
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
        self.To = to_text if to_text is not None else "; ".join(
            r.Address for r in self.Recipients if r.Type == FakeRecipient.TO)
        self.CC = cc_text if cc_text is not None else "; ".join(
            r.Address for r in self.Recipients if r.Type == FakeRecipient.CC)
        self.Attachments = [a if isinstance(a, FakeAttachment) else FakeAttachment(*a) for a in attachments]
        self._msg_bytes = msg_bytes
        self._save_error = save_error
        self.save_calls = []
        if savable or save_error:
            self.SaveAs = self._save_as

    @property
    def Body(self):
        self.body_reads += 1
        return self._body

    def _msg_content(self):
        head = MSG_MAGIC + f"subject={self.Subject}\nbody={self._body}\n".encode("utf-8")
        wanted = self._msg_bytes if self._msg_bytes is not None else len(head)
        return head + b"\0" * max(0, wanted - len(head)) if wanted >= len(head) else head[:wanted]

    @property
    def Size(self):
        return self._msg_bytes if self._msg_bytes is not None else len(self._msg_content())

    def _save_as(self, path, fmt=3):
        """Outlook's SaveAs: writes the (fake) .msg file; ``fmt`` 3 = olMSG."""
        self.save_calls.append((str(path), fmt))
        if self._save_error:
            raise RuntimeError(self._save_error)
        with open(path, "wb") as f:
            f.write(self._msg_content())


class FakeItemRef:
    """What GetItemFromID returns: a handle that forwards everything to the fake item. Handles are counted while they are
    alive, which is how a test proves that no COM object is kept after the scan."""

    def __init__(self, item, namespace):
        object.__setattr__(self, "_item", item)
        namespace._opened(self)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_item"), name)


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
    def __init__(self, name, items=(), children=(), fail_sort=False, store_id=None):
        self.Name = name
        self._store_id = store_id
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

    @property
    def StoreID(self):
        """The store a folder lives in: one per top-level root (like an Outlook archive)."""
        if self._store_id is not None:
            return self._store_id
        return self.parent.StoreID if self.parent else "STORE:" + self.Name

    def walk(self):
        yield self
        for child in self.Folders:
            yield from child.walk()


class FakeNamespace:
    def __init__(self, roots):
        self.Folders = FakeFolders(roots)
        self.live_handles = 0
        self.max_live_handles = 0
        self.fetched = []                              # (entry_id, store_id) of every GetItemFromID

    def _opened(self, handle):
        self.live_handles += 1
        self.max_live_handles = max(self.max_live_handles, self.live_handles)
        weakref.finalize(handle, self._closed)

    def _closed(self):
        self.live_handles -= 1

    def GetItemFromID(self, entry_id, store_id=None):
        """Find a mail by EntryID. A wrong ``store_id`` fails like Outlook does when the store does not hold the item."""
        self.fetched.append((entry_id, store_id))
        for root in self.Folders:
            for folder in root.walk():
                for item in folder.items._items:
                    if item.EntryID == entry_id and (store_id is None or store_id == folder.StoreID):
                        return FakeItemRef(item, self)
        raise RuntimeError("The item cannot be found.")

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
