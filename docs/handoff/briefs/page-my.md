# Agent brief: page-my

PROJECT: /home/user/CALLLOG is the NBE Call Log web app (Flask, file-based storage on a shared folder). We are extending it into the NBE Operations Hub: four old stand-alone Windows scripts become collectors + web pages inside one application.
READ docs/HUB-DESIGN.md FIRST - it is the contract (data layout, snapshot schemas, API, roles, engine entry points, credentials, collector_config.json).
Already written, tested and committed - read them, do not rewrite them: calllog/ops/store.py (incl. events), settings.py (incl. email.classification), schemas.py, events.py (timeline diff), collector_config.py, engines/common.py, tests/test_ops_foundation.py, tests/test_collector_config.py. The existing app is under calllog/ (app.py, api.py, auth.py, storage.py, static/, templates/) - follow its conventions.

LEGACY SCRIPTS (read-only; never modify, import or execute them):
  EMAIL (Outlook COM, 12k lines): /root/.claude/uploads/19104c95-e70a-5581-94ec-2168a72ad886/cefaafe4-Daily_Team_Reply_Analytics_Fast_Scan_2026.py
  SLA (SSO portal, Playwright):   /root/.claude/uploads/19104c95-e70a-5581-94ec-2168a72ad886/37a6fe6a-SLA.PY
  ITSM (CA SDM, Chrome DevTools): /root/.claude/uploads/19104c95-e70a-5581-94ec-2168a72ad886/c09da970-itsm_status.py
They contain a real password and real employee e-mail addresses. NEVER copy any password, user number, employee/team e-mail address or the embedded base64 logo into the repo, tests, docs, logs or your report. Tests use obviously fake data (alice@example.test, "secret-pw", user 1001, ...). Generic domain vocabulary (Arabic/English keywords, application names, team names) may be ported.

ENVIRONMENT: python = /tmp/claude-0/venv/bin/python (Linux sandbox; Flask, pytest, playwright with Chromium at /opt/pw-browsers, openpyxl, matplotlib, keyring are installed; NO pandas allowed in the project; no pywin32, no Outlook). Run tests with:  cd /home/user/CALLLOG && /tmp/claude-0/venv/bin/python -m pytest -q <file>.  Before you finish, run the WHOLE suite. The machine has only 4 CPUs shared with another agent: do not run heavy loops, kill any browser/server you start.
Windows-only code (Outlook COM, pythonw, powershell, msvcrt) cannot run here: keep it as close to the legacy as possible, import it lazily, guard it with os.name checks, and test everything else through the injection points of HUB-DESIGN section 12.

RULES
1. You own ONLY the files listed under OWNERSHIP. Other agents are editing other files in this same working tree right now; never edit their files (put what you need from them in needs_from_others). NEVER run git add/commit/checkout/stash/reset/clean - the lead commits.
2. Be faithful to the legacy behaviour unless this brief says otherwise; every deliberate difference goes into behaviour_changes in your report.
3. No secrets or personal data anywhere. Never log or return a password. Treat scraped / e-mail text as untrusted: HTML-escape it in e-mail and HTML, and the web UI only uses textContent.
4. Everything must import on Linux with only the standard library + flask/openpyxl; matplotlib, playwright, win32com, pythoncom, keyring are imported lazily inside functions.
5. Code style of calllog/: a module docstring saying what and why, small functions, comments only for non-obvious why, no type-annotation noise, f-strings.
6. Write real pytest tests for your area, run them, fix failures. Report honestly: if something could not be verified here (needs Windows/Outlook/bank network) say exactly what.
7. The user asked that users, passwords and the Outlook archive name come from a config file (collector_config.json, see HUB-DESIGN section 10): engines get passwords only via ctx.password(kind, username) and settings (already overlaid with the file) via ctx.settings - never read the file or environment themselves.

YOUR TASK: the employee page "My work" (/my) - everything that belongs to the signed-in employee under their own user, plus the shared Team status strip.
OWNERSHIP: calllog/static/my.js, calllog/static/ops-my.css (optional), tests/test_my_page.py. The shell calllog/templates/my.html, the /my route, the top bar, ops-common.js (OPS.sharedStrip etc.) and ops.css belong to the front-end foundation agent: use them, do not edit them (missing helper -> write it locally in my.js and list it in needs_from_others).
READ: docs/HUB-DESIGN.md sections 3, 3.1, 3.2; calllog/ops/api.py (GET /api/ops/my and /shared, tests/test_ops_my.py), calllog/ops/demo.py (seed + seed_users_and_calls), calllog/static/ops-common.js header, calllog/static/user.js + common.js (the existing call-logging page you link to).

Build (employee sees only their own data; admin/manager can open /my?as=<uid> read-only and then see a clear "Viewing <name> - read only" banner with a link back):
- Header: greeting, date, quick link "Log a call" (/dashboard), freshness of the data.
- Team status strip (OPS.sharedStrip): same component and numbers as the admin dashboard.
- My calls: today / yesterday / last 7 days tiles, split Resolved / Transferred / Ticket Inquiry (use the status colours of the Call Log), calls per hour today chart, my share of the team total if the API provides it.
- My e-mails: assigned to me (pending first, waiting time, SLA colour, subject/customer shown because they are mine), e-mails I replied to today (count, average first reply, SLA met/breached), my quality reviews with rating chips, notes and an Appeal button for low ratings (POST the appeal API; show the decision when there is one). Honest empty states when the account has no e-mail address set ("Ask your administrator to add your e-mail address to your account").
- My security requests: requests assigned to my security_name with status chip and live age; empty state explaining security_name is not set.
- Polling every 30 s (pause when hidden), no flicker on refresh, keep scroll. Everything via textContent / CL.h. Dark mode, keyboard accessible, Arabic dir=auto, works from 1024px width (employees may use small screens).
Tests (tests/test_my_page.py): the server-side gating (employee 200, admin without ?as redirected, ?as allowed for admin/manager, anonymous -> login) and a real-browser Playwright test (skip cleanly if Chromium is unavailable) with demo data + demo users: log in as an employee and check own numbers equal the API, that another employee's subjects never appear anywhere in the page text, the admin "view as" banner, no console errors / CSP violations. Screenshot at 1280x800 (light) and one dark, LOOK at them (Read the png files), fix problems, keep only docs/img/ops-my-work.png.
