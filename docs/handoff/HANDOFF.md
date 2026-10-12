# NBE Operations Hub - handoff prompt

Paste everything below the line into a new Claude Code session opened on this repository
(`EslamDawood86/CALLLOG`, branch `claude/inspiring-carson-v5va9x`). It is written so the new session can continue
without any other context. The long agent briefs it refers to are in `docs/handoff/briefs/`.

---------------------------------------------------------------------------------------------------

## ROLE AND GOAL

You are continuing a big job for an Egyptian bank IT team lead (the user writes Egyptian Arabic mixed with English; answer in short,
plain Egyptian Arabic, keep technical names in English). The user had **four stand-alone Windows scripts** and wants **one complete monitoring
application** that follows everything in their sector, running **24/7 from a Windows shared folder (`\\10.104.46.34\calllog`)** together with
the NBE Call Log app that already exists in this repo:

1. **NBE Call Log** - Flask app, employees log incoming calls, admins see live statistics (this repo, already in production use).
2. **Daily Team Reply Analytics** (Outlook COM, 12k-line script) - which business e-mails got a team reply, SLA, teams, quality, Excel + HTML reports.
3. **SLA monitor** (Playwright on an SSO portal) - security requests waiting at the "security user" for more than 1 h (reminder) / 2 h (breach).
4. **ITSM status** (CA SDM through Chrome DevTools) - Implementation / Rollback request counts per role.

The three legacy scripts are NOT in the repo (they hold a real password and real employee addresses). If you need to re-read them, ask the user to upload them again:
`Daily_Team_Reply_Analytics_Fast_Scan_2026.py`, `SLA.PY`, `itsm_status.py`. Everything useful from them was already ported (see status).
**Never copy their passwords, user numbers or employee e-mail addresses into the repo, tests, docs or logs.** Use fake data (`alice@example.test`).

## THE USER'S REQUIREMENTS (collected over the conversation - all still valid)

* One integrated app: Calls + Email + SLA + ITSM, working on the shared folder together with Call Log.
* **Admin dashboard** sees everything and the current state. The **normal employee** has his own page ("My work") with everything that is his, under his user,
  plus a **shared "Team status" strip** that is the same component/numbers for both (counts only).
* **Unclassified e-mails** (e-mails that no team could be assigned to) must be **adjustable from Settings** (keyword -> team rules, mailbox -> team rules, a default team),
  and **must never appear to a normal employee**: an employee sees only the e-mails of **his own team(s)**.
* **An employee can belong to more than one team.**
* A **config file** holds the users and **passwords** the collectors need and the **Outlook archive name** etc. -> `collector_config.json` (done, see docs/HUB-DESIGN.md section 10).
* It must handle **60 users at the same time** (performance budget in docs/HUB-DESIGN.md section 14) and **run 24/7** (section 15).
* **NO `.bat` / `.cmd` / `.ps1` / `.vbs` / `.exe` files in the output** - everything starts with `python run.py` / `python collector.py` (the ZIP builder fails if one is present).
* "Fix any error you face, for example at the front end and the runner."
* The final delivery is a **ZIP in the same layout as the user's `NBE-CallLog.zip`** (`READ-ME-FIRST.txt`, `program/`, `guides/` with EN + AR user guides and the employee quick card)
  containing the whole Hub, built with `python docs/build/build_package.py`. Refresh the guides for the new pages (docs/build has the Node/LibreOffice tooling).
* "Full app monitoring": health of the application itself, collectors, disk, timeline of events.

## REPOSITORY, ENVIRONMENT, HOW TO RUN

* Repo `EslamDawood86/CALLLOG`, work branch **`claude/inspiring-carson-v5va9x`** (push there only; do not open a PR unless asked).
* Python venv used so far: `/tmp/claude-0/venv` (recreate: `python3 -m venv --system-site-packages /tmp/venv && /tmp/venv/bin/pip install flask waitress openpyxl pytest keyring playwright matplotlib`).
  Playwright's Chromium is under `/opt/pw-browsers` (the sandbox build is chromium-1194; the browser tests pick it through `settings.browser_executable`).
* Tests: `python -m pytest -q` (whole suite ~1-2 min incl. real-Chromium end-to-end tests; the three browser files are `tests/test_sla_engine.py`, `tests/test_itsm_engine.py`, `tests/test_sla_itsm_pages.py`).
* The app: `python run.py` (this PC only) / `python run.py --share`; collector: `python collector.py --check`, `python collector.py --once sla --no-email`.
* Demo data for trying pages: `calllog/ops/demo.py` (`seed(ops_store)`, `seed_users_and_calls(call_store)`); `python manage.py seed-demo`.
* Windows-only code (Outlook COM, pythonw, powershell, msvcrt) cannot run in the Linux sandbox - it is kept close to the legacy code, imported lazily and tested only through fakes.
* **Usage limits**: the previous session hit the 5-hour limit twice; 4-agent bursts failed with HTTP 429. Run at most 2 agents at a time, give each agent a narrow file ownership, commit and push after every finished piece.

