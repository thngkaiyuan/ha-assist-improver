// The settings view's pickers, each under the chip that opened it (a sheet from the bottom on a phone): a step's AI
// model, ranked by fit, with a new AI Task or the recommended setup a click away; Simulate's tests and feedback
// check; and the assistant Your Assist uses.

const v = new URL(import.meta.url).search;  // ?v=<version>, passed on to every module this one loads.
const [{esc, fill}, {TEXT, STEPS}, {switchRow}] = await Promise.all(
  ["format", "text", "common"].map((name) => import(`./${name}.js${v}`)));

export const task = (s, entityId) => s.ai_tasks.find((t) => t.entity_id === entityId);

function options(s, role, roleName) {
  // The AI Tasks, best fit first; a quick step's menu ends with its way to go without one.
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
    (role.optional ? `<div class="opt" data-pick="" data-tip="${esc(roleName === "triage" ? TEXT.passThrough.tip : TEXT.feedbackOff)}"><div class="opt-main"><span class="check">${role.entity_id ? "" : "&#10003;"}</span>
      <span class="opt-label">${esc(roleName === "triage" ? TEXT.passThrough.label : "Off")}</span></div></div>` : "");
}

function privacy(s) {
  // What's kept: recording for the household, and for each of its people (an admin sets each; they flip together).
  const P = TEXT.privacy, people = s.recording_people || [];
  return `<div class="pop-privacy">${switchRow("recording", s.recording !== false, P.recording, P.recordingTip, "pop-toggle")}
    ${people.length ? `<div class="pop-people" role="group" aria-label="${esc(P.people)}"><div class="pop-sub">${esc(P.people)}</div>${
      people.map((x, n) => switchRow(`recording-person-${n}`, x.on, x.name, "", "pop-toggle",
                                     `data-recording-person="${esc(x.user_id)}"`)).join("")}</div>` : ""}</div>`;
}

const PICKERS = {
  assist(p, s) {
    const list = p.household?.pipelines || [];
    return `<div class="pop-head"><b>Your Assist</b> uses</div>` + (list.length ? list.map((x) =>
      `<div class="opt" data-prefer="${esc(x.id)}"><div class="opt-main"><span class="check">${x.preferred ? "&#10003;" : ""}</span>
        <span class="opt-label">${esc(x.name)}</span></div></div>`).join("")
      : `<div class="pop-lead">${esc(TEXT.assistant.none)}</div>`) +
      privacy(s);
  },
  test(p, s) {
    const n = s.tests_per_goal;
    return `<div class="pop-head"><b>Simulate</b> on your assistant</div>
      <div class="stepper-row" data-tip="${esc(fill(TEXT.simulate.cost, {n}))}">
      <button class="round" id="t-minus" ${n <= 1 ? "disabled" : ""}>&#8722;</button>
      <span class="t-n">${n}</span><button class="round" id="t-plus" ${n >= 20 ? "disabled" : ""}>+</button></div>
      <div class="pop-lead" style="text-align:center">${esc(TEXT.simulate.per)}</div>
      <div class="pop-head" data-tip="${esc(TEXT.feedbackOff)} when off">Check feedback with</div>${options(s, s.roles.feedback_check, "feedback_check")}`;
  },
  create(p, s, pop, step) {
    // A new AI Task for this step, added to an AI provider with the model the step needs suggested.
    const providers = s.providers, provider = providers.find((x) => x.entry_id === pop.entry_id), models = pop.models || [];
    return `<div class="pop-head">New AI Task for <b>${esc(step.title)}</b></div>` + (!providers.length
      ? `<div class="pop-lead">Add an AI provider (such as Anthropic or OpenRouter) in Settings &#8594; Devices &amp; services first.</div>`
      : `<div class="form">
        ${providers.length > 1 ? `<label>Provider<select id="c-provider">${providers.map((x) =>
          `<option value="${esc(x.entry_id)}" ${x.entry_id === pop.entry_id ? "selected" : ""}>${esc(x.title)}</option>`).join("")}</select></label>` : ""}
        ${pop.loading ? `<div class="pop-lead" style="padding:0">Loading models…</div>` : models.length
          ? `<label>Model<select id="c-model">${models.map((m) =>
              `<option value="${esc(m.value)}" ${m.value === pop.model ? "selected" : ""}>${esc(m.label)}</option>`).join("")}</select></label>`
          : `<label>Model<input id="c-model" value="${esc(pop.model || "")}" placeholder="The model's ID"></label>`}
        ${pop.named ? `<label>Name<input id="c-name" value="${esc(pop.name || "")}"></label>` : ""}
        ${pop.error ? `<div class="error-text">${esc(pop.error)}</div>` : ""}
        <div class="actions"><button class="btn ghost" id="c-back">Back</button>
        <button class="btn" id="c-create" data-tip="Added to ${esc(provider?.title || "the provider")} with its default settings for the rest" ${pop.loading || pop.busy ? "disabled" : ""}>${pop.busy ? "Adding…" : "Add and use"}</button></div></div>`);
  },
  role(p, s, pop, step) {
    const role = s.roles[pop.role];
    return `<div class="pop-head" data-tip="${esc(step.tip)} ${esc(TEXT.models[role.size])}"><b>${esc(step.title)}</b> uses</div>
      ${options(s, role, pop.role)}<div class="opt new" id="new-task">+ New AI Task…</div>
      ${s.recommended === false ? `<div class="opt new recommended" id="use-recommended" data-tip="${esc(TEXT.models.recommendedTip)}">${
        pop.busy ? esc(TEXT.models.recommendedBusy) : `&#10022; ${esc(TEXT.models.recommended)}`}</div>` : ""}
      ${pop.error ? `<div class="pop-lead error-text">${esc(pop.error)}</div>` : ""}`;
  },
};

