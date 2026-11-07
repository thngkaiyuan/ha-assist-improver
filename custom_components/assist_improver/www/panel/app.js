// The panel's controller: what it shows (its state), loading that over Home Assistant's websocket, and drawing it.
// Each view (home.js, review.js, settings.js, developer.js) turns the state into HTML for three regions: the toolbar,
// the page, and what floats over it (a footer, menus, modals). A region is patched, never rebuilt (morph.js), and only
// when its HTML changed, so a poll redraws a run's progress and nothing else. The listeners are wired once, on the
// shadow root, and find their action by what the event reached (each module's `actions`).

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{morph}, {STYLE}, {reviewUrl}, common, home, review, settings, pickers, household, developer, conversations] =
  await Promise.all(["morph", "style", "format", "common", "home", "review", "settings", "pickers", "household", "developer",
                     "conversations"].map((name) => import(`./${name}.js${v}`)));

const {KEPT} = review;
const REGIONS = {bar: "bar", main: "page", layer: "layer"};  // Each region's element, and the part of a view it shows.
const ACTIONS = {};  // An event's type → [selector, action] pairs, from every module.
for (const module of [common, home, review, settings, pickers, household, developer, conversations]) {
  for (const [type, list] of Object.entries(module.actions)) (ACTIONS[type] ||= []).push(...list);
}
const TIPS = [["pointerover", true], ["pointerout", false], ["focusin", true], ["focusout", false]];
const POLLED = ["home", "settings"];  // The views that show runs, and a run's progress.
// What a tap reveals by putting it on the page: it fades in where it stays (fade), on the page it was revealed on.
const REVEALS = ".done-msg, .done-row > :last-child, .menu, .cs-read, .cs-confirm, .cs-status-menu, .modal, .modal-shade, .pop";

const STILL = matchMedia("(prefers-reduced-motion: reduce)");  // The household asked for less motion: no fades.

// The saver (panel.py's EditorChannel). The improver never writes a configuration file itself: it asks the panel to
// save a script, automation or scene, or delete one (config null), through Home Assistant's own editor for it, with the
// signed-in admin's session, and the panel answers how it went. One per connection, kept while Home Assistant makes the
// panel again (a change may be under way). A reconnect (Home Assistant restarted, say) drops it, and it isn't renewed by itself (the
// improver may not be set up again yet): it's subscribed again at the next finish, and after a refusal at the one after.
const saver = {hass: null, connection: null, ready: null, stale: true, drop: null, saving: new Set()};

function saveItems(hass, again = false) {
  saver.hass = hass;
  const {connection} = hass;
  if (!connection || (connection === saver.connection && !(again && saver.stale))) return saver.ready;
  if (connection !== saver.connection) {
    saver.connection = connection;
    connection.addEventListener("ready", () => { if (saver.connection === connection) saver.drop(); });
  }
  saver.stale = false;
  saver.ready = new Promise((resolve) => {
    const drop = saver.drop = () => { saver.stale = true; resolve(); };  // Never left waiting on a lost socket.
    connection.subscribeMessage(saveItem, {type: "assist_improver/editors"}, {resubscribe: false}).then(resolve, drop);
  });
  return saver.ready;
}

function saveItem(asked) {
  // Each save is kept until it's answered: finish waits for them, so a change is never finished before one lands.
  const saving = save(asked).finally(() => saver.saving.delete(saving));
  saver.saving.add(saving);
}

async function save({key, kind, id, config}) {
  const {hass} = saver;
  let error;
  try {
    // Each part encoded: nothing in an id takes the call past its editor's own address.
    const path = `config/${encodeURIComponent(kind)}/config/${encodeURIComponent(id)}`;
    await hass.callApi(config ? "POST" : "DELETE", path, config || undefined);
  } catch (err) {
    error = String(err?.body?.message || err?.error || err?.message || err);  // Home Assistant's reason, as it says it.
  }
  try {
    await hass.callWS({type: "assist_improver/editors/saved", key, ...(error ? {error} : {})});
  } catch (err) { /* Not waited for any more: the change is finished from where it stands (finish). */ }
}

function fade(el) {
  // Marked for the fade (style.js .fresh) until it ends: moved or patched later, it never fades again.
  if (STILL.matches) return;
  el.classList.add("fresh");
  const done = (ev) => {
    if (ev.target !== el) return;
    el.classList.remove("fresh");
    for (const type of ["animationend", "animationcancel"]) el.removeEventListener(type, done);
  };
  for (const type of ["animationend", "animationcancel"]) el.addEventListener(type, done);
}

