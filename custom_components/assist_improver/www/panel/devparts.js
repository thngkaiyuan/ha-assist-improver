// The developer view's building blocks: IDs (a link to Find, and a copy button), values, tags, chips, timelines,
// journal entries, a run's stage bar, and the waterfall of a trace's spans. Journal text and tags are untrusted:
// every value goes through esc().

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, plural, ago, cls, bytes, dur, num, isIso, stamp, devUrl}, {svg}] = await Promise.all(
  ["format", "icons"].map((name) => import(`./${name}.js${v}`)));

// The keys whose values are IDs (each a link to Find).
const ID_KEYS = new Set(["id", "run_id", "recording_id", "improvement_id", "case_id", "trace_id", "generator_run", "role_call",
                         "run", "recording", "improvement", "case", "conversation_id"]);
const RUN_STAGES = ["analyze", "propose", "test", "write"];
export const WF_DEPTH = 2;  // Waterfall levels shown open at first; deeper ones start collapsed.
const WF_HINTS = ["recording_id", "goal", "improvement_id", "case_id", "hypothesis", "replay", "turn", "tool", "role"];  // First beside a span's name.
const WF_ROWS = 1500;  // Waterfall rows drawn at most (a day's trace can hold thousands of spans).

export function idLink(id, full = false, label = null) {
  if (id == null || id === "") return "";
  const text = String(id), shown = label ?? (full || text.length <= 12 ? text : text.slice(0, 8));
  return `<span class="dv-id"><a href="${esc(devUrl("find", {q: text}))}" data-nav="1" title="${esc(text)}">${esc(shown)}</a><button
    class="dv-copy" data-copy="${esc(text)}" title="Copy" aria-label="Copy ${esc(text)}">${svg("copy")}</button></span>`;
}

export const ids = (list) => (list || []).map((id) => idLink(id)).join(" ");

function when(iso) {
  // A time to the millisecond, how long ago when that's today's news, and the exact ISO time on hover.
  const recent = Date.now() - Date.parse(iso) < 86400000;
  return `<span title="${esc(iso)}">${esc(stamp(iso, "ms"))}</span>${recent ? ` <span class="dv-dim">${esc(ago(iso))}</span>` : ""}`;
}

function val(key, value) {
  if (value == null || value === "") return `<span class="dv-dim">–</span>`;
  if (ID_KEYS.has(key) && typeof value === "string") return idLink(value, true);
  if (ID_KEYS.has(key) && Array.isArray(value) && value.every((x) => typeof x === "string")) return ids(value);
  if (isIso(value)) return when(value);
  if (typeof value === "object") return `<code class="dv-json">${esc(JSON.stringify(value, null, 2))}</code>`;
  if (typeof value === "number") return esc(/bytes$/.test(key) ? bytes(value) : num(value));
  return esc(String(value));
}

export function kv(obj, empty = "Nothing recorded.") {
  const rows = Object.entries(obj || {}).filter(([, x]) => x != null && x !== "" && !(typeof x === "object" && !Object.keys(x).length));
  if (!rows.length) return `<div class="dv-empty">${esc(empty)}</div>`;
  return `<dl class="dv-kv">${rows.map(([k, x]) => `<dt>${esc(k)}</dt><dd>${val(k, x)}</dd>`).join("")}</dl>`;
}

export function tags(list) {
  return Object.entries(list || {}).filter(([, x]) => x != null && x !== "").map(([k, x]) => {
    if (ID_KEYS.has(k) && typeof x === "string") return `<span class="dv-tag">${esc(k)} ${idLink(x)}</span>`;
    if (k === "goal" && typeof x === "string" && /^[0-9a-f]{8}:/.test(x)) {  // A goal names its recording's short ID.
      return `<span class="dv-tag">goal ${idLink(x.slice(0, 8), false, x)}</span>`;
    }
    return `<span class="dv-tag">${esc(k)}=${esc(typeof x === "object" ? JSON.stringify(x) : x)}</span>`;
  }).join("");
}

export const chip = (text, color = "") => `<span class="dv-chip ${color}">${esc(text)}</span>`;

export function statusChip(status) {
  const color = {running: "c-primary", done: "c-green", completed: "c-green", ok: "c-green", failed: "c-red",
                 error: "c-red", cancelled: "c-amber", interrupted: "c-amber", held: "c-amber"};
  return chip(status || "?", Object.hasOwn(color, status) ? color[status] : "");
}

