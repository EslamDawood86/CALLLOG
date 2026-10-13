"use strict";
/* Employee dashboard: log a call (IP phone + reason), see today's numbers, edit/delete own calls. */
(function () {
  const { h, $, api, toast, icon, editRecord, deleteRecord, rowActions, statusChip, reasonName, trimHours, drawChart,
    legend, chartWithToggle, bidi, reasonBars, branchPicker, ipLookup, normalizeIp, normAr, fmtNum, hhmm, fmtDay, addDays, pct, STATUS_NAMES } = CL;

  const state = { me: null, reasons: [], branches: [], picker: null, lookup: null, today: "", day: "", data: null, saving: false, dialogOpen: false };

  const el = {
    ip: $("#ipInput"), ipHelp: $("#ipHelp"), ipUnit: $("#ipUnit"), note: $("#noteInput"), reasons: $("#reasons"), entry: $("#entryCard"),
    dayInput: $("#dayInput"), search: $("#searchInput"), body: $("#recordsBody"), empty: $("#emptyMsg"),
  };

  // ------------------------------------------------------------ rendering
  function renderProfile() {
    const u = state.me.user;
    $("#pName").textContent = u.name;
    $("#pId").textContent = `ID ${u.user_id}`;
    $("#pDept").textContent = u.department || "No department";
    $("#avatar").textContent = (u.name.trim()[0] || "?").toUpperCase();
  }

  function renderReasons() {
    el.reasons.replaceChildren(...state.reasons.map((r) => h("button", {
      type: "button", class: "reason-btn", dataset: { key: r.key }, onclick: () => saveCall(r.key),
    }, h("i", { class: `dot s-${r.status}` }), h("span", { class: "ar", dir: "auto" }, reasonName(r)), h("span", { class: "en" }, r.label_en))));
  }

  function renderStats() {
    const { stats, date } = state.data;
    const isToday = date === state.today;
    $("#totalLabel").textContent = isToday ? "Calls today" : `Calls · ${fmtDay(date)}`;
    $("#kTotal").textContent = fmtNum(stats.total);
    $("#kResolved").textContent = fmtNum(stats.resolved);
    $("#kTransferred").textContent = fmtNum(stats.transferred);
    $("#kTicket").textContent = fmtNum(stats.ticket_inquiry);
    $("#sResolved").textContent = pct(stats.resolved, stats.total);
    $("#sTransferred").textContent = pct(stats.transferred, stats.total);
    $("#sTicket").textContent = pct(stats.ticket_inquiry, stats.total);
    $("#kRange").textContent = stats.first_call
      ? `First ${hhmm(stats.first_call.slice(11))} · Last ${hhmm(stats.last_call.slice(11))}` : "No calls logged yet";
    renderChart();
    $("#reasonBars").replaceChildren(reasonBars(state.reasons, stats.by_reason));
  }

  const hourBuckets = () => trimHours(state.data.stats.hourly).map((b) => ({
    ...b, label: String(b.key).padStart(2, "0"),
    title: `${String(b.key).padStart(2, "0")}:00 – ${String(b.key).padStart(2, "0")}:59`,
  }));
  const renderChart = chartWithToggle($("#chart"), $("#viewToggle"), () => ({ buckets: hourBuckets(), firstColumn: "Hour" }));

  function renderRecords(flashId) {
    const { records, date } = state.data;
    const q = el.search.value.trim().toLowerCase();
    const qn = normAr(q);
    const rows = records.filter((r) => !q || r.ip_phone.includes(q) || (r.note || "").toLowerCase().includes(q)
      || r.branch.includes(q) || r.branch_name.includes(q) || (r.ip_unit && normAr(r.ip_unit).includes(qn)));
    $("#recordsTitle").textContent = date === state.today ? "Today's calls" : `Calls on ${fmtDay(date)}`;
    el.body.replaceChildren(...rows.map((r) => {
      const tr = h("tr", { class: r.id === flashId ? "flash" : "" },
        h("td", { class: "nowrap" }, hhmm(r.time), h("span", { class: "hint" }, r.time.slice(5))),
        h("td", { class: "ip" }, r.ip_phone, r.ip_unit ? h("div", { class: "ip-where", dir: "auto", title: r.ip_unit }, r.ip_unit) : null),
        h("td", { class: "branch-cell" }, r.branch
          ? [h("strong", {}, r.branch), bidi(r.branch_name), h("div", { class: "hint" }, r.region)]
          : h("span", { class: "branch-none" }, "—")),
        h("td", {}, bidi(r.reason_ar), h("div", { class: "hint" }, r.reason_en)),
        h("td", {}, statusChip(r.status, r.status_label)),
        h("td", { class: "note" }, bidi(r.note || ""), r.edited ? h("span", { class: "edited-mark", title: `Edited ${r.updated_at}` }, "edited") : null),
        h("td", { class: "actions-col" }, r.editable ? rowActions(r, () => edit(r), () => remove(r)) : h("span", { class: "hint" }, "locked")));
      return tr;
    }));
    el.empty.hidden = rows.length > 0;
    el.empty.textContent = records.length === 0
      ? (date === state.today ? "No calls logged yet today. Enter the IP phone above, then click the reason." : "No calls were logged on this day.")
      : "No calls match your search.";
    $("#recordsTable").hidden = rows.length === 0;
    updateDownloadLinks();
  }

  function renderDateNav() {
    const isToday = state.day === state.today;
    el.dayInput.value = state.day;
    el.dayInput.max = state.today;
    $("#nextDay").disabled = isToday;
    $("#todayBtn").disabled = isToday;
    el.entry.hidden = !isToday;      // calls are logged live, so only "today" accepts new entries
    $(".grid-main").classList.toggle("no-entry", !isToday);   // without the entry card the numbers use the full width
  }

  function updateDownloadLinks() {
    const month = $("#dlScope").value === "month";
    const from = month ? state.day.slice(0, 8) + "01" : state.day;
    const to = month ? lastOfMonth(state.day) : state.day;
    $("#dlXlsx").href = `/export/my.xlsx?from=${from}&to=${to}`;
    $("#dlCsv").href = `/export/my.csv?from=${from}&to=${to}`;
  }
  function lastOfMonth(iso) {
    const [y, m] = iso.split("-").map(Number);
    return `${y}-${String(m).padStart(2, "0")}-${String(new Date(y, m, 0).getDate()).padStart(2, "0")}`;
  }

  // ----------------------------------------------------------- data flow
  async function loadDay(flashId, quiet) {
    try {
      const data = await api("GET", `/api/records?date=${state.day}`);
      if (data.today !== state.today) {          // midnight passed while the page stayed open
        const followToday = state.day === state.today;
        state.today = data.today;
        if (followToday) { state.day = data.today; renderDateNav(); loadDay(); return; }
        renderDateNav();
      }
      const changed = JSON.stringify(data.records) !== JSON.stringify(state.data && state.data.records);
      state.data = data;
      if (changed || !quiet) { renderStats(); renderRecords(flashId); }
    } catch (e) {
      if (!quiet) toast(e.message, "error");
    }
  }

  function goDay(iso) {
    if (iso > state.today) iso = state.today;
    state.day = iso;
    renderDateNav();
    loadDay();
  }

  // -------------------------------------------------------------- actions
  function showIpError(msg) {
    el.ipHelp.textContent = msg || "";
    el.ipHelp.hidden = !msg;
    el.ip.setAttribute("aria-invalid", msg ? "true" : "false");
  }

  async function saveCall(reasonKey) {
    if (state.saving) return;
    const ip = normalizeIp(el.ip.value);
    if (!/^\d{3,10}$/.test(ip)) {
      showIpError(ip ? "IP phone must be digits only, for example 75942." : "Enter the IP phone number first.");
      el.ip.focus();
      return;
    }
    showIpError("");
    state.saving = true;
    el.reasons.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      await state.lookup.ready();                  // an answer still on its way may fill in the branch: wait for it (1.5 s at most)
      const branchProblem = state.picker.problem();
      $("#branchHelp").textContent = branchProblem;
      $("#branchHelp").hidden = !branchProblem;
      if (branchProblem) { state.picker.markInvalid(); state.picker.focus(); return; }
      const { record } = await api("POST", "/api/records", { ip_phone: ip, reason: reasonKey, note: el.note.value,
        branch: state.picker.value(), ip_unit_auto: state.picker.isAuto() });
      el.ip.value = "";
      el.note.value = "";
      state.picker.clear();
      state.lookup.reset();
      toast(`Saved · ${record.ip_phone} · ${record.reason_en}`);
      await loadDay(record.id);
      el.ip.focus();
    } catch (e) {
      toast(e.message, "error");
      el.ip.focus();
    } finally {
      state.saving = false;
      el.reasons.querySelectorAll("button").forEach((b) => (b.disabled = false));
    }
  }

  async function edit(rec) {
    state.dialogOpen = true;
    const changed = await editRecord(rec, state.reasons, state.branches);
    state.dialogOpen = false;
    if (changed) loadDay(rec.id);
  }

  async function remove(rec) {
    state.dialogOpen = true;
    const done = await deleteRecord(rec);
    state.dialogOpen = false;
    if (done) loadDay();
  }

  // ------------------------------------------------------------------ init
  async function init() {
    legend($("#legend"));
    $("#prevDay").append(icon("left"));
    $("#nextDay").append(icon("right"));
    try {
      state.me = await api("GET", "/api/me");
    } catch (e) {
      toast(e.message, "error");
      return;
    }
    state.reasons = state.me.reasons;
    try {
      state.branches = (await api("GET", "/api/branches")).branches;
    } catch (e) {
      toast("The branch list could not be loaded: " + e.message, "error");
    }
    state.picker = branchPicker(state.branches);
    $("#branchHost").append(state.picker.el);
    state.lookup = ipLookup({ input: el.ip, host: el.ipUnit, picker: state.picker, branches: state.branches });
    state.today = state.day = state.me.today;
    renderProfile();
    renderReasons();
    renderDateNav();

    el.ip.addEventListener("input", () => {
      const v = normalizeIp(el.ip.value);
      if (v !== el.ip.value) el.ip.value = v;
      if (el.ip.value) showIpError("");
    });
    el.ip.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {          // Enter moves on to the branch box, so the whole flow works from the keyboard
        ev.preventDefault();
        state.lookup.flush();            // ask for the unit now instead of waiting for the pause
        state.picker.focus();
      }
    });
    $("#prevDay").addEventListener("click", () => goDay(addDays(state.day, -1)));
    $("#nextDay").addEventListener("click", () => goDay(addDays(state.day, 1)));
    $("#todayBtn").addEventListener("click", () => goDay(state.today));
    el.dayInput.addEventListener("change", () => el.dayInput.value && goDay(el.dayInput.value));
    el.search.addEventListener("input", () => renderRecords());
    $("#dlScope").addEventListener("change", updateDownloadLinks);

    await loadDay();
    // Pick up changes made elsewhere (e.g. an administrator corrected a record) and roll over at midnight.
    setInterval(() => {
      if (document.hidden || state.dialogOpen || state.saving) return;
      loadDay(undefined, true);
    }, 20000);
  }
  init();
})();