export class PanelApp {
  constructor(host) {
    this.host = host;
    this.root = host.shadowRoot;
    this.open = KEPT.open;  // Sections kept open across updates, and across a new element (KEPT).
    this.drawn = {};  // Each region's HTML as last drawn.
    this.root.innerHTML = `<style>${STYLE}</style><div class="status-strip"></div>${
      Object.keys(REGIONS).map((id) => `<div class="region" id="${id}"></div>`).join("")}
      <div class="tip" id="tip" hidden></div>`;
    for (const type of Object.keys(ACTIONS)) this.root.addEventListener(type, (ev) => this.dispatch(type, ev));
    this.root.addEventListener("toggle", (ev) => {  // A section opened or closed (toggle doesn't bubble: caught on its way in).
      const key = ev.target.dataset?.key;
      if (ev.target.tagName === "DETAILS" && key) ev.target.open ? this.open.add(key) : this.open.delete(key);
    }, true);
    this.root.addEventListener("click", (ev) => {
      // A section about to open from its own summary (a tap, or Enter or Space there): what it shows fades in, marked
      // before it's drawn open. One drawn open never does.
      const summary = ev.composedPath()[0]?.closest?.("summary"), section = summary?.parentElement;
      if (section?.tagName !== "DETAILS" || section.open || summary !== section.querySelector(":scope > summary")) return;
      for (const el of section.children) if (el !== summary) fade(el);
    }, true);
    for (const [type, show] of TIPS) this.root.addEventListener(type, (ev) => this.tip(ev, show));
    this.root.addEventListener("pointerdown", (ev) => {  // A click acts; its bubble steps aside for a moment.
      if (ev.target?.closest?.("[data-tip]")?.matches("[tabindex]")) return;  // One there to explain shows it at a tap.
      this.root.getElementById("tip").hidden = true;
      this.tipMuted = {text: ev.target?.closest?.("[data-tip]")?.dataset.tip, until: Date.now() + 1500};
    });
    this.onLocation = () => this.load();
    this.onKey = (ev) => {
      // The conversations' Status and order menus, an opened one's ⋮, and Delete's question (focus back to its ⋮).
      const lists = this.cv?.statusMenu || this.cv?.sortMenu || this.cv?.menu || this.cv?.deleting;
      const asked = this.cv?.deleting;
      const leaving = this.uninstall && !this.uninstall.busy;  // Uninstall's question, until it's on its way.
      if (ev.key !== "Escape" || !(this.pop || this.runMenu || this.confirming || this.menuOpen || lists || leaving)) return;
      Object.assign(this, {pop: null, runMenu: false, confirming: false, picking: false, menuOpen: false});
      if (leaving) this.uninstall = null;
      if (this.cv) Object.assign(this.cv, {statusMenu: false, sortMenu: false, menu: null, deleting: null});
      this.render();
      if (asked) this.root.getElementById("cs-actions")?.focus({preventScroll: true});
    };
    this.resize = new ResizeObserver(() => {
      if (this.view === "settings") settings.relayout(this);  // Vertical when narrow.
      this.barHeight();  // The Run bar may wrap onto another line.
    });
  }

  connect() {
    window.addEventListener("keydown", this.onKey);
    window.addEventListener("location-changed", this.onLocation);
    window.addEventListener("popstate", this.onLocation);
    this.resize.observe(this.host);
    this.poll();
  }

  disconnect() {
    clearTimeout(this.pollTimer);
    clearTimeout(this.devTimer);
    window.removeEventListener("location-changed", this.onLocation);
    window.removeEventListener("popstate", this.onLocation);
    window.removeEventListener("keydown", this.onKey);
    this.resize.disconnect();
    this.letGo?.();
  }

  setHass(hass) {
    const first = !this.hass;
    this.hass = hass;
    saveItems(hass);
    this.menuButton();
    if (first) {
      this.render();  // At once: the toolbar, and a spinner while the first replies are on their way.
      this.load();
    }
  }

  setNarrow(narrow) {
    this.narrow = narrow;
    this.menuButton();
  }

  // Talking to Home Assistant, and moving between the panel's addresses.

  ws(type, extra = {}) {
    return this.hass.callWS({type: `assist_improver/${type}`, ...extra});
  }

  async finish() {
    // A change left open (Home Assistant stopped in the middle of it, or a panel went away) is finished first, its
    // scripts, automations and scenes saved from here once this panel's own saves are answered: what it did
    // ({result}), or nothing.
    try {
      await saveItems(this.hass, true);
      await Promise.all(saver.saving);
      return await this.ws("changeset/finish");
    } catch (err) {
      return null;  // Tried again at the next load.
    }
  }

