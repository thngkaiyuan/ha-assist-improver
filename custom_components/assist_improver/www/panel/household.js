// Household knowledge: what the assistants know about the household. A modal, since the list grows: everyone's lines,
// then each person's; each clamped to two lines (Read more), editable in place, searchable once the list is long.

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, plural, fill}, {TEXT}, {glyph}] = await Promise.all(
  ["format", "text", "icons"].map((name) => import(`./${name}.js${v}`)));

const fresh = () => ({open: new Set(), edit: null, remove: null, adding: null, draft: "", error: null, busy: false, search: ""});
const reset = (st) => Object.assign(st, fresh(), {open: st.open, search: st.search});
const split = (key) => { const at = key.lastIndexOf(":"); return [key.slice(0, at), Number(key.slice(at + 1))]; };
const linesOf = (h, scope) => scope === "everyone" ? h.everyone || [] : ((h.people || []).find((x) => x.user_id === scope) || {}).lines || [];

export function modal(p) {
  const h = p.household || {}, K = TEXT.knowledge, st = p.kn;
  const sections = [{scope: "everyone", name: K.everyone, lines: h.everyone || []},
                    ...(h.people || []).map((x) => ({scope: x.user_id, name: x.name, lines: x.lines}))];
  const me = h.me;
  if (me && st.adding === me.user_id && !sections.some((s) => s.scope === me.user_id)) sections.push({scope: me.user_id, name: me.name, lines: []});
  const q = st.search.trim().toLowerCase(), hit = (text) => !q || text.toLowerCase().includes(q);
  const editor = () => `<div class="kn-editor"><textarea id="kn-text" placeholder="${esc(K.placeholder)}">${esc(st.draft)}</textarea>
    ${st.error ? `<div class="error-text">${esc(st.error)}</div>` : ""}
    <div class="kn-buttons"><button class="btn ghost" id="kn-cancel">${esc(K.cancel)}</button><button class="btn" id="kn-save" ${st.busy ? "disabled" : ""}>${esc(K.save)}</button></div></div>`;
  const line = (scope, i, text) => {
    const key = `${scope}:${i}`;
    if (st.edit === key) return `<li class="kn-line editing">${editor()}</li>`;
    const open = st.open.has(key);
    const confirm = st.remove === key ? `<div class="kn-confirm"><span>${esc(K.confirmRemove)}</span>
      <button class="link" id="kn-cancel">${esc(K.cancel)}</button><button class="btn danger" id="kn-remove" ${st.busy ? "disabled" : ""}>${esc(K.remove)}</button></div>` : "";
    return `<li class="kn-line" ${hit(text) ? "" : "hidden"}><div class="kn-row"><div class="kn-main"><div class="kn-text ${open ? "" : "clamp"}">${esc(text)}</div>
      <button class="link kn-more" data-kn-more="${esc(key)}">${esc(open ? K.less : K.more)}</button></div>
      <div class="kn-icons"><button class="kn-icon" data-kn-edit="${esc(key)}" aria-label="${esc(K.edit)}" data-tip="${esc(K.edit)}">${glyph("mdi:pencil-outline")}</button>
      <button class="kn-icon" data-kn-ask="${esc(key)}" aria-label="${esc(K.remove)}" data-tip="${esc(K.remove)}">${glyph("mdi:trash-can-outline")}</button></div></div>${confirm}</li>`;
  };
  // While searching, a section without a match steps aside; the line being edited always stays.
  const shown = (s) => !q || s.lines.some((text, i) => hit(text) || st.edit === `${s.scope}:${i}`);
  const group = (s) => `<section class="kn-section" ${shown(s) ? "" : "hidden"}><div class="kn-group">${esc(s.name)}<span>${s.lines.length || ""}</span></div>
    <ul>${s.lines.map((text, i) => line(s.scope, i, text)).join("")}
    ${st.adding === s.scope ? `<li class="kn-line editing">${editor()}</li>`
      : `<li class="kn-add"><button class="link" data-kn-add="${esc(s.scope)}">+ ${esc(K.add)}</button></li>`}</ul></section>`;
  const forMe = me && !sections.some((s) => s.scope === me.user_id)
    ? `<div class="kn-add kn-add-me"><button class="link" data-kn-add="${esc(me.user_id)}">+ ${esc(fill(K.addFor, {name: me.name}))}</button></div>` : "";
  const none = !sections.some((s) => s.lines.some(hit));
  return `<div class="modal-shade" id="backdrop"></div><div class="modal" role="dialog" aria-modal="true" aria-label="${esc(K.title)}">
    <div class="modal-head">${glyph("mdi:book-heart-outline")}<b>${esc(K.title)}</b><span class="kn-count">${plural(h.lines || 0, "line")}</span>
      <button class="modal-close" id="modal-close" aria-label="Close">&#10005;</button></div>
    ${(h.lines || 0) > 6 ? `<input class="kn-search" id="kn-search" type="search" placeholder="${esc(K.search)}">` : ""}
    <div class="modal-body">${h.lines ? "" : `<p class="kn-empty">${esc(K.empty)}</p>`}${sections.map(group).join("")}${forMe}
    <div class="pop-lead kn-none" ${q && none ? "" : "hidden"}>${esc(K.none)}</div></div></div>`;
}