export function stageChip(stage) {
  const color = {active: "c-primary", waiting: "c-amber", held: "c-amber", failing: "c-red", set_aside: "c-red",
                 analysed: "c-blue", improvement: "c-green"};
  return chip(stage, Object.hasOwn(color, stage) ? color[stage] : "");
}

export function timeline(events) {
  if (!events?.length) return `<div class="dv-empty">Nothing recorded.</div>`;
  const color = {run: "c-primary", finished: "c-primary", analysis: "c-blue", proposal: "c-blue", role_call: "c-blue",
                 problem: "c-red", case: "c-amber", test: "c-amber", improvement: "c-green", decision: "c-green"};
  return events.map((e) => `<div class="dv-tl-row"><span class="dv-time" title="${esc(e.at)}">${esc(stamp(e.at))}</span>
    ${chip(e.kind, Object.hasOwn(color, e.kind) ? color[e.kind] : "")}
    <div class="dv-what">${esc(e.what)}${Object.keys(e.ids || {}).length ? `<div class="dv-tags">${tags(e.ids)}</div>` : ""}</div></div>`).join("");
}

function entry(p, e, key) {
  const level = cls(e.level || "info");
  const extra = [e.trace_id && `<span class="dv-tag">trace ${idLink(e.trace_id)}</span>`,
                 e.span && `<span class="dv-tag">span ${esc(e.span)}</span>`,
                 e.logger && `<span class="dv-tag">${esc(e.logger)}</span>`].filter(Boolean).join("");
  const all = tags(e.tags) + extra;
  const exc = e.exception ? `<details data-key="${esc(key)}" ${p.open.has(key) ? "open" : ""}><summary>Traceback</summary>
    <pre>${esc(e.exception)}</pre></details>` : "";
  return `<div class="dv-log lv-${level}"><div class="dv-log-head"><span class="dv-time" title="${esc(e.at)}">${esc(stamp(e.at, "ms"))}</span>
    <span class="dv-level lv-${level}">${esc(e.level)}</span></div><div class="dv-msg">${esc(e.message)}</div>
    ${all ? `<div class="dv-tags">${all}</div>` : ""}${exc}</div>`;
}

export function journal(p, entries, prefix) {
  if (!entries?.length) return `<div class="dv-empty">No journal entries.</div>`;
  return entries.map((e, i) => entry(p, e, `${prefix}:${e.at}:${i}`)).join("");
}

export function stageBar(timings) {
  // A run's seconds per stage (from the run log, kept a year): a bar and its legend.
  const parts = [...RUN_STAGES.filter((s) => timings?.[s] != null).map((s) => [s, Number(timings[s]) || 0]),
                 ...Object.entries(timings || {}).filter(([s]) => !RUN_STAGES.includes(s)).map(([s, x]) => [s, Number(x) || 0])];
  if (!parts.length) return "";
  const total = parts.reduce((sum, [, x]) => sum + x, 0);
  return `<div class="dv-stages">${parts.map(([s, x]) => `<i class="s-${cls(s)}" style="width:${total ? (100 * x / total).toFixed(2) : 0}%"></i>`).join("")}</div>
    <div class="dv-legend">${parts.map(([s, x]) => `<span><i class="s-${cls(s)}"></i>${esc(s)} ${dur(x * 1000)}</span>`).join("")}</div>`;
}

export const problemCount = (list) => `<span style="color:var(--del-fg)">${plural(list.length, "problem")}</span>`;

// The waterfall: a trace's spans as a tree, each a row indented by depth, its bar placed by start and length.

