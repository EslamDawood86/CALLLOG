# NBE Operations Hub - version 2 features (contract for the build agents)

Requested by the user after reviewing checkpoint 4. Read `docs/HUB-DESIGN.md` first (the base contract); this file only adds to it.
Everything in HUB-DESIGN section "rules" still applies: strict CSP (no inline script/style), untrusted text only via `textContent`,
CSRF on every write, an audit line for every action, no `.bat/.cmd/.ps1/.vbs/.exe`, **no real names / numbers / addresses / passwords in
the repo, tests, docs or logs** (use made-up data such as `alice@example.test`), Python 3.9 compatible (no `match`, no `X | Y` types,
no `zip(strict=)`), Windows + SMB-share friendly (atomic writes, no long locks).

User's words (Egyptian Arabic) and what they mean:

1. "when he enters the IP phone, catch the name of the branch or the department of that IP" -> **Feature A**.
2. "the employee can open the e-mails he has, like the manager, view them and download them; the pending page shows 10 and he can
   move between pages; when an e-mail is assigned to him he presses *Done* and it shows to his manager as done; a notification for both"
   -> **Feature B**.
3. "arrange the look, consistent, an impressive layout" -> **Feature C** (design pass, after A and B).
4. "make sure everything is fine, no bug, and the performance is right" -> every feature ships with tests, is driven in a real
   browser, and `tools/loadtest_hub.py` is extended and still meets the targets of HUB-DESIGN section 14.

---------------------------------------------------------------------------------------------------------------------------------

## Feature A - IP phone -> branch / department

