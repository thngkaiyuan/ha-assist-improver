// A review: what you get on top, then the evidence (each test's before and after, open), then why it's safe; what went
// wrong and what changes folded below, the changes as a code review shows them. One decision at a time in the footer;
// after a decision the footer says what happened and offers the way on, never another decision where the last one was.

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
  // What a review shows open at first: each test's before and after; and, once what changes is opened, each file short
  // enough to read at a glance.
  return [...r.tests.flatMap((t, n) => t.before?.length || t.after?.length ? [`test${n}`] : []),
          ...r.changes.flatMap((c, n) => c.rows.length && c.rows.length <= SHORT ? [`file${n}`] : [])];
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

function stillOpen(open) {
  // What the household pointed out that this test doesn't show fixed, in their words, under the replay: each with a
  // bubble, the reply that still did it and why, or that this test couldn't tell.
  const R = TEXT.review;
  return open.length ? `<div class="rp-open"><div class="rp-open-head">${esc(R.open.title)}</div>${open.map((o) =>
    `<div class="line open" tabindex="0" data-tip="${esc(o.set_aside ? R.open.setAside : fill(R.open.reply, {n: o.reply, why: o.why}))
    }"><span class="sign">!</span><span>${esc(o.quote)}</span></div>`).join("")}</div>` : "";
}

function replay(t, open) {
  // What a test replayed: the conversation as it went (the turns the test covers), then with the change, read as the
  // home page reads a conversation; side by side where there's room. What's still open closes the replay.
  const R = TEXT.review, before = t.before || [], after = t.after || [];
  const side = (cls, title, turns, empty, end = "") => `<div class="rp-side ${cls}"><div class="rp-head">${esc(title)}</div>${
    turns.length ? dialog(turns) : `<p class="rp-empty">${esc(empty)}</p>`}${end}</div>`;
  // Without its recording (tidied away since), only the replay.
  return `<div class="rp${before.length ? " two" : ""}">${before.length ? side("rp-before", R.before, before) : ""}${
    side("rp-after", R.after, after, R.stopped, stillOpen(open))}</div>`;
}

function test(p, t, n, open) {
  // A test's row: what it tested and what it showed; opened (at first), its before and after.
  const R = TEXT.review, more = t.more_words;
  const row = `<span class="mark">${t.improved ? "&#10003;" : "&#8211;"}</span><span>${esc(t.summary)}</span>
    <span class="result">${esc(t.result || (t.improved ? "improved" : "not improved"))}</span>${more
      ? `<span class="result more-words" title="${esc(fill(R.moreWordsTip, more))}">${esc(R.moreWords)}</span>` : ""}${t.open
      ? `<span class="result still-open" title="${esc(t.happened ? R.open.chipTip : R.open.setAside)}">${
        esc(fill(R.open.chip, {n: t.open}))}</span>` : ""}`;
  const key = `test${n}`;
  return `<li class="${t.improved ? "ok" : "no"}">${t.before?.length || t.after?.length
    ? `<details data-key="${key}" ${p.open.has(key) ? "open" : ""}><summary class="row">${row}</summary>${replay(t, open)}</details>`
    : `<div class="row">${row}</div>${stillOpen(open)}`}</li>`;
}

function reasonsFor(r, decision) {
  // The reasons a decision can be given with (Decline: not an issue), those the panel has words for.
  return (r.choices.find((c) => c.value === decision)?.reasons || []).filter((key) => TEXT.review.reasons[key]);
}

function forward(r) {
  // The way on after a decision: the next review waiting, else back to the list (all caught up, unless this one still
  // waits for the household).
  const waits = ["awaiting_review", "needs_attention"].includes(r.status) && !r.revising;
  return r.next_pending ? `<button class="btn" id="next" data-id="${esc(r.next_pending.id)}">Next: ${esc(r.next_pending.title)} &#8594;</button>`
    : `<button class="link" id="back-list">${esc(waits ? TEXT.review.back : TEXT.review.caughtUp)}</button>`;
}

function footer(p, r) {
  // Right after a decision, the footer says what happened and offers the way on, where the decision's button was:
  // never another decision there. What else it offers is a link beside what happened: Undo after an accept, Decide
  // again once undone, Send it back to resolve once an accept met a conflict. While its revision is worked on, the
  // same, opened afresh too: the way on, and Decide on this version, the household's way out meanwhile. The page above
  // stays where it was.
  const R = TEXT.review, ok = p.result ? p.resultOk : r.recent_outcome_ok;
  // An accept that met a conflict: why is under "Can't apply it now:" above, so here only that nothing changed.
  const restated = !ok && !p.failed && r.status === "conflict" && r.conflicts.length > 0;
  const said = (p.result || r.recent_outcome) && (restated ? R.nothingChanged : p.result || r.recent_outcome);
  const has = (value) => r.choices.some((c) => c.value === value), off = p.busy ? "disabled" : "";
  const link = has("undo") ? `<button class="link" id="undo-link" ${off}>${esc(R.undo)}</button>`
    : has("resolve") ? `<button class="link" id="resolve-link" ${off}>${esc(r.choices[0].label)}</button>`
    : has("accept") ? `<button class="link" id="again-link">${esc(r.revising ? R.thisVersion : R.again)}</button>` : "";
  const message = (withLink) => said && `<div class="done-msg ${ok ? "ok" : "bad"}"><span class="mark">${ok ? "&#10003;" : "!"}</span><span>${
    esc(said)}${withLink && link ? ` ${link}` : ""}</span></div>`;
  // A decision that couldn't be made and is still open keeps its bar, with why above it and the note as typed.
  if ((said || r.revising) && !p.decideAgain && !(p.failed && has(p.failed))) {
    return `<div class="footer"><div class="footer-inner done-row">${message(true) || `<div class="done-msg">${link}</div>`}${
      forward(r)}</div></div>`;
  }
  if (!r.choices.length) return "";  // Decided and closed: nothing to decide.
  const decision = p.decision || r.choices[0].value;
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
  // While a decision is on its way the button keeps its words and size; the arrow turns into a spinner. One choice
  // alone (Undo changes, Send it back to resolve) has no arrow.
  const busy = p.busy ? "disabled" : "", one = r.choices.length === 1;
  return `${p.menuOpen ? `<div class="backdrop" id="menu-backdrop"></div>` : ""}<div class="footer"><div class="footer-inner">
      ${said ? message(false) : ""}${reasons}${needsNote ? `<textarea id="note" placeholder="${esc(prompt)}">${esc(p.note || "")}</textarea>` : ""}
      <div class="split${one ? " one" : ""}" ${p.busy ? 'aria-busy="true"' : ""}><button class="main" id="decide" ${busy}>${esc(label)}</button>${
        one ? "" : `<button class="arrow" id="arrow" aria-label="Other decisions" ${busy}><span class="caret">&#9662;</span>${
          p.busy ? `<span class="spinner" aria-hidden="true"></span>` : ""}</button>`}${menu}</div>
    </div></div>`;
}

