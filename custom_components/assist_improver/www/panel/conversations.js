// Your conversations, on the home page: find the ones that didn't go the way you wanted (by what was said, when, or
// the most back-and-forth first), read one, and send them in: pick some and Run, or send one in alone with a note
// (conversations.py). Found with assist_improver/conversations, read with .../conversation, sent with
// .../conversation/send; a Run on picked ones is assist_improver/run/start with them (common.js). An opened one can
// also be archived, out of the list (.../conversation/hide: the improver keeps using it), or deleted from Assist
// Improver (.../conversation/delete). Where each stands, whether the list shows it, and whether it can be sent in or
// picked for a run come with each row, from its flow record (conversations.row): one in Your household or up for
// review is left out on its own until it stands otherwise; Unarchive keeps it in the list.

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, when, plural, fill}, {TEXT}, {svg}, common] = await Promise.all(
  ["format", "text", "icons", "common"].map((name) => import(`./${name}.js${v}`)));

const DAYS = [["", "all"], ["1", "today"], ["7", "week"], ["30", "month"]];
const ORDERS = ["newest", "effort", "oldest"];
const SEARCH_AFTER = 300;  // ms of no typing before a search.

const errText = (err) => err?.message || String(err);

export function state(p) {
  // order: the one chosen, or null: newest first, or the closest to a time searched for (closest).
  // showArchived: archived ones are listed too; archivedCount: how many the list leaves out (archived, or settled).
  // picked: the selection, any row, kept through searches, filters and Show more; byHand: those ticked one by one;
  // known: where each one listed so far stands ({status, stage, hidden, runnable, archivable}); every: the IDs of all
  // that match now. A row with stays: the opened one, kept in view though it no longer matches (list).
  return p.cv || (p.cv = {query: "", days: "", order: null, rows: null, total: 0,
                          opened: null, shown: null, note: "", sent: {}, picked: new Set(), byHand: new Set(),
                          known: new Map(), every: [], error: null, showArchived: false, archivedCount: 0,
                          only: null, byStatus: {}, statusMenu: false,
                          runnable() { return runnable(this); }});
}

function runnable(cv) {
  // The selected ones a Run takes in: those a run can take (runnable: new, or held); one in Your household (sent in
  // again, for a new round) only if ticked by hand (never by Select all: each is a paid run).
  return [...cv.picked].filter((id) => {
    const known = cv.known.get(id);
    return known?.runnable && (known.stage !== "Your household" || cv.byHand.has(id));
  });
}

function unpick(cv, ids) {
  for (const id of ids) {
    cv.picked.delete(id);
    cv.byHand.delete(id);
  }
}

export async function load(p) {
  // The first page, as the filters stand; what was picked stays picked.
  await list(p, state(p), false, false);
}

const served = (cv) => cv.rows.filter((r) => !r.stays).length;  // The rows Home Assistant listed: where the next page starts.

