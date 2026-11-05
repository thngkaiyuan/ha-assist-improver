// Settings: the pipeline drawn as a track (track.js), each AI step's model a click away (pickers.js), the household
// knowledge (household.js), one run control, the history of what the improver did, and Uninstall.

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, plural, fill, listed, ago}, {TEXT, STAGES, STEPS, AT_WORK, COUNTS, SCHEDULE, GATE_NAMES}, {svg, glyph},
       track, common, pickers, household] = await Promise.all(
  ["format", "text", "icons", "track", "common", "pickers", "household"].map((name) => import(`./${name}.js${v}`)));

const IDS = ["requests", "triage", "analyze", "propose", "review", "test", "write", "you", "household"];

function step(p, id, L, s) {
  // One step's card: its title and what it does (a bubble), its model or setting, its gate, and how many conversations
  // are at it: new at Your Assist, held at a step between (held there while it was blocked, else for another cause,
  // the why on each conversation's chip), up for your review.
  const station = STEPS[id], box = L.at[id], run = p.run?.run, preview = p.run?.preview || {};
  const paused = new Set(s.paused), B = TEXT.badges;
  const isPaused = station.gate && paused.has(station.gate);
  const atWork = run?.status === "running" && (AT_WORK[run.stage] || []).includes(id);  // The run's stage at work.
  const n = (preview.stages || {})[COUNTS[id]] || 0;
  let badge = "";
  if (n) {
    const blocked = ((preview.held || {})[station.gate] || {}).blocked || 0;
    const kind = isPaused || blocked ? "held" : id === "requests" ? "in" : id === "you" ? "out" : "";
    const what = id === "requests" ? fill(B.new, {n}) : id === "you" ? fill(B.review, {n})
      : [blocked && fill(B.held, {n: blocked}), n > blocked && fill(B.waiting, {n: n - blocked})].filter(Boolean).join(" · ");
    badge = `<span class="badge ${kind}" data-tip="${what}">${n}</span>`;
  }
  let chip = "";
  if (station.role) {
    const role = s.roles[station.role], task = pickers.task(s, role.entity_id), M = TEXT.models;
    const warn = !!role.problem || (role.size === "large" && task?.weak);
    const off = role.problem === "none" ? M.none : id === "triage" ? TEXT.passThrough : {label: "Off"};
    const label = role.problem === "missing" ? M.missing.label : role.entity_id ? (task?.label || role.entity_id) : off.label;
    const tip = role.problem === "missing" ? M.missing.tip : role.problem === "unavailable" ? M.unavailable
      : role.entity_id ? (warn ? M.weak : "") : off.tip;
    chip = `<button class="chip-model ${warn ? "warn" : ""} ${role.entity_id ? "" : "off"}" data-role="${station.role}"
      ${tip ? `data-tip="${esc(tip)}"` : ""} aria-label="${esc(station.title)}: ${esc(label)}">${warn ? "&#9888; " : ""}${esc(label)} &#9662;</button>`;
  } else if (id === "test") {
    chip = `<button class="chip-model fixed" data-knob="test" data-tip="${esc(fill(TEXT.simulate.tip, {n: s.tests_per_goal}))}">${esc(TEXT.simulate.label)}</button>`;
  } else if (id === "requests") {
    const h = p.household || {}, pipeline = (h.pipelines || []).find((x) => x.preferred);  // The one Assist uses by default.
    chip = `<div class="chip-row"><button class="chip-model" data-assist="1" data-tip="${esc(TEXT.assistant.tip)}">${
      esc(pipeline?.name || "Choose…")} &#9662;</button></div>`;
  } else if (id === "household") {
    // The lines learned, and how many improvements were applied.
    const lines = p.household?.lines || 0, K = TEXT.knowledge;
    chip = `<div class="chip-row"><button class="chip-model" data-knowledge="1" data-tip="${esc(K.tip)}">${
      glyph("mdi:book-heart-outline", "in-chip")}${esc(fill(K[L.vertical ? "chipShort" : "chip"], {n: plural(lines, "line")}))}</button>${
      preview.applied ? `<span class="done-count" data-tip="${esc(fill(B.applied, {n: preview.applied}))}">${svg("check")}${preview.applied}</span>` : ""}</div>`;
  }
  const gate = station.gate ? `<button class="gate ${isPaused ? "paused" : ""}" data-gate="${station.gate}"
    data-tip="${esc(fill(TEXT.block[isPaused ? "blocked" : "tip"], {step: station.title.toLowerCase()}))}"
    aria-label="${isPaused ? "Unblock" : "Block"} ${esc(station.title)}" aria-pressed="${!!isPaused}">${svg("block")}</button>` : "";
  const info = `<span class="info-tip" data-tip="${esc(station.tip)}" tabindex="0" aria-label="About ${esc(station.title)}">${svg("info")}</span>`;
  const cls = [id === "review" && "sub", id === "you" && "you", atWork && "now", isPaused && "paused"].filter(Boolean).join(" ");
  const place = L.vertical ? `left:${box.x}px;top:${box.y}px;width:${box.w}px;min-height:${box.min}px`
    : `left:${box.cx}px;top:${box.cy}px${box.fixed ? `;width:${box.w}px` : ""}`;
  return `<div class="stn ${cls}" data-step="${id}" style="${place}">
    <div class="s-head">${glyph(station.icon)}<span class="s-title">${esc(station.title)}</span>${info}${L.vertical ? "" : gate}</div>
    ${chip}${L.vertical ? gate : ""}${badge}</div>`;
}

