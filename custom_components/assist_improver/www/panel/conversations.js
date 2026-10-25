// Your conversations, on the home page: find the ones that didn't go the way you wanted (by what was said, when, or
// the most back-and-forth first), read one, and send them in: pick some and Run, or send one in alone with a note
// (conversations.py). Found with assist_improver/conversations, read with .../conversation, sent with
// .../conversation/send; a Run on picked ones is assist_improver/run/start with them (common.js).

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, when, plural, fill}, {TEXT}, {svg}, common] = await Promise.all(
  ["format", "text", "icons", "common"].map((name) => import(`./${name}.js${v}`)));

const DAYS = [["", "all"], ["1", "today"], ["7", "week"], ["30", "month"]];
const ORDERS = ["newest", "effort", "oldest"];
const SEARCH_AFTER = 300;  // ms of no typing before a search.

const errText = (err) => err?.message || String(err);

export function state(p) {
  // order: the one chosen, or null: newest first, or the closest to a time searched for (closest).
  return p.cv || (p.cv = {query: "", days: "", order: null, rows: null, total: 0, waiting: [], counts: {},
                          opened: null, shown: null, note: "", sent: {}, picked: new Set(), error: null});
}

export async function load(p) {
  // The first page, as the filters stand; what was picked stays picked.
  await list(p, state(p), false, false);
}

async function list(p, cv, more, draw = true) {
  const seq = cv.seq = (cv.seq || 0) + 1;
  try {
    const found = await p.ws("conversations", {query: cv.query || undefined, days: cv.days ? Number(cv.days) : undefined,
                                               order: cv.order || undefined, offset: more ? cv.rows.length : 0});
    if (seq !== cv.seq) return;  // A newer search took over.
    Object.assign(cv, {rows: more ? [...cv.rows, ...found.conversations] : found.conversations, total: found.total,
                       waiting: found.waiting || [], counts: found.counts || {}, close: !!found.close, closest: !!found.closest,
                       onDemand: found.on_demand, error: null});
    for (const row of cv.rows) if (row.status !== "waiting") cv.picked.delete(row.id);  // Sent in or taken in since.
    // The opened one stands where its row now does (a run may have taken it in since it was opened).
    const row = cv.shown && !cv.shown.error && cv.rows.find((r) => r.id === cv.opened);
    if (row) Object.assign(cv.shown, {status: row.status, note: row.note});
  } catch (err) {
    if (seq !== cv.seq) return;
    cv.error = errText(err);
  }
  if (draw) p.render();
}

async function open(p, cv, id) {
  if (cv.opened === id) { Object.assign(cv, {opened: null, shown: null}); p.render(); return; }
  Object.assign(cv, {opened: id, shown: null, note: ""});
  p.render();
  scrollTo(p, id);
  try {
    const shown = await p.ws("conversation", {recording_id: id});
    if (cv.opened === id) cv.shown = shown;
  } catch (err) {
    if (cv.opened === id) cv.shown = {error: errText(err)};
  }
  p.render();
  if (cv.opened === id) scrollTo(p, id);  // Not if it was closed, or another opened, while it loaded.
}

function scrollTo(p, id) {
  // The opened conversation, brought into view (above the Run bar), as far as it fits.
  const row = [...p.root.querySelectorAll("[data-cs]")].find((el) => el.dataset.cs === id)?.closest(".cs-row");
  row?.scrollIntoView?.({block: "nearest", behavior: "smooth"});
}

async function send(p, cv, id) {
  const C = TEXT.conversations;
  cv.sending = id;
  p.render();
  try {
    const result = await p.ws("conversation/send", {recording_id: id, note: cv.note || ""});
    cv.sent[id] = {text: result.on_demand ? C.sentNow : C.sentLater};
    for (const row of cv.rows || []) if (row.id === id) Object.assign(row, {status: result.status, note: cv.note || null});
    if (cv.shown?.id === id) Object.assign(cv.shown, {status: result.status, note: cv.note || null});
    cv.picked.delete(id);
    cv.waiting = cv.waiting.filter((w) => w !== id);
    cv.counts = {...cv.counts, waiting: Math.max(0, (cv.counts.waiting || 1) - 1), sent: (cv.counts.sent || 0) + 1};
  } catch (err) {
    cv.sent[id] = {text: errText(err), error: true};
  }
  cv.sending = null;
  p.render();
}

// What a Run on the home page takes: the picked conversations, the ones sent in earlier, and what the improver has.

export function picking(p) {
  const cv = state(p);
  return {picked: cv.picked.size, sent: cv.counts.sent || 0};
}

export function runPreview(p, preview) {
  // The confirmation's numbers for a Run on picked conversations: what it takes in is the picked and sent-in ones,
  // said as one, as the Run bar does.
  const {picked, sent} = picking(p);
  return {...preview, backlog: {...(preview.backlog || {}), requests: picked + sent, sent: 0}};
}

