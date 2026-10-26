// Home: what to review first, earlier improvements folded away, then your conversations, to find the ones that didn't
// go well and send them in (pick and Run, conversations.js); a run in progress on top.

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, plural}, {TEXT, STATUS}, {COG}, common, conversations] = await Promise.all([
  import(`./format.js${v}`), import(`./text.js${v}`), import(`./icons.js${v}`), import(`./common.js${v}`),
  import(`./conversations.js${v}`)]);

const rows = (items) => items.map((i) => {
  const gains = [i.enabled && `${plural(i.enabled, "new thing")} Assist can do`, i.removed && `${plural(i.removed, "fewer turn")}`].filter(Boolean).join(" · ");
  // A revision of an earlier improvement says so beside its title.
  return `<button class="item" type="button" data-id="${esc(i.id)}"><span class="title">${esc(i.title)}${
      i.revision ? ` <span class="tag">Revision</span>` : ""}
      ${gains ? `<span class="gains">${esc(gains)}</span>` : ""}</span>
      <span class="chip ${esc(i.status)}">${esc(STATUS[i.status] || i.status)}</span></button>`;
}).join("");

function main(p, items) {
  // What waits for the household's decision, then their conversations.
  const {run} = p.run || {};
  const pending = items.filter((i) => i.pending);
  const reviews = pending.length ? `<h1>${plural(pending.length, "improvement")} to review</h1>
      <p class="lead">Each was tested on your own conversations. Open one, check what changes, and decide.</p>
      <div class="card">${rows(pending)}</div>` : "";
  return `${reviews}${common.failure(p, run)}${history(p, items)}${conversations.section(p)}`;
}

function history(p, items) {
  // Improvements already decided, folded away (above the conversations, which can run long).
  const earlier = items.filter((i) => !i.pending);
  return earlier.length ? `<details class="history" data-key="history" ${p.open.has("history") ? "open" : ""}>
      <summary><span class="name">Earlier improvements (${earlier.length})</span></summary>
      <div class="card">${rows(earlier)}</div></details>` : "";
}

export function render(p) {
  const items = p.list, {run, preview} = p.run || {};
  const running = run?.status === "running";
  const settings = `<button class="tb-btn" id="open-settings" title="Settings" aria-label="Settings">
      <svg viewBox="0 0 24 24"><path d="${COG}"/></svg></button>`;
  return {
    bar: common.toolbar("Assist Improver", false, (p.developer ? common.devLink() : "") + settings),
    page: `<div class="page">${running ? common.progress(run, p.cancelling) : ""}${main(p, items)}</div>`,
    layer: (p.confirming && preview ? common.confirm(p, p.picking ? conversations.runPreview(p, preview) : preview) : "")
      + (p.confirming ? "" : conversations.bar(p, preview, running)),
  };
}

export function after(p) {
  conversations.after(p);
}

export const actions = {
  click: [
    [".item", (p, el) => p.go(el.dataset.id)],
  ],
};