async function list(p, cv, more, draw = true) {
  const seq = cv.seq = (cv.seq || 0) + 1;
  // Where the selected ones and the opened one stand now, whatever the filters (as many as Home Assistant takes).
  const ids = [...new Set([...cv.picked, ...(cv.opened ? [cv.opened] : [])])].slice(0, 5000);
  try {
    const found = await p.ws("conversations", {query: cv.query || undefined, days: cv.days ? Number(cv.days) : undefined,
                                               order: cv.order || undefined, offset: more ? served(cv) : 0,
                                               hidden: cv.showArchived || undefined, statuses: cv.only ? [...cv.only] : undefined,
                                               ids: ids.length ? ids : undefined});
    if (seq !== cv.seq) return;  // A newer search took over.
    const stands = new Map((found.stands || []).map((r) => [r.id, r]));
    // A page appended: the opened one kept in view (stays) gives way to its own row when that page brings it.
    const fresh = new Set(found.conversations.map((r) => r.id));
    let rows = more ? [...cv.rows.filter((r) => !(r.stays && fresh.has(r.id))), ...found.conversations]
      : found.conversations;
    // The opened one stays in view while it's read, though it no longer matches (a run settled it, so it's archived,
    // or took it past the statuses chosen); it goes once it's closed and the list is read again.
    const was = !more && cv.opened && (cv.rows || []).find((r) => r.id === cv.opened);
    if (was && stands.has(was.id) && !rows.some((r) => r.id === was.id)) {
      const at = Math.min(cv.rows.indexOf(was), rows.length);
      rows = [...rows.slice(0, at), {...was, ...stands.get(was.id), stays: true}, ...rows.slice(at)];
    }
    Object.assign(cv, {rows, total: found.total, close: !!found.close, closest: !!found.closest,
                       archivedCount: found.hidden || 0, every: (found.every || []).map((r) => r.id),
                       byStatus: found.by_status || {}, onDemand: found.on_demand, error: null});
    if (!cv.archivedCount) cv.showArchived = false;  // None archived any more: nothing to show them by.
    for (const r of [...(found.every || []), ...stands.values()]) {
      cv.known.set(r.id, {status: r.status, stage: r.stage, hidden: r.hidden, runnable: r.runnable,
                          archivable: r.archivable});
    }
    // Selected ones archived since (a run settled them) are out of view: none of them stays picked.
    const inView = new Set(rows.map((r) => r.id));
    if (!cv.showArchived) unpick(cv, [...stands.values()].filter((r) => r.hidden && !inView.has(r.id)).map((r) => r.id));
    // The opened one stands where its row now does (a run may have taken it in since it was opened).
    const row = cv.shown && !cv.shown.error && cv.rows.find((r) => r.id === cv.opened);
    if (row) Object.assign(cv.shown, {status: row.status, note: row.note, hidden: row.hidden, sendable: row.sendable,
                                      archivable: row.archivable});
  } catch (err) {
    if (seq !== cv.seq) return;
    cv.error = errText(err);
  }
  if (draw) p.render();
}

async function open(p, cv, id) {
  Object.assign(cv, {deleting: null, tidyError: null});
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
    // One settled (archived on its own, opened while archived ones are shown) is back in the list once sent in again.
    const hidden = result.hidden ?? cv.known.get(id)?.hidden;
    if (cv.known.get(id)?.hidden && !hidden) {
      cv.archivedCount = Math.max(0, cv.archivedCount - 1);
      if (!cv.archivedCount) cv.showArchived = false;
    }
    // Sent again without a note, it keeps the one it had (DETAILS P8.9.4).
    const note = cv.note?.trim() || cv.shown?.note || null;
    for (const row of cv.rows || []) if (row.id === id) Object.assign(row, {status: result.status, note, hidden});
    cv.known.set(id, {...cv.known.get(id), status: result.status, hidden});
    if (cv.shown?.id === id) Object.assign(cv.shown, {status: result.status, note, hidden});
    cv.note = "";
    unpick(cv, [id]);
    // What a run would take now, for the Run bar and its confirmation; or the run it started, followed as it goes.
    p.setRun?.(await p.ws("run").catch(() => p.run));
    p.poll?.();
  } catch (err) {
    cv.sent[id] = {text: errText(err), error: true};
  }
  cv.sending = null;
  p.render();
}

function drop(cv, row) {
  // A conversation gone from the list (archived, or deleted): closed, and no longer picked.
  cv.rows = cv.rows.filter((r) => r.id !== row.id);
  if (!row.stays) cv.total = Math.max(0, cv.total - 1);
  cv.every = cv.every.filter((w) => w !== row.id);
  unpick(cv, [row.id]);
  if (cv.opened === row.id) Object.assign(cv, {opened: null, shown: null});
}

