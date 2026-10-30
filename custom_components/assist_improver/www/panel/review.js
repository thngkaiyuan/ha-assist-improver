// A review: the improvement shown the way a code review shows a change. A summary on top, the details folded, and one
// decision at a time in the footer; after a decision the footer says what happened, with Undo and the next review.

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, fill}, {STATUS, TEXT}, common, {dialog}] = await Promise.all([
  import(`./format.js${v}`), import(`./text.js${v}`), import(`./common.js${v}`), import(`./conversations.js${v}`)]);

// Home Assistant makes the panel's element again whenever its sidebar title (the count) changes, as it does after a
// decision. What was open and where the page was scrolled carry over to the new element, so nothing moves.
export const KEPT = {open: new Set(), page: null, scroll: null};

const NO_NOTE = ["accept", "undo", "resolve"];

const section = (p, key, title, inner, rich = false) =>
  // rich: the title is already the summary's parts (name, kind, counts), laid out side by side.
  `<details data-key="${key}" ${p.open.has(key) ? "open" : ""}><summary>${rich ? title : `<span class="name">${title}</span>`}</summary>${inner}</details>`;

const SHORT = 40;  // Diff rows a file can have and still show open at first.

export function opened(r) {
  // What a review shows open at first: what changes, and each file short enough to read at a glance.
  return ["changes", ...r.changes.flatMap((c, n) => c.rows.length && c.rows.length <= SHORT ? [`file${n}`] : [])];
}

function bar(added, removed) {
  const total = added + removed;
  if (!total) return "";
  const a = Math.round((added / total) * 5);
  return `<span class="bar">${Array.from({length: 5}, (_, n) => `<i class="${n < a ? "a" : "d"}"></i>`).join("")}</span>`;
}