## ARCHITECTURE (read docs/HUB-DESIGN.md - it is the contract)

Collectors (Outlook / SSO portal / ITSM) run on a Windows PC and write **JSON snapshots** to `data/ops/` in the shared folder; the Flask app only reads them and lets people act
(assign, no-action, team change, quality review, Run now, settings). Roles: `admin`, `manager` (new), `user` (employee). Strict CSP (no inline script/style), CSRF on writes, audit log for every action.

```
calllog/ops/store.py settings.py schemas.py events.py collector_config.py   foundation (done, tested)
calllog/ops/engines/common.py email_engine.py email_rules.py email_report.py sla_engine.py itsm_engine.py
calllog/ops/api.py overlay.py health.py exports.py views.py demo.py          web side
calllog/ops/runner.py legacy_import.py  +  collector.py  manage.py           collector side
calllog/static/ops-common.js hub.js sla.js itsm.js ...  templates/hub|email|sla|itsm|my|system.html
```

## STATUS (as of this hand-off)

| piece | state | tests |
|---|---|---|
| Foundation: OpsStore, settings (incl. `email.classification`), schemas, events diff, collector_config, engine common | **done** | `tests/test_ops_foundation.py`, `test_collector_config.py` |
| E-mail scan engine (`email_engine.py`, `email_rules.py`) incl. configurable Unclassified, TO-only business filter | **done** | 99 tests, `tests/fake_outlook.py` |
| SLA engine (`sla_engine.py`) incl. Excel/e-mail/escalation, error_kind "auth" | **done** | 114 tests (11 real Chromium) |
| ITSM engine (`itsm_engine.py`) CDP client, changes, slot-based e-mail | **done** | 65 tests (3 real Chromium) |
| Backend API (`ops/api.py`, `overlay.py`, `health.py`, `exports.py`, roles, Employees tab) | **done**, multi-team part PARTIAL | `test_ops_api.py`, `test_ops_my.py` |
| Multi-team users + employee sees only own teams' e-mails, never Unclassified | **PARTIAL** - code edits made, `tests/test_ops_teams.py` + admin.js checkbox UI + docs missing (a workflow wave was scheduled for it) | brief: `briefs/multi-team.md` |
| `email_report.py` (Excel, e-mails, .msg zips, assignment/quality/EOD mails) | **done** - `tests/test_email_report.py` green (57 tests) | brief: `briefs/email-report.md` |
| `runner.py`, `collector.py`, `legacy_import.py`, manage.py `import-legacy`/`seed-demo` | written, tests pass; **24/7 hardening still to do**: child-process jobs + timeouts, auth pause, back-off, epoch `updated_ts`, `--service`, housekeeping | `test_runner.py`, `test_legacy_import.py`; brief `briefs/runner-collector.md` + `hardening-24x7-and-performance.md` items 1-5 |
| Front-end foundation: `views.py`, `demo.py`, templates, `ops-common.js`, `hub.js`, `ops.css`, `system.js` | **mostly done** - Home and System render and have tests (`tests/test_ops_views.py`, `test_ops_demo.py`); still check the shared strip / timeline / classification editor in a real browser and look at screenshots | brief: `briefs/front-foundation.md` |
| Pages: `sla.js`, `itsm.js` | working with tests (`tests/test_sla_itsm_pages.py`); integrate with the final `ops-common.js` API | briefs: `page-sla-itsm.md`, `page-sla-itsm-integrate.md` |
| Pages: Email (`email.js`), My work (`my.js`) | **not started** (`email.html` / `my.html` templates exist as drafts) | briefs: `page-email.md`, `page-my.md` |
| Perf (60 users) | **done**: `OpsStore` background refresher (stale-while-revalidate, remembers missing files), `Store.memo` serves the previous result while one request recomputes, `settings.load` cache, `GET /healthz`, `python tools/loadtest_hub.py` | `tests/test_ops_perf.py` (14); measured with the real intervals: unchanged p95 3.8 ms, e-mail full 22 ms, /my 98 ms (cold first open), /shared 5.8 ms (5 ms added per file operation). At 5x polling speed unchanged p95 is ~6 ms. Put final numbers in README |
| Review (security, privacy scrub, legacy fidelity), docs, guides EN/AR, ZIP | **not started** | - |

**Workflow run 2 result:** Wave 1 (e-mail report, front foundation) finished. Waves 2-4 (multi-team, runner hardening, Email page, My work page, SLA/ITSM integration) did NOT run - every agent got "You've hit your session limit" (reset 9:50 pm UTC). `tests/test_ops_teams.py` (619 lines, untracked until now) is a **draft written by the cut-off multi-team agent: 6 pass, 1 fails, 29 error (missing fixtures/helpers)** - finish it with `briefs/multi-team.md`; do not delete it.