async function archive(p, cv, id, hidden) {
  // Out of the list (unless archived ones are shown), kept all the same for the improver; or back in the list. Where
  // it stands then (its chip, its ⋮) and what the list shows are read again, from its record.
  Object.assign(cv, {tidying: id, tidyError: null});
  p.render();
  try {
    await p.ws("conversation/hide", {recording_id: id, hidden});
    if (hidden && !cv.showArchived && cv.opened === id) Object.assign(cv, {opened: null, shown: null});  // Out of view.
    p.setRun?.(await p.ws("run").catch(() => p.run));  // What a run would take now: none archived.
  } catch (err) {
    cv.tidyError = {id, text: errText(err)};
  }
  cv.tidying = null;
  await list(p, cv, false);
}

async function remove(p, cv, id) {
  // Deleted for good, with what the improver derived from it: gone from the list, and from what a run takes.
  Object.assign(cv, {tidying: id, tidyError: null});
  p.render();
  try {
    await p.ws("conversation/delete", {recording_id: id});
    const row = cv.rows.find((r) => r.id === id);
    if (row) {
      if (row.hidden) cv.archivedCount = Math.max(0, cv.archivedCount - 1);
      drop(cv, row);
    }
    if (!cv.archivedCount) cv.showArchived = false;
    cv.deleting = null;
    p.setRun?.(await p.ws("run").catch(() => p.run));  // What a run would take now.
  } catch (err) {
    cv.tidyError = {id, text: errText(err)};
  }
  cv.tidying = null;
  p.render();
}

// What a Run on the home page takes: the picked conversations a run can take.

export function picking(p) {
  return {picked: runnable(state(p)).length};
}

const KEYS = {Triage: "triage", Analyze: "analyze", Propose: "propose", "Write up": "write"};  // flow.KEYS.

export function runPreview(p, preview) {
  // The numbers for a Run on picked conversations, as Pipeline.preview gives a full run's: it moves only the picked
  // ones it can (P0.7), nothing else, less those that would stop at a blocked step. The new ones it admits start at
  // Triage, or at Analyze past the quick check (one it found smooth before, sent in again; one sent in with a note);
  // the held ones it releases go on from the step holding them. which: what was said first in each of them, as listed
  // (the confirmation names a few; one not in the list is counted in its "and N more"). quick and analyses: what the
  // cost line's estimate counts.
  const cv = state(p), blocked = new Set(preview.paused || []);
  const rows = new Map((cv.rows || []).map((r) => [r.id, r])), moved = [], releases = {};
  let admits = 0, quick = 0, analyses = 0;
  for (const id of runnable(cv)) {
    const known = cv.known.get(id), held = KEYS[known?.stage];
    // One sent in again from Your household skips the quick check (an earlier round looked at it), as one with a note.
    const start = held || (known?.stage === "Your household" || (known?.status === "sent" && rows.get(id)?.note) ? "analyze" : "triage");
    if (blocked.has(start)) continue;
    moved.push(id);
    if (held) releases[held] = (releases[held] || 0) + 1; else admits += 1;
    quick += start === "triage" ? 1 : 0;
    analyses += ["triage", "analyze"].includes(start) ? 1 : 0;
  }
  return {...preview, admits, releases, quick: preview.spend?.triage ? quick : 0,
          analyses: blocked.has("analyze") ? 0 : analyses,
          which: moved.filter((id) => rows.has(id)).map((id) => rows.get(id).said || TEXT.conversations.nothing)};
}

// Drawing.

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
  // Where it stands; one up for review is its own button beside the row (reviewLink), so it can open that review.
  // Its tooltip says what comes next, or, held, why (flow.py's words).
  const C = TEXT.conversations, tips = {smooth: C.smoothTip, nofix: C.nofixTip, fine: C.fineTip, unclear: C.unclearTip,
                                        duplicate: C.duplicateTip};
  const text = row.held ? fill(C.heldTip, {why: row.held.why}) : tips[row.status];
  const tip = text ? ` title="${esc(text)}"` : "";
  return row.status === "review" && row.review ? ""
    : `<span class="chip cs-${row.status}"${tip}>${esc(C.status[row.status] || row.status)}</span>`;
}

