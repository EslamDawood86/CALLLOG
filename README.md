# NBE Call Log

A small web app that replaces the per-employee Excel sheet *"Incoming Calls Registration Log"*.

* **Employees** sign in with their User ID, type the **IP phone** that received the call, pick the
  **branch** the call came from (only if the caller is a branch), click the **reason**
  (the four "Action Taken" values of the Excel sheet), and see their own numbers for the day.
  They can edit, delete or add a note to their own records.
* **Administrators** see every employee live: totals, Resolved / Transferred / Ticket Inquiry split, calls
  per hour or per day, calls per region and the busiest branches, a live feed of the latest calls, all records,
  user management, the branch list, a full **audit log**, and **Excel / CSV export**.
* **No database and no server software.** One Python program serves the pages; the program files, its
  libraries and all data are plain files in the shared folder `\\10.104.46.34\calllog`.

## How it works (read this once)

A web page cannot save files to a shared folder by itself, so **a PC has to run the program**
(`python run.py`). The program itself and all data stay in the shared folder, so it can be started from
any PC that has Python, and restarted or moved at any time without losing anything.

```
 employees' browsers ──http──▶ PC running run.py ──files──▶ \\10.104.46.34\calllog  (program + libs + data)
```

Two ways to use it:

| | what you do | who needs Python |
|---|---|---|
| **One server PC** (recommended for ~60 people) | the server PC runs `run.py --service`; everybody opens `http://<server-pc>:8080` | only the server PC |
| **Everyone runs it** | each employee double-clicks `run.py` in the shared folder; it opens their browser | every PC |

## Setup - everything in the shared folder

1. Copy **all the project files** into `\\10.104.46.34\calllog` so that this file exists:
   `\\10.104.46.34\calllog\run.py`.
2. On the PC that will run it, install **Python 3.9 or newer** (python.org, tick *Add python.exe to PATH*).
3. Make sure the Windows account that runs it can **write** to the shared folder.
4. Start it once by hand to see that it works:
   * double-click `\\10.104.46.34\calllog\run.py`, or
   * Command Prompt: `python \\10.104.46.34\calllog\run.py --share`

   The first start installs Flask etc. into the `libs` folder **inside the shared folder** (internet needed
   once - afterwards any PC with Python can run it without installing anything).
5. The window prints the address (e.g. `http://192.168.1.20:8080`) and, on the very first run, the one-time
   password of the `admin` account. Write it down; you must change it at first sign-in.
6. Other PCs open that address. If they cannot, allow **TCP port 8080** in Windows Firewall on the server PC.

Without `--share` the site opens only on the PC that started it (no firewall question, nothing exposed).

## Keep it running with Task Scheduler (and always up to date)

`python run.py --service` is made for Task Scheduler. It starts the server on the network, **restarts it if it
ever stops** (or if the share is not reachable yet right after a reboot), and **restarts it by itself about
30 seconds after you replace program files in the shared folder** - so the running copy is never old.
It writes a `server.log` next to `run.py`.

First do step 4 above once by hand and sign in as `admin`. Then, on the server PC:
**Task Scheduler → Create Task…**

| tab | setting |
|---|---|
| General | Name `NBE Call Log`. **Run whether user is logged on or not.** Do **not** tick "Do not store password" (the task needs your Windows password to reach the share). The account must have write access to the shared folder. |
| Triggers | New → **At startup**, *Delay task for* 1 minute. |
| Actions | New → *Start a program*. **Program:** full path of `python.exe` (find it with `where python`). **Arguments:** `"\\10.104.46.34\calllog\run.py" --service`. **Start in:** leave empty (Task Scheduler does not accept network paths there). |
| Conditions | Untick "Start only if the computer is on AC power". Tick "Start only if the following network connection is available: Any connection". |
| Settings | Tick **"If the task fails, restart every 1 minute"**, up to 999 times. "If the task is already running": **Do not start a new instance**. Untick "Stop the task if it runs longer than 3 days". |

