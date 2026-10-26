// Settings: the pipeline drawn as a track (track.js), each AI step's model a click away (pickers.js), the household
// knowledge (household.js), the switches, one run control, and the history of what the improver did.

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, plural, fill, listed, ago}, {TEXT, STAGES, STEPS, AT_WORK, BACKLOG, SCHEDULE, GATE_NAMES}, {svg, glyph},
       track, common, pickers, household] = await Promise.all(
  ["format", "text", "icons", "track", "common", "pickers", "household"].map((name) => import(`./${name}.js${v}`)));

const IDS = ["requests", "triage", "analyze", "propose", "review", "test", "write", "you", "household"];

function step(p, id, L, s) {
  // One step's card: its title and what it does (a bubble), its model or setting, its gate, and what waits at it.
  const station = STEPS[id], box = L.at[id], run = p.run?.run, preview = p.run?.preview || {};
  const backlog = preview.backlog || {}, paused = new Set(s.paused);
  const isPaused = station.gate && paused.has(station.gate);
  // At work: the run's stage; within analysis, only the half it's in (the quick check, then analysis alone).
  const working = run?.stage === "analyze" && run.progress?.phase === "analyze" ? ["analyze"] : AT_WORK[run?.stage] || [];
  const atWork = run?.status === "running" && working.includes(id);
  const n = backlog[BACKLOG[id]] || 0;
  let badge = "";
  if (n && id !== "household") {
    const kind = isPaused ? "held" : id === "requests" ? "in" : id === "you" ? "out" : "";
    const what = fill(TEXT.badges[isPaused ? "held" : id === "you" ? "review" : id === "requests" ? "new" : "waiting"], {n});
    badge = `<span class="badge ${kind}" data-tip="${what}">${n}</span>`;
  }
  let chip = "";
  if (station.role) {
    const role = s.roles[station.role], task = pickers.task(s, role.entity_id), M = TEXT.models;
    const warn = !!role.problem || (role.size === "large" && task?.weak);
    const off = role.problem === "none" ? M.none : id === "triage" ? TEXT.passThrough : {label: "Off", tip: TEXT.followUpsOff};
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
    // Its one switch, whether every assistant on Assist reads the lines, beside the lines themselves (dimmed while off).
    const lines = p.household?.lines || 0, K = TEXT.knowledge, on = s.household_knowledge !== false;
    const tip = fill(K[on ? "on" : "off"], {lines: plural(lines, "line")});
    chip = `<div class="chip-row">${common.toggle("household-knowledge", on, `aria-label="${esc(K.label)}" data-tip="${esc(tip)}"`)}<button
      class="chip-model ${on ? "" : "off"}" data-knowledge="1" data-tip="${esc(K.tip)}">${
      glyph("mdi:book-heart-outline", "in-chip")}${esc(fill(K[L.vertical ? "chipShort" : "chip"], {n: plural(lines, "line")}))}</button>${
      backlog.household ? `<span class="done-count" data-tip="${esc(fill(TEXT.badges.applied, {n: backlog.household}))}">${svg("check")}${backlog.household}</span>` : ""}</div>`;
  }
  const gate = station.gate ? `<button class="gate ${isPaused ? "paused" : ""}" data-gate="${station.gate}"
    data-tip="${esc(fill(TEXT.block[isPaused ? "blocked" : "tip"], {step: station.title.toLowerCase()}))}"
    aria-label="${isPaused ? "Unblock" : "Block"} ${esc(station.title)}" aria-pressed="${!!isPaused}">${svg("block")}</button>` : "";
  const info = `<span class="info-tip" data-tip="${esc(station.tip)}" tabindex="0" aria-label="About ${esc(station.title)}">${svg("info")}</span>`;
  const cls = [id === "review" && "sub", id === "you" && "you", atWork && "now", isPaused && "paused"].filter(Boolean).join(" ");
  const place = L.vertical ? `left:${box.x}px;top:${box.y}px;width:${box.w}px;height:${box.h}px`
    : `left:${box.cx}px;top:${box.cy}px${box.fixed ? `;width:${box.w}px` : ""}`;
  return `<div class="stn ${cls}" data-step="${id}" style="${place}">
    <div class="s-head">${glyph(station.icon)}<span class="s-title">${esc(station.title)}</span>${info}${L.vertical ? "" : gate}</div>
    ${chip}${L.vertical ? gate : ""}${badge}</div>`;
}