const reviewLink = (row) => row.status === "review" && row.review
  ? `<button class="chip cs-review" data-cs-review="${esc(row.review)}">${esc(TEXT.conversations.status.review)}</button>` : "";

function paragraphs(text, query) {
  // Each paragraph on its own, line breaks kept, with the words searched for marked.
  return text.split(/\n\s*\n/).map((part) => `<p>${marked(part.trim(), query).replace(/\n/g, "<br>")}</p>`).join("");
}

export function dialog(turns, query = "") {
  // A conversation as Assist's own dialog shows it: what you said on the right, Assist's replies on the left, and
  // between them, any step that failed: what Assist tried, and Home Assistant's error. The review page reads a test's
  // conversation and its replay this way too.
  const C = TEXT.conversations;
  const message = (who, cls, text) => `<div class="cs-msg ${cls}"><div class="cs-who">${esc(who)}</div>
      <div class="cs-says">${paragraphs(text, query)}</div></div>`;
  const failed = (f) => `<div class="cs-step" role="note"><span class="cs-step-mark" aria-hidden="true">!</span>
      <span><span class="cs-step-what"><span class="cs-step-label">${esc(C.failedStepLabel)}</span> ${esc(f.step)}</span>
      <span class="cs-step-error">${esc(f.error)}</span></span></div>`;
  return turns.map((t) => `<div class="cs-turn">${message(C.you, "cs-you", t.you)}${(t.failed || []).map(failed).join("")}
      ${t.assist ? message(C.assist, "cs-assist", t.assist) : ""}</div>`).join("");
}

function reading(p, cv, row) {
  // The conversation opened, to read and send in.
  const C = TEXT.conversations, shown = cv.shown;
  if (!shown) return `<div class="cs-read">${common.loadingRow()}</div>`;
  if (shown.error) return `<div class="cs-read"><div class="conflict">${esc(shown.error)}</div></div>`;
  const turns = dialog(shown.turns, cv.query);
  // What sending it in did: while it's sent in (not once the improver has taken it), or what went wrong.
  const sent = cv.sent[row.id] && (cv.sent[row.id].error || shown.status === "sent") ? cv.sent[row.id] : null;
  // Sending it in starts a run for it (on demand), so it looks like Run: the note and the button stay in view while
  // it's read, unless it was just sent in. Archive and Delete wait behind the ⋮ beside it, so the row stays calm.
  const send = shown.sendable && !(sent && !sent.error)
    ? `<div class="cs-send"><input class="cs-note" id="cs-note" maxlength="500" placeholder="${esc(C.note)}" value="${esc(cv.note)}">
        <button class="btn play" id="cs-send" data-id="${esc(row.id)}" ${cv.sending ? "disabled" : ""}>${svg("send")}${
          esc(cv.sending === row.id ? C.sending : C.send)}</button>${dots(cv, row, shown)}</div>` : "";
  const note = shown.note ? `<div class="cs-sub">${esc(C.yourNote)} ${esc(shown.note)}</div>` : "";
  const error = cv.tidyError?.id === row.id ? `<div class="conflict" role="status">${esc(cv.tidyError.text)}</div>` : "";
  const alone = !send && cv.deleting !== row.id ? `<div class="cs-tidy">${dots(cv, row, shown)}</div>` : "";
  return `<div class="cs-read">${turns}${note}${send}${alone}${confirm(cv, row)}${
    sent ? `<div class="${sent.error ? "conflict" : "cs-done"}" role="status">${esc(sent.text)}</div>` : ""}${error}</div>`;
}

