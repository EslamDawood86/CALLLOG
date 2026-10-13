# NBE Operations Hub - design and contracts

The Call Log app grows into one application that follows the whole sector: **Calls**, **Email**
(team reply analytics), **SLA** (security requests) and **ITSM** (implementation / rollback requests).
This file is the single source of truth for everybody who writes code for it. If code and this file
disagree, fix the code or change this file in the same commit.

## 1. The idea in one picture

```
 Windows PC with Outlook / Chrome            shared folder \\10.104.46.34\calllog           server PC (run.py --service)
 ┌───────────────────────────────┐   write   ┌─────────────────────────────────┐   read    ┌───────────────────────┐
 │ collector.py                  │ ────────▶ │ data\ops\snapshots\*.json       │ ────────▶ │ Flask app (calllog\)  │──▶ browsers
 │  jobs: email, email_report,   │           │ data\ops\history, overrides,    │ ◀──────── │  Home / Calls / Email │
 │        sla, itsm              │ ◀──────── │ jobs\requests (run-now), logs   │   write   │  SLA / ITSM / System  │
 │  (Outlook COM, Playwright,    │   read    └─────────────────────────────────┘ overrides └───────────────────────┘
 │   Chrome DevTools)            │
 └───────────────────────────────┘
```

* **Collectors** are the only code that touches Outlook, the SSO portal or ITSM. They run where those
  tools are (a user PC that is logged in), write plain JSON into the shared folder and send the
  e-mails the three old scripts used to send.
* **The web app never talks to Outlook / portals.** It only reads snapshots, lets people act
  (assign an e-mail, mark "no action", review quality, press "Run now") and shows collector health.
* Nothing needs a database. Everything is a file in `data\ops\` written atomically under a lock
  (same rules as the Call Log: `calllog/storage.py`).
* **Accounts and passwords live in one file you edit in Notepad: `collector_config.json`** (next to `collector.py`,
  see section 10). Only the collector reads it - never the web server. It is git-ignored and left out of the ZIP.
  No password is ever stored in the repo, in `settings.json`, in a snapshot, in a log, or sent to a browser.

## 2. Code layout

```
calllog/ops/
  __init__.py
  store.py        OpsStore: every read/write under data\ops (see section 4)       [foundation, done]
  settings.py     defaults, validation, load/save of data\ops\settings.json       [foundation, done]
  collector_config.py  reads collector_config.json (accounts + pinned settings)      [foundation, done]
  schemas.py      constants (module names, status codes) + light snapshot validators
  overlay.py      applies e-mail overrides on top of an e-mail snapshot (pure functions)
  mailbox.py      saved copies of mails: file names, preview documents, which mails to copy, housekeeping (section 4); no Flask, no Outlook
  notifications.py  the in-app notifications (the bell): per-person files, poll, mark read, who is told about what (section 7.2)
  api.py          Flask blueprint  /api/ops/...   (section 7)
  views.py        HTML pages /hub /email /sla /itsm /system                       (section 8)
  exports.py      /export/ops/... (xlsx/csv built from snapshots, report download)
  engines/
    common.py     JobContext, JobResult, html/brand helpers, Outlook mail sender, time helpers
    email_engine.py   scan + analysis (port of Daily_Team_Reply_Analytics_Fast_Scan_2026.py)
    email_report.py   Excel workbook + HTML e-mails (report, team pending, assignment, quality, end-of-day)
    sla_engine.py     port of SLA.PY
    itsm_engine.py    port of itsm_status.py
  runner.py       scheduler used by collector.py (section 6)
  legacy_import.py  reads the three old scripts (never executes them) -> settings.json
