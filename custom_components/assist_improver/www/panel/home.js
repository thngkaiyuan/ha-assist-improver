// Home: one thing at a time. What to review, else a run to start, else all caught up; a run in progress on top, and
// earlier improvements folded away below.

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, plural}, {TEXT, STATUS}, {svg, COG}, common] = await Promise.all([
  import(`./format.js${v}`), import(`./text.js${v}`), import(`./icons.js${v}`), import(`./common.js${v}`)]);

const rows = (items) => items.map((i) => {
  const gains = [i.enabled && `Enables ${plural(i.enabled, "task")}`, i.removed && `${plural(i.removed, "fewer turn")}`].filter(Boolean).join(" · ");
  return `<div class="item" data-id="${esc(i.id)}"><span class="title">${esc(i.title)}
      ${gains ? `<div class="gains">${esc(gains)}</div>` : ""}</span>
      <span class="chip ${esc(i.status)}">${esc(STATUS[i.status] || i.status)}</span></div>`;
}).join("");

function offer(p) {
  // Household knowledge, offered to the assistants that don't read it yet.
  const h = p.household;
  const missing = h && h.lines ? h.assistants.filter((a) => !a.using) : [];
  if (!missing.length) return "";
  return `<div class="offer"><div><b>Household knowledge</b> · ${plural(h.lines, "line")} learned so far.
      These assistants don't read it yet:</div>
      ${missing.map((a) => {
        const busy = p.turningOn === a.target;
        return `<div class="offer-row"><span>${esc(a.title)}</span>
        <button class="turn-on" data-target="${esc(a.target)}" ${busy ? "disabled" : ""}>${busy ? "Turning on…" : "Turn on"}</button></div>`;
      }).join("")}</div>`;
}

function main(p, items) {
  const {run, preview} = p.run || {};
  const pending = items.filter((i) => i.pending);
  const running = run?.status === "running";
  const waiting = preview?.conversations_to_analyze || 0;
  const toWrite = preview?.backlog?.write || 0;
  const error = common.failure(p, run);
  if (pending.length) {
    const line = running ? "" : `<div class="run-line"><span>${esc(common.lastRun(run) || "No runs yet")}${
      waiting ? ` · ${plural(waiting, "new conversation")} waiting` : ""}</span>
      ${waiting ? `<button class="link" id="run-now">Run now</button>` : ""}</div>`;
    return `<h1>${plural(pending.length, "improvement")} to review</h1>
      <p class="lead">Each was tested on your own conversations. Open one, check what changes, and decide.</p>
      <div class="card">${rows(pending)}</div>${line}${error}`;
  }
  if (running) return "";
  if (toWrite) {
    return `<div class="hero"><div class="hero-title">${plural(toWrite, "improvement")} waiting to be written up</div>
      <div class="lead">${(preview.paused || []).includes("write") ? "Write up is blocked. " : ""}They go up for review
      once written in plain words.</div>${error}<button class="btn play" id="run-now">${svg("play")}${esc(TEXT.runNow)}</button></div>`;
  }
  if (waiting) {
    return `<div class="hero"><div class="hero-title">${plural(waiting, "new conversation")} since your last run</div>
      <div class="lead">The improver finds where Assist could've done better, and tests fixes for it.
      ${run ? `${esc(common.lastRun(run))}.` : ""}</div>${error}
      <button class="btn play" id="run-now">${svg("play")}${esc(TEXT.runNow)}</button></div>`;
  }
  if (!preview?.conversations_recorded) {
    return `<div class="hero"><div class="hero-title">Use Assist as usual</div>
      <div class="lead">The improver learns from your conversations with Assist. Once you've had a few, run it here.</div></div>`;
  }
  return `<div class="hero"><div class="hero-title">All caught up</div>
      <div class="lead">Nothing to review, and no new conversations since your last run. ${run ? `${esc(common.lastRun(run))}.` : ""}</div>${error}
      <button class="link" id="run-anyway">Run anyway</button></div>`;
}

export function render(p) {
  const items = p.list, {run, preview} = p.run || {};
  const running = run?.status === "running";
  const earlier = items.filter((i) => !i.pending);
  const history = earlier.length ? `<details class="history" data-key="history" ${p.open.has("history") ? "open" : ""}>
      <summary><span class="name">Earlier improvements (${earlier.length})</span></summary>
      <div class="card">${rows(earlier)}</div></details>` : "";
  const settings = `<button class="tb-btn" id="open-settings" title="Settings" aria-label="Settings">
      <svg viewBox="0 0 24 24"><path d="${COG}"/></svg></button>`;
  return {
    bar: common.toolbar("Assist Improver", false, (p.developer ? common.devLink() : "") + settings),
    page: `<div class="page">${running ? common.progress(run, p.cancelling) : ""}${offer(p)}${main(p, items)}${history}</div>`,
    layer: p.confirming && preview ? common.confirm(p, preview) : "",
  };
}

async function turnOn(p, target) {
  p.turningOn = target;
  p.render();
  try {
    await p.ws("household/enable", {target});
  } catch (err) {
    alert(err.message || String(err));
  }
  p.turningOn = null;
  await p.load();
}

export const actions = {
  click: [
    [".item", (p, el) => p.go(el.dataset.id)],
    [".turn-on", (p, el) => turnOn(p, el.dataset.target)],
  ],
};