function dots(cv, row, shown) {
  // The ⋮ beside Send: Archive (it stays kept for the improver), or Delete from Assist Improver, after asking. Not
  // Archive while its improvement waits for the household (archivable): deciding it is the way out.
  const P = TEXT.privacy, busy = cv.tidying ? "disabled" : "", id = esc(row.id), open = cv.menu === row.id;
  const menu = open ? `<div class="backdrop" id="cs-menu-close"></div><div class="menu cs-menu" role="menu">${shown.hidden
      ? `<button role="menuitem" id="cs-unarchive" data-id="${id}" ${busy}>${svg("unarchive")}${esc(P.unarchive)}</button>`
      : shown.archivable ? `<button role="menuitem" id="cs-archive" data-id="${id}" ${busy}>${svg("archive")}${esc(P.archive)}</button>` : ""}
      <button role="menuitem" class="danger" id="cs-delete-ask" data-id="${id}" ${busy}>${svg("trash")}${esc(P.delete)}</button></div>` : "";
  return `<span class="cs-actions"><button class="cs-dots" id="cs-actions" aria-haspopup="menu" aria-expanded="${open}"
      aria-label="${esc(P.more)}" title="${esc(P.more)}">${svg("dots")}</button>${menu}</span>`;
}

function confirm(cv, row) {
  // Delete from Assist Improver, asking first.
  const P = TEXT.privacy, busy = cv.tidying ? "disabled" : "", id = esc(row.id);
  if (cv.deleting !== row.id) return "";
  return `<div class="cs-tidy cs-confirm" role="group" aria-label="${esc(P.delete)}"><span>${esc(P.confirmDelete)}</span>
      <button class="link" id="cs-delete-cancel">${esc(P.cancel)}</button>
      <button class="btn play soft-danger" id="cs-delete" data-id="${id}" ${busy}>${svg("trash")}${esc(P.delete)}</button></div>`;
}

function rowHtml(p, cv, row) {
  const C = TEXT.conversations, opened = cv.opened === row.id, picked = cv.picked.has(row.id);
  const pick = `<label class="cs-pick" title="${esc(C.pick)}"><input type="checkbox" data-cs-pick="${esc(row.id)}" ${picked ? "checked" : ""}
        aria-label="${esc(fill(C.pickOne, {said: row.said || C.nothing}))}"></label>`;
  const failed = row.failed ? ` · <span class="cs-failed">${esc(plural(row.failed, C.failedStep))}</span>` : "";
  return `<div class="cs-row ${opened ? "open" : ""} ${picked ? "picked" : ""} ${row.hidden ? "cs-archived" : ""}"><div class="cs-line">${pick}
      <button class="cs-head" data-cs="${esc(row.id)}" aria-expanded="${opened}">
      <span class="cs-text"><span class="cs-said">${row.said ? marked(row.said, cv.query) : esc(C.nothing)}</span>
        ${row.match ? `<span class="cs-reply cs-match">${marked(row.match, cv.query)}</span>`
          : row.reply ? `<span class="cs-reply">${marked(row.reply, cv.query)}</span>` : ""}
        <span class="cs-sub">${row.timed ? `<mark>${esc(when(row.started))}</mark>` : esc(when(row.started))} · ${esc(plural(row.turns || 0, "turn"))}${failed}</span></span>${badge(row)}</button>${reviewLink(row)}</div>
      ${opened ? reading(p, cv, row) : ""}</div>`;
}