collector.py      launcher (top level, next to run.py)
requirements-collector.txt
```

Engines and `store.py` must **import on Linux/macOS without Outlook, Chrome, pywin32, Playwright or
pandas installed** (import those lazily inside the functions that need them). This is what makes them
testable here. Do not add pandas to the project.

## 3. Roles and the two dashboards

| role | home page | can |
|---|---|---|
| `admin` | `/hub` **Admin dashboard** | everything: all modules, all teams, settings, collectors, users, audit, "view as employee" |
| `manager` | `/hub` | sees the same dashboards, acts on e-mails (assign, no-action, team, quality, classification rules) for **their teams** (`user.teams`; empty or `__all__` = all teams); no settings, no user management; cannot log calls |
| `user` (employee) | `/my` **My work** | logs calls as today (`/dashboard`), and sees *everything that is theirs* under their own user (section 3.2) plus the shared team strip |

User profile fields (all optional): `email` (their mailbox, lower-case; links replies and assignments to the account),
`teams` (a list of team names; a manager may hold `__all__` = every team), `security_name` (their name exactly as in the
portal's "مستخدم السرية" column; links SLA requests to the account).

**An account (employee or manager) may belong to more than one team** and then sees the union of those teams. The older
single `team` field is still read (an account with only `team` counts as `[team]`) and still exposed in `public_user`
(the first team) so existing pages and scripts keep working; new code reads and writes `teams`. A team name renamed or
removed in the settings never blocks editing an account that already holds it. An **employee is never put in
"Unclassified"**: that bucket is for managers/admins only (classification is adjusted in Settings, section 5.5).

### 3.1 Shared information (same numbers for the same viewer, wherever the strip is drawn)
`GET /api/ops/shared` returns **counts only, never subjects, customers or names**: calls today (team total), e-mails
(business / replied / pending / reply rate / SLA breached), security requests (open / reminder / breached), ITSM
(per category totals), collector freshness (online, last update). The same "Team status" strip component renders it on
the Admin dashboard and on My work, so both always agree for the same viewer.

Calls, security requests and ITSM are **sector-wide** counts for everyone. The **e-mail numbers are scoped to the
viewer**: an administrator (and a manager of every team) counts all rows; a manager or employee counts only the rows of
their own teams, and an employee never counts an "Unclassified" mail. The e-mail block says which with `scope`
(`"all"` | `"teams"`) and `teams` (the viewer's team names, `[]` for "all" or "no team"). So two different viewers may
see different e-mail numbers, but every dashboard shows the *same* viewer the *same* numbers.

### 3.2 My work (`/my`, employee; admin/manager may open it for anyone with `?as=<user_id>`, read-only)
`GET /api/ops/my[?as=uid]` returns the signed-in user's own data:
* **calls**: today / yesterday / last 7 days totals, split by status, calls per hour today (from the existing call storage);
* **e-mails** (`email`):
  * `assigned` / `replied` - the mails that are mine *by name*: assigned to my `email` and the ones I replied to today (count,
    average first-reply minutes, SLA met / breached). A mail that is mine stays visible even if it later becomes "Unclassified"
    (it was explicitly handed to me), and I may open it (section 7.1). `assigned` = `{pending, done, other, items}`: **pending**
    (= open) is a mail still waiting for a reply that I have not marked done; **done** is a mail I marked Done *or* answered in
    Outlook myself (`done_auto`); **other** is a mail somebody else answered or that needs no action. `items` (at most 300, one per
    mail even when the collector split it between teams) are ordered open first, then done, then other, newest assignment first; each
    carries `assignment_done, done_at, done_by, done_note, done_auto, mail_copy` next to the usual fields.
  * `team_emails` - the e-mails of **my own team(s)**: overlaid rows whose `team` or any of `all_teams` is one of my teams,
    status Pending or Replied, **never the "Unclassified" bucket**, pending-first then oldest-first, at most 300. Only a
    whitelist of fields is returned (`row_id, message_id, subject, customer_name, team, status, received_time,
    current_waiting_time, sla_status, escalated, is_carryover, first_reply_by, first_reply_time, assigned_to_name,
    detected_systems`) - no body text, no customer address, no quality notes, nothing of another team.
  * `team_summary` - the headline numbers (`teams, all_teams, pending, replied, no_action, sla_breached, escalated,
    reply_rate, listed, more`) of exactly those team rows.
  * `team_emails_hint` - `"no_team"` when the account has no team (empty `team_emails`), otherwise `""`.
* **quality**: my quality reviews and appeals;
* **security requests**: SLA requests whose `security_user` equals my `security_name` (status, age) - empty state when no
  `security_name` is set on the account;
* **shared**: the section 3.1 strip (viewer-scoped, see 3.1).
An employee never receives anybody else's rows, and never an "Unclassified" one through `team_emails`. A manager opening
`?as=<uid>` sees only what **both** of them may see (the intersection of the two scopes).

## 4. Data layout (inside `data_dir`, i.e. the shared folder)

```
ops/settings.json                       all module settings (no secrets)
ops/snapshots/<module>.json             LAST SUCCESSFUL scan of: email | sla | itsm
ops/history/<module>/<YYYY-MM-DD>.json  small daily file: list of per-scan points + extras (section 5)
ops/overrides/email/no_action.json      { message_id: {marked_by, marked_at, subject} }
ops/overrides/email/team.json           { message_id: {team, marked_by, marked_at, subject} }
ops/overrides/email/assignments.json    { message_id: {assigned_to_email, assigned_to_name, note, marked_by, marked_by_id, marked_at, subject, team, notified,
                                          done_at, done_by, done_by_id, done_note,      <- set by Done (7.1); cleared when the mail goes to someone else
                                          replied_notified} }                           <- set once the "replied" notification was sent
ops/overrides/email/quality.json        { message_id: {rating, note, reasons, replied_by, replied_by_email, reviewed_by, reviewed_at, appeal, appeal_by, appeal_at, appeal_decision, decided_by, decided_at, rating_withdrawn, subject} }
ops/state/<module>.json                 engine-private state (alert de-duplication, last sent, ...)
ops/events/<YYYY-MM-DD>.jsonl           timeline events (section 5.4): one line per event, written by the runner
ops/jobs/heartbeat/<job>.json           collector heartbeat (section 6)
ops/jobs/requests/<job>-<stamp>-<rand>.json   "Run now" requests written by the web app
ops/jobs/logs/<job>.log                 rolling text log (tail shown in System > Logs), rotated at ~1 MB
ops/mail/<key>.msg                      saved copy of a mail (Outlook SaveAs, olMSG); key = sha1(message_id).hexdigest()[:32]
ops/mail/<key>.json                     its preview: {message_id, subject, from_name, from_email, to[], cc[], received, body_text (<= 30,000 chars),
                                          truncated, attachments[{name,size}], saved_at, msg_bytes, too_large}  (msg_bytes 0 = preview only)
