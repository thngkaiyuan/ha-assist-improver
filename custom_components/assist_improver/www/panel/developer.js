// The developer view (developer mode only): any ID's lifecycle (Find), every conversation, runs and where their time
// went, the improver's footprint (Health), and the journal (Logs), from the assist_improver/dev/... commands
// (devview.py). Dense and technical: wide tables scroll inside their card, never the page.

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, bytes, dur, num, stamp, ago, between, devUrl}, {svg}, common, parts] = await Promise.all(
  ["format", "icons", "common", "devparts"].map((name) => import(`./${name}.js${v}`)));
const {idLink, ids, kv, chip, statusChip, stageChip, timeline, journal, stageBar, waterfall, problemCount, WF_DEPTH} = parts;

const TABS = [["find", "Find"], ["conversations", "Conversations"], ["runs", "Runs"], ["health", "Health"], ["logs", "Logs"]];
const STAGES = ["active", "waiting", "held", "failing", "set_aside", "analysed", "improvement", "duplicate", "smooth", "test", "empty"];
const LEVELS = ["debug", "info", "warning", "error", "critical"];
const LOG_PAGE = 100;
const NO_FILTERS = {level: "", text: "", run: "", recording: ""};

const errText = (err) => err?.message ? `${err.message}${err.code ? ` (${err.code})` : ""}` : String(err);

// Loading.

export async function load(p, params) {
  const tab = TABS.some(([key]) => key === params.get("tab")) ? params.get("tab") : "find";
  const q = (params.get("q") || "").trim();
  const dev = p.dev || (p.dev = {conv: {stage: "", search: "", sort: "started", desc: true}});
  const moved = p.view !== "developer" || dev.tab !== tab || dev.q !== q;
  clearTimeout(p.pollTimer);
  p.view = "developer"; p.review = null; p.pop = null;
  if (moved) {  // Another tab or ID: show it at once, loading, rather than the old one until the data is in.
    Object.assign(dev, {tab, q, error: null, loading: true, toTop: true, find: null, trace: null, traceError: null,
                        flipped: new Set(), shown: new Set(), depth: WF_DEPTH, journalAll: false});
    if (tab === "logs") {
      const filters = Object.fromEntries(Object.keys(NO_FILTERS).map((k) => [k, params.get(k) || ""]));
      dev.logs = {filters, entries: null, more: false, query: null, busy: false, error: null};
    }
    p.render();
  }
  const seq = p.devSeq = (p.devSeq || 0) + 1;
  const got = {};
  try {
    if (tab === "find" && q) {
      got.find = await p.ws("dev/lookup", {query: q});
      if (got.find.trace_id) Object.assign(got, await trace(p, got.find.trace_id));
    } else if (tab === "conversations") {
      got.rows = (await p.ws("dev/conversations")).rows;
    } else if (tab === "runs") {
      got.runs = (await p.ws("dev/runs")).runs;
      const run = q && got.runs.find((r) => r.id === q);
      if (run?.trace_available) Object.assign(got, await trace(p, run.trace_id));
    } else if (tab === "health") {
      got.health = await p.ws("dev/health");
    } else if (tab === "logs") {
      const query = await logQuery(p, dev.logs.filters);
      got.logs = {query, ...(await p.hass.callWS(query))};
    }
  } catch (err) {
    got.error = errText(err);
  }
  // A newer load (another tab, or Refresh) took over, or the panel has moved on to another view.
  if (seq !== p.devSeq || new URLSearchParams(location.search).get("view") !== "developer") return;
  dev.loading = false;
  dev.error = got.error || null;
  if (!got.error) {
    if (tab === "find" || tab === "runs") Object.assign(dev, {trace: got.trace || null, traceError: got.traceError || null});
    if (tab === "find") dev.find = got.find || null;
    if (got.rows) dev.rows = got.rows;
    if (got.runs) dev.runs = got.runs;
    if (got.health) dev.health = got.health;
    if (got.logs) Object.assign(dev.logs, {query: got.logs.query, entries: got.logs.entries, more: got.logs.more, error: null});
  }
  poll(p);
}