### Source data
The user has an Active Directory export `ip_phones.xlsx` (one sheet, ~42,000 rows, 3 columns):
`telephoneNumber` (the IP phone / extension, mostly 5 digits; some 10-digit mobiles, some junk), `displayName` (**a person's name - personal data**),
`msExchExtensionAttribute27` (the **branch / department name** in Arabic, e.g. "فرع جامعة الازهر"). About a third of the rows have no number
(section header rows or people without a phone) and ~1,900 have no unit. About 25,500 distinct numbers, ~680 distinct unit names, ~230 numbers
appear under more than one unit (a number that was re-used). ~490 of the 680 unit names equal a name in the Call Log branch list
(`calllog/default_branches.json`: `{code,name,region,type,id}`) after Arabic normalisation and dropping a leading "فرع".
**The real file must never be committed or copied into the repo; tests build their own small made-up workbook.**

### Storage (data folder, not git)
`<data_dir>/ip_phones.json`, written atomically (`calllog/storage.py` helpers):
`{"version":1,"imported_at":ISO,"imported_by":uid,"source_name":"ip_phones.xlsx","count":N,"units":["...","..."],"numbers":{"75942":3,"73550":[4,9]}}`
(number -> index into `units`, or a list of indexes when the number is ambiguous, most frequent first). **The importer discards `displayName` completely**
(never stored, never logged, never in the audit details); the uploaded file itself is not kept either.

### Import
* Admin page, **Branches** tab: a new card "IP phones" with the status (numbers, units, imported at/by) and an **Import IP phones** button (Excel / CSV,
  same drag-and-drop pattern as "Import branches"). The result shows counts: rows read, numbers kept, units, ambiguous numbers, rows skipped (no number / no unit / bad number).
* `POST /api/admin/ip-phones/import` (admin only, multipart `file`, 20 MB limit - the app's global limit is 2 MB, so raise it for this route only or stream) and
  `GET /api/admin/ip-phones` (status). Import replaces the whole directory; the old file is kept as `ip_phones.previous.json` until the next import.
* `python manage.py import-ip-phones PATH` does the same from the command line (this is the way for a 40k-row file).
* Header detection by name, case-insensitive, with fallbacks: number = `telephoneNumber` / `phone` / `ip phone` / `extension` / first column;
  unit = `msExchExtensionAttribute27` / `branch` / `department` / `unit` / last column. Numbers: Arabic-Indic digits converted, spaces / dashes stripped, must be digits
  only, 3-15 long (everything else counted as "bad number"). Units: trimmed, collapsed whitespace, trailing `#` / digits-glued-to-name noise removed only
  when it keeps a non-empty name (keep the display text otherwise unchanged - it is the admin's data).
* Importing 40k rows must take well under 10 s and must not block other requests for long (parse outside the lock, swap the file atomically).

### Lookup
* `GET /api/ip-phones/<number>` (any signed-in user). Response:
  `{"found":true,"number":"82877","unit":"فرع جامعة الازهر","alternatives":["..."],"branch":{"id":"..","code":"..","name":"..","region":".."}|null}` or `{"found":false,"number":".."}`.
  `branch` = the Call Log branch whose normalised name equals the normalised unit (normalisation: `norm_arabic`, drop leading فرع/فروع/إدارة/ادارة/مكتب, drop
  trailing digits / `#` / "2000"-style suffixes only for the comparison). Exactly one match -> `branch`; zero or several -> `null` (never guess).
  Cheap: the directory is cached in memory (re-checked like other data files), the answer is a dict lookup, `Cache-Control: no-store` stays.
* The Call Log record gets two new optional fields: `ip_unit` (text, the directory unit at the time of the call, <= 120 chars) and `ip_unit_auto` (bool: the
  branch was filled automatically). The server **re-resolves** the unit from the number when saving (never trusts the client's `ip_unit`), so it can be
  checked in tests. `POST /api/records` and `PUT /api/records/<id>` keep working without the fields (older clients, imports).

### Employee form (`user.html` / `user.js`)
* Typing in "IP phone that received the call": after 3+ digits and a 250 ms pause the page asks the lookup (ignore out-of-order answers; cache answers per number
  for the page's life). Under the field a line appears (always `textContent`): `📍 <unit>` with a small hint "from the IP phone directory"; several units ->
  the first plus "also used by: ..."; not found -> a quiet "No branch / department found for this number" (never an error, never blocks saving).
* When `branch` is present and the Branch picker is empty or was filled automatically before, select it and show "filled from the IP phone" next to the picker;
  the employee can change it (then it is no longer automatic). When the unit is not a branch (a department), the unit line is all that is shown and it is saved in `ip_unit`.
* Today's calls table: the IP cell shows the number and, below it in small text, `ip_unit`. The edit dialog has the same lookup. Search also matches `ip_unit`.
* Excel / CSV exports (employee and admin) get a column **IP phone location** (`ip_unit`). Admin "Records" table shows it too.
* Keyboard flow stays: Enter in the IP box moves on; the lookup never steals focus. Works in light and dark, RTL text in `dir="auto"`.

---------------------------------------------------------------------------------------------------------------------------------

## Feature B - assigned e-mails: open, download, Done, 10 per page, notifications

### B1. A saved copy of the mails people need to open
The web app cannot talk to Outlook, so the **collector** saves what the web pages need, inside the data folder:
`<data_dir>/ops/mail/<key>.msg` (Outlook `item.SaveAs(path, 3)`) and `<key>.json` (preview), `key = sha1(message_id).hexdigest()[:32]`.
* Preview JSON: `{"message_id","subject","from_name","from_email","to":[..],"cc":[..],"received":ISO,"body_text":"<= 30,000 chars, plain text","truncated":bool,
  "attachments":[{"name","size"}],"saved_at":ISO,"msg_bytes":N}` (attachment **names and sizes only**, the files are inside the .msg).
* Which mails: setting `email.mail_copies = {"scope": "assigned_and_pending" | "assigned" | "off", "max_per_scan": 40, "max_msg_mb": 25, "keep_days": 21}`
  (default `assigned_and_pending`; settings validation + System -> Settings editor + the collector_config pin like every other key).
  `assigned` = mails with an entry in the `assignments` override; `pending` = rows with status Pending Reply that are not "no action".
* Incremental: a mail whose files already exist is skipped, so after the first scan only new mails cost time (`SaveAs` ~50-300 ms each). At most
  `max_per_scan` per scan (assigned first, then oldest-pending first), the rest next scan. Larger than `max_msg_mb` -> preview only, `msg_bytes: 0`, and the page says the
  copy is too large (still downloadable from Outlook). A COM failure on one mail is logged and skipped (never fails the scan). The scan must still not keep live COM objects
  (see the comment in `email_engine.py` about Outlook becoming unresponsive): save **while iterating**, release immediately.
* Housekeeping (collector daily housekeeping): delete copies older than `keep_days` after the mail left the pending/assigned set, and any file without a partner.
* Everything is testable with `tests/fake_outlook.py` (its fake items already support `SaveAs`; extend it with `Body`, `SenderName`, `To`, `CC`, `Attachments`).
  Never run anything against the real Outlook.

### B2. Who may open / download what
* **Employee**: only mails whose assignment (`assignments[message_id].assigned_to_email`) equals their own `user.email` (case-insensitive). Nothing else - not the team's mails.
* **Manager**: mails of their teams (the same scope rule as the E-mail page). **Admin**: everything. **Never** an `Unclassified` mail for an employee.
* `GET /api/ops/email/message?message_id=<id>` -> preview JSON + `{"has_msg":bool,"state":"ready|preparing|too_large|unavailable"}` (`preparing` = the mail is assigned/pending but the collector
  has not saved it yet - the page says "The collector will have it in a few minutes" and offers **Prepare now**, which writes a normal run request for the `email` job).
  Answers 403 (not yours) / 404 (no such mail) with the same wording for "not yours" and "does not exist" so ids cannot be probed.
* `GET /api/ops/email/message/download?message_id=<id>` streams the `.msg` (`Content-Disposition: attachment; filename="<safe subject up to 60 chars>.msg"`, RFC 5987 for Arabic,
  `application/vnd.ms-outlook`, `X-Content-Type-Options: nosniff`). Path built from the hash only - no user text in a path. One audit line per open and per download.
* Both routes live in the existing `calllog/ops/api.py` blueprint (or a new `calllog/ops/mailbox.py` imported by it) and are covered by role-matrix tests (employee own / other's / unassigned,
  manager own team / other team, admin, anonymous, path-traversal attempts, huge ids).

### B3. Done / Reopen
* Assignment entry (`assignments[message_id]`) gains `done_at`, `done_by`, `done_by_id`, `done_note` (<= 300 chars). Unassigning or re-assigning to someone else clears them.
* `POST /api/ops/email/done {message_id, note?}`: allowed to the assignee (employee, matching e-mail) and to a manager/admin in scope (recorded as `done_by` = that person).
  `DELETE /api/ops/email/done {message_id}` (reopen): the assignee or a manager/admin in scope. Both are audited, both idempotent (marking done twice is a no-op with 200).
* The overlay adds to every row that has an assignment: `assignment_done` (bool), `done_at`, `done_by`, `done_note`. **Reply statistics are not changed** (a Done mail that nobody replied
  to is still Pending Reply for SLA purposes) - Done is the assignee's statement "I handled it", shown next to the status.
* Counts: the E-mail KPI "Assigned" tile shows `assigned (done)` and My work "Assigned to me" shows `open / done`; `done` there means **marked done or replied by me**.
* If the assignee replies in Outlook and the scan sees it, the row becomes Replied as before and the assignment shows Done automatically (`done_by` empty, `done_auto: true`);
  that also notifies the manager (type `replied`) once.

### B4. Notifications (in-app, both sides)
* Store: `<data_dir>/ops/notifications/<user_id>.json` = `{"items":[{"id","ts","type","title","text","link","actor","ref":{"message_id"},"read":bool}],"seen_ts"}`, newest first,
  at most 100 items, written with `OpsStore._update_json` under a per-user lock. Never contains mail bodies - title/text <= 200 chars (subject, who, what).
* Types and recipients:
  | event | who is notified | title |
  |---|---|---|
  | assigned (manager/admin assigns) | the assignee **if** a user account with that e-mail exists | "New e-mail assigned to you" + subject + by whom |
  | reassigned away / unassigned | the previous assignee | "Assignment removed" |
  | done (assignee marks it) | the assigner (`marked_by_id`) **and** every manager whose teams include the row's team (not the actor, no duplicates), plus admins only if they were the assigner | "<name> marked an e-mail as done" + subject |
  | reopened | the other side (assignee <-> assigner/managers) | "E-mail reopened" |
  | replied (assignee replied in Outlook, seen by the scan) | assigner + team managers | "<name> replied to an assigned e-mail" |
  | quality / appeal events already exist as timeline events - they stay as they are |
* `GET /api/ops/notifications?since=<ts>` -> `{"unread":N,"items":[<=30 newest],"ts":server_ts}` or `{"unchanged":true,"unread":N}` when nothing is newer than `since`;
  `POST /api/ops/notifications/read {ids:[..]|"all"}`. Any signed-in role. A user only ever reads their own file (the id comes from the session, never from the request).
  Cost: one cached file read per poll (the OpsStore refresher keeps it warm) - 60 users polling every 30 s must stay far below the HUB-DESIGN section 14 targets.
* UI (every signed-in page, in `_topbar.html` + `ops-common.js`): a bell button with an unread badge before the theme switch; click opens a popover list (newest 20: title, text,
  relative time, unread dot, click = mark read + go to `link`), "Mark all read", empty state "You are all caught up". New arrivals while the page is open raise a toast once
  and update `document.title` to `(n) ...`. Polling: 30 s with a random start offset, back-off on errors, **no polling while the tab is hidden**, one poll shared by
  all code on the page. Links: employee -> `/my?mail=<message_id>` (opens the viewer), manager/admin -> `/email?tab=pending&open=<message_id>`.
  Keyboard accessible (button, `aria-expanded`, Escape closes, focus returns), screen-reader text for the badge, works in dark mode and on a 360 px phone.

### B5. Pages
* **E-mail page** (manager/admin): the Pending, Replied and Unclassified tables, and every other long table on the page, get a shared **pager** (`OPS.pager` in `ops-common.js`):
  default **10 per page**, page-size choice 10 / 25 / 50, Prev / Next + numbered buttons with ellipsis, "Showing 11-20 of 87", keyboard operable, the page number and page size kept in the
  URL hash/query and `localStorage` (try/catch), filters and search reset to page 1, a poll that brings new data keeps the current page and does not jump the user around.
  Each row gets **Open** (the viewer) and, when the copy exists, **Download**; assigned rows show the assignee and a **Done** badge with who/when/note; a filter "Assignment: any / open / done".
* **Viewer** (shared component `OPS.mailViewer`, a modal dialog opened from the E-mail page, My work and notification links): subject, from, to/cc, received, status/SLA badges,
  assignment line (assigned by/at/note, Done by/at), the plain-text body in a scrollable `<pre>`-like block (`textContent`, `dir="auto"`, `white-space: pre-wrap`), attachment names with sizes,
  buttons **Download .msg**, **Done / Reopen** (when allowed), **Close**. States: loading, preparing (with Prepare now), too large, not available, forbidden. Focus trap, Escape, restores focus.
* **My work** (employee): "My e-mails" lists the mails assigned to the employee (pager, 10 per page, open first then done, newest assignment first) with **Open**, **Download**, **Done** (with an optional note
  prompt in the viewer, one click in the list), and the same columns as the manager sees (received, waiting, SLA, assigned by, note). "My team's e-mails" (no body, no download) gets the pager too.
  `?mail=<id>` opens the viewer on load. The Team status strip and numbers stay as they are.
* All of it works for `?as=<uid>` (admin/manager looking at an employee's page) **read-only**: no Done, no download of someone else's mail unless the viewer is allowed by B2 themselves.

### B6. Out of scope (say so in the docs, do not build)
Replying from the web, e-mail (Outlook) notifications for Done (the in-app notification is the notification), editing the e-mail, a full HTML rendering of the message body.

---------------------------------------------------------------------------------------------------------------------------------

## Feature C - one consistent, impressive look (after A and B are in)

Goal: every page (login, password, message, Log calls, admin Calls tabs, Home, E-mail, SLA, ITSM, System, My work) looks like one product, in light and dark, at 1440 / 1280 / 1024 / 768 / 360 px.
Look at real screenshots (`python tools/shots_hub.py --out DIR [--theme dark] [--width 1024]`) and fix what you see. Known issues in the checkpoint-4 look: the E-mail KPI tiles wrap
into an orphan 8th tile; page headers differ per page (title/buttons/status line positions); the My calls card (sparkline + legend) is cramped; Hub cards have different paddings; two CSS systems
(`app.css` for Call Log, `ops.css` for the Hub) define similar things twice; tables differ in density and header style; empty / loading / error states differ.
Deliver: shared design tokens (spacing 4/8/12/16/24/32, radius, shadow, type scale, brand green `#00573F` family + gold accent, status colours with AA contrast in both themes), one **page header**
pattern (title + subtitle + status chip on the left, actions on the right, wraps on mobile), one **KPI grid** (auto-fit, never an orphan), one card / table / badge / button / form-control look, consistent
icons (the inline SVG set in `ops-common.js`), consistent empty/loading/error blocks, sticky table headers where tables scroll, focus rings, reduced-motion support, print-friendly E-mail/SLA pages.
No new libraries or fonts from a CDN (offline bank network): system font stack + inline SVG only. Strict CSP stays. Do not change behaviour or element ids/classes that tests use, unless you update the tests.
Add subtle polish that makes it feel premium (tabular numerals in KPI, soft elevation, smooth but fast transitions, skeleton loading) without slowing the 60-user budget (no big images, CSS only).

---------------------------------------------------------------------------------------------------------------------------------

## Acceptance (all features)

1. `python -m pytest -q` is green on Python 3.13 (Linux sandbox, with the Chromium browser tests running) and on a Python 3.9 venv.
2. New tests per feature: unit (parsers, storage, overlay), API role matrix, source rules (no innerHTML / inline style / eval, `textContent`), and **real-browser** flows
   (Playwright, `tests/test_*_page.py` style with the shared fixtures in `tests/test_ops_views.py`) with no console error / CSP violation / failed request.
3. `python tools/loadtest_hub.py --seconds 45 --interval-scale 1` still prints "all targets met"; the load test is extended with the notification poll (60 users, 30 s) and the
   message viewer opens (some employees open a mail every minute); targets unchanged.
4. Privacy scan: `grep -rIn` for the real user number / password / real names finds nothing in the tree; the IP-phone importer provably never stores `displayName` (a test inspects the
   written file and the audit log).
5. `python docs/build/build_package.py` still passes its checks (no `.bat/.cmd/.ps1/.vbs/.exe`, no secrets/data/tests).
