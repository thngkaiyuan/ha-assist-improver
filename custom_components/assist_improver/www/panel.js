// Assist Improver's review panel: improvements shown the way a code review shows a change, and the settings.
// Talks to Home Assistant over websocket commands (assist_improver/improvements, /improvement, /decide, /settings...);
// each review has its own address (/assist-improver?id=...), which the notification links to; the settings are at
// /assist-improver?view=settings.

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const ago = (iso) => {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
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
  sandbox: {label: "Sandbox mode", tip: "When turned on, your Assist conversations use a simulated copy of your home, so nothing real changes; the improver learns from them as practice. This applies only to your user.", others: "Also on for {names}."},
  autoAccept: {label: "Auto-accept", tip: "Automatically accepts Assist Improver's proposed changes"},
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
            run: {done: "Run finished", failed: "Run failed", cancelled: "Run cancelled", interrupted: "Run interrupted"},
            failed: "{n} couldn't be analyzed: {why}", addressed: "{n} already addressed by an improvement", posted: "Up for review", decisions: {accept: "Accepted", decline: "Declined", revise: "Asked for a revision",
            needs_improvement: "Said it needs improvement", fine_as_is: "Said it was fine as it was", was_test: "Marked as a test",
            undo: "Undid", ignore: "Ignored"}, auto: "Accepted automatically"},
};
const fill = (text, values) => text.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? "");
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
              clock: "M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16zm0-18a10 10 0 1 1 0 20 10 10 0 0 1 0-20zm.5 5v5.25l4.5 2.67-.75 1.23L11 13V7z"};
const svg = (name, cls = "") => `<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="${ICON[name]}"/></svg>`;
const COG = "M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43,12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37C21.73,9.22 21.78,8.95 21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5,2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95,5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.21,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.21,15.05 2.34,15.27L4.34,18.73C4.46,18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.67 16.04,18.34 16.56,17.94L19.05,18.95C19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z";
const STATUS = {awaiting_review: "To review", needs_attention: "Needs attention", applied: "Applied", undone: "Undone",
  declined: "Declined", revision_requested: "Revision requested", revised: "Revised", ignored: "Ignored",
  conflict: "Changed since tested"};

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
    padding: 24px; margin: 8px 0 16px; text-align: center; }
  .hero-title { font-size: 20px; font-weight: 500; margin-bottom: 8px; }
  .hero .lead { margin: 0 auto 16px; max-width: 560px; }
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
      if (ev.key === "Escape" && (this._pop || this._runMenu || this._confirming)) {
        this._pop = null; this._runMenu = false; this._confirming = false; this._render();
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
        if (!this.isConnected || this._review) return;
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
    try {
      if (params.get("view") === "settings") {
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
        this._list = (await this._hass.callWS({type: "assist_improver/improvements"})).improvements;
        this._household = await this._hass.callWS({type: "assist_improver/household"});
        this._run = await this._hass.callWS({type: "assist_improver/run"});
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
    else if (this._view === "settings" && this._settings) body = this._renderSettings(this._settings);
    else if (this._review) body = this._renderReview(this._review);
    else if (this._list) body = this._renderList(this._list);
    else body = this._toolbar("Assist Improver") + `<div class="page">Loading…</div>`;
    root.innerHTML = `<style>${STYLE}</style>${body}<div class="tip" id="tip" hidden></div>`;
    this._wire();
    if (this._review) this._keepScroll();
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
    const what = {done: "Last run", failed: "Last run failed", cancelled: "Last run cancelled",
                  interrupted: "Last run was interrupted"}[run.status] || "Last run";
    const found = p.new_improvements != null ? ` · ${plural(p.new_improvements, "new improvement")}` : "";
    return `${what} ${ago(run.finished || run.started)}${found}`;
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
    const error = run?.error && run.status === "failed" ? `<div class="error-text">${esc(run.error)}</div>` : "";
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
        <div class="lead">The improver looks for where you had to repeat, correct or clarify yourself, and tests fixes
        for it. ${esc(this._lastRun(run))}</div>${error}
        <button class="btn play" id="run-now">${svg("play")}${esc(TEXT.runNow)}</button></div>`;
    } else if (!preview?.conversations_recorded) {
      main = `<div class="hero"><div class="hero-title">Use Assist as usual</div>
        <div class="lead">The improver learns from your conversations with Assist. Once you've had a few, run it here.</div></div>`;
    } else {
      main = `<div class="hero"><div class="hero-title">All caught up</div>
        <div class="lead">Nothing to review, and no new conversations since your last run. ${esc(this._lastRun(run))}</div>${error}
        <button class="link" id="run-anyway">Run anyway</button></div>`;
    }
    const history = earlier.length ? `<details class="history" data-key="history" ${this._open.has("history") ? "open" : ""}>
      <summary><span class="name">Earlier improvements (${earlier.length})</span></summary>
      <div class="card">${this._rows(earlier)}</div></details>` : "";
    const settings = `<button class="tb-btn" id="open-settings" title="Settings" aria-label="Settings">
      <svg viewBox="0 0 24 24"><path d="${COG}"/></svg></button>`;
    return this._toolbar("Assist Improver", false, settings) +
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
      <span class="info-tip" data-tip="${esc(tip)}" tabindex="0" aria-label="About ${esc(text)}">${svg("info")}</span></div>`;
    const head = `<div class="flow-head"><div class="toggles">${row("practice", s.practice.on, TEXT.sandbox.label, sandboxTip)}
      ${row("auto-accept", s.auto_accept, TEXT.autoAccept.label, TEXT.autoAccept.tip)}</div>
      <span class="spacer"></span>${run}</div>`;
    const label = `<span class="stream-label ${L.labelVertical ? "vertical" : ""}" style="left:${L.label[0]}px;top:${L.label[1]}px">${
      esc(L.vertical ? TEXT.revise.short : TEXT.revise.label)}</span>`;
    const flow = `<div class="card flow-card">${head}<div class="track ${L.vertical ? "vertical" : ""}" style="height:${L.H}px">
      ${this._trackSvg(L, running)}${ids.map((id) => this._step(id, L, s)).join("")}${label}</div></div>`;
    const confirm = this._confirming && preview ? this._confirm(preview) : "";
    const shade = this._runMenu ? `<div class="backdrop" id="menu-backdrop"></div>` : "";
    return this._toolbar("Settings", true) + `<div class="page wide">${flow}${this._history()}</div>${shade}${this._popover(s)}${confirm}`;
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
      return [`${H.run[e.status] || "Run"}${e.only ? ` · ${GATE_NAMES[e.only]} only` : ""}${e.trigger === "scheduled" ? " · scheduled" : ""}`,
              e.error || [...parts, failed].filter(Boolean).join(" · ")];
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
    const auto = root.getElementById("auto-accept");
    auto?.addEventListener(auto.tagName === "HA-SWITCH" ? "change" : "click", (ev) => {
      ev.preventDefault(); this._saveSetting({auto_accept: !this._settings.auto_accept});
    });
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