function poll(p) {
  // Nothing polls but Runs, every 5 s while a run is active; Refresh reloads any tab.
  clearTimeout(p.devTimer);
  const dev = p.dev, shown = (dev?.runs || []).filter((r) => !dev.q || r.id === dev.q);  // The list, or the run open.
  if (dev?.tab !== "runs" || !shown.some((r) => r.status === "running")) return;
  p.devTimer = setTimeout(() => {
    if (p.host.isConnected && p.view === "developer" && p.dev.tab === "runs") p.load();
  }, 5000);
}

async function trace(p, traceId) {
  try { return {trace: (await p.ws("dev/trace", {trace_id: traceId})).trace}; }
  catch (err) { return {traceError: errText(err)}; }
}

async function logQuery(p, filters) {
  // The journal command for these filters. The journal matches tags exactly, so a run or conversation given by a
  // prefix, a short ID or Home Assistant's conversation_id is resolved to its full ID first.
  const msg = {type: "assist_improver/dev/journal", limit: LOG_PAGE};
  if (filters.level) msg.level = filters.level;
  if (filters.text.trim()) msg.text = filters.text.trim();
  if (filters.run.trim()) msg.run_id = await resolveId(p, filters.run.trim(), ["run"]);
  if (filters.recording.trim()) msg.recording_id = await resolveId(p, filters.recording.trim(), ["recording", "conversation_id"]);
  return msg;
}

async function resolveId(p, value, kinds) {
  if (value.length >= 32) return value;
  try {
    const found = await p.ws("dev/lookup", {query: value});
    if (found.match && kinds.includes(found.match.kind)) return found.match.recording || found.match.id;
  } catch (err) { /* Not resolvable: filter by it as given. */ }
  return value;
}

async function applyLogs(p, filters) {
  const logs = p.dev.logs;
  Object.assign(logs, {filters, busy: true, error: null});
  p.render();
  try {
    logs.query = await logQuery(p, filters);
    const page = await p.hass.callWS(logs.query);
    Object.assign(logs, {entries: page.entries, more: page.more});
  } catch (err) { logs.error = errText(err); }
  logs.busy = false;
  p.render();
}

async function moreLogs(p) {
  // The next page: entries older than the last one shown.
  const logs = p.dev.logs, last = logs.entries?.[logs.entries.length - 1];
  if (!last || logs.busy || !logs.query) return;
  logs.busy = true;
  p.render();
  try {
    const page = await p.hass.callWS({...logs.query, before: last.at});
    Object.assign(logs, {entries: [...logs.entries, ...page.entries], more: page.more});
  } catch (err) { logs.error = errText(err); }
  logs.busy = false;
  p.render();
}

async function copy(p, text, el) {
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch (err) {
    // No clipboard API (as on a page served over plain http): the old way.
    const area = document.createElement("textarea");
    area.value = text; area.style.cssText = "position:fixed;top:0;left:0;opacity:0";
    p.root.appendChild(area); area.select();
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    area.remove();
  }
  if (!ok) return;
  el.classList.add("done"); el.innerHTML = svg("check");
  setTimeout(() => { el.classList.remove("done"); el.innerHTML = svg("copy"); }, 1200);
}

// The tabs.

export function render(p) {
  const dev = p.dev;
  const refresh = `<button class="tb-btn" id="dev-refresh" title="Refresh" aria-label="Refresh">${svg("refresh")}</button>`;
  const tabs = `<nav class="dv-tabs">${TABS.map(([key, label]) =>
    `<a class="dv-tab ${key === dev.tab ? "on" : ""}" href="${esc(devUrl(key))}" data-nav="1">${label}</a>`).join("")}</nav>`;
  const state = dev.error ? `<div class="conflict">${esc(dev.error)}</div>` : dev.loading ? common.loadingRow("dv-empty dv-loading") : "";
  const body = {find, conversations, runs, health, logs}[dev.tab](dev, p, state);
  return {bar: common.toolbar("Developer", true, refresh), page: `<div class="page wide dv">${tabs}${body}</div>`};
}

export function after(p) {
  if (!p.dev?.toTop) return;  // A new tab or ID starts at the top.
  p.dev.toTop = false;
  requestAnimationFrame(() => { p.scroller().scrollTop = 0; });
}

