// The panel's styles: Home Assistant's theme variables, with the panel's own tints on top.

export const STYLE = `
  :host { display: block; min-height: 100vh; background: var(--primary-background-color); color: var(--primary-text-color);
    font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
    --add-bg: rgba(46, 160, 67, 0.15); --add-num: rgba(46, 160, 67, 0.30); --add-fg: #1a7f37;
    --del-bg: rgba(248, 81, 73, 0.15); --del-num: rgba(248, 81, 73, 0.30); --del-fg: #cf222e; --warn-fg: #946100; }
  /* Text in the panel's own tints reads at 3:1 or better: deeper tones on a light theme, the brighter ones on a dark. */
  :host(.dark) { --add-fg: #2da44e; --del-fg: #e5534b; --warn-fg: #c9861a; }
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
  .conflict { background: var(--del-bg); border: 1px solid var(--del-fg); border-radius: 8px; padding: 10px 14px; margin-bottom: 12px;
    overflow-wrap: anywhere; }
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
  .confirm .c-warn { color: var(--warn-fg); font-size: 13px; }
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
  .kn-empty { margin: 0; padding: 14px 20px 0; font-size: 14px; line-height: 1.45; color: var(--secondary-text-color); }
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