Right-click the task → **Run**, then open the address in a browser. Check `server.log` if something is wrong.

**Updating the program:** copy the new files over the old ones in the shared folder (keep `data`, `libs` and
your own `config.json`). The service notices, waits until the copy is finished, and restarts the server
(the first start after an update installs any new library by itself). If `run.py` itself changed, the service
exits and Task Scheduler's "restart if the task fails" starts the new one within a minute.

## Branches

The call form has a searchable **Branch** box (597 branches in 11 regions, from the bank's workbook).
Type the branch number (`100`) or part of the name; spelling variants such as أ/ا and ة/ه are matched.
**It is optional:** if the caller is not a branch (a sector, a department, anyone else) the employee just
leaves it empty and enters the IP phone. Each call keeps the branch number, name and region it was logged
with, so later changes to the list never rewrite history.

Admins see calls **by region** and the **top branches** on the Overview, can filter Records by region or
search by branch, and the Excel report has a *By branch* sheet.
**Branches tab → Import from Excel** replaces the list (columns `رقم الفرع`, `اسم الفرع`, optionally
`اسم المنطقة التابع لها`, `نوع الفرع`; English titles also work). Rows that repeat the same number+name+region
are merged; where the same number exists in two regions both are offered with their region shown.

## Adding employees

Sign in as `admin` → **Employees**:

* **Add employee** - User ID (employee number), name, department. A temporary password is generated and shown
  once; the employee chooses their own at first sign-in.
* **Import from Excel / CSV** - columns `user_id, name, department` (optional `password`). Download the
  generated passwords right after the import.
* **Reset password**, **Disable / Enable**, **Edit** are on each row. Disabled people are signed out
  immediately; their recorded calls are kept.

Forgot the admin password? On the server PC: `python \\10.104.46.34\calllog\manage.py reset-password admin`.

## Daily use

* Employee: IP phone → (branch) → click the reason → saved. `Enter` in the IP box jumps to the branch box;
  type `100` and press `Tab` to take the best match. The newest call appears at the top; the arrows or the date
  box show earlier days; *Download* gives their own calls as Excel/CSV.
* Admin: the open tab refreshes by itself ("Live" indicator, top right): today every 5 seconds, longer
  ranges every 15 seconds, past days rarely (they cannot change). One date range (Today / Yesterday /
  Last 7 days / This month / Custom) applies to every tab. Click an employee to see their calls.
* The **Audit log** records every sign-in, failed sign-in, create/edit/delete of a call (old and new values,
  deleted records are kept in the log), account change, branch import and export.

## Settings - `config.json` (optional)

Copy `config.example.json` to `config.json` next to `run.py` and change what you need
(your own `config.json` is never overwritten by an update).

| key | meaning |
|---|---|
| `data_dir` | where all data is stored. `"data"` = a folder next to the program files (default, so running from the shared folder keeps the data there). A full path such as `\\10.104.46.34\calllog\data` also works. |
| `host` | `"127.0.0.1"` = only this PC can open it (default); `"0.0.0.0"` = other PCs too (same as `--share`). |
| `port` | web port, default `8080` |
| `threads` | requests handled at the same time, default `16` |
| `user_edit_days` | `null` = employees may edit/delete their own records of any day; a number limits it (e.g. `7`). Administrators are never limited. |
| `ip_phone_regex` | what counts as a valid IP-phone number (default 3-10 digits). Arabic-Indic digits are converted automatically. |
| `session_hours` | automatic sign-out after this many hours |

The reasons are in `data\reasons.json` (created on first run); edit it in Notepad to rename a reason or change
its status (`resolved`, `transferred`, `ticket_inquiry`).

## What is stored where

```
run.py, calllog\, ...                program files            libs\   Flask etc. (installed on first start)
data\users.json                      accounts (passwords hashed, never readable)
data\reasons.json, branches.json     the two drop-down lists
data\records\2026-10-11\230966.json  one file per employee per day
data\logs\audit-2026-10-11.jsonl     audit log, one line per event
data\.locks\                         lock files (several PCs may run the program at once, safely)
```

**Backup** = copy the `data` folder. Files are written atomically (temp file, then rename) under a lock, so a
crash cannot leave a half-written file and two people saving at once cannot overwrite each other - even when
two PCs run the program at the same time (tested with separate processes).

## Speed with ~60 people

Measured with 60 simulated employees (each logging a call every ~30 s, about 10x busier than real life) plus
four admin views, 142,000 calls (30 days) on disk, and an artificial 5 ms delay on **every** file operation to
imitate a network share: employee requests take 10 ms (page refresh) to 60 ms (saving a call); the admin
Overview about 75 ms for today and about 200 ms for a whole month (including its first, cold load); the server
used 3-5% of one CPU core after the sign-in rush; no errors. With a slow share (15 ms per file operation) saving
a call took ~170 ms and nothing failed. (Before these optimisations the same test showed 4+ seconds for the
admin month views.) How:

* employees' pages refresh with one small request every 20 s; the admin tab polls only as often as its data can
  change; the branch list is downloaded once and cached by the browser;
* parsed files are cached in memory and re-checked on the share only every 3 s (today) / 30 s (older days);
* every past day is summarised once, so a month view merges 30 small summaries instead of re-reading
  140,000 calls; cold reads from the share run 16 at a time;
* 16 locks for call records (by employee and day) - different employees never wait for each other;
* sign-in uses 260,000 PBKDF2 rounds (~0.1 s) so 60 people signing in at 08:00 are not queued.

Things that can make it slower: a share with very high latency or a PC with one slow core. Data seen by another
PC that also runs the program can be a few seconds old (up to ~10 s on Windows network shares).
These figures come from a simulation on Linux; they were **not** measured on a real Windows/SMB share.

## Security notes

* Passwords are hashed (PBKDF2-SHA256); 5 wrong passwords lock that user+PC out for 10 minutes. The initial
  admin password is never written to `server.log`.
* Pages use CSRF tokens, a strict Content-Security-Policy and HttpOnly session cookies. The site speaks plain
  **HTTP**, normal on an internal network; do not expose the port to the internet.
* Exports neutralise spreadsheet formulas (a note starting with `=` cannot run in Excel).
* **The program runs from the shared folder, so anyone who can write there can change the program.** Give
  write access to the administrators and the server account only. With the *one server PC* setup employees need
  **no** access to the shared folder at all. In the *everyone runs it* setup every PC needs write access to
  `data` (and so could edit the data files directly - the audit log protects against mistakes, not against
  tampering).

## Troubleshooting

| problem | what to do |
|---|---|
| "Cannot use the data folder …" | the share is not reachable or the account cannot write to it - open it in Explorer first |
| Others cannot open the address | allow TCP port 8080 in Windows Firewall on the server PC; start with `--share` |
| "Port 8080 is already in use" | another program uses it; choose another: `--port 8081` (or `port` in `config.json`) |
| The service does not start after a reboot | open `server.log`; the share is often not ready yet - the service retries by itself |
| Packages cannot be installed | internet/proxy needed once; delete the `libs` folder if it is half-filled and start again |

## Development

```
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt pytest
.venv\Scripts\python -m pytest              # 40 tests (incl. separate processes sharing one folder)
set CALLLOG_DATA_DIR=C:\temp\calllog-data   # try it without touching the share
.venv\Scripts\python run.py
```

Code: `calllog/storage.py` (files, locks, caches, daily summaries), `stats.py`/`overview.py` (figures),
`api.py` (JSON API), `exports.py`, `views.py` (pages), `branches.py` (branch list), `static/` and `templates/`
(plain HTML/CSS/JS - no CDN, works offline). `static/nbe-logo.svg` is a redraw of the NBE logo; replace the
file to use the official artwork. Not covered by automated tests: Windows against a real `\\server\share`
and Task Scheduler itself (the restart logic was tested on Linux).