function find(dev, p, state) {
  const f = dev.find;
  const box = `<form class="dv-search" id="dv-find-form"><input id="dv-find" type="search" value="${esc(dev.q)}" autocomplete="off"
    spellcheck="false" autocapitalize="off" placeholder="Any ID, or its first 4+ characters" aria-label="ID"><button class="btn" type="submit">Find</button></form>`;
  if (!dev.q) {
    return box + `<p class="dv-hint">Runs, role calls, recordings (full ID or 8-character short ID), Home Assistant conversation_ids,
      improvements, cases and day traces (day_YYYYMMDD). Every ID in this view links here.</p>`;
  }
  if (state || !f) return box + state;
  if (f.matches) {
    if (!f.matches.length) {
      return box + `<div class="card dv-card"><div class="dv-empty">No match for “${esc(dev.q)}”${
        dev.q.length < 4 ? ": a prefix needs at least 4 characters" : ""}.</div></div>`;
    }
    return box + `<div class="card dv-card"><div class="dv-card-title">${f.matches.length} matches for “${esc(dev.q)}”</div>
      <div class="dv-matches">${f.matches.map((m) => `<div class="dv-match">${chip(m.kind)}${idLink(m.id, true)}
        <span class="dv-dim">${esc(m.title)}</span></div>`).join("")}</div></div>`;
  }
  const m = f.match, record = {...(f.record || {})}, progress = {...(record.progress || {})};
  delete record.id; delete record.progress;
  // A run's progress: its stage timings as a bar, its counts, and the stages that failed.
  const timings = progress.timings, failed = Array.isArray(progress.problems) ? progress.problems : [];
  delete progress.timings; delete progress.problems;
  const more = stageBar(timings) + (Object.keys(progress).length
    ? `<div class="dv-card-title" style="margin:12px 0 6px">Progress</div>${kv(progress)}` : "")
    + failed.map((x) => `<div class="dv-err">${esc(x.stage)}: ${esc(x.error)}</div>`).join("");
  const head = `<div class="card dv-card"><div class="dv-head">${chip(m.kind, "c-primary")}${idLink(m.id, true)}</div>
    ${m.title ? `<div class="dv-head"><span class="dv-title">${esc(m.title)}</span></div>` : ""}
    ${m.recording ? `<div class="dv-head"><span class="dv-dim">recording</span>${idLink(m.recording, true)}</div>` : ""}
    ${kv(record)}${more}</div>`;
  const events = `<div class="card dv-card"><div class="dv-card-title">Timeline <span class="dv-dim">${f.timeline.length}, oldest first</span></div>
    ${timeline(f.timeline)}</div>`;
  const logsFor = m.kind === "run" ? {run: m.id} : m.recording || m.kind === "recording" ? {recording: m.recording || m.id} : null;
  const entries = `<div class="card dv-card"><div class="dv-card-title">Journal <span class="dv-dim">${f.journal.length}, newest first</span>${
    logsFor ? `<a class="dv-right" href="${esc(devUrl("logs", logsFor))}" data-nav="1">Open in Logs</a>` : ""}</div>${journal(p, f.journal, "dv:find")}</div>`;
  let spans;
  if (f.trace_id) {
    spans = `<div class="card dv-card flush"><div class="dv-card-title">Spans <span class="dv-dim">trace</span>${idLink(f.trace_id)}</div>${
      dev.trace ? waterfall(dev, dev.trace, f.record?.status === "running" || undefined) : `<div class="dv-note">${esc(dev.traceError || "No trace.")}</div>`}</div>`;
  } else {
    const why = m.kind === "recording" || m.recording
      ? "A conversation's spans are in the traces of the runs that analysed it (linked in the timeline)."
      : "No trace for this ID.";
    spans = `<div class="card dv-card"><div class="dv-card-title">Spans</div><div class="dv-note">${esc(why)}</div></div>`;
  }
  return box + head + events + entries + spans;
}