  go(id) {
    this.nav(reviewUrl(id));
  }

  nav(url) {
    history.pushState(null, "", url);
    window.dispatchEvent(new CustomEvent("location-changed"));
  }

  setRun(run, read = false) {
    // read: a load's, read with what the page shows (first, so nothing shown is older): what each refresh compares
    // with. An action's own read updates the Run bar only, so a run that ended since the load is still noticed.
    this.run = run;
    if (read) this.seen = run?.run;
    if (run?.run?.status !== "running") this.cancelling = false;
  }

  async load(own = false) {
    // What the address asks for. A newer load (the address changed again) supersedes an older one still waiting.
    // own: the panel's own (its poll's), not a tap's.
    if (!this.hass) return;
    const params = new URLSearchParams(location.search), id = params.get("id");
    const seq = this.loadSeq = (this.loadSeq || 0) + 1, current = () => seq === this.loadSeq;
    this.error = null;
    clearTimeout(this.devTimer);
    if (!own) await this.finish();  // Before anything is read.
    try {
      if (params.get("view") === "developer") {
        KEPT.page = null;
        await developer.load(this, params);
      } else if (params.get("view") === "settings") {
        // The run first: Home Assistant takes them in the order sent, so nothing read with it is older (setRun).
        const [run, found, known, list, log] = await Promise.all([this.ws("run"), this.ws("settings"), this.ws("household"),
          this.ws("improvements").then((r) => r.improvements), this.ws("log").then((r) => r.events)]);
        if (!current()) return;
        Object.assign(this, {settings: found, household: known, list, log, view: "settings", review: null});
        this.setRun(run, true);
        this.poll();
      } else if (id) {
        this.view = "review";
        const fresh = this.review?.id !== id && KEPT.page !== id;
        if (fresh) {  // Another review: none of the last one's choices follow.
          Object.assign(this, {decision: null, note: "", reason: null, result: null, menuOpen: false, decideAgain: false, failed: null});
          const history = this.open.has("history");  // Earlier improvements stay open on the way back.
          this.open.clear();
          if (history) this.open.add("history");
        }
        KEPT.page = id;
        const found = await this.ws("improvement", {improvement_id: id});
        if (!current()) return;
        if (fresh) for (const key of review.opened(found)) this.open.add(key);
        Object.assign(this, {review: found, list: null});
        if (fresh) this.scroller().scrollTop = 0;  // Another review starts at its top (the same one stays put).
      } else {
        // The settings only say whether developer mode is on (the Developer link); home works without them.
        const fromReview = this.view === "review";
        const [run, found, options] = await Promise.all([this.ws("run"), this.ws("improvements"),  // The run first.
                                                         this.ws("settings").catch(() => null), conversations.load(this)]);
        if (!current()) return;
        Object.assign(this, {list: found.improvements, questions: found.questions || [], developer: !!options?.developer,
                             review: null, view: "home", pop: null});
        if (fromReview) this.scroller().scrollTop = 0;  // Back from a review: home starts at its top.
        this.setRun(run, true);
        KEPT.page = null;
        this.poll();
      }
    } catch (err) {
      if (!current()) return;
      this.error = err.message || String(err);
    }
    if (current()) this.render(own);
  }

