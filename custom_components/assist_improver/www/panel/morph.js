// Drawing by patching: morph() brings a live DOM tree in line with new HTML and changes only what differs, so an
// update (a poll, a toggle, a list that grew) leaves the rest alone: focus, a text field's caret and what's typed in
// it, scroll positions, open menus and running animations all survive.
//
// Old and new elements pair up by id (or data-key, data-id, data-step), else by tag and first class, in order. What
// stays as it is: a form control keeps its live value unless the panel's own value for it changed (a switch always
// shows the setting), an element marked data-keep keeps its children (the panel fills it itself), and a custom
// element keeps attributes the panel never gave it (it may set its own).

const KEYS = ["id", "data-key", "data-id", "data-step"];
const LOOKAHEAD = 8;  // How far ahead an unkeyed element looks for its old self before it's made anew.

export function morph(target, html) {
  const template = document.createElement("template");  // Inert: nothing in it loads or runs until it's placed.
  template.innerHTML = html;
  patchChildren(target, template.content);
}

const keyOf = (node) => {
  if (node.nodeType !== Node.ELEMENT_NODE) return null;
  for (const name of KEYS) {
    const value = node.getAttribute(name);
    if (value != null) return `${name}=${value}`;
  }
  return null;
};

const kindOf = (node) => node.nodeType === Node.ELEMENT_NODE
  ? `${node.nodeName} ${(node.getAttribute("class") || "").trim().split(/\s+/)[0]}` : `#${node.nodeType}`;

function find(cursor, node) {
  // The old node that becomes `node`: the one with its key, else the next one of its kind (not far ahead).
  const key = keyOf(node);
  if (key) {
    for (let old = cursor; old; old = old.nextSibling) if (keyOf(old) === key && old.nodeName === node.nodeName) return old;
    return null;
  }
  const kind = kindOf(node);
  for (let old = cursor, n = 0; old && n < LOOKAHEAD; old = old.nextSibling, n++) {
    if (!keyOf(old) && kindOf(old) === kind) return old;
  }
  return null;
}

function patchChildren(live, next) {
  let cursor = live.firstChild;
  for (let node = next.firstChild; node;) {
    const following = node.nextSibling;  // Read first: placing `node` moves it out of `next`.
    const old = find(cursor, node);
    if (!old) {
      remember(node);
      live.insertBefore(node, cursor);
    } else {
      if (old === cursor) cursor = cursor.nextSibling;
      else live.insertBefore(old, cursor);
      patch(old, node);
    }
    node = following;
  }
  while (cursor) {
    const gone = cursor;
    cursor = cursor.nextSibling;
    gone.remove();
  }
}

function patch(live, next) {
  if (live.nodeType !== Node.ELEMENT_NODE) {
    if (live.nodeValue !== next.nodeValue) live.nodeValue = next.nodeValue;
    return;
  }
  attributes(live, next);
  if (live.nodeName === "TEXTAREA") {
    if (live.defaultValue !== next.defaultValue) {
      live.defaultValue = next.defaultValue;
      show(live, next.defaultValue);
    }
    return;
  }
  if (next.hasAttribute("data-keep")) return;
  const chosen = live.nodeName === "SELECT" ? selected(live) : null;
  patchChildren(live, next);
  if (chosen !== null && chosen !== selected(live)) {  // The panel chose another option: show it.
    live.value = [...live.options].find((option) => option.hasAttribute("selected"))?.value ?? live.options[0]?.value ?? "";
  }
}

const selected = (select) => {
  const option = [...select.options].find((o) => o.hasAttribute("selected"));
  return option ? `${option.index}:${option.value}` : "";
};

// The panel's value for a field changed: show it (unless it's what's typed already, which keeps the caret where it is).
const show = (field, value) => { if (field.value !== value) field.value = value; };

const given = new WeakMap();  // A custom element → the attributes the panel gave it last time.

function remember(node) {
  // A custom element just placed: note which attributes are the panel's, so later patches leave its own alone.
  if (node.nodeType !== Node.ELEMENT_NODE) return;
  for (const el of [node, ...node.querySelectorAll("*")]) {
    if (el.nodeName.includes("-")) given.set(el, el.getAttributeNames());
  }
}

function attributes(live, next) {
  for (const {name, value} of next.attributes) {
    if (live.getAttribute(name) === value) continue;
    live.setAttribute(name, value);
    if (name === "value" && live.nodeName === "INPUT") show(live, value);
  }
  const custom = live.nodeName.includes("-");
  for (const name of custom ? given.get(live) || [] : live.getAttributeNames()) {
    if (!next.hasAttribute(name)) live.removeAttribute(name);
  }
  if (custom) given.set(live, next.getAttributeNames());
  if ("checked" in live) live.checked = next.hasAttribute("checked");
}
