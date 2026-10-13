"use strict";
/* Home (/hub) - the Admin dashboard: everything that is going on, right now, on one page.

   Reads GET /api/ops/overview (headline numbers of the four modules, the "needs attention" list, collector health) and
   GET /api/ops/events (today's timeline) every 15 s / 30 s through OPS.loader (random start offset, back-off, paused while the
   tab is hidden). The first answers are already inside the page (data-seed, written by views.home_first_paint), so the dashboard
   is drawn at once and the first request is a poll. The "Team status" strip on top is OPS.sharedStrip - the same component the employee page shows - fed with the
   `shared` block of the overview, so strip and cards can never disagree.
   Everything from the server goes in as text (CL.h); Arabic text carries dir="auto". */
(function () {
  const { h, $, toast, bidi, fmtNum } = CL;
  const page = $("#hub-page");
  const ROLE = page.dataset.role;
  const IS_ADMIN = ROLE === "admin";
  const JOBS = Object.entries(OPS.JOB_LABELS);
  const MODULE_NAMES = { email: "E-mail", sla: "Security requests", itsm: "ITSM", calls: "Calls" };
  const LEVEL_NAMES = { danger: "Critical", warn: "Warning", info: "For information", ok: "Good news" };
  const state = { day: page.dataset.day || OPS.localNowIso().slice(0, 10), seen: {}, overview: null };     // the SERVER's day: this PC's clock may differ
  const strip = OPS.sharedStrip($("#hubStrip"), { poll: false, links: IS_ADMIN ? true : { calls: false, system: false } });

  // ------------------------------------------------------------------------------------------------ helpers
  /** Re-draw a section only when its data changed, so hover tooltips and scroll positions survive the 15 s refresh. */
  function paint(name, data, draw) {
    const key = JSON.stringify(data);
    if (state.seen[name] === key) return;
    state.seen[name] = key;
    draw(data);
  }
  function setLive(ok, text) {
    $("#live").className = "live " + (ok ? "ok" : "bad");
    $("#liveText").textContent = text;
  }
  const plural = (n, word) => `${fmtNum(n)} ${word}${n === 1 ? "" : "s"}`;
  const link = (href, ...kids) => (href ? h("a", { class: "link", href }, ...kids) : h("span", {}, ...kids));
  /** Home links that point to the System page only work for administrators. */
  const systemHref = (hash) => (IS_ADMIN ? `/system${hash}` : null);

  /** Small chip in a module card: when the data is from, or why it cannot be trusted. Short on purpose (the cards are narrow). */
  function freshness(moduleKey, overview) {
    const info = (((overview.shared || {}).collector || {}).modules || {})[moduleKey];
    const make = (text, tone, iconName, title) => { const c = OPS.chip(text, tone, iconName); if (title) c.title = title; return c; };
    if (!info) return make("No data", "na", "dash");
    if (!info.online) return make("Collector offline", "danger", "alert", info.updated_at ? `Last data from ${OPS.fmtWhen(info.updated_at, overview.server_time)}` : "");
    if (info.stale) return make("Data is old", "warn", "clock", info.updated_at ? `Last data from ${OPS.fmtWhen(info.updated_at, overview.server_time)}` : "");
    return info.updated_at ? make(OPS.fmtClock(info.updated_at), "ok", "check", `Data updated at ${OPS.fmtWhen(info.updated_at, overview.server_time)}`) : make("Waiting", "info", "clock");
  }

  // -------------------------------------------------------------------------------------- needs attention
  function renderAttention(items) {
    const counts = { danger: 0, warn: 0, info: 0 };
    items.forEach((i) => { counts[i.level] = (counts[i.level] || 0) + 1; });
    $("#attnHint").textContent = items.length
      ? [counts.danger && `${counts.danger} critical`, counts.warn && `${counts.warn} warning${counts.warn === 1 ? "" : "s"}`, counts.info && `${counts.info} to know`].filter(Boolean).join(" · ")
      : "";
    if (!items.length) {
      $("#attnBody").replaceChildren(OPS.emptyState({ title: "Nothing needs attention", icon: "check",
        text: "The collectors are running and no request is past its SLA." }));
      return;
    }
    $("#attnBody").replaceChildren(h("ul", { class: "op-attn" }, items.map((item) => {
      const href = item.link === "/system" ? systemHref("#collectors") : item.link;
      return h("li", { class: `op-attn-item op-t-${item.level}` },
        OPS.icon(OPS.LEVEL_ICON[item.level] || "info"),
        h("span", { class: "op-attn-text" }, h("span", { class: "sr-only" }, `${LEVEL_NAMES[item.level] || ""}: `), bidi(item.text)),
        h("span", { class: "op-attn-module" }, MODULE_NAMES[item.module] || item.module),
        href ? h("a", { class: "link op-attn-open", href }, "Open", OPS.icon("arrow")) : h("span"));
    })));
  }

  // ------------------------------------------------------------------------------------------- collectors
  function renderCollectors(d) {
    const now = d.server_time;
    $("#colBody").replaceChildren(h("ul", { class: "op-jobs" }, JOBS.map(([key, label]) => {
      const job = (d.collectors || {})[key] || { present: false };
      const when = (stamp) => (stamp ? OPS.fmtWhen(stamp, now) : "—");
      return h("li", { class: "op-job" },
        h("div", { class: "op-job-main" }, h("strong", {}, label), OPS.jobChip(job)),
        job.present
          ? h("div", { class: "op-job-sub" }, `last OK ${when(job.last_ok_at)}`, job.next_run_at ? ` · next ${when(job.next_run_at)}` : "", job.host ? ` · ${job.host}` : "")
          : h("div", { class: "op-job-sub" }, "The collector has not reported yet."),
        job.last_status === "error" && job.last_error ? h("div", { class: "op-job-err", title: job.last_error }, bidi(job.last_error)) : null);
    })));
    $("#colLink").hidden = !IS_ADMIN;
  }

  // ----------------------------------------------------------------------------------------- module cards
  function moduleCard({ key, icon, title, href, linkText, chip, body }) {
    return h("article", { class: `card op-module op-module-${key}` },
      h("header", { class: "op-module-head" }, h("h2", {}, OPS.icon(icon), title), chip),
      body,
      href ? h("a", { class: "op-module-link", href }, linkText, OPS.icon("arrow")) : null);
  }
  const fact = (label, value, tone) => h("div", { class: "op-fact" + (tone ? ` op-t-${tone}` : "") }, h("dt", {}, label), h("dd", {}, value));
  const big = (value, label) => h("div", { class: "op-big-line" }, h("strong", { class: "op-big" }, value), h("span", { class: "muted" }, label));

  function callsCard(d) {
    const today = d.calls.today, yesterday = d.calls.yesterday;
    const hours = (d.calls.per_hour_today || []).filter((b) => b.total > 0);
    const lo = hours.length ? Math.max(0, hours[0].key - 1) : 8, hi = hours.length ? hours[hours.length - 1].key : 9;
    const series = (d.calls.per_hour_today || []).filter((b) => b.key >= lo && b.key <= hi).map((b) => b.total);
    const dot = (s, label) => h("span", { class: "status-chip" }, h("i", { class: `dot s-${s}` }), label);
    return moduleCard({
      key: "calls", icon: "phone", title: "Calls today", href: IS_ADMIN ? "/calls" : null, linkText: "Open the Calls dashboard",
      chip: OPS.chip(`${today.active_employees} of ${today.employees} active`, today.active_employees ? "ok" : "na", "user"),
      body: [big(fmtNum(today.total), "calls so far"),
        series.length > 1 ? h("div", { class: "op-viz" }, OPS.sparkline(series, { width: 220, height: 40, label: "Calls per hour today" }),
          h("span", { class: "hint" }, `per hour, ${String(lo).padStart(2, "0")}:00 - now`)) : null,
        h("dl", { class: "op-facts" }, fact(dot("resolved", "Resolved"), fmtNum(today.resolved)), fact(dot("transferred", "Transferred"), fmtNum(today.transferred)),
          fact(dot("ticket_inquiry", "Ticket inquiry"), fmtNum(today.ticket_inquiry)), fact("Yesterday, whole day", fmtNum(yesterday.total)))],
    });
  }

  function emailCard(d) {
    const e = d.email;
    const chip = freshness("email", d);
    if (!e.available) {
      return moduleCard({ key: "email", icon: "mail", title: "E-mail", href: "/email", linkText: "Open E-mail", chip,
        body: OPS.emptyState({ title: "No e-mail data yet", text: "The collector has not written an e-mail scan.", icon: "mail" }) });
    }
    const donut = OPS.donut([{ label: "Replied", value: e.replied, cls: "op-t-ok" }, { label: "Pending", value: e.pending, cls: "op-t-warn" },
      { label: "No action needed", value: e.no_action_needed, cls: "op-t-na" }], { size: 112, thickness: 14, centerValue: OPS.fmtPct(e.reply_rate).replace(/\.\d+/, ""), centerLabel: "replied" });
    return moduleCard({
      key: "email", icon: "mail", title: "E-mail", href: "/email", linkText: "Open E-mail", chip,
      body: [big(fmtNum(e.business_emails), e.scope ? `e-mails for ${e.scope}` : "business e-mails today"),
        h("div", { class: "op-viz op-viz-donut" }, donut, h("div", { class: "op-legend op-legend-col" },
          h("span", { class: "op-legend-item" }, h("i", { class: "op-swatch op-t-ok" }), fmtNum(e.replied), h("span", { class: "muted" }, " replied")),
          h("span", { class: "op-legend-item" }, h("i", { class: "op-swatch op-t-warn" }), fmtNum(e.pending), h("span", { class: "muted" }, " pending")),
          h("span", { class: "op-legend-item" }, h("i", { class: "op-swatch op-t-na" }), fmtNum(e.no_action_needed), h("span", { class: "muted" }, " no action")))),
        e.carryover_count ? h("p", { class: "hint" }, `The split above also counts ${fmtNum(e.carryover_count)} still-open e-mail${e.carryover_count === 1 ? "" : "s"} from earlier days.`) : null,
        h("dl", { class: "op-facts" },
          fact("Past the SLA", fmtNum(e.sla_breached), e.sla_breached ? "danger" : null), fact("Escalated (no reply)", fmtNum(e.escalated_count), e.escalated_count ? "danger" : null),
          fact("Carried over", e.carryover_count ? `${fmtNum(e.carryover_count)} (oldest ${e.carryover_oldest_days} d)` : "0", e.carryover_count ? "warn" : null),
          fact("Average first reply", OPS.fmtDuration(e.average_response)))],
    });
  }

  function slaCard(d) {
    const s = d.sla;
    const chip = freshness("sla", d);
    if (!s.available) {
      return moduleCard({ key: "sla", icon: "shield", title: "Security requests", href: "/sla", linkText: "Open SLA", chip,
        body: OPS.emptyState({ title: "No SLA data yet", text: "The collector has not read the portal yet.", icon: "shield" }) });
    }
    const limits = s.thresholds || {};
    const top = (s.top_users || []).filter((u) => u.breached || u.reminder).slice(0, 3);
    return moduleCard({
      key: "sla", icon: "shield", title: "Security requests", href: "/sla", linkText: "Open SLA", chip,
      body: [big(fmtNum(s.open), "open requests"),
        OPS.stackbar([{ label: "within", value: s.within, cls: "op-t-ok" }, { label: "reminder", value: s.reminder, cls: "op-t-warn" },
          { label: "breached", value: s.breached, cls: "op-t-danger" }, { label: "no date", value: s.no_date, cls: "op-t-na" }]),
        h("dl", { class: "op-facts" }, fact("Oldest request", OPS.fmtDuration(s.oldest_minutes), s.breached ? "danger" : null),
          fact("Reminder / breach after", `${OPS.fmtDuration(limits.warning_minutes)} / ${OPS.fmtDuration(limits.breach_minutes)}`)),
        top.length ? h("div", { class: "op-mini" }, h("span", { class: "hint" }, "Most overdue"), h("ul", {}, top.map((u) =>
          h("li", {}, link(`/sla?q=${encodeURIComponent(u.user)}`, bidi(u.user)), h("span", { class: "hint" }, ` ${u.breached} breached, ${u.reminder} reminder`))))) : null],
    });
  }

  function itsmCard(d) {
    const t = d.itsm;
    const chip = freshness("itsm", d);
    if (!t.available) {
      return moduleCard({ key: "itsm", icon: "server", title: "ITSM", href: "/itsm", linkText: "Open ITSM", chip,
        body: OPS.emptyState({ title: "No ITSM data yet", text: "The collector has not read the scoreboard yet.", icon: "server" }) });
    }
    const cats = t.categories.length ? t.categories : Object.keys(t.totals);
    const total = cats.reduce((a, c) => a + (t.totals[c] || 0), 0);
    const errors = t.role_errors || [];
    return moduleCard({
      key: "itsm", icon: "server", title: "ITSM", href: "/itsm", linkText: "Open ITSM", chip,
      body: [big(fmtNum(total), "open requests"),
        OPS.hbars(cats.map((c, i) => ({ label: c, value: t.totals[c] || 0, cls: /rollback/i.test(c) ? "op-t-warn" : "op-s0" })), { max: Math.max(1, total) }),
        h("dl", { class: "op-facts" }, fact("New since last scan", fmtNum(t.changes.new)), fact("Closed since last scan", fmtNum(t.changes.closed))),
        errors.length ? h("div", { class: "op-note op-t-danger" }, OPS.icon("alert"),
          bidi(`${plural(errors.length, "role")} could not be read: ${errors.map((e) => e.role).join(", ")}`)) : null],
    });
  }

  function renderCards(d) {
    $("#hubCards").replaceChildren(callsCard(d), emailCard(d), slaCard(d), itsmCard(d));
  }

  // ----------------------------------------------------------------------------------------------- timeline
  const EVENT_LINK = {
    sla: (i) => (i.user ? { href: `/sla?q=${encodeURIComponent(i.user)}`, text: i.user } : null),
    email: (i) => (i.subject ? { href: `/email?q=${encodeURIComponent(i.subject)}${i.team ? "&team=" + encodeURIComponent(i.team) : ""}`, text: i.subject, hint: i.team } : null),
    itsm: (i) => (i.number ? { href: `/itsm?q=${encodeURIComponent(i.number)}`, text: `#${i.number} ${i.summary || ""}`.trim(), hint: i.role } : null),
  };
  function eventItems(ev) {
    const make = EVENT_LINK[ev.module];
    const links = (ev.items || []).map((i) => (make && typeof i === "object" ? make(i) : null)).filter(Boolean);
    if (!links.length) return null;
    const more = (ev.count || 0) - links.length;
    return h("ul", { class: "op-tl-items" }, links.map((l) => h("li", {}, h("a", { class: "link", href: l.href, dir: "auto" }, l.text), l.hint ? h("span", { class: "hint" }, ` ${l.hint}`) : null)),
      more > 0 ? h("li", { class: "hint" }, `and ${more} more`) : null);
  }
  function renderTimeline(day, events) {
    const body = $("#tlBody");
    if (!events.length) {
      body.replaceChildren(OPS.emptyState({ title: "Quiet so far", text: "Nothing has changed today yet. New breaches, escalations and closed requests appear here.", icon: "activity" }));
      return;
    }
    body.replaceChildren(h("ol", { class: "op-tl" }, events.slice(0, 40).map((ev) => h("li", { class: `op-tl-item op-t-${ev.level}` },
      h("span", { class: "op-tl-time" }, OPS.fmtClock(ev.ts)),
      h("span", { class: "op-tl-icon" }, OPS.icon(OPS.LEVEL_ICON[ev.level] || "info")),
      h("div", { class: "op-tl-main" },
        h("div", { class: "op-tl-text" }, h("span", { class: "sr-only" }, `${LEVEL_NAMES[ev.level] || ""}: `), bidi(ev.text), h("span", { class: "op-tl-module" }, MODULE_NAMES[ev.module] || ev.module)),
        eventItems(ev))))));
  }

  // ----------------------------------------------------------------------------------------- e-mail by team
  let teamTable = null;
  function renderTeams(d) {
    const e = d.email, body = $("#teamBody");
    teamTable = null;
    if (!e.available || !e.teams.length) {
      body.replaceChildren(OPS.emptyState({ title: "No e-mail data yet", text: "Teams appear when the first e-mail scan has been read.", icon: "mail" }));
      return;
    }
    teamTable = OPS.table({
      search: false, copy: false, pageSize: 10, pageSizes: [10], caption: "E-mail by team", sort: { key: "pending", desc: true },
      columns: [
        { key: "team", label: "Team", sort: true, render: (r) => h("a", { class: "link", href: `/email?team=${encodeURIComponent(r.team)}`, dir: "auto" }, r.team) },
        { key: "business_emails", label: "E-mails", num: true, sort: true },
        { key: "replied", label: "Replied", num: true, sort: true },
        { key: "pending", label: "Pending", num: true, sort: true, render: (r) => h("strong", {}, fmtNum(r.pending)) },
        { key: "escalated_count", label: "Escalated", num: true, sort: true,
          render: (r) => (r.escalated_count ? h("span", { class: "op-note op-t-danger" }, OPS.icon("alert"), fmtNum(r.escalated_count)) : "0") },
        { key: "carryover_count", label: "Carry-over", num: true, sort: true },
      ],
      rows: e.teams,
    });
    body.replaceChildren(teamTable.el, e.unclassified
      ? h("p", { class: "op-note op-t-info op-block" }, OPS.icon("info"), `${plural(e.unclassified, "e-mail")} not classified to a team. `,
        h("a", { class: "link", href: "/email#unclassified" }, "Assign a team or add a rule"))
      : null);
  }

  // ------------------------------------------------------------------------------------------------ loading
  function showOverview(d) {
    state.overview = d;
    state.day = d.date || state.day;
    strip.update(d.shared);
    $("#hubSub").textContent = `${new Date(`${d.date}T12:00:00`).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" })} · server time ${OPS.fmtClock(d.server_time)}`
      + (d.scope ? ` · team ${d.scope}` : "");
    paint("attention", d.attention, renderAttention);
    paint("collectors", [d.collectors, d.server_time.slice(0, 16)], () => renderCollectors(d));
    paint("cards", [d.calls, d.email, d.sla, d.itsm, d.shared.collector], () => renderCards(d));
    paint("teams", d.email, () => renderTeams(d));
    setLive(true, `Live · updated ${OPS.fmtClock(d.server_time, true)}`);
  }
  function showError(e) {
    setLive(false, e.network ? "Offline · retrying…" : e.message);
    if (!state.overview && !e.network) toast(e.message, "error");
  }

  const overview = OPS.loader({ url: "/api/ops/overview", interval: 15000, since: false, onData: showOverview, onError: showError });
  const timeline = OPS.loader({
    url: () => `/api/ops/events?day=${state.day}&limit=60`, interval: 30000, since: false,
    onData: (d) => paint("timeline", d.events, (events) => renderTimeline(d.day, events)),
    onError: () => {},                                   // the overview already shows the connection state
  });

  // "View as employee": choosing a person opens their page at once
  const picker = $("#viewAs");
  if (picker) picker.addEventListener("change", () => { if (picker.value) $("#viewAsForm").submit(); });

  // The page carries its first paint (views.home_first_paint); polling starts after a random pause. Without it, ask at once.
  let seed = null;
  try { seed = JSON.parse(page.dataset.seed || "null"); } catch (e) { seed = null; }
  if (seed && seed.overview && seed.overview.server_time) {
    showOverview(seed.overview);
    paint("timeline", seed.events || [], (events) => renderTimeline(seed.overview.date, events));
    overview.start({ wait: true });
    timeline.start({ wait: true });
  } else {
    overview.start();
    timeline.start();
  }
})();