// Drawing.

const sendable = (status) => status === "waiting" || status === "active";

function marked(text, query) {
  // The text, with the words searched for marked.
  const words = (query || "").toLowerCase().split(/\s+/).filter((w) => w.length > 1);
  if (!words.length) return esc(text);
  const pattern = new RegExp(words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "gi");
  let out = "", last = 0;
  for (const m of text.matchAll(pattern)) {
    out += `${esc(text.slice(last, m.index))}<mark>${esc(m[0])}</mark>`;
    last = m.index + m[0].length;
  }
  return out + esc(text.slice(last));
}

function badge(row) {
  const C = TEXT.conversations;
  return row.status === "waiting" ? "" : `<span class="chip cs-${row.status}">${esc(C.status[row.status])}</span>`;
}

function reading(p, cv, row) {
  const C = TEXT.conversations, shown = cv.shown;
  if (!shown) return `<div class="cs-read">${common.loadingRow()}</div>`;
  if (shown.error) return `<div class="cs-read"><div class="conflict">${esc(shown.error)}</div></div>`;
  const turns = shown.turns.map((t) => `<div class="cs-turn"><div class="cs-you"><b>${esc(C.you)}</b> ${marked(t.you, cv.query)}</div>
      ${t.assist ? `<div class="cs-assist"><b>${esc(C.assist)}</b> ${marked(t.assist, cv.query)}</div>` : ""}</div>`).join("");
  // What sending it in did: while it's sent in (not once the improver has taken it), or what went wrong.
  const sent = cv.sent[row.id] && (cv.sent[row.id].error || shown.status === "sent") ? cv.sent[row.id] : null;
  const action = sendable(shown.status)
    ? `<div class="cs-send"><input class="cs-note" id="cs-note" maxlength="500" placeholder="${esc(C.note)}" value="${esc(cv.note)}">
        <button class="btn ghost" id="cs-send" data-id="${esc(row.id)}" ${cv.sending ? "disabled" : ""}>${
          esc(cv.sending === row.id ? C.sending : C.send)}</button></div>`
    : shown.note ? `<div class="cs-sub">${esc(C.yourNote)} ${esc(shown.note)}</div>` : "";
  return `<div class="cs-read">${turns}${action}${sent ? `<div class="${sent.error ? "conflict" : "cs-done"}" role="status">${esc(sent.text)}</div>` : ""}</div>`;
}

function rowHtml(p, cv, row) {
  const C = TEXT.conversations, opened = cv.opened === row.id, picked = cv.picked.has(row.id);
  const pick = row.status === "waiting"
    ? `<label class="cs-pick" title="${esc(C.pick)}"><input type="checkbox" data-cs-pick="${esc(row.id)}" ${picked ? "checked" : ""}
        aria-label="${esc(fill(C.pickOne, {said: row.said || C.nothing}))}"></label>`
    : `<span class="cs-pick cs-pick-none"></span>`;
  const failed = row.failed ? ` · <span class="cs-failed">${esc(plural(row.failed, C.failedStep))}</span>` : "";
  return `<div class="cs-row ${opened ? "open" : ""} ${picked ? "picked" : ""}"><div class="cs-line">${pick}
      <button class="cs-head" data-cs="${esc(row.id)}" aria-expanded="${opened}">
      <span class="cs-text"><span class="cs-said">${row.said ? marked(row.said, cv.query) : esc(C.nothing)}</span>
        ${row.match ? `<span class="cs-reply cs-match">${marked(row.match, cv.query)}</span>`
          : row.reply ? `<span class="cs-reply">${marked(row.reply, cv.query)}</span>` : ""}
        <span class="cs-sub">${row.timed ? `<mark>${esc(when(row.started))}</mark>` : esc(when(row.started))} · ${esc(plural(row.turns || 0, "turn"))}${failed}</span></span>${badge(row)}</button></div>
      ${opened ? reading(p, cv, row) : ""}</div>`;
}