function head(p, s) {
  // One run control: Run now (on all a full run moves), and its menu for when it runs on its own and for running one
  // step (on what's held there).
  const running = p.run?.run?.status === "running";
  const preview = p.run?.preview || {};
  const last = esc(common.lastRun(p.run?.run) || "No runs yet");
  const runWords = common.runLabel(preview);
  const waits = (gate) => common.heldAt(preview, gate);
  const when = SCHEDULE.find(([value]) => value === s.schedule) || SCHEDULE[0];
  const menu = p.runMenu ? `<div class="menu">${SCHEDULE.map(([value, label]) =>
    `<div data-schedule="${value}" class="row-ic ${s.schedule === value ? "on" : ""}">${value === "off" ? `<span class="ic-space"></span>` : svg("clock")}<span class="lbl">${label}</span>${
      s.schedule === value ? `<span class="count">&#10003;</span>` : ""}</div>`).join("")}
    <div class="group">${esc(TEXT.runOneStep)}</div>${Object.entries(GATE_NAMES).map(([gate, name]) =>
      `<div data-only="${gate}" class="row-ic">${svg("play")}<span class="lbl">${name}</span>${waits(gate) ? `<span class="count">${waits(gate)}</span>` : ""}</div>`).join("")}</div>` : "";
  const run = running
    ? `<button class="btn ghost" id="cancel-run" data-tip="${esc(common.runTitle(p.run.run))} · ${esc((STAGES.find(([k]) => k === p.run.run.stage) || [])[1] || "")}"
        ${p.cancelling ? "disabled" : ""}>${p.cancelling ? "Cancelling…" : "Cancel run"}</button>`
    : `<div class="split down"><button class="main" id="run-now" data-tip="${last}">${svg("play")}${esc(runWords)}</button>
       <button class="arrow" id="run-menu" aria-label="When it runs, or run one step" data-tip="${esc(when[1])}">${s.schedule === "off" ? "&#9662;" : svg("clock")}</button>${menu}</div>`;
  return `<div class="flow-head">${run}</div>`;
}

