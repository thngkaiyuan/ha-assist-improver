// What the views share: the toolbar, the loading and error pages, a run's progress and how the last one went, and
// the run confirmation; and the actions behind them (back, the header's links, Run now, Cancel).

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, plural, fill, listed, ago, devUrl, SETTINGS_URL}, {TEXT, STAGES, GATE_NAMES}, {svg}] = await Promise.all([
  import(`./format.js${v}`), import(`./text.js${v}`), import(`./icons.js${v}`)]);

export const toolbar = (title, back, end = "") => `<div class="toolbar">${back
  ? `<button id="back" class="tb-back" type="button"><span aria-hidden="true">&#8592;</span><span>${esc(title)}</span></button>`
  : `<span id="menu" class="menu-slot" data-keep></span><span>${esc(title)}</span>`}${end}</div>`;

// A labelled switch (HA's own when the page has it) with an optional ⓘ tip, and any attributes the switch needs; its
// clicks are the settings view's.
export const toggle = (id, on, attrs) => customElements.get("ha-switch") ? `<ha-switch id="${id}" ${attrs} ${on ? "checked" : ""}></ha-switch>`
  : `<button class="toggle ${on ? "on" : ""}" id="${id}" ${attrs} role="switch" aria-checked="${on}"></button>`;
export const switchRow = (id, on, text, tip, cls = "", attrs = "") => `<div class="toggle-row ${cls}"><label class="practice">${toggle(id, on, attrs)}${esc(text)}</label>
    ${tip ? `<span class="info-tip" data-tip="${esc(tip)}" tabindex="0" aria-label="About ${esc(text)}">${svg("info")}</span>` : ""}</div>`;

export const devLink = () =>
  `<button class="tb-btn tb-text" id="open-developer" title="Developer view" aria-label="Developer view">${svg("code")}Developer</button>`;

// A spinner beside the word, which fades in only if the wait is noticeable.
export const loadingRow = (cls = "loading") => `<div class="${cls}" role="status"><span class="spinner" aria-hidden="true"></span>Loading…</div>`;

export const loadingPage = {render: () => ({bar: toolbar("Assist Improver"), page: `<div class="page">${loadingRow()}</div>`})};

export const errorPage = {render: (p) => ({bar: toolbar("Assist Improver", true),
                                           page: `<div class="page"><div class="conflict">${esc(p.error)}</div></div>`})};

export function runTitle(run) {
  // Which run this is: one step, what was sent in (on-demand's own run), the conversations picked on the home page, or
  // a full run on all of them (with how many, once it has taken them in).
  const P = TEXT.progress, n = run.progress?.analyze_total || 0;
  return run.only ? fill(P.step, {step: GATE_NAMES[run.only] || run.only}) : run.trigger === "filed" ? P.sent
    : run.picked ? P.picked : n > 1 ? fill(P.all, {n}) : n ? P.one : P.working;
}

export function progress(run, cancelling) {
  // A run in progress: which it is and where, with live counts; it can be left and come back to.
  const p = run.progress || {};
  const at = STAGES.findIndex(([key]) => key === run.stage);
  const detail = {
    analyze: p.analyze_total != null ? `Analyzing conversations: ${p.analyzed || 0} of ${p.analyze_total}` +
      (p.skipped_as_smooth ? ` (${p.skipped_as_smooth} went smoothly, skipped).` : ".") : "Finding conversations to analyze…",
    propose: `Proposing fixes from ${plural(p.sessions || 0, "conversation")}, with a reviewer checking each…`,
    test: `Testing ${plural(p.hypotheses || 0, "fix")} in a sandboxed copy of your home…`,
    write: `Writing up ${plural(p.writing || 0, "improvement")}…`}[run.stage];
  return `<div class="run"><div class="run-head"><div class="what"><div class="title">${esc(runTitle(run))}</div>
      <div class="sub">${esc(detail)} You can leave this page; it keeps going.</div></div>
      <button class="btn ghost" id="cancel-run" ${cancelling ? "disabled" : ""}>${cancelling ? "Cancelling…" : "Cancel"}</button></div>
      <div class="stepper">${STAGES.map(([key, label], n) =>
        `<div class="step ${n < at ? "done" : n === at ? "now" : ""}"><div class="bar"></div>${label}</div>`).join("")}</div></div>`;
}

export function lastRun(run, count = true) {
  // How the last run went and when, with what it found (left out with count false: home, once all of it is reviewed).
  if (!run) return "";
  const p = run.progress || {};
  const what = {done: "Last run", failed: "Last run stopped early", cancelled: "Last run cancelled",
                interrupted: "Last run was interrupted"}[run.status] || "Last run";
  const found = count && p.new_improvements != null ? ` · ${plural(p.new_improvements, "new improvement")}` : "";
  return `${what} ${ago(run.finished || run.started)}${found}`;
}

export function failure(p, run) {
  // What went wrong in the last run, on the home page only when the household must act: a step whose model
  // can't run names the step and opens the settings. What the next run tries again by itself stays in the
  // settings' History, with its details.
  if (run?.status !== "failed" || !/ can't run: /.test(run.error || "")) return "";
  return `<div class="notice">${esc(run.error)} <button class="link go-settings">${esc(TEXT.failure.settings)}</button></div>`;
}

export function details(p, key, lines) {
  // A run's technical details, folded away for a bug report (and kept open, once opened, while it's shown).
  return `<details class="h-details" data-key="${esc(key)}" ${p.open.has(key) ? "open" : ""}><summary>${esc(TEXT.failure.details)}</summary><code>${
    lines.map(esc).join("\n")}</code></details>`;
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

// How many conversations a full run (Settings' Run now) works on: every one not analyzed yet, the new ones and those
// an earlier run took in, less those waiting at a blocked step (new ones go in whatever is blocked). Its button and
// the confirmation's title say it.
export const allConversations = (b, blocked) =>
  (b.requests || 0) + ["triage", "analyze"].reduce((n, gate) => n + (blocked.has(gate) ? 0 : b[gate] || 0), 0);

export const runLabel = (n) => n > 1 ? fill(TEXT.runAll, {n}) : n ? TEXT.runOne : TEXT.runNow;

const count = (n, text) => fill(text, {n, s: n === 1 ? "" : "s"});

export function waiting(b, blocked) {
  // What waits past the start, step by step, as items of a sentence ("2 conversations to analyze"), leaving out what
  // waits at a blocked step: the confirmation's sentence, and the home page's Run bar. The revisions asked for come
  // first; the proposer's count holds them too, so its findings are named only when there's more to propose from.
  const C = TEXT.confirm;
  const order = [["revisions", "propose"], ["triage", "triage"], ["analyze", "analyze"], ["propose", "propose"],
                 ["test", "test"], ["write", "write"]];
  const n = (key) => key === "propose" ? (b.propose || 0) - (b.revisions || 0) : b[key] || 0;
  return order.filter(([key, gate]) => n(key) > 0 && !blocked.has(gate))
    .map(([key]) => key === "propose" ? C.items.propose : count(b[key], C.items[key]));
}

// A token count to two significant figures, with thousands separators: 123,456 is "120,000".
const rough = (n) => {
  const step = 10 ** Math.max(0, Math.floor(Math.log10(n)) - 1);
  return (Math.round(n / step) * step).toLocaleString("en-US");
};

export function cost(p, preview, b, blocked) {
  // The cost line; when this run analyses conversations and past runs say what one takes, a rough estimate under it,
  // one line per model: analysing this run's own count of them (all, those picked and waiting, or those waiting at
  // Analyze for that step alone) on the model Analyze runs on now, then the quick check on its own, for those the run
  // takes in or finds waiting at Triage, less those sent in past it (straight: with a note, from Assist, or again
  // after an analysis); one step skips it. Both steps on one model: one line. With Triage blocked, the new ones that
  // need the quick check wait there, so only those sent in past it are analysed.
  const C = TEXT.confirm, S = C.spend, spend = preview.spend || {}, a = spend.analyze, q = spend.triage, only = p.only;
  const held = blocked.has("triage") ? Math.max(0, (b.requests || 0) - (b.straight || 0)) : 0;
  const n = only ? (only === "analyze" ? b.analyze || 0 : 0) : blocked.has("analyze") ? 0 : allConversations(b, blocked) - held;
  if (!a || !n) return `<p class="cost">${esc(C.cost)}</p>`;
  const checked = Math.max(0, (b.requests || 0) - (b.straight || 0)) + (b.triage || 0);
  const quick = !only && q && !blocked.has("triage") ? q.tokens * checked : 0;
  const one = quick && q.model === a.model, s = n === 1 ? "" : "s";
  const lines = [fill(one ? S.both : S.analyze, {tokens: rough(a.tokens * n + (one ? quick : 0)), model: a.model, n, s}),
                 ...(quick && !one ? [fill(S.quick, {tokens: rough(quick), model: q.model})] : [])];
  return `<div class="cost c-cost"><p>${esc(S.lead)}</p><ul class="c-spend">${lines.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>
      <p>${esc(S.more)}</p></div>`;
}

const WHICH = 3, WORDS = 40;  // Conversations a confirmation names, and how much of what was said in each.
// Cut at a word: one more character tells whether the last word ended there.
const firstWords = (said) => said.length <= WORDS ? said : `${said.slice(0, WORDS + 1).replace(/\s+\S*$/, "")}…`;

export function confirm(p, preview) {
  // What helps decide at a glance: the title says how many conversations the run takes; under it, which (up to
  // three by their first words, and how many more), any other work it does ("Also: …"), the warnings when they
  // matter, and the cost. One step, and a run with no conversations to take, say what they do in a sentence instead.
  // In a modal, so the page stays put; a click outside or Esc is "Not now".
  const b = preview.backlog || {}, only = p.only, C = TEXT.confirm, blocked = new Set(only ? [] : preview.paused || []);
  const all = only ? 0 : allConversations(b, blocked);
  let lead;
  if (only) {
    const n = only === "triage" ? (b.requests || 0) + (b.triage || 0) : b[only] || 0;
    lead = `<p class="c-what">${esc(count(n, C.steps[n ? only : `${only}None`]))}</p>`;
  } else if (!all) {
    const items = waiting(b, blocked), shown = items.slice(0, 2).join(" and ");
    lead = `<p class="c-what">${esc(p.picking && items.length ? fill(C.pickedRun, {items: items.length > 2 ? fill(C.andMore, {items: items.slice(0, 2).join(", ")}) : shown})
      : !items.length ? (p.force ? C.again : C.nothing)
      : items.length > 2 ? fill(C.more, {items: items.slice(0, 2).join(", ")})
      : fill(C.waiting, {items: shown, are: items.length === 1 && /^1 /.test(items[0]) ? "is" : "are"}))}</p>`;
  } else {
    // Which, adding up to the title's count; then what it does besides conversations, at the later steps.
    const names = (preview.which || []).slice(0, WHICH).map((said) => fill(C.quoted, {said: firstWords(said)}));
    const more = Math.max(0, all - names.length);
    const which = !names.length ? "" : more ? fill(C.which, {names: names.join(", "), n: more}) : listed(names);
    const also = waiting({...b, triage: 0, analyze: 0}, blocked);
    lead = (which ? `<p class="c-which">${esc(which)}</p>` : "")
      + (also.length ? `<p class="c-also">${esc(fill(C.also, {items: also.join(" · ")}))}</p>` : "");
  }
  const held = [...blocked].map((g) => GATE_NAMES[g]).filter(Boolean);
  const stages = only ? [only] : Object.keys(GATE_NAMES).filter((g) => !blocked.has(g));
  const broken = [...new Set(stages.flatMap((g) => (preview.blocked_models || {})[g] || []))];
  const modelsLine = broken.length ? fill(C.models, {steps: broken.join(" and "), its: broken.length === 1 ? C.its : C.their}) : "";
  const blockedLine = held.length ? fill(C.blocked, {steps: held.join(", "), is: held.length === 1 ? "is" : "are"}) : "";
  // How many conversations it runs on, as its button says: those not analyzed yet from the settings, those picked
  // (and waiting) from the home page.
  const title = only ? fill(C.step, {step: GATE_NAMES[only]}) : p.picking && all ? count(all, C.on)
    : all > 1 ? fill(C.all, {n: all}) : all ? C.one : C.title;
  return `<div class="modal-shade" id="confirm-shade"></div><div class="modal confirm-modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="modal-head">${svg("play")}<b>${esc(title)}</b><span class="kn-count"></span>
        <button class="modal-close" id="cancel-confirm" aria-label="${esc(C.notNow)}">&#10005;</button></div>
      <div class="modal-body confirm">${lead}
      ${blockedLine ? `<p class="cost">${esc(blockedLine)}</p>` : ""}${modelsLine ? `<p class="c-warn">&#9888; ${esc(modelsLine)}</p>` : ""}
      ${cost(p, preview, b, blocked)}
      <div class="actions"><button class="btn ghost" id="cancel-confirm-2">${esc(C.notNow)}</button>
      <button class="btn" id="start-run" ${modelsLine || p.starting ? "disabled" : ""}>${esc(p.starting ? "Starting…" : C.run)}</button></div></div></div>`;
}

async function startRun(p) {
  p.starting = true;
  p.render();
  try {
    // A Run on the home page takes the selected conversations that can still be sent in, and only conversations sent
    // in; those leave the selection.
    const runs = p.picking ? p.cv?.runnable() || [] : [];
    await p.ws("run/start", {confirmed: true, force: !!p.force, ...(p.only ? {only: p.only} : {}),
                             ...(p.picking ? {picked: true, conversations: runs} : {})});
    for (const id of runs) { p.cv.picked.delete(id); p.cv.byHand.delete(id); }
    p.host.scrollIntoView?.({block: "start", behavior: "smooth"});  // Up to the run's progress.
  } catch (err) {
    alert(err.message || String(err));
  }
  p.starting = false;
  p.confirming = false;
  p.picking = false;
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

const ask = (p, force) => { p.confirming = true; p.force = force; p.only = null; p.picking = false; p.render(); };

export const actions = {
  click: [
    ["#back", (p) => p.go(null)],
    ["#open-settings, .go-settings", (p) => p.nav(SETTINGS_URL)],
    ["#open-developer", (p) => p.nav(devUrl("find"))],
    ["#run-now", (p) => ask(p, false)],
    ["#run-anyway", (p) => ask(p, true)],
    ["#cancel-confirm, #cancel-confirm-2, #confirm-shade", (p) => { p.confirming = false; p.picking = false; p.render(); }],
    ["#start-run", (p) => startRun(p)],
    ["#cancel-run", (p) => cancelRun(p)],
    ["#menu-backdrop", (p) => { p.menuOpen = false; p.runMenu = false; p.render(); }],
  ],
};