export function section(p) {
  // The conversations, with what finds them; "" until the first page is in.
  const C = TEXT.conversations, cv = state(p);
  if (cv.rows === null && !cv.error) return common.loadingRow();
  const seg = (items, on, attr) => `<div class="seg">${items.map(([value, key]) =>
    `<button data-${attr}="${value}" class="${value === on ? "on" : ""}">${esc(C[key])}</button>`).join("")}</div>`;
  const all = cv.waiting.length && cv.waiting.every((id) => cv.picked.has(id));
  const selectAll = cv.waiting.length > 1 ? `<label class="cs-all"><input type="checkbox" id="cs-all" ${all ? "checked" : ""}>
      ${esc(fill(C.selectAll, {n: cv.waiting.length}))}</label>` : "";
  const nothingYet = !cv.query && !cv.days && !cv.total;
  const rows = cv.error ? `<div class="conflict">${esc(cv.error)}</div>`
    : cv.rows.length ? (cv.close ? `<p class="cs-close">${esc(C.close)}</p>` : "") + `<div class="card cs-list">${cv.rows.map((row) => rowHtml(p, cv, row)).join("")}</div>`
      + (cv.rows.length < cv.total ? `<button class="link cs-more" id="cs-more">${esc(C.more)}</button>` : "")
    : `<p class="cs-empty">${esc(nothingYet ? C.empty : C.none)}</p>`;
  // How the last run went; what it found is left out once nothing is left to review.
  const run = p.run?.run, last = run && run.status !== "running" ? common.lastRun(run, !!p.list?.some((i) => i.pending)) : "";
  return `<section class="cs" aria-label="${esc(C.title)}"><h2 class="cs-title">${esc(C.title)}</h2>
      <p class="cs-lead">${esc(cv.onDemand ? C.leadOnDemand : C.lead)}</p>
      ${last ? `<p class="cs-last">${esc(last)}</p>` : ""}
      ${nothingYet ? "" : `<div class="cs-bar"><span class="cs-find"><input type="search" id="cs-search" placeholder="${esc(C.search)}"
        aria-label="${esc(C.search)}" value="${esc(cv.query)}">${cv.query ? `<button class="cs-clear-search" id="cs-search-clear"
        type="button" aria-label="${esc(C.clearSearch)}" title="${esc(C.clearSearch)}">&#10005;</button>` : ""}</span>
        ${seg(DAYS, cv.days, "days")}${seg(ORDERS.map((o) => [o, o]), cv.order || (cv.closest ? null : "newest"), "order")}</div>${selectAll}`}${rows}</section>`;
}

export function bar(p, preview, running) {
  // The Run bar: what a Run would take (picked, sent in earlier, revisions asked for, the improver's own backlog), with
  // Run. "" with nothing to run, or while a run goes.
  if (running || !preview) return "";
  const C = TEXT.conversations, cv = state(p), {picked, sent} = picking(p), b = preview.backlog || {};
  const backlog = (b.triage || 0) + (b.analyze || 0) + (b.test || 0) + (b.write || 0) + (b.propose ? 1 : 0);
  if (!picked && !sent && !backlog) return "";
  const revisions = b.revisions || 0;
  const parts = [picked + sent && fill(C.ready, {n: picked + sent}),
                 revisions && fill(C.revisions, {n: revisions, s: revisions === 1 ? "" : "s"})].filter(Boolean);
  const what = parts.length ? parts.join(" · ") : C.improverWaiting;
  return `<div class="footer cs-footer"><div class="footer-inner"><span class="cs-what">${esc(what)}</span>
      ${picked ? `<button class="link" id="cs-clear">${esc(C.clear)}</button>` : ""}
      <button class="btn play" id="cs-run">${svg("play")}${esc(C.run)}</button></div></div>`;
}

let typing = null;

function refilter(p, change) {
  Object.assign(p.cv, change, {opened: null, shown: null});
  list(p, p.cv, false);
}

export const actions = {
  click: [
    ["[data-cs]", (p, el) => open(p, p.cv, el.dataset.cs)],
    ["[data-days]", (p, el) => refilter(p, {days: el.dataset.days})],
    ["[data-order]", (p, el) => refilter(p, {order: el.dataset.order})],
    ["#cs-more", (p) => list(p, p.cv, true)],
    ["#cs-search-clear", (p) => {
      clearTimeout(typing);
      const box = p.root.getElementById("cs-search");
      if (box) box.value = "";  // Emptied at once: what's typed next is a new search.
      refilter(p, {query: ""});
      p.render();
      p.root.getElementById("cs-search")?.focus();
    }],
    ["#cs-send", (p, el) => send(p, p.cv, el.dataset.id)],
    ["#cs-clear", (p) => { p.cv.picked.clear(); p.render(); }],
    ["#cs-run", (p) => { p.picking = true; p.only = null; p.force = false; p.confirming = true; p.render(); }],
  ],
  change: [
    ["[data-cs-pick]", (p, el) => { el.checked ? p.cv.picked.add(el.dataset.csPick) : p.cv.picked.delete(el.dataset.csPick); p.render(); }],
    ["#cs-all", (p, el) => {
      for (const id of p.cv.waiting) el.checked ? p.cv.picked.add(id) : p.cv.picked.delete(id);
      p.render();
    }],
  ],
  input: [
    ["#cs-search", (p, el) => {
      p.cv.query = el.value;
      clearTimeout(typing);
      typing = setTimeout(() => refilter(p, {}), SEARCH_AFTER);
    }],
    ["#cs-note", (p, el) => { p.cv.note = el.value; }],
  ],
  keydown: [
    ["#cs-note", (p, el, ev) => {  // Enter sends it in, as the button does.
      const id = p.root.getElementById("cs-send")?.dataset.id;
      if (ev.key === "Enter" && id && !p.cv.sending) send(p, p.cv, id);
    }],
  ],
};