function head(p, s) {
  // The switches, and one run control: Run now, and its menu for when it runs on its own and for running one step.
  const running = p.run?.run?.status === "running";
  const backlog = p.run?.preview?.backlog || {};
  const last = esc(common.lastRun(p.run?.run) || "No runs yet");
  const runWords = common.runLabel(common.allConversations(backlog, new Set(s.paused)));  // A full run: on all of them.
  const waits = (gate) => gate === "triage" ? (backlog.requests || 0) + (backlog.triage || 0) : backlog[gate] || 0;  // Triage takes in the new ones.
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
  const sandboxTip = TEXT.sandbox.tip + (s.practice.others.length ? " " + fill(TEXT.sandbox.others, {names: s.practice.others.join(", ")}) : "");
  const row = common.switchRow;
  return `<div class="flow-head"><div class="toggles">${row("on-demand", s.on_demand, TEXT.onDemand.label, TEXT.onDemand.tip)}
    ${row("practice", s.practice.on, TEXT.sandbox.label, sandboxTip)}
    ${row("auto-accept", s.auto_accept, TEXT.autoAccept.label, TEXT.autoAccept.tip)}</div>
    <span class="spacer"></span>${run}</div>`;
}

function history(p) {
  // What the improver did, newest first: runs, improvements going up for review, and decisions on them.
  const events = p.log || [], shown = p.historyAll ? events : events.slice(0, 8), H = TEXT.history;
  const icon = {run: "mdi:play-circle-outline", posted: "mdi:feather", accept: "mdi:check-circle-outline",
                undo: "mdi:undo-variant", decline: "mdi:close-circle-outline", revise: "mdi:pencil-outline"};
  const runText = (e) => {
    const x = e.progress || {}, parts = [x.taken_in && `${x.taken_in} taken in`, x.analyzed && `${x.analyzed} analyzed`,
      x.new_improvements && plural(x.new_improvements, "new improvement"), x.auto_accepted && `${x.auto_accepted} accepted automatically`,
      x.already_addressed && fill(H.addressed, {n: x.already_addressed})];
    const failed = x.analyze_failed && fill(H.failed, {n: x.analyze_failed, s: x.analyze_failed === 1 ? "" : "s", why: x.problem || ""});
    const unfinished = (x.problems || []).length && fill(H.unfinished, {steps: listed(x.problems.map((y) => GATE_NAMES[y.stage] || y.stage))});
    const aside = x.unreadable && fill(H.setAside, {n: x.unreadable, s: x.unreadable === 1 ? "" : "s"});
    const which = H.triggers[e.picked ? "picked" : e.trigger];  // A full run from here says nothing more.
    // What went wrong, for a bug report: the error, or each step that couldn't finish (no longer on the home page).
    const lines = e.error && !/ can't run: /.test(e.error) ? [e.error]
      : (x.problems || []).map((y) => `${GATE_NAMES[y.stage] || y.stage}: ${y.error}`);
    return [`${H.run[e.status] || "Run"}${e.only ? ` · ${GATE_NAMES[e.only]} only` : ""}${which ? ` · ${which}` : ""}`,
            e.error ? (/ can't run: /.test(e.error) ? e.error : H.stopped) : [...parts, failed, unfinished, aside].filter(Boolean).join(" · "),
            lines.length ? common.details(p, `history:${e.at}`, lines) : ""];
  };
  const rows = shown.map((e) => {
    const [title, sub, more = ""] = e.kind === "run" ? runText(e) : e.kind === "posted" ? [H.posted, e.title]
      : [e.auto ? H.auto : H.decisions[e.decision] || e.decision, e.title + (e.by && !e.auto ? ` · ${e.by}` : "")];
    const glyphName = e.kind === "decision" ? icon[e.decision] || "mdi:account-check-outline" : icon[e.kind];
    return `<div class="h-row ${e.id ? "h-link" : ""}" ${e.id ? `data-open="${esc(e.id)}"` : ""}>${glyph(glyphName)}
      <div class="h-text"><div>${esc(title)}</div>${sub ? `<div class="h-sub">${esc(sub)}</div>` : ""}${more}</div>
      <span class="h-when">${esc(ago(e.at))}</span></div>`;
  }).join("");
  return `<div class="card history"><div class="h-title">${esc(H.title)}</div>${rows || `<div class="h-empty">${esc(H.empty)}</div>`}
    ${events.length > shown.length ? `<div class="h-more"><button class="link" id="history-more">${esc(H.more)}</button></div>` : ""}</div>`;
}

export function render(p) {
  const s = p.settings;
  const width = p.trackWidth || Math.max(300, Math.min(p.host.clientWidth || 1000, 1180) - 64);
  const L = track.layout(width, p.sizes);
  const running = p.run?.run?.status === "running";
  const preview = p.run?.preview;
  const label = `<span class="stream-label ${L.labelVertical ? "vertical" : ""}" style="left:${L.label[0]}px;top:${L.label[1]}px">${
    esc(L.vertical ? TEXT.revise.short : TEXT.revise.label)}</span>`;
  const flow = `<div class="card flow-card">${head(p, s)}<div class="track ${L.vertical ? "vertical" : ""}" style="height:${L.H}px">
    ${track.trackSvg(L, running)}${IDS.map((id) => step(p, id, L, s)).join("")}${label}</div></div>`;
  const confirm = p.confirming && preview ? common.confirm(p, preview) : p.asking === "auto-accept" ? common.askAutoAccept() : "";
  const shade = p.runMenu ? `<div class="backdrop" id="menu-backdrop"></div>` : "";
  const pop = p.pop?.kind === "knowledge" ? household.modal(p) : pickers.render(p, s);
  return {
    bar: common.toolbar("Settings", true, s.developer ? common.devLink() : ""),
    page: `<div class="page wide">${flow}${history(p)}</div>`,
    layer: `${shade}${pop}${confirm}`,
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
  // Each card's own size in the wide layout, measured on a hidden copy (so it works whichever layout shows), for the
  // next layout; drawn again only if one changed.
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
  if (changed) { p.sizes = sizes; p.render(); }
}

export function after(p) {
  relayout(p);
  measure(p);
  pickers.place(p);
  household.after(p);
}

// Each switch: whether it's on, and what flipping it does.
const SWITCHES = {
  "on-demand": [(s) => s.on_demand, (p) => p.saveSetting({on_demand: !p.settings.on_demand})],
  "household-knowledge": [(s) => s.household_knowledge !== false,
                          (p) => p.saveSetting({household_knowledge: p.settings.household_knowledge === false})],
  recording: [(s) => s.recording !== false, (p) => p.saveSetting({recording: p.settings.recording === false})],
  "auto-accept": [(s) => s.auto_accept, (p) => {
    if (p.settings.auto_accept) return p.saveSetting({auto_accept: false});
    p.asking = "auto-accept";  // Turning it on says what it will change first.
    p.render();
  }],
  practice: [(s) => s.practice.on, async (p) => {
    try { p.settings = await p.ws("practice", {on: !p.settings.practice.on}); } catch (err) { alert(err.message || String(err)); }
    p.render();
  }],
};

function personSwitched(p, el, ev) {
  // One person's recording switch (Your Assist), shown as it's stored until the save comes back.
  const person = (p.settings.recording_people || []).find((x) => x.user_id === el.dataset.recordingPerson);
  ev.preventDefault();
  if (!person) return;
  if (el.tagName === "HA-SWITCH") el.checked = person.on;
  p.saveSetting({recording: !person.on, person: person.user_id});
}

function switched(p, el, ev) {
  // A switch shows the setting until it's saved: a save that fails, or a confirmation turned down, leaves it as it was.
  const [on, flip] = SWITCHES[el.id];
  ev.preventDefault();
  if (el.tagName === "HA-SWITCH") el.checked = on(p.settings);
  flip(p);
}

export const actions = {
  click: [
    ...Object.keys(SWITCHES).map((id) => [`button#${id}`, switched]),
    ["button[data-recording-person]", personSwitched],
    ["#ask-ok", (p) => { p.asking = null; p.saveSetting({auto_accept: true}); }],
    [".ask-cancel", (p) => { p.asking = null; p.render(); }],
    ["#run-menu", (p) => { p.runMenu = !p.runMenu; p.render(); }],
    ["[data-schedule]", (p, el) => { p.runMenu = false; p.saveSetting({schedule: el.dataset.schedule}); }],
    ["[data-only]", (p, el) => { Object.assign(p, {runMenu: false, only: el.dataset.only, force: false, confirming: true}); p.render(); }],
    ["[data-gate]", (p, el) => p.saveSetting({stage: el.dataset.gate, pause: !p.settings.paused.includes(el.dataset.gate)})],
    ["#history-more", (p) => { p.historyAll = true; p.render(); }],
    ["[data-open]", (p, el) => p.go(el.dataset.open)],
  ],
  change: [...Object.keys(SWITCHES).map((id) => [`ha-switch#${id}`, switched]), ["ha-switch[data-recording-person]", personSwitched]],
};