function conversations(dev, p, state) {
  // Every recording, sortable, filtered in place by stage and by any text in its row.
  if (state) return state;
  const conv = dev.conv, rows = [...(dev.rows || [])];
  const keys = {started: (r) => r.started || "", bytes: (r) => r.bytes ?? -1, user_turns: (r) => r.user_turns ?? -1};
  const key = keys[conv.sort] || keys.started;
  rows.sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0) * (conv.desc ? -1 : 1));
  const counts = new Map();
  for (const r of rows) counts.set(r.stage, (counts.get(r.stage) || 0) + 1);
  const stages = [...STAGES.filter((s) => counts.has(s)), ...[...counts.keys()].filter((s) => !STAGES.includes(s))];
  if (conv.stage && !counts.has(conv.stage)) conv.stage = "";
  const chips = [["", "all", rows.length], ...stages.map((s) => [s, s, counts.get(s)])].map(([value, label, n]) =>
    `<button class="dv-fchip ${conv.stage === value ? "on" : ""}" data-stage="${esc(value)}">${esc(label)}<b>${n}</b></button>`).join("");
  const sort = (k, label) => `<button data-sort="${k}" class="${conv.sort === k ? "on" : ""}">${label}${
    conv.sort === k ? (conv.desc ? " &#9662;" : " &#9652;") : ""}</button>`;
  const head = `<thead><tr><th class="sticky">Recording</th><th>Stage</th><th class="n">${sort("bytes", "Size")}</th>
    <th class="n">${sort("user_turns", "Turns")}</th><th>${sort("started", "Started")}</th><th class="n">Goals</th>
    <th class="n">Friction</th><th class="n">Strikes</th><th>Status</th><th>Stored</th><th>Improvements</th><th>Cases</th><th>Runs</th></tr></thead>`;
  const needle = conv.search.trim().toLowerCase();
  let shown = 0, size = 0;
  const body = rows.map((r) => {
    const text = [r.id, r.stage, r.status, r.stored, r.waiting_at, r.practice && "practice", ...(r.improvements || []),
                  ...(r.cases || []), ...(r.runs || [])].filter(Boolean).join(" ").toLowerCase();
    const hit = (!conv.stage || r.stage === conv.stage) && (!needle || text.includes(needle));
    if (hit) { shown += 1; size += Number(r.bytes) || 0; }
    return `<tr data-stage="${esc(r.stage)}" ${hit ? "" : "hidden"}>
      <td class="sticky">${idLink(r.id)}</td>
      <td>${stageChip(r.stage)}${r.waiting_at ? ` <span class="dv-dim">at ${esc(r.waiting_at)}</span>` : ""}${
        r.practice ? ` <span class="dv-tag">practice</span>` : ""}${r.filed ? ` <span class="dv-tag">filed</span>` : ""}</td>
      <td class="n">${bytes(r.bytes)}</td><td class="n">${num(r.user_turns)}</td>
      <td><span title="${esc(r.started)}">${esc(stamp(r.started, "min"))}</span></td>
      <td class="n">${num(r.goals)}</td><td class="n">${num(r.friction)}</td><td class="n">${r.strikes ? num(r.strikes) : ""}</td>
      <td>${esc(r.status)}</td><td>${esc(r.stored)}</td><td>${ids(r.improvements)}</td><td>${ids(r.cases)}</td>
      <td>${ids(r.runs)}</td></tr>`;
  }).join("");
  const count = rows.length ? `${shown === rows.length ? rows.length : `${shown} of ${rows.length}`} · ${bytes(size)}` : "";
  return `<div class="dv-bar"><input id="cv-search" type="search" placeholder="Filter by ID, stage, status, run…" value="${esc(conv.search)}"
      aria-label="Filter conversations" autocomplete="off" spellcheck="false"><span class="dv-dim" id="cv-count">${count}</span></div>
    <div class="dv-chips">${chips}</div>
    <div class="card">${rows.length ? `<div class="dv-scroll"><table class="dv-table">${head}<tbody>${body}</tbody></table></div>`
      : `<div class="dv-empty" style="padding:12px 14px">No recordings.</div>`}</div>`;
}

function runs(dev, p, state) {
  if (state) return state;
  const list = dev.runs || [];
  if (dev.q) return run(dev, p, list.find((r) => r.id === dev.q));
  if (!list.length) return `<div class="card dv-card"><div class="dv-empty">No pipeline runs in the run log.</div></div>`;
  const live = list.some((r) => r.status === "running");
  return `<div class="card dv-card flush"><div class="dv-card-title">Pipeline runs <span class="dv-dim">${list.length}, newest first</span>${
    live ? `<span class="dv-live dv-right">Refreshing every 5 s</span>` : ""}</div>${list.map(runRow).join("")}</div>`;
}

