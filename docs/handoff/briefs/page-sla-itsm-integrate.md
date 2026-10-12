# Agent brief: page-sla-itsm-integrate

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

YOUR TASK: another agent wrote calllog/static/sla.js, itsm.js, ops-sla.css and tests/test_sla_itsm_pages.py BEFORE the shared front-end existed (it had to guess the helper API). The shared front-end (views.py, templates, ops.css, ops-common.js, hub.js, system.js, demo.py) is finished now. Make the SLA and ITSM pages work for real on top of it.
OWNERSHIP: calllog/static/sla.js, itsm.js, ops-sla.css, tests/test_sla_itsm_pages.py (you may fix small bugs you find in the shared files only if the owner cannot - list every such edit in your report).
Do: read ops-common.js (header comment = helper API), templates/sla.html + itsm.html, demo.py; replace every local fallback/guess in sla.js and itsm.js with the real helpers; run the pages against demo data in a real browser (Playwright), check KPIs, filters, sorting, the live age, the run-now flow, dark mode, the freshness banner, empty states and the error-role variant; look at the screenshots (Read the png files) and fix every visual or functional problem; no console errors or CSP violations. Keep the final screenshots docs/img/ops-sla.png and docs/img/ops-itsm.png.
