// What the views share: the toolbar, the loading and error pages, a run's progress and how the last one went, and
// the run confirmation; and the actions behind them (back, the header's links, Run now, Cancel).

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, plural, fill, listed, ago, devUrl, SETTINGS_URL}, {TEXT, STAGES, GATE_NAMES}, {svg}] = await Promise.all([
  import(`./format.js${v}`), import(`./text.js${v}`), import(`./icons.js${v}`)]);

export const toolbar = (title, back, end = "") => `<div class="toolbar">${back ? `<a id="back" style="color:inherit">&#8592;</a>`
  : `<span id="menu" class="menu-slot" data-keep></span>`}
      <span>${esc(title)}</span>${end}</div>`;

export const devLink = () =>
  `<button class="tb-btn tb-text" id="open-developer" title="Developer view" aria-label="Developer view">${svg("code")}Developer</button>`;

// A spinner beside the word, which fades in only if the wait is noticeable.
export const loadingRow = (cls = "loading") => `<div class="${cls}" role="status"><span class="spinner" aria-hidden="true"></span>Loading…</div>`;

export const loadingPage = {render: () => ({bar: toolbar("Assist Improver"), page: `<div class="page">${loadingRow()}</div>`})};

export const errorPage = {render: (p) => ({bar: toolbar("Assist Improver", true),
                                           page: `<div class="page"><div class="conflict">${esc(p.error)}</div></div>`})};

export function progress(run, cancelling) {
  // A run in progress: where it is, with live counts; it can be left and come back to.
  const p = run.progress || {};
  const at = STAGES.findIndex(([key]) => key === run.stage);
  const detail = {
    analyze: p.analyze_total != null ? `Analyzing conversations: ${p.analyzed || 0} of ${p.analyze_total}` +
      (p.skipped_as_smooth ? ` (${p.skipped_as_smooth} went smoothly, skipped)` : "") : "Finding conversations to analyze…",
    propose: `Proposing fixes from ${plural(p.sessions || 0, "conversation")}, with a reviewer checking each…`,
    test: `Testing ${plural(p.hypotheses || 0, "fix")} in a sandboxed copy of your home…`,
    write: `Writing up ${plural(p.writing || 0, "improvement")}…`}[run.stage];
  return `<div class="run"><div class="run-head"><div class="what"><div class="title">Improving your assistant</div>
      <div class="sub">${esc(detail)} You can leave this page; it keeps going.</div></div>
      <button class="btn ghost" id="cancel-run" ${cancelling ? "disabled" : ""}>${cancelling ? "Cancelling…" : "Cancel"}</button></div>
      <div class="stepper">${STAGES.map(([key, label], n) =>
        `<div class="step ${n < at ? "done" : n === at ? "now" : ""}"><div class="bar"></div>${label}</div>`).join("")}</div></div>`;
}

export function lastRun(run) {
  if (!run) return "";
  const p = run.progress || {};
  const what = {done: "Last run", failed: "Last run stopped early", cancelled: "Last run cancelled",
                interrupted: "Last run was interrupted"}[run.status] || "Last run";
  const found = p.new_improvements != null ? ` · ${plural(p.new_improvements, "new improvement")}` : "";
  return `${what} ${ago(run.finished || run.started)}${found}`;
}

export function failure(p, run) {
  // What went wrong in the last run, in a line, with what happens next. A step whose model is missing names the
  // step and opens the settings; a step that couldn't finish is named, and the next run tries it again; the
  // technical details are folded away, for a bug report (and stay open, once opened, while that run is shown).
  const F = TEXT.failure, problems = run?.progress?.problems || [], key = `problem:${run?.id}`;
  const details = (lines) => `<details data-key="${esc(key)}" ${p.open.has(key) ? "open" : ""}><summary>${esc(F.details)}</summary><code>${
    lines.map(esc).join("\n")}</code></details>`;
  if (run?.status === "failed" && run.error) {
    if (/ can't run: /.test(run.error)) {
      return `<div class="notice">${esc(run.error)} <button class="link go-settings">${esc(F.settings)}</button></div>`;
    }
    return `<div class="notice">${esc(F.stopped)} ${details([run.error])}</div>`;
  }
  if (!problems.length) return "";
  const names = problems.map((p) => GATE_NAMES[p.stage] || p.stage);
  return `<div class="notice">${esc(fill(F.stage, {steps: listed(names)}))} ${details(problems.map((p, i) => `${names[i]}: ${p.error}`))}</div>`;
}

export function askAutoAccept() {
  const A = TEXT.autoAccept;
  return `<div class="modal-shade ask-cancel"></div><div class="modal confirm-modal" role="dialog" aria-modal="true" aria-label="${esc(A.confirm)}">
      <div class="modal-head"><b>${esc(A.confirm)}</b><span class="kn-count"></span>
        <button class="modal-close ask-cancel" aria-label="${esc(TEXT.confirm.notNow)}">&#10005;</button></div>
      <div class="modal-body confirm"><p class="c-what">${esc(A.confirmText)}</p>
      <div class="actions"><button class="btn ghost ask-cancel">${esc(TEXT.confirm.notNow)}</button>
      <button class="btn" id="ask-ok">${esc(A.on)}</button></div></div></div>`;
}

export function confirm(p, preview) {
  // One sentence on what the run will do, a gentle line on cost, and Run: in a modal, so the page stays put; a click
  // outside or Esc is "Not now".
  const b = preview.backlog || {}, only = p.only, C = TEXT.confirm, blocked = new Set(only ? [] : preview.paused || []);
  const count = (n, text) => fill(text, {n, s: n === 1 ? "" : "s"});
  let sentence;
  if (only) {
    const n = only === "triage" ? (b.requests || 0) + (b.triage || 0) : b[only] || 0;
    sentence = count(n, C.steps[n ? only : `${only}None`]);
  } else {
    const order = [["requests", "triage"], ["analyze", "analyze"], ["propose", "propose"], ["test", "test"], ["write", "write"]];
    const items = order.filter(([key, gate]) => b[key] && !(key !== "requests" && blocked.has(gate)))
      .map(([key]) => key === "propose" ? C.items.propose : count(b[key], C.items[key]));
    const shown = items.slice(0, 2).join(" and ");
    sentence = !items.length ? (p.force ? C.again : C.nothing)
      : items.length > 2 ? fill(C.more, {items: items.slice(0, 2).join(", ")})
      : fill(C.waiting, {items: shown, are: items.length === 1 && /^1 /.test(items[0]) ? "is" : "are"});
  }
  const held = [...blocked].map((g) => GATE_NAMES[g]).filter(Boolean);
  const stages = only ? [only] : Object.keys(GATE_NAMES).filter((g) => !blocked.has(g));
  const broken = [...new Set(stages.flatMap((g) => (preview.blocked_models || {})[g] || []))];
  const modelsLine = broken.length ? fill(C.models, {steps: broken.join(" and "), its: broken.length === 1 ? C.its : C.their}) : "";
  const blockedLine = held.length ? fill(C.blocked, {steps: held.join(", "), is: held.length === 1 ? "is" : "are"}) : "";
  const title = only ? fill(C.step, {step: GATE_NAMES[only]}) : C.title;
  return `<div class="modal-shade" id="confirm-shade"></div><div class="modal confirm-modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="modal-head">${svg("play")}<b>${esc(title)}</b><span class="kn-count"></span>
        <button class="modal-close" id="cancel-confirm" aria-label="${esc(C.notNow)}">&#10005;</button></div>
      <div class="modal-body confirm"><p class="c-what">${esc(sentence)}</p>
      ${blockedLine ? `<p class="cost">${esc(blockedLine)}</p>` : ""}${modelsLine ? `<p class="c-warn">&#9888; ${esc(modelsLine)}</p>` : ""}
      <p class="cost">${esc(C.cost)}</p>
      <div class="actions"><button class="btn ghost" id="cancel-confirm-2">${esc(C.notNow)}</button>
      <button class="btn" id="start-run" ${modelsLine || p.starting ? "disabled" : ""}>${esc(p.starting ? "Starting…" : C.run)}</button></div></div></div>`;
}

async function startRun(p) {
  p.starting = true;
  p.render();
  try {
    await p.ws("run/start", {confirmed: true, force: !!p.force, ...(p.only ? {only: p.only} : {})});
  } catch (err) {
    alert(err.message || String(err));
  }
  p.starting = false;
  p.confirming = false;
  await p.load();
}

async function cancelRun(p) {
  p.cancelling = true;
  p.render();
  try {
    await p.ws("run/cancel");
  } catch (err) {
    p.cancelling = false;
    alert(err.message || String(err));
  }
  setTimeout(() => p.load(), 800);  // Long enough for the run to stop; sooner than the next refresh.
}

const ask = (p, force) => { p.confirming = true; p.force = force; p.only = null; p.render(); };

export const actions = {
  click: [
    ["#back", (p) => p.go(null)],
    ["#open-settings, .go-settings", (p) => p.nav(SETTINGS_URL)],
    ["#open-developer", (p) => p.nav(devUrl("find"))],
    ["#run-now", (p) => ask(p, false)],
    ["#run-anyway", (p) => ask(p, true)],
    ["#cancel-confirm, #cancel-confirm-2, #confirm-shade", (p) => { p.confirming = false; p.render(); }],
    ["#start-run", (p) => startRun(p)],
    ["#cancel-run", (p) => cancelRun(p)],
    ["#menu-backdrop", (p) => { p.menuOpen = false; p.runMenu = false; p.render(); }],
  ],
};
