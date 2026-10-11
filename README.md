# NBE Call Log

A small web app that replaces the per-employee Excel sheet *"Incoming Calls Registration Log"*.

* **Employees** sign in with their User ID, type the **IP phone** that received the call, click the
  **reason** (the four "Action Taken" values from the Excel sheet) and see their own numbers for the day.
  They can edit, delete or add a note to their own records.
* **Administrators** see every employee live: totals, Resolved / Transferred / Ticket Inquiry split,
  calls per hour or per day, a live feed of the latest calls, all records, user management, a full
  **audit log**, and **Excel / CSV export**.
* **No database and no server software.** One Python program serves the pages; all data is saved as
  plain files in the shared folder `\\10.104.46.34\calllog`.

## How it works (read this once)

A web page cannot save files to a shared folder by itself, so **one computer has to run the program**
(`start.bat`). Employees open that computer's address in their browser. The data does not live on that
computer – it is written to `\\10.104.46.34\calllog\data`, so the server can be moved or restarted at
any time without losing anything.

```
 employees' browsers  ──http──▶  PC running start.bat  ──files──▶  \\10.104.46.34\calllog\data
```

Pick a PC that is on during working hours (or the file server itself if it is a Windows machine).

## Setup (Windows, about 5 minutes)

1. Install **Python 3.9 or newer** from python.org (tick *Add python.exe to PATH*).
2. Copy this folder to the PC that will run the server, e.g. `C:\CallLog`
   (a **local** folder, not the network share).
3. Make sure the Windows account that will run it can **write** to `\\10.104.46.34\calllog`
   (open the folder in Explorer once and sign in if asked).
4. Double-click **`start.bat`**. The first start downloads Flask (internet needed once).
5. The window prints the address, e.g. `http://192.168.1.20:8080`, and – on the very first run – the
   one-time password of the `admin` account. Write it down; you will be asked to change it at sign-in.
6. If other PCs cannot open the address, allow **TCP port 8080** in Windows Firewall on the server PC.

Leave the black window open while people are working. Closing it stops the server (no data is lost).

## Adding employees

Sign in as `admin` → **Employees**:

* **Add employee** – User ID (their employee number), name, department. A temporary password is
  generated and shown once; the employee must choose their own at first sign-in.
* **Import from Excel / CSV** – columns `user_id, name, department` (optional `password`). Download the
  generated passwords right after the import.
* **Reset password**, **Disable / Enable**, **Edit** are on each row. Disabled people are signed out
  immediately; their recorded calls are kept.

Forgot the admin password? On the server PC: `python manage.py reset-password admin`.

## Daily use

* Employee: type the IP phone → click the reason → saved. The newest call appears at the top of the
  table. Use the arrows or date box to look at earlier days. *Download* gives their own calls as Excel/CSV.
* Admin: the open tab refreshes by itself every 5 seconds ("Live" indicator, top right). One date range
  (Today / Yesterday / Last 7 days / This month / Custom) applies to every tab. Click an employee in the
  overview to see their calls.
* Every sign-in, failed sign-in, create/edit/delete of a call, account change and export is written to
  the **Audit log** with who, when and the old and new values (deleted records are kept in the log).

## Settings – `config.json`

| key | meaning |
|---|---|
| `data_dir` | where all data is stored (default `\\10.104.46.34\calllog\data`) |
| `port` | web port, default `8080` |
| `user_edit_days` | `null` = employees may edit/delete their own records of any day; a number limits it (e.g. `7`). Administrators are never limited. |
| `ip_phone_regex` | what counts as a valid IP-phone number (default: 3–10 digits). Arabic-Indic digits are converted automatically. |
| `session_hours` | automatic sign-out after this many hours |

The four reasons are in `data\reasons.json` (created on first run). Edit it in Notepad to rename a
reason or change which status it belongs to (`resolved`, `transferred`, `ticket_inquiry`) – restart is
not needed.

## What is stored where

```
data\users.json                     accounts (passwords are stored hashed, never readable)
data\reasons.json                   the list of reasons
data\records\2026-10-11\230966.json one file per employee per day
data\logs\audit-2026-10-11.jsonl    audit log, one line per event
```

**Backup** = copy the `data` folder. Files are written atomically (temp file, then rename) under a lock,
so a crash cannot leave a half-written file and two people saving at once cannot overwrite each other.
Anyone with access to the share can read these files, so give the `calllog` share to the administrators and
the server account only – employees never need direct access to it.

## Security notes

* Passwords are hashed (PBKDF2-SHA256); 5 wrong passwords lock that user+PC out for 10 minutes.
* Pages use CSRF tokens, a strict Content-Security-Policy and HttpOnly session cookies.
* The server speaks plain **HTTP**, which is normal on an internal network. Do not expose the port to
  the internet.
* Exports neutralise spreadsheet formulas (a note starting with `=` cannot run in Excel).

## Development

```
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt pytest
.venv\Scripts\python -m pytest              # 31 tests
set CALLLOG_DATA_DIR=C:\temp\calllog-data   # try it without touching the share
.venv\Scripts\python run.py
```

Code layout: `calllog/storage.py` (files, locking, audit), `api.py` (JSON API), `exports.py`,
`views.py` (pages), `static/` and `templates/` (plain HTML/CSS/JS – no CDN, works offline).
`static/nbe-logo.svg` is a redraw of the NBE logo; replace the file to use the official artwork.

Not covered by automated tests: running on Windows against a real `\\server\share` (developed and
tested on Linux). If the share is slow, saving a call may take a moment longer than on a local disk.
