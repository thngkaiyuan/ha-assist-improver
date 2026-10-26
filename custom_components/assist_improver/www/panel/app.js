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
const POLLED = ["home", "settings"];  // The views that show a run's progress.

export class PanelApp {
  constructor(host) {
    this.host = host;
    this.root = host.shadowRoot;
    this.open = KEPT.open;  // Sections kept open across updates, and across a new element (KEPT).
    this.drawn = {};  // Each region's HTML as last drawn.
    this.root.innerHTML = `<style>${STYLE}</style>${Object.keys(REGIONS).map((id) => `<div class="region" id="${id}"></div>`).join("")}
      <div class="tip" id="tip" hidden></div>`;
    for (const type of Object.keys(ACTIONS)) this.root.addEventListener(type, (ev) => this.dispatch(type, ev));
    this.root.addEventListener("toggle", (ev) => {  // A section opened or closed (toggle doesn't bubble: caught on its way in).
      const key = ev.target.dataset?.key;
      if (ev.target.tagName === "DETAILS" && key) ev.target.open ? this.open.add(key) : this.open.delete(key);
    }, true);
    for (const [type, show] of TIPS) this.root.addEventListener(type, (ev) => this.tip(ev, show));
    this.root.addEventListener("pointerdown", (ev) => {  // A click acts; its bubble steps aside for a moment.
      this.root.getElementById("tip").hidden = true;
      this.tipMuted = {text: ev.target?.closest?.("[data-tip]")?.dataset.tip, until: Date.now() + 1500};
    });
    this.onLocation = () => this.load();
    this.onKey = (ev) => {
      if (ev.key !== "Escape" || !(this.pop || this.runMenu || this.confirming || this.asking || this.menuOpen)) return;
      Object.assign(this, {pop: null, runMenu: false, confirming: false, picking: false, asking: null, menuOpen: false});
      this.render();
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
  }

  setHass(hass) {
    const first = !this.hass;
    this.hass = hass;
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

  go(id) {
    this.nav(reviewUrl(id));
  }

  nav(url) {
    history.pushState(null, "", url);
    window.dispatchEvent(new CustomEvent("location-changed"));
  }

  setRun(run) {
    this.run = run;
    if (run?.run?.status !== "running") this.cancelling = false;
  }

  async load() {
    // What the address asks for. A newer load (the address changed again) supersedes an older one still waiting.
    if (!this.hass) return;
    const params = new URLSearchParams(location.search), id = params.get("id");
    const seq = this.loadSeq = (this.loadSeq || 0) + 1, current = () => seq === this.loadSeq;
    this.error = null;
    clearTimeout(this.devTimer);
    try {
      if (params.get("view") === "developer") {
        KEPT.page = null;
        await developer.load(this, params);
      } else if (params.get("view") === "settings") {
        const [found, run, known, list, log] = await Promise.all([this.ws("settings"), this.ws("run"), this.ws("household"),
          this.ws("improvements").then((r) => r.improvements), this.ws("log").then((r) => r.events)]);
        if (!current()) return;
        Object.assign(this, {settings: found, household: known, list, log, view: "settings", review: null});
        this.setRun(run);
        this.poll();
      } else if (id) {
        this.view = "review";
        const fresh = this.review?.id !== id && KEPT.page !== id;
        if (fresh) {  // Another review: none of the last one's choices follow.
          Object.assign(this, {decision: null, note: "", reason: null, result: null, menuOpen: false});
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
        const [found, run, options] = await Promise.all([this.ws("improvements"), this.ws("run"),
                                                         this.ws("settings").catch(() => null), conversations.load(this)]);
        if (!current()) return;
        Object.assign(this, {list: found.improvements, developer: !!options?.developer, review: null, view: "home",
                             pop: null});
        if (fromReview) this.scroller().scrollTop = 0;  // Back from a review: home starts at its top.
        this.setRun(run);
        KEPT.page = null;
        this.poll();
      }
    } catch (err) {
      if (!current()) return;
      this.error = err.message || String(err);
    }
    if (current()) this.render();
  }

  poll() {
    // While a run is in progress, its progress every few seconds.
    clearTimeout(this.pollTimer);
    if (this.run?.run?.status === "running") this.pollTimer = setTimeout(() => this.refresh(), 3000);
  }

  async refresh() {
    if (!this.host.isConnected || !POLLED.includes(this.view)) return;
    if (this.pop) { this.poll(); return; }  // Not while a menu is open: it stays as it was opened.
    const before = this.run.run.status;
    let run;
    try {
      run = await this.ws("run");
    } catch (err) {
      this.poll();  // Try again at the next refresh.
      return;
    }
    if (!POLLED.includes(this.view)) return;
    this.setRun(run);
    if (before !== run.run?.status) { await this.load(); return; }  // Finished: new improvements may be in.
    this.render();
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

  render() {
    // Brings the page in line with the state. Drawing can change the layout (the track's width, its cards' sizes),
    // which asks for another draw: that one follows straight on, never inside this one.
    if (this.rendering) { this.again = true; return; }
    this.rendering = true;
    try {
      for (let n = 0; n < 5; n++) {
        this.again = false;
        this.draw();
        if (!this.again) break;
      }
    } finally {
      this.rendering = false;
    }
  }

  draw() {
    const view = this.current(), parts = view.render(this);
    for (const [id, part] of Object.entries(REGIONS)) {
      const html = parts[part] || "";
      if (this.drawn[id] === html) continue;
      this.drawn[id] = html;
      morph(this.root.getElementById(id), html);
    }
    this.menuButton();
    this.barHeight();
    const confirming = !!this.root.querySelector(".confirm-modal");  // A run's confirmation, just shown: Enter runs it.
    if (confirming && !this.confirmShown) this.root.getElementById("start-run")?.focus();
    this.confirmShown = confirming;
    view.after?.(this);
    if (this.tipAnchor && !this.tipAnchor.isConnected) this.root.getElementById("tip").hidden = true;  // Its control is gone.
  }

  barHeight() {
    // The Run bar's height (0 without it), as --bar: what sticks to the bottom of the page sits just above it. And the
    // conversations' pinned controls', as --controls: an opened conversation scrolls into view below them.
    const bar = this.root.querySelector(".cs-footer")?.offsetHeight || 0;
    if (bar !== this.bar) this.host.style.setProperty("--bar", `${this.bar = bar}px`);
    const controls = this.root.querySelector(".cs-controls")?.offsetHeight || 0;
    if (controls !== this.controls) this.host.style.setProperty("--controls", `${this.controls = controls}px`);
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
    tip.hidden = false;
    const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    const pop = el.closest(".pop:not(.sheet)");
    if (pop) {  // Beside a menu, so the bubble never covers its other options.
      const p = pop.getBoundingClientRect(), right = p.right + 8 + w <= window.innerWidth - 8;
      tip.style.left = `${right ? p.right + 8 : Math.max(8, p.left - w - 8)}px`;
      tip.style.top = `${Math.min(Math.max(8, r.top + r.height / 2 - h / 2), window.innerHeight - h - 8)}px`;
      return;
    }
    const top = r.top - h - 8 < 8 ? r.bottom + 8 : r.top - h - 8;
    tip.style.left = `${Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8)}px`;
    tip.style.top = `${top}px`;
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