function runRow(r) {
  const live = r.status === "running", took = live ? Date.now() - Date.parse(r.started) : between(r.started, r.finished);
  const recent = Date.now() - Date.parse(r.started) < 86400000;
  const sub = [esc(r.trigger || "?"), r.only && `only ${esc(r.only)}`, live && r.stage && `at ${esc(r.stage)}`,
               r.problems?.length && problemCount(r.problems), !r.trace_available && "no trace"].filter(Boolean);
  return `<div class="dv-run" data-run="${esc(r.id)}" role="link" tabindex="0">
    <div class="dv-run-top">${statusChip(r.status)}<span class="dv-time" title="${esc(r.started)}">${esc(stamp(r.started))}</span>${
      recent ? `<span class="dv-dim" style="font-size:12.5px">${esc(ago(r.started))}</span>` : ""}<span class="dv-right">${dur(took)}</span></div>
    <div class="dv-run-sub">${idLink(r.id)}${sub.map((s) => `<span>${s}</span>`).join("")}</div>
    ${stageBar(r.timings)}${r.error ? `<div class="dv-err">${esc(r.error)}</div>` : ""}</div>`;
}

function run(dev, p, r) {
  const back = `<a class="dv-back" href="${esc(devUrl("runs"))}" data-nav="1">&#8592; All runs</a>`;
  if (!r) {
    return back + `<div class="card dv-card"><div class="dv-empty">No pipeline run ${esc(dev.q)} in the run log.
      <a href="${esc(devUrl("find", {q: dev.q}))}" data-nav="1">Find it</a></div></div>`;
  }
  const live = r.status === "running", took = live ? Date.now() - Date.parse(r.started) : between(r.started, r.finished);
  const head = `<div class="card dv-card"><div class="dv-head">${statusChip(r.status)}<span class="dv-title">Run</span>${
    idLink(r.id, true)}${live ? `<span class="dv-live">Refreshing every 5 s</span>` : ""}</div>
    ${kv({trigger: r.trigger, only: r.only, stage: live ? r.stage : null, started: r.started, finished: r.finished,
          took: dur(took), trace: r.trace_available ? "kept" : "not kept", error: r.error})}${stageBar(r.timings)}</div>`;
  const counts = Object.keys(r.counts || {}).length
    ? `<div class="card dv-card"><div class="dv-card-title">Progress</div>${kv(r.counts)}</div>` : "";
  const t = dev.trace;
  const wf = t ? waterfall(dev, t, live) : `<div class="dv-note">${esc(dev.traceError || (r.trace_available ? "No trace."
    : "No trace for this run: traces are kept for a week, and runs from before tracing have none. The stage timings above come from the run log."))}</div>`;
  const spans = `<div class="card dv-card flush"><div class="dv-card-title">Waterfall</div>${wf}</div>`;
  const failed = r.problems?.length ? `<div class="card dv-card"><div class="dv-card-title">Problems <span class="dv-dim">${r.problems.length}</span></div>
    ${r.problems.map((x) => `<div class="dv-log lv-error"><div class="dv-log-head"><span class="dv-level lv-error">${esc(x.stage)}</span></div>
      <div class="dv-msg">${esc(x.error)}</div></div>`).join("")}</div>` : "";
  const entries = t?.journal || [], shown = dev.journalAll ? entries : entries.slice(0, 200);
  const dropped = t?.dropped?.journal ? `, ${num(t.dropped.journal)} dropped over the cap` : "";
  const entriesCard = t ? `<div class="card dv-card"><div class="dv-card-title">Journal <span class="dv-dim">${num(entries.length)}, newest first${dropped}</span>
    <a class="dv-right" href="${esc(devUrl("logs", {run: r.id}))}" data-nav="1">Open in Logs</a></div>${journal(p, shown, "dv:run")}
    ${shown.length < entries.length ? `<div class="dv-more"><button class="btn ghost" id="journal-all">Show all ${num(entries.length)}</button></div>` : ""}</div>` : "";
  return back + `<div class="dv-grid">${head}${counts}</div>` + spans + failed + entriesCard;
}

