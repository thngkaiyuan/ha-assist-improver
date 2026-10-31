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
  // showArchived: archived ones are listed too, on their own below Show archived (away: that list, {rows, total,
  // every, close, byStatus}, as the list's own are kept on cv); archivedCount: how many the list leaves out (archived,
  // or settled). picked: the selection, any row, kept through searches, filters and Show more; byHand: those ticked
  // one by one; known: where each one listed so far stands ({status, stage, hidden, runnable, archivable}); every: the
  // IDs of all that match now. A row with stays: the opened one, kept in view though it no longer matches (list).
  return p.cv || (p.cv = {query: "", days: "", order: null, rows: null, total: 0,
                          opened: null, shown: null, note: "", sent: {}, picked: new Set(), byHand: new Set(),
                          known: new Map(), every: [], error: null, showArchived: false, archivedCount: 0, away: null,
                          only: null, byStatus: {}, statusMenu: false, sortMenu: false,
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
  await list(p, state(p), {draw: false});
}

const served = (rows) => rows.filter((r) => !r.stays).length;  // The rows Home Assistant listed: where the next page starts.
const listedRows = (cv) => [...(cv.rows || []), ...(cv.away?.rows || [])];  // Both lists' rows.

function shownIn(cv, id) {
  // The list that shows this one: where it stays (kept where it's read), or else where it's listed. Home Assistant
  // may list it in the other one too by then (archived, or unarchived, while it's read); that row waits till it's
  // closed and the lists are read again.
  const has = (list, stays) => list?.rows.some((r) => r.id === id && (!stays || r.stays));
  return has(cv, true) ? cv : has(cv.away, true) ? cv.away : has(cv) ? cv : has(cv.away) ? cv.away : null;
}

async function list(p, cv, {more = false, only = null, draw = true, anew = false} = {}) {
  // The list, and the archived ones below Show archived while it's ticked: both read again from the start, or only
  // one of them (only: "rows" or "away"), or that one's next page (more). Each has its own sequence: a newer read of
  // one never drops the other's.
  const reads = {rows: only !== "away", away: only !== "rows" && cv.showArchived};
  const seq = {rows: reads.rows && (cv.seq = (cv.seq || 0) + 1), away: reads.away && (cv.awaySeq = (cv.awaySeq || 0) + 1)};
  const stale = (which) => seq[which] !== (which === "rows" ? cv.seq : cv.awaySeq) || (which === "away" && !cv.showArchived);
  // Where the selected ones and the opened one stand now, whatever the filters (as many as Home Assistant takes).
  const ids = [...new Set([...cv.picked, ...(cv.opened ? [cv.opened] : [])])].slice(0, 5000);
  const ask = (archived, offset) => p.ws("conversations", {
    query: cv.query || undefined, days: cv.days ? Number(cv.days) : undefined, order: cv.order || undefined, offset,
    archived: archived || undefined, statuses: cv.only ? [...cv.only] : undefined,
    ids: ids.length && !archived ? ids : undefined});
  try {
    let [found, away] = await Promise.all([reads.rows ? ask(false, more ? served(cv.rows) : 0) : null,
                                           reads.away ? ask(true, more ? served(cv.away.rows) : 0) : null]);
    if (found && stale("rows")) found = null;  // A newer read took over (or the archived ones were hidden since).
    if (away && stale("away")) away = null;
    if (!found && !away) return;
    const stands = new Map((found?.stands || []).map((r) => [r.id, r]));
    const page = (old, got) => {
      // A page appended: the opened one kept in view (stays) gives way to its own row when that page brings it.
      const fresh = new Set(got.conversations.map((r) => r.id));
      return more ? [...old.filter((r) => !(r.stays && fresh.has(r.id))), ...got.conversations] : got.conversations;
    };
    const keep = (old, now) => {
      // The opened one stays where it's read, though its list no longer has it (a run settled it, so it's archived,
      // or took it past the statuses chosen; or it was archived or unarchived while it's read); it goes once it's
      // closed and the list is read again.
      const was = !more && cv.opened && (old || []).find((r) => r.id === cv.opened);
      if (!was || !stands.has(was.id) || now.some((r) => r.id === was.id)) return now;
      const at = Math.min(old.indexOf(was), now.length);
      return [...now.slice(0, at), {...was, ...stands.get(was.id), stays: true}, ...now.slice(at)];
    };
    if (found) {
      Object.assign(cv, {rows: keep(cv.rows, page(cv.rows, found)), total: found.total, close: !!found.close,
                         closest: !!found.closest, archivedCount: found.hidden || 0,
                         every: (found.every || []).map((r) => r.id), byStatus: found.by_status || {},
                         onDemand: found.on_demand, error: null});
    }
    if (away) {
      cv.away = {rows: keep(cv.away?.rows, page(cv.away?.rows, away)), total: away.total, close: !!away.close,
                 every: (away.every || []).map((r) => r.id), byStatus: away.by_status || {}};
    }
    if (!cv.archivedCount) Object.assign(cv, {showArchived: false, away: null});  // None archived any more.
    for (const r of [...(found?.every || []), ...(away?.every || []), ...stands.values()]) {
      cv.known.set(r.id, {status: r.status, stage: r.stage, hidden: r.hidden, runnable: r.runnable,
                          archivable: r.archivable});
    }
    // Selected ones archived since (a run settled them) are out of view: none of them stays picked.
    const inView = new Set(listedRows(cv).map((r) => r.id));
    if (!cv.showArchived) unpick(cv, [...stands.values()].filter((r) => r.hidden && !inView.has(r.id)).map((r) => r.id));
    // The opened one stands where its row now does (a run may have taken it in since it was opened).
    const row = cv.shown && !cv.shown.error && shownIn(cv, cv.opened)?.rows.find((r) => r.id === cv.opened);
    if (row) Object.assign(cv.shown, {status: row.status, note: row.note, hidden: row.hidden, sendable: row.sendable,
                                      archivable: row.archivable});
  } catch (err) {
    if (stale("rows") && (!reads.away || stale("away"))) return;
    cv.error = errText(err);
  }
  if (anew) p.letGo?.();  // Another list: as long as it is.
  if (draw) p.render();
}

async function open(p, cv, id) {
  Object.assign(cv, {deleting: null, tidyError: null});
  if (cv.opened === id) { Object.assign(cv, {opened: null, shown: null}); steady(p, id); return; }
  Object.assign(cv, {opened: id, shown: null, note: ""});
  steady(p, id);  // One open above it closes as it opens.
  try {
    const shown = await p.ws("conversation", {recording_id: id});
    if (cv.opened === id) cv.shown = shown;
  } catch (err) {
    if (cv.opened === id) cv.shown = {error: errText(err)};
  }
  steady(p, id);  // What it brings, or the browser keeping something else in place, may shift the page.
  if (cv.opened === id) scrollTo(p, id);  // Not if it was closed, or another opened, while it loaded.
}

function steady(p, id) {
  // Drawn again with the row tapped where it was on the screen: the page is put back at once by however far the row's
  // head moved (a scroll in between, the household's own, is kept), and at the page's end it has the room to be.
  const head = () => [...p.root.querySelectorAll("[data-cs]")].find((el) => el.dataset.cs === id)?.getBoundingClientRect().top;
  const before = head();
  p.hold();
  p.render();
  const after = head();
  if (before != null && after != null && after !== before) p.scroller().scrollBy({top: after - before, behavior: "instant"});
  p.fit?.();  // Only as long as where it is now needs.
}

function scrollTo(p, id) {
  // Once it's in, the opened conversation's first turn in view above its Send row (and the Run bar under that), scrolled
  // just that far, and never so far that the row tapped goes under the pinned controls: in view already, nothing moves.
  // Where the Send row and the controls will stick, not where they are now (before a scroll, neither may be stuck).
  const row = [...p.root.querySelectorAll("[data-cs]")].find((el) => el.dataset.cs === id)?.closest(".cs-row");
  const first = row?.querySelector(".cs-turn") || row?.querySelector(".cs-read .conflict");  // Or why it couldn't.
  if (!first) return;
  const send = row.querySelector(".cs-send"), controls = p.root.querySelector(".cs-controls");
  const below = send ? parseFloat(getComputedStyle(send).bottom) + send.offsetHeight : Math.max(p.bar || 0, insetBottom(p));
  const floor = innerHeight - below - 8;
  const ceiling = (controls ? parseFloat(getComputedStyle(controls).top) + controls.offsetHeight : 0) + 8;
  const by = Math.min(first.getBoundingClientRect().bottom - floor, row.getBoundingClientRect().top - ceiling);
  if (by > 0) p.scroller().scrollBy({top: by, behavior: "smooth"});
}

function insetBottom(p) {
  // A phone's home indicator's strip (Home Assistant's app), as the panel's style has it.
  const probe = p.root.appendChild(document.createElement("div"));
  probe.style.cssText = "position: fixed; visibility: hidden; height: var(--inset-bottom)";
  const height = probe.offsetHeight;
  probe.remove();
  return height;
}

async function send(p, cv, id) {
  const C = TEXT.conversations;
  cv.sending = id;
  p.render();
  try {
    const result = await p.ws("conversation/send", {recording_id: id, note: cv.note || ""});
    cv.sent[id] = {text: result.on_demand ? C.sentNow : C.sentLater};
    // One settled (archived on its own, opened while archived ones are shown) is back in the list once sent in again:
    // the lists are read again below (it stays where it's read till it's closed).
    const hidden = result.hidden ?? cv.known.get(id)?.hidden, back = cv.known.get(id)?.hidden && !hidden;
    // Sent again without a note, it keeps the one it had (DETAILS P8.9.4).
    const note = cv.note?.trim() || cv.shown?.note || null;
    for (const row of listedRows(cv)) if (row.id === id) Object.assign(row, {status: result.status, note, hidden});
    cv.known.set(id, {...cv.known.get(id), status: result.status, hidden});
    if (cv.shown?.id === id) Object.assign(cv.shown, {status: result.status, note, hidden});
    cv.note = "";
    unpick(cv, [id]);
    if (back) await list(p, cv, {draw: false});
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
  // A conversation gone (deleted): out of both lists, closed, and no longer picked.
  for (const list of [cv, cv.away].filter(Boolean)) {
    if (list.rows.some((r) => r.id === row.id && !r.stays)) list.total = Math.max(0, list.total - 1);
    list.rows = list.rows.filter((r) => r.id !== row.id);
    list.every = list.every.filter((w) => w !== row.id);
  }
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
  await list(p, cv);
}

async function remove(p, cv, id) {
  // Deleted for good, with what the improver derived from it: gone from the list, and from what a run takes.
  Object.assign(cv, {tidying: id, tidyError: null});
  p.render();
  try {
    await p.ws("conversation/delete", {recording_id: id});
    const row = listedRows(cv).find((r) => r.id === id);
    if (row) {
      if (row.hidden) cv.archivedCount = Math.max(0, cv.archivedCount - 1);
      drop(cv, row);
    }
    if (!cv.archivedCount) Object.assign(cv, {showArchived: false, away: null});
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
  const rows = new Map(listedRows(cv).map((r) => [r.id, r])), moved = [], releases = {};
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
  return row.status === "review" && row.review || cannot(row) ? ""
    : `<span class="chip cs-${row.status}"${tip}>${esc(C.status[row.status] || row.status)}</span>`;
}

// One Home Assistant answered itself (DETAILS P9.9.9): what to do, said in full in its bubble, which a tap or focus
// shows too; beside the row, so reading it doesn't open the conversation.
const cannot = (row) => row.cant_improve && !row.held;
function cannotChip(row) {
  if (!cannot(row)) return "";
  const C = TEXT.conversations, said = fill(C.cannotTip, row.cant_improve);
  return `<span class="chip cs-cannot" data-tip="${esc(said)}" tabindex="0" role="note"
    aria-label="${esc(`${C.status.cannot}: ${said}`)}">${esc(C.status.cannot)}</span>`;
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

// What sending the opened one in did, while it's shown (until the improver has taken it), or what went wrong; and
// whether its Send row is shown: it can be sent in, and wasn't just.
const sentNote = (cv, id) => cv.sent[id] && (cv.sent[id].error || cv.shown?.status === "sent") ? cv.sent[id] : null;
const sending = (cv, id) => !!cv.shown?.sendable && !(sentNote(cv, id) && !sentNote(cv, id).error);

function reading(p, cv, row) {
  // The conversation opened, to read and send in.
  const C = TEXT.conversations, shown = cv.shown;
  // While it loads, a box of its own (cs-wait first): the conversation, once in, is put in anew, so it fades in then.
  if (!shown) return `<div class="cs-wait cs-read">${common.loadingRow()}</div>`;
  if (shown.error) return `<div class="cs-read"><div class="conflict">${esc(shown.error)}</div></div>`;
  const turns = dialog(shown.turns, cv.query);
  const sent = sentNote(cv, row.id);
  // Sending it in starts a run for it (on demand), so it looks like Run: the note and the button stay in view while
  // it's read, unless it was just sent in. Archive and Delete wait behind the ⋮ beside it, so the row stays calm.
  // Delete asks there, in the row's place (in view, where the ⋮ was tapped, and no Send beside it to tap instead).
  const asking = cv.deleting === row.id;
  const error = cv.tidyError?.id === row.id ? `<div class="conflict" role="status">${esc(cv.tidyError.text)}</div>` : "";
  const send = sending(cv, row.id) && asking ? confirm(cv, row, "cs-send", error)
    : sending(cv, row.id) ? `<div class="cs-send"><input class="cs-note" id="cs-note" maxlength="500" placeholder="${esc(C.note)}" value="${esc(cv.note)}">
        <button class="btn play" id="cs-send" data-id="${esc(row.id)}" ${cv.sending ? "disabled" : ""}${
          cv.sending === row.id ? ' aria-busy="true"' : ""}>${cv.sending === row.id  // On its way: same words, same size.
          ? `<span class="spinner ic" aria-hidden="true"></span>` : svg("send")}${esc(C.send)}</button>${dots(cv, row, shown)}</div>` : "";
  const note = shown.note ? `<div class="cs-sub">${esc(C.yourNote)} ${esc(shown.note)}</div>` : "";
  const alone = !send && !asking ? `<div class="cs-tidy">${dots(cv, row, shown)}</div>` : "";
  const pinned = send && asking;  // Asking in the Send row's place: why it couldn't is said there, in view.
  return `<div class="cs-read">${turns}${note}${send}${alone}${send ? "" : confirm(cv, row, "cs-tidy")}${
    sent ? `<div class="${sent.error ? "conflict" : "cs-done"}" role="status">${esc(sent.text)}</div>` : ""}${pinned ? "" : error}</div>`;
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

function confirm(cv, row, where, error = "") {
  // Delete from Assist Improver, asking first: in the Send row's place (cs-send, with why if it couldn't), or where
  // the ⋮ was (cs-tidy).
  const P = TEXT.privacy, busy = cv.tidying ? "disabled" : "", id = esc(row.id);
  if (cv.deleting !== row.id) return "";
  return `<div class="${where} cs-confirm" role="group" aria-label="${esc(P.delete)}"><span>${esc(P.confirmDelete)}</span>
      <button class="link" id="cs-delete-cancel">${esc(P.cancel)}</button>
      <button class="btn play soft-danger" id="cs-delete" data-id="${id}" ${busy}>${svg("trash")}${esc(P.delete)}</button>${error}</div>`;
}

function rowHtml(p, cv, row) {
  const C = TEXT.conversations, opened = cv.opened === row.id, picked = cv.picked.has(row.id);
  const pick = `<label class="cs-pick" title="${esc(C.pick)}"><input type="checkbox" data-cs-pick="${esc(row.id)}" ${picked ? "checked" : ""}
        aria-label="${esc(fill(C.pickOne, {said: row.said || C.nothing}))}"></label>`;
  const failed = row.failed ? ` · <span class="cs-failed">${esc(plural(row.failed, C.failedStep))}</span>` : "";
  // Keyed, so a row moves whole when the list changes above it (morph): an opened conversation never fades in again.
  return `<div class="cs-row ${opened ? "open" : ""} ${picked ? "picked" : ""} ${row.hidden ? "cs-archived" : ""}" data-key="cs-${esc(row.id)}"><div class="cs-line">${pick}
      <button class="cs-head" data-cs="${esc(row.id)}" aria-expanded="${opened}">
      <span class="cs-text"><span class="cs-said">${row.said ? marked(row.said, cv.query) : esc(C.nothing)}</span>
        ${row.match ? `<span class="cs-reply cs-match">${marked(row.match, cv.query)}</span>`
          : row.reply ? `<span class="cs-reply">${marked(row.reply, cv.query)}</span>` : ""}
        <span class="cs-sub">${row.timed ? `<mark>${esc(when(row.started))}</mark>` : esc(when(row.started))} · ${esc(plural(row.turns || 0, "turn"))}${failed}</span></span>${badge(row)}</button>${reviewLink(row)}${cannotChip(row)}</div>
      ${opened ? reading(p, cv, row) : ""}</div>`;
}

export function section(p) {
  // The conversations, with what finds them; "" until the first page is in.
  const C = TEXT.conversations, cv = state(p);
  if (cv.rows === null && !cv.error) return common.loadingRow();
  const seg = (items, on, attr) => `<div class="seg">${items.map(([value, key]) =>
    `<button data-${attr}="${value}" class="${value === on ? "on" : ""}">${esc(C[key])}</button>`).join("")}</div>`;
  // Select all: every one its list matches, past this page too: the list's, pinned on top, and the archived ones',
  // heading them below Show archived. Ticked with all of them selected, mixed with only some of that list's (after);
  // unticking clears that list's picks.
  const selectAllBox = (list, id, archived) => {
    const all = list.every.length && list.every.every((w) => cv.picked.has(w));
    const some = !all && [...cv.picked].some((w) => !!cv.known.get(w)?.hidden === archived);
    return list.every.length > 1 ? `<label class="cs-all"><input type="checkbox" id="${id}" ${all ? "checked" : ""}
      ${some ? "data-mixed" : ""}>${esc(fill(C.selectAll, {n: list.every.length}))}</label>` : "";
  };
  const shown = (list) => list.rows.filter((row) => shownIn(cv, row.id) === list);
  const listed = (list, more) => (list.close ? `<p class="cs-close">${esc(C.close)}</p>` : "")
    + `<div class="card cs-list">${shown(list).map((row) => rowHtml(p, cv, row)).join("")}</div>`
    + (served(list.rows) < list.total ? `<button class="link cs-more" id="${more}">${esc(C.more)}</button>` : "");
  const nothingYet = !cv.query && !cv.days && !cv.only && !cv.total && !cv.archivedCount;
  // Every one archived (settled, or by hand), no search or filter on: nothing left for the household here.
  const allAway = !cv.query && !cv.days && !cv.only && !cv.total && cv.archivedCount;
  const rows = cv.error ? `<div class="conflict">${esc(cv.error)}</div>` : shown(cv).length ? listed(cv, "cs-more")
    : `<p class="cs-empty">${esc(nothingYet ? C.empty : allAway ? C.allArchived : C.none)}</p>`;
  // Show archived, and the archived ones below it, so ticking it moves nothing on the screen. None of them matching
  // is said there only when the list above has some: otherwise its own words say it for both.
  const archived = cv.archivedCount && !cv.error ? `<label class="cs-all cs-show-archived"><input type="checkbox" id="cs-archived" ${
    cv.showArchived ? "checked" : ""}>${esc(fill(TEXT.privacy.showArchived, {n: cv.archivedCount}))}</label>${
    cv.showArchived && cv.away ? `<div class="cs-away">${selectAllBox(cv.away, "cs-all-away", true)}${shown(cv.away).length
      ? listed(cv.away, "cs-more-away") : shown(cv).length ? `<p class="cs-empty">${esc(C.none)}</p>` : ""}</div>` : ""}` : "";
  // How the last run went; what it found is left out once nothing is left to review.
  const run = p.run?.run, last = run && run.status !== "running" ? common.lastRun(run, !!p.list?.some((i) => i.pending)) : "";
  // What the list is for, on demand: in the bubble beside its title (a note screen readers read in full, the title
  // left as it is).
  const intro = cv.onDemand ? C.leadOnDemand : C.lead;
  const about = `<span class="info-tip" data-tip="${esc(intro)}" tabindex="0" role="note"
      aria-label="${esc(`About ${C.title}: ${intro}`)}">${svg("info")}</span>`;
  return `<section class="cs" aria-label="${esc(C.title)}"><div class="cs-top"><h2 class="cs-title">${esc(C.title)}</h2>${about}</div>
      ${last ? `<p class="cs-last">${esc(last)}</p>` : ""}
      ${nothingYet ? "" : `<div class="cs-controls"><div class="cs-bar"><span class="cs-find"><input type="search" id="cs-search" placeholder="${esc(C.search)}"
        aria-label="${esc(C.search)}" value="${esc(cv.query)}">${cv.query ? `<button class="cs-clear-search" id="cs-search-clear"
        type="button" aria-label="${esc(C.clearSearch)}" title="${esc(C.clearSearch)}">&#10005;</button>` : ""}</span>
        <span class="cs-filters">${seg(DAYS, cv.days, "days")}${statusFilter(cv)}</span>${sortMenu(cv)}</div>${
          selectAllBox(cv, "cs-all", false)}</div>`}${rows}${archived}</section>`;
}

// A menu's label, its button as wide as its longest choice, as a select is: choosing one moves nothing beside it.
const sized = (label, choices) => `<span class="cs-sizer" data-choices="${esc(choices.join("\n"))}"><span
    class="cs-status-label">${esc(label)}</span></span>`;

function sortMenu(cv) {
  // The order, one compact control beside the search: Newest first, unless another is chosen; none while a time
  // searched for puts the closest first.
  const C = TEXT.conversations, on = cv.order || (cv.closest ? null : "newest");
  const menu = cv.sortMenu ? `<div class="backdrop" id="cs-sort-backdrop"></div><div class="cs-status-menu cs-sort-menu" role="menu"
      aria-label="${esc(C.sort)}">${ORDERS.map((o) => `<button role="menuitemradio" aria-checked="${o === on}" data-order="${o}"
      class="${o === on ? "on" : ""}">${esc(C[o])}</button>`).join("")}</div>` : "";
  return `<span class="cs-status cs-sort"><button class="cs-status-btn" id="cs-sort" aria-haspopup="true"
      aria-expanded="${!!cv.sortMenu}">${sized(on ? C[on] : C.sort, [...ORDERS.map((o) => C[o]), C.sort])}<span
      aria-hidden="true">&#9662;</span></button>${menu}</span>`;
}

// The Status filter's choices, in the order a conversation moves through them.
const STATUSES = ["waiting", "sent", "active", "smooth", "queued", "fine", "unclear", "cannot", "working", "writing",
                  "review", "declined", "nofix", "duplicate", "improved", "test", "unreadable", "archived"];
// The standings to choose from: those some conversation matching the rest has, and any chosen.
const standing = (cv, s) => (cv.byStatus[s] || 0) + (cv.away?.byStatus[s] || 0);  // In the list and below Show archived.
const choosable = (cv) => STATUSES.filter((s) => standing(cv, s) || cv.only?.has(s));

function statusFilter(cv) {
  // Status ▾: All, and a checkbox for each standing to choose from, with how many stand so (only: those chosen, or
  // null for all). Its label says what's shown when it isn't everything: the one standing's name, or how many.
  const C = TEXT.conversations, only = cv.only;
  const label = !only ? C.statusAll : only.size === 1 ? C.status[[...only][0]] : fill(C.statusSome, {n: only.size});
  const box = (attr, on, text, n, mixed) => `<label class="cs-status-opt"><input type="checkbox" ${attr} ${on ? "checked" : ""}
      ${mixed ? "data-mixed" : ""}><span class="cs-status-name">${esc(text)}</span>${n == null ? "" : `<span class="cs-status-n">${n}</span>`}</label>`;
  const menu = cv.statusMenu ? `<div class="backdrop" id="cs-status-backdrop"></div><div class="cs-status-menu" role="group" aria-label="${esc(C.statusAll)}">
      ${box('id="cs-status-all"', !only, C.all, null, only && only.size > 0)}
      ${choosable(cv).map((s) => box(`data-cs-status="${s}"`, !only || only.has(s), C.status[s], standing(cv, s))).join("")}</div>` : "";
  return `<span class="cs-status"><button class="cs-status-btn ${only ? "on" : ""}" id="cs-status" aria-haspopup="true"
      aria-expanded="${!!cv.statusMenu}">${sized(label, [C.statusAll, ...STATUSES.map((s) => C.status[s]),
        fill(C.statusSome, {n: STATUSES.length})])}<span aria-hidden="true">&#9662;</span></button>${menu}</span>`;
}

export function after(p) {
  // A mixed Select all (some picked, not all), or a mixed All among the statuses: a checkbox's indeterminate state is
  // a property, set once it's drawn.
  for (const id of ["cs-all", "cs-all-away", "cs-status-all"]) {
    const box = p.root.getElementById(id);
    if (box) box.indeterminate = box.hasAttribute("data-mixed");
  }
}

function runButton(p, preview, running) {
  // Run, saying what it runs on (it means what it says): with some selected, how many of them a run can take; with
  // none, a full run, in Settings' Run's words. "" with nothing to run (or only at a blocked step), or while a run goes.
  if (running || !preview) return "";
  const {picked} = picking(p), n = common.moves(picked ? runPreview(p, preview) : preview);
  if (!n) return "";
  const words = picked ? fill(TEXT.conversations.runOn, {n, s: n === 1 ? "" : "s"}) : common.runLabel(preview);
  // A conversation open to send in has the one primary action, Send this one in: Run steps back (same size, same place).
  const cv = state(p), open = !cv.error && listedRows(cv).some((r) => r.id === cv.opened) && sending(cv, cv.opened);
  return `<button class="btn play${open ? " quiet" : ""}" id="cs-run">${svg("play")}${esc(words)}</button>`;
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
  await list(p, cv);
}

function selectAll(p, on, archived) {
  // Select all, ticked (or from mixed): every one its list matches is selected (the list's, or the archived ones' below
  // Show archived). Unticked: none of that list's is, those ticked by hand too.
  const cv = p.cv;
  if (on) for (const id of archived ? cv.away.every : cv.every) cv.picked.add(id);
  else unpick(cv, [...cv.picked].filter((id) => !!cv.known.get(id)?.hidden === archived));
  p.render();
}

let typing = null;

function refilter(p, change) {
  Object.assign(p.cv, change, {opened: null, shown: null});
  list(p, p.cv, {anew: true});
}

export const actions = {
  click: [
    ["[data-cs]", (p, el) => open(p, p.cv, el.dataset.cs)],
    ["[data-cs-review]", (p, el) => p.go(el.dataset.csReview)],
    ["#cs-status", (p) => { p.cv.statusMenu = !p.cv.statusMenu; p.render(); }],
    ["#cs-status-backdrop", (p) => { p.cv.statusMenu = false; p.render(); }],
    ["#cs-sort", (p) => { p.cv.sortMenu = !p.cv.sortMenu; p.render(); }],
    ["#cs-sort-backdrop", (p) => { p.cv.sortMenu = false; p.render(); }],
    ["[data-days]", (p, el) => refilter(p, {days: el.dataset.days})],
    ["[data-order]", (p, el) => { refilter(p, {order: el.dataset.order, sortMenu: false}); p.render(); }],
    ["#cs-more", (p) => list(p, p.cv, {more: true, only: "rows"})],
    ["#cs-more-away", (p) => list(p, p.cv, {more: true, only: "away"})],
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
    ["#cs-delete-ask", (p, el) => {  // The question, Cancel first in focus; back to the ⋮ once it's answered.
      Object.assign(p.cv, {menu: null, deleting: el.dataset.id, tidyError: null});
      p.render();
      p.root.getElementById("cs-delete-cancel")?.focus({preventScroll: true});
    }],
    ["#cs-delete-cancel", (p) => { p.cv.deleting = null; p.render(); p.root.getElementById("cs-actions")?.focus({preventScroll: true}); }],
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
    ["#cs-all", (p, el) => selectAll(p, el.checked, false)],
    ["#cs-all-away", (p, el) => selectAll(p, el.checked, true)],
    ["[data-cs-status]", (p, el) => {
      // One standing in or out; all of them chosen is no filter at all.
      const all = choosable(p.cv), only = new Set(p.cv.only || all);
      el.checked ? only.add(el.dataset.csStatus) : only.delete(el.dataset.csStatus);
      refilter(p, {only: all.every((s) => only.has(s)) ? null : only});
    }],
    ["#cs-status-all", (p, el) => refilter(p, {only: el.checked ? null : new Set()})],
    ["#cs-archived", (p, el) => {
      // Ticked: the archived ones are read, to show below it; nothing above it changes. Unticked: they go, none of
      // them stays picked, and one opened among them closes.
      const cv = p.cv;
      cv.showArchived = el.checked;
      if (el.checked) return list(p, cv, {only: "away"});
      unpick(cv, [...cv.picked].filter((id) => cv.known.get(id)?.hidden));
      if (cv.opened && shownIn(cv, cv.opened) === cv.away) Object.assign(cv, {opened: null, shown: null});
      cv.away = null;
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
