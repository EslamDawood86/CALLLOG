# Agent brief: 24/7 hardening + performance (NOT STARTED) - implement docs/HUB-DESIGN.md sections 14 and 15

1. runner.py: run every job in a CHILD PROCESS (`collector.py --run-job <job>`, result handed back as a JSON file) with a hard timeout per job (email 20 min, email_report 30,
   sla 5, itsm 10; settings may lower) and kill the whole process tree (Outlook scan, Chrome, Playwright) on timeout.
2. JobResult.error_kind == "auth" (already produced by the SLA and ITSM engines) pauses scheduled retries of that job until the credential changes
   (collector_config.json mtime / password source) or an admin presses Run now; System page + Home show "login failed - retries paused". Protects the SSO account from lock-out.
   Back-off after repeated failures (1x,2x,4x ... capped 30 min), reset on success.
3. Heartbeat carries epoch seconds `updated_ts`; online/stale decisions use it (DST-safe; tolerate 2 min clock skew between PCs).
4. collector.py --service: supervisor that restarts the runner if it dies (same pattern as run.py --service). README: the collector PC must stay logged in (locked screen is fine).
5. Daily housekeeping (outside business hours): prune history 60 d, events 30 d, reports 60 d / 30 copies, debug dumps 7 d (ops/debug/sla, ops/debug/itsm), job logs, stale run requests.
6. GET /healthz (no sign-in): {ok, time, collectors:{job:online}} only.
7. Performance (60 concurrent users): cache the overlaid + team-scoped e-mail answer per (scope, snapshot version, overrides version) serialized + gzipped; per-snapshot indexes for /my;
   `?since=` answered from the cached envelope in < 5 ms; page polling with random start offset, back-off on errors, no polling while the tab is hidden;
   tools/loadtest_hub.py simulating 60 users (50 employees on /my every 30 s, 10 admins on hub/email/sla/itsm every 15 s) over demo data
   (500 e-mail rows, 300 SLA requests, 142k calls) with an extra 5 ms delay on each file operation; targets: unchanged < 5 ms, full e-mail answer p95 < 150 ms, /my p95 < 100 ms,
   /shared < 20 ms. Put the measured numbers in README.