export function section(p) {
  // The conversations, with what finds them; "" until the first page is in.
  const C = TEXT.conversations, cv = state(p);
  if (cv.rows === null && !cv.error) return common.loadingRow();
  const seg = (items, on, attr) => `<div class="seg">${items.map(([value, key]) =>
    `<button data-${attr}="${value}" class="${value === on ? "on" : ""}">${esc(C[key])}</button>`).join("")}</div>`;
  // Select all: every one that matches, past this page too. Ticked with all of them selected, mixed with only some
  // (after); unticking clears the selection.
  const all = cv.every.length && cv.every.every((id) => cv.picked.has(id)), some = !all && cv.picked.size > 0;
  const selectAll = cv.every.length > 1 ? `<label class="cs-all"><input type="checkbox" id="cs-all" ${all ? "checked" : ""}
      ${some ? "data-mixed" : ""}>${esc(fill(C.selectAll, {n: cv.every.length}))}</label>` : "";
  const nothingYet = !cv.query && !cv.days && !cv.only && !cv.total && !cv.archivedCount;
  // Every one archived (settled, or by hand), no search or filter on: nothing left for the household here.
  const allAway = !cv.query && !cv.days && !cv.only && !cv.total && cv.archivedCount && !cv.showArchived;
  const rows = cv.error ? `<div class="conflict">${esc(cv.error)}</div>`
    : cv.rows.length ? (cv.close ? `<p class="cs-close">${esc(C.close)}</p>` : "") + `<div class="card cs-list">${cv.rows.map((row) => rowHtml(p, cv, row)).join("")}</div>`
      + (served(cv) < cv.total ? `<button class="link cs-more" id="cs-more">${esc(C.more)}</button>` : "")
    : `<p class="cs-empty">${esc(nothingYet ? C.empty : allAway ? C.allArchived : C.none)}</p>`;
  const archived = cv.archivedCount && !cv.error ? `<label class="cs-all cs-show-archived"><input type="checkbox" id="cs-archived" ${
    cv.showArchived ? "checked" : ""}>${esc(fill(TEXT.privacy.showArchived, {n: cv.archivedCount}))}</label>` : "";
  // How the last run went; what it found is left out once nothing is left to review.
  const run = p.run?.run, last = run && run.status !== "running" ? common.lastRun(run, !!p.list?.some((i) => i.pending)) : "";
  return `<section class="cs" aria-label="${esc(C.title)}"><h2 class="cs-title">${esc(C.title)}</h2>
      <p class="cs-lead">${esc(cv.onDemand ? C.leadOnDemand : C.lead)}</p>
      ${last ? `<p class="cs-last">${esc(last)}</p>` : ""}
      ${nothingYet ? "" : `<div class="cs-controls"><div class="cs-bar"><span class="cs-find"><input type="search" id="cs-search" placeholder="${esc(C.search)}"
        aria-label="${esc(C.search)}" value="${esc(cv.query)}">${cv.query ? `<button class="cs-clear-search" id="cs-search-clear"
        type="button" aria-label="${esc(C.clearSearch)}" title="${esc(C.clearSearch)}">&#10005;</button>` : ""}</span>
        <span class="cs-filters">${seg(DAYS, cv.days, "days")}${statusFilter(cv)}</span>${
          seg(ORDERS.map((o) => [o, o]), cv.order || (cv.closest ? null : "newest"), "order")}</div>${selectAll}</div>`}${rows}${archived}</section>`;
}

// The Status filter's choices, in the order a conversation moves through them.
const STATUSES = ["waiting", "sent", "active", "smooth", "queued", "fine", "unclear", "working", "writing", "review",
                  "declined", "nofix", "duplicate", "improved", "test", "unreadable", "archived"];
// The standings to choose from: those some conversation matching the rest has, and any chosen.
const choosable = (cv) => STATUSES.filter((s) => cv.byStatus[s] || cv.only?.has(s));

function statusFilter(cv) {
  // Status ▾: All, and a checkbox for each standing to choose from, with how many stand so (only: those chosen, or
  // null for all). Its label says what's shown when it isn't everything: the one standing's name, or how many.
  const C = TEXT.conversations, only = cv.only;
  const label = !only ? C.statusAll : only.size === 1 ? C.status[[...only][0]] : fill(C.statusSome, {n: only.size});
  const box = (attr, on, text, n, mixed) => `<label class="cs-status-opt"><input type="checkbox" ${attr} ${on ? "checked" : ""}
      ${mixed ? "data-mixed" : ""}><span class="cs-status-name">${esc(text)}</span>${n == null ? "" : `<span class="cs-status-n">${n}</span>`}</label>`;
  const menu = cv.statusMenu ? `<div class="backdrop" id="cs-status-backdrop"></div><div class="cs-status-menu" role="group" aria-label="${esc(C.statusAll)}">
      ${box('id="cs-status-all"', !only, C.all, null, only && only.size > 0)}
      ${choosable(cv).map((s) => box(`data-cs-status="${s}"`, !only || only.has(s), C.status[s], cv.byStatus[s] || 0)).join("")}</div>` : "";
  return `<span class="cs-status"><button class="cs-status-btn ${only ? "on" : ""}" id="cs-status" aria-haspopup="true"
      aria-expanded="${!!cv.statusMenu}">${esc(label)}<span aria-hidden="true">&#9662;</span></button>${menu}</span>`;
}

