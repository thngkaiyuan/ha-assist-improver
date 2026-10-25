// Assist Improver's review panel: improvements shown the way a code review shows a change, and the settings.
// Talks to Home Assistant over websocket commands (assist_improver/improvements, /improvement, /decide, /settings...);
// each review has its own address (/assist-improver?id=...), which the notification links to; the settings are at
// /assist-improver?view=settings. In developer mode, the developer view is at
// /assist-improver?view=developer&tab=find|conversations|runs|health|logs&q=<id> (assist_improver/dev/... commands).

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const ago = (iso) => {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}\u00a0min ago`;  // Never "1" at a line's end and "h ago" on the next.
  if (s < 86400) return `${Math.round(s / 3600)}\u00a0h ago`;
  return new Date(iso).toLocaleDateString(undefined, {month: "short", day: "numeric"});
};
const STAGES = [["analyze", "Analyze"], ["propose", "Propose"], ["test", "Simulate"], ["write", "Write up"]];
// Every label and tooltip the settings view shows, in one place: edit the words here. {name}-style parts are filled
// in when shown.
const TEXT = {
  steps: {
    requests: {title: "Your Assist", tip: "Your conversations with Assist, recorded as you use it. They wait here until the next run takes them in. The assistant shown is the one Assist uses by default."},
    triage: {title: "Triage", tip: "Recommended to use a small model to reduce downstream costs by filtering out conversations that clearly need no improvement."},
    analyze: {title: "Analyze", tip: "Reads each conversation to identify your intents, and where Assist could've done better."},
    propose: {title: "Propose", tip: "Propose changes to make your Assist experience smoother."},
    review: {title: "Review", tip: "Align proposals with the broader view of how Assist is used in your household."},
    test: {title: "Simulate", tip: "Replay your conversations with proposed fixes in a simulated copy of your home. Nothing in your home moves."},
    write: {title: "Write up", tip: "Packages technical improvements so they're easy for you to review."},
    you: {title: "Your review", tip: "Accept, decline, or ask for a revision."},
    household: {title: "Your household", tip: "Your house, made easier."},
  },
  onDemand: {label: "On-demand improvements", tip: ""},  // No bubble yet: its words wait for approval.
  sandbox: {label: "Sandbox mode", tip: "When turned on, your Assist conversations use a simulated copy of your home, so nothing real changes; the improver learns from them as practice. This applies only to your user.", others: "Also on for {names}."},
  autoAccept: {label: "Auto-accept low-risk changes",
               tip: "Automatically accepts Assist Improver's low-risk changes: household knowledge, and new names for devices other than locks, alarms and doors.",
               confirm: "Auto-accept low-risk changes?",
               confirmText: "Improvements that only add household knowledge lines or new device names will be applied without your review. Anything that lets Assist control more still waits for you. You'll be notified each time, and can undo.",
               on: "Turn on"},
  revise: {label: "Revisions", short: "Revisions"},
  block: {tip: "Stop this stage.", blocked: "Resume this stage."},
  badges: {new: "{n} new: the next run takes them in", waiting: "{n} waiting", held: "{n} held while blocked", review: "{n} to review", applied: "{n} applied"},
  assistant: {tip: "The assistant Assist uses by default", knowsOn: "Household knowledge on: {name} reads the {lines} learned so far. Click to turn off.",
              knowsOff: "Household knowledge off for {name}. Click to turn it on, so it reads what the household taught it.",
              none: "Add an assistant in Settings → Voice assistants first.", lines: "{lines} of household knowledge"},
  simulate: {label: "Your assistant", tip: "A maximum of {n} simulation rounds will be run against your assistant.", perGoal: "simulations per goal",
             cost: "Each simulation replays one of your conversations with a fix, on your own assistant: {n} per goal costs a maximum of about {n} times those conversations"},
  passThrough: {label: "Pass through", tip: "No triage model: conversations pass straight on to analysis."},
  followUpsOff: "Follow-ups aren't checked",
  models: {large: "Needs judgement: a strong model works best.", small: "A small, quick model is enough.",
           missing: {label: "Not available", tip: "This AI model is gone from Home Assistant. Choose another."},
           unavailable: "This AI model is unavailable right now.", none: {label: "Choose…", tip: "This step needs an AI model."},
           weak: "Not recommended for this step", unknown: "Fit unknown",
           unknownTip: "Assist Improver can't tell which model this is. A strong model, such as Claude Opus 5.5, works best.",
           recommended: "Use recommended setup", recommendedBusy: "Setting up…",
           recommendedTip: "Claude Opus 5.5 for the steps that need judgement, a small model for Triage and Write up. AI Tasks that fit are reused; the rest are added to your AI provider."},
  schedule: {off: "Only when you run it", daily: "Also every day at 3:00", weekly: "Also every Sunday at 3:00"},
  runNow: "Run now", runOneStep: "Run one step",
  failure: {stopped: "Nothing was lost: the next run tries again.",
            stage: "{steps} couldn't finish this time; the next run tries again.", details: "Details",
            settings: "Open settings"},
  confirm: {title: "Run Assist Improver?", step: "Run {step}?", run: "Run", notNow: "Not now",
            cost: "Runs use your AI models, so they cost money.", waiting: "{items} {are} waiting.", more: "{items} and more are waiting.",
            nothing: "Nothing is waiting right now.", again: "Nothing new is waiting; this proposes again from what's already analyzed.",
            blocked: "{steps} {is} blocked.", models: "{steps} can't run: {its} available. Choose another first.",
            its: "its AI model isn't", their: "their AI models aren't",
            items: {requests: "{n} new conversation{s}", analyze: "{n} conversation{s} to analyze", propose: "new findings to propose from",
                    test: "{n} idea{s} to simulate", write: "{n} write-up{s}"},
            steps: {triage: "Takes in {n} new conversation{s} and triages them.", triageNone: "Nothing is waiting for triage.",
                    analyze: "Analyzes {n} conversation{s}.", analyzeNone: "Nothing is waiting for analysis.",
                    propose: "Proposes fixes from what's new.", proposeNone: "Nothing new to propose from.",
                    test: "Simulates {n} idea{s}.", testNone: "Nothing is waiting to be simulated.",
                    write: "Writes up {n} improvement{s}.", writeNone: "Nothing is waiting to be written up."}},
  knowledge: {chip: "{n} learned", chipShort: "{n}", tip: "What your assistants know about your household. Accepted improvements add to it.",
              title: "Household knowledge", everyone: "Everyone", empty: "Nothing learned yet: accepted improvements add lines here.",
              search: "Search", none: "Nothing matches.", more: "Read more", less: "Show less", add: "Add a line",
              addFor: "Add a line just for {name}", placeholder: "One fact or preference, in a sentence", save: "Save", cancel: "Cancel",
              edit: "Edit", remove: "Delete", confirmRemove: "Delete this line?"},
  history: {title: "History", more: "Show more", empty: "Nothing yet: runs and your decisions show up here.",
            run: {done: "Run finished", failed: "Run stopped early", cancelled: "Run cancelled", interrupted: "Run interrupted"},
            stopped: "Nothing was lost: the next run tries again.", unfinished: "{steps} couldn't finish",
            setAside: "{n} conversation{s} set aside",
            failed: "{n} couldn't be analyzed: {why}", addressed: "{n} already addressed by an improvement", posted: "Up for review", decisions: {accept: "Accepted", decline: "Declined", revise: "Asked for a revision",
            needs_improvement: "Said it needs improvement", fine_as_is: "Said it was fine as it was", was_test: "Marked as a test",
            undo: "Undid", ignore: "Ignored"}, auto: "Accepted automatically"},
};
const fill = (text, values) => text.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? "");
const listed = (names) => names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names.join("");
// The settings view draws the pipeline as a track: requests merge onto the improvement loop (analyze, propose, test,
// write up), a small loop joins the proposer and its reviewer, and you review at the far end; revisions go round
// again and what you accept peels off to the household. Each AI step names its model; the ones with a gate can be
// blocked (a run then stops there and what reaches it waits).
const STEPS = {
  requests: {...TEXT.steps.requests, icon: "mdi:comment-processing-outline"},
  triage: {...TEXT.steps.triage, role: "triage", gate: "triage", icon: "mdi:filter-outline"},
  analyze: {...TEXT.steps.analyze, role: "analyzer", gate: "analyze", icon: "mdi:text-search"},
  propose: {...TEXT.steps.propose, role: "generator", gate: "propose", icon: "mdi:lightbulb-on-outline"},
  review: {...TEXT.steps.review, role: "reviewer", icon: "mdi:scale-balance"},
  test: {...TEXT.steps.test, gate: "test", icon: "mdi:flask-outline"},
  write: {...TEXT.steps.write, role: "editor", gate: "write", icon: "mdi:feather"},
  you: {...TEXT.steps.you, icon: "mdi:account-circle-outline"},
  household: {...TEXT.steps.household, icon: "mdi:home-heart"},
};
const AT_WORK = {analyze: ["triage", "analyze"], propose: ["propose", "review"], test: ["test"], write: ["write"]};
const BACKLOG = {requests: "requests", triage: "triage", analyze: "analyze", propose: "propose", test: "test",
                 write: "write", you: "review"};
const SCHEDULE = Object.entries(TEXT.schedule);
const glyph = (icon, cls = "") => customElements.get("ha-icon") ? `<ha-icon class="glyph ${cls}" icon="${icon}"></ha-icon>` : "";
const GATE_NAMES = Object.fromEntries(["triage", "analyze", "propose", "test", "write"].map((g) => [g, STEPS[g].title]));
const ICON = {play: "M8 5.14v13.72L19 12z",
              block: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 2a8 8 0 0 1 6.32 12.9L7.1 5.68A7.96 7.96 0 0 1 12 4zM4 12c0-1.85.63-3.55 1.68-4.9l11.22 11.22A8 8 0 0 1 4 12z", pause: "M7 5h3.5v14H7zm6.5 0H17v14h-3.5z",
              info: "M11 7h2v2h-2zm0 4h2v6h-2zm1-9a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 18a8 8 0 1 1 0-16 8 8 0 0 1 0 16z",
              check: "M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z",
              clock: "M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16zm0-18a10 10 0 1 1 0 20 10 10 0 0 1 0-20zm.5 5v5.25l4.5 2.67-.75 1.23L11 13V7z",
              copy: "M19 21H8V7h11m0-2H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2m-3-4H4a2 2 0 0 0-2 2v14h2V3h12z",
              refresh: "M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.73 10h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4z",
              code: "M14.6 16.6 19.2 12l-4.6-4.6L16 6l6 6-6 6zm-5.2 0L4.8 12l4.6-4.6L8 6l-6 6 6 6z"};
const svg = (name, cls = "") => `<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="${ICON[name]}"/></svg>`;
const COG = "M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43,12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37C21.73,9.22 21.78,8.95 21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5,2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95,5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.21,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.21,15.05 2.34,15.27L4.34,18.73C4.46,18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.67 16.04,18.34 16.56,17.94L19.05,18.95C19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z";
const STATUS = {awaiting_review: "To review", needs_attention: "Needs attention", applied: "Applied", undone: "Undone",
  declined: "Declined", revision_requested: "Revision requested", revised: "Revised", ignored: "Ignored",
  conflict: "Changed since tested"};

// The developer view: its tabs, the keys whose values are IDs (each a link to Find), and plain formatting.
const DEV_TABS = [["find", "Find"], ["conversations", "Conversations"], ["runs", "Runs"], ["health", "Health"], ["logs", "Logs"]];
const ID_KEYS = new Set(["id", "run_id", "recording_id", "improvement_id", "case_id", "trace_id", "generator_run", "role_call",
                         "run", "recording", "improvement", "case", "conversation_id"]);
const DEV_STAGES = ["active", "waiting", "held", "failing", "set_aside", "analysed", "improvement", "duplicate", "smooth", "test", "empty"];
const LEVELS = ["debug", "info", "warning", "error", "critical"];
const RUN_STAGES = ["analyze", "propose", "test", "write"];
const WF_DEPTH = 2;  // Waterfall levels shown open at first; deeper ones start collapsed.
const WF_HINTS = ["recording_id", "goal", "improvement_id", "case_id", "hypothesis", "replay", "turn", "tool", "role"];  // First beside a span's name.
const WF_ROWS = 1500;  // Waterfall rows drawn at most (a day's trace can hold thousands of spans).
const LOG_PAGE = 100;
const cls = (s) => String(s ?? "").replace(/[^A-Za-z0-9_-]/g, "");
const devUrl = (tab, extra = {}) => {
  const params = new URLSearchParams({view: "developer", tab});
  for (const [key, value] of Object.entries(extra)) if (value) params.set(key, value);
  return `/assist-improver?${params}`;
};
const bytes = (n) => n == null ? "–" : n < 1000 ? `${n}\u00a0B` : n < 1e6 ? `${(n / 1e3).toFixed(1)}\u00a0kB`
  : n < 1e9 ? `${(n / 1e6).toFixed(1)}\u00a0MB` : `${(n / 1e9).toFixed(2)}\u00a0GB`;
const dur = (ms) => {
  if (ms == null || !isFinite(ms)) return "–";
  if (ms === 0) return "0\u00a0ms";
  if (ms < 1) return `${ms.toFixed(2)}\u00a0ms`;
  if (ms < 1000) return `${Math.round(ms)}\u00a0ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}\u00a0s`;
  if (ms < 3600000) return `${Math.floor(ms / 60000)}m\u00a0${String(Math.floor((ms % 60000) / 1000)).padStart(2, "0")}s`;
  return `${Math.floor(ms / 3600000)}h\u00a0${String(Math.floor((ms % 3600000) / 60000)).padStart(2, "0")}m`;
};
const num = (n) => n == null ? "–" : Number(n).toLocaleString();
const isIso = (v) => typeof v === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v);
const stamp = (iso, unit = "s") => {
  // Local time to the minute, second or millisecond (unit "min", "s" or "ms"), with the date unless it's today.
  const d = new Date(iso);
  if (!iso || isNaN(d)) return iso ? String(iso) : "–";
  const time = d.toLocaleTimeString(undefined, {hour12: false, hour: "2-digit", minute: "2-digit",
                                                ...(unit === "min" ? {} : {second: "2-digit"})})
    + (unit === "ms" ? `.${String(d.getMilliseconds()).padStart(3, "0")}` : "");
  return d.toDateString() === new Date().toDateString() ? time
    : `${d.toLocaleDateString(undefined, {month: "short", day: "numeric"})} ${time}`;
};
const between = (from, to) => from && to ? Date.parse(to) - Date.parse(from) : null;

const STYLE = `
  :host { display: block; min-height: 100vh; background: var(--primary-background-color); color: var(--primary-text-color);
    font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
    --add-bg: rgba(46, 160, 67, 0.15); --add-num: rgba(46, 160, 67, 0.30); --add-fg: #2da44e;
    --del-bg: rgba(248, 81, 73, 0.15); --del-num: rgba(248, 81, 73, 0.30); --del-fg: #e5534b; }
  .toolbar { display: flex; align-items: center; gap: 8px; height: 56px; padding: 0 12px; font-size: 20px;
    background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, white); }
  .page { max-width: 960px; margin: 0 auto; padding: 16px 16px 24px; }
  a { color: var(--primary-color); text-decoration: none; cursor: pointer; }
  .card { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px;
    overflow: hidden; margin-bottom: 12px; }
  .item { display: flex; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid var(--divider-color); cursor: pointer; }
  .item:last-child { border-bottom: none; }
  .item:hover { background: var(--secondary-background-color); }
  .item .title { flex: 1; }
  .chip { font-size: 12px; padding: 2px 8px; border-radius: 10px; border: 1px solid var(--divider-color); color: var(--secondary-text-color); white-space: nowrap; }
  .chip.pending, .chip.awaiting_review { border-color: var(--primary-color); color: var(--primary-color); }
  .chip.applied { border-color: var(--add-fg); color: var(--add-fg); }
  .chip.needs_attention, .chip.conflict { border-color: var(--del-fg); color: var(--del-fg); }
  h1 { font-size: 22px; font-weight: 500; margin: 8px 0 4px; }
  .outcome { color: var(--add-fg); font-weight: 600; margin: 4px 0 16px; }
  details { border-top: 1px solid var(--divider-color); }
  details:first-of-type { border-top: none; }
  summary { list-style: none; cursor: pointer; padding: 12px 16px; display: flex; align-items: center; gap: 8px; user-select: none; }
  summary::-webkit-details-marker { display: none; }
  summary::before { content: "\\25B8"; color: var(--secondary-text-color); width: 12px; transition: transform .15s; }
  details[open] > summary::before { transform: rotate(90deg); }
  summary .name { flex: 1; }
  .file summary .name { flex: 0 1 auto; margin-right: 4px; }
  .kind { font-size: 12px; color: var(--secondary-text-color); border: 1px solid var(--divider-color); border-radius: 6px; padding: 1px 6px; }
  /* Each kind of change its own tint (green and red stay for added and removed lines); mid tones read in light and dark. */
  .kind.script { color: #9a6dd7; background: rgba(154, 109, 215, 0.12); border-color: rgba(154, 109, 215, 0.40); }
  .kind.exposure { color: #3d8bd9; background: rgba(61, 139, 217, 0.12); border-color: rgba(61, 139, 217, 0.40); }
  .kind.aliases { color: #c9861a; background: rgba(201, 134, 26, 0.12); border-color: rgba(201, 134, 26, 0.40); }
  .kind.instructions { color: #1f9e9a; background: rgba(31, 158, 154, 0.12); border-color: rgba(31, 158, 154, 0.40); }
  .kind.household { color: #1f9e9a; background: rgba(31, 158, 154, 0.12); border-color: rgba(31, 158, 154, 0.40); }
  .kind.assistant { color: #8a8f98; background: rgba(138, 143, 152, 0.14); border-color: rgba(138, 143, 152, 0.45); }
  .count-add { color: var(--add-fg); font-weight: 600; } .count-del { color: var(--del-fg); font-weight: 600; }
  .bar { display: inline-flex; gap: 2px; } .bar i { width: 8px; height: 8px; border-radius: 2px; background: var(--divider-color); }
  .bar i.a { background: var(--add-fg); } .bar i.d { background: var(--del-fg); }
  .lines { margin: 0 16px 12px; border: 1px solid var(--divider-color); border-radius: 8px; overflow: hidden; }
  .line { display: flex; gap: 8px; padding: 4px 12px; }
  .line.add { background: var(--add-bg); } .line.del { background: var(--del-bg); }
  .line .sign { width: 12px; color: var(--secondary-text-color); }
  .files { padding: 0 0 4px; }
  .file { margin: 0 16px 8px; border: 1px solid var(--divider-color); border-radius: 8px; overflow: hidden; }
  .file > details { border-top: none; }
  .file summary { background: var(--secondary-background-color); padding: 8px 12px; }
  table.diff { width: 100%; border-collapse: collapse; font-family: var(--code-font-family, ui-monospace, Menlo, monospace);
    font-size: 12.5px; line-height: 20px; table-layout: fixed; }
  table.diff td { padding: 0 8px; vertical-align: top; }
  table.diff td.num { width: 36px; text-align: right; color: var(--secondary-text-color); user-select: none; }
  table.diff td.sign { width: 14px; user-select: none; }
  table.diff td.text { white-space: pre-wrap; word-break: break-word; }
  tr.add td { background: var(--add-bg); } tr.add td.num { background: var(--add-num); }
  tr.del td { background: var(--del-bg); } tr.del td.num { background: var(--del-num); }
  tr.gap td { background: var(--secondary-background-color); color: var(--secondary-text-color); text-align: center; }
  .empty { padding: 8px 12px; color: var(--secondary-text-color); }
  .summary { padding: 16px 20px 6px; }
  .summary .benefit { font-size: 16px; line-height: 1.5; margin: 0 0 14px; }
  .safe { list-style: none; margin: 0 0 14px; padding: 0; line-height: 1.5; }
  .safe li { display: flex; gap: 8px; padding: 2px 0; } .safe .mark { color: var(--add-fg); font-weight: 700; width: 16px; flex: none; }
  .details-body { padding: 0 16px 16px 36px; color: var(--primary-text-color); line-height: 1.55; font-size: 15px; }
  .details-body p { margin: 0 0 14px; }
  .label { font-size: 12px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
    color: var(--secondary-text-color); margin: 0 0 6px; }
  .tests { list-style: none; margin: 0 0 14px; padding: 0; }
  .tests li { display: flex; align-items: baseline; gap: 8px; padding: 3px 0; }
  .tests .mark { width: 16px; font-weight: 700; } .tests .ok .mark { color: var(--add-fg); }
  .tests .no .mark { color: var(--secondary-text-color); }
  .tests .result { font-size: 12px; padding: 0 8px; border-radius: 10px; white-space: nowrap; }
  .tests .ok .result { color: var(--add-fg); background: var(--add-bg); }
  .tests .no .result { color: var(--secondary-text-color); background: var(--secondary-background-color); }
  .note { display: flex; gap: 8px; font-size: 13px; color: var(--secondary-text-color); border-top: 1px solid var(--divider-color);
    padding-top: 10px; margin-top: 4px; }
  .conflict { background: var(--del-bg); border: 1px solid var(--del-fg); border-radius: 8px; padding: 10px 14px; margin-bottom: 12px; }
  .footer { position: sticky; bottom: 0; z-index: 11; background: var(--card-background-color);
    border-top: 1px solid var(--divider-color); padding: 12px 16px; }
  .footer-inner { max-width: 960px; margin: 0 auto; display: flex; flex-direction: column; gap: 8px; align-items: flex-end; }
  textarea { width: 100%; box-sizing: border-box; min-height: 64px; padding: 8px; border-radius: 8px; font: inherit;
    border: 1px solid var(--divider-color); background: var(--primary-background-color); color: var(--primary-text-color); }
  .split { position: relative; display: inline-flex; }
  .split button { border: none; font: inherit; font-weight: 500; cursor: pointer; color: var(--text-primary-color, white);
    background: var(--primary-color); padding: 10px 16px; }
  .split .main { border-radius: 20px 0 0 20px; } .split .arrow { border-radius: 0 20px 20px 0; border-left: 1px solid rgba(255,255,255,.35); padding: 10px 12px; }
  .split button:disabled { opacity: .6; cursor: default; }
  .menu { position: absolute; right: 0; bottom: 48px; min-width: 260px; background: var(--card-background-color);
    border: 1px solid var(--divider-color); border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,.25); overflow: hidden; }
  .menu div { padding: 10px 14px; cursor: pointer; } .menu div:hover { background: var(--secondary-background-color); }
  .menu div.on { color: var(--primary-color); font-weight: 500; }
  .run { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px;
    padding: 14px 16px; margin: 8px 0 16px; }
  .run-head { display: flex; align-items: center; gap: 12px; }
  .run-head .what { flex: 1; }
  .run-head .title { font-weight: 500; }
  .run-head .sub { color: var(--secondary-text-color); font-size: 13px; margin-top: 2px; }
  .btn { border: none; border-radius: 18px; padding: 8px 16px; font: inherit; font-weight: 500; cursor: pointer;
    background: var(--primary-color); color: var(--text-primary-color, white); white-space: nowrap; }
  .btn.ghost { background: transparent; color: var(--primary-color); border: 1px solid var(--divider-color); }
  .btn:disabled { opacity: .6; cursor: default; }
  .btn.play { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px 8px 13px; }
  .stepper { display: flex; gap: 6px; margin: 12px 0 4px; }
  .step { flex: 1; text-align: center; font-size: 12px; color: var(--secondary-text-color); }
  .step .bar { height: 4px; border-radius: 2px; background: var(--divider-color); margin-bottom: 6px; }
  .step.done .bar { background: var(--add-fg); } .step.now .bar { background: var(--primary-color); animation: pulse 1.2s infinite; }
  .step.now { color: var(--primary-text-color); font-weight: 500; }
  @keyframes pulse { 50% { opacity: .45; } }
  .confirm { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--divider-color); line-height: 1.55; }
  .confirm ul { margin: 6px 0 10px; padding-left: 20px; }
  .confirm .cost { color: var(--secondary-text-color); font-size: 13px; margin-bottom: 10px; }
  .confirm .c-warn { color: #c9861a; font-size: 13px; }
  .confirm .actions { display: flex; gap: 8px; justify-content: flex-end; }
  .error-text { color: var(--del-fg); font-size: 13px; margin-top: 6px; }
  @media (max-width: 600px) { table.diff td.num { display: none; } .page { padding: 12px 10px 24px; }
    .run-head { flex-wrap: wrap; } }
  .run > .title { font-size: 16px; font-weight: 500; }
  .hero { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px;
    padding: 24px; margin: 8px 0 16px; }
  .hero-title { font-size: 20px; font-weight: 500; margin-bottom: 8px; }
  .hero .lead { margin: 0 0 16px; }
  .notice { color: var(--secondary-text-color); font-size: 14px; line-height: 1.5; margin: -6px 0 16px; }
  .notice details { display: inline; } .notice summary { display: inline; cursor: pointer; color: var(--primary-color); }
  .notice code { display: block; margin-top: 6px; font-size: 12px; white-space: pre-wrap; word-break: break-word; }
  .lead { color: var(--secondary-text-color); line-height: 1.5; margin: 0 0 12px; }
  .gains { color: var(--add-fg); font-size: 13px; font-weight: 500; }
  .run-line { display: flex; align-items: center; justify-content: space-between; gap: 8px;
    color: var(--secondary-text-color); font-size: 13px; margin: -4px 4px 16px; }
  .link { border: none; background: none; color: var(--primary-color); font: inherit; font-weight: 500; cursor: pointer; padding: 4px 0; }
  .history { border: none; margin-top: 8px; } .history > summary { padding: 8px 4px; color: var(--secondary-text-color); }
  .next { display: flex; justify-content: flex-end; margin: -4px 0 12px; }
  .offer { background: var(--card-background-color); border: 1px solid var(--primary-color); border-radius: 12px;
    padding: 12px 16px; margin: 8px 0 16px; line-height: 1.5; }
  .offer-row { display: flex; align-items: center; justify-content: space-between; padding-top: 8px; }
  .turn-on { border: none; border-radius: 16px; padding: 6px 14px; font: inherit; cursor: pointer;
    background: var(--primary-color); color: var(--text-primary-color, white); }
  .revises { padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; border: 1px solid var(--primary-color);
    color: var(--primary-text-color); background: var(--card-background-color); }
  .done-row { flex-direction: row !important; align-items: center !important; justify-content: space-between; flex-wrap: wrap; gap: 12px !important; }
  .done-msg { display: flex; align-items: baseline; gap: 8px; flex: 1 1 320px; min-width: 0; line-height: 1.45; }
  .done-msg .mark { font-weight: 700; } .done-msg.ok .mark { color: var(--add-fg); } .done-msg.bad .mark { color: var(--del-fg); }
  .done-row .btn { max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
  .outcome-banner { padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; border: 1px solid; }
  .outcome-banner.ok { background: var(--add-bg); border-color: var(--add-fg); }
  .outcome-banner.bad { background: var(--del-bg); border-color: var(--del-fg); }
  .tb-btn { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; border: none; background: none;
    color: inherit; font: inherit; font-size: 15px; cursor: pointer; padding: 8px; border-radius: 18px; }
  .tb-btn:hover { background: rgba(255, 255, 255, 0.12); }
  .tb-btn svg { width: 22px; height: 22px; fill: currentColor; }
  .page.wide { max-width: 1180px; }
  /* The pipeline as a track: one translucent layer of solid strokes (overlaps don't darken), colors blending
     along it; the step cards sit on top. Laid out in _layout(). */
  :host { --track-in: #a3a8af; --track-ai: #a3a8af; --track-out: #a3a8af; --held: #bf9a55; --badge: #8796a8; --done: #6f9e98; }
  :host(.dark) { --track-in: #d4d7dc; --track-ai: #d4d7dc; --track-out: #d4d7dc; }
  .flow-card { padding: 12px 16px 16px; overflow: visible; }
  .flow-head { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .flow-head .spacer { flex: 1; }
  .practice { display: inline-flex; align-items: center; gap: 10px; font-size: 14px; cursor: pointer; user-select: none; }
  .flow-head { align-items: flex-start; } .toggles { display: flex; flex-direction: column; gap: 10px; }
  .toggle-row { display: flex; align-items: center; gap: 4px; }
  .flow-head .info-tip { margin-left: 2px; } .flow-head .info-tip .ic { width: 15px; height: 15px; }
  .glyph.in-chip { --mdc-icon-size: 14px; width: 14px; height: 14px; margin-right: 4px; vertical-align: -2px; }
  .history { margin-top: 12px; } .history .h-title { font-size: 14px; font-weight: 500; padding: 12px 16px 4px; }
  .h-row { display: flex; align-items: center; gap: 10px; padding: 8px 16px; font-size: 13.5px; }
  .h-row .glyph { --mdc-icon-size: 18px; width: 18px; height: 18px; } .h-row .h-text { flex: 1; min-width: 0; line-height: 1.35; }
  .h-row .h-sub { color: var(--secondary-text-color); font-size: 12.5px; }
  .h-row .h-when { color: var(--secondary-text-color); font-size: 12.5px; white-space: nowrap; }
  .h-row.h-link { cursor: pointer; } .h-row.h-link:hover { background: var(--secondary-background-color); }
  .h-more { padding: 6px 16px 12px; } .h-empty { padding: 4px 16px 14px; color: var(--secondary-text-color); font-size: 13px; }
  .modal-shade { position: fixed; inset: 0; z-index: 20; background: rgba(0, 0, 0, .45); }
  .modal { position: fixed; z-index: 21; left: 50%; top: 50%; transform: translate(-50%, -50%); width: min(560px, calc(100vw - 32px));
    max-height: min(80vh, 720px); display: flex; flex-direction: column; box-sizing: border-box; border-radius: 16px;
    background: var(--card-background-color); color: var(--primary-text-color); box-shadow: 0 16px 48px rgba(0, 0, 0, .35); }
  .modal-head { display: flex; align-items: center; gap: 10px; padding: 16px 16px 12px 20px; font-size: 17px; border-bottom: 1px solid var(--divider-color); }
  .modal-head .glyph { --mdc-icon-size: 20px; width: 20px; height: 20px; } .modal-head b { font-weight: 500; }
  .kn-count { color: var(--secondary-text-color); font-size: 13px; flex: 1; }
  .modal-close { border: none; background: none; color: var(--secondary-text-color); font-size: 16px; cursor: pointer; width: 32px;
    height: 32px; border-radius: 16px; } .modal-close:hover { background: var(--secondary-background-color); }
  .kn-search { margin: 12px 20px 0; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--divider-color); font: inherit;
    font-size: 14px; background: var(--primary-background-color); color: var(--primary-text-color); }
  .modal-body { overflow: auto; padding: 4px 0 16px; }
  .confirm-modal { width: min(480px, calc(100vw - 32px)); } .confirm-modal .modal-head .ic { width: 18px; height: 18px; color: var(--primary-color); }
  .modal-body.confirm { padding: 14px 20px 18px; border-top: none; margin-top: 0; line-height: 1.55; }
  .modal-body.confirm p { margin: 0 0 8px; } .modal-body.confirm .c-what { font-size: 15px; margin-bottom: 12px; }
  .modal-body.confirm .actions { margin-top: 16px; }
  .kn-section ul { list-style: none; margin: 0; padding: 0 20px; }
  .kn-group { display: flex; justify-content: space-between; padding: 14px 20px 6px; font-size: 12px; font-weight: 500;
    letter-spacing: .04em; text-transform: uppercase; color: var(--secondary-text-color); }
  .kn-line { padding: 9px 2px; font-size: 14px; line-height: 1.45; border-top: 1px solid var(--divider-color); }
  .kn-row { display: flex; gap: 8px; align-items: flex-start; } .kn-main { flex: 1; min-width: 0; }
  .kn-text.clamp { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .kn-more { font-size: 12.5px; padding: 2px 0 0; } .kn-more[hidden] { display: none; }
  .kn-icons { display: flex; gap: 2px; flex: none; }
  .kn-icon { border: none; background: none; padding: 4px; border-radius: 14px; cursor: pointer; color: var(--secondary-text-color);
    opacity: .6; display: inline-flex; --mdc-icon-size: 17px; } .kn-icon:hover, .kn-icon:focus-visible { opacity: 1; background: var(--secondary-background-color); }
  .kn-icon .glyph { width: 17px; height: 17px; --mdc-icon-size: 17px; }
  .kn-editor textarea { width: 100%; box-sizing: border-box; min-height: 72px; padding: 8px 10px; border-radius: 8px; font: inherit;
    font-size: 14px; border: 1px solid var(--primary-color); background: var(--primary-background-color); color: var(--primary-text-color); resize: vertical; }
  .kn-buttons, .kn-confirm { display: flex; gap: 8px; justify-content: flex-end; align-items: center; margin-top: 8px; }
  .kn-confirm span { flex: 1; color: var(--secondary-text-color); font-size: 13px; }
  .btn.danger { background: var(--del-fg); } .kn-add { list-style: none; padding: 6px 2px 2px; } .kn-add-me { padding: 8px 22px 0; }
  .kn-line:first-child { border-top: none; }
  @media (max-width: 600px) { .modal { top: auto; bottom: 0; transform: translateX(-50%); width: 100vw; max-height: 88vh;
    border-radius: 16px 16px 0 0; } }
  .split.down { z-index: 11; }
  .split.down .menu { top: 44px; bottom: auto; z-index: 12; min-width: 250px; }
  .menu .group { padding: 8px 14px 4px; font-size: 12px; color: var(--secondary-text-color); cursor: default;
    border-top: 1px solid var(--divider-color); } .menu .group:hover { background: none; }
  .menu .row-ic { display: flex; align-items: center; gap: 10px; } .menu .ic-space { width: 17px; flex: none; } .menu .row-ic .ic { width: 17px; height: 17px; color: var(--secondary-text-color); }
  .menu .row-ic.on .ic { color: var(--primary-color); } .menu .row-ic .lbl { flex: 1; }
  .menu .count { color: var(--secondary-text-color); margin-left: 16px; } .menu .on .count { color: var(--primary-color); }
  .split.down .main { display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px 8px 12px; }
  .split.down .main .ic, .split.down .arrow .ic { width: 16px; height: 16px; }
  .split.down .arrow { display: inline-flex; align-items: center; padding: 8px 10px; }
  .track { position: relative; }
  .track > svg { position: absolute; inset: 0; overflow: visible; pointer-events: none; }
  .band { opacity: .14; } :host(.dark) .band { opacity: .10; }
  .chev { fill: none; stroke: var(--secondary-text-color); stroke-opacity: .45; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  .flowing { fill: none; stroke: var(--secondary-text-color); stroke-width: 3; stroke-linecap: round; stroke-dasharray: 1 16; opacity: .55; }
  @media (prefers-reduced-motion: no-preference) { .flowing { animation: flow 1.1s linear infinite; } }
  @keyframes flow { to { stroke-dashoffset: -17; } }
  .stn { position: absolute; box-sizing: border-box; display: flex; flex-direction: column; justify-content: center; gap: 6px;
    padding: 8px 9px; border-radius: 12px; background: var(--card-background-color);
    border: 1px solid var(--divider-color); box-shadow: 0 1px 3px rgba(0, 0, 0, .06); }

  .glyph { --mdc-icon-size: 16px; width: 16px; height: 16px; color: var(--secondary-text-color); flex: none; display: inline-flex; }
  .info-tip { flex: none; display: inline-flex; color: var(--secondary-text-color); opacity: .7; cursor: help; }
  .info-tip .ic { width: 13px; height: 13px; } .info-tip:hover { opacity: 1; }
  .knows { flex: none; width: 24px; height: 24px; border-radius: 12px; padding: 0; cursor: pointer; display: inline-flex; --mdc-icon-size: 15px;
    align-items: center; justify-content: center; border: 1px solid var(--divider-color); background: var(--card-background-color);
    color: var(--secondary-text-color); --mdc-icon-size: 15px; }
  .knows.on { color: var(--done); border-color: rgba(111, 158, 152, .55); background: rgba(111, 158, 152, .1); }
  .chip-row { display: flex; align-items: center; gap: 6px; justify-content: center; max-width: 100%; min-width: 0; }
  .tip { position: fixed; z-index: 30; max-width: 260px; padding: 7px 10px; border-radius: 8px; pointer-events: none;
    font-size: 12.5px; line-height: 1.4; color: #fff; background: rgba(40, 42, 46, .96); box-shadow: 0 4px 14px rgba(0, 0, 0, .25); }
  .tip[hidden] { display: none; }
  /* Dark themes: the cards lift off the background and their text and controls read clearly; the band stays faint. */
  :host(.dark) .stn { background: #26272a; border-color: rgba(255, 255, 255, .13); box-shadow: 0 1px 4px rgba(0, 0, 0, .45); }
  :host(.dark) .s-title { color: #f1f1f1; }
  :host(.dark) .chip-model { background: rgba(255, 255, 255, .08); border-color: rgba(255, 255, 255, .2); color: #ececec; }
  :host(.dark) .chip-model.off { border-color: rgba(255, 255, 255, .32); color: #c9c9c9; }
  :host(.dark) .chip-model.fixed { color: #cfcfcf; }
  :host(.dark) .gate, :host(.dark) .knows { background: #2b2c2f; border-color: rgba(255, 255, 255, .24); color: #d0d0d0; }
  :host(.dark) .gate.paused { background: var(--held); border-color: var(--held); color: #fff; }
  :host(.dark) .knows.on { color: #8fc7bf; border-color: rgba(143, 199, 191, .6); background: rgba(143, 199, 191, .14); }
  :host(.dark) .glyph { color: #cfcfcf; }
  :host(.dark) .info-tip { color: #c4c4c4; opacity: .9; }
  :host(.dark) .stream-label { color: #b9b9b9; }
  :host(.dark) .chev { stroke: #d0d0d0; stroke-opacity: .5; }
  :host(.dark) .badge { --badge: #93a3b8; }
  /* The step at work: the card stays solid; a soft ring around it breathes. */
  .stn.now { border-color: var(--badge); box-shadow: 0 0 0 3px rgba(135, 150, 168, .22); }
  @media (prefers-reduced-motion: no-preference) { .stn.now { animation: working 1.8s ease-in-out infinite; } }
  @keyframes working { 0%, 100% { box-shadow: 0 0 0 2px rgba(135, 150, 168, .30); } 50% { box-shadow: 0 0 0 7px rgba(135, 150, 168, .10); } }
  .track:not(.vertical) .stn { align-items: center; text-align: center; width: max-content; padding: 10px 12px;
    transform: translate(-50%, -50%); }
  .track:not(.vertical) .s-head { justify-content: center; max-width: 100%; }
  .track:not(.vertical) .s-title { flex: none; max-width: 100%; }
  .track:not(.vertical) .chip-model { align-self: center; }
  .stn.paused .chip-model, .stn.paused .s-title { opacity: .55; }
  .s-head { display: flex; align-items: center; gap: 6px; min-width: 0; }
  .s-title { flex: 1; font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ic { width: 16px; height: 16px; fill: currentColor; flex: none; }
  .gate { flex: none; width: 22px; height: 22px; border-radius: 11px; border: 1px solid var(--divider-color); padding: 0;
    display: inline-grid; place-items: center; line-height: 0; box-sizing: border-box; cursor: pointer;
    background: var(--card-background-color); color: var(--secondary-text-color); }
  .gate .ic { width: 13px; height: 13px; display: block; }
  .gate:hover { border-color: var(--badge); color: var(--badge); }
  .gate.paused { border-color: var(--held); color: #fff; background: var(--held); }
  /* The pause button sits on the card's bottom corner (the backlog badge is on the top one): always there, never in the way. */
  .track:not(.vertical) .gate { position: absolute; bottom: -10px; right: -10px; box-shadow: 0 0 0 2px var(--card-background-color); }
  .chip-model { align-self: flex-start; max-width: 100%; box-sizing: border-box; font: inherit; font-size: 12px; line-height: 1.3;
    padding: 3px 8px; border-radius: 12px; cursor: pointer; text-align: left; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; border: 1px solid var(--divider-color); background: var(--secondary-background-color);
    color: var(--primary-text-color); }
  .chip-model:hover { border-color: var(--badge); }
  .chip-model.off { color: var(--secondary-text-color); border-style: dashed; }
  .chip-model.warn { color: var(--held); border-color: rgba(201, 134, 26, .6); }
  .chip-model.fixed { cursor: pointer; color: var(--secondary-text-color); }
  .badge { position: absolute; top: -8px; right: -8px; min-width: 20px; height: 20px; box-sizing: border-box; padding: 0 6px;
    border-radius: 10px; font-size: 11.5px; font-weight: 600; line-height: 20px; text-align: center;
    color: white; background: var(--badge); box-shadow: 0 0 0 2px var(--card-background-color); }
  .badge.held { background: var(--held); } .badge.in { background: #9aa0a6; } .badge.out { background: var(--done); }
  .done-count { display: inline-flex; align-items: center; gap: 4px; font-size: 12.5px; color: var(--done); }
  .done-count .ic { width: 14px; height: 14px; }
  .stream-label { position: absolute; transform: translate(-50%, -50%); white-space: nowrap; font-size: 12px; font-weight: 500;
    letter-spacing: .02em; color: var(--secondary-text-color); }
  .stream-label.vertical { transform: translate(-50%, -50%) rotate(-90deg); font-size: 11px; }
  .track.vertical .stn { flex-direction: row; align-items: center; gap: 10px; }
  .track.vertical .s-head { flex: 1; } .track.vertical .s-title { flex: 0 1 auto; }
  .track.vertical .chip-row { flex: 0 1 auto; max-width: 62%; } .track.vertical .chip-row .chip-model { max-width: none; }
  .chip-row .chip-model { max-width: none; flex: 0 1 auto; min-width: 0; }
  .track.vertical .chip-model { align-self: center; max-width: 46%; }
  .info { border: none; background: none; padding: 2px; margin: 0 0 0 2px; cursor: pointer; color: var(--secondary-text-color);
    display: inline-flex; vertical-align: middle; } .info .ic { width: 15px; height: 15px; }
  .backdrop { position: fixed; inset: 0; z-index: 10; }
  .pop { position: fixed; z-index: 11; width: 320px; max-height: 70vh; overflow: auto; box-sizing: border-box;
    background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .3); padding: 6px 0; }
  .pop.sheet { left: 0 !important; right: 0; top: auto !important; bottom: 0; width: auto; border-radius: 16px 16px 0 0; max-height: 80vh; }
  .pop-head { padding: 8px 14px 2px; font-size: 14px; }
  .pop-lead { padding: 0 14px 8px; font-size: 12.5px; color: var(--secondary-text-color); line-height: 1.4; }
  .opt { padding: 8px 14px; cursor: pointer; } .opt:hover { background: var(--secondary-background-color); }
  .opt-main { display: flex; align-items: center; gap: 8px; } .opt-main .spacer { flex: 1; }
  .pop-foot { display: flex; align-items: center; gap: 6px; padding-top: 8px; border-top: 1px solid var(--divider-color); margin-top: 4px; }
  .opt .check { width: 14px; color: var(--primary-color); font-weight: 700; flex: none; }
  .opt-label { font-weight: 500; }
  .opt-sub, .opt-warn { font-size: 12px; color: var(--secondary-text-color); margin-left: 22px; line-height: 1.35; }
  .opt-warn { color: #c9861a; }
  .tag { font-size: 11px; padding: 0 7px; border-radius: 9px; white-space: nowrap; color: var(--secondary-text-color);
    background: rgba(127, 127, 127, 0.14); }
  .tag.good { color: var(--add-fg); background: var(--add-bg); } .tag.bad { color: #c9861a; background: rgba(201, 134, 26, 0.14); }
  .opt.new { color: var(--primary-color); font-weight: 500; border-top: 1px solid var(--divider-color); margin-top: 4px; }
  .opt.new + .opt.new { border-top: none; margin-top: 0; }
  .form { padding: 4px 14px 10px; display: flex; flex-direction: column; gap: 10px; }
  .form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--secondary-text-color); }
  .form select, .form input { font: inherit; font-size: 14px; padding: 7px 8px; border-radius: 8px; color: var(--primary-text-color);
    border: 1px solid var(--divider-color); background: var(--primary-background-color); }
  .form .actions { display: flex; gap: 8px; justify-content: flex-end; }
  .stepper-row { display: flex; align-items: center; justify-content: center; gap: 18px; padding: 8px 14px 10px; }
  .round { width: 34px; height: 34px; border-radius: 17px; border: 1px solid var(--divider-color); background: none;
    color: var(--primary-text-color); font-size: 18px; cursor: pointer; } .round:disabled { opacity: .4; cursor: default; }
  .t-n { font-size: 22px; font-weight: 500; min-width: 28px; text-align: center; }
  .rows .row { display: flex; align-items: center; gap: 16px; padding: 14px 16px; border-bottom: 1px solid var(--divider-color); }
  .rows .row:last-child { border-bottom: none; }
  .row-text { flex: 1; min-width: 0; } .row-title { font-weight: 500; }
  .row-sub { font-size: 13px; color: var(--secondary-text-color); line-height: 1.45; margin-top: 2px; }
  .seg { display: inline-flex; border: 1px solid var(--divider-color); border-radius: 18px; overflow: hidden; flex: none; }
  .seg button { border: none; background: none; font: inherit; font-size: 13px; padding: 7px 12px; cursor: pointer;
    color: var(--primary-text-color); border-left: 1px solid var(--divider-color); }
  .seg button:first-child { border-left: none; }
  .seg button.on { background: var(--primary-color); color: var(--text-primary-color, white); }
  .toggle { flex: none; width: 44px; height: 24px; border-radius: 12px; border: none; cursor: pointer; position: relative;
    background: var(--divider-color); transition: background .15s; }
  .toggle::after { content: ""; position: absolute; top: 3px; left: 3px; width: 18px; height: 18px; border-radius: 9px;
    background: white; transition: left .15s; box-shadow: 0 1px 2px rgba(0,0,0,.3); }
  .toggle.on { background: var(--primary-color); } .toggle.on::after { left: 23px; }
  .uses { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 6px; font-size: 13px; }
  .uses .ok { color: var(--add-fg); }
  @media (max-width: 600px) { .rows .row { flex-wrap: wrap; } .seg { width: 100%; } .seg button { flex: 1; padding: 7px 4px; } }
  /* The developer view: dense and technical; wide tables scroll inside their card, never the page. */
  .tb-btn + .tb-btn { margin-left: 0; }
  .tb-btn.tb-text { font-size: 14px; padding: 6px 10px; } .tb-btn.tb-text svg { width: 18px; height: 18px; }
  .dv { --mono: var(--code-font-family, ui-monospace, Menlo, monospace); --dv-blue: #3d8bd9; --dv-purple: #9a6dd7;
    --dv-amber: #c9861a; --dv-teal: #1f9e9a; --dv-grey: #8a8f98; font-size: 14px; }
  .dv-tabs { display: flex; gap: 2px; overflow-x: auto; scrollbar-width: none; margin: -4px 0 12px;
    border-bottom: 1px solid var(--divider-color); }
  .dv-tabs::-webkit-scrollbar { display: none; }
  .dv-tab { padding: 10px 11px 9px; white-space: nowrap; font-size: 14px; font-weight: 500; color: var(--secondary-text-color);
    border-bottom: 2px solid transparent; margin-bottom: -1px; }
  .dv-tab.on { color: var(--primary-color); border-bottom-color: var(--primary-color); }
  .dv-card { padding: 12px 14px; }
  .dv-card.flush { padding: 0; } .dv-card.flush > .dv-card-title { padding: 12px 14px 8px; }
  .dv-card.flush > .dv-note { padding: 0 14px 12px; }
  .dv-card-title { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; font-weight: 500; margin-bottom: 8px; }
  .dv-card-title .dv-dim { font-weight: 400; font-size: 12.5px; }
  .dv-card-title .dv-right { margin-left: auto; }
  .dv-dim { color: var(--secondary-text-color); }
  .dv-note { color: var(--secondary-text-color); font-size: 13px; line-height: 1.45; }
  .dv-empty { color: var(--secondary-text-color); font-size: 13px; padding: 4px 0; }
  .dv-flush-empty { padding: 0 14px 12px; }
  .dv-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 460px), 1fr)); gap: 0 12px; align-items: start; }
  .dv-grid > .wide { grid-column: 1 / -1; }
  .dv-tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 160px), 1fr)); gap: 8px; margin-bottom: 12px; }
  .dv-tile { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px; padding: 10px 12px; min-width: 0; }
  .dv-tile .t-label { font-size: 12px; color: var(--secondary-text-color); }
  .dv-tile .t-value { font-size: 20px; font-weight: 500; margin: 2px 0; font-variant-numeric: tabular-nums; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  .dv-tile .t-sub { font-size: 12px; color: var(--secondary-text-color); line-height: 1.35; overflow-wrap: anywhere; }
  .dv-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .dv-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .dv-table th, .dv-table td { padding: 6px 10px; text-align: left; white-space: nowrap; border-top: 1px solid var(--divider-color);
    vertical-align: top; }
  .dv-table th { font-size: 12px; font-weight: 500; color: var(--secondary-text-color); border-top: none; }
  .dv-table .n { text-align: right; font-variant-numeric: tabular-nums; }
  .dv-table th.sticky, .dv-table td.sticky { position: sticky; left: 0; z-index: 1; background: var(--card-background-color); }
  .dv-table th button { border: none; background: none; padding: 0; font: inherit; color: inherit; cursor: pointer; }
  .dv-table th button.on { color: var(--primary-color); }
  .dv-table td.bad { color: var(--del-fg); font-weight: 500; }
  .dv-card.flush .dv-table th:first-child, .dv-card.flush .dv-table td:first-child { padding-left: 14px; }
  .dv-card.flush .dv-table th:last-child, .dv-card.flush .dv-table td:last-child { padding-right: 14px; }
  .dv-pad { padding: 4px 14px 12px; } .dv-card-title.dv-sub { padding: 8px 14px 4px; margin: 0; }
  .dv-id { display: inline-flex; align-items: center; gap: 1px; font-family: var(--mono); font-size: 12.5px; max-width: 100%; vertical-align: baseline; }
  .dv-id a { overflow-wrap: anywhere; word-break: break-all; }
  .dv-copy { border: none; background: none; padding: 2px 3px; margin: -2px 0; cursor: pointer; color: var(--secondary-text-color);
    opacity: .55; display: inline-flex; border-radius: 4px; flex: none; }
  .dv-copy:hover, .dv-copy:focus-visible { opacity: 1; background: var(--secondary-background-color); }
  .dv-copy .ic { width: 13px; height: 13px; } .dv-copy.done { color: var(--add-fg); opacity: 1; }
  .dv-mono { font-family: var(--mono); font-size: 12.5px; }
  .dv-chip { display: inline-block; font-size: 11.5px; line-height: 18px; padding: 0 7px; border-radius: 9px; white-space: nowrap;
    color: var(--secondary-text-color); background: rgba(127, 127, 127, .14); }
  .dv-chip.c-blue { color: var(--dv-blue); background: rgba(61, 139, 217, .14); }
  .dv-chip.c-green { color: var(--add-fg); background: var(--add-bg); }
  .dv-chip.c-amber { color: var(--dv-amber); background: rgba(201, 134, 26, .15); }
  .dv-chip.c-red { color: var(--del-fg); background: var(--del-bg); }
  .dv-chip.c-primary { color: var(--primary-color); background: rgba(3, 169, 244, .14); }
  .dv-tag { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--secondary-text-color);
    background: var(--secondary-background-color); border-radius: 6px; padding: 1px 6px; max-width: 100%; box-sizing: border-box;
    overflow-wrap: anywhere; }
  .dv-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; min-width: 0; }
  .dv-kv { display: grid; grid-template-columns: minmax(90px, max-content) minmax(0, 1fr); gap: 4px 14px; margin: 0; font-size: 13px; }
  .dv-kv dt { color: var(--secondary-text-color); overflow-wrap: anywhere; } .dv-kv dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
  .dv-json { display: block; font-family: var(--mono); font-size: 12px; white-space: pre-wrap; word-break: break-word;
    max-height: 240px; overflow: auto; background: var(--secondary-background-color); border-radius: 6px; padding: 6px 8px; }
  .dv-search { display: flex; gap: 8px; margin-bottom: 12px; }
  .dv input, .dv select { font: inherit; font-size: 14px; padding: 8px 10px; border-radius: 8px; min-width: 0; box-sizing: border-box;
    color: var(--primary-text-color); border: 1px solid var(--divider-color); background: var(--card-background-color); }
  .dv-search input { flex: 1; font-family: var(--mono); font-size: 13px; }
  .dv-hint { color: var(--secondary-text-color); font-size: 13px; line-height: 1.5; margin: 0 0 12px; }
  .dv-head { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 10px; margin-bottom: 10px; min-width: 0; }
  .dv-head .dv-title { font-weight: 500; min-width: 0; overflow-wrap: anywhere; }
  .dv-matches .dv-match { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; padding: 8px 0;
    border-top: 1px solid var(--divider-color); }
  .dv-matches .dv-match:first-child { border-top: none; }
  .dv-tl-row { display: grid; grid-template-columns: max-content max-content minmax(0, 1fr); gap: 2px 10px; padding: 7px 0;
    border-top: 1px solid var(--divider-color); align-items: baseline; }
  .dv-tl-row:first-child { border-top: none; } .dv-tl-row > .dv-chip { justify-self: start; }
  .dv-time { font-family: var(--mono); font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; }
  .dv-what { min-width: 0; overflow-wrap: anywhere; line-height: 1.4; }
  .dv-log { padding: 8px 0; border-top: 1px solid var(--divider-color); min-width: 0; }
  .dv-log:first-child { border-top: none; }
  .dv-log-head { display: flex; align-items: baseline; gap: 8px; }
  .dv-msg { margin-top: 2px; font-size: 13px; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; }
  .dv-level { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; color: var(--dv-grey); }
  .dv-level.lv-info { color: var(--dv-blue); } .dv-level.lv-warning { color: var(--dv-amber); }
  .dv-level.lv-error, .dv-level.lv-critical { color: var(--del-fg); }
  .dv-log.lv-error, .dv-log.lv-critical { box-shadow: inset 3px 0 0 var(--del-fg); padding-left: 10px; }
  .dv-log.lv-warning { box-shadow: inset 3px 0 0 var(--dv-amber); padding-left: 10px; }
  .dv details { border-top: none; } .dv summary { padding: 4px 0; font-size: 12.5px; color: var(--primary-color); }
  .dv pre { margin: 4px 0 0; font-family: var(--mono); font-size: 11.5px; line-height: 1.45; white-space: pre-wrap; word-break: break-word;
    background: var(--secondary-background-color); border-radius: 6px; padding: 8px; max-height: 360px; overflow: auto; }
  .dv-run { padding: 10px 14px; border-top: 1px solid var(--divider-color); cursor: pointer; min-width: 0; }
  .dv-run:first-child { border-top: none; } .dv-run:hover { background: var(--secondary-background-color); }
  .dv-run-top { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 8px; }
  .dv-run-top .dv-right { margin-left: auto; font-family: var(--mono); font-size: 12.5px; }
  .dv-run-sub { display: flex; align-items: baseline; flex-wrap: wrap; gap: 2px 8px; font-size: 12.5px; color: var(--secondary-text-color); margin-top: 3px; }
  .dv-err { color: var(--del-fg); font-size: 12.5px; margin-top: 4px; overflow-wrap: anywhere; }
  .dv-stages { display: flex; height: 6px; border-radius: 3px; overflow: hidden; margin-top: 8px; background: var(--divider-color); }
  .dv-stages i { display: block; height: 100%; min-width: 2px; }
  .s-analyze { background: var(--dv-blue); } .s-propose { background: var(--dv-purple); } .s-test { background: var(--dv-amber); }
  .s-write { background: var(--dv-teal); }
  .dv-legend { display: flex; flex-wrap: wrap; gap: 2px 12px; margin-top: 5px; font-size: 12px; color: var(--secondary-text-color); }
  .dv-legend span { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
  .dv-legend i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
  .dv-back { display: inline-block; margin: 0 0 10px; font-weight: 500; }
  .dv-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; margin-bottom: 10px; }
  .dv-bar input { flex: 1 1 220px; }
  .dv-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
  .dv-fchip { font: inherit; font-size: 12.5px; padding: 4px 10px; border-radius: 14px; cursor: pointer; white-space: nowrap;
    border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); }
  .dv-fchip.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, white); }
  .dv-fchip b { font-weight: 500; opacity: .7; margin-left: 4px; }
  .dv-filters { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-bottom: 12px; align-items: end; }
  .dv-filters label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--secondary-text-color); min-width: 0; }
  .dv-filters select, .dv-filters input { height: 38px; }
  .dv-filters .actions { grid-column: 1 / -1; display: flex; gap: 12px; justify-content: flex-end; align-items: center; }
  .dv-more { display: flex; justify-content: center; padding: 8px 0 2px; }
  .dv-live { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--secondary-text-color); }
  .dv-live::before { content: ""; width: 8px; height: 8px; border-radius: 4px; background: var(--primary-color); animation: pulse 1.2s infinite; }
  .dv-list { margin: 0; padding: 0 0 0 18px; font-family: var(--mono); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
  /* Waterfall: a row per span, indented by depth, its bar placed by start and length within the trace. */
  .wf { font-size: 12.5px; }
  .wf-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 12px; padding: 0 14px 8px; font-size: 12.5px; color: var(--secondary-text-color); }
  .wf-tools .link { font-size: 12.5px; padding: 0; }
  .wf-row { display: grid; grid-template-columns: minmax(0, 11fr) 62px minmax(0, 9fr); align-items: center; min-height: 26px;
    border-top: 1px solid var(--divider-color); }
  .wf-row:hover { background: var(--secondary-background-color); }
  .wf-axis { min-height: 22px; font-size: 11.5px; color: var(--secondary-text-color); border-top: none; }
  .wf-axis:hover { background: none; }
  .wf-name { display: flex; align-items: center; gap: 2px; min-width: 0; padding: 0 6px 0 calc(var(--d, 0) * 12px + 4px);
    box-sizing: border-box; }
  .wf-tog { flex: none; width: 18px; height: 22px; border: none; background: none; padding: 0; cursor: pointer; font-size: 11px;
    color: var(--secondary-text-color); }
  .wf-sp { flex: none; width: 18px; }
  .wf-label { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--primary-text-color);
    font-family: var(--mono); font-size: 12px; }
  .wf-label .dv-dim { font-size: 11.5px; }
  .wf-row.st-error .wf-label { color: var(--del-fg); }
  .wf-ms { text-align: right; font-family: var(--mono); font-size: 11.5px; color: var(--secondary-text-color); padding-right: 8px; white-space: nowrap; }
  .wf-track { position: relative; height: 26px; margin-right: 12px;
    background: linear-gradient(to right, var(--divider-color) 1px, transparent 1px) 0 0 / 25% 100% repeat-x; }
  .wf-axis .wf-track { height: 22px; background: none; }
  .wf-track > i { position: absolute; top: 9px; height: 8px; border-radius: 2px; min-width: 2px; background: var(--primary-color); }
  .wf-row.st-error .wf-track > i { background: var(--del-fg); }
  .wf-row.st-cancelled .wf-track > i { background: var(--dv-grey); }
  .wf-row.open .wf-track > i { background: repeating-linear-gradient(45deg, var(--primary-color) 0 4px, transparent 4px 7px);
    border: 1px solid var(--primary-color); box-sizing: border-box; }
  .wf-scale span { position: absolute; top: 4px; white-space: nowrap; font-family: var(--mono); }
  .wf-scale .wf-mid { left: 50%; transform: translateX(-50%); }
  .wf-detail { padding: 6px 14px 10px; border-top: 1px dashed var(--divider-color); background: var(--secondary-background-color); }
  .wf-detail .dv-kv { font-size: 12.5px; }
  .wf-cap { padding: 8px 14px; font-size: 12.5px; color: var(--secondary-text-color); border-top: 1px solid var(--divider-color); }
  @media (min-width: 900px) { .wf-row { grid-template-columns: minmax(0, 420px) 76px minmax(0, 1fr); } }
  @media (max-width: 600px) {
    .dv-tl-row { grid-template-columns: max-content minmax(0, 1fr); } .dv-tl-row .dv-what { grid-column: 1 / -1; }
    .dv-card { padding: 10px 12px; } .wf-detail { padding: 6px 12px 10px; } .wf-tools { padding: 0 12px 8px; }
    .dv-card.flush > .dv-card-title { padding: 10px 12px 8px; } .dv-card.flush > .dv-note, .dv-flush-empty { padding: 0 12px 10px; }
    .dv-run { padding: 10px 12px; } .wf-track { margin-right: 8px; } .wf-ms { padding-right: 6px; }
    .wf-name { padding-left: calc(var(--d, 0) * 9px + 2px); } .wf-scale .wf-mid { display: none; }
    .dv-pad { padding: 4px 12px 10px; } .dv-card-title.dv-sub { padding: 8px 12px 4px; }
    .dv-card.flush .dv-table th:first-child, .dv-card.flush .dv-table td:first-child { padding-left: 12px; }
    .dv-card.flush .dv-table th:last-child, .dv-card.flush .dv-table td:last-child { padding-right: 12px; } }
`;

// Home Assistant makes the panel's element again whenever its sidebar title (the count) changes, as it does after a
// decision. What was open and where the page was scrolled carry over to the new element, so nothing moves.
const KEPT = {open: new Set(), page: null, scroll: null};

class AssistImproverPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._open = KEPT.open;  // Sections kept open across re-renders, and across a new element (KEPT).
    this._onLocation = () => this._load();
    this._resize = new ResizeObserver(() => this._relayout());  // The track turns vertical when narrow.
    for (const [type, show] of [["pointerover", true], ["pointerout", false], ["focusin", true], ["focusout", false]]) {
      this.shadowRoot.addEventListener(type, (ev) => this._tip(ev, show));
    }
    this.shadowRoot.addEventListener("pointerdown", (ev) => {  // A click acts; its bubble steps aside for a moment.
      const tip = this.shadowRoot.getElementById("tip");
      if (tip) tip.hidden = true;
      this._tipMuted = {text: ev.target?.closest?.("[data-tip]")?.dataset.tip, until: Date.now() + 1500};
    });
  }

  _tip(ev, show) {
    // A bubble with more about whatever is under the pointer (or focused): explanations on demand, not on the page.
    const tip = this.shadowRoot.getElementById("tip"), el = ev.target?.closest?.("[data-tip]");
    if (!tip) return;
    clearTimeout(this._tipTimer);
    if (!show) {
      if (el && el.contains(ev.relatedTarget)) return;
      if (ev.pointerType === "touch") { this._tipTimer = setTimeout(() => { tip.hidden = true; }, 2500); return; }
      tip.hidden = true;
      return;
    }
    const muted = this._tipMuted;
    if (!el || (muted && muted.text === el.dataset.tip && Date.now() < muted.until)) return;
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

  connectedCallback() {
    this._onKey = (ev) => {
      if (ev.key === "Escape" && (this._pop || this._runMenu || this._confirming || this._asking)) {
        this._pop = null; this._runMenu = false; this._confirming = false; this._asking = null; this._render();
      }
    };
    window.addEventListener("keydown", this._onKey);
    window.addEventListener("location-changed", this._onLocation);
    window.addEventListener("popstate", this._onLocation);
    this._resize.observe(this);
  }

  _poll() {
    // While a run is in progress, refresh its card every few seconds.
    clearTimeout(this._pollTimer);
    if (this._run?.run?.status === "running") {
      this._pollTimer = setTimeout(async () => {
        if (!this.isConnected || this._review || this._view === "developer") return;
        if (this._pop) { this._poll(); return; }  // Not while a picker is open (it would lose what's typed).
        const before = this._run.run.status;
        this._run = await this._hass.callWS({type: "assist_improver/run"});
        if (before !== this._run.run?.status) { await this._load(); return; }  // Finished: new improvements may be in.
        this._render(); this._poll();
      }, 3000);
    }
  }

  disconnectedCallback() {
    clearTimeout(this._pollTimer);
    clearTimeout(this._devTimer);
    window.removeEventListener("location-changed", this._onLocation);
    window.removeEventListener("popstate", this._onLocation);
    window.removeEventListener("keydown", this._onKey);
    this._resize.disconnect();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    this.classList.toggle("dark", !!hass.themes?.darkMode);
    if (first) this._load();
  }

  set narrow(narrow) { this._narrow = narrow; }

  _go(id) {
    this._nav(id ? `/assist-improver?id=${encodeURIComponent(id)}` : "/assist-improver");
  }

  _nav(url) {
    history.pushState(null, "", url);
    window.dispatchEvent(new CustomEvent("location-changed"));
  }

  async _load() {
    if (!this._hass) return;
    const params = new URLSearchParams(location.search);
    const id = params.get("id");
    this._error = null;
    clearTimeout(this._devTimer);
    try {
      if (params.get("view") === "developer") {
        await this._loadDev(params);
      } else if (params.get("view") === "settings") {
        const ws = (type) => this._hass.callWS({type});
        [this._settings, this._run, this._household, this._list, this._log] = await Promise.all([
          ws("assist_improver/settings"), ws("assist_improver/run"), ws("assist_improver/household"),
          ws("assist_improver/improvements").then((r) => r.improvements), ws("assist_improver/log").then((r) => r.events)]);
        this._view = "settings"; this._review = null;
        this._poll();
      } else if (id) {
        this._view = "review";
        if (this._review?.id !== id && KEPT.page !== id) { this._decision = null; this._note = ""; this._result = null; this._open.clear(); }
        KEPT.page = id;

        this._review = await this._hass.callWS({type: "assist_improver/improvement", improvement_id: id});
        this._list = null;
      } else {
        // The settings only say whether developer mode is on (the Developer link); home works without them.
        const settings = this._hass.callWS({type: "assist_improver/settings"}).catch(() => null);
        this._list = (await this._hass.callWS({type: "assist_improver/improvements"})).improvements;
        this._household = await this._hass.callWS({type: "assist_improver/household"});
        this._run = await this._hass.callWS({type: "assist_improver/run"});
        this._developer = !!(await settings)?.developer;
        this._review = null; this._view = "home"; this._pop = null; KEPT.page = null;
        this._poll();
      }
    } catch (err) {
      this._error = err.message || String(err);
    }
    this._render();
  }

  _toolbar(title, back, end = "") {
    return `<div class="toolbar">${back ? `<a id="back" style="color:inherit">&#8592;</a>` : `<span id="menu"></span>`}
      <span>${esc(title)}</span>${end}</div>`;
  }

  _render() {
    const root = this.shadowRoot;
    let body;
    if (this._error) body = this._toolbar("Assist Improver", true) + `<div class="page"><div class="conflict">${esc(this._error)}</div></div>`;
    else if (this._view === "developer" && this._dev) body = this._renderDev();
    else if (this._view === "settings" && this._settings) body = this._renderSettings(this._settings);
    else if (this._review) body = this._renderReview(this._review);
    else if (this._list) body = this._renderList(this._list);
    else body = this._toolbar("Assist Improver") + `<div class="page">Loading…</div>`;
    root.innerHTML = `<style>${STYLE}</style>${body}<div class="tip" id="tip" hidden></div>`;
    this._wire();
    if (this._review) this._keepScroll();
    if (this._view === "developer" && this._dev?.toTop) {  // A new tab or ID starts at the top.
      this._dev.toTop = false;
      requestAnimationFrame(() => { this._scroller().scrollTop = 0; });
    }
  }

  _progress(run) {
    // A run in progress: where it is, with live counts; it can be left and come back to.
    const p = run.progress || {};
    const at = STAGES.findIndex(([key]) => key === run.stage);
    const detail = {
      analyze: p.analyze_total != null ? `Analyzing conversations: ${p.analyzed || 0} of ${p.analyze_total}` +
        (p.skipped_as_smooth ? ` (${p.skipped_as_smooth} went smoothly, skipped)` : "") : "Finding conversations to analyze…",
      propose: `Proposing fixes from ${plural(p.sessions || 0, "conversation")}, with a reviewer checking each…`,
      test: `Testing ${plural(p.hypotheses || 0, "fix")} in a sandboxed copy of your home…`,
      write: `Writing up ${plural(p.writing || 0, "improvement")}…`}[run.stage];
    return `<div class="run"><div class="run-head"><div class="what"><div class="title">Improving your assistant</div>
      <div class="sub">${esc(detail)} You can leave this page; it keeps going.</div></div>
      <button class="btn ghost" id="cancel-run">Cancel</button></div>
      <div class="stepper">${STAGES.map(([key, label], n) =>
        `<div class="step ${n < at ? "done" : n === at ? "now" : ""}"><div class="bar"></div>${label}</div>`).join("")}</div></div>`;
  }

  _lastRun(run) {
    if (!run) return "";
    const p = run.progress || {};
    const what = {done: "Last run", failed: "Last run stopped early", cancelled: "Last run cancelled",
                  interrupted: "Last run was interrupted"}[run.status] || "Last run";
    const found = p.new_improvements != null ? ` · ${plural(p.new_improvements, "new improvement")}` : "";
    return `${what} ${ago(run.finished || run.started)}${found}`;
  }

  _failure(run) {
    // What went wrong in the last run, in a line, with what happens next. A step whose model is missing names the
    // step and opens the settings; a step that couldn't finish is named, and the next run tries it again; the
    // technical details are folded away, for a bug report.
    const F = TEXT.failure, problems = run?.progress?.problems || [];
    const details = (lines) => `<details><summary>${esc(F.details)}</summary><code>${lines.map(esc).join("\n")}</code></details>`;
    if (run?.status === "failed" && run.error) {
      if (/ can't run: /.test(run.error)) {
        return `<div class="notice">${esc(run.error)} <button class="link go-settings">${esc(F.settings)}</button></div>`;
      }
      return `<div class="notice">${esc(F.stopped)} ${details([run.error])}</div>`;
    }
    if (!problems.length) return "";
    const names = problems.map((p) => GATE_NAMES[p.stage] || p.stage);
    return `<div class="notice">${esc(fill(F.stage, {steps: listed(names)}))} ${details(problems.map((p, i) => `${names[i]}: ${p.error}`))}</div>`;
  }

  _askAutoAccept() {
    const A = TEXT.autoAccept;
    return `<div class="modal-shade ask-cancel"></div><div class="modal confirm-modal" role="dialog" aria-modal="true" aria-label="${esc(A.confirm)}">
        <div class="modal-head"><b>${esc(A.confirm)}</b><span class="kn-count"></span>
          <button class="modal-close ask-cancel" aria-label="${esc(TEXT.confirm.notNow)}">&#10005;</button></div>
        <div class="modal-body confirm"><p class="c-what">${esc(A.confirmText)}</p>
        <div class="actions"><button class="btn ghost ask-cancel">${esc(TEXT.confirm.notNow)}</button>
        <button class="btn" id="ask-ok">${esc(A.on)}</button></div></div></div>`;
  }

  _confirm(preview) {
    // One sentence on what the run will do, a gentle line on cost, and Run: in a modal, so the page stays put; a click
    // outside or Esc is "Not now".
    const b = preview.backlog || {}, only = this._only, C = TEXT.confirm, blocked = new Set(only ? [] : preview.paused || []);
    const count = (n, text) => fill(text, {n, s: n === 1 ? "" : "s"});
    let sentence;
    if (only) {
      const n = only === "triage" ? (b.requests || 0) + (b.triage || 0) : b[only] || 0;
      sentence = count(n, C.steps[n ? only : `${only}None`]);
    } else {
      const order = [["requests", "triage"], ["analyze", "analyze"], ["propose", "propose"], ["test", "test"], ["write", "write"]];
      const items = order.filter(([key, gate]) => b[key] && !(key !== "requests" && blocked.has(gate)))
        .map(([key]) => key === "propose" ? C.items.propose : count(b[key], C.items[key]));
      const shown = items.slice(0, 2).join(" and ");
      sentence = !items.length ? (this._force ? C.again : C.nothing)
        : items.length > 2 ? fill(C.more, {items: items.slice(0, 2).join(", ")})
        : fill(C.waiting, {items: shown, are: items.length === 1 && /^1 /.test(items[0]) ? "is" : "are"});
    }
    const held = [...blocked].map((g) => GATE_NAMES[g]).filter(Boolean);
    const stages = only ? [only] : Object.keys(GATE_NAMES).filter((g) => !blocked.has(g));
    const broken = [...new Set(stages.flatMap((g) => (preview.blocked_models || {})[g] || []))];
    const modelsLine = broken.length ? fill(C.models, {steps: broken.join(" and "), its: broken.length === 1 ? C.its : C.their}) : "";
    const blockedLine = held.length ? fill(C.blocked, {steps: held.join(", "), is: held.length === 1 ? "is" : "are"}) : "";
    const title = only ? fill(C.step, {step: GATE_NAMES[only]}) : C.title;
    return `<div class="modal-shade" id="confirm-shade"></div><div class="modal confirm-modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="modal-head">${svg("play")}<b>${esc(title)}</b><span class="kn-count"></span>
          <button class="modal-close" id="cancel-confirm" aria-label="${esc(C.notNow)}">&#10005;</button></div>
        <div class="modal-body confirm"><p class="c-what">${esc(sentence)}</p>
        ${blockedLine ? `<p class="cost">${esc(blockedLine)}</p>` : ""}${modelsLine ? `<p class="c-warn">&#9888; ${esc(modelsLine)}</p>` : ""}
        <p class="cost">${esc(C.cost)}</p>
        <div class="actions"><button class="btn ghost" id="cancel-confirm-2">${esc(C.notNow)}</button>
        <button class="btn" id="start-run" ${modelsLine ? "disabled" : ""}>${esc(C.run)}</button></div></div></div>`;
  }

  _rows(items) {
    return items.map((i) => {
      const gains = [i.enabled && `Enables ${plural(i.enabled, "task")}`, i.removed && `${plural(i.removed, "fewer turn")}`].filter(Boolean).join(" · ");
      return `<div class="item" data-id="${esc(i.id)}"><span class="title">${esc(i.title)}
        ${gains ? `<div class="gains">${esc(gains)}</div>` : ""}</span>
        <span class="chip ${esc(i.status)}">${esc(STATUS[i.status] || i.status)}</span></div>`;
    }).join("");
  }

  _renderList(items) {
    // One thing at a time: what to review, else a run to start, else all caught up.
    const {run, preview} = this._run || {};
    const pending = items.filter((i) => i.pending), earlier = items.filter((i) => !i.pending);
    const running = run?.status === "running";
    const waiting = preview?.conversations_to_analyze || 0;
    const toWrite = preview?.backlog?.write || 0;
    const h = this._household;
    const missing = h && h.lines ? h.assistants.filter((a) => !a.using) : [];
    const offer = missing.length ? `<div class="offer"><div><b>Household knowledge</b> · ${plural(h.lines, "line")} learned so far.
      These assistants don't read it yet:</div>
      ${missing.map((a) => `<div class="offer-row"><span>${esc(a.title)}</span>
        <button class="turn-on" data-target="${esc(a.target)}">Turn on</button></div>`).join("")}</div>` : "";
    const error = this._failure(run);
    let main;
    if (pending.length) {
      const line = running ? "" : `<div class="run-line"><span>${esc(this._lastRun(run) || "No runs yet")}${
        waiting ? ` · ${plural(waiting, "new conversation")} waiting` : ""}</span>
        ${waiting ? `<button class="link" id="run-now">Run now</button>` : ""}</div>`;
      main = `<h1>${plural(pending.length, "improvement")} to review</h1>
        <p class="lead">Each was tested on your own conversations. Open one, check what changes, and decide.</p>
        <div class="card">${this._rows(pending)}</div>${line}${error}`;
    } else if (running) {
      main = "";
    } else if (toWrite) {
      main = `<div class="hero"><div class="hero-title">${plural(toWrite, "improvement")} waiting to be written up</div>
        <div class="lead">${(preview.paused || []).includes("write") ? "Write up is blocked. " : ""}They go up for review
        once written in plain words.</div>${error}<button class="btn play" id="run-now">${svg("play")}${esc(TEXT.runNow)}</button></div>`;
    } else if (waiting) {
      main = `<div class="hero"><div class="hero-title">${plural(waiting, "new conversation")} since your last run</div>
        <div class="lead">The improver finds where Assist could've done better, and tests fixes for it.
        ${run ? `${esc(this._lastRun(run))}.` : ""}</div>${error}
        <button class="btn play" id="run-now">${svg("play")}${esc(TEXT.runNow)}</button></div>`;
    } else if (!preview?.conversations_recorded) {
      main = `<div class="hero"><div class="hero-title">Use Assist as usual</div>
        <div class="lead">The improver learns from your conversations with Assist. Once you've had a few, run it here.</div></div>`;
    } else {
      main = `<div class="hero"><div class="hero-title">All caught up</div>
        <div class="lead">Nothing to review, and no new conversations since your last run. ${run ? `${esc(this._lastRun(run))}.` : ""}</div>${error}
        <button class="link" id="run-anyway">Run anyway</button></div>`;
    }
    const history = earlier.length ? `<details class="history" data-key="history" ${this._open.has("history") ? "open" : ""}>
      <summary><span class="name">Earlier improvements (${earlier.length})</span></summary>
      <div class="card">${this._rows(earlier)}</div></details>` : "";
    const settings = `<button class="tb-btn" id="open-settings" title="Settings" aria-label="Settings">
      <svg viewBox="0 0 24 24"><path d="${COG}"/></svg></button>`;
    return this._toolbar("Assist Improver", false, (this._developer ? this._devLink() : "") + settings) +
      `<div class="page">${running ? this._progress(run) : ""}${offer}${main}${history}</div>${this._confirming && preview ? this._confirm(preview) : ""}`;
  }

  // Settings: the pipeline drawn as a track, each AI step's model a click away, then the other settings.

  _task(entityId) {
    return this._settings.ai_tasks.find((t) => t.entity_id === entityId);
  }

  _layout(W) {
    // Where each step card sits and the track runs: left to right when wide, top to bottom when narrow.
    return W < 820 ? this._layoutVertical(W) : this._layoutWide(W);
  }

  _layoutWide(W) {
    // Your Assist and triage on the intake, the loop's steps along its top, your review at its far end, and your
    // household below that, where the track peels off. Each card is as big as its content (measured after it's
    // drawn, in _measure); the layout spaces them by those sizes, and turns vertical when they don't fit across.
    const size = (id) => this._sizes?.[id] || (["you", "household"].includes(id) ? {w: 140, h: 42} : {w: id === "requests" ? 150 : 120, h: 68});
    const A = size("requests"), T = size("triage"), Y = size("you"), Hh = size("household");
    const topW1 = Math.max(...["analyze", "propose", "test", "write", "review"].map((id) => size(id).w));  // one width, even rhythm
    const top = ["analyze", "propose", "test", "write"].map((id) => [id, {...size(id), w: topW1, fixed: true}]);
    const R = {...size("review"), w: topW1, fixed: true};
    const hTop = Math.max(...top.map(([, s]) => s.h));
    // Gaps between cards (g), between Your Assist and triage (gi), before the loop (m) and inside its ends (p): roomy
    // when there's space, tighter on a narrow window, and vertical when even the tight ones don't fit.
    const fixed = A.w + T.w + top.reduce((sum, [, s]) => sum + s.w, 0) + Y.w / 2;
    const roomy = {g: 28, gi: 30, m: 22, p: 18}, tight = {g: 20, gi: 22, m: 10, p: 8};  // corner buttons need 10px + room
    const span = ({g, gi, m, p}) => gi + m + 2 * p + 3 * g;
    const t = Math.max(0, Math.min(1, (fixed + span(roomy) - W) / (span(roomy) - span(tight))));
    if (fixed + span(tight) > W) return this._layoutVertical(W);
    const [g, gi, m, p] = ["g", "gi", "m", "p"].map((k) => roomy[k] - t * (roomy[k] - tight[k]));
    const content = fixed + span({g, gi, m, p});  // your household lines up under your review
    const x0 = (W - content) / 2;
    const below = 40;  // between the loop's steps and the reviewer: room for the block buttons and the small loop's arrows
    const r = Math.max(76, (hTop / 2 + below + R.h + 12 + 13) / 2);
    const yT = hTop / 2 + 12, yC = yT + r, yB = yT + 2 * r, yIn = yC + 30;  // the loop's top, middle and bottom; the intake
    const box = (cx, cy, s) => ({cx, cy, w: s.w, h: s.h, x: cx - s.w / 2, y: cy - s.h / 2, fixed: s.fixed});
    const at = {requests: box(x0 + A.w / 2, yIn, A), triage: box(x0 + A.w + gi + T.w / 2, yIn, T)};
    const xL = x0 + A.w + gi + T.w + m;
    let cursor = xL + p;
    for (const [id, s] of top) { at[id] = box(cursor + s.w / 2, yT, s); cursor += s.w + g; }
    const xR = cursor - g + p;
    at.review = box(at.propose.cx, yT + hTop / 2 + below + R.h / 2, R);
    at.you = box(xR, yC, Y);
    at.household = box(xR + Math.min(0, Y.w / 2 - Hh.w / 2), yB + 24 + Hh.h / 2, Hh);
    const j = [xL + r - r * Math.SQRT1_2, yC - r * Math.SQRT1_2];
    const start = at.triage.x + T.w - 12, k = 0.45 * Math.hypot(j[0] - start, yIn - j[1]);
    const mw = 64, pc = at.propose.cx, gapY = (yT + hTop / 2 + 11 + at.review.y) / 2;  // below the block buttons
    return {W, H: at.household.y + Hh.h + 6, at, vertical: false,
      loop: {x: xL, y: yT, w: xR - xL, h: 2 * r, r},
      intake: `M${at.requests.cx},${yIn}H${start}C${start + 0.55 * (j[0] - start)},${yIn} ${j[0] - 0.7071 * k},${j[1] + 0.7071 * k} ${j[0]},${j[1]}`,
      inGrad: [x0, yIn, j[0], j[1]],
      exit: `M${xR},${yC}V${at.household.y + 14}`, outGrad: [xR, yC, xR, at.household.y],
      mini: {x: pc - mw / 2, y: yT, w: mw, h: at.review.cy - yT, sw: 22}, label: [(xL + xR) / 2, yB],
      chevrons: [[xL + r + (xR - xL - 2 * r) * 0.14, yB, "left"], [xL + r + (xR - xL - 2 * r) * 0.86, yB, "left"],
                 [pc - mw / 2, gapY, "down"], [pc + mw / 2, gapY, "up"]]};
  }

  _measure() {
    // Each card's own size in the wide layout, measured on a hidden copy (so it works whichever layout shows), for
    // the next layout; drawn again only if one changed.
    const page = this.shadowRoot?.querySelector(".page");
    if (!page || !this._settings) return;
    const ids = Object.keys(STEPS), zero = {cx: 0, cy: 0, x: 0, y: 0, w: 0, h: 0};
    const probe = document.createElement("div");
    probe.className = "track";
    probe.style.cssText = "position:absolute;visibility:hidden;left:-10000px;top:0;width:2000px;height:200px";
    probe.innerHTML = ids.map((id) => this._step(id, {vertical: false, at: Object.fromEntries(ids.map((i) => [i, zero]))},
                                                 this._settings)).join("");
    page.appendChild(probe);
    const sizes = {};
    let changed = false;
    probe.querySelectorAll(".stn").forEach((el) => {
      const r = el.getBoundingClientRect();  // Exact (offsetWidth rounds), plus a pixel so nothing truncates.
      const s = {w: Math.ceil(r.width) + 1, h: Math.ceil(r.height)}, old = this._sizes?.[el.dataset.step];
      sizes[el.dataset.step] = s;
      changed = changed || !old || Math.abs(old.w - s.w) > 1 || Math.abs(old.h - s.h) > 1;
    });
    probe.remove();
    if (changed) { this._sizes = sizes; this._render(); }
  }

  _layoutVertical(W) {
    // On a phone: one wide band straight down through the steps; the proposer and reviewer go back and forth along the
    // left edge, and a revision goes back up the right edge to the proposer.
    // The side lanes run midway between the panel's edge (its 16px padding outside the track) and the cards' sides.
    const g = 14, h = 52, hs = 46, pad = 16, cx = 32, cw = W - 2 * cx, eL = (cx - pad) / 2, eR = W - eL;
    const at = {}, place = (id, gap, inset = 0, height = h) => {
      at[id] = {x: cx + inset, y, w: cw - 2 * inset, h: height}; y += height + gap;
    };
    let y = 0;
    place("requests", g); place("triage", g); place("analyze", g); place("propose", 24); place("review", 24, 0, hs);
    place("test", g); place("write", g); place("you", g); place("household", 0);
    const mid = cx + cw / 2, centre = (id) => at[id].y + at[id].h / 2;
    const pc = centre("propose"), rc = centre("review"), yc = centre("you");
    const side = (x, from, to, inner) => {  // A return lane: out of one card's edge, up the side, into another's.
      const s = Math.sign(inner - x), r = 12;
      return `M${inner},${from}H${x + s * r}Q${x},${from} ${x},${from - r}V${to + r}Q${x},${to} ${x + s * r},${to}H${inner}`;
    };
    return {W, H: at.household.y + h + 4, at, vertical: true,
      paths: [{d: `M${mid},${at.requests.y + 10}V${at.household.y + h - 10}`, w: Math.min(150, cw * 0.44), cap: "butt"},
              {d: side(eL, rc, pc, at.review.x + 6), w: 18}, {d: side(eR, yc, pc, cx + cw - 6), w: 18}],
      flow: `M${mid},${centre("requests")}V${centre("household")}`,
      label: [eR, (pc + yc) / 2], labelVertical: true,
      chevrons: [[mid, (at.propose.y + h + at.review.y) / 2, "down"], [eL, (pc + rc) / 2, "up"],
                 [eR, pc + (yc - pc) * 0.22, "up"], [eR, pc + (yc - pc) * 0.78, "up"]]};
  }

  _trackSvg(L, running) {
    const w = 26;
    const grad = (id, [x1, y1, x2, y2], from, to) => `<linearGradient id="${id}" gradientUnits="userSpaceOnUse" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}">
      <stop offset="0" style="stop-color:var(${from})"/><stop offset="1" style="stop-color:var(${to})"/></linearGradient>`;
    const chev = ([x, y, dir]) => {
      const d = {left: `M${x + 3},${y - 5}L${x - 3},${y}L${x + 3},${y + 5}`, up: `M${x - 5},${y + 3}L${x},${y - 3}L${x + 5},${y + 3}`,
                 down: `M${x - 5},${y - 3}L${x},${y + 3}L${x + 5},${y - 3}`}[dir];
      return `<path class="chev" d="${d}"/>`;
    };
    const band = [], defs = [];
    if (L.loop) {
      const {loop, mini} = L;
      defs.push(grad("ai-in", L.inGrad, "--track-in", "--track-ai"), grad("ai-out", L.outGrad, "--track-ai", "--track-out"));
      band.push(`<rect x="${loop.x}" y="${loop.y}" width="${loop.w}" height="${loop.h}" rx="${loop.r}" fill="none" stroke="var(--track-ai)" stroke-width="${w}"/>`,
        `<path d="${L.intake}" fill="none" stroke="url(#ai-in)" stroke-width="${w}"/>`,
        `<path d="${L.exit}" fill="none" stroke="url(#ai-out)" stroke-width="${w}" stroke-linecap="round"/>`,
        `<rect x="${mini.x}" y="${mini.y}" width="${mini.w}" height="${mini.h}" rx="${Math.min(mini.w, mini.h) / 2}" fill="none" stroke="var(--track-ai)" stroke-width="${mini.sw}"/>`);
    }
    for (const p of L.paths || []) {
      band.push(`<path d="${p.d}" fill="none" stroke="var(--track-ai)" stroke-width="${p.w}" stroke-linecap="${p.cap || "round"}" stroke-linejoin="round"/>`);
    }
    const flowing = !running ? "" : L.loop
      ? `<rect class="flowing" x="${L.loop.x}" y="${L.loop.y}" width="${L.loop.w}" height="${L.loop.h}" rx="${L.loop.r}"/>`
      : `<path class="flowing" d="${L.flow}"/>`;
    return `<svg width="${L.W}" height="${L.H}" viewBox="0 0 ${L.W} ${L.H}"><defs>${defs.join("")}</defs>
      <g class="band">${band.join("")}</g>${flowing}${L.chevrons.map(chev).join("")}</svg>`;
  }

  _assistant() {
    // The assistant Assist uses by default, and the household knowledge it reads.
    const h = this._household || {};
    return {pipeline: (h.pipelines || []).find((p) => p.preferred), lines: h.lines || 0};
  }

  _knows(pipeline, lines) {
    // Household knowledge for one assistant, as a toggle glyph: a book with a heart, tinted while it reads it.
    if (!pipeline?.target) return "";
    const tip = esc(fill(TEXT.assistant[pipeline.using ? "knowsOn" : "knowsOff"], {name: pipeline.name, lines: plural(lines, "line")}));
    return `<button class="knows ${pipeline.using ? "on" : ""}" data-knows="${esc(pipeline.target)}" data-on="${pipeline.using ? "" : "1"}"
      data-tip="${tip}" aria-label="Household knowledge">${glyph("mdi:book-heart-outline")}</button>`;
  }

  _step(id, L, s) {
    const step = STEPS[id], box = L.at[id], run = this._run?.run, preview = this._run?.preview || {};
    const backlog = preview.backlog || {}, paused = new Set(s.paused);
    const isPaused = step.gate && paused.has(step.gate);
    const atWork = run?.status === "running" && (AT_WORK[run.stage] || []).includes(id);
    const n = backlog[BACKLOG[id]] || 0;
    let badge = "";
    if (n && id !== "household") {
      const kind = isPaused ? "held" : id === "requests" ? "in" : id === "you" ? "out" : "";
      const what = fill(TEXT.badges[isPaused ? "held" : id === "you" ? "review" : id === "requests" ? "new" : "waiting"], {n});
      badge = `<span class="badge ${kind}" data-tip="${what}">${n}</span>`;
    }
    let chip = "";
    if (step.role) {
      const role = s.roles[step.role], task = this._task(role.entity_id), M = TEXT.models;
      const warn = !!role.problem || (role.size === "large" && task?.weak);
      const off = role.problem === "none" ? M.none : id === "triage" ? TEXT.passThrough : {label: "Off", tip: TEXT.followUpsOff};
      const label = role.problem === "missing" ? M.missing.label : role.entity_id ? (task?.label || role.entity_id) : off.label;
      const tip = role.problem === "missing" ? M.missing.tip : role.problem === "unavailable" ? M.unavailable
        : role.entity_id ? (warn ? M.weak : "") : off.tip;
      chip = `<button class="chip-model ${warn ? "warn" : ""} ${role.entity_id ? "" : "off"}" data-role="${step.role}"
        ${tip ? `data-tip="${esc(tip)}"` : ""} aria-label="${esc(step.title)}: ${esc(label)}">${warn ? "&#9888; " : ""}${esc(label)} &#9662;</button>`;
    } else if (id === "test") {
      chip = `<button class="chip-model fixed" data-knob="test" data-tip="${esc(fill(TEXT.simulate.tip, {n: s.tests_per_goal}))}">${esc(TEXT.simulate.label)}</button>`;
    } else if (id === "requests") {
      const {pipeline, lines} = this._assistant();
      chip = `<div class="chip-row"><button class="chip-model" data-assist="1" data-tip="${esc(TEXT.assistant.tip)}">${
        esc(pipeline?.name || "Choose…")} &#9662;</button>${this._knows(pipeline, lines)}</div>`;
    } else if (id === "household") {
      const lines = this._household?.lines || 0;
      chip = `<div class="chip-row"><button class="chip-model" data-knowledge="1" data-tip="${esc(TEXT.knowledge.tip)}">${
        glyph("mdi:book-heart-outline", "in-chip")}${esc(fill(TEXT.knowledge[L.vertical ? "chipShort" : "chip"], {n: plural(lines, "line")}))}</button>${
        backlog.household ? `<span class="done-count" data-tip="${esc(fill(TEXT.badges.applied, {n: backlog.household}))}">${svg("check")}${backlog.household}</span>` : ""}</div>`;
    }
    const gate = step.gate ? `<button class="gate ${isPaused ? "paused" : ""}" data-gate="${step.gate}"
      data-tip="${esc(fill(TEXT.block[isPaused ? "blocked" : "tip"], {step: step.title.toLowerCase()}))}"
      aria-label="${isPaused ? "Unblock" : "Block"} ${esc(step.title)}" aria-pressed="${!!isPaused}">${svg("block")}</button>` : "";
    const info = `<span class="info-tip" data-tip="${esc(step.tip)}" tabindex="0" aria-label="About ${esc(step.title)}">${svg("info")}</span>`;
    const cls = [id === "review" && "sub", id === "you" && "you", atWork && "now", isPaused && "paused"].filter(Boolean).join(" ");
    const place = L.vertical ? `left:${box.x}px;top:${box.y}px;width:${box.w}px;height:${box.h}px`
      : `left:${box.cx}px;top:${box.cy}px${box.fixed ? `;width:${box.w}px` : ""}`;
    return `<div class="stn ${cls}" data-step="${id}" style="${place}">
      <div class="s-head">${glyph(step.icon)}<span class="s-title">${esc(step.title)}</span>${info}${L.vertical ? "" : gate}</div>
      ${chip}${L.vertical ? gate : ""}${badge}</div>`;
  }

  _renderSettings(s) {
    const width = this._trackWidth || Math.max(300, Math.min(this.clientWidth || 1000, 1180) - 64);
    const L = this._layout(width);
    const running = this._run?.run?.status === "running";
    const preview = this._run?.preview;
    const backlog = preview?.backlog || {};
    const ids = ["requests", "triage", "analyze", "propose", "review", "test", "write", "you", "household"];
    const last = esc(this._lastRun(this._run?.run) || "No runs yet");
    const when = SCHEDULE.find(([value]) => value === s.schedule) || SCHEDULE[0];
    // One run control: Run now, and its menu for when it runs on its own and for running a single step.
    const menu = this._runMenu ? `<div class="menu">${SCHEDULE.map(([value, label]) =>
      `<div data-schedule="${value}" class="row-ic ${s.schedule === value ? "on" : ""}">${value === "off" ? `<span class="ic-space"></span>` : svg("clock")}<span class="lbl">${label}</span>${
        s.schedule === value ? `<span class="count">&#10003;</span>` : ""}</div>`).join("")}
      <div class="group">${esc(TEXT.runOneStep)}</div>${Object.entries(GATE_NAMES).map(([gate, name]) =>
        `<div data-only="${gate}" class="row-ic">${svg("play")}<span class="lbl">${name}</span>${backlog[gate] ? `<span class="count">${backlog[gate]}</span>` : ""}</div>`).join("")}</div>` : "";
    const run = running
      ? `<button class="btn ghost" id="cancel-run" data-tip="Running: ${esc((STAGES.find(([k]) => k === this._run.run.stage) || [])[1] || "")}">Cancel run</button>`
      : `<div class="split down"><button class="main" id="run-now" data-tip="${last}">${svg("play")}${esc(TEXT.runNow)}</button>
         <button class="arrow" id="run-menu" aria-label="When it runs, or run one step" data-tip="${esc(when[1])}">${s.schedule === "off" ? "&#9662;" : svg("clock")}</button>${menu}</div>`;
    const sandboxTip = TEXT.sandbox.tip + (s.practice.others.length ? " " + fill(TEXT.sandbox.others, {names: s.practice.others.join(", ")}) : "");
    const toggle = (id, on) => customElements.get("ha-switch")
      ? `<ha-switch id="${id}" ${on ? "checked" : ""}></ha-switch>`
      : `<button class="toggle ${on ? "on" : ""}" id="${id}" role="switch" aria-checked="${on}"></button>`;
    const row = (id, on, text, tip) => `<div class="toggle-row"><label class="practice">${toggle(id, on)}${esc(text)}</label>
      ${tip ? `<span class="info-tip" data-tip="${esc(tip)}" tabindex="0" aria-label="About ${esc(text)}">${svg("info")}</span>` : ""}</div>`;
    const head = `<div class="flow-head"><div class="toggles">${row("on-demand", s.on_demand, TEXT.onDemand.label, TEXT.onDemand.tip)}
      ${row("practice", s.practice.on, TEXT.sandbox.label, sandboxTip)}
      ${row("auto-accept", s.auto_accept, TEXT.autoAccept.label, TEXT.autoAccept.tip)}</div>
      <span class="spacer"></span>${run}</div>`;
    const label = `<span class="stream-label ${L.labelVertical ? "vertical" : ""}" style="left:${L.label[0]}px;top:${L.label[1]}px">${
      esc(L.vertical ? TEXT.revise.short : TEXT.revise.label)}</span>`;
    const flow = `<div class="card flow-card">${head}<div class="track ${L.vertical ? "vertical" : ""}" style="height:${L.H}px">
      ${this._trackSvg(L, running)}${ids.map((id) => this._step(id, L, s)).join("")}${label}</div></div>`;
    const confirm = this._confirming && preview ? this._confirm(preview) : this._asking === "auto-accept" ? this._askAutoAccept() : "";
    const shade = this._runMenu ? `<div class="backdrop" id="menu-backdrop"></div>` : "";
    return this._toolbar("Settings", true, s.developer ? this._devLink() : "") +
      `<div class="page wide">${flow}${this._history()}</div>${shade}${this._popover(s)}${confirm}`;
  }

  _history() {
    // What the improver did, newest first: runs, improvements going up for review, and decisions on them.
    const events = this._log || [], shown = this._historyAll ? events : events.slice(0, 8), H = TEXT.history;
    const icon = {run: "mdi:play-circle-outline", posted: "mdi:feather", accept: "mdi:check-circle-outline",
                  undo: "mdi:undo-variant", decline: "mdi:close-circle-outline", revise: "mdi:pencil-outline"};
    const runText = (e) => {
      const p = e.progress || {}, parts = [p.taken_in && `${p.taken_in} taken in`, p.analyzed && `${p.analyzed} analyzed`,
        p.new_improvements && plural(p.new_improvements, "new improvement"), p.auto_accepted && `${p.auto_accepted} accepted automatically`,
        p.already_addressed && fill(H.addressed, {n: p.already_addressed})];
      const failed = p.analyze_failed && fill(H.failed, {n: p.analyze_failed, s: p.analyze_failed === 1 ? "" : "s", why: p.problem || ""});
      const unfinished = (p.problems || []).length && fill(H.unfinished, {steps: listed(p.problems.map((x) => GATE_NAMES[x.stage] || x.stage))});
      const aside = p.unreadable && fill(H.setAside, {n: p.unreadable, s: p.unreadable === 1 ? "" : "s"});
      return [`${H.run[e.status] || "Run"}${e.only ? ` · ${GATE_NAMES[e.only]} only` : ""}${e.trigger === "scheduled" ? " · scheduled" : ""}`,
              e.error ? (/ can't run: /.test(e.error) ? e.error : H.stopped) : [...parts, failed, unfinished, aside].filter(Boolean).join(" · ")];
    };
    const rows = shown.map((e) => {
      const [title, sub] = e.kind === "run" ? runText(e) : e.kind === "posted" ? [H.posted, e.title]
        : [e.auto ? H.auto : H.decisions[e.decision] || e.decision, e.title + (e.by && !e.auto ? ` · ${e.by}` : "")];
      const glyphName = e.kind === "decision" ? icon[e.decision] || "mdi:account-check-outline" : icon[e.kind];
      return `<div class="h-row ${e.id ? "h-link" : ""}" ${e.id ? `data-open="${esc(e.id)}"` : ""}>${glyph(glyphName)}
        <div class="h-text"><div>${esc(title)}</div>${sub ? `<div class="h-sub">${esc(sub)}</div>` : ""}</div>
        <span class="h-when">${esc(ago(e.at))}</span></div>`;
    }).join("");
    return `<div class="card history"><div class="h-title">${esc(H.title)}</div>${rows || `<div class="h-empty">${esc(H.empty)}</div>`}
      ${events.length > shown.length ? `<div class="h-more"><button class="link" id="history-more">${esc(H.more)}</button></div>` : ""}</div>`;
  }

  _relayout() {
    // Lay the track out again when its width changed (and on first sight, once the width is known).
    const track = this.shadowRoot?.querySelector(".track");
    if (!track) return;
    const width = Math.round(track.clientWidth);
    if (width && Math.abs(width - (this._trackWidth || 0)) > 2) { this._trackWidth = width; this._render(); }
  }

  _popover(s) {
    const pop = this._pop;
    if (!pop) return "";
    const step = Object.values(STEPS).find((x) => x.role === pop.role) || STEPS.test;
    const options = (role, roleName) => {
      const fit = (t) => role.size === "large" ? (t.strong ? 2 : t.weak ? 0 : 1) : (t.small ? 2 : 1);
      const tasks = [...s.ai_tasks].sort((a, b) => fit(b) - fit(a));
      const tag = (t) => role.size === "large" ? (t.strong ? `<span class="tag good">Recommended</span>`
        : t.weak ? `<span class="tag bad">Not recommended</span>` : !t.known ? `<span class="tag">${esc(TEXT.models.unknown)}</span>` : "")
        : (t.small ? `<span class="tag good">Good fit</span>` : "");
      const why = (t) => role.size !== "large" ? "" : t.weak && t.warnings.length ? t.warnings[0] : !t.known ? TEXT.models.unknownTip : "";
      return tasks.map((t) => `<div class="opt" data-pick="${esc(t.entity_id)}" ${
          why(t) ? `data-tip="${esc(why(t))}"` : ""}><div class="opt-main">
          <span class="check">${t.entity_id === role.entity_id ? "&#10003;" : ""}</span><span class="opt-label">${esc(t.label)}</span>${tag(t)}</div>
          ${t.title !== t.label ? `<div class="opt-sub">${esc(t.title)}</div>` : ""}</div>`).join("") +
        (role.optional ? `<div class="opt" data-pick="" data-tip="${esc(roleName === "triage" ? TEXT.passThrough.tip : TEXT.followUpsOff)}"><div class="opt-main"><span class="check">${role.entity_id ? "" : "&#10003;"}</span>
          <span class="opt-label">${esc(roleName === "triage" ? TEXT.passThrough.label : "Off")}</span></div></div>` : "");
    };
    let inner;
    if (pop.kind === "knowledge") {
      // A modal, since the list grows: everyone's lines, then each person's; each clamped to two lines (Read more),
      // editable in place, searchable once the list is long.
      const h = this._household || {}, K = TEXT.knowledge, st = this._kn || {};
      const sections = [{scope: "everyone", name: K.everyone, lines: h.everyone || []},
                        ...(h.people || []).map((p) => ({scope: p.user_id, name: p.name, lines: p.lines}))];
      const me = h.me;
      if (me && st.adding === me.user_id && !sections.some((s) => s.scope === me.user_id)) sections.push({scope: me.user_id, name: me.name, lines: []});
      const editor = (value) => `<div class="kn-editor"><textarea id="kn-text" placeholder="${esc(K.placeholder)}">${esc(value)}</textarea>
        ${st.error ? `<div class="error-text">${esc(st.error)}</div>` : ""}
        <div class="kn-buttons"><button class="btn ghost" id="kn-cancel">${esc(K.cancel)}</button><button class="btn" id="kn-save" ${st.busy ? "disabled" : ""}>${esc(K.save)}</button></div></div>`;
      const line = (scope, i, text) => {
        const key = `${scope}:${i}`;
        if (st.edit === key) return `<li class="kn-line editing">${editor(text)}</li>`;
        const open = (st.open || new Set()).has(key);
        const confirm = st.remove === key ? `<div class="kn-confirm"><span>${esc(K.confirmRemove)}</span>
          <button class="link" id="kn-cancel">${esc(K.cancel)}</button><button class="btn danger" id="kn-remove" ${st.busy ? "disabled" : ""}>${esc(K.remove)}</button></div>` : "";
        return `<li class="kn-line"><div class="kn-row"><div class="kn-main"><div class="kn-text ${open ? "" : "clamp"}">${esc(text)}</div>
          <button class="link kn-more" data-kn-more="${esc(key)}">${esc(open ? K.less : K.more)}</button></div>
          <div class="kn-icons"><button class="kn-icon" data-kn-edit="${esc(key)}" aria-label="${esc(K.edit)}" data-tip="${esc(K.edit)}">${glyph("mdi:pencil-outline")}</button>
          <button class="kn-icon" data-kn-ask="${esc(key)}" aria-label="${esc(K.remove)}" data-tip="${esc(K.remove)}">${glyph("mdi:trash-can-outline")}</button></div></div>${confirm}</li>`;
      };
      const group = ({scope, name, lines}) => `<section class="kn-section"><div class="kn-group">${esc(name)}<span>${lines.length || ""}</span></div>
        <ul>${lines.map((text, i) => line(scope, i, text)).join("")}
        ${st.adding === scope ? `<li class="kn-line editing">${editor(st.draft || "")}</li>`
          : `<li class="kn-add"><button class="link" data-kn-add="${esc(scope)}">+ ${esc(K.add)}</button></li>`}</ul></section>`;
      const forMe = me && !sections.some((s) => s.scope === me.user_id)
        ? `<div class="kn-add kn-add-me"><button class="link" data-kn-add="${esc(me.user_id)}">+ ${esc(fill(K.addFor, {name: me.name}))}</button></div>` : "";
      return `<div class="modal-shade" id="backdrop"></div><div class="modal" role="dialog" aria-modal="true" aria-label="${esc(K.title)}">
        <div class="modal-head">${glyph("mdi:book-heart-outline")}<b>${esc(K.title)}</b><span class="kn-count">${plural(h.lines || 0, "line")}</span>
          <button class="modal-close" id="modal-close" aria-label="Close">&#10005;</button></div>
        ${(h.lines || 0) > 6 ? `<input class="kn-search" id="kn-search" type="search" placeholder="${esc(K.search)}">` : ""}
        <div class="modal-body">${sections.map(group).join("")}${forMe}<div class="pop-lead kn-none" hidden>${esc(K.none)}</div></div></div>`;
    } else if (pop.kind === "assist") {
      const h = this._household || {}, list = h.pipelines || [];
      inner = `<div class="pop-head"><b>Your Assist</b> uses</div>` + (list.length ? list.map((p) =>
        `<div class="opt" data-prefer="${esc(p.id)}"><div class="opt-main"><span class="check">${p.preferred ? "&#10003;" : ""}</span>
          <span class="opt-label">${esc(p.name)}</span><span class="spacer"></span>${this._knows(p, h.lines || 0)}</div></div>`).join("")
        : `<div class="pop-lead">${esc(TEXT.assistant.none)}</div>`) +
        `<div class="pop-lead pop-foot">${glyph("mdi:book-heart-outline")}<span>${esc(fill(TEXT.assistant.lines, {lines: plural(h.lines || 0, "line")}))}</span></div>`;
    } else if (pop.kind === "test") {
      const n = s.tests_per_goal;
      inner = `<div class="pop-head"><b>Simulate</b> on your assistant</div>
        <div class="stepper-row" data-tip="${esc(fill(TEXT.simulate.cost, {n}))}">
        <button class="round" id="t-minus" ${n <= 1 ? "disabled" : ""}>&#8722;</button>
        <span class="t-n">${n}</span><button class="round" id="t-plus" ${n >= 20 ? "disabled" : ""}>+</button></div>
        <div class="pop-lead" style="text-align:center">${esc(TEXT.simulate.perGoal)}</div>
        <div class="pop-head" data-tip="${esc(TEXT.followUpsOff)} when off">Check follow-ups with</div>${options(s.roles.followup_check, "followup_check")}`;
    } else if (pop.kind === "create") {
      const providers = s.providers;
      const provider = providers.find((p) => p.entry_id === pop.entry_id);
      const models = pop.models || [];
      inner = `<div class="pop-head">New AI Task for <b>${esc(step.title)}</b></div>` + (!providers.length
        ? `<div class="pop-lead">Add an AI provider (such as Anthropic or OpenRouter) in Settings &#8594; Devices &amp; services first.</div>`
        : `<div class="form">
          ${providers.length > 1 ? `<label>Provider<select id="c-provider">${providers.map((p) =>
            `<option value="${esc(p.entry_id)}" ${p.entry_id === pop.entry_id ? "selected" : ""}>${esc(p.title)}</option>`).join("")}</select></label>` : ""}
          ${pop.loading ? `<div class="pop-lead" style="padding:0">Loading models…</div>` : models.length
            ? `<label>Model<select id="c-model">${models.map((m) =>
                `<option value="${esc(m.value)}" ${m.value === pop.model ? "selected" : ""}>${esc(m.label)}</option>`).join("")}</select></label>`
            : `<label>Model<input id="c-model" value="${esc(pop.model || "")}" placeholder="The model's ID"></label>`}
          ${pop.named ? `<label>Name<input id="c-name" value="${esc(pop.name || "")}"></label>` : ""}
          ${pop.error ? `<div class="error-text">${esc(pop.error)}</div>` : ""}
          <div class="actions"><button class="btn ghost" id="c-back">Back</button>
          <button class="btn" id="c-create" data-tip="Added to ${esc(provider?.title || "the provider")} with its default settings for the rest" ${pop.loading || pop.busy ? "disabled" : ""}>${pop.busy ? "Adding…" : "Add and use"}</button></div></div>`);
    } else {
      const role = s.roles[pop.role];
      inner = `<div class="pop-head" data-tip="${esc(step.tip)} ${esc(TEXT.models[role.size])}"><b>${esc(step.title)}</b> uses</div>
        ${options(role, pop.role)}<div class="opt new" id="new-task">+ New AI Task…</div>
        ${s.recommended === false ? `<div class="opt new recommended" id="use-recommended" data-tip="${esc(TEXT.models.recommendedTip)}">${
          pop.busy ? esc(TEXT.models.recommendedBusy) : `&#10022; ${esc(TEXT.models.recommended)}`}</div>` : ""}
        ${pop.error ? `<div class="pop-lead error-text">${esc(pop.error)}</div>` : ""}`;
    }
    return `<div class="backdrop" id="backdrop"></div><div class="pop" id="pop">${inner}</div>`;
  }

  _placePop() {
    // Under the control that opened it, kept on screen; a sheet from the bottom on a phone.
    const pop = this.shadowRoot.getElementById("pop");
    if (!pop) return;
    if (window.innerWidth < 600) { pop.classList.add("sheet"); return; }
    const anchor = this.shadowRoot.querySelector(this._pop.anchor);
    if (!anchor) return;
    const a = anchor.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
    const left = Math.min(Math.max(8, a.left), window.innerWidth - w - 8);
    const top = a.bottom + 6 + h > window.innerHeight - 8 ? Math.max(8, a.top - h - 6) : a.bottom + 6;
    pop.style.left = `${left}px`; pop.style.top = `${top}px`;
  }

  async _saveSetting(change) {
    try { this._settings = await this._hass.callWS({type: "assist_improver/settings/set", ...change}); }
    catch (err) { alert(err.message || String(err)); }
    if ("stage" in change) this._run = await this._hass.callWS({type: "assist_improver/run"});  // Backlogs held or freed.
    this._render();
  }

  async _loadModels(entryId) {
    const role = this._settings.roles[this._pop.role];
    Object.assign(this._pop, {entry_id: entryId, loading: true, models: [], error: null}); this._render();
    try {
      const found = await this._hass.callWS({type: "assist_improver/settings/models", entry_id: entryId});
      const model = found.suggested?.[role.size] || found.default || found.models[0]?.value || "";
      Object.assign(this._pop, {models: found.models, named: found.named, model, loading: false, nameEdited: false});
      this._suggestName();
    } catch (err) {
      Object.assign(this._pop, {loading: false, error: err.message || String(err)});
    }
    this._render();
  }

  _suggestName() {
    if (this._pop.nameEdited) return;
    const m = (this._pop.models || []).find((o) => o.value === this._pop.model);
    this._pop.name = `Assist Improver (${m?.label || this._pop.model})`;
  }

  _wireKnowledge(root) {
    // The household knowledge modal: read more, edit in place, delete (after asking), add.
    if (this._pop?.kind !== "knowledge") return;
    const st = this._kn || (this._kn = {open: new Set()}), h = this._household || {};
    const linesOf = (scope) => scope === "everyone" ? h.everyone || [] : ((h.people || []).find((p) => p.user_id === scope) || {}).lines || [];
    const split = (key) => { const at = key.lastIndexOf(":"); return [key.slice(0, at), Number(key.slice(at + 1))]; };
    root.querySelectorAll(".kn-text.clamp").forEach((el) => {  // Read more only where the line is cut.
      const more = el.parentElement.querySelector(".kn-more");
      if (more && el.scrollHeight <= el.clientHeight + 1) more.hidden = true;
    });
    root.querySelectorAll("[data-kn-more]").forEach((el) => el.addEventListener("click", () => {
      const key = el.dataset.knMore;
      st.open.has(key) ? st.open.delete(key) : st.open.add(key); this._render();
    }));
    const reset = () => { st.edit = st.remove = st.adding = null; st.draft = ""; st.error = null; st.busy = false; };
    root.querySelectorAll("[data-kn-edit]").forEach((el) => el.addEventListener("click", () => {
      reset(); st.edit = el.dataset.knEdit; this._render(); root.getElementById("kn-text")?.focus();
    }));
    root.querySelectorAll("[data-kn-ask]").forEach((el) => el.addEventListener("click", () => {
      reset(); st.remove = el.dataset.knAsk; this._render();
    }));
    root.querySelectorAll("[data-kn-add]").forEach((el) => el.addEventListener("click", () => {
      reset(); st.adding = el.dataset.knAdd; this._render(); root.getElementById("kn-text")?.focus();
    }));
    root.querySelectorAll("#kn-cancel").forEach((el) => el.addEventListener("click", () => { reset(); this._render(); }));
    root.getElementById("kn-text")?.addEventListener("input", (ev) => { st.draft = ev.target.value; });
    const send = async (msg) => {
      st.busy = true; st.error = null; this._render();
      try {
        await this._hass.callWS({type: "assist_improver/household/edit", ...msg});
        this._household = await this._hass.callWS({type: "assist_improver/household"});
        reset();
      } catch (err) { st.busy = false; st.error = err.message || String(err); }
      this._render();
    };
    root.getElementById("kn-save")?.addEventListener("click", () => {
      const text = root.getElementById("kn-text")?.value || "";
      if (st.adding) send({scope: st.adding, action: "add", text});
      else if (st.edit) { const [scope, index] = split(st.edit); send({scope, action: "update", index, old: linesOf(scope)[index], text}); }
    });
    root.getElementById("kn-remove")?.addEventListener("click", () => {
      const [scope, index] = split(st.remove); send({scope, action: "delete", index, old: linesOf(scope)[index]});
    });
  }

  _wireSettings(root) {
    root.querySelectorAll("[data-role]").forEach((el) => el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      this._pop = {kind: "role", role: el.dataset.role, anchor: `[data-role="${el.dataset.role}"]`}; this._render();
    }));
    root.querySelector("[data-knob]")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      this._pop = {kind: "test", role: "followup_check", anchor: "[data-knob]"}; this._render();
    });
    root.querySelector("[data-knowledge]")?.addEventListener("click", (ev) => {
      ev.stopPropagation(); this._pop = {kind: "knowledge", anchor: "[data-knowledge]"}; this._kn = {open: new Set()}; this._render();
    });
    root.getElementById("modal-close")?.addEventListener("click", () => { this._pop = null; this._render(); });
    this._wireKnowledge(root);
    root.getElementById("kn-search")?.addEventListener("input", (ev) => {  // Filters in place, keeping the cursor.
      const q = ev.target.value.trim().toLowerCase();
      let shown = 0;
      root.querySelectorAll(".kn-section").forEach((section) => {
        let any = false;
        section.querySelectorAll(".kn-line").forEach((li) => { const hit = !q || li.textContent.toLowerCase().includes(q);
          li.hidden = !hit; any = any || hit; shown += hit; });
        section.hidden = !any;
      });
      root.querySelector(".kn-none").hidden = shown > 0;
    });
    root.querySelectorAll("[data-open]").forEach((el) => el.addEventListener("click", () => this._go(el.dataset.open)));
    root.getElementById("history-more")?.addEventListener("click", () => { this._historyAll = true; this._render(); });
    const onDemand = root.getElementById("on-demand");
    onDemand?.addEventListener(onDemand.tagName === "HA-SWITCH" ? "change" : "click", (ev) => {
      ev.preventDefault(); this._saveSetting({on_demand: !this._settings.on_demand});
    });
    const auto = root.getElementById("auto-accept");
    auto?.addEventListener(auto.tagName === "HA-SWITCH" ? "change" : "click", (ev) => {
      ev.preventDefault();
      if (this._settings.auto_accept) { this._saveSetting({auto_accept: false}); return; }
      this._asking = "auto-accept"; this._render();  // Turning it on says what it will change first.
    });
    root.getElementById("ask-ok")?.addEventListener("click", () => {
      this._asking = null; this._saveSetting({auto_accept: true});
    });
    root.querySelectorAll(".ask-cancel").forEach((el) => el.addEventListener("click", () => { this._asking = null; this._render(); }));
    root.querySelector("[data-assist]")?.addEventListener("click", (ev) => {
      ev.stopPropagation(); this._pop = {kind: "assist", anchor: "[data-assist]"}; this._render();
    });
    root.querySelectorAll("[data-prefer]").forEach((el) => el.addEventListener("click", async () => {
      try { await this._hass.callWS({type: "assist_improver/assistant/prefer", pipeline_id: el.dataset.prefer}); }
      catch (err) { alert(err.message || String(err)); }
      this._pop = null; this._household = await this._hass.callWS({type: "assist_improver/household"}); this._render();
    }));
    root.querySelectorAll("[data-knows]").forEach((el) => el.addEventListener("click", async (ev) => {
      ev.stopPropagation(); el.disabled = true;
      try { await this._hass.callWS({type: "assist_improver/household/enable", target: el.dataset.knows, on: !!el.dataset.on}); }
      catch (err) { alert(err.message || String(err)); }
      this._household = await this._hass.callWS({type: "assist_improver/household"}); this._render();
    }));
    root.querySelectorAll("[data-only]").forEach((el) => el.addEventListener("click", () => {
      this._runMenu = false; this._only = el.dataset.only; this._force = false; this._confirming = true; this._render();
    }));
    root.querySelectorAll("[data-gate]").forEach((el) => el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      this._saveSetting({stage: el.dataset.gate, pause: !this._settings.paused.includes(el.dataset.gate)});
    }));
    root.querySelectorAll("[data-info]").forEach((el) => el.addEventListener("click", () => {
      this._info = this._info === el.dataset.info ? null : el.dataset.info; this._render();
    }));
    root.getElementById("backdrop")?.addEventListener("click", () => { this._pop = null; this._render(); });
    root.querySelectorAll("[data-pick]").forEach((el) => el.addEventListener("click", () => {
      const role = this._pop.role; this._pop = null;
      this._saveSetting({role, entity_id: el.dataset.pick || null});
    }));
    root.getElementById("use-recommended")?.addEventListener("click", async () => {
      const pop = this._pop;
      if (pop.busy) return;
      pop.busy = true; pop.error = null; this._render();
      try {
        const result = await this._hass.callWS({type: "assist_improver/settings/recommended"});
        this._settings = result;
        if (result.notes?.length) { pop.busy = false; pop.error = result.notes.join(" "); } else this._pop = null;
      } catch (err) { pop.busy = false; pop.error = err.message || String(err); }
      this._render();
    });
    root.getElementById("new-task")?.addEventListener("click", () => {
      this._pop = {...this._pop, kind: "create"};
      const first = this._settings.providers[0];
      if (first) this._loadModels(first.entry_id); else this._render();
    });
    root.getElementById("c-provider")?.addEventListener("change", (ev) => this._loadModels(ev.target.value));
    root.getElementById("c-model")?.addEventListener("change", (ev) => { this._pop.model = ev.target.value; this._suggestName(); this._render(); });
    root.getElementById("c-model")?.addEventListener("input", (ev) => { this._pop.model = ev.target.value; });
    root.getElementById("c-name")?.addEventListener("input", (ev) => { this._pop.name = ev.target.value; this._pop.nameEdited = true; });
    root.getElementById("c-back")?.addEventListener("click", () => { this._pop = {...this._pop, kind: "role", error: null}; this._render(); });
    root.getElementById("c-create")?.addEventListener("click", async () => {
      const pop = this._pop;
      if (!pop.model) { pop.error = "Choose a model."; this._render(); return; }
      pop.busy = true; pop.error = null; this._render();
      try {
        this._settings = await this._hass.callWS({type: "assist_improver/settings/create", entry_id: pop.entry_id,
          model: pop.model, role: pop.role, ...(pop.named && pop.name ? {name: pop.name} : {})});
        this._pop = null;
      } catch (err) { pop.busy = false; pop.error = err.message || String(err); }
      this._render();
    });
    const tests = (delta) => this._saveSetting({tests_per_goal: this._settings.tests_per_goal + delta});
    root.getElementById("t-minus")?.addEventListener("click", () => tests(-1));
    root.getElementById("t-plus")?.addEventListener("click", () => tests(1));
    root.getElementById("run-menu")?.addEventListener("click", (ev) => { ev.stopPropagation(); this._runMenu = !this._runMenu; this._render(); });
    root.querySelectorAll("[data-schedule]").forEach((el) => el.addEventListener("click", () => {
      this._runMenu = false; this._saveSetting({schedule: el.dataset.schedule});
    }));
    const practice = root.getElementById("practice");
    practice?.addEventListener(practice.tagName === "HA-SWITCH" ? "change" : "click", async (ev) => {
      ev.preventDefault();
      try { this._settings = await this._hass.callWS({type: "assist_improver/practice", on: !this._settings.practice.on}); }
      catch (err) { alert(err.message || String(err)); }
      this._render();
    });
    root.querySelectorAll(".turn-on-link").forEach((el) => el.addEventListener("click", async () => {
      el.disabled = true; el.textContent = "Turning on…";
      try { await this._hass.callWS({type: "assist_improver/household/enable", target: el.dataset.target}); }
      catch (err) { alert(err.message || String(err)); }
      this._household = await this._hass.callWS({type: "assist_improver/household"}); this._render();
    }));
    this._relayout();
    this._measure();
    this._placePop();
  }

  _section(key, title, inner, rich = false) {
    // rich: the title is already the summary's parts (name, kind, counts), laid out side by side.
    return `<details data-key="${key}" ${this._open.has(key) ? "open" : ""}><summary>${rich ? title : `<span class="name">${title}</span>`}</summary>${inner}</details>`;
  }

  _bar(added, removed) {
    const total = added + removed;
    if (!total) return "";
    const a = Math.round((added / total) * 5);
    return `<span class="bar">${Array.from({length: 5}, (_, n) => `<i class="${n < a ? "a" : "d"}"></i>`).join("")}</span>`;
  }

  _diff(rows, change) {
    if (!rows.length) return `<div class="empty">${change.proposed ? "As proposed; not applied." : "Already in place; nothing changes."}</div>`;
    const cls = {"+": "add", "-": "del", " ": "", gap: "gap"};
    return `<table class="diff">${rows.map((r) => r.sign === "gap"
      ? `<tr class="gap"><td colspan="4">&#8943;</td></tr>`
      : `<tr class="${cls[r.sign]}"><td class="num">${r.old ?? ""}</td><td class="num">${r.new ?? ""}</td>
         <td class="sign">${r.sign === " " ? "" : r.sign === "-" ? "&#8722;" : "+"}</td><td class="text">${esc(r.text)}</td></tr>`).join("")}</table>`;
  }

  _renderReview(r) {
    const outcome = [r.enabled.length && `Enables ${plural(r.enabled.length, "task")}`,
      r.removed.length && `${plural(r.removed.length, "fewer turn")}`].filter(Boolean).join(" · ");
    const lines = (cls, sign, items) => `<div class="lines">${items.map((t) =>
      `<div class="line ${cls}"><span class="sign">${sign}</span><span>${esc(t)}</span></div>`).join("")}</div>`;
    const files = r.changes.map((c, n) => `<div class="file">${this._section(`file${n}`,
      `<span class="name">${esc(c.name)}</span><span class="kind ${esc(c.kind)}">${esc(c.kind)}</span>
       ${c.added ? `<span class="count-add">+${c.added}</span>` : ""}${c.removed ? `<span class="count-del">&#8722;${c.removed}</span>` : ""}
       ${this._bar(c.added, c.removed)}`, this._diff(c.rows, c), true)}</div>`).join("");
    const tests = r.tests.map((t) => `<li class="${t.improved ? "ok" : "no"}"><span class="mark">${t.improved ? "&#10003;" : "&#8211;"}</span>
      <span>${esc(t.summary)}</span><span class="result">${esc(t.result || (t.improved ? "improved" : "not improved"))}</span></li>`).join("");
    const improvedCount = r.tests.filter((t) => t.improved).length;
    // The summary, always open at the top: what you get (the editor's words), why it's safe (stated by code), and
    // what was tested. The technical cause is last, for whoever wants it.
    const summary = `<div class="card summary">
      ${r.benefit ? `<div class="label">What you get</div><p class="benefit">${esc(r.benefit)}</p>` : ""}
      <div class="label">Why it's safe</div><ul class="safe">${r.safety.map((f) =>
        `<li><span class="mark">&#10003;</span><span>${esc(f)}</span></li>`).join("")}</ul>
      <div class="label">Tested on ${plural(r.tests.length, "conversation")} · ${improvedCount} improved</div><ul class="tests">${tests}</ul>
      ${r.untested ? `<p class="lead">Expected to help ${plural(r.untested, "more conversation")} too (not tested).</p>` : ""}</div>`;
    const how = `<div class="details-body"><p>${esc(r.cause)}</p>
      <div class="note"><span>&#9432;</span><span>Physical positions aren't simulated in the sandbox: a door's motion, for
      example, is taken as done when its command succeeds.</span></div></div>`;
    const sections = [
      r.enabled.length && this._section("enabled", `Tasks now enabled (${r.enabled.length})`, lines("add", "+", r.enabled)),
      r.removed.length && this._section("removed", `Turns no longer needed (${r.removed.length})`, lines("del", "−", r.removed)),
      this._section("changes", `Changeset (${r.changes.length})`, `<div class="files">${files}</div>`),
      this._section("details", "How it works", how),
    ].filter(Boolean).join("");
    const conflicts = r.conflicts.length ? `<div class="conflict"><b>Can't apply it now:</b><br>${r.conflicts.map((c) =>
      `${esc(c.what)}: ${esc(c.why)}`).join("<br>")}</div>` : "";
    return this._toolbar("Improvements", true) + `<div class="page">
      <h1>${esc(r.title)} <span class="chip ${esc(r.status)}">${esc(STATUS[r.status] || r.status)}</span></h1>
      <div class="outcome">${esc(outcome)}</div>
      ${r.revises ? `<div class="revises">Revised after your note on “${esc(r.revises.title)}”${r.revises.note ? `: “${esc(r.revises.note)}”` : ""}</div>` : ""}
      ${conflicts}
      ${summary}<div class="card">${sections}</div></div>${this._footer(r)}`;
  }

  _footer(r) {
    // Right after a decision, the footer says what happened (with Undo) and offers the next review: the page above
    // stays where it was.
    if (this._result || r.recent_outcome) {
      const ok = this._result ? this._resultOk : r.recent_outcome_ok;
      return `<div class="footer"><div class="footer-inner done-row">
        <div class="done-msg ${ok ? "ok" : "bad"}"><span class="mark">${ok ? "&#10003;" : "!"}</span><span>${esc(this._result || r.recent_outcome)}${
          r.choices.some((c) => c.value === "undo") ? ` <button class="link" id="undo-link">Undo</button>` : ""}</span></div>
        ${r.next_pending ? `<button class="btn" id="next" data-id="${esc(r.next_pending.id)}">Next: ${esc(r.next_pending.title)} &#8594;</button>`
          : `<button class="link" id="back-list">All caught up · back to the list</button>`}</div></div>`;
    }
    if (!r.choices.length) return "";
    const decision = this._decision || r.choices[0].value;
    const label = r.choices.find((c) => c.value === decision)?.label || decision;
    const needsNote = !["accept", "undo", "was_test"].includes(decision);
    const prompt = {revise: "What should change? (needed for the revision)", needs_improvement: "What could be better? (optional)",
                    decline: "Why not? (optional; the next run takes it into account)",
                    fine_as_is: "Anything to add? (optional)"}[decision] || "Add a note (optional)";
    const menu = this._menuOpen ? `<div class="menu">${r.choices.map((c) =>
      `<div data-choice="${esc(c.value)}" class="${c.value === decision ? "on" : ""}">${esc(c.label)}</div>`).join("")}</div>` : "";
    return `${this._menuOpen ? `<div class="backdrop" id="menu-backdrop"></div>` : ""}<div class="footer"><div class="footer-inner">
      ${needsNote ? `<textarea id="note" placeholder="${esc(prompt)}">${esc(this._note || "")}</textarea>` : ""}
      <div class="split"><button class="main" id="decide" ${this._busy ? "disabled" : ""}>${esc(this._busy ? "Working…" : label)}</button>
      <button class="arrow" id="arrow" aria-label="Other decisions" ${this._busy ? "disabled" : ""}>&#9662;</button>${menu}</div>
    </div></div>`;
  }

  // The developer view (developer mode only): any ID's lifecycle, every conversation, runs and where their time went,
  // the improver's footprint, and the journal, from the assist_improver/dev/... commands (devview.py). Journal text
  // and tags are untrusted: every value goes through esc().

  _devLink() {
    return `<button class="tb-btn tb-text" id="open-developer" title="Developer view" aria-label="Developer view">${svg("code")}Developer</button>`;
  }

  async _loadDev(params) {
    const tab = DEV_TABS.some(([key]) => key === params.get("tab")) ? params.get("tab") : "find";
    const q = (params.get("q") || "").trim();
    const dev = this._dev || (this._dev = {conv: {stage: "", search: "", sort: "started", desc: true}});
    const moved = this._view !== "developer" || dev.tab !== tab || dev.q !== q;
    clearTimeout(this._pollTimer);
    this._view = "developer"; this._review = null; this._pop = null; KEPT.page = null;
    if (moved) {  // Another tab or ID: show it at once, loading, rather than the old one until the data is in.
      Object.assign(dev, {tab, q, error: null, loading: true, toTop: true, find: null, trace: null, traceError: null,
                          flipped: new Set(), shown: new Set(), depth: WF_DEPTH, journalAll: false});
      if (tab === "logs") {
        const filters = Object.fromEntries(["level", "text", "run", "recording"].map((k) => [k, params.get(k) || ""]));
        dev.logs = {filters, entries: null, more: false, query: null, busy: false, error: null};
      }
      this._render();
    }
    const seq = this._devSeq = (this._devSeq || 0) + 1;
    const ws = (msg) => this._hass.callWS(msg);
    const got = {};
    try {
      if (tab === "find" && q) {
        got.find = await ws({type: "assist_improver/dev/lookup", query: q});
        if (got.find.trace_id) Object.assign(got, await this._devTrace(got.find.trace_id));
      } else if (tab === "conversations") {
        got.rows = (await ws({type: "assist_improver/dev/conversations"})).rows;
      } else if (tab === "runs") {
        got.runs = (await ws({type: "assist_improver/dev/runs"})).runs;
        const run = q && got.runs.find((r) => r.id === q);
        if (run?.trace_available) Object.assign(got, await this._devTrace(run.trace_id));
      } else if (tab === "health") {
        got.health = await ws({type: "assist_improver/dev/health"});
      } else if (tab === "logs") {
        const query = await this._logQuery(dev.logs.filters);
        got.logs = {query, ...(await ws(query))};
      }
    } catch (err) {
      got.error = this._errText(err);
    }
    // A newer load (another tab, or Refresh) took over, or the panel has moved on to another view.
    if (seq !== this._devSeq || new URLSearchParams(location.search).get("view") !== "developer") return;
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
    this._devPoll();
  }

  _devPoll() {
    // Nothing polls but Runs, every 5 s while a run is active; Refresh reloads any tab.
    clearTimeout(this._devTimer);
    const dev = this._dev, shown = (dev?.runs || []).filter((r) => !dev.q || r.id === dev.q);  // The list, or the run open.
    if (dev?.tab !== "runs" || !shown.some((r) => r.status === "running")) return;
    this._devTimer = setTimeout(() => {
      if (this.isConnected && this._view === "developer" && this._dev.tab === "runs") this._load();
    }, 5000);
  }

  async _devTrace(traceId) {
    try { return {trace: (await this._hass.callWS({type: "assist_improver/dev/trace", trace_id: traceId})).trace}; }
    catch (err) { return {traceError: this._errText(err)}; }
  }

  _errText(err) {
    return err?.message ? `${err.message}${err.code ? ` (${err.code})` : ""}` : String(err);
  }

  async _logQuery(filters) {
    // The journal command for these filters. The journal matches tags exactly, so a run or conversation given by a
    // prefix, a short ID or Home Assistant's conversation_id is resolved to its full ID first.
    const msg = {type: "assist_improver/dev/journal", limit: LOG_PAGE};
    if (filters.level) msg.level = filters.level;
    if (filters.text.trim()) msg.text = filters.text.trim();
    if (filters.run.trim()) msg.run_id = await this._resolveId(filters.run.trim(), ["run"]);
    if (filters.recording.trim()) msg.recording_id = await this._resolveId(filters.recording.trim(), ["recording", "conversation_id"]);
    return msg;
  }

  async _resolveId(value, kinds) {
    if (value.length >= 32) return value;
    try {
      const found = await this._hass.callWS({type: "assist_improver/dev/lookup", query: value});
      if (found.match && kinds.includes(found.match.kind)) return found.match.recording || found.match.id;
    } catch (err) { /* Not resolvable: filter by it as given. */ }
    return value;
  }

  async _applyLogs(filters) {
    const logs = this._dev.logs;
    Object.assign(logs, {filters, busy: true, error: null}); this._render();
    try {
      logs.query = await this._logQuery(filters);
      const page = await this._hass.callWS(logs.query);
      Object.assign(logs, {entries: page.entries, more: page.more});
    } catch (err) { logs.error = this._errText(err); }
    logs.busy = false; this._render();
  }

  async _moreLogs() {
    // The next page: entries older than the last one shown.
    const logs = this._dev.logs, last = logs.entries?.[logs.entries.length - 1];
    if (!last || logs.busy || !logs.query) return;
    logs.busy = true; this._render();
    try {
      const page = await this._hass.callWS({...logs.query, before: last.at});
      Object.assign(logs, {entries: [...logs.entries, ...page.entries], more: page.more});
    } catch (err) { logs.error = this._errText(err); }
    logs.busy = false; this._render();
  }

  async _copy(text, el) {
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch (err) {
      // No clipboard API (as on a page served over plain http): the old way.
      const area = document.createElement("textarea");
      area.value = text; area.style.cssText = "position:fixed;top:0;left:0;opacity:0";
      this.shadowRoot.appendChild(area); area.select();
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      area.remove();
    }
    if (!ok) return;
    el.classList.add("done"); el.innerHTML = svg("check");
    setTimeout(() => { el.classList.remove("done"); el.innerHTML = svg("copy"); }, 1200);
  }

  // Building blocks: IDs (a link to Find, and a copy button), values, tags, timelines and journal entries.

  _idLink(id, full = false, label = null) {
    if (id == null || id === "") return "";
    const text = String(id), shown = label ?? (full || text.length <= 12 ? text : text.slice(0, 8));
    return `<span class="dv-id"><a href="${esc(devUrl("find", {q: text}))}" data-nav="1" title="${esc(text)}">${esc(shown)}</a><button
      class="dv-copy" data-copy="${esc(text)}" title="Copy" aria-label="Copy ${esc(text)}">${svg("copy")}</button></span>`;
  }

  _ids(list) {
    return (list || []).map((id) => this._idLink(id)).join(" ");
  }

  _when(iso) {
    // A time to the millisecond, how long ago when that's today's news, and the exact ISO time on hover.
    const recent = Date.now() - Date.parse(iso) < 86400000;
    return `<span title="${esc(iso)}">${esc(stamp(iso, "ms"))}</span>${recent ? ` <span class="dv-dim">${esc(ago(iso))}</span>` : ""}`;
  }

  _val(key, value) {
    if (value == null || value === "") return `<span class="dv-dim">–</span>`;
    if (ID_KEYS.has(key) && typeof value === "string") return this._idLink(value, true);
    if (ID_KEYS.has(key) && Array.isArray(value) && value.every((v) => typeof v === "string")) return this._ids(value);
    if (isIso(value)) return this._when(value);
    if (typeof value === "object") return `<code class="dv-json">${esc(JSON.stringify(value, null, 2))}</code>`;
    if (typeof value === "number") return esc(/bytes$/.test(key) ? bytes(value) : num(value));
    return esc(String(value));
  }

  _kv(obj, empty = "Nothing recorded.") {
    const rows = Object.entries(obj || {}).filter(([, v]) => v != null && v !== "" && !(typeof v === "object" && !Object.keys(v).length));
    if (!rows.length) return `<div class="dv-empty">${esc(empty)}</div>`;
    return `<dl class="dv-kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${this._val(k, v)}</dd>`).join("")}</dl>`;
  }

  _tags(tags) {
    return Object.entries(tags || {}).filter(([, v]) => v != null && v !== "").map(([k, v]) => {
      if (ID_KEYS.has(k) && typeof v === "string") return `<span class="dv-tag">${esc(k)} ${this._idLink(v)}</span>`;
      if (k === "goal" && typeof v === "string" && /^[0-9a-f]{8}:/.test(v)) {  // A goal names its recording's short ID.
        return `<span class="dv-tag">goal ${this._idLink(v.slice(0, 8), false, v)}</span>`;
      }
      return `<span class="dv-tag">${esc(k)}=${esc(typeof v === "object" ? JSON.stringify(v) : v)}</span>`;
    }).join("");
  }

  _chip(text, color = "") {
    return `<span class="dv-chip ${color}">${esc(text)}</span>`;
  }

  _statusChip(status) {
    const color = {running: "c-primary", done: "c-green", completed: "c-green", ok: "c-green", failed: "c-red",
                   error: "c-red", cancelled: "c-amber", interrupted: "c-amber", held: "c-amber"};
    return this._chip(status || "?", Object.hasOwn(color, status) ? color[status] : "");
  }

  _stageChip(stage) {
    const color = {active: "c-primary", waiting: "c-amber", held: "c-amber", failing: "c-red", set_aside: "c-red",
                   analysed: "c-blue", improvement: "c-green"};
    return this._chip(stage, Object.hasOwn(color, stage) ? color[stage] : "");
  }

  _timeline(events) {
    if (!events?.length) return `<div class="dv-empty">Nothing recorded.</div>`;
    const color = {run: "c-primary", finished: "c-primary", analysis: "c-blue", proposal: "c-blue", role_call: "c-blue",
                   problem: "c-red", case: "c-amber", test: "c-amber", improvement: "c-green", decision: "c-green"};
    return events.map((e) => `<div class="dv-tl-row"><span class="dv-time" title="${esc(e.at)}">${esc(stamp(e.at))}</span>
      ${this._chip(e.kind, Object.hasOwn(color, e.kind) ? color[e.kind] : "")}
      <div class="dv-what">${esc(e.what)}${Object.keys(e.ids || {}).length ? `<div class="dv-tags">${this._tags(e.ids)}</div>` : ""}</div></div>`).join("");
  }

  _entry(e, key) {
    const level = cls(e.level || "info");
    const extra = [e.trace_id && `<span class="dv-tag">trace ${this._idLink(e.trace_id)}</span>`,
                   e.span && `<span class="dv-tag">span ${esc(e.span)}</span>`,
                   e.logger && `<span class="dv-tag">${esc(e.logger)}</span>`].filter(Boolean).join("");
    const tags = this._tags(e.tags) + extra;
    const exc = e.exception ? `<details data-key="${esc(key)}" ${this._open.has(key) ? "open" : ""}><summary>Traceback</summary>
      <pre>${esc(e.exception)}</pre></details>` : "";
    return `<div class="dv-log lv-${level}"><div class="dv-log-head"><span class="dv-time" title="${esc(e.at)}">${esc(stamp(e.at, "ms"))}</span>
      <span class="dv-level lv-${level}">${esc(e.level)}</span></div><div class="dv-msg">${esc(e.message)}</div>
      ${tags ? `<div class="dv-tags">${tags}</div>` : ""}${exc}</div>`;
  }

  _journal(entries, prefix) {
    if (!entries?.length) return `<div class="dv-empty">No journal entries.</div>`;
    return entries.map((e, i) => this._entry(e, `${prefix}:${e.at}:${i}`)).join("");
  }

  _stageBar(timings) {
    // A run's seconds per stage (from the run log, kept a year): a bar and its legend.
    const parts = [...RUN_STAGES.filter((s) => timings?.[s] != null).map((s) => [s, Number(timings[s]) || 0]),
                   ...Object.entries(timings || {}).filter(([s]) => !RUN_STAGES.includes(s)).map(([s, v]) => [s, Number(v) || 0])];
    if (!parts.length) return "";
    const total = parts.reduce((sum, [, v]) => sum + v, 0);
    return `<div class="dv-stages">${parts.map(([s, v]) => `<i class="s-${cls(s)}" style="width:${total ? (100 * v / total).toFixed(2) : 0}%"></i>`).join("")}</div>
      <div class="dv-legend">${parts.map(([s, v]) => `<span><i class="s-${cls(s)}"></i>${esc(s)} ${dur(v * 1000)}</span>`).join("")}</div>`;
  }

  // The waterfall: a trace's spans as a tree, each a row indented by depth, its bar placed by start and length.

  _waterfall(trace, live) {
    const dev = this._dev, now = Date.now(), updated = Date.parse(trace.updated) || now;
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
      if (rows.length < WF_ROWS) rows.push(this._wfRow(node, depth, open, parentTags, t0, total));
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

  _wfRow(node, depth, open, parentTags, t0, total) {
    const left = Math.min(Math.max(((node.start - t0) / total) * 100, 0), 100);
    const width = Math.min(Math.max(((node.ms || 0) / total) * 100, 0), 100 - left);
    // Beside the name, the tags this span adds to its parent's (context tags repeat down the tree).
    const rank = (k) => (WF_HINTS.indexOf(k) + WF_HINTS.length + 1) % (WF_HINTS.length + 1);  // Unlisted tags last.
    const hint = Object.entries(node.tags || {}).filter(([k, v]) => k !== "run_id" && parentTags[k] !== v)
      .sort(([a], [b]) => rank(a) - rank(b)).slice(0, 3)
      .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : ID_KEYS.has(k) && String(v).length > 12 ? String(v).slice(0, 8) : v}`).join(" ");
    const toggle = node.kids.length ? `<button class="wf-tog" data-wf="${esc(node.id)}" aria-expanded="${open}"
      aria-label="${open ? "Collapse" : "Expand"} ${esc(node.name)}">${open ? "&#9662;" : "&#9656;"}</button>` : `<span class="wf-sp"></span>`;
    const count = node.kids.length && !open ? ` <span class="dv-dim">+${num(this._wfCount(node))}</span>` : "";
    const detail = this._dev.shown.has(node.id) ? `<div class="wf-detail">${this._kv({name: node.name, status: node.status,
      error: node.error, started: node.at, took: dur(node.ms), span: node.id, "parent span": node.parent})}
      ${Object.keys(node.tags || {}).length ? `<div class="dv-tags">${this._tags(node.tags)}</div>` : ""}</div>` : "";
    return `<div class="wf-row st-${cls(node.status)} ${node.open ? "open" : ""}"><div class="wf-name" style="--d:${Math.min(depth, 10)}">${toggle}<a
      class="wf-label" data-wfi="${esc(node.id)}" title="${esc(node.name)}${hint ? ` ${esc(hint)}` : ""}">${esc(node.name)}${
      hint ? ` <span class="dv-dim">${esc(hint)}</span>` : ""}${count}</a></div><div class="wf-ms">${dur(node.ms)}</div>
      <div class="wf-track"><i style="left:${left.toFixed(3)}%;width:${width.toFixed(3)}%"></i></div></div>${detail}`;
  }

  _wfCount(node) {
    return node.kids.reduce((n, kid) => n + 1 + this._wfCount(kid), 0);
  }

  // The tabs.

  _renderDev() {
    const dev = this._dev;
    const refresh = `<button class="tb-btn" id="dev-refresh" title="Refresh" aria-label="Refresh">${svg("refresh")}</button>`;
    const tabs = `<nav class="dv-tabs">${DEV_TABS.map(([key, label]) =>
      `<a class="dv-tab ${key === dev.tab ? "on" : ""}" href="${esc(devUrl(key))}" data-nav="1">${label}</a>`).join("")}</nav>`;
    const state = dev.error ? `<div class="conflict">${esc(dev.error)}</div>` : dev.loading ? `<div class="dv-empty">Loading…</div>` : "";
    const body = {find: () => this._devFind(state), conversations: () => this._devConversations(state),
                  runs: () => this._devRuns(state), health: () => this._devHealth(state), logs: () => this._devLogs(state)}[dev.tab]();
    return this._toolbar("Developer", true, refresh) + `<div class="page wide dv">${tabs}${body}</div>`;
  }

  _devFind(state) {
    const dev = this._dev, f = dev.find;
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
        <div class="dv-matches">${f.matches.map((m) => `<div class="dv-match">${this._chip(m.kind)}${this._idLink(m.id, true)}
          <span class="dv-dim">${esc(m.title)}</span></div>`).join("")}</div></div>`;
    }
    const m = f.match, record = {...(f.record || {})}, progress = {...(record.progress || {})};
    delete record.id; delete record.progress;
    // A run's progress: its stage timings as a bar, its counts, and the stages that failed.
    const timings = progress.timings, problems = Array.isArray(progress.problems) ? progress.problems : [];
    delete progress.timings; delete progress.problems;
    const more = this._stageBar(timings) + (Object.keys(progress).length
      ? `<div class="dv-card-title" style="margin:12px 0 6px">Progress</div>${this._kv(progress)}` : "")
      + problems.map((p) => `<div class="dv-err">${esc(p.stage)}: ${esc(p.error)}</div>`).join("");
    const head = `<div class="card dv-card"><div class="dv-head">${this._chip(m.kind, "c-primary")}${this._idLink(m.id, true)}</div>
      ${m.title ? `<div class="dv-head"><span class="dv-title">${esc(m.title)}</span></div>` : ""}
      ${m.recording ? `<div class="dv-head"><span class="dv-dim">recording</span>${this._idLink(m.recording, true)}</div>` : ""}
      ${this._kv(record)}${more}</div>`;
    const timeline = `<div class="card dv-card"><div class="dv-card-title">Timeline <span class="dv-dim">${f.timeline.length}, oldest first</span></div>
      ${this._timeline(f.timeline)}</div>`;
    const logsFor = m.kind === "run" ? {run: m.id} : m.recording || m.kind === "recording" ? {recording: m.recording || m.id} : null;
    const journal = `<div class="card dv-card"><div class="dv-card-title">Journal <span class="dv-dim">${f.journal.length}, newest first</span>${
      logsFor ? `<a class="dv-right" href="${esc(devUrl("logs", logsFor))}" data-nav="1">Open in Logs</a>` : ""}</div>${this._journal(f.journal, "dv:find")}</div>`;
    let spans;
    if (f.trace_id) {
      spans = `<div class="card dv-card flush"><div class="dv-card-title">Spans <span class="dv-dim">trace</span>${this._idLink(f.trace_id)}</div>${
        dev.trace ? this._waterfall(dev.trace, f.record?.status === "running" || undefined) : `<div class="dv-note">${esc(dev.traceError || "No trace.")}</div>`}</div>`;
    } else {
      const why = m.kind === "recording" || m.recording
        ? "A conversation's spans are in the traces of the runs that analysed it (linked in the timeline)."
        : "No trace for this ID.";
      spans = `<div class="card dv-card"><div class="dv-card-title">Spans</div><div class="dv-note">${esc(why)}</div></div>`;
    }
    return box + head + timeline + journal + spans;
  }

  _devConversations(state) {
    if (state) return state;
    const dev = this._dev, conv = dev.conv, rows = [...(dev.rows || [])];
    const keys = {started: (r) => r.started || "", bytes: (r) => r.bytes ?? -1, user_turns: (r) => r.user_turns ?? -1};
    const key = keys[conv.sort] || keys.started;
    rows.sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0) * (conv.desc ? -1 : 1));
    const counts = new Map();
    for (const r of rows) counts.set(r.stage, (counts.get(r.stage) || 0) + 1);
    const stages = [...DEV_STAGES.filter((s) => counts.has(s)), ...[...counts.keys()].filter((s) => !DEV_STAGES.includes(s))];
    if (conv.stage && !counts.has(conv.stage)) conv.stage = "";
    const chips = [["", "all", rows.length], ...stages.map((s) => [s, s, counts.get(s)])].map(([value, label, n]) =>
      `<button class="dv-fchip ${conv.stage === value ? "on" : ""}" data-stage="${esc(value)}">${esc(label)}<b>${n}</b></button>`).join("");
    const sort = (k, label) => `<button data-sort="${k}" class="${conv.sort === k ? "on" : ""}">${label}${
      conv.sort === k ? (conv.desc ? " &#9662;" : " &#9652;") : ""}</button>`;
    const head = `<thead><tr><th class="sticky">Recording</th><th>Stage</th><th class="n">${sort("bytes", "Size")}</th>
      <th class="n">${sort("user_turns", "Turns")}</th><th>${sort("started", "Started")}</th><th class="n">Goals</th>
      <th class="n">Friction</th><th class="n">Strikes</th><th>Status</th><th>Stored</th><th>Improvements</th><th>Cases</th><th>Runs</th></tr></thead>`;
    const body = rows.map((r) => {
      const text = [r.id, r.stage, r.status, r.stored, r.waiting_at, r.practice && "practice", ...(r.improvements || []),
                    ...(r.cases || []), ...(r.runs || [])].filter(Boolean).join(" ").toLowerCase();
      return `<tr data-stage="${esc(r.stage)}" data-text="${esc(text)}" data-bytes="${Number(r.bytes) || 0}">
        <td class="sticky">${this._idLink(r.id)}</td>
        <td>${this._stageChip(r.stage)}${r.waiting_at ? ` <span class="dv-dim">at ${esc(r.waiting_at)}</span>` : ""}${
          r.practice ? ` <span class="dv-tag">practice</span>` : ""}${r.filed ? ` <span class="dv-tag">filed</span>` : ""}</td>
        <td class="n">${bytes(r.bytes)}</td><td class="n">${num(r.user_turns)}</td>
        <td><span title="${esc(r.started)}">${esc(stamp(r.started, "min"))}</span></td>
        <td class="n">${num(r.goals)}</td><td class="n">${num(r.friction)}</td><td class="n">${r.strikes ? num(r.strikes) : ""}</td>
        <td>${esc(r.status)}</td><td>${esc(r.stored)}</td><td>${this._ids(r.improvements)}</td><td>${this._ids(r.cases)}</td>
        <td>${this._ids(r.runs)}</td></tr>`;
    }).join("");
    return `<div class="dv-bar"><input id="cv-search" type="search" placeholder="Filter by ID, stage, status, run…" value="${esc(conv.search)}"
        aria-label="Filter conversations" autocomplete="off" spellcheck="false"><span class="dv-dim" id="cv-count"></span></div>
      <div class="dv-chips">${chips}</div>
      <div class="card">${rows.length ? `<div class="dv-scroll"><table class="dv-table">${head}<tbody>${body}</tbody></table></div>`
        : `<div class="dv-empty" style="padding:12px 14px">No recordings.</div>`}</div>`;
  }

  _devRuns(state) {
    if (state) return state;
    const dev = this._dev, runs = dev.runs || [];
    if (dev.q) return this._devRun(runs.find((r) => r.id === dev.q));
    if (!runs.length) return `<div class="card dv-card"><div class="dv-empty">No pipeline runs in the run log.</div></div>`;
    const live = runs.some((r) => r.status === "running");
    return `<div class="card dv-card flush"><div class="dv-card-title">Pipeline runs <span class="dv-dim">${runs.length}, newest first</span>${
      live ? `<span class="dv-live dv-right">Refreshing every 5 s</span>` : ""}</div>${runs.map((r) => this._runRow(r)).join("")}</div>`;
  }

  _runRow(r) {
    const live = r.status === "running", took = live ? Date.now() - Date.parse(r.started) : between(r.started, r.finished);
    const recent = Date.now() - Date.parse(r.started) < 86400000;
    const sub = [esc(r.trigger || "?"), r.only && `only ${esc(r.only)}`, live && r.stage && `at ${esc(r.stage)}`,
                 r.problems?.length && `<span style="color:var(--del-fg)">${plural(r.problems.length, "problem")}</span>`,
                 !r.trace_available && "no trace"].filter(Boolean);
    return `<div class="dv-run" data-run="${esc(r.id)}" role="link" tabindex="0">
      <div class="dv-run-top">${this._statusChip(r.status)}<span class="dv-time" title="${esc(r.started)}">${esc(stamp(r.started))}</span>${
        recent ? `<span class="dv-dim" style="font-size:12.5px">${esc(ago(r.started))}</span>` : ""}<span class="dv-right">${dur(took)}</span></div>
      <div class="dv-run-sub">${this._idLink(r.id)}${sub.map((s) => `<span>${s}</span>`).join("")}</div>
      ${this._stageBar(r.timings)}${r.error ? `<div class="dv-err">${esc(r.error)}</div>` : ""}</div>`;
  }

  _devRun(run) {
    const dev = this._dev, back = `<a class="dv-back" href="${esc(devUrl("runs"))}" data-nav="1">&#8592; All runs</a>`;
    if (!run) {
      return back + `<div class="card dv-card"><div class="dv-empty">No pipeline run ${esc(dev.q)} in the run log.
        <a href="${esc(devUrl("find", {q: dev.q}))}" data-nav="1">Find it</a></div></div>`;
    }
    const live = run.status === "running", took = live ? Date.now() - Date.parse(run.started) : between(run.started, run.finished);
    const head = `<div class="card dv-card"><div class="dv-head">${this._statusChip(run.status)}<span class="dv-title">Run</span>${
      this._idLink(run.id, true)}${live ? `<span class="dv-live">Refreshing every 5 s</span>` : ""}</div>
      ${this._kv({trigger: run.trigger, only: run.only, stage: live ? run.stage : null, started: run.started, finished: run.finished,
                  took: dur(took), trace: run.trace_available ? "kept" : "not kept", error: run.error})}${this._stageBar(run.timings)}</div>`;
    const counts = Object.keys(run.counts || {}).length
      ? `<div class="card dv-card"><div class="dv-card-title">Progress</div>${this._kv(run.counts)}</div>` : "";
    const trace = dev.trace;
    const wf = trace ? this._waterfall(trace, live) : `<div class="dv-note">${esc(dev.traceError || (run.trace_available ? "No trace."
      : "No trace for this run: traces are kept for a week, and runs from before tracing have none. The stage timings above come from the run log."))}</div>`;
    const spans = `<div class="card dv-card flush"><div class="dv-card-title">Waterfall</div>${wf}</div>`;
    const problems = run.problems?.length ? `<div class="card dv-card"><div class="dv-card-title">Problems <span class="dv-dim">${run.problems.length}</span></div>
      ${run.problems.map((p) => `<div class="dv-log lv-error"><div class="dv-log-head"><span class="dv-level lv-error">${esc(p.stage)}</span></div>
        <div class="dv-msg">${esc(p.error)}</div></div>`).join("")}</div>` : "";
    const entries = trace?.journal || [], shown = dev.journalAll ? entries : entries.slice(0, 200);
    const dropped = trace?.dropped?.journal ? `, ${num(trace.dropped.journal)} dropped over the cap` : "";
    const journal = trace ? `<div class="card dv-card"><div class="dv-card-title">Journal <span class="dv-dim">${num(entries.length)}, newest first${dropped}</span>
      <a class="dv-right" href="${esc(devUrl("logs", {run: run.id}))}" data-nav="1">Open in Logs</a></div>${this._journal(shown, "dv:run")}
      ${shown.length < entries.length ? `<div class="dv-more"><button class="btn ghost" id="journal-all">Show all ${num(entries.length)}</button></div>` : ""}</div>` : "";
    return back + `<div class="dv-grid">${head}${counts}</div>` + spans + problems + journal;
  }

  _devHealth(state) {
    if (state) return state;
    const h = this._dev.health || {}, disk = h.disk || {}, stores = disk.stores || {}, rec = disk.recordings || {};
    const traces = disk.traces || {}, mem = h.memory || {}, now = h.now || {}, run = now.run || {};
    const storeBytes = Object.values(stores).reduce((sum, n) => sum + (n || 0), 0);
    const diskBytes = storeBytes + (rec.bytes || 0) + (traces.bytes || 0);
    const parts = Object.entries(mem).filter(([, v]) => v && typeof v === "object" && !Array.isArray(v) && "bytes" in v)
      .sort((a, b) => (b[1].bytes || 0) - (a[1].bytes || 0));
    const memBytes = parts.reduce((sum, [, v]) => sum + (v.bytes || 0), 0), memMs = parts.reduce((sum, [, v]) => sum + (v.ms || 0), 0);
    const tile = (label, value, sub = "") => `<div class="dv-tile"><div class="t-label">${esc(label)}</div><div class="t-value">${value}</div>${
      sub ? `<div class="t-sub">${sub}</div>` : ""}</div>`;
    const tiles = [
      tile("On disk", bytes(diskBytes), `stores ${bytes(storeBytes)} · recordings ${bytes(rec.bytes)} · traces ${bytes(traces.bytes)}`),
      tile("Recordings", num(rec.count), `${bytes(rec.bytes)}, largest ${bytes(rec.largest)}`),
      tile("Traces", num(traces.count), bytes(traces.bytes)),
      tile("Memory, estimated", bytes(memBytes), `JSON size of what we hold; took ${dur(memMs)}`),
      tile("HA process peak RSS", bytes(h.process?.max_rss_bytes), "the whole Home Assistant process"),
      tile("Pipeline", run.running ? "running" : "idle", run.running ? `${esc(run.stage || "")} ${this._idLink(run.id)}` : ""),
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
    const practice = mem["practice sessions"], active = Array.isArray(mem["active traces"]) ? mem["active traces"] : [];
    const memRows = parts.map(([name, v]) => `<tr><td>${esc(name)}</td><td class="n">${num(v.items)}</td><td class="n">${bytes(v.bytes)}</td>
      <td class="n">${dur(v.ms)}</td></tr>`);
    const traceRows = active.map((t) => `<tr><td>${this._idLink(t.id)}</td><td>${esc(t.kind)}</td><td>${esc(stamp(t.started))}</td>
      <td class="n">${num(t.spans)}</td><td class="n">${num(t.journal)}</td><td class="n">${num(t.open)}</td>
      <td class="n">${num((t.dropped?.spans || 0) + (t.dropped?.journal || 0))}</td></tr>`);
    const memCard = card("Memory", `estimated on request as JSON size, ${dur(memMs)}`, table(["Structure", "Items ", "Size ", "Took "], memRows) +
      (practice ? sub("Practice sessions") + pad(this._kv(practice)) : "") + sub("Active traces", "in memory") +
      table(["Trace", "Kind", "Started", "Spans ", "Journal ", "Open ", "Dropped "], traceRows));
    const tasks = now.tasks || [];
    // sandbox.STATS: counts, and seconds (the *_seconds totals and the slowest_* ones).
    const sandboxes = Object.fromEntries(Object.entries(now.sandboxes || {}).map(([k, v]) =>
      [k, typeof v === "number" && (k.endsWith("seconds") || k.startsWith("slowest")) ? dur(v * 1000) : v]));
    const nowCard = card("Now", "", pad(this._kv({running: run.running ? "yes" : "no", stage: run.stage, run_id: run.id,
      pending_writes: now.pending_writes})) + sub("Sandboxes") + pad(this._kv(sandboxes)) + sub("Asyncio tasks", String(tasks.length)) +
      pad(tasks.length ? `<ul class="dv-list">${tasks.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : `<div class="dv-empty">None.</div>`));
    const hotRows = (h.hot || []).map((r) => `<tr><td class="dv-mono">${esc(r.name)}</td><td class="n">${num(r.count)}</td>
      <td class="n">${dur(r.total_ms)}</td><td class="n">${dur(r.count ? r.total_ms / r.count : null)}</td><td class="n">${dur(r.max_ms)}</td>
      <td>${esc(stamp(r.last_at))}</td></tr>`);
    const hotCard = card("Hot spots", "every span and timed section since Home Assistant started, by total time",
      table(["Name", "Count ", "Total ", "Mean ", "Max ", "Last"], hotRows, "Nothing timed yet."), true);
    const tokens = (t) => Object.entries(t || {}).map(([k, v]) => `<div>${esc(k.replace(/_tokens$/, ""))} ${num(v)}</div>`).join("");
    const aiRows = Object.entries(h.ai_calls || {}).sort((a, b) => (b[1].seconds || 0) - (a[1].seconds || 0)).map(([role, r]) =>
      `<tr><td>${esc(role)}</td><td class="n">${num(r.calls)}</td><td class="n ${r.failed ? "bad" : ""}">${num(r.failed)}</td>
      <td class="n">${dur((r.seconds || 0) * 1000)}</td><td class="n">${dur((r.slowest || 0) * 1000)}</td><td>${tokens(r.tokens) || "–"}</td></tr>`);
    const aiCard = card("AI calls", "per role, over the retained run log; tokens as the provider reported them",
      table(["Role", "Calls ", "Failed ", "Total ", "Slowest ", "Tokens"], aiRows, "No AI calls in the run log."), true);
    const flat = (obj, prefix = "") => Object.entries(obj || {}).reduce((out, [k, v]) => v && typeof v === "object" && !Array.isArray(v)
      ? {...out, ...flat(v, `${prefix}${k}.`)} : {...out, [`${prefix}${k}`]: v}, {});
    const retentionCard = card("Last tidy-up", "", pad(this._kv(flat(h.retention), "No tidy-up since Home Assistant started.")));
    const processCard = card("Process", "", pad(this._kv({version: h.version, peak_rss: bytes(h.process?.max_rss_bytes)}) +
      (h.process?.note ? `<p class="dv-note" style="margin:8px 0 0">${esc(h.process.note)}</p>` : "")));
    return `<div class="dv-tiles">${tiles}</div><div class="dv-grid">${diskCard}${memCard}${hotCard}${aiCard}${nowCard}
      <div>${retentionCard}${processCard}</div></div>`;
  }

  _devLogs(state) {
    const logs = this._dev.logs, f = logs.filters, filtered = Object.values(f).some(Boolean);
    const form = `<form class="dv-filters" id="dv-log-form">
      <label>Min level<select id="lg-level"><option value="">any</option>${LEVELS.map((l) =>
        `<option value="${l}" ${f.level === l ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <label>Text<input id="lg-text" type="search" value="${esc(f.text)}" placeholder="in message or traceback"></label>
      <label>Run<input id="lg-run" value="${esc(f.run)}" placeholder="run ID" autocomplete="off" spellcheck="false"></label>
      <label>Conversation<input id="lg-recording" value="${esc(f.recording)}" placeholder="recording ID or conversation_id"
        autocomplete="off" spellcheck="false"></label>
      <div class="actions">${filtered ? `<button class="link" type="button" id="lg-clear">Clear</button>` : ""}
        <button class="btn" type="submit" ${logs.busy ? "disabled" : ""}>Apply</button></div></form>`;
    if (state) return form + state;
    const entries = logs.entries || [];
    const list = entries.length ? this._journal(entries, "dv:log")
      : `<div class="dv-empty">No journal entries${filtered ? " match these filters" : ""}.</div>`;
    return form + `<div class="card dv-card"><div class="dv-card-title">Journal <span class="dv-dim">${num(entries.length)}${logs.more ? "+" : ""},
      newest first, across the retained traces</span></div>${list}${logs.error ? `<div class="error-text">${esc(logs.error)}</div>` : ""}
      ${logs.more ? `<div class="dv-more"><button class="btn ghost" id="lg-more" ${logs.busy ? "disabled" : ""}>${
        logs.busy ? "Loading…" : "Load more"}</button></div>` : ""}</div>`;
  }

  _wireDev(root) {
    const dev = this._dev;
    root.querySelectorAll("a[data-nav]").forEach((el) => el.addEventListener("click", (ev) => {
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button) return;  // A new tab or window.
      ev.preventDefault(); ev.stopPropagation(); this._nav(el.getAttribute("href"));
    }));
    root.querySelectorAll("[data-copy]").forEach((el) => el.addEventListener("click", (ev) => {
      ev.preventDefault(); ev.stopPropagation(); this._copy(el.dataset.copy, el);
    }));
    root.getElementById("dev-refresh")?.addEventListener("click", () => this._load());
    root.getElementById("dv-find-form")?.addEventListener("submit", (ev) => {
      ev.preventDefault(); this._nav(devUrl("find", {q: root.getElementById("dv-find").value.trim()}));
    });
    root.querySelectorAll("[data-run]").forEach((el) => {
      const open = () => this._nav(devUrl("runs", {q: el.dataset.run}));
      el.addEventListener("click", open);
      el.addEventListener("keydown", (ev) => { if (ev.key === "Enter") open(); });
    });
    root.querySelectorAll("[data-wf]").forEach((el) => el.addEventListener("click", () => {
      const id = el.dataset.wf;
      dev.flipped.has(id) ? dev.flipped.delete(id) : dev.flipped.add(id); this._render();
    }));
    root.querySelectorAll("[data-wfi]").forEach((el) => el.addEventListener("click", () => {
      const id = el.dataset.wfi;
      dev.shown.has(id) ? dev.shown.delete(id) : dev.shown.add(id); this._render();
    }));
    root.getElementById("wf-depth")?.addEventListener("click", () => {
      dev.depth = dev.depth > WF_DEPTH ? WF_DEPTH : Infinity; dev.flipped = new Set(); this._render();
    });
    root.getElementById("journal-all")?.addEventListener("click", () => { dev.journalAll = true; this._render(); });
    const search = root.getElementById("cv-search");
    if (search) {  // Conversations filter in place, so typing keeps its focus.
      const filter = () => {
        const needle = dev.conv.search.trim().toLowerCase(), stage = dev.conv.stage, rows = root.querySelectorAll("tr[data-stage]");
        let shown = 0, size = 0;
        rows.forEach((tr) => {
          const hit = (!stage || tr.dataset.stage === stage) && (!needle || tr.dataset.text.includes(needle));
          tr.hidden = !hit;
          if (hit) { shown += 1; size += Number(tr.dataset.bytes) || 0; }
        });
        root.getElementById("cv-count").textContent = `${shown === rows.length ? rows.length : `${shown} of ${rows.length}`} · ${bytes(size)}`;
      };
      search.addEventListener("input", () => { dev.conv.search = search.value; filter(); });
      root.querySelectorAll("button[data-stage]").forEach((el) => el.addEventListener("click", () => {
        dev.conv.stage = el.dataset.stage;
        root.querySelectorAll("button[data-stage]").forEach((chip) => chip.classList.toggle("on", chip === el));
        filter();
      }));
      root.querySelectorAll("[data-sort]").forEach((el) => el.addEventListener("click", () => {
        const key = el.dataset.sort;
        dev.conv.desc = dev.conv.sort === key ? !dev.conv.desc : true; dev.conv.sort = key; this._render();
      }));
      filter();
    }
    root.getElementById("dv-log-form")?.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const value = (id) => root.getElementById(id).value;
      this._applyLogs({level: value("lg-level"), text: value("lg-text"), run: value("lg-run"), recording: value("lg-recording")});
    });
    root.getElementById("lg-clear")?.addEventListener("click", () => this._applyLogs({level: "", text: "", run: "", recording: ""}));
    root.getElementById("lg-more")?.addEventListener("click", () => this._moreLogs());
  }

  _wire() {
    const root = this.shadowRoot;
    const menuSlot = root.getElementById("menu");
    if (menuSlot && customElements.get("ha-menu-button")) {
      const button = document.createElement("ha-menu-button");
      button.hass = this._hass; button.narrow = this._narrow;
      menuSlot.replaceWith(button);
    }
    root.getElementById("back")?.addEventListener("click", () => this._go(null));
    root.getElementById("open-settings")?.addEventListener("click", () => this._nav("/assist-improver?view=settings"));
    root.getElementById("open-developer")?.addEventListener("click", () => this._nav(devUrl("find")));
    if (this._view === "developer") this._wireDev(root);
    root.querySelectorAll(".go-settings").forEach((el) => el.addEventListener("click", () => this._nav("/assist-improver?view=settings")));
    if (this._view === "settings") this._wireSettings(root);
    root.querySelectorAll(".item").forEach((el) => el.addEventListener("click", () => this._go(el.dataset.id)));
    root.getElementById("run-now")?.addEventListener("click", () => { this._confirming = true; this._force = false; this._only = null; this._render(); });
    root.getElementById("run-anyway")?.addEventListener("click", () => { this._confirming = true; this._force = true; this._only = null; this._render(); });
    for (const id of ["cancel-confirm", "cancel-confirm-2", "confirm-shade"]) {
      root.getElementById(id)?.addEventListener("click", () => { this._confirming = false; this._render(); });
    }
    root.getElementById("start-run")?.addEventListener("click", async (ev) => {
      ev.target.disabled = true; ev.target.textContent = "Starting…";
      try { await this._hass.callWS({type: "assist_improver/run/start", confirmed: true, force: !!this._force,
                                      ...(this._only ? {only: this._only} : {})}); }
      catch (err) { alert(err.message || String(err)); }
      this._confirming = false; await this._load();
    });
    root.getElementById("cancel-run")?.addEventListener("click", async (ev) => {
      ev.target.disabled = true; ev.target.textContent = "Cancelling…";
      await this._hass.callWS({type: "assist_improver/run/cancel"}); setTimeout(() => this._load(), 800);
    });
    root.querySelectorAll(".turn-on").forEach((el) => el.addEventListener("click", async () => {
      el.disabled = true; el.textContent = "Turning on…";
      try { await this._hass.callWS({type: "assist_improver/household/enable", target: el.dataset.target}); }
      catch (err) { alert(err.message || String(err)); }
      await this._load();
    }));
    root.querySelectorAll("details").forEach((el) => el.addEventListener("toggle", () => {
      el.open ? this._open.add(el.dataset.key) : this._open.delete(el.dataset.key);
    }));
    root.getElementById("note")?.addEventListener("input", (ev) => { this._note = ev.target.value; });
    root.getElementById("arrow")?.addEventListener("click", () => { this._menuOpen = !this._menuOpen; this._render(); });
    root.getElementById("menu-backdrop")?.addEventListener("click", () => { this._menuOpen = false; this._runMenu = false; this._render(); });
    root.querySelectorAll("[data-choice]").forEach((el) => el.addEventListener("click", () => {
      this._decision = el.dataset.choice; this._menuOpen = false; this._render();
      root.getElementById("note")?.focus();
    }));
    root.getElementById("decide")?.addEventListener("click", () => this._decide());
    root.getElementById("next")?.addEventListener("click", (ev) => this._go(ev.currentTarget.dataset.id));
    root.getElementById("back-list")?.addEventListener("click", () => this._go(null));
    root.getElementById("undo-link")?.addEventListener("click", () => { this._decision = "undo"; this._decide(); });
  }

  _scroller() {
    // Whatever scrolls the page: the nearest scrolling ancestor, across shadow roots, else the document.
    for (let el = this; el; ) {
      el = el.parentElement || el.getRootNode?.()?.host;
      if (!el) break;
      if (/(auto|scroll)/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight) return el;
    }
    return document.scrollingElement || document.documentElement;
  }

  _keepScroll() {
    // Back to where the page was (for a few seconds after a decision, while the panel may be made again).
    const kept = KEPT.scroll;
    if (!kept || kept.page !== KEPT.page || Date.now() > kept.until) return;
    requestAnimationFrame(() => { this._scroller().scrollTop = kept.top; });
  }

  async _decide() {
    const r = this._review;
    const decision = this._decision || r.choices[0].value;
    KEPT.scroll = {page: r.id, top: this._scroller().scrollTop, until: Date.now() + 8000};
    this._busy = true; this._render();
    try {
      const res = await this._hass.callWS({type: "assist_improver/decide", improvement_id: r.id, decision,
        note: ["accept", "undo", "was_test"].includes(decision) ? "" : (this._note || "")});
      this._result = res.result; this._resultOk = res.ok; this._decision = null; this._note = "";
    } catch (err) {
      this._result = err.message || String(err); this._resultOk = false;
    }
    this._busy = false;
    await this._load();
  }
}

// An update loads the script again under its new version's address; the page keeps the first definition until reload.
if (!customElements.get("assist-improver-panel")) customElements.define("assist-improver-panel", AssistImproverPanel);