export function waterfall(dev, trace, live) {
  const now = Date.now(), updated = Date.parse(trace.updated) || now;
  // Spans still open: going on in an active trace (drawn up to now), else cut off when it was last saved.
  const going = live ?? (!trace.finished && now - updated < 120000);
  const nodes = new Map();
  for (const s of trace.spans || []) nodes.set(s.id, {...s, start: Date.parse(s.at), kids: []});
  for (const s of trace.open || []) {
    const start = Date.parse(s.at);
    nodes.set(s.id, {...s, start, ms: Math.max(0, (going ? now : updated) - start), status: going ? "running" : "cut off",
                     open: true, kids: []});
  }
  if (!nodes.size) return `<div class="dv-note dv-flush-empty">No spans in this trace.</div>`;
  const roots = [];
  let t0 = Infinity, t1 = -Infinity;
  for (const node of nodes.values()) {
    const parent = node.parent ? nodes.get(node.parent) : null;
    (parent && parent !== node ? parent.kids : roots).push(node);
    if (isFinite(node.start)) { t0 = Math.min(t0, node.start); t1 = Math.max(t1, node.start + (node.ms || 0)); }
  }
  const total = Math.max(t1 - t0, 1), order = (a, b) => a.start - b.start, rows = [], seen = new Set();
  let hidden = 0;
  const walk = (node, depth, parentTags) => {
    if (seen.has(node.id)) return;
    seen.add(node.id);
    node.kids.sort(order);
    const open = node.kids.length > 0 && ((depth < dev.depth) !== dev.flipped.has(node.id));
    if (rows.length < WF_ROWS) rows.push(row(dev, node, depth, open, parentTags, t0, total));
    else hidden += 1;
    if (open) for (const kid of node.kids) walk(kid, depth + 1, node.tags || {});
  };
  for (const root of roots.sort(order)) walk(root, 0, {});
  const dropped = trace.dropped || {}, opened = (trace.open || []).length;
  const tools = `<div class="wf-tools"><span>${num(nodes.size)} spans over ${dur(total)}, from ${esc(stamp(new Date(t0).toISOString()))}</span>
    ${opened ? `<span>${opened} ${going ? "still open" : "cut off"}</span>` : ""}
    ${dropped.spans ? `<span style="color:var(--del-fg)">${num(dropped.spans)} dropped over the cap</span>` : ""}
    <button class="link" id="wf-depth">${dev.depth > WF_DEPTH ? "Collapse deep levels" : "Expand all"}</button></div>`;
  const axis = `<div class="wf-row wf-axis"><div class="wf-name"><span class="wf-sp"></span>Span</div><div class="wf-ms">Took</div>
    <div class="wf-track wf-scale"><span style="left:3px">0</span><span class="wf-mid">${dur(total / 2)}</span>
    <span style="right:0">${dur(total)}</span></div></div>`;
  const cap = hidden ? `<div class="wf-cap">${num(hidden)} more rows not drawn (at most ${num(WF_ROWS)}): collapse some levels.</div>` : "";
  return `<div class="wf">${tools}${axis}${rows.join("")}${cap}</div>`;
}

function row(dev, node, depth, open, parentTags, t0, total) {
  const left = Math.min(Math.max(((node.start - t0) / total) * 100, 0), 100);
  const width = Math.min(Math.max(((node.ms || 0) / total) * 100, 0), 100 - left);
  // Beside the name, the tags this span adds to its parent's (context tags repeat down the tree).
  const rank = (k) => (WF_HINTS.indexOf(k) + WF_HINTS.length + 1) % (WF_HINTS.length + 1);  // Unlisted tags last.
  const hint = Object.entries(node.tags || {}).filter(([k, x]) => k !== "run_id" && parentTags[k] !== x)
    .sort(([a], [b]) => rank(a) - rank(b)).slice(0, 3)
    .map(([k, x]) => `${k}=${typeof x === "object" ? JSON.stringify(x) : ID_KEYS.has(k) && String(x).length > 12 ? String(x).slice(0, 8) : x}`).join(" ");
  const toggle = node.kids.length ? `<button class="wf-tog" data-wf="${esc(node.id)}" aria-expanded="${open}"
    aria-label="${open ? "Collapse" : "Expand"} ${esc(node.name)}">${open ? "&#9662;" : "&#9656;"}</button>` : `<span class="wf-sp"></span>`;
  const count = node.kids.length && !open ? ` <span class="dv-dim">+${num(descendants(node))}</span>` : "";
  const detail = dev.shown.has(node.id) ? `<div class="wf-detail">${kv({name: node.name, status: node.status,
    error: node.error, started: node.at, took: dur(node.ms), span: node.id, "parent span": node.parent})}
    ${Object.keys(node.tags || {}).length ? `<div class="dv-tags">${tags(node.tags)}</div>` : ""}</div>` : "";
  return `<div class="wf-row st-${cls(node.status)} ${node.open ? "open" : ""}"><div class="wf-name" style="--d:${Math.min(depth, 10)}">${toggle}<a
    class="wf-label" data-wfi="${esc(node.id)}" title="${esc(node.name)}${hint ? ` ${esc(hint)}` : ""}">${esc(node.name)}${
    hint ? ` <span class="dv-dim">${esc(hint)}</span>` : ""}${count}</a></div><div class="wf-ms">${dur(node.ms)}</div>
    <div class="wf-track"><i style="left:${left.toFixed(3)}%;width:${width.toFixed(3)}%"></i></div></div>${detail}`;
}

const descendants = (node) => node.kids.reduce((n, kid) => n + 1 + descendants(kid), 0);