Whole-suite result at hand-off #2: **772 passed, 2 skipped, 1 xfailed** (Python 3.13, ~3 min incl. real-Chromium tests). A Python 3.9 venv exists at `/tmp/claude-0/py39` (`uv venv --python 3.9`, deps installed incl. matplotlib/playwright) - rerun the suite there before the final ZIP (the first 3.9 run only failed in files that were still being written, not on syntax).
Known flake seen once: `tests/test_sla_itsm_pages.py` reported `request failed .../api/ops/events` (a page request cancelled while the test closes) - check `ops-common.js` polling/abort handling.
Smoke test: the app starts, an admin can open `/hub /email /sla /itsm /system /my?as=admin /admin` (200) and `/api/ops/overview|shared|sla|health` (200).

## DECISIONS ALREADY MADE (keep them)

* Business e-mail filter counts **TO only** (the code did; one legacy comment saying "TO or CC" was wrong).
* Classification precedence: manual team override > `address_team_map` > system words (admin `system_team_map` beats built-in) > `unclassified_team` > "Unclassified".
* A mail addressed to several teams stays one row per team (`row_id = message_id|team`).
* Overrides (no-action, team, assignment, quality) are stored in `data/ops/overrides/email/*.json` by the web API and applied on every read (`overlay.py`) and by the collector.
* The old standalone HTML dashboards and the `--serve` HTTP server and `DASHBOARD_USERS` default passwords were **dropped on purpose**; pandas is not used.
* Passwords: env var > `collector_config.json` > Windows Credential Manager. Never in settings/snapshots/logs/API. A wrong password (`error_kind "auth"`) must pause retries (SSO lock-out protection) - runner part not yet done.
* Employees: `mine` (assigned to / replied by them) stays visible even if later Unclassified; the team list excludes Unclassified; the shared strip's e-mail numbers are scoped to the viewer.
* Chrome profile for ITSM is local (`%LOCALAPPDATA%\NBE-Ops\itsm_profile`), not on the share.

## WHAT TO DO NEXT (in this order)

1. `git pull`; run the whole suite; read `docs/HUB-DESIGN.md` and the briefs you need.
2. (`email_report.py` is finished and green.)
3. Finish the multi-team work (brief `multi-team.md`), then update HUB-DESIGN sections 3/3.1/3.2/7.
4. Finish the front-end foundation (brief `front-foundation.md`): `ops.css`, `system.js` (Collectors / Settings incl. Classification editor / Logs / Reports / Health), fix demo data, shared strip, timeline, "view as employee".
   Send `rev=<meta.rev>` together with `since` when polling (see the backend report: otherwise an override made after the scan forces a full answer).
5. Build the pages: Email incl. the **Unclassified** panel (brief `page-email.md`), My work (`page-my.md`), integrate SLA/ITSM (`page-sla-itsm-integrate.md`). Drive them in a real browser (Playwright), LOOK at screenshots, no console errors / CSP violations.
6. Hardening 24/7 (`hardening-24x7-and-performance.md` items 1-5: child-process jobs with timeouts, auth pause, back-off, epoch heartbeat, collector `--service`, housekeeping). Performance, `/healthz` and the load test are DONE (`tools/loadtest_hub.py`); start the refresher with `calllog.app.start_refresher(app)` (run.py does).
7. Review in parallel lenses: security (CSRF, XSS - everything via textContent, path traversal, secrets, role leaks - especially "employee never sees Unclassified"), privacy scrub (grep for real names/addresses/password/user number), legacy fidelity of each port, Windows compatibility (paths, Python 3.9 compatibility, no `.bat`), UX/accessibility.
8. Docs + package: README for the Hub (setup in 10 steps, collector on a PC that stays logged in, Task Scheduler for the web server, `collector_config.json`), EN + AR guides and employee quick card refreshed, `python docs/build/build_package.py`, ZIP layout `READ-ME-FIRST.txt / program / guides`, the ZIP builder must exclude `collector_config.json`, `data`, `libs`, tests; no `.bat`.
9. Commit + push to `claude/inspiring-carson-v5va9x`; tell the user in short Egyptian Arabic what works, what could NOT be verified here, and the one-time steps on their side.

## NOT VERIFIED IN THE SANDBOX (say so honestly in the final answer)

Real Outlook COM behaviour and Outlook rendering of the e-mails; the real SSO portal and real CA SDM markup (selectors may need tuning); Windows paths (`%LOCALAPPDATA%`, Startup shortcut, pythonw, powershell kill); Python 3.9/3.10 on the user's PC;
behaviour on the SMB share (locks/latency); the 60-user load on real hardware; real login lock-out behaviour.

## SAFETY RULES

* Do not commit `collector_config.json`, `config.json`, `data/`, `libs/`, `.secret_key`. Before every commit: `grep -rIn -E "<real password>|<real user number>" .` and look for real employee names/addresses.
* The user pasted a real password into the legacy scripts: recommend they change it after moving it into `collector_config.json`.
* Windows output must contain no `.bat/.cmd/.ps1/.vbs/.exe`.
* Insert untrusted text only with `textContent`; escape HTML in e-mails; no inline scripts/styles (CSP).