  poll() {
    // Every few seconds: the run in progress, or one that started since (sent in from Assist, say, or on schedule).
    clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => this.refresh(), 3000);
  }

  async refresh() {
    if (!this.host.isConnected || !POLLED.includes(this.view)) return;
    if (this.pop) { this.poll(); return; }  // Not while a menu is open: it stays as it was opened.
    let run;
    try {
      run = await this.ws("run");
    } catch (err) {
      this.poll();  // Try again at the next refresh.
      return;
    }
    if (!POLLED.includes(this.view)) return;
    this.setRun(run);
    // Another run, or this one finished, since the page read what it shows: what it moved, and new improvements, are
    // read again.
    if (this.seen?.id !== run.run?.id || this.seen?.status !== run.run?.status) { await this.load(true); return; }
    this.render(true);
    this.poll();
  }

  async saveSetting(change) {
    try { this.settings = await this.ws("settings/set", change); } catch (err) { alert(err.message || String(err)); }
    if ("stage" in change) {  // Backlogs held or freed.
      try { this.setRun(await this.ws("run")); } catch (err) { /* The next load reads them. */ }
    }
    this.render();
  }

  async reloadHousehold() {
    try { this.household = await this.ws("household"); } catch (err) { alert(err.message || String(err)); }
    this.render();
  }

  // Drawing.

  current() {
    if (this.error) return common.errorPage;
    if (this.view === "developer" && this.dev) return developer;
    if (this.view === "settings" && this.settings) return settings;
    if (this.review) return review;
    if (this.list) return home;
    return common.loadingPage;
  }

  render(own = false) {
    // Brings the page in line with the state. Drawing can change the layout (the track's width, its cards' sizes),
    // which asks for another draw: that one follows straight on, never inside this one. own: drawn on the panel's
    // own (its poll), not at a tap, so what's being read stays put (anchor).
    if (this.rendering) { this.again = true; return; }
    Object.assign(this, {rendering: true, own});
    try {
      for (let n = 0; n < 5; n++) {
        this.again = false;
        this.draw();
        if (!this.again) break;
      }
    } finally {
      Object.assign(this, {rendering: false, own: false});
    }
  }

  draw() {
    const view = this.current(), parts = view.render(this);
    // The page it draws: a reveal put on it fades in only on the page that was already there (a menu, a confirmation,
    // an opened conversation, what a decision did); a page arriving, or a panel Home Assistant makes again, shows at once.
    const page = view === common.loadingPage ? null : `${this.view}:${this.review?.id || ""}`, same = page && page === this.page;
    this.page = page;
    if (!same) this.letGo?.();  // Another page: as long as it is.
    const anchor = same && this.own ? this.anchor() : null;
    for (const [id, part] of Object.entries(REGIONS)) {
      const html = parts[part] || "";
      if (this.drawn[id] === html) continue;
      this.drawn[id] = html;
      const placed = morph(this.root.getElementById(id), html);
      if (same) {
        for (const node of placed) {
          if (node.nodeType !== Node.ELEMENT_NODE) continue;
          for (const el of node.matches(REVEALS) ? [node] : node.querySelectorAll(REVEALS)) fade(el);
        }
      }
    }
    this.menuButton();
    this.barHeight();
    this.reanchor(anchor);
    const confirming = !!this.root.querySelector(".confirm-modal");  // A run's confirmation, just shown: Enter runs it.
    if (confirming && !this.confirmShown) this.root.getElementById("start-run")?.focus();
    this.confirmShown = confirming;
    view.after?.(this);
    this.unhold();
    const tip = this.root.getElementById("tip"), words = this.tipAnchor?.dataset.tip;
    if (this.tipAnchor && !this.tipAnchor.isConnected) tip.hidden = true;  // Its control is gone.
    else if (!tip.hidden && words && words !== tip.textContent) this.tip({target: this.tipAnchor}, true);  // Its words changed.
  }

  barHeight() {
    // The Run bar's height (0 without it), as --bar: what sticks to the bottom of the page sits just above it.
    const bar = this.root.querySelector(".cs-footer")?.offsetHeight || 0;
    if (bar !== this.bar) this.host.style.setProperty("--bar", `${this.bar = bar}px`);
  }

  menuButton() {
    // Home Assistant's sidebar button, in the toolbar of home (the other views have a back arrow instead).
    const slot = this.root.getElementById("menu");
    if (!slot || !customElements.get("ha-menu-button")) return;
    const button = slot.firstElementChild || slot.appendChild(document.createElement("ha-menu-button"));
    button.hass = this.hass;
    button.narrow = this.narrow;
  }

  dispatch(type, ev) {
    // The action for the innermost element the event reached that has one.
    for (const el of ev.composedPath()) {
      if (el === this.root) return;
      if (el.nodeType !== Node.ELEMENT_NODE || el.getRootNode() !== this.root) continue;
      const found = ACTIONS[type].find(([selector]) => el.matches(selector));
      if (found) { found[1](this, el, ev); return; }
    }
  }

  tip(ev, show) {
    // A bubble with more about whatever is under the pointer (or focused): explanations on demand, not on the page.
    const tip = this.root.getElementById("tip"), el = ev.target?.closest?.("[data-tip]");
    clearTimeout(this.tipTimer);
    if (!show) {
      if (el && el.contains(ev.relatedTarget)) return;
      if (ev.pointerType === "touch") { this.tipTimer = setTimeout(() => { tip.hidden = true; }, 2500); return; }
      tip.hidden = true;
      return;
    }
    const muted = this.tipMuted;
    if (!el || (muted && muted.text === el.dataset.tip && Date.now() < muted.until)) return;
    this.tipAnchor = el;
    tip.textContent = el.dataset.tip;
    if (tip.hidden) fade(tip);
    tip.hidden = false;
    const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    const edge = this.root.querySelector(".status-strip").offsetHeight + 8;  // Clear of a phone's status bar.
    const pop = el.closest(".pop:not(.sheet)");
    if (pop) {  // Beside a menu, so the bubble never covers its other options.
      const p = pop.getBoundingClientRect(), right = p.right + 8 + w <= window.innerWidth - 8;
      tip.style.left = `${right ? p.right + 8 : Math.max(8, p.left - w - 8)}px`;
      tip.style.top = `${Math.min(Math.max(edge, r.top + r.height / 2 - h / 2), window.innerHeight - h - 8)}px`;
      return;
    }
    const top = r.top - h - 8 < edge ? r.bottom + 8 : r.top - h - 8;
    tip.style.left = `${Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8)}px`;
    tip.style.top = `${top}px`;
  }

  anchor() {
    // What the household is reading, so a change above it on the page's own (a run starting or ending, what it found)
    // leaves it where it is on the screen, as a browser's own scroll anchoring would (Safari has none): what's a third
    // of the way down the screen, under the controls while they're pinned, and each block it sits in, with where each
    // was; not a menu's backdrop or what's pinned. Not at the very top, where what's new shows as it comes.
    if (this.view !== "home" || this.scroller().scrollTop <= 0) return null;
    const page = this.root.querySelector("#main > .page"), controls = this.root.querySelector(".cs-controls");
    if (!page) return null;
    const box = page.getBoundingClientRect(), bar = controls?.getBoundingClientRect();
    const pinned = bar && bar.top <= parseFloat(getComputedStyle(controls).top) + 1;
    const y = Math.max(innerHeight / 3, pinned ? bar.bottom + 8 : 0);
    const hit = this.root.elementsFromPoint(box.left + box.width / 2, y)
      .find((el) => page.contains(el) && !el.closest(".backdrop, .cs-controls, .cs-send"));
    const chain = [];
    for (let el = hit; el && el !== page; el = el.parentElement) chain.push({el, top: el.getBoundingClientRect().top});
    return chain;
  }

  reanchor(chain) {
    // Put back by however far the innermost block still there moved.
    const kept = chain?.find((a) => a.el.isConnected), by = kept ? kept.el.getBoundingClientRect().top - kept.top : 0;
    if (Math.abs(by) >= 0.5) this.scroller().scrollBy({top: by, behavior: "instant"});
  }

  hold() {
    // The page keeps its length through the next draw, so what closes at its end can be put back where it was rather
    // than pulled down the screen as the page shortens. The room left over goes as the household scrolls back up and
    // never comes back; it's let go once the page is as long without it, and on another page.
    const scroller = this.scroller(), doc = scroller === (document.scrollingElement || document.documentElement);
    this.held = Math.max(this.held || 0, this.host.getBoundingClientRect().height);
    this.host.style.minHeight = `max(100vh, ${this.held}px)`;
    if (this.letGo) return;
    const target = doc ? document : scroller;
    const shrink = () => {  // As long as the page needs to be to stay where it's scrolled to.
      const bottom = doc ? innerHeight : scroller.getBoundingClientRect().bottom;
      this.held = Math.min(this.held, Math.ceil(bottom - this.host.getBoundingClientRect().top));
      this.host.style.minHeight = `max(100vh, ${this.held}px)`;
      this.unhold();
    };
    target.addEventListener("scroll", shrink, {passive: true});
    this.fit = shrink;
    this.letGo = () => {
      this.fit = null;
      target.removeEventListener("scroll", shrink);
      this.host.style.minHeight = "";
      this.held = 0;
      this.letGo = null;
    };
  }

  unhold() {
    // Let go of the page's length once it's as long without it: what it shows, its footer under that.
    const page = this.root.querySelector("#main > .page");
    if (!this.letGo || !page) return;
    const footer = [...this.root.querySelectorAll("#layer > .footer")].reduce((n, el) => n + el.offsetHeight, 0);
    const own = page.getBoundingClientRect().bottom - this.host.getBoundingClientRect().top + footer;
    if (this.held <= Math.max(own, innerHeight) + 1) this.letGo();
  }

  scroller() {
    // Whatever scrolls the page: the nearest scrolling ancestor, across shadow roots, else the document.
    for (let el = this.host; el;) {
      el = el.parentElement || el.getRootNode?.()?.host;
      if (!el) break;
      if (/(auto|scroll)/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight) return el;
    }
    return document.scrollingElement || document.documentElement;
  }
}