export function after(p) {
  // A mixed Select all (some picked, not all), or a mixed All among the statuses: a checkbox's indeterminate state is
  // a property, set once it's drawn.
  for (const id of ["cs-all", "cs-status-all"]) {
    const box = p.root.getElementById(id);
    if (box) box.indeterminate = box.hasAttribute("data-mixed");
  }
}

function runButton(p, preview, running) {
  // Run, naming how many conversations it moves (it means what it says): with some selected, those of them a run can
  // take; with none, all a full run moves, as Settings' Run. "" with nothing to run (or only at a blocked step), or
  // while a run goes.
  if (running || !preview) return "";
  const {picked} = picking(p), n = common.moves(picked ? runPreview(p, preview) : preview);
  if (!n) return "";
  return `<button class="btn play" id="cs-run">${svg("play")}${esc(fill(TEXT.conversations.runOn, {n, s: n === 1 ? "" : "s"}))}</button>`;
}

function chosen(cv) {
  // What the selection's actions apply to: archive (those not archived that can be; or, while archived ones are
  // shown, unarchive those archived), and delete (every one selected).
  const ids = [...cv.picked], archived = ids.filter((id) => cv.known.get(id)?.hidden);
  return cv.showArchived ? {unarchive: archived, delete: ids}
    : {archive: ids.filter((id) => cv.known.get(id)?.archivable && !cv.known.get(id).hidden), delete: ids};
}

export function bar(p, preview, running) {
  // The bottom bar: Run, and with a selection the actions that apply to it, each with how many (Delete asks first).
  // "" with nothing to run and nothing selected.
  const C = TEXT.conversations, P = TEXT.privacy, cv = state(p), act = chosen(cv), busy = cv.bulk ? "disabled" : "";
  const count = (text, n) => fill(text, {n, s: n === 1 ? "" : "s", them: n === 1 ? "it" : "them"});
  if (cv.asking && act.delete.length) {
    return `<div class="footer cs-footer"><div class="footer-inner"><div class="cs-ask" role="group" aria-label="${esc(P.delete)}">
        <span>${esc(count(C.deleteAsk, act.delete.length))}</span><button class="link" id="cs-many-cancel">${esc(P.cancel)}</button>
        <button class="btn play soft-danger" id="cs-many-delete" ${busy}>${svg("trash")}${esc(P.delete)}</button></div></div></div>`;
  }
  const tidy = [act.archive?.length && `<button class="btn play quiet" id="cs-many-archive" ${busy}>${svg("archive")}${esc(count(C.archiveMany, act.archive.length))}</button>`,
                act.unarchive?.length && `<button class="btn play quiet" id="cs-many-unarchive" ${busy}>${svg("unarchive")}${esc(count(C.unarchiveMany, act.unarchive.length))}</button>`,
                act.delete.length && `<button class="btn play soft-danger" id="cs-many-ask" ${busy}>${svg("trash")}${esc(count(C.deleteMany, act.delete.length))}</button>`]
    .filter(Boolean).join("");
  const run = runButton(p, preview, running);
  if (!run && !tidy) return "";
  return `<div class="footer cs-footer"><div class="footer-inner">${run}${tidy}</div></div>`;
}

async function bulk(p, cv, ids, call, change) {
  // One action on many (the selection's): they leave the selection, and the list and its counts are read again.
  cv.bulk = true;
  p.render();
  try {
    await p.ws(call, change);
    unpick(cv, ids);
    if (ids.includes(cv.opened)) Object.assign(cv, {opened: null, shown: null});
    if (call === "conversations/delete") for (const id of ids) cv.known.delete(id);
    p.setRun?.(await p.ws("run").catch(() => p.run));  // What a run would take now (archived ones: none).
  } catch (err) {
    alert(errText(err));
  }
  Object.assign(cv, {bulk: false, asking: false});
  await list(p, cv, false);
}