export function after(p) {
  // Read more only where a line is cut.
  if (p.pop?.kind !== "knowledge") return;
  p.root.querySelectorAll(".kn-text.clamp").forEach((el) => {
    const more = el.parentElement.querySelector(".kn-more");
    if (more) more.hidden = el.scrollHeight <= el.clientHeight + 1;
  });
}

function begin(p, change) {
  // Opens one line's editor (or the delete question), closing any other; what it edits is focused.
  reset(p.kn);
  Object.assign(p.kn, change);
  p.render();
  p.root.getElementById("kn-text")?.focus();
}

async function send(p, msg) {
  const st = p.kn;
  st.busy = true; st.error = null;
  p.render();
  try {
    await p.ws("household/edit", msg);
    p.household = await p.ws("household");
    reset(st);
  } catch (err) {
    st.busy = false; st.error = err.message || String(err);  // What was typed stays, to try again.
  }
  p.render();
}

export const actions = {
  click: [
    ["[data-knowledge]", (p) => { p.pop = {kind: "knowledge", anchor: "[data-knowledge]"}; p.kn = fresh(); p.render(); }],
    ["#modal-close", (p) => { p.pop = null; p.render(); }],
    ["[data-kn-more]", (p, el) => {
      const open = p.kn.open, key = el.dataset.knMore;
      open.has(key) ? open.delete(key) : open.add(key);
      p.render();
    }],
    ["[data-kn-edit]", (p, el) => {
      const [scope, index] = split(el.dataset.knEdit);
      begin(p, {edit: el.dataset.knEdit, draft: linesOf(p.household || {}, scope)[index] ?? ""});
    }],
    ["[data-kn-ask]", (p, el) => begin(p, {remove: el.dataset.knAsk})],
    ["[data-kn-add]", (p, el) => begin(p, {adding: el.dataset.knAdd})],
    ["#kn-cancel", (p) => { reset(p.kn); p.render(); }],
    ["#kn-save", (p) => {
      const st = p.kn, text = p.root.getElementById("kn-text")?.value || "";
      if (st.adding) send(p, {scope: st.adding, action: "add", text});
      else if (st.edit) {
        const [scope, index] = split(st.edit);
        send(p, {scope, action: "update", index, old: linesOf(p.household || {}, scope)[index], text});
      }
    }],
    ["#kn-remove", (p) => {
      const [scope, index] = split(p.kn.remove);
      send(p, {scope, action: "delete", index, old: linesOf(p.household || {}, scope)[index]});
    }],
  ],
  input: [
    ["#kn-text", (p, el) => { p.kn.draft = el.value; }],
    ["#kn-search", (p, el) => { p.kn.search = el.value; p.render(); }],  // Filters in place, keeping the cursor.
  ],
};