function diff(rows, change) {
  if (!rows.length) return `<div class="empty">${change.proposed ? "As proposed; not applied." : "Already in place; nothing changes."}</div>`;
  const cls = {"+": "add", "-": "del", " ": "", gap: "gap"};
  return `<table class="diff">${rows.map((r) => r.sign === "gap"
    ? `<tr class="gap"><td colspan="4">&#8943;</td></tr>`
    : `<tr class="${cls[r.sign]}"><td class="num">${r.old ?? ""}</td><td class="num">${r.new ?? ""}</td>
       <td class="sign">${r.sign === " " ? "" : r.sign === "-" ? "&#8722;" : "+"}</td><td class="text">${esc(r.text)}</td></tr>`).join("")}</table>`;
}

function replay(t) {
  // What a test replayed: the conversation with the change, then as it went (the turns the test covers), read as the
  // home page reads a conversation; side by side where there's room, as it went on the left.
  const R = TEXT.review, before = t.before || [], after = t.after || [];
  const side = (cls, title, turns, empty) => `<div class="rp-side ${cls}"><div class="rp-head">${esc(title)}</div>${
    turns.length ? dialog(turns) : `<p class="rp-empty">${esc(empty)}</p>`}</div>`;
  // Without its recording (tidied away since), only the replay.
  return `<div class="rp${before.length ? " two" : ""}">${side("rp-after", R.after, after, R.stopped)}${
    before.length ? side("rp-before", R.before, before) : ""}</div>`;
}

function test(p, t, n) {
  // A test's row: what it tested and what it showed; opened, its replay.
  const R = TEXT.review, more = t.more_words;
  const row = `<span class="mark">${t.improved ? "&#10003;" : "&#8211;"}</span><span>${esc(t.summary)}</span>
    <span class="result">${esc(t.result || (t.improved ? "improved" : "not improved"))}</span>${more
      ? `<span class="result more-words" title="${esc(fill(R.moreWordsTip, more))}">${esc(R.moreWords)}</span>` : ""}${t.open
      ? `<span class="result still-open" title="${esc(t.happened ? R.open.chipTip : R.open.setAside)}">${
        esc(fill(R.open.chip, {n: t.open}))}</span>` : ""}`;
  const key = `test${n}`;
  return `<li class="${t.improved ? "ok" : "no"}">${t.before?.length || t.after?.length
    ? `<details data-key="${key}" ${p.open.has(key) ? "open" : ""}><summary class="row">${row}</summary>${replay(t)}</details>`
    : `<div class="row">${row}</div>`}</li>`;
}

function reasonsFor(r, decision) {
  // The reasons a decision can be given with (Decline: not an issue), those the panel has words for.
  return (r.choices.find((c) => c.value === decision)?.reasons || []).filter((key) => TEXT.review.reasons[key]);
}

function footer(p, r) {
  // Right after a decision, the footer says what happened (with Undo) and offers the next review: the page above
  // stays where it was.
  const said = p.result || r.recent_outcome, ok = p.result ? p.resultOk : r.recent_outcome_ok;
  const done = said && `<div class="done-msg ${ok ? "ok" : "bad"}"><span class="mark">${ok ? "&#10003;" : "!"}</span><span>${
    esc(said)}${r.choices.some((c) => c.value === "undo") ? ` <button class="link" id="undo-link">Undo</button>` : ""}</span></div>`;
  // Undone: up for review again, so the decision is open right here, under what happened; an accept that met a
  // conflict asks here at once whether to send it back to resolve it.
  const again = said && ["awaiting_review", "conflict"].includes(r.status) && r.choices.length > 0;
  if (said && !again) {
    return `<div class="footer"><div class="footer-inner done-row">${done}
      ${r.next_pending ? `<button class="btn" id="next" data-id="${esc(r.next_pending.id)}">Next: ${esc(r.next_pending.title)} &#8594;</button>`
        : `<button class="link" id="back-list">All caught up · back to the list</button>`}</div></div>`;
  }
  if (!r.choices.length) return "";
  const decision = p.decision || r.choices[0].value, R = TEXT.review;
  const label = r.choices.find((c) => c.value === decision)?.label || decision;
  const needsNote = !NO_NOTE.includes(decision);
  // Each reason this decision can be given with, a box to tick (ticked, the note asks only for anything to add).
  const offered = reasonsFor(r, decision), reason = offered.includes(p.reason) ? p.reason : null;
  const reasons = offered.map((key) => `<label class="reason"><input type="checkbox" data-reason="${esc(key)}"
      ${key === reason ? "checked" : ""}><span><span class="reason-label">${esc(R.reasons[key].label)}</span>
      <span class="reason-hint">${esc(R.reasons[key].hint)}</span></span></label>`).join("");
  const prompt = reason ? R.reasons[reason].note : {revise: "What should change? (needed for the revision)",
                  decline: "Why not? (optional)"}[decision] || "Add a note (optional)";
  const menu = p.menuOpen ? `<div class="menu">${r.choices.map((c) =>
    `<div data-choice="${esc(c.value)}" class="${c.value === decision ? "on" : ""}">${esc(c.label)}</div>`).join("")}</div>` : "";
  return `${p.menuOpen ? `<div class="backdrop" id="menu-backdrop"></div>` : ""}<div class="footer"><div class="footer-inner">
      ${again ? done : ""}${reasons}${needsNote ? `<textarea id="note" placeholder="${esc(prompt)}">${esc(p.note || "")}</textarea>` : ""}
      <div class="split"><button class="main" id="decide" ${p.busy ? "disabled" : ""}>${esc(p.busy ? "Working…" : label)}</button>
      <button class="arrow" id="arrow" aria-label="Other decisions" ${p.busy ? "disabled" : ""}>&#9662;</button>${menu}</div>
    </div></div>`;
}

export function render(p) {
  const r = p.review;
  const R = TEXT.review, spared = r.spared || [], followed = r.followed || [], open = r.still_open || [];
  const outcome = common.gains({enabled: r.enabled.length, removed: r.removed.length, spared: spared.length,
                                followed: followed.length});
  const lines = (cls, sign, items) => `<div class="lines">${items.map((t) =>
    `<div class="line ${cls}"><span class="sign">${sign}</span><span>${esc(t)}</span></div>`).join("")}</div>`;
  const files = r.changes.map((c, n) => `<div class="file">${section(p, `file${n}`,
    `<span class="name">${esc(c.name)}</span><span class="kind ${esc(c.kind)}">${esc(c.kind)}</span>
     ${c.added ? `<span class="count-add">+${c.added}</span>` : ""}${c.removed ? `<span class="count-del">&#8722;${c.removed}</span>` : ""}
     ${bar(c.added, c.removed)}`, diff(c.rows, c), true)}</div>`).join("");
  const tests = r.tests.map((t, n) => test(p, t, n)).join("");
  // The summary, always open at the top: what you get (the editor's words), why it's safe (stated by code), and
  // what was tested, each test opening to its replay. The technical cause is last, for whoever wants it.
  const summary = `<div class="card summary">
    ${r.benefit ? `<div class="label">What you get</div><p class="benefit">${esc(r.benefit)}</p>` : ""}
    <div class="label">Why it's safe</div><ul class="safe">${r.safety.map((f) =>
      `<li><span class="mark">&#10003;</span><span>${esc(f)}</span></li>`).join("")}</ul>
    <div class="label">Tested on</div><ul class="tests">${tests}</ul>
    ${r.wordier ? `<p class="lead more-words-line">${esc(fill(TEXT.review.moreWordsLine, {n: r.wordier}))}</p>` : ""}</div>`;
  const how = `<div class="details-body"><p>${esc(r.cause)}</p>
    ${r.physical ? `<div class="note"><span>&#9432;</span><span>Physical positions aren't simulated in the sandbox: a door's motion, for
    example, is taken as done when its command succeeds.</span></div>` : ""}</div>`;
  const sections = [
    r.enabled.length && section(p, "enabled", `New things Assist can do (${r.enabled.length})`, lines("add", "+", r.enabled)),
    r.removed.length && section(p, "removed", `Turns no longer needed (${r.removed.length})`, lines("del", "−", r.removed)),
    spared.length && section(p, "spared", esc(fill(R.spared.title, {n: spared.length})), lines("add", "✓", spared)),
    followed.length && section(p, "followed", esc(fill(R.followed.title, {n: followed.length})), lines("add", "✓", followed)),
    // Each with a bubble: the reply that still did it and why, or that this test couldn't tell.
    open.length && section(p, "open", esc(fill(R.open.title, {n: open.length})), `<div class="lines">${open.map((o) =>
      `<div class="line open" tabindex="0" data-tip="${esc(o.set_aside ? R.open.setAside : fill(R.open.reply, {n: o.reply, why: o.why}))
      }"><span class="sign">!</span><span>${esc(o.quote)}</span></div>`).join("")}</div>`),
    section(p, "changes", `What changes (${r.changes.length})`, `<div class="files">${files}</div>`),
    section(p, "details", "What went wrong", how),
  ].filter(Boolean).join("");
  const conflicts = r.conflicts.length ? `<div class="conflict"><b>Can't apply it now:</b><br>${r.conflicts.map((c) =>
    `${esc(c.what)}: ${esc(c.why)}`).join("<br>")}</div>` : "";
  return {
    bar: common.toolbar("Improvements", true),
    page: `<div class="page">
    <h1>${esc(r.title)} <span class="chip ${esc(r.status)}">${esc(STATUS[r.status] || r.status)}</span></h1>
    <div class="outcome">${esc(outcome)}</div>
    ${r.revises ? `<div class="revises">${r.revises.conflict ? `Revised after a conflict on “${esc(r.revises.title)}”`
      : `Revised after your note on “${esc(r.revises.title)}”${r.revises.note ? `: “${esc(r.revises.note)}”` : ""}`}</div>` : ""}
    ${r.unrevised ? `<div class="revises unrevised">${esc(fill(r.unrevised.why ? R.unrevised : R.unrevisedPlain,
      r.unrevised))}</div>` : ""}
    ${conflicts}
    ${summary}<div class="card">${sections}</div></div>`,
    layer: footer(p, r),
  };
}

export function after(p) {
  // Back to where the page was (for a few seconds after a decision, while the panel may be made again).
  const kept = KEPT.scroll;
  if (!kept || kept.page !== KEPT.page || Date.now() > kept.until) return;
  requestAnimationFrame(() => { p.scroller().scrollTop = kept.top; });
}

async function decide(p, decision = p.decision || p.review.choices[0].value) {
  const r = p.review;
  KEPT.scroll = {page: r.id, top: p.scroller().scrollTop, until: Date.now() + 8000};
  p.busy = true;
  p.render();
  try {
    const reason = reasonsFor(r, decision).includes(p.reason) ? p.reason : null;  // Ticked for this decision.
    const res = decision === "resolve"  // Sent back to resolve its conflict: not a decision on it.
      ? {...await p.ws("conflict/resolve", {improvement_id: r.id}), ok: true}
      : await p.ws("decide", {improvement_id: r.id, decision, note: NO_NOTE.includes(decision) ? "" : (p.note || ""),
                              ...(reason ? {reason} : {})});
    Object.assign(p, {result: res.result, resultOk: res.ok, decision: null, note: "", reason: null});
  } catch (err) {
    Object.assign(p, {result: err.message || String(err), resultOk: false});
  }
  p.busy = false;
  await p.load();
}

export const actions = {
  click: [
    ["#arrow", (p) => { p.menuOpen = !p.menuOpen; p.render(); }],
    ["[data-choice]", (p, el) => {
      p.decision = el.dataset.choice; p.menuOpen = false; p.render();
      p.root.getElementById("note")?.focus();
    }],
    ["#decide", (p) => decide(p)],
    ["#undo-link", (p) => decide(p, "undo")],
    ["#next", (p, el) => p.go(el.dataset.id)],
    ["#back-list", (p) => p.go(null)],
  ],
  input: [["#note", (p, el) => { p.note = el.value; }]],
  change: [["[data-reason]", (p, el) => { p.reason = el.checked ? el.dataset.reason : null; p.render(); }]],
};