let typing = null;

function refilter(p, change) {
  Object.assign(p.cv, change, {opened: null, shown: null});
  list(p, p.cv, false);
}

export const actions = {
  click: [
    ["[data-cs]", (p, el) => open(p, p.cv, el.dataset.cs)],
    ["[data-cs-review]", (p, el) => p.go(el.dataset.csReview)],
    ["#cs-status", (p) => { p.cv.statusMenu = !p.cv.statusMenu; p.render(); }],
    ["#cs-status-backdrop", (p) => { p.cv.statusMenu = false; p.render(); }],
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
    ["#cs-actions", (p) => { p.cv.menu = p.cv.menu === p.cv.opened ? null : p.cv.opened; p.render(); }],
    ["#cs-menu-close", (p) => { p.cv.menu = null; p.render(); }],
    ["#cs-archive", (p, el) => { p.cv.menu = null; archive(p, p.cv, el.dataset.id, true); }],
    ["#cs-unarchive", (p, el) => { p.cv.menu = null; archive(p, p.cv, el.dataset.id, false); }],
    ["#cs-delete-ask", (p, el) => { Object.assign(p.cv, {menu: null, deleting: el.dataset.id, tidyError: null}); p.render(); }],
    ["#cs-delete-cancel", (p) => { p.cv.deleting = null; p.render(); }],
    ["#cs-many-archive", (p) => { const ids = chosen(p.cv).archive; bulk(p, p.cv, ids, "conversations/hide", {recording_ids: ids, hidden: true}); }],
    ["#cs-many-unarchive", (p) => { const ids = chosen(p.cv).unarchive; bulk(p, p.cv, ids, "conversations/hide", {recording_ids: ids, hidden: false}); }],
    ["#cs-many-ask", (p) => { p.cv.asking = true; p.render(); }],
    ["#cs-many-cancel", (p) => { p.cv.asking = false; p.render(); }],
    ["#cs-many-delete", (p) => { const ids = chosen(p.cv).delete; bulk(p, p.cv, ids, "conversations/delete", {recording_ids: ids}); }],
    ["#cs-delete", (p, el) => remove(p, p.cv, el.dataset.id)],
    // With some selected, a run on those; with none, a full run, as Settings' Run.
    ["#cs-run", (p) => { p.picking = picking(p).picked > 0; p.only = null; p.confirming = true; p.render(); }],
  ],
  change: [
    ["[data-cs-pick]", (p, el) => {
      const id = el.dataset.csPick;
      if (el.checked) { p.cv.picked.add(id); p.cv.byHand.add(id); } else unpick(p.cv, [id]);
      p.render();
    }],
    ["#cs-all", (p, el) => {
      // Ticked (or from mixed): every one that matches is selected. Unticked: nothing is, those ticked by hand too.
      if (el.checked) for (const id of p.cv.every) p.cv.picked.add(id);
      else unpick(p.cv, [...p.cv.picked]);
      p.render();
    }],
    ["[data-cs-status]", (p, el) => {
      // One standing in or out; all of them chosen is no filter at all.
      const all = choosable(p.cv), only = new Set(p.cv.only || all);
      el.checked ? only.add(el.dataset.csStatus) : only.delete(el.dataset.csStatus);
      refilter(p, {only: all.every((s) => only.has(s)) ? null : only});
    }],
    ["#cs-status-all", (p, el) => refilter(p, {only: el.checked ? null : new Set()})],
    ["#cs-archived", (p, el) => {
      // Archived ones out of view again: none of them stays picked.
      if (!el.checked) unpick(p.cv, [...p.cv.picked].filter((id) => p.cv.known.get(id)?.hidden));
      refilter(p, {showArchived: el.checked});
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