function health(dev, p, state) {
  if (state) return state;
  const h = dev.health || {}, disk = h.disk || {}, stores = disk.stores || {}, rec = disk.recordings || {};
  const traces = disk.traces || {}, mem = h.memory || {}, now = h.now || {}, active = now.run || {};
  const storeBytes = Object.values(stores).reduce((sum, n) => sum + (n || 0), 0);
  const diskBytes = storeBytes + (rec.bytes || 0) + (traces.bytes || 0);
  const memParts = Object.entries(mem).filter(([, x]) => x && typeof x === "object" && !Array.isArray(x) && "bytes" in x)
    .sort((a, b) => (b[1].bytes || 0) - (a[1].bytes || 0));
  const memBytes = memParts.reduce((sum, [, x]) => sum + (x.bytes || 0), 0), memMs = memParts.reduce((sum, [, x]) => sum + (x.ms || 0), 0);
  const tile = (label, value, sub = "") => `<div class="dv-tile"><div class="t-label">${esc(label)}</div><div class="t-value">${value}</div>${
    sub ? `<div class="t-sub">${sub}</div>` : ""}</div>`;
  const tiles = [
    tile("On disk", bytes(diskBytes), `stores ${bytes(storeBytes)} · recordings ${bytes(rec.bytes)} · traces ${bytes(traces.bytes)}`),
    tile("Recordings", num(rec.count), `${bytes(rec.bytes)}, largest ${bytes(rec.largest)}`),
    tile("Traces", num(traces.count), bytes(traces.bytes)),
    tile("Memory, estimated", bytes(memBytes), `JSON size of what we hold; took ${dur(memMs)}`),
    tile("HA process peak RSS", bytes(h.process?.max_rss_bytes), "the whole Home Assistant process"),
    tile("Pipeline", active.running ? "running" : "idle", active.running ? `${esc(active.stage || "")} ${idLink(active.id)}` : ""),
    tile("Our asyncio tasks", num((now.tasks || []).length)),
    tile("Sandboxes running", num(now.sandboxes?.running)),
    tile("Pending store writes", num(now.pending_writes)),
  ].join("");
  const table = (head, rows, empty = "None.") => rows.length
    ? `<div class="dv-scroll"><table class="dv-table"><thead><tr>${head.map((label) => {
        const right = label.endsWith(" ");  // A trailing space marks a numeric column.
        return `<th class="${right ? "n" : ""}">${esc(label.trim())}</th>`;
      }).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
    : `<div class="dv-empty dv-flush-empty">${esc(empty)}</div>`;
  const card = (title, sub, inner, wide = false) => `<div class="card dv-card flush ${wide ? "wide" : ""}"><div class="dv-card-title">${
    esc(title)}${sub ? ` <span class="dv-dim">${sub}</span>` : ""}</div>${inner}</div>`;
  const pad = (inner) => `<div class="dv-pad">${inner}</div>`;
  const sub = (title, note = "") => `<div class="dv-card-title dv-sub">${esc(title)}${note ? ` <span class="dv-dim">${esc(note)}</span>` : ""}</div>`;
  const storeRows = [...Object.entries(stores).sort((a, b) => (b[1] || 0) - (a[1] || 0)).map(([name, size]) =>
      `<tr><td class="dv-mono">${esc(name)}</td><td class="n">${bytes(size)}</td></tr>`),
    `<tr><td class="dv-mono">session_* (${num(rec.count)} recordings)</td><td class="n">${bytes(rec.bytes)}</td></tr>`,
    `<tr><td class="dv-mono">trace_* (${num(traces.count)} traces)</td><td class="n">${bytes(traces.bytes)}</td></tr>`];
  const forms = Object.entries(disk.recordings_by_storage || {}).map(([form, n]) => `${esc(form)} ${num(n)}`).join(" · ");
  const diskCard = card("Disk", `${bytes(diskBytes)} in .storage`, table(["Store", "Size "], storeRows) +
    (forms ? `<div class="dv-note dv-pad">Recordings by storage: ${forms}</div>` : ""));
  const practice = mem["practice sessions"], traced = Array.isArray(mem["active traces"]) ? mem["active traces"] : [];
  const memRows = memParts.map(([name, x]) => `<tr><td>${esc(name)}</td><td class="n">${num(x.items)}</td><td class="n">${bytes(x.bytes)}</td>
    <td class="n">${dur(x.ms)}</td></tr>`);
  const traceRows = traced.map((t) => `<tr><td>${idLink(t.id)}</td><td>${esc(t.kind)}</td><td>${esc(stamp(t.started))}</td>
    <td class="n">${num(t.spans)}</td><td class="n">${num(t.journal)}</td><td class="n">${num(t.open)}</td>
    <td class="n">${num((t.dropped?.spans || 0) + (t.dropped?.journal || 0))}</td></tr>`);
  const memCard = card("Memory", `estimated on request as JSON size, ${dur(memMs)}`, table(["Structure", "Items ", "Size ", "Took "], memRows) +
    (practice ? sub("Practice sessions") + pad(kv(practice)) : "") + sub("Active traces", "in memory") +
    table(["Trace", "Kind", "Started", "Spans ", "Journal ", "Open ", "Dropped "], traceRows));
  const tasks = now.tasks || [];
  // sandbox.STATS: counts, and seconds (the *_seconds totals and the slowest_* ones).
  const sandboxes = Object.fromEntries(Object.entries(now.sandboxes || {}).map(([k, x]) =>
    [k, typeof x === "number" && (k.endsWith("seconds") || k.startsWith("slowest")) ? dur(x * 1000) : x]));
  const nowCard = card("Now", "", pad(kv({running: active.running ? "yes" : "no", stage: active.stage, run_id: active.id,
    pending_writes: now.pending_writes})) + sub("Sandboxes") + pad(kv(sandboxes)) + sub("Asyncio tasks", String(tasks.length)) +
    pad(tasks.length ? `<ul class="dv-list">${tasks.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : `<div class="dv-empty">None.</div>`));
  const hotRows = (h.hot || []).map((r) => `<tr><td class="dv-mono">${esc(r.name)}</td><td class="n">${num(r.count)}</td>
    <td class="n">${dur(r.total_ms)}</td><td class="n">${dur(r.count ? r.total_ms / r.count : null)}</td><td class="n">${dur(r.max_ms)}</td>
    <td>${esc(stamp(r.last_at))}</td></tr>`);
  const hotCard = card("Hot spots", "every span and timed section since Home Assistant started, by total time",
    table(["Name", "Count ", "Total ", "Mean ", "Max ", "Last"], hotRows, "Nothing timed yet."), true);
  const tokens = (t) => Object.entries(t || {}).map(([k, x]) => `<div>${esc(k.replace(/_tokens$/, ""))} ${num(x)}</div>`).join("");
  const aiRows = Object.entries(h.ai_calls || {}).sort((a, b) => (b[1].seconds || 0) - (a[1].seconds || 0)).map(([role, r]) =>
    `<tr><td>${esc(role)}</td><td class="n">${num(r.calls)}</td><td class="n ${r.failed ? "bad" : ""}">${num(r.failed)}</td>
    <td class="n">${dur((r.seconds || 0) * 1000)}</td><td class="n">${dur((r.slowest || 0) * 1000)}</td><td>${tokens(r.tokens) || "–"}</td></tr>`);
  const aiCard = card("AI calls", "per role, over the retained run log; tokens as the provider reported them",
    table(["Role", "Calls ", "Failed ", "Total ", "Slowest ", "Tokens"], aiRows, "No AI calls in the run log."), true);
  const flat = (obj, prefix = "") => Object.entries(obj || {}).reduce((out, [k, x]) => x && typeof x === "object" && !Array.isArray(x)
    ? {...out, ...flat(x, `${prefix}${k}.`)} : {...out, [`${prefix}${k}`]: x}, {});
  const retentionCard = card("Last tidy-up", "", pad(kv(flat(h.retention), "No tidy-up since Home Assistant started.")));
  const processCard = card("Process", "", pad(kv({version: h.version, peak_rss: bytes(h.process?.max_rss_bytes)}) +
    (h.process?.note ? `<p class="dv-note" style="margin:8px 0 0">${esc(h.process.note)}</p>` : "")));
  return `<div class="dv-tiles">${tiles}</div><div class="dv-grid">${diskCard}${memCard}${hotCard}${aiCard}${nowCard}
    <div>${retentionCard}${processCard}</div></div>`;
}

