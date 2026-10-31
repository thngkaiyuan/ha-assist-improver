// The panel's styles: Home Assistant's theme variables, with the panel's own tints on top.

export const STYLE = `
  /* A column the height of the screen at least: the header, the page, and a footer at the bottom however short the page,
     so it never sits under a short list and drops away as the page grows. */
  :host { display: flex; flex-direction: column; min-height: 100vh; background: var(--primary-background-color);
    color: var(--primary-text-color);
    font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
    --add-bg: rgba(46, 160, 67, 0.15); --add-num: rgba(46, 160, 67, 0.30); --add-fg: #1a7f37;
    --del-bg: rgba(248, 81, 73, 0.15); --del-num: rgba(248, 81, 73, 0.30); --del-fg: #cf222e; --warn-fg: #946100; }
  /* Text in the panel's own tints reads at 3:1 or better: deeper tones on a light theme, the brighter ones on a dark. */
  :host(.dark) { --add-fg: #2da44e; --del-fg: #e5534b; --warn-fg: #c9861a; }
  /* The screen's safe areas (a phone's status bar and home indicator, its rounded corners in landscape), as Home
     Assistant gives them: the panel handles them itself (panel.py), so the header runs up under the status bar and
     the footer down to the bottom edge, with what's in them clear of both, and nothing shifts at the end of a scroll. */
  :host { --inset-top: var(--safe-area-inset-top, 0px); --inset-bottom: var(--safe-area-inset-bottom, 0px);
    --inset-left: var(--safe-area-content-inset-left, var(--safe-area-inset-left, 0px));
    --inset-right: var(--safe-area-inset-right, 0px); }
  .region { display: contents; }
  .menu-slot { display: flex; }
  .loading { display: flex; align-items: center; gap: 10px; padding: 8px 4px; color: var(--secondary-text-color);
    animation: appear .2s .15s both; }
  .spinner { flex: none; width: 1.1em; height: 1.1em; box-sizing: border-box; border-radius: 50%;
    border: 2px solid var(--divider-color); border-top-color: var(--primary-color); animation: spin .8s linear infinite; }
  @media (prefers-reduced-motion: reduce) { .spinner { animation-duration: 2.4s; } }
  .dv-loading { display: flex; align-items: center; gap: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes appear { from { opacity: 0; } }
  /* Motion: what a tap reveals fades in where it will stay (the panel marks it fresh for the fade, app.js); nothing
     slides, grows or moves from under the finger. */
  @media (prefers-reduced-motion: no-preference) { .fresh { animation: appear .16s ease-out; } }
  /* Under a phone's status bar (Home Assistant's app): the toolbar's colour, pinned, so nothing scrolls under the clock. */
  .status-strip { position: sticky; top: 0; z-index: 12; flex: none; height: var(--inset-top);
    background: var(--app-header-background-color, var(--primary-color)); }
  .toolbar { display: flex; align-items: center; gap: 8px; height: 56px; font-size: 20px;
    padding: 0 max(12px, var(--inset-right)) 0 max(12px, var(--inset-left));
    background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, white); }
  .tb-back { display: inline-flex; align-items: center; gap: 8px; border: none; background: none; color: inherit;
    font: inherit; padding: 6px 8px; margin-left: -8px; border-radius: 8px; cursor: pointer; }
  .tb-back:hover { background: rgba(127, 127, 127, 0.15); }
  .page { width: 100%; max-width: 992px; box-sizing: border-box; margin: 0 auto;  /* 960 px of content, as wide as can be. */
    padding: 16px max(16px, var(--inset-right)) 24px max(16px, var(--inset-left)); }
  a { color: var(--primary-color); text-decoration: none; cursor: pointer; }
  .card { background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px;
    overflow: hidden; margin-bottom: 12px; }
  .item { display: flex; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid var(--divider-color); cursor: pointer; }
  button.item { width: 100%; border-width: 0 0 1px; border-style: solid; background: none; font: inherit; color: inherit; text-align: left; }
  .item:last-child { border-bottom: none; }
  .item:hover { background: var(--secondary-background-color); }
  .item .title { flex: 1; }
  .item .go { font-size: 22px; line-height: 1; color: var(--secondary-text-color); }
  .item .tag { border: 1px solid var(--divider-color); background: none; margin-left: 4px; vertical-align: 1px; }
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
  /* Each kind of change its own tint (green and red stay for added and removed lines): mid-tone washes, and the text in
     a deeper tone of each on a light theme. */
  .kind.script { color: #7046b8; background: rgba(154, 109, 215, 0.12); border-color: rgba(154, 109, 215, 0.40); }
  .kind.exposure { color: #1a68b8; background: rgba(61, 139, 217, 0.12); border-color: rgba(61, 139, 217, 0.40); }
  .kind.aliases { color: #8a5a00; background: rgba(201, 134, 26, 0.12); border-color: rgba(201, 134, 26, 0.40); }
  .kind.instructions { color: #13716d; background: rgba(31, 158, 154, 0.12); border-color: rgba(31, 158, 154, 0.40); }
  .kind.household { color: #13716d; background: rgba(31, 158, 154, 0.12); border-color: rgba(31, 158, 154, 0.40); }
  .kind.assistant { color: #5f6670; background: rgba(138, 143, 152, 0.14); border-color: rgba(138, 143, 152, 0.45); }
  :host(.dark) .kind.script { color: #9a6dd7; } :host(.dark) .kind.exposure { color: #3d8bd9; }
  :host(.dark) .kind.aliases { color: #c9861a; } :host(.dark) .kind.instructions, :host(.dark) .kind.household { color: #1f9e9a; }
  :host(.dark) .kind.assistant { color: #8a8f98; }
  .count-add { color: var(--add-fg); font-weight: 600; } .count-del { color: var(--del-fg); font-weight: 600; }
  .bar { display: inline-flex; gap: 2px; } .bar i { width: 8px; height: 8px; border-radius: 2px; background: var(--divider-color); }
  .bar i.a { background: var(--add-fg); } .bar i.d { background: var(--del-fg); }
  .line { display: flex; gap: 8px; padding: 4px 12px; }
  .line.open { background: color-mix(in srgb, var(--warn-fg) 10%, transparent); cursor: default; }
  .line.open .sign { color: var(--warn-fg); font-weight: 700; }
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
  .tests { list-style: none; margin: 0 0 14px; padding: 0; container-type: inline-size; }
  .tests .row { display: flex; align-items: baseline; gap: 8px; padding: 3px 0; }
  .tests div.row::before { content: ""; width: 12px; flex: none; }  /* In line with the rows that open. */
  .tests details { border-top: none; }
  /* A test opened: its conversation as it went, then its replay with the change; side by side where there's room. */
  .rp { display: grid; gap: 18px 24px; padding: 10px 0 16px; }
  @container (min-width: 620px) { .rp.two { grid-template-columns: 1fr 1fr; } }
  .rp-side { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  .rp-head { font-size: 13px; font-weight: 600; color: var(--secondary-text-color); padding-bottom: 6px;
    border-bottom: 1px solid var(--divider-color); }
  .rp-empty { margin: 0; font-size: 14px; color: var(--secondary-text-color); }
  .tests .ok .rp-after .rp-head { color: var(--add-fg); }  /* Where an improved test's after starts, at a glance. */
  .rp-open { border: 1px solid var(--divider-color); border-radius: 8px; overflow: hidden; }
  .tests div.row + .rp-open { margin: 6px 0 4px 20px; }  /* Under a test with no replay to open. */
  .rp-open-head { font-size: 12px; font-weight: 600; color: var(--warn-fg); padding: 6px 12px 2px;
    background: color-mix(in srgb, var(--warn-fg) 10%, transparent); }
  .tests .mark { width: 16px; font-weight: 700; } .tests .ok .mark { color: var(--add-fg); }
  .tests .no .mark { color: var(--secondary-text-color); }
  .tests .result { font-size: 12px; padding: 0 8px; border-radius: 10px; white-space: nowrap; }
  /* Narrow (a phone): what a test showed goes on its own line, under what it tested, so a longer result ("3 → 1 turns ·
     follows your feedback") never squeezes it; the longest wraps within the row. */
  @container (max-width: 479px) { .tests .row { flex-wrap: wrap; row-gap: 2px; }
    .tests .row > span:nth-child(2) { flex: 1 1 calc(100% - 48px); }
    .tests .result { margin-left: 44px; white-space: normal; } }
  .tests .ok .result { color: var(--add-fg); background: var(--add-bg); }
  .tests .no .result { color: var(--secondary-text-color); background: var(--secondary-background-color); }
  .tests .result.more-words, .tests .result.still-open { color: var(--warn-fg);
    background: color-mix(in srgb, var(--warn-fg) 12%, transparent); }
  .note { display: flex; gap: 8px; font-size: 13px; color: var(--secondary-text-color); border-top: 1px solid var(--divider-color);
    padding-top: 10px; margin-top: 4px; }
  div.conflict { background: var(--del-bg); border: 1px solid var(--del-fg); border-radius: 8px; padding: 10px 14px; margin-bottom: 12px;
    overflow-wrap: anywhere; }
  .footer { position: sticky; bottom: 0; margin-top: auto; z-index: 11; background: var(--card-background-color);
    border-top: 1px solid var(--divider-color);
    padding: 12px max(16px, var(--inset-right)) calc(12px + var(--inset-bottom)) max(16px, var(--inset-left)); }
  .footer-inner { max-width: 960px; margin: 0 auto; display: flex; flex-direction: column; gap: 8px; align-items: flex-end; }
  .reason { align-self: stretch; display: flex; align-items: flex-start; gap: 10px; font-size: 14px; line-height: 1.4;
    cursor: pointer; }
  .reason input { width: 18px; height: 18px; flex: none; margin: 1px 0 0; accent-color: var(--primary-color); cursor: pointer; }
  .reason-label { font-weight: 500; } .reason-hint { color: var(--secondary-text-color); }
  textarea { width: 100%; box-sizing: border-box; min-height: 64px; padding: 8px; border-radius: 8px; font: inherit;
    border: 1px solid var(--divider-color); background: var(--primary-background-color); color: var(--primary-text-color); }
  .split { position: relative; display: inline-flex; }
  .split button { border: none; font: inherit; font-weight: 500; cursor: pointer; color: var(--text-primary-color, white);
    background: var(--primary-color); padding: 10px 16px; }
  .split .main { border-radius: 20px 0 0 20px; } .split .arrow { border-radius: 0 20px 20px 0; border-left: 1px solid rgba(255,255,255,.35); padding: 10px 12px; }
  .split button:disabled { opacity: .6; cursor: default; }
  .split.one .main { border-radius: 20px; }
  /* A decision on its way: the arrow turns into a spinner, so nothing changes size under the finger. */
  .split .arrow { position: relative; } .split[aria-busy] .arrow .caret { visibility: hidden; }
  .split .arrow .spinner { position: absolute; inset: 0; margin: auto; width: 12px; height: 12px;
    border-color: rgba(255, 255, 255, .35); border-top-color: var(--text-primary-color, white); }
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
  .btn.play { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 8px 16px 8px 13px; }
  /* A quiet secondary action, and a destructive one kept quieter than the main action (tinted, not filled): outlined
     inside, so they line up with the filled ones; the red text leans toward the theme's text colour to read in both. */
  .btn.quiet { background: var(--secondary-background-color); color: var(--primary-text-color); box-shadow: inset 0 0 0 1px var(--divider-color); }
  .btn.quiet:hover:not(:disabled) { background: color-mix(in srgb, var(--primary-text-color) 8%, var(--secondary-background-color)); }
  .btn.soft-danger { background: var(--del-bg); color: color-mix(in srgb, var(--del-fg) 75%, var(--primary-text-color));
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--del-fg) 45%, transparent); }
  .btn.soft-danger:hover:not(:disabled) { background: color-mix(in srgb, var(--del-fg) 22%, transparent); }
  .stepper { display: flex; gap: 6px; margin: 12px 0 4px; }
  .step { flex: 1; text-align: center; font-size: 12px; color: var(--secondary-text-color); }
  .step .bar { display: block; height: 4px; border-radius: 2px; background: var(--divider-color); margin-bottom: 6px; }
  .step.done .bar { background: var(--add-fg); } .step.now .bar { background: var(--primary-color); animation: pulse 1.2s infinite; }
  .step.now { color: var(--primary-text-color); font-weight: 500; }
  @keyframes pulse { 50% { opacity: .45; } }
  .confirm { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--divider-color); line-height: 1.55; }
  .confirm ul { margin: 6px 0 10px; padding-left: 20px; }
  .confirm .cost { color: var(--secondary-text-color); font-size: 13px; margin-bottom: 10px; }
  .confirm .c-warn { color: var(--warn-fg); font-size: 13px; }
  .confirm .actions { display: flex; gap: 8px; justify-content: flex-end; }
  .error-text { color: var(--del-fg); font-size: 13px; margin-top: 6px; }
  /* On a phone the line numbers give way: their columns shrink to nothing (hidden cells would leave a gap row's
     columns to squeeze the text). */
  @media (max-width: 600px) { table.diff td.num { width: 0; padding: 0; font-size: 0; } .page { padding: 12px max(10px, var(--inset-right)) 24px max(10px, var(--inset-left)); }
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
  .gains { display: block; color: var(--add-fg); font-size: 13px; font-weight: 500; }
  .run-line { display: flex; align-items: center; justify-content: space-between; gap: 8px;
    color: var(--secondary-text-color); font-size: 13px; margin: -4px 4px 16px; }
  .link { border: none; background: none; color: var(--primary-color); font: inherit; font-weight: 500; cursor: pointer; padding: 4px 0; }
  .history { border: none; margin-top: 8px; } .history > summary { padding: 8px 4px; color: var(--secondary-text-color); }
  .next { display: flex; justify-content: flex-end; margin: -4px 0 12px; }
  .revises { padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; border: 1px solid var(--primary-color);
    color: var(--primary-text-color); background: var(--card-background-color); }
  .revises.unrevised { border-color: var(--warn-fg); }
  .done-row { flex-direction: row !important; align-items: center !important; justify-content: space-between; flex-wrap: wrap; gap: 12px !important; }
  .done-msg { display: flex; align-items: baseline; gap: 8px; flex: 1 1 320px; min-width: 0; line-height: 1.45; }
  .footer-inner:not(.done-row) > .done-msg { flex: none; align-self: stretch; }  /* Above a decision open again. */
  .done-msg .mark { font-weight: 700; } .done-msg.ok .mark { color: var(--add-fg); } .done-msg.bad .mark { color: var(--del-fg); }
  .done-row .btn { max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
  .done-row > :last-child { margin-left: auto; }  /* The way on stays where the decision's button was. */
  .outcome-banner { padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; border: 1px solid; }
  .outcome-banner.ok { background: var(--add-bg); border-color: var(--add-fg); }
  .outcome-banner.bad { background: var(--del-bg); border-color: var(--del-fg); }
  .tb-btn { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; border: none; background: none;
    color: inherit; font: inherit; font-size: 15px; cursor: pointer; padding: 8px; border-radius: 18px; }
  .tb-btn:hover { background: rgba(255, 255, 255, 0.12); }
  .tb-btn svg { width: 22px; height: 22px; fill: currentColor; }
  .page.wide { max-width: 1212px; }  /* 1180 px of content. */
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
  /* On a phone the run control gets a row of its own, above the switches, so its words fit on one line. */
  @media (max-width: 600px) { .flow-head { flex-direction: column-reverse; align-items: stretch; }
    .flow-head .spacer { display: none; } .flow-head > :last-child { align-self: flex-end; } }
  .glyph.in-chip { --mdc-icon-size: 14px; width: 14px; height: 14px; margin-right: 4px; vertical-align: -2px; }
  .history { margin-top: 12px; } .history .h-title { font-size: 14px; font-weight: 500; padding: 12px 16px 4px; }
  .h-row { display: flex; align-items: center; gap: 10px; padding: 8px 16px; font-size: 13.5px; }
  .h-row .glyph { --mdc-icon-size: 18px; width: 18px; height: 18px; } .h-row .h-text { flex: 1; min-width: 0; line-height: 1.35; }
  .h-row .h-sub { color: var(--secondary-text-color); font-size: 12.5px; }
  .h-details summary { cursor: pointer; color: var(--primary-color); font-size: 12.5px; margin-top: 2px; }
  .h-details code { display: block; margin-top: 4px; font-size: 12px; white-space: pre-wrap; word-break: break-word; color: var(--secondary-text-color); }
  .h-row .h-when { color: var(--secondary-text-color); font-size: 12.5px; white-space: nowrap; }
  .h-row.h-link { cursor: pointer; } .h-row.h-link:hover { background: var(--secondary-background-color); }
  .h-more { padding: 6px 16px 12px; } .h-empty { padding: 4px 16px 14px; color: var(--secondary-text-color); font-size: 13px; }
  .modal-shade { position: fixed; inset: 0; z-index: 20; background: rgba(0, 0, 0, .45); }
  /* Your conversations, on the home page. */
  .cs { margin-top: 20px; }
  .cs-top { display: flex; align-items: center; gap: 6px; margin: 0 0 12px; }
  .cs-title { font-size: 18px; font-weight: 500; margin: 0; } .cs-top .info-tip .ic { width: 15px; height: 15px; }
  .cs-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; }
  .cs-find { position: relative; display: flex; flex: 1 1 220px; min-width: 0; }
  .cs-find input { padding-right: 34px; } #cs-search::-webkit-search-cancel-button { display: none; }
  /* The day filter and the Status filter, side by side; Status opens a menu of checkboxes, each with how many. */
  .cs-filters { display: inline-flex; align-items: center; gap: 8px; flex: none; }
  .cs-status { position: relative; flex: none; }
  .cs-status-btn { display: inline-flex; align-items: center; gap: 4px; font: inherit; font-size: 13px; padding: 7px 12px;
    border-radius: 18px; border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color);
    cursor: pointer; white-space: nowrap; }
  .cs-status-btn.on { border-color: var(--primary-color); color: var(--primary-color); }
  .cs-status-menu { position: absolute; right: 0; top: calc(100% + 6px); z-index: 12; min-width: 230px; max-height: 60vh;
    overflow: auto; padding: 6px 0; box-sizing: border-box; border-radius: 12px; border: 1px solid var(--divider-color);
    background: var(--card-background-color); box-shadow: 0 8px 24px rgba(0, 0, 0, .3); }
  .cs-status-opt { display: flex; align-items: center; gap: 10px; padding: 8px 14px; font-size: 14px; cursor: pointer; }
  .cs-status-opt:hover { background: var(--secondary-background-color); }
  .cs-bar .cs-status-opt input { flex: none; width: 18px; height: 18px; margin: 0; padding: 0; accent-color: var(--primary-color);
    cursor: pointer; }
  .cs-controls:has(.cs-status-menu) { z-index: 12; }  /* Its menu open: over the list and the Run bar. */
  .cs-status-name { flex: 1; } .cs-status-n { color: var(--secondary-text-color); font-size: 13px; }
  /* Narrow (a phone, or a narrow window beside Home Assistant's sidebar: the band's own width decides): the search and
     the order share the first row, the search giving way to the order's name, whichever it is; the day filter and
     Status the next. The order stays at the right end of its row, its menu opening on the screen. */
  .cs-controls { container-type: inline-size; }
  .cs-sort { margin-left: auto; }
  @container (max-width: 780px) {  /* Up to where one row holds them all, whichever order's and status's names (766 px). */
    .cs-bar .cs-find { flex: 1 1 0; min-width: 0; }
    .cs-filters { display: flex; width: 100%; order: 1; } .cs-filters .seg { flex: 1 0 auto; width: auto; }
    /* A long status's name takes its room from the day filter, whose words stay on one line; only on the smallest
       phones does the name give way, cut short. */
    .cs-filters .seg button { flex: 1; white-space: nowrap; }
    .cs-filters .cs-status { flex: 0 1 auto; min-width: 0; } .cs-filters .cs-status-btn { max-width: 100%; }
    /* The order shares its row with the search, which takes up a change in its width: nothing else moves. */
    .cs-sort .cs-sizer::after { content: none; } }
  .cs-status-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  /* The order's and Status's buttons are as wide as their longest choice (each choice a hidden line, sizing it). */
  .cs-sizer { display: inline-grid; grid-template-columns: minmax(0, auto); min-width: 0; text-align: left; }
  .cs-sizer > *, .cs-sizer::after { grid-area: 1 / 1; }
  .cs-sizer::after { content: attr(data-choices); white-space: pre; visibility: hidden; height: 0; overflow: hidden; }
  .cs-sort-menu { min-width: 200px; padding: 6px 0; }
  .cs-sort-menu button { display: block; width: 100%; border: none; background: none; font: inherit; font-size: 14px;
    color: var(--primary-text-color); text-align: left; padding: 9px 16px; cursor: pointer; }
  .cs-sort-menu button:hover { background: var(--secondary-background-color); }
  .cs-sort-menu button.on { color: var(--primary-color); font-weight: 500; }
  .cs-clear-search { position: absolute; right: 4px; top: 50%; transform: translateY(-50%); width: 28px; height: 28px;
    border: none; border-radius: 14px; background: none; color: var(--secondary-text-color); font-size: 15px; cursor: pointer; }
  .cs-clear-search:hover { background: var(--secondary-background-color); }
  .cs-bar input, .cs-note { flex: 1 1 220px; min-width: 0; font: inherit; font-size: 14px; padding: 8px 12px; border-radius: 8px;
    box-sizing: border-box; color: var(--primary-text-color); border: 1px solid var(--divider-color); background: var(--card-background-color); }
  .cs-all { display: inline-flex; align-items: center; gap: 10px; margin: 2px 0 10px 16px; font-size: 14px; cursor: pointer; }
  .cs-all input, .cs-pick input { width: 18px; height: 18px; accent-color: var(--primary-color); cursor: pointer; margin: 0; }
  /* Clipped, not hidden: an opened conversation's send row can stick to the bottom of the page (above the Run bar). */
  .cs-list { overflow: clip; }
  .cs-row { border-bottom: 1px solid var(--divider-color); }
  /* Search, the day filter, the sort and Select all stay on top while the list scrolls. */
  /* Even padding all round: the bar's own margin would slip out of the box when Select all isn't there. */
  .cs-controls { position: sticky; top: var(--inset-top); z-index: 3; margin: 0 0 10px; padding: 8px; border-radius: 12px;
    background: var(--card-background-color); box-shadow: 0 1px 0 var(--divider-color); }
  .cs-controls .cs-bar { margin-bottom: 0; }
  /* A block (no line box under it), its box in line with each row's below. */
  .cs-controls .cs-all { display: flex; width: fit-content; margin: 12px 0 0 8px; }
  .cs-row:last-child { border-bottom: none; }
  .cs-row.picked { background: color-mix(in srgb, var(--primary-color) 7%, transparent); }
  .cs-line { display: flex; align-items: stretch; }
  .cs-pick { flex: none; display: flex; align-items: center; justify-content: center; width: 48px; cursor: pointer; }
  .cs-head { flex: 1; min-width: 0; display: flex; align-items: center; gap: 12px; padding: 12px 16px 12px 0; border: none;
    background: none; font: inherit; color: inherit; text-align: left; cursor: pointer; }
  .cs-line:hover .cs-head, .cs-row.open .cs-head { color: inherit; } .cs-line:hover { background: var(--secondary-background-color); }
  .cs-text { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .cs-said { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cs-sub { font-size: 13px; color: var(--secondary-text-color); } .cs-failed { color: var(--warn-fg); }
  .cs-reply { font-size: 13px; color: var(--secondary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cs-close { font-size: 13px; color: var(--secondary-text-color); margin: 0 0 8px; }
  .cs-last { font-size: 13px; color: var(--secondary-text-color); margin: -8px 0 12px; }
  .chip.cs-waiting, .chip.cs-sent, .chip.cs-queued, .chip.cs-working, .chip.cs-writing,
  .chip.cs-unclear { border-color: var(--primary-color); color: var(--primary-color); }
  .chip.cs-cannot { align-self: center; flex: none; margin-right: 16px; border-color: var(--warn-fg); color: var(--warn-fg);
    cursor: help; }
  button.chip.cs-review { align-self: center; flex: none; margin-right: 16px; font: inherit; font-size: 12px; cursor: pointer;
    border-color: var(--primary-color); background: color-mix(in srgb, var(--primary-color) 14%, transparent); color: var(--primary-color); }
  button.chip.cs-review:hover { background: color-mix(in srgb, var(--primary-color) 24%, transparent); }
  .chip.cs-improved { border-color: var(--add-fg); background: var(--add-bg); color: var(--add-fg); }
  .cs-text mark, .cs-turn mark, .cs-sub mark { background: color-mix(in srgb, var(--primary-color) 22%, transparent); color: inherit; border-radius: 2px; }
  .cs-read { padding: 6px 16px 16px 48px; display: flex; flex-flow: row wrap; gap: 18px 8px; }
  .cs-turn { display: flex; flex-direction: column; gap: 8px; font-size: 14px; line-height: 1.5; }
  .cs-msg { display: flex; flex-direction: column; gap: 3px; max-width: min(68ch, 88%); }
  .cs-you { align-self: flex-end; align-items: flex-end; } .cs-assist { align-self: flex-start; }
  .cs-who { font-size: 12px; font-weight: 500; color: var(--secondary-text-color); padding: 0 6px; }
  .cs-says { padding: 8px 12px; border-radius: 14px; color: var(--primary-text-color); overflow-wrap: anywhere; }
  .cs-you .cs-says { background: color-mix(in srgb, var(--primary-color) 18%, var(--card-background-color));
    border-bottom-right-radius: 4px; }
  .cs-assist .cs-says { background: var(--secondary-background-color); border-bottom-left-radius: 4px; }
  .cs-says p { margin: 0; } .cs-says p + p { margin-top: 8px; }
  .cs-step { align-self: flex-start; display: flex; gap: 10px; max-width: min(68ch, 88%); box-sizing: border-box;
    padding: 8px 12px; border-radius: 10px; font-size: 13px; line-height: 1.45;
    background: color-mix(in srgb, var(--warn-fg) 10%, transparent); border: 1px solid color-mix(in srgb, var(--warn-fg) 35%, transparent); }
  .cs-step-mark { flex: none; width: 18px; height: 18px; border-radius: 9px; background: var(--warn-fg);
    color: var(--card-background-color); font-weight: 700; font-size: 12px; display: flex; align-items: center; justify-content: center; }
  .cs-step-label { font-weight: 600; color: var(--warn-fg); } .cs-step-what { overflow-wrap: anywhere; }
  .cs-step-error { display: block; margin-top: 2px; color: var(--secondary-text-color); overflow-wrap: anywhere; }
  .cs-send { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 4px; padding: 8px 0; min-height: 40px;
    position: sticky; bottom: max(var(--bar, 0px), var(--inset-bottom)); background: var(--card-background-color); }
  .cs-row.picked .cs-send { background: linear-gradient(color-mix(in srgb, var(--primary-color) 7%, transparent),
    color-mix(in srgb, var(--primary-color) 7%, transparent)), var(--card-background-color); }
  .cs-done { font-size: 13px; color: var(--add-fg); }
  /* The note, Send and the ⋮ stay in view (sticky); Archive and Delete open from the ⋮, upwards. */
  .cs-read > * { flex: 1 1 100%; min-width: 0; } .cs-read > .cs-confirm { flex: 1 1 100%; }
  .cs-actions { position: relative; flex: none; display: inline-flex; }
  .cs-dots { width: 40px; height: 40px; border-radius: 20px; border: 1px solid var(--divider-color); background: none;
    color: var(--secondary-text-color); cursor: pointer; display: inline-flex; align-items: center; justify-content: center; }
  .cs-dots:hover, .cs-dots[aria-expanded="true"] { background: var(--secondary-background-color); color: var(--primary-text-color); }
  .cs-dots .ic { width: 20px; height: 20px; }
  .cs-menu { bottom: 46px; right: 0; z-index: 12; min-width: 250px; width: max-content; max-width: calc(100vw - 48px); }
  .cs-menu button { display: flex; align-items: center; gap: 10px; width: 100%; padding: 10px 14px; border: none;
    background: none; font: inherit; color: var(--primary-text-color); text-align: left; cursor: pointer; }
  .cs-menu button:hover { background: var(--secondary-background-color); } .cs-menu button .ic { width: 18px; height: 18px; }
  .cs-menu button.danger { color: var(--del-fg); }
  .cs-tidy { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 4px; padding: 8px 0; }
  .cs-confirm { gap: 8px 12px; } .cs-confirm span { flex: 1 1 240px; font-size: 13px; line-height: 1.45; }
  .cs-confirm > .conflict { flex: 1 1 100%; margin: 0; }
  .cs-archived .cs-text { opacity: .6; }
  .cs-all.cs-show-archived { margin: 12px 0 0 16px; color: var(--secondary-text-color); }
  /* Below Show archived, so ticking it moves nothing: the archived ones, headed by their own Select all. */
  .cs-away { margin-top: 10px; } .cs-away > .cs-all { display: flex; width: fit-content; }
  .cs-more { margin: 10px 0 0; } .cs-empty { color: var(--secondary-text-color); }
  .cs-footer .footer-inner { flex-direction: row; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 8px 16px; }
  .cs-ask { flex: 1 1 100%; display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 8px 12px; }
  .cs-ask span { flex: 1 1 220px; font-size: 14px; line-height: 1.45; }
  @media (max-width: 600px) { .cs-footer #cs-run { flex: 1 1 100%; } .cs-footer .btn { flex: 1 1 auto; } }
  @media (max-width: 600px) { .cs-read { padding-left: 16px; } .cs-send .btn, .cs-confirm .btn { flex: 1 1 auto; } }
  .pop-privacy { border-top: 1px solid var(--divider-color); margin-top: 6px; padding-top: 6px; }
  .pop-toggle { padding: 6px 14px; }
  .pop-sub { padding: 6px 14px 0 28px; font-size: 12.5px; color: var(--secondary-text-color); }
  .pop-people .pop-toggle { padding-left: 28px; }
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
  .modal-body.confirm .c-which { font-size: 15px; margin-bottom: 10px; overflow-wrap: anywhere; }
  .modal-body.confirm .c-also { margin-bottom: 12px; }
  /* The rough estimate: a short list under the cost line, each model's figure on a line of its own. */
  .modal-body.confirm .c-cost p { margin: 0 0 4px; } .modal-body.confirm .c-cost p:last-child { margin: 4px 0 0; }
  .modal-body.confirm .c-spend { margin: 0; padding-left: 18px; color: var(--primary-text-color); line-height: 1.5; }
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
  .kn-empty { margin: 0; padding: 14px 20px 0; font-size: 14px; line-height: 1.45; color: var(--secondary-text-color); }
  @media (max-width: 600px) { .modal { top: auto; bottom: 0; transform: translateX(-50%); width: 100vw; max-height: 88vh;
    border-radius: 16px 16px 0 0; padding-bottom: var(--inset-bottom); } }
  .split.down { z-index: 11; }
  .split.down .menu { top: 44px; bottom: auto; z-index: 12; min-width: 250px; width: max-content; max-width: calc(100vw - 32px); }
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
  /* An ⓘ reads at 3:1 or better, as any mark that says something is there to open. */
  .info-tip { flex: none; display: inline-flex; color: var(--secondary-text-color); cursor: help; }
  .info-tip .ic { width: 13px; height: 13px; } .info-tip:hover { color: var(--primary-text-color); }
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
  :host(.dark) .gate { background: #2b2c2f; border-color: rgba(255, 255, 255, .24); color: #d0d0d0; }
  :host(.dark) .gate.paused { background: var(--held); border-color: var(--held); color: #fff; }
  :host(.dark) .glyph { color: #cfcfcf; }
  :host(.dark) .info-tip { color: #c4c4c4; } :host(.dark) .info-tip:hover { color: var(--primary-text-color); }
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
  .btn .spinner { border-color: color-mix(in srgb, currentColor 35%, transparent); border-top-color: currentColor; }
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
  .chip-model.warn { color: var(--warn-fg); border-color: rgba(201, 134, 26, .6); }
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
  /* Vertical: the title in full, its controls beside it, or below it when they don't fit (the card grows to fit). */
  .track.vertical .stn { flex-direction: row; flex-wrap: wrap; align-items: center; justify-content: flex-end;
    gap: 6px 10px; }
  .track.vertical .s-head { flex: 1 0 auto; } .track.vertical .s-title { flex: none; }
  .track.vertical .chip-row { flex: 0 1 auto; max-width: 62%; } .track.vertical .chip-row .chip-model { max-width: none; }
  .chip-row .chip-model { max-width: none; flex: 0 1 auto; min-width: 0; }
  .track.vertical .chip-model { align-self: center; max-width: 46%; }
  .info { border: none; background: none; padding: 2px; margin: 0 0 0 2px; cursor: pointer; color: var(--secondary-text-color);
    display: inline-flex; vertical-align: middle; } .info .ic { width: 15px; height: 15px; }
  .backdrop { position: fixed; inset: 0; z-index: 10; }
  .pop { position: fixed; z-index: 11; width: 320px; max-height: 70vh; overflow: auto; box-sizing: border-box;
    background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 12px;
    box-shadow: 0 8px 28px rgba(0, 0, 0, .3); padding: 6px 0; }
  .pop.sheet { left: 0 !important; right: 0; top: auto !important; bottom: 0; width: auto; border-radius: 16px 16px 0 0; max-height: 80vh;
    padding-bottom: calc(6px + var(--inset-bottom)); }
  .pop-head { padding: 8px 14px 2px; font-size: 14px; }
  .pop-lead { padding: 0 14px 8px; font-size: 12.5px; color: var(--secondary-text-color); line-height: 1.4; }
  .opt { padding: 8px 14px; cursor: pointer; } .opt:hover { background: var(--secondary-background-color); }
  .opt-main { display: flex; align-items: center; gap: 8px; } .opt-main .spacer { flex: 1; }
  .opt .check { width: 14px; color: var(--primary-color); font-weight: 700; flex: none; }
  .opt-label { font-weight: 500; }
  .opt-sub, .opt-warn { font-size: 12px; color: var(--secondary-text-color); margin-left: 22px; line-height: 1.35; }
  .opt-warn { color: var(--warn-fg); }
  .tag { font-size: 11px; padding: 0 7px; border-radius: 9px; white-space: nowrap; color: var(--secondary-text-color);
    background: rgba(127, 127, 127, 0.14); }
  .tag.good { color: var(--add-fg); background: var(--add-bg); } .tag.bad { color: var(--warn-fg); background: rgba(201, 134, 26, 0.14); }
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
  @media (max-width: 500px) { .tb-btn.tb-text { font-size: 0; gap: 0; padding: 6px; } .tb-btn.tb-text svg { width: 22px; height: 22px; } }
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