ops/notifications/<user id>.json        {items[{id,ts,type,title,text,link,actor,ref{message_id},read}], ts, seen_ts}, newest first, <= 100 items
ops/reports/email/<name>.xlsx           reports the collector produced
ops/reports/sla/<name>.xlsx
ops/reports/itsm/<name>.html
ops/locks/                              lock files
```

The two files of a saved mail are written atomically, the `.msg` first and the `.json` last (a preview that exists is complete); the
web app builds both paths from the hash only, never from text a person typed. `mail/` and `notifications/` are the only folders that
grow with use: housekeeping (section 15) prunes them, `GET /api/ops/health` reports their size.

Dates inside files are **naive local time of the collector PC**, ISO 8601 (`2026-10-11T09:15:03`);
days are `YYYY-MM-DD`. The web app never converts time zones (everything is in Cairo local time).

## 5. Snapshot contracts

Every file in `ops/snapshots/` has this envelope. `data` differs per module.

```jsonc
{
  "schema": 1,
  "module": "sla",                  // email | sla | itsm
  "generated_at": "2026-10-11T09:15:03",
  "duration_seconds": 12.4,
  "host": "PC-123",                 // collector PC
  "date": "2026-10-11",
  "data": { ... }
}
```

A failed scan **never overwrites** the snapshot (the UI keeps showing the last good data with a banner,
using the heartbeat's `last_error`).

### 5.1 `sla`

```jsonc
"data": {
  "thresholds": {"warning_minutes": 60, "breach_minutes": 120},
  "totals": {"open": 31, "within": 20, "reminder": 7, "breached": 4, "no_date": 0, "oldest_minutes": 410},
  "by_user": [   // sorted breached desc, reminder desc, total desc
    {"user": "احمد ...", "total": 5, "within": 3, "reminder": 1, "breached": 1, "no_date": 0, "oldest_minutes": 150}
  ],
  "requests": [  // sorted by age desc
    {"key": "stable-hash-of-the-request-columns", "security_user": "احمد ...",
     "sent_at": "2026-10-11T08:12:00" | null, "age_minutes": 63 | null,
     "status": "within" | "reminder" | "breached" | "no_date",
     "fields": {"اسم الموظف": "...", "رقم المستخدم": "...", "الفرع / الادارة": "...",
                "النظام": "...", "نوع الطلب": "...", "موظف الاسناد": "..."}}   // detail_headers present on the page
  ]
}
```
History point (`ops/history/sla/<day>.json` = `{"points": [{"ts","open","within","reminder","breached"}], "first_breach": {key: ts}}`).

### 5.2 `itsm`

```jsonc
"data": {
  "categories": ["Implementation Requests", "Rollback Requests"],     // settings order
  "roles": [
    {"role": "IMP User provisioning", "error": null,
     "categories": {
        "Implementation Requests": {"count": 3, "rows": [{"number": "123456", "summary": "..."}],
                                    "warning": null, "note": null},
        "Rollback Requests": {"count": null, "rows": [], "warning": null, "note": null}   // null = not on this role's scoreboard
     }},
    {"role": "IMP UAT I-Flex SMS", "error": "Set Role button not found", "categories": {}}
  ],
  "totals": {"Implementation Requests": 5, "Rollback Requests": 1},
  "changes": {"new":   [{"role": "...", "category": "...", "number": "...", "summary": "..."}],
              "closed": [{"role": "...", "category": "...", "number": "...", "summary": "..."}]}  // vs previous good scan of the same day or last known
}
```
History point: `{"ts", "totals": {category: n}, "per_role": {role: {category: n}}}`.

### 5.3 `email`

`data` mirrors the old live-dashboard payload (`_json_safe_stats` / `_json_safe_row`) plus the extras the
manager dashboard used. Row keys are stable; unknown extra keys are allowed (UI ignores them).

```jsonc
"data": {
  "date": "2026-10-11",
  "sla_target_minutes": 60,
  "teams": ["MFA Team","Flex Cube","Card Management","Third Party","Open System","Unclassified"],
  "stats": {
    "business_emails": 0, "replied": 0, "pending": 0, "no_action_needed": 0, "needed_reply": 0,
    "reply_rate": 0.0, "team_replies": 0, "total_team_replies": 0,
    "average_response": 0, "fastest_response": 0, "slowest_response": 0,        // minutes
    "conversation_matches": 0, "conversation_index_matches": 0, "subject_matches": 0, "no_matches": 0,
    "sla_target_minutes": 60, "sla_breached": 0, "sla_compliance_rate": 0.0, "escalated_count": 0,
    "carryover_count": 0, "carryover_oldest_days": 0, "assigned_count": 0,
    "team_stats": {"Flex Cube": {"business_emails":0,"replied":0,"pending":0,"no_action_needed":0,"escalated_count":0,
                                 "carryover_count":0,"carryover_oldest_days":0,"response_minutes_sum":0,"response_count":0,
                                 "fastest_response_minutes":null,"sla_breached_count":0,"team_emails_sent":0}},
    "system_volume": {"Flexcube": 12},                 // canonical application -> count
    "business_address_volume": {"it.user_provisioning@nbe.com.eg": 20},
    "reply_breakdown": [{"name":"...","team":"...","count":3}],
    "top_slowest_replies": [{"subject","customer_name","team","first_reply_by","entry_id","response_minutes"}],
    "escalated_items": [{"subject","customer_name","team","current_waiting_time"}],
    "quality": {"reviewed":0,"excellent":0,"good":0,"needs_improvement":0,"appeals":0,"coverage_rate":0.0,
                "repliable_count":0,"manager_reply_count":0},
    "trend": {...}                                      // compute_trend() output, may be {}
  },
  "rows": [ {
    "subject","entry_id","message_id","detected_systems","team","all_teams":[],"teams_pending":[],"teams_replied":[],
    "customer_name","customer_email","received_time":"ISO","mailbox","folder",
    "match_method","first_reply_by","first_reply_email","first_reply_time":"ISO|null",
    "first_response_time":"1 H 5 Min","response_minutes": int|null,"duration_minutes": int|null,
    "reply_count":0,"current_waiting_time":"","status":"Replied|Pending Reply|No Action Needed",
    "sla_minutes":60,"sla_status":"Met|Breached|Within SLA|N/A","escalated":false,"reminder_count":0,
    "is_carryover":false,"age_days":0,"no_action_reason":"","reply_by_manager":false,
    "assigned_to_email":"","assigned_to_name":"",
    "last_reply_by":"","last_reply_email":"","last_reply_time":"ISO|null",   // the newest team reply (the first one is first_reply_*)
    "mail_copy":"ready|too_large|",                                          // a saved copy exists (scan time); "" = none yet
    "quality_rating":"Excellent|Good|Needs Improvement|","quality_note":"","quality_reasons":"",
    "quality_appeal":"","appeal_decision":"","rating_withdrawn":false,
    "body_preview":"<= 300 chars", "reply_body_preview":"<= 300 chars"
  } ],
  "quality": {"scorecard": [...], "team_index": {...}, "leaderboard_top": [...], "leaderboard_bottom": [...],
              "speed_quality_matrix": [...], "person_trends": [...], "repeat_issue_alerts": [...]}
}
```
The web overlay (`overlay.py`) adds to every row, from the assignments override: `assigned_*` (to, name, note, by, at) and the Done
fields `assignment_done` (bool), `done_at`, `done_by`, `done_note`, `done_auto`. A row is **done** when the assignee marked it Done, or -
with no mark - when the assignee wrote the first or the last reply in Outlook (`done_auto: true`, `done_by` empty). Done never changes
`status`, `sla_status` or any reply number: a Done mail nobody replied to is still Pending Reply. `stats` gets `assigned_done_count`
next to `assigned_count`.

History point (`ops/history/email/<day>.json`): `{"points":[{"ts","business_emails","replied","pending","no_action_needed","sla_breached","escalated"}], "final": {...stats subset...}}`.

### 5.4 Timeline events (`calllog/ops/events.py`, already written)
After each good scan the runner calls `events.diff_events(module, old_snapshot, new_snapshot)`; a failing / recovering scan
adds `events.job_event(...)`; all go to `store.add_events`. Event: `{ts, module, level: danger|warn|info|ok, kind, text, count?, items?}`.
Home shows "Today" from `GET /api/ops/events?day=`; admin/manager only (items may contain subjects).

### 5.5 E-mail classification settings ("Unclassified")
`settings.email.classification` (see `settings.py`) makes the unclassified bucket adjustable without touching code:
`system_team_map` (keyword/application -> team, wins over the built-in map), `address_team_map` (business mailbox the mail
was sent TO -> team; generalises the legacy MFA-address rule), `unclassified_team` (where mails with no detected team go
instead of "Unclassified"), `extra_no_action_keywords`. The Email page has an **Unclassified** panel (rows with team
"Unclassified", their detected words) with *Assign team* (one mail, an override) and *Create rule* (keyword -> team, saved into
`system_team_map` through `POST /api/ops/email/classification/rule`, admin or a manager for their own team, audited).
The System > Settings page has a Classification editor for the same keys.

## 6. Collector and jobs

`python collector.py` runs a loop (see `calllog/ops/runner.py`). Jobs:

| job | what it does | schedule keys in `settings.json` |
|---|---|---|
| `email` | read-only Outlook scan of today (+ carry-over) -> `snapshots/email.json` | `email.interval_minutes` |
| `email_report` | the old daily run: Excel, charts, pending `.msg` zips, report / team / assignment / quality / end-of-day e-mails, history | `email.report.send_times` (`["16:30"]`), or "Run now" |
| `sla` | portal scan -> `snapshots/sla.json`, reminder / breach e-mails with the old escalation + repeat logic | `sla.interval_minutes` |
| `itsm` | scoreboard scan -> `snapshots/itsm.json`, optional status e-mail | `itsm.interval_minutes`, `itsm.mail.send_times` |

Rules every job follows:

* Scheduled runs happen only on `general.work_days` between `general.active_hours`, never on
  `general.holidays`. **Run now** ignores that.
* An OS file lock `ops/locks/job-<job>.lock` guarantees one runner per job even with two collector PCs.
  Outlook jobs (`email`, `email_report`, and every mail send) also share one in-process lock because Outlook
  COM must not be used from two threads at once.
* A job never raises into the runner: it returns a `JobResult`.
* The runner owns writing snapshots, history points and heartbeats; the engine returns data.
* Heartbeat `ops/jobs/heartbeat/<job>.json`:
  `{"job","host","pid","running":false,"enabled":true,"started_at","last_run_at","last_ok_at","last_status":"ok|error|skipped",
    "last_error":null,"last_summary":"31 open, 4 breached","last_duration_seconds":12.4,"next_run_at","runs_ok":0,"runs_failed":0,"version":"..."}`
  The web app calls a collector **online** when `now - heartbeat updated_at < 3 x interval + 2 min`.
* `JobContext` (engines/common.py): `settings` (the module's section), `general`, `store` (OpsStore), `now()`,
  `log(msg)`, `credential(name)`, `send_mail(...)`, `debug`, `dry_run`, `no_email`, `data_dir`.
  `JobResult`: `ok`, `snapshot_data` (dict or None), `summary`, `error`, `emails_sent`, `history_extra`.

## 7. Web API (`/api/ops`, all JSON, CSRF header on writes, errors `{"error": "..."}`)

| method + path | role | notes |
|---|---|---|
| `GET /overview` | admin, manager | Home data: per-module headline numbers, `attention` list, collector health, calls today |
| `GET /<module>?since=<generated_at>` | admin, manager | snapshot or `{"unchanged": true}`; adds `meta` (heartbeat, `online`, `stale`, `last_error`); email rows are scoped to the viewer's team set (a manager with teams sees only those teams, never anybody else's rows) and have overrides applied (`overlay.py`); the email answer also carries `options` = `{teams, quality_reasons, ratings, people: [{email, name, team}]}` |
| `GET /<module>/history?days=14` | admin, manager | list of history days |
| `GET /shared` | any signed-in | section 3.1 numbers, counts only; e-mail block carries `scope` + `teams` (viewer-scoped, an employee never counts Unclassified) |
| `GET /my[?as=uid]` | any signed-in (`as`: admin, manager) | section 3.2; the e-mail block has `assigned`/`replied` (mine by name) plus `team_emails`/`team_summary`/`team_emails_hint` (my team(s), never Unclassified) |
| `GET /events?day=` | admin, manager | timeline events, newest first |
| `POST /email/classification/rule` `{keyword, team}` / `DELETE` | admin, manager (own teams) | adds / removes a `system_team_map` entry; 403 if `team` is outside the manager's team set |
| `POST /email/assign` `{message_id, assigned_to_email, assigned_to_name, note}` / `DELETE` to take it back | admin, manager | 403 if the mail is outside the manager's team set; stores the mail's `team` in the entry so the manager can still handle it after it left the scan; tells the new assignee (and the previous one, "Assignment removed") |
| `GET /email/message?message_id=` | any signed-in, see 7.1 | preview JSON + `{state, has_msg, source, mail, assignment, can}`; one audit line per open |
| `GET /email/message/download?message_id=` | any signed-in, see 7.1 | the `.msg` as an attachment; one audit line per download |
| `POST /email/message/prepare` `{message_id}` | any signed-in, see 7.1 | "Prepare now": queues one `email` run request when the copy is not saved yet |
| `POST /email/done` `{message_id, note?}` / `DELETE /email/done` (reopen) | the assignee, or a manager / admin of the mail's team | 7.1; idempotent; audited; notifies the other side |
| `GET /notifications?since=<ts>` / `POST /notifications/read` `{ids:[..]\|"all"}` | any signed-in | 7.2; always the caller's own file |
| `POST /email/no-action` `{message_id}` / `DELETE` to undo | admin, manager | 403 if the mail is outside the manager's team set |
| `POST /email/team` `{message_id, team}` | admin, manager | 403 if the mail is outside the manager's team set |
| `POST /email/quality` `{message_id, rating, note, reasons}`; `/email/quality/appeal`, `/email/quality/decision` | admin, manager (appeal: the replier) | |
| `GET /jobs` | admin | heartbeats + pending requests + online flag + `collector_info` (written by the collector into `ops/state/collector_info.json`: host, config path, pinned keys, per-account `username` + `has_password`; never a password) |
| `POST /jobs/<job>/run` | admin, manager (`email`,`sla`,`itsm`), admin (`email_report`) | writes a request file; returns `{queued: true}` |
| `GET /jobs/<job>/log?lines=200` | admin | tail |
| `GET /settings`, `PUT /settings` | admin | full settings, validated (section 9) |
| `GET /reports`, download via `/export/ops/report/<module>/<name>` | admin, manager | no path traversal |
| `GET /export/ops/<module>.xlsx` | admin, manager | built from the snapshot |

Every write action calls `auth.audit(...)` with an `ops_*` action name (`ops_done`, `ops_reopen`, `ops_mail_open`, `ops_mail_download`,
`ops_mail_prepare`, `ops_mail_denied`, `ops_notifications_read`, ...).

### 7.1 Opening, downloading and finishing an assigned e-mail (docs/FEATURES-V2.md, feature B)

**Who may open / download / mark done / reopen one e-mail** (`api.may_open_mail`, the one rule behind all five routes):

| person | may handle |
|---|---|
| the person the mail is assigned to (`assignments[message_id].assigned_to_email` equals their account `email`, any role) | that mail, even if it later became "Unclassified" or left the scan |
| a manager | any mail of their teams (the same scope rule as the E-mail page; a manager with no team or `__all__` sees every team); a mail that left the scan is judged by the `team` stored in its assignment |
| an administrator | everything that exists (in the scan, assigned, or with a saved copy) |
| any other employee, anonymous | nothing (401 for anonymous) |

A mail that does not exist and a mail the person may not handle get the **same answer** (`404 {"error": "That e-mail is not available."}`),
so ids cannot be probed; a denied attempt by a signed-in person leaves an `ops_mail_denied` audit line. An empty, control-character or
longer-than-500-character id is `400` (checked before anything touches the disk). Done / Reopen refuse (`403`) when the page is somebody
else's (`?as=<user>`); opening and downloading use the viewer's own rights and never the viewed person's.

`state` of a copy: `ready` (preview and `.msg` saved), `too_large` (preview only; open it in Outlook), `preparing` (the mail is assigned or,
with the default scope, still pending, but the collector has not saved it yet; `can.prepare` is true), `unavailable` (nothing saved and
nothing planned, e.g. a replied mail nobody holds). Without a saved copy the answer falls back to what the scan knows (`source: "snapshot"`,
the first 300 characters of the text, `truncated: true`). `can` = `{download, prepare, done, reopen}` for this person on this mail.

Done / Reopen: Done writes `done_at, done_by, done_by_id, done_note` into the assignment (note <= 300 characters), Reopen removes them; both
are idempotent (`already: true`, 200, no second audit line or notification); Reopen of a mail the assignee answered in Outlook is `409` (the reply
cannot be taken back). Re-assigning to another person, or unassigning, clears the mark; a new note for the same person keeps it.

### 7.2 Notifications

`GET /notifications?since=<ts>` returns `{unread, items (the 30 newest), ts, seen_ts}`, or `{unchanged: true, unread, ts}` when `since` is the
`ts` the person already has (`ts` changes on every write to their file: a new item or a read mark). The file is the caller's own - the id
comes from the session, there is no parameter for another person. `POST /notifications/read` marks `ids` (<= 200) or `"all"` read.

| event | told | title | link |
|---|---|---|---|
| assigned (a different person than before) | the assignee, if an account with that mailbox exists | New e-mail assigned to you | `/my?mail=<id>` |
| unassigned / reassigned | the previous assignee | Assignment removed | `/my?mail=<id>` |
| done by the assignee | whoever assigned it + every active manager whose teams include the mail's team (an administrator only as the assigner); never the actor, nobody twice | `<name> marked an e-mail as done` | `/email?tab=pending&open=<id>` |
| done by a manager for the assignee | the assignee | `<name> marked an e-mail as done` | `/my?mail=<id>` |
| reopened | the other side (assignee -> assigner + managers; manager -> assignee) | E-mail reopened | per recipient |
| replied (the assignee answered in Outlook) | assigner + team managers, once (`replied_notified`) | `<name> replied to an assigned e-mail` | `/email?tab=pending&open=<id>` |

The collector only writes snapshots, so the *replied* notification is created by the web app: every notification poll checks (memoised on
the snapshot and the assignments, so almost always free) for rows that are Replied by their assignee and not yet announced; the flag is
set under the assignments lock, so concurrent polls and several servers announce a reply once. Items never hold a mail body; title and text are
cut to 200 characters. A notification that fails to be written is logged and never undoes the action that caused it.

## 8. Pages

`/hub` Admin dashboard (Home), `/my` My work (employee), `/calls` = existing admin dashboard (`/admin` stays), `/email`, `/sla`, `/itsm`, `/system`.
After sign-in: `admin`/`manager` -> `/hub`; `user` -> `/my` (its top bar links to `/dashboard` for logging calls). One shared top bar with module links
(`templates/_topbar.html`). Strict CSP stays (`script-src 'self'; style-src 'self'`): **no inline scripts or
style attributes**; everything from `static/*.js|css`. All scraped / e-mail text is inserted with
`textContent`. Arabic text uses `dir="auto"`. Same visual language as the Call Log (`app.css` tokens).

## 9. Settings (`ops/settings.json`)

See `calllog/ops/settings.py` for the authoritative defaults and validation. Lists of e-mail addresses are stored lower-case,
de-duplicated, sorted. Unknown keys are preserved, invalid values are rejected with a message naming the key.

`email.mail_copies` - the saved copies of mails (section 4, docs/FEATURES-V2.md B1):
`{"scope": "assigned_and_pending" | "assigned" | "off", "max_per_scan": 40, "max_msg_mb": 25, "keep_days": 21}`. The `email` job saves, at
the end of each scan, a copy of the assigned mails and (default scope) of the mails still waiting for a reply: assigned and not done first, then
the oldest pending first, at most `max_per_scan` new copies per scan, nothing that already has its two files. Each mail is fetched again by
EntryID (with its store id) *one at a time* and released at once - the scan never keeps a live COM object, because whether a mail is pending is only known
after reply matching, which is after the walk through the folders. A mail bigger than `max_msg_mb` gets a preview only. A COM failure on one mail is
logged (never with its subject) and skipped. Pin it per machine in `collector_config.json` like any other key.

## 10. Credentials and `collector_config.json`

The file `collector_config.json` (path: `--config`, env `CALLLOG_COLLECTOR_CONFIG`, else next to `collector.py`) is the **one
place for users, passwords and machine-specific values**. Same shape as the settings, plus `username` / `password`:

```json
{
  "sla":   {"username": "1001", "password": "...", "base_url": "https://nbesrv.nbe.ahly.bank"},
  "itsm":  {"username": "1001", "password": "..."},
  "email": {"target_archive_name": "2026_Part3"},
  "data_dir": "data"
}
```

* Any settings key written there **wins** over the value saved from the System page (`CollectorConfig.apply`); the System page
  lists the pinned keys (`pinned_keys()`) as read-only. Keys starting with `_` are comments.
* `password` is read by the collector only (`collector_config.get_password`): env `NBE_SSO_PASSWORD` / `ITSM_PASS` first, then the file,
  then Windows Credential Manager (`python collector.py --set-password sla|itsm`). It is stripped from the overlay, so it can never reach
  `settings.json`, a snapshot, a log line, an API answer or a debug dump.
* A missing password is a clear error in the job log and on the System page - the collector never prompts in the background.
* `collector_config.example.json` is committed (empty values). `collector_config.json` is git-ignored and excluded from the ZIP builder.
* `python manage.py import-legacy` reads the old scripts (never runs them) and writes both `settings.json` and a ready
  `collector_config.json` (the users / passwords / archive name found in the old scripts).
* The web app shows only `has_password: true/false` for each account.

## 11. What is intentionally NOT carried over from the old scripts

* The old standalone HTML dashboards and the `--serve` HTTP server (`DASHBOARD_USERS` with default passwords): replaced by the
  authenticated pages above.
* Passwords and recipient lists written inside the code: passwords are now in `collector_config.json`, lists in settings
  (import both with `python manage.py import-legacy ...`).
* pandas (replaced by plain Python), `win32com` at import time.

## 12. Engine entry points (what the runner calls)

```python
# calllog/ops/engines/email_engine.py
def run_scan(ctx, namespace=None) -> JobResult            # job "email"; `namespace` lets tests inject a fake Outlook
# calllog/ops/engines/email_report.py
def run_report(ctx, namespace=None) -> JobResult          # job "email_report"
# calllog/ops/engines/sla_engine.py
def run(ctx, scraper=None) -> JobResult                   # `scraper(ctx) -> (headers, rows)` replaces the browser in tests
# calllog/ops/engines/itsm_engine.py
def run(ctx, browser_factory=None) -> JobResult           # `browser_factory(ctx) -> Browser-like` replaces Chrome in tests
```

`ctx` is `engines.common.JobContext`. Engines read `ctx.settings` (their own section) and `ctx.general`, log with `ctx.log`,
send mail only through `ctx.mailer` and only when `ctx.can_email()`, read passwords only through `ctx.password(kind, username)`,
keep private state in `ctx.store.read_state/write_state`, and never write snapshots or heartbeats themselves
(the runner does). Overrides are read with `ctx.store.overrides(name)`.

## 13. Demo data

`calllog/ops/demo.py` writes contract-valid snapshots, history and heartbeats into an `OpsStore`
(`python manage.py seed-demo`), used by tests, screenshots and for trying the pages before any collector runs. It also seeds the
assigned-mail workflow: for employee 1001 two open assignments, one marked Done (still waiting for a reply) and one answered in Outlook
(done by itself); one open assignment for 1002 with no saved copy yet (state `preparing`); saved copies for them and for six pending
mails (fake `.msg` bytes that are NOT real Outlook messages, a preview with Arabic and English bodies and attachment names, one
"too large" preview-only mail); and 4 notifications each for 1001 and for the manager `mgr.flex`, telling that story.

## 14. Performance budget - 60 people at the same time

Planning load: ~50 employees on `/my` (poll every 30 s) or logging calls (existing page, already tuned and measured in the README),
~10 admins / managers on Home, Email, SLA, ITSM (poll every 15 s), everybody signing in around 08:00. About 3-5 requests per second
sustained on a 4-core server PC with the data on a network share.

What keeps it cheap, and what every endpoint must respect:

* The web app reads **three snapshot files and a few small state files** - never the collector, never Outlook, never the call records of
  other days. A request must not scan a folder whose size grows (events, history, reports are read by day / by name only).
* A snapshot file is parsed **once per change per process** (OpsStore keys its cache on size + mtime and re-checks the share at most once
  per second). `?since=<generated_at>` is answered from the cached envelope without serialising anything (< 5 ms).
* Anything computed from a snapshot for many people is computed **once and shared** (like `Store.memo` for the Call Log): the overlaid + team-scoped
  e-mail answer per (team scope, snapshot version, overrides version), the `shared` strip, the Home overview, per-user indexes for `/my`
  (rows by assigned e-mail / replier e-mail / security name built once per snapshot). Large JSON answers are cached serialised and gzipped.
* Employees only ever get their own rows (small answers). Admin answers are the big ones; they are cached per scope.
* Browsers poll with a random start offset, back off on errors, stop while the tab is hidden, and never poll faster than the numbers above.
* The bell (`/notifications`, every 30 s per person) reads ONE small file per person through the store's cached read; those files are not
  followed by the 1 s refresher (60 of them would be 60 share round trips a second), a poll looks at its own file once its 1 s cache time is up.
  Measured with the Flask test client, 60 people, a 5 ms delay on every file operation and no cache: `?since=` unchanged p95 ~6 ms, full answer
  p95 ~7 ms (without the delay ~1 ms). Saved mail previews are read straight from disk (a few dozen KB, only when somebody opens a mail).
* Targets on the reference demo data (500 e-mail rows, 300 SLA requests): `?since=` unchanged < 5 ms, full e-mail answer p95 < 150 ms,
  `/my` p95 < 100 ms, `/shared` < 20 ms, with 60 simulated users and an extra 5 ms delay on every file operation. `tools/loadtest_hub.py`
  measures this and its result is quoted in the README.

## 15. Running 24/7

The web server and the collector are expected to run all day, every day, for months, and to survive reboots, Outlook crashes and
network hiccups without anybody touching them.

**Web server (`run.py --service`)** - already supervised by Task Scheduler + its own restart loop. Additionally: bounded caches only,
`server.log` rotation, a public `GET /healthz` (no sign-in; JSON `{ok, time, collectors: {job: online}}`, nothing else) that an
existing monitoring tool can poll, and a daily housekeeping step (below).

**Collector (`collector.py --service`)** - same pattern as `run.py --service`: a small supervisor that restarts the runner if it dies and keeps
the Startup-folder / Task Scheduler entry. Outlook COM and the SSO browser need an interactive Windows session: the collector PC must
stay logged in (a locked screen is fine) - say so in the README.

Rules for 24/7 safety:

* **Jobs run in child processes** (`collector.py --run-job <job>` started by the runner, result handed back as a JSON file) with a hard
  timeout per job (email 20 min, email_report 30 min, sla 5 min, itsm 10 min, settings can lower them) and a kill of the whole process tree
  (Outlook scan, Chrome, Playwright) on timeout. A hung COM call or a leaking browser can therefore never freeze the collector or grow
  memory for days.
* **Never hammer a login.** A wrong or expired password (`JobResult.error_kind == "auth"`) stops the scheduled runs of that job until
  the credential changes (collector_config.json mtime / password source changed) or an admin presses *Run now*; the System page and Home show
  "login failed - fix the password, retries paused". This protects the SSO account from a lock-out.
* **Back-off**: after repeated failures the retry interval grows (1x, 2x, 4x ... capped at 30 min) and resets on success; the heartbeat says so.
* **Heartbeat stamps carry epoch seconds** (`updated_ts`) next to the local ISO time, and online / stale decisions use the epoch, so daylight-saving
  changes (Egypt uses DST) and small clock differences between the PCs (tolerance 2 minutes) cannot make a collector look offline.
* **Housekeeping once a day** (runner, outside business hours): prune history (60 days), events (30), reports (60 days / 30 copies), debug dumps (7),
  job logs (rotation), stale run requests (> 1 day), saved mail copies (a copy goes `keep_days` days after its mail left the assigned / pending set;
  a `.msg` without its preview, or a preview that promises a missing `.msg`, once an hour old; temp files; the countdown is kept in
  `ops/state/mail-copies.json`), notifications older than 30 days, and write the result to the job log. Nothing under `records/` or `logs/audit-*` is touched.
* **Midnight / weekend**: scans outside the active window are skipped but the heartbeat keeps ticking, so "online" stays true and the pages show
  "Outside working hours - showing the last scan from <time>" instead of an alarm. Settings can make the schedule all-day (`active_hours: ""`, every
  `work_days`) for real 24/7 scanning.
* Writes are atomic and locked, readers tolerate a half-finished copy (retry once) - a network share hiccup must never crash a page or a job.