export function render(p) {
  const r = p.review, R = TEXT.review;
  const outcome = common.gains(r);
  const files = r.changes.map((c, n) => `<div class="file">${section(p, `file${n}`,
    `<span class="name">${esc(c.name)}</span><span class="kind ${esc(c.kind)}">${esc(c.kind)}</span>
     ${c.added ? `<span class="count-add">+${c.added}</span>` : ""}${c.removed ? `<span class="count-del">&#8722;${c.removed}</span>` : ""}
     ${bar(c.added, c.removed)}`, diff(c.rows, c), true)}</div>`).join("");
  // What the household pointed out that isn't shown fixed goes with the test that counts it (the first, without one).
  const open = r.still_open, counted = Math.max(0, r.tests.findIndex((t) => t.open));
  const tests = r.tests.map((t, n) => test(p, t, n, n === counted ? open : [])).join("");
  // On top, open: what you get (the editor's words), the evidence (each test's before and after), and why it's safe
  // (stated by code). Below, folded: what went wrong, then what changes.
  const summary = `<div class="card summary">
    ${r.benefit ? `<div class="label">${esc(R.get)}</div><p class="benefit">${esc(r.benefit)}</p>` : ""}
    <div class="label">${esc(R.tested)}</div><ul class="tests">${tests}</ul>
    ${r.wordier ? `<p class="lead more-words-line">${esc(fill(R.moreWordsLine, {n: r.wordier}))}</p>` : ""}
    <div class="label">${esc(R.safe)}</div><ul class="safe">${r.safety.map((f) =>
      `<li><span class="mark">&#10003;</span><span>${esc(f)}</span></li>`).join("")}</ul></div>`;
  const how = `<div class="details-body"><p>${esc(r.cause)}</p>
    ${r.physical ? `<div class="note"><span>&#9432;</span><span>Physical positions aren't simulated in the sandbox: a door's motion, for
    example, is taken as done when its command succeeds.</span></div>` : ""}</div>`;
  const sections = section(p, "details", esc(R.wrong), how)
    + section(p, "changes", esc(fill(R.changes, {n: r.changes.length})), `<div class="files">${files}</div>`);
  const conflicts = r.conflicts.length ? `<div class="conflict"><b>Can't apply it now:</b><br>${r.conflicts.map((c) =>
    `${esc(c.what)}: ${esc(c.why)}`).join("<br>")}</div>` : "";
  // Being revised: the chip says so, and the note it's being revised for is under the headline.
  const status = r.revising ? "revising" : r.status;
  return {
    bar: common.toolbar("Improvements", true),
    page: `<div class="page">
    <h1>${esc(r.title)} <span class="chip ${esc(status)}">${esc(STATUS[status] || status)}</span></h1>
    <div class="outcome">${esc(outcome)}</div>
    ${r.revises ? `<div class="revises">${r.revises.conflict ? `Revised after a conflict on “${esc(r.revises.title)}”`
      : `Revised after your note on “${esc(r.revises.title)}”${r.revises.note ? `: “${esc(r.revises.note)}”` : ""}`}</div>` : ""}
    ${r.unrevised ? `<div class="revises unrevised">${esc(fill(r.unrevised.why ? R.unrevised : R.unrevisedPlain,
      r.unrevised))}</div>` : ""}
    ${r.revising?.note ? `<div class="revises">${esc(fill(R.revisingNote, r.revising))}</div>` : ""}
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
    Object.assign(p, {result: res.result, resultOk: res.ok, decision: null, note: "", reason: null, decideAgain: false,
                      failed: null});
  } catch (err) {
    // Not made: if it's still open once the review is read again, its bar stays (footer); if not (decided elsewhere
    // meanwhile), what's there now never sits where its button was.
    Object.assign(p, {result: err.message || String(err), resultOk: false, decideAgain: false, failed: decision});
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
    ["#resolve-link", (p) => decide(p, "resolve")],
    ["#again-link", (p) => { p.decideAgain = true; p.render(); }],
    ["#next", (p, el) => p.go(el.dataset.id)],
    ["#back-list", (p) => p.go(null)],
  ],
  input: [["#note", (p, el) => { p.note = el.value; }]],
  change: [["[data-reason]", (p, el) => { p.reason = el.checked ? el.dataset.reason : null; p.render(); }]],
};
