# Agent brief: multi-team users + employee team view (STATUS: partly done, agent was cut off by the usage limit)

Already edited (uncommitted work was committed as WIP): calllog/auth.py, calllog/api.py, calllog/ops/api.py, calllog/ops/overlay.py, calllog/ops/exports.py,
calllog/templates/_topbar.html, tests/test_ops_api.py, tests/test_ops_my.py. NOT done: tests/test_ops_teams.py, the Employees-tab checkbox UI in calllog/static/admin.js,
the docs update. Review the diff (git log -p -3 -- calllog/auth.py calllog/ops/overlay.py) and finish.

USER REQUIREMENTS
1. "Unclassified e-mails must NOT appear to the normal employee. The employee sees only the e-mails of HIS OWN team."
2. "Can an employee belong to more than one team?" -> YES.

BUILD
A. Multi-team profile: users (roles user and manager) get `teams` (list of settings email.teams names; managers/admin may hold "__all__"). Keep reading the old single `team`
   string (if `teams` missing and `team` set -> [team]); keep exposing `team` in public_user for compatibility plus the new `teams`. Create/update/import APIs accept `teams`
   (list or "A;B") and the old `team`. Import column `teams`. Employees tab: checkbox list of settings teams (+ "All teams" for managers); table shows them comma-separated.
B. Scope helpers multi-team: auth.team_scope() -> None (all) or a frozenset of teams; may_act_on_team = any match; overlay.scope_rows / view_email_data / events / exports /
   classification-rule checks / quality / assign / no-action / team change all use the set. A multi-team mail (row.all_teams) is visible when ANY of the user's teams matches.
C. Employees (role user) get from GET /api/ops/my[?as=uid] and /api/ops/shared:
   - my.email.mine (assigned to their e-mail or replied by it) - stays visible even if the row is Unclassified (explicitly handed over);
   - NEW my.email.team_emails: rows (overlaid) whose team or any all_teams is one of the user's teams, status Pending Reply or Replied, NEVER Unclassified, pending-first
     oldest-first, max 300, ONLY these fields: row_id, message_id, subject, customer_name, team, status, received_time, current_waiting_time, sla_status, escalated,
     is_carryover, first_reply_by, first_reply_time, assigned_to_name, detected_systems (no body previews, no customer addresses, no quality notes, nothing of other teams);
   - NEW my.email.team_summary {teams, pending, replied, no_action, sla_breached, escalated, reply_rate} from exactly those rows;
   - no teams -> empty lists + team_emails_hint "no_team";
   - the e-mail block of /shared and my.shared is scoped to the viewer (employee: own teams, Unclassified excluded, scope "teams"; admin: all rows, scope "all"; manager per teams);
     calls / security / ITSM blocks stay sector-wide counts;
   - every other place an employee could learn about an Unclassified mail (events, attention, overview, health, exports, options) stays admin/manager only.
D. Tests tests/test_ops_teams.py: union of two teams, never Unclassified; two employees isolated; no team; multi-team mail visible to a member of either team; team renamed/removed
   in settings does not break an account; manager with two teams acts on both, 403 on a third; classification rules limited to their teams; import with `teams`;
   a sweep of EVERY GET endpoint as an employee asserting no Unclassified marker subject appears anywhere; shared strip equal between /shared and my.shared; 2000 rows < ~30 ms.
E. Update docs/HUB-DESIGN.md sections 3, 3.1, 3.2 and 7 to the final shapes.