function logs(dev, p, state) {
  const l = dev.logs, f = l.filters, filtered = Object.values(f).some(Boolean);
  const form = `<form class="dv-filters" id="dv-log-form">
    <label>Min level<select id="lg-level"><option value="">any</option>${LEVELS.map((x) =>
      `<option value="${x}" ${f.level === x ? "selected" : ""}>${x}</option>`).join("")}</select></label>
    <label>Text<input id="lg-text" type="search" value="${esc(f.text)}" placeholder="in message or traceback"></label>
    <label>Run<input id="lg-run" value="${esc(f.run)}" placeholder="run ID" autocomplete="off" spellcheck="false"></label>
    <label>Conversation<input id="lg-recording" value="${esc(f.recording)}" placeholder="recording ID or conversation_id"
      autocomplete="off" spellcheck="false"></label>
    <div class="actions">${filtered ? `<button class="link" type="button" id="lg-clear">Clear</button>` : ""}
      <button class="btn" type="submit" ${l.busy ? "disabled" : ""}>Apply</button></div></form>`;
  if (state) return form + state;
  const entries = l.entries || [];
  const list = entries.length ? journal(p, entries, "dv:log")
    : `<div class="dv-empty">No journal entries${filtered ? " match these filters" : ""}.</div>`;
  return form + `<div class="card dv-card"><div class="dv-card-title">Journal <span class="dv-dim">${num(entries.length)}${l.more ? "+" : ""},
    newest first, across the retained traces</span></div>${list}${l.error ? `<div class="error-text">${esc(l.error)}</div>` : ""}
    ${l.more ? `<div class="dv-more"><button class="btn ghost" id="lg-more" ${l.busy ? "disabled" : ""}>${
      l.busy ? "Loading…" : "Load more"}</button></div>` : ""}</div>`;
}