function history(p) {
  // What the improver did, newest first: runs, improvements going up for review, and decisions on them.
  const events = p.log || [], shown = p.historyAll ? events : events.slice(0, 8), H = TEXT.history;
  const icon = {run: "mdi:play-circle-outline", posted: "mdi:feather", accept: "mdi:check-circle-outline",
                undo: "mdi:undo-variant", decline: "mdi:close-circle-outline", revise: "mdi:pencil-outline"};
  const runText = (e) => {
    // What the run did, from the results it published (flow.Flow.run): taken in, analysed, new improvements, already
    // covered, held (with the first one's why), couldn't be read.
    const x = e.progress || {};
    const parts = [x.taken_in && `${x.taken_in} taken in`, x.analyzed && `${x.analyzed} analyzed`,
      x.new_improvements && plural(x.new_improvements, "new improvement"), x.duplicates && fill(H.addressed, {n: x.duplicates})];
    const held = x.held && fill(H.failed, {n: x.held, why: x.why || ""});
    // A step that couldn't finish: its own failure (one item's stays with that item, listed under Details only).
    const stopped = (x.problems || []).filter((y) => !y.item);
    const unfinished = stopped.length && fill(H.unfinished, {steps: listed([...new Set(stopped.map((y) => GATE_NAMES[y.stage] || y.stage))])});
    const unreadable = x.unreadable && fill(H.setAside, {n: x.unreadable});
    const which = H.triggers[e.picked ? "picked" : e.trigger];  // A manual full run says nothing more.
    // What went wrong, for a bug report: the error, or each step that couldn't finish (no longer on the home page).
    const lines = e.error && !/ can't run: /.test(e.error) ? [e.error]
      : (x.problems || []).map((y) => `${GATE_NAMES[y.stage] || y.stage}${y.item ? ` · ${y.item}` : ""}: ${y.error}`);
    return [`${H.run[e.status] || "Run"}${e.only ? ` · ${GATE_NAMES[e.only]} only` : ""}${which ? ` · ${which}` : ""}`,
            e.error ? (/ can't run: /.test(e.error) ? e.error : H.stopped) : [...parts, held, unfinished, unreadable].filter(Boolean).join(" · "),
            lines.length ? common.details(p, `history:${e.at}`, lines) : ""];
  };
  const rows = shown.map((e) => {
    const [title, sub, more = ""] = e.kind === "run" ? runText(e) : e.kind === "posted" ? [H.posted, e.title]
      : [H.decisions[e.reason || e.decision] || e.decision, e.title + (e.by ? ` · ${e.by}` : "")];
    const glyphName = e.kind === "decision" ? icon[e.decision] || "mdi:account-check-outline" : icon[e.kind];
    return `<div class="h-row ${e.id ? "h-link" : ""}" ${e.id ? `data-open="${esc(e.id)}"` : ""}>${glyph(glyphName)}
      <div class="h-text"><div>${esc(title)}</div>${sub ? `<div class="h-sub">${esc(sub)}</div>` : ""}${more}</div>
      <span class="h-when">${esc(ago(e.at))}</span></div>`;
  }).join("");
  return `<div class="card history"><div class="h-title">${esc(H.title)}</div>${rows || `<div class="h-empty">${esc(H.empty)}</div>`}
    ${events.length > shown.length ? `<div class="h-more"><button class="link" id="history-more">${esc(H.more)}</button></div>` : ""}</div>`;
}

function uninstall(p) {
  // Uninstall's one step, in a modal: what it removes; with lines learned, the household's lines as one text to keep
  // if it likes (a copy button in the box's corner), and a box to tick before it can go. Uninstall removes the
  // integration, then Home Assistant's integrations page opens.
  const U = TEXT.uninstall, u = p.uninstall, text = p.household?.text || "", copy = u.copied ? U.copied : U.copy;
  const keep = text ? `<div class="u-box"><pre class="u-text" tabindex="0">${esc(text)}</pre><button class="u-copy ${
        u.copied ? "done" : ""}" id="u-copy" aria-label="${esc(copy)}" data-tip="${esc(copy)}">${svg(u.copied ? "check" : "copy")}</button></div>
      <label class="reason"><input type="checkbox" id="u-saved" ${u.saved ? "checked" : ""}><span class="reason-label">${
        esc(U.saved)}</span></label>` : "";
  return `<div class="modal-shade u-cancel"></div><div class="modal confirm-modal" role="dialog" aria-modal="true" aria-label="${esc(U.title)}">
      <div class="modal-head"><b>${esc(U.title)}</b><span class="kn-count"></span>
        <button class="modal-close u-cancel" aria-label="${esc(TEXT.confirm.notNow)}">&#10005;</button></div>
      <div class="modal-body confirm u-body"><p class="c-what">${esc(`${U.what} ${U.stay}${text ? ` ${U.keep}` : ""}`)}</p>${keep}${
        u.error ? `<div class="error-text">${esc(u.error)}</div>` : ""}</div>
      <div class="u-foot"><button class="btn ghost u-cancel" ${u.busy ? "disabled" : ""}>${esc(TEXT.confirm.notNow)}</button>
      <button class="btn danger" id="u-confirm" ${u.busy || (text && !u.saved) ? "disabled" : ""}>${
        esc(u.busy ? U.busy : U.button)}</button></div></div>`;
}

async function copyLines(p) {
  // The lines to the clipboard; the button shows they went (a check mark) for a moment.
  const u = p.uninstall;
  if (!await common.copy(p, p.household?.text || "") || p.uninstall !== u) return;
  u.copied = true;
  p.render();
  clearTimeout(u.copiedTimer);
  u.copiedTimer = setTimeout(() => { u.copied = false; if (p.uninstall === u) p.render(); }, 2000);
}

async function confirmUninstall(p) {
  const u = p.uninstall;
  Object.assign(u, {busy: true, error: null});
  p.render();
  try {
    await p.ws("uninstall");
  } catch (err) {
    Object.assign(u, {busy: false, error: err.message || String(err)});
    p.render();
    return;
  }
  p.uninstall = null;
  p.nav("/config/integrations/dashboard");  // The panel goes with the integration.
}

export function render(p) {
  const s = p.settings;
  const width = p.trackWidth || Math.max(300, Math.min(p.host.clientWidth || 1000, 1180) - 64);
  const L = track.layout(width, p.sizes, p.heights);
  const running = p.run?.run?.status === "running";
  const preview = p.run?.preview;
  const label = `<span class="stream-label ${L.labelVertical ? "vertical" : ""}" style="left:${L.label[0]}px;top:${L.label[1]}px">${
    esc(L.vertical ? TEXT.revise.short : TEXT.revise.label)}</span>`;
  const flow = `<div class="card flow-card">${head(p, s)}<div class="track ${L.vertical ? "vertical" : ""}" style="height:${L.H}px">
    ${track.trackSvg(L, running)}${IDS.map((id) => step(p, id, L, s)).join("")}${label}</div></div>`;
  const confirm = p.confirming && preview ? common.confirm(p, preview) : "";
  const shade = p.runMenu ? `<div class="backdrop" id="menu-backdrop"></div>` : "";
  const pop = p.pop?.kind === "knowledge" ? household.modal(p) : pickers.render(p, s);
  const end = `<div class="end-row"><button class="btn soft-danger" id="uninstall">${esc(TEXT.uninstall.button)}</button></div>`;
  return {
    bar: common.toolbar("Settings", true, s.developer ? common.devLink() : ""),
    page: `<div class="page wide">${flow}${history(p)}${end}</div>`,
    layer: `${shade}${pop}${confirm}${p.uninstall ? uninstall(p) : ""}`,
  };
}

export function relayout(p) {
  // Lay the track out again when its width changed (and on first sight, once the width is known).
  const box = p.root.querySelector(".track");
  if (!box) return;
  const width = Math.round(box.clientWidth);
  if (width && Math.abs(width - (p.trackWidth || 0)) > 2) { p.trackWidth = width; p.render(); }
}

function measure(p) {
  // Each card's own size in the wide layout, measured on a hidden copy (so it works whichever layout shows), and in
  // the vertical layout, each card's height as drawn (two lines when its title and controls don't fit on one), for
  // the next layout; drawn again only if one changed.
  const page = p.root.querySelector(".page");
  if (!page || !p.settings) return;
  const zero = {cx: 0, cy: 0, x: 0, y: 0, w: 0, h: 0}, L = {vertical: false, at: Object.fromEntries(IDS.map((id) => [id, zero]))};
  const probe = document.createElement("div");
  probe.className = "track";
  probe.style.cssText = "position:absolute;visibility:hidden;left:-10000px;top:0;width:2000px;height:200px";
  probe.innerHTML = IDS.map((id) => step(p, id, L, p.settings)).join("");
  page.appendChild(probe);
  const sizes = {};
  let changed = false;
  probe.querySelectorAll(".stn").forEach((el) => {
    const r = el.getBoundingClientRect();  // Exact (offsetWidth rounds), plus a pixel so nothing truncates.
    const size = {w: Math.ceil(r.width) + 1, h: Math.ceil(r.height)}, old = p.sizes?.[el.dataset.step];
    sizes[el.dataset.step] = size;
    changed = changed || !old || Math.abs(old.w - size.w) > 1 || Math.abs(old.h - size.h) > 1;
  });
  probe.remove();
  if (changed) p.sizes = sizes;
  const drawn = [...p.root.querySelectorAll(".track.vertical > .stn")];
  if (drawn.some((el) => el.offsetHeight !== p.heights?.[el.dataset.step])) {
    p.heights = Object.fromEntries(drawn.map((el) => [el.dataset.step, el.offsetHeight]));
    changed = true;
  }
  if (changed) p.render();
}

export function after(p) {
  relayout(p);
  measure(p);
  pickers.place(p);
  household.after(p);
  const lines = p.root.querySelector(".u-text");  // Uninstall's copy button stays clear of the box's scrollbar, if it takes room.
  lines?.parentElement.style.setProperty("--scrollbar", `${lines.offsetWidth - lines.clientWidth}px`);
}

export const actions = {
  click: [
    ["#run-menu", (p) => { p.runMenu = !p.runMenu; p.render(); }],
    ["[data-schedule]", (p, el) => { p.runMenu = false; p.saveSetting({schedule: el.dataset.schedule}); }],
    ["[data-only]", (p, el) => { Object.assign(p, {runMenu: false, only: el.dataset.only, confirming: true}); p.render(); }],
    ["[data-gate]", (p, el) => p.saveSetting({stage: el.dataset.gate, pause: !p.settings.paused.includes(el.dataset.gate)})],
    ["#history-more", (p) => { p.historyAll = true; p.render(); }],
    ["[data-open]", (p, el) => p.go(el.dataset.open)],
    ["#uninstall", (p) => { p.uninstall = {saved: false, copied: false, error: null, busy: false}; p.render(); }],
    [".u-cancel", (p) => { if (!p.uninstall?.busy) { p.uninstall = null; p.render(); } }],
    ["#u-copy", (p) => copyLines(p)],
    ["#u-confirm", (p) => confirmUninstall(p)],
  ],
  change: [["#u-saved", (p, el) => { p.uninstall.saved = el.checked; p.render(); }]],
};