export function render(p, s) {
  const pop = p.pop;
  if (!pop) return "";
  const step = Object.values(STEPS).find((x) => x.role === pop.role) || STEPS.test;
  const sheet = window.innerWidth < 600;  // A sheet from the bottom on a phone.
  return `<div class="backdrop" id="backdrop"></div><div class="pop ${sheet ? "sheet" : ""}" id="pop">${PICKERS[pop.kind](p, s, pop, step)}</div>`;
}

export function place(p) {
  // Under the control that opened it, kept on screen.
  const pop = p.root.getElementById("pop");
  if (!pop || pop.classList.contains("sheet")) return;
  const anchor = p.root.querySelector(p.pop.anchor);
  if (!anchor) return;
  const a = anchor.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
  const left = Math.min(Math.max(8, a.left), window.innerWidth - w - 8);
  const top = a.bottom + 6 + h > window.innerHeight - 8 ? Math.max(8, a.top - h - 6) : a.bottom + 6;
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

const open = (p, pop) => { p.pop = pop; p.render(); };

async function loadModels(p, entryId) {
  const pop = p.pop, role = p.settings.roles[pop.role];
  Object.assign(pop, {entry_id: entryId, loading: true, models: [], error: null});
  p.render();
  try {
    const found = await p.ws("settings/models", {entry_id: entryId});
    const model = found.suggested?.[role.size] || found.default || found.models[0]?.value || "";
    Object.assign(pop, {models: found.models, named: found.named, model, loading: false, nameEdited: false});
    suggestName(pop);
  } catch (err) {
    Object.assign(pop, {loading: false, error: err.message || String(err)});
  }
  p.render();
}

function suggestName(pop) {
  if (pop.nameEdited) return;
  const m = (pop.models || []).find((o) => o.value === pop.model);
  pop.name = `Assist Improver (${m?.label || pop.model})`;
}

async function busy(p, call, done) {
  // Sends a picker's request, saying so on its button; what comes back closes the picker, or says what went wrong.
  const pop = p.pop;
  if (pop.busy) return;
  pop.busy = true; pop.error = null;
  p.render();
  try {
    done(await call());
  } catch (err) {
    pop.busy = false; pop.error = err.message || String(err);
  }
  p.render();
}

export const actions = {
  click: [
    ["[data-role]", (p, el) => open(p, {kind: "role", role: el.dataset.role, anchor: `[data-role="${el.dataset.role}"]`})],
    ["[data-knob]", (p) => open(p, {kind: "test", role: "feedback_check", anchor: "[data-knob]"})],
    ["[data-assist]", (p) => open(p, {kind: "assist", anchor: "[data-assist]"})],
    ["#backdrop", (p) => { p.pop = null; p.render(); }],
    ["[data-pick]", (p, el) => {
      const role = p.pop.role;
      p.pop = null;
      p.saveSetting({role, entity_id: el.dataset.pick || null});
    }],
    ["[data-prefer]", async (p, el) => {
      try { await p.ws("assistant/prefer", {pipeline_id: el.dataset.prefer}); } catch (err) { alert(err.message || String(err)); }
      p.pop = null;
      await p.reloadHousehold();
    }],
    ["#t-minus", (p) => p.saveSetting({tests_per_goal: p.settings.tests_per_goal - 1})],
    ["#t-plus", (p) => p.saveSetting({tests_per_goal: p.settings.tests_per_goal + 1})],
    ["#use-recommended", (p) => busy(p, () => p.ws("settings/recommended"), (result) => {
      p.settings = result;
      if (result.notes?.length) Object.assign(p.pop, {busy: false, error: result.notes.join(" ")});
      else p.pop = null;
    })],
    ["#new-task", (p) => {
      p.pop = {...p.pop, kind: "create"};
      const first = p.settings.providers[0];
      if (first) loadModels(p, first.entry_id); else p.render();
    }],
    ["#c-back", (p) => { p.pop = {...p.pop, kind: "role", error: null}; p.render(); }],
    ["#c-create", (p) => {
      const pop = p.pop;
      pop.model = p.root.getElementById("c-model")?.value ?? pop.model;  // As typed, even before the field lets go.
      if (!pop.model) { pop.error = "Choose a model."; p.render(); return; }
      busy(p, () => p.ws("settings/create", {entry_id: pop.entry_id, model: pop.model, role: pop.role,
                                              ...(pop.named && pop.name ? {name: pop.name} : {})}),
           (settings) => { p.settings = settings; p.pop = null; });
    }],
  ],
  change: [
    ["#c-provider", (p, el) => loadModels(p, el.value)],
    ["#c-model", (p, el) => {
      p.pop.model = el.value;
      suggestName(p.pop);
      if (el.tagName === "SELECT") p.render();  // The name follows the model chosen.
    }],
  ],
  input: [
    ["#c-model", (p, el) => { p.pop.model = el.value; }],
    ["#c-name", (p, el) => { p.pop.name = el.value; p.pop.nameEdited = true; }],
  ],
};