const toggle = (set, id) => { set.has(id) ? set.delete(id) : set.add(id); };
const openRun = (p, el) => p.nav(devUrl("runs", {q: el.dataset.run}));

export const actions = {
  click: [
    ["a[data-nav]", (p, el, ev) => {
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button) return;  // A new tab or window.
      ev.preventDefault();
      p.nav(el.getAttribute("href"));
    }],
    ["[data-copy]", (p, el, ev) => { ev.preventDefault(); copy(p, el.dataset.copy, el); }],
    ["#dev-refresh", (p) => p.load()],
    ["[data-run]", openRun],
    ["[data-wf]", (p, el) => { toggle(p.dev.flipped, el.dataset.wf); p.render(); }],
    ["[data-wfi]", (p, el) => { toggle(p.dev.shown, el.dataset.wfi); p.render(); }],
    ["#wf-depth", (p) => { p.dev.depth = p.dev.depth > WF_DEPTH ? WF_DEPTH : Infinity; p.dev.flipped = new Set(); p.render(); }],
    ["#journal-all", (p) => { p.dev.journalAll = true; p.render(); }],
    ["button[data-stage]", (p, el) => { p.dev.conv.stage = el.dataset.stage; p.render(); }],
    ["[data-sort]", (p, el) => {
      const conv = p.dev.conv, key = el.dataset.sort;
      conv.desc = conv.sort === key ? !conv.desc : true; conv.sort = key;
      p.render();
    }],
    ["#lg-clear", (p) => applyLogs(p, {...NO_FILTERS})],
    ["#lg-more", (p) => moreLogs(p)],
  ],
  keydown: [["[data-run]", (p, el, ev) => { if (ev.key === "Enter" && ev.target === el) openRun(p, el); }]],
  input: [["#cv-search", (p, el) => { p.dev.conv.search = el.value; p.render(); }]],  // Typing keeps its focus.
  submit: [
    ["#dv-find-form", (p, el, ev) => { ev.preventDefault(); p.nav(devUrl("find", {q: p.root.getElementById("dv-find").value.trim()})); }],
    ["#dv-log-form", (p, el, ev) => {
      ev.preventDefault();
      const value = (id) => p.root.getElementById(id).value;
      applyLogs(p, {level: value("lg-level"), text: value("lg-text"), run: value("lg-run"), recording: value("lg-recording")});
    }],
  ],
};
