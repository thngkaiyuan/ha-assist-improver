// Assist Improver's review panel: improvements shown the way a code review shows a change, and the settings. This is
// the module Home Assistant loads (panel.py's module_url, panel.js?v=<version>); the panel itself is in panel/, loaded
// under the same ?v=<version>, so after an upgrade a browser fetches every module afresh and never mixes old and new.
//
// The panel talks to Home Assistant over websocket commands (assist_improver/improvements, /improvement, /decide,
// /settings...). Each review has its own address (/assist-improver?id=...), which the notification links to; the
// settings are at /assist-improver?view=settings. In developer mode, the developer view is at
// /assist-improver?view=developer&tab=find|conversations|runs|health|logs&q=<id> (assist_improver/dev/... commands).
//
//   panel/app.js        the controller: state, loading, drawing, events
//   panel/home.js       home: what to review, else a run to start
//   panel/review.js     a review, and the decision on it
//   panel/settings.js   the pipeline as a track, the switches and the history (track.js, pickers.js, household.js)
//   panel/developer.js  the developer view (devparts.js)
//   panel/common.js     what the views share; format.js, text.js (every word), icons.js, style.js, morph.js

const version = new URL(import.meta.url).search;
const app = import(`./panel/app.js${version}`);

class AssistImproverPanel extends HTMLElement {
  // Defined at once, so Home Assistant can make the panel straight away; what it's given waits for the modules.
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._panel = app.then(({PanelApp}) => new PanelApp(this));
  }

  set hass(hass) {
    this.classList.toggle("dark", !!hass.themes?.darkMode);  // The theme follows Home Assistant's, live.
    this._panel.then((panel) => panel.setHass(hass));
  }

  set narrow(narrow) {
    this._panel.then((panel) => panel.setNarrow(narrow));
  }

  connectedCallback() {
    this._panel.then((panel) => { if (this.isConnected) panel.connect(); });
  }

  disconnectedCallback() {
    this._panel.then((panel) => panel.disconnect());
  }
}

// An update loads this script again under its new version's address; the page keeps the first definition until reload.
if (!customElements.get("assist-improver-panel")) customElements.define("assist-improver-panel", AssistImproverPanel);
await app;  // Whoever imports this module gets the panel ready to draw.
