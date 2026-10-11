"use strict";
/* Shared helpers: DOM builder, API calls, dialogs, record editor, stacked-column chart.
   Everything user-supplied is inserted with textContent - never innerHTML. */
(function () {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const STATUSES = ["resolved", "transferred", "ticket_inquiry"];
  const STATUS_NAMES = { resolved: "Resolved", transferred: "Transferred", ticket_inquiry: "Ticket Inquiry" };
  const ICONS = {
    pencil: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
    trash: "M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6",
    download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
    upload: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12",
    left: "M15 18l-6-6 6-6",
    right: "M9 18l6-6-6-6",
    plus: "M12 5v14M5 12h14",
    key: "M21 2l-9.6 9.6M15.5 7.5l3 3L22 7l-3-3M7.5 11.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11z",
    check: "M20 6L9 17l-5-5",
  };

  // ------------------------------------------------------------------ DOM
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (["value", "checked", "disabled", "selected", "hidden"].includes(k)) el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    append(el, kids);
    return el;
  }
  function append(el, kids) {
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", ICONS[name]);
    svg.append(p);
    return svg;
  }
  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
  }
  const $ = (sel, root = document) => root.querySelector(sel);

  // ------------------------------------------------------------------ API
  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;

  async function api(method, url, body) {
    const opts = { method, headers: { "X-CSRF-Token": csrf() }, credentials: "same-origin" };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (e) {
      const err = new Error("Cannot reach the server. Check your network connection.");
      err.network = true;
      throw err;
    }
    if (resp.status === 401) {
      location.href = "/login";
      throw new Error("Please sign in.");
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const err = new Error(data.error || `Request failed (${resp.status}).`);
      err.status = resp.status;
      throw err;
    }
    return data;
  }

  // -------------------------------------------------------------- formatting
  const fmtNum = (n) => Number(n || 0).toLocaleString("en-US");
  const hhmm = (t) => (t || "").slice(0, 5);
  function parseISODate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d, 12);
  }
  function fmtDay(iso) {
    return parseISODate(iso).toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
  }
  function isoDate(d) {
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }
  function addDays(iso, n) {
    const d = parseISODate(iso);
    d.setDate(d.getDate() + n);
    return isoDate(d);
  }
  function pct(part, total) {
    return total ? `${Math.round((part / total) * 100)}%` : "";
  }
  function minutesBetween(a, b) {
    const t = (s) => new Date(s.replace(" ", "T")).getTime();
    return Math.round((t(b) - t(a)) / 60000);
  }
  function agoText(serverTime, stamp) {
    if (!stamp) return "";
    const mins = minutesBetween(stamp, serverTime);
    if (mins < 0 || mins > 180 || stamp.slice(0, 10) !== serverTime.slice(0, 10)) return "";
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    return `${Math.floor(mins / 60)} h ${mins % 60} min ago`;
  }

  // ------------------------------------------------------------------ toast
  function toast(message, kind) {
    const host = document.getElementById("toast");
    const el = h("div", { class: "toast-msg" + (kind === "error" ? " error" : "") }, message);
    host.append(el);
    setTimeout(() => el.remove(), kind === "error" ? 6000 : 2600);
  }

  // ----------------------------------------------------------------- dialogs
  /** Modal dialog. onSubmit() may throw Error(message) to keep the dialog open and show it. */
  function modal({ title, body, submitLabel = "Save", cancelLabel = "Cancel", danger = false, onSubmit, wide = false }) {
    return new Promise((resolve) => {
      const err = h("p", { class: "form-error", role: "alert", hidden: true });
      const submit = h("button", { type: "submit", class: "btn " + (danger ? "btn-danger" : "btn-primary") }, submitLabel);
      const cancel = h("button", { type: "button", class: "btn btn-ghost", onclick: () => dlg.close() }, onSubmit ? cancelLabel : "Close");
      const form = h("form", { method: "dialog", class: "dlg-form", novalidate: true },
        h("h2", { class: "dlg-title" }, title), body, err,
        h("div", { class: "dlg-actions" }, cancel, onSubmit ? submit : null));
      const dlg = h("dialog", { class: "dlg" + (wide ? " wide" : "") }, form);
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        if (!onSubmit) return dlg.close();
        submit.disabled = true;
        err.hidden = true;
        try {
          const result = await onSubmit();
          resolve(result === undefined ? true : result);
          dlg.close();
        } catch (e) {
          err.textContent = e.message;
          err.hidden = false;
          submit.disabled = false;
        }
      });
      dlg.addEventListener("close", () => { dlg.remove(); resolve(false); });
      document.body.append(dlg);
      dlg.showModal();
      const first = dlg.querySelector("input:not([type=hidden]), textarea, select");
      (first || (onSubmit ? submit : cancel)).focus();
    });
  }

  function confirmDialog({ title, message, confirmLabel, danger = true }) {
    return modal({ title, body: h("p", { class: "muted", dir: "auto" }, message), submitLabel: confirmLabel, danger, onSubmit: async () => true });
  }

  // ------------------------------------------------------------- bar rows
  /** Horizontal bars. items: {label, hint, value, cls}. Square at the baseline, 4px rounded data end. */
  function barRows(items) {
    const max = Math.max(1, ...items.map((i) => i.value));
    return h("div", { class: "reason-rows" }, items.map((i) => {
      const bar = h("div", { class: `rbar ${i.cls || "neutral"}` });
      bar.style.width = `${(i.value / max) * 100}%`;
      return h("div", { class: "rrow" },
        h("div", { class: "rlabel" }, bidi(i.label), i.hint ? h("span", { class: "hint" }, i.hint) : null),
        h("div", { class: "rtrack" }, bar), h("strong", { class: "rcount" }, fmtNum(i.value)));
    }));
  }

  // --------------------------------------------------------- branch picker
  const DIGITS = "٠١٢٣٤٥٦٧٨٩";
  /** Lower-case, no diacritics/tatweel, one spelling for alef/yaa/taa-marbuta, ASCII digits. */
  function normAr(text) {
    return String(text || "").toLowerCase()
      .replace(/[\u0640\u064B-\u065F\u0670]/g, "")
      .replace(/[أإآٱ]/g, "ا").replace(/ى/g, "ي").replace(/ة/g, "ه")
      .replace(/[٠-٩]/g, (d) => String(DIGITS.indexOf(d))).replace(/[۰-۹]/g, (d) => String("۰۱۲۳۴۵۶۷۸۹".indexOf(d)));
  }
  const branchText = (b) => `${b.code} · ${b.name}`;
  let pickerSeq = 0;

  /** Search-as-you-type branch chooser. Optional: leave it empty for callers that are not branches. */
  function branchPicker(branches) {
    const index = branches.map((b) => ({ b, n: normAr(`${b.code} ${b.name} ${b.region}`), name: normAr(b.name) }));
    const id = `bp${++pickerSeq}`;
    const input = h("input", { type: "text", class: "bp-input", role: "combobox", autocomplete: "off", spellcheck: "false", dir: "ltr",
      "aria-autocomplete": "list", "aria-expanded": "false", "aria-controls": `${id}-list`, "aria-label": "Branch the call came from",
      placeholder: "Type the branch number or name — leave empty if not a branch" });
    const list = h("ul", { class: "bp-list", id: `${id}-list`, role: "listbox", hidden: true });
    const clearBtn = h("button", { type: "button", class: "bp-clear", "aria-label": "Clear branch", hidden: true, tabindex: "-1" }, "×");
    const el = h("div", { class: "bp" }, input, clearBtn, list);
    let selected = null, unchanged = false, matches = [], active = -1;

    function search(query) {
      const q = normAr(query).trim();
      if (!q) return [];
      const words = q.split(/\s+/), found = [];
      for (const e of index) {
        if (!words.every((w) => e.n.includes(w))) continue;
        found.push([e.b.code === q ? 0 : e.b.code.startsWith(q) ? 1 : e.name.startsWith(q) ? 2 : 3, e.b]);
      }
      return found.sort((x, y) => x[0] - y[0]).slice(0, 50).map((x) => x[1]);
    }
    function close() { list.hidden = true; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); active = -1; }
    function render() {
      list.replaceChildren(...matches.map((b, i) => h("li", { id: `${id}-o${i}`, role: "option", "aria-selected": String(i === active), class: i === active ? "active" : "",
        onmousedown: (ev) => { ev.preventDefault(); choose(b); } },
        h("span", { class: "bp-main" }, h("strong", {}, b.code), " ", bidi(b.name)), h("span", { class: "hint" }, [b.region, b.type].filter(Boolean).join(" · ")))));
      if (!matches.length) list.replaceChildren(h("li", { class: "bp-none", role: "presentation" }, "No branch matches. Leave it empty if the caller is not a branch."));
      list.hidden = false; input.setAttribute("aria-expanded", "true");
      if (active >= 0) { input.setAttribute("aria-activedescendant", `${id}-o${active}`); list.children[active].scrollIntoView({ block: "nearest" }); }
    }
    function choose(b) {
      selected = b; unchanged = false; input.value = branchText(b); input.setAttribute("aria-invalid", "false");
      clearBtn.hidden = false; close();
    }
    function clear() { selected = null; unchanged = false; input.value = ""; clearBtn.hidden = true; close(); input.removeAttribute("aria-invalid"); }

    input.addEventListener("input", () => {
      selected = null; unchanged = false; clearBtn.hidden = !input.value; input.removeAttribute("aria-invalid");
      matches = search(input.value); active = matches.length ? 0 : -1;
      input.value ? render() : close();
    });
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        if (!matches.length) return;
        ev.preventDefault();
        active = (active + (ev.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length;
        render();
      } else if (ev.key === "Enter" && !list.hidden && matches.length) {
        ev.preventDefault(); choose(matches[Math.max(active, 0)]);
      } else if (ev.key === "Escape" && !list.hidden) {
        ev.preventDefault(); ev.stopPropagation(); close();
      } else if (ev.key === "Tab" && !selected && input.value && matches.length) {
        choose(matches[Math.max(active, 0)]);          // typed "100" then Tab: take the best match and move on
      }
    });
    input.addEventListener("blur", () => close());
    clearBtn.addEventListener("click", () => { clear(); input.focus(); });

    return {
      el, focus: () => input.focus(), clear,
      /** Branch id to send: "" = none, undefined = leave a stored branch that is no longer in the list unchanged. */
      value: () => (selected ? selected.id : unchanged ? undefined : ""),
      /** Typed something but never picked it from the list. */
      problem: () => (input.value.trim() && !selected && !unchanged
        ? "Choose the branch from the list, or clear the field if the caller is not a branch." : ""),
      markInvalid: () => input.setAttribute("aria-invalid", "true"),
      setFromRecord(rec) {
        if (!rec.branch) return clear();
        const hit = branches.find((b) => b.code === rec.branch && b.name === rec.branch_name && b.region === rec.region);
        if (hit) return choose(hit);
        selected = null; unchanged = true; input.value = `${rec.branch} · ${rec.branch_name}`; clearBtn.hidden = false;   // branch was removed from the list
      },
    };
  }

  // ---------------------------------------------------------- record editor
  const reasonName = (r) => r.label_ar || r.label_en || r.key;
  const bidi = (text) => h("span", { class: "bidi", dir: "auto" }, text);

  /** Horizontal bars: how many calls per reason (bar colour = the reason's status). */
  function reasonBars(reasons, byReason) {
    const counts = reasons.map((r) => byReason[r.key] || 0);
    const max = Math.max(1, ...counts);
    return h("div", { class: "reason-rows" }, reasons.map((r, i) => {
      const bar = h("div", { class: `rbar s-${r.status}` });
      bar.style.width = `${(counts[i] / max) * 100}%`;
      return h("div", { class: "rrow" },
        h("div", { class: "rlabel" }, bidi(reasonName(r)), h("span", { class: "hint" }, r.label_en)),
        h("div", { class: "rtrack" }, bar),
        h("strong", { class: "rcount" }, fmtNum(counts[i])));
    }));
  }

  function statusChip(status, label) {
    return h("span", { class: "status-chip" }, h("i", { class: `dot s-${status}` }), label || STATUS_NAMES[status] || "Other");
  }

  /** Edit one call. ``reasons`` = list from /api/me. Resolves true if the record changed. */
  function editRecord(rec, reasons, branches) {
    const ip = h("input", { value: rec.ip_phone, inputmode: "numeric", maxlength: "10", autocomplete: "off", "aria-label": "IP phone" });
    const note = h("textarea", { rows: "3", maxlength: "300", "aria-label": "Note" });
    note.value = rec.note || "";
    const radios = reasons.map((r) => h("label", {},
      h("input", { type: "radio", name: "reason", value: r.key, checked: r.key === rec.reason }),
      h("i", { class: `dot s-${r.status}` }),
      h("span", { class: "rl-text" }, bidi(reasonName(r)), h("span", { class: "hint" }, r.label_en))));
    const picker = branchPicker(branches || []);
    picker.setFromRecord(rec);
    const body = h("div", { class: "dlg-body" },
      h("label", { class: "field" }, "IP phone", ip),
      h("div", { class: "field" }, h("span", {}, "Branch ", h("span", { class: "hint" }, "(only if the caller is a branch)")), picker.el),
      h("div", { class: "field" }, "Reason for the call", h("div", { class: "radio-list" }, radios)),
      h("label", { class: "field" }, "Note", note),
      h("p", { class: "hint" }, `${fmtDay(rec.date)} · ${hhmm(rec.time)}`, rec.name && rec.user_id ? [" · ", h("bdi", {}, rec.name)] : null));
    return modal({
      title: "Edit call", body, submitLabel: "Save changes",
      onSubmit: async () => {
        const reason = body.querySelector("input[name=reason]:checked");
        const problem = picker.problem();
        if (problem) { picker.markInvalid(); throw new Error(problem); }
        const payload = { ip_phone: ip.value, reason: reason && reason.value, note: note.value };
        if (picker.value() !== undefined) payload.branch = picker.value();
        await api("PUT", `/api/records/${encodeURIComponent(rec.id)}`, payload);
        toast("Changes saved");
        return true;
      },
    });
  }

  async function deleteRecord(rec) {
    const ok = await confirmDialog({
      title: "Delete this call?",
      message: `${hhmm(rec.time)} · IP phone ${rec.ip_phone} · ${rec.reason_ar || rec.reason}. This is recorded in the audit log.`,
      confirmLabel: "Delete call",
    });
    if (!ok) return false;
    try {
      await api("DELETE", `/api/records/${encodeURIComponent(rec.id)}`);
      toast("Call deleted");
      return true;
    } catch (e) {
      toast(e.message, "error");
      return false;
    }
  }

  function rowActions(rec, onEdit, onDelete) {
    return h("span", { class: "actions" },
      h("button", { class: "btn btn-ghost btn-sm", type: "button", title: "Edit", "aria-label": `Edit call ${rec.ip_phone}`, onclick: onEdit }, icon("pencil"), "Edit"),
      h("button", { class: "btn btn-ghost btn-sm btn-danger-text", type: "button", title: "Delete", "aria-label": `Delete call ${rec.ip_phone}`, onclick: onDelete }, icon("trash")));
  }

  // ---------------------------------------------------------------- charts
  /** Keep a sensible window of hours around the calls instead of all 24. */
  function trimHours(buckets) {
    const used = buckets.filter((b) => b.total > 0).map((b) => b.key);
    let lo = used.length ? Math.min(...used) - 1 : 8;
    let hi = used.length ? Math.max(...used) + 1 : 16;
    lo = Math.max(0, lo);
    hi = Math.min(23, hi);
    while (hi - lo < 7) {
      if (hi < 23) hi++;
      if (hi - lo < 7 && lo > 0) lo--;
    }
    return buckets.filter((b) => b.key >= lo && b.key <= hi);
  }

  function niceScale(max) {
    if (max <= 0) return { top: 4, step: 1 };
    const rough = max / 4;
    const pow = Math.pow(10, Math.floor(Math.log10(rough)));
    const frac = rough / pow;
    const step = Math.max(1, (frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10) * pow);
    return { top: Math.ceil(max / step) * step, step };
  }

  function legend(host) {
    host.replaceChildren(...STATUSES.map((s) => h("span", {}, h("i", { class: `dot s-${s}` }), STATUS_NAMES[s])));
  }

  /** Stacked columns, one per bucket: {key, label, title, resolved, transferred, ticket_inquiry, total}. */
  function drawChart(host, buckets) {
    host._buckets = buckets;
    if (!host._ro) {
      host._ro = new ResizeObserver(() => {
        if (host._buckets && host.clientWidth !== host._lastWidth) drawChart(host, host._buckets);
      });
      host._ro.observe(host);
    }
    host._lastWidth = host.clientWidth;
    const W = Math.max(host.clientWidth, 280), H = 236;
    const m = { l: 34, r: 8, t: 22, b: 26 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const n = buckets.length, band = pw / Math.max(n, 1);
    const barW = Math.max(3, Math.min(24, band - 6));
    const max = Math.max(0, ...buckets.map((b) => b.total));
    const { top, step } = niceScale(max);
    const k = ph / top;
    const base = m.t + ph;

    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "group", "aria-label": "Calls chart" });
    for (let v = 0; v <= top; v += step) {
      const y = base - v * k;
      svg.append(svgEl("line", { class: v === 0 ? "axis-line" : "grid-line", x1: m.l, x2: W - m.r, y1: y, y2: y }));
      const t = svgEl("text", { class: "ax", x: m.l - 6, y: y + 4, "text-anchor": "end" });
      t.textContent = v >= 1000 ? `${v / 1000}K` : v;
      svg.append(t);
    }
    const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(pw / 38))));
    const tip = host._tip || (host._tip = h("div", { class: "tooltip", hidden: true, role: "tooltip" }));
    const peakIdx = max > 0 ? buckets.findIndex((b) => b.total === max) : -1;

    buckets.forEach((b, i) => {
      const cx = m.l + band * i + band / 2;
      const x = cx - barW / 2;
      const g = svgEl("g", { class: "col" });
      let cum = 0, topSeg = -1;
      STATUSES.forEach((s, si) => { if (b[s] > 0) topSeg = si; });
      STATUSES.forEach((s, si) => {
        const v = b[s];
        if (!v) return;
        const yTop = base - (cum + v) * k;
        const yBot = base - cum * k - (cum > 0 ? 2 : 0);   // 2px surface gap between stacked segments
        const hgt = Math.max(1, yBot - yTop);
        if (si === topSeg) {                                // 4px rounded data end, square at the baseline side
          const r = Math.min(4, hgt / 2, barW / 2);
          g.append(svgEl("path", { class: `seg s-${s}`,
            d: `M${x},${yTop + hgt} L${x},${yTop + r} Q${x},${yTop} ${x + r},${yTop} L${x + barW - r},${yTop} Q${x + barW},${yTop} ${x + barW},${yTop + r} L${x + barW},${yTop + hgt} Z` }));
        } else {
          g.append(svgEl("rect", { class: `seg s-${s}`, x, y: yTop, width: barW, height: hgt }));
        }
        cum += v;
      });
      if (i % every === 0) {
        const t = svgEl("text", { class: "ax", x: cx, y: H - 8, "text-anchor": "middle" });
        t.textContent = b.label;
        svg.append(t);
      }
      if (i === peakIdx) {   // label only the peak; the axis and tooltip carry the rest
        const t = svgEl("text", { class: "peak", x: cx, y: Math.max(12, base - b.total * k - 6), "text-anchor": "middle" });
        t.textContent = b.total;
        g.append(t);
      }
      const hit = svgEl("rect", { class: "hit", x: m.l + band * i, y: m.t, width: band, height: ph + m.b - 6, tabindex: "0", role: "img",
        "aria-label": `${b.title}: ${b.total} calls (${STATUSES.map((s) => `${b[s]} ${STATUS_NAMES[s]}`).join(", ")})` });
      const show = () => {
        g.classList.add("hover");
        tip.replaceChildren(
          h("div", { class: "tt-title" }, b.title),
          ...STATUSES.map((s) => h("div", { class: "tt-row" }, h("i", { class: `key s-${s}` }), h("strong", {}, b[s]), h("span", { class: "label" }, STATUS_NAMES[s]))),
          h("div", { class: "tt-row tt-total" }, h("strong", {}, b.total), h("span", { class: "label" }, "Total")));
        tip.hidden = false;
        const left = Math.min(Math.max(cx - tip.offsetWidth / 2, 0), W - tip.offsetWidth);
        tip.style.left = `${left}px`;
        tip.style.top = `${Math.max(0, base - b.total * k - tip.offsetHeight - 10)}px`;
      };
      const hide = () => { g.classList.remove("hover"); tip.hidden = true; };
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("pointerleave", hide);
      hit.addEventListener("focus", show);
      hit.addEventListener("blur", hide);
      g.append(hit);
      svg.append(g);
    });
    if (max === 0) {
      const t = svgEl("text", { class: "empty-note", x: m.l + pw / 2, y: m.t + ph / 2, "text-anchor": "middle" });
      t.textContent = "No calls in this period";
      svg.append(t);
    }
    host.replaceChildren(svg, tip);
  }

  /** Table twin of the chart (the accessible equivalent: every value readable without hovering). */
  function chartTable(buckets, firstColumn) {
    return h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, h("th", {}, firstColumn), ...STATUSES.map((s) => h("th", { class: "num" }, STATUS_NAMES[s])), h("th", { class: "num" }, "Total"))),
      h("tbody", {}, buckets.map((b) => h("tr", { class: b.total ? "" : "zero" },
        h("td", {}, b.title), ...STATUSES.map((s) => h("td", { class: "num" }, fmtNum(b[s]))), h("td", { class: "num" }, fmtNum(b.total)))))));
  }

  /** Re-draw a chart <-> table toggle. ``getView`` returns {buckets, firstColumn}. */
  function chartWithToggle(host, button, getView) {
    let asTable = false;
    const render = () => {
      const { buckets, firstColumn } = getView();
      button.textContent = asTable ? "View as chart" : "View as table";
      if (asTable) { host.replaceChildren(chartTable(buckets, firstColumn)); host._buckets = null; }
      else drawChart(host, buckets);
    };
    button.addEventListener("click", () => { asTable = !asTable; render(); });
    return render;
  }

  window.CL = { h, $, svgEl, icon, api, csrf, toast, modal, confirmDialog, editRecord, deleteRecord, rowActions, statusChip,
    reasonName, bidi, reasonBars, barRows, branchPicker, branchText, normAr, trimHours, drawChart, legend, chartWithToggle, fmtNum, hhmm, fmtDay, isoDate, addDays, parseISODate, pct, agoText,
    STATUSES, STATUS_NAMES };
})();
