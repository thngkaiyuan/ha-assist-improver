// A stand-in Assist Improver behind the real panel, for trying the whole loop by hand (tools/ui/loop.py): find
// conversations, send them in, run, watch the run, review what it proposes, accept, and go again. State lives in the
// page; a run takes a few seconds and turns the conversations it took in that had friction (more than one turn, or a
// failed step) into improvements. No Home Assistant, no model: the panel's websocket commands, answered here.

window.loopSim = (seed) => {
  const now = () => new Date().toISOString();
  const S = {conversations: seed.conversations.map((c) => ({...c})), improvements: [...seed.improvements], runs: [],
             run: null, log: [], nextImp: 1, template: seed.review, decided: seed.decided,
             knowledge: ["Keep answers short."]};
  const said = (c) => c.transcript[0]?.you || "";
  const bare = (text) => text.replace(/[.?!]+$/, "");

  // What a run proposes for a conversation, by what went wrong in it (as the real improver would, roughly), taking
  // in the note it was sent in with; a revision takes the note asked with it in turn.
  const DOORS = ["switch.garage_door_left", "switch.garage_door_right"];
  const FIXES = [
    [/garage/i, {kind: "script", title: "Open both garage doors with one request", name: "Open both garage doors",
                 enabled: "Open both garage doors at once",
                 benefit: "Say “open the garage doors” and both open, first time.",
                 script: ["alias: Open both garage doors", "sequence:", "  - action: switch.turn_off", "    target:",
                          "      entity_id:", ...DOORS.map((d) => `        - ${d}`), "  - delay: 1",
                          "  - action: switch.turn_on", "    target:", "      entity_id:", ...DOORS.map((d) => `        - ${d}`)],
                 cause: "Your garage doors are switches that move on an off-then-on pulse. Assist only switched them on, so they didn't move, and it couldn't tell.",
                 physical: true}],
    [/\bcar\b/i, {kind: "instructions", title: "Get the car ready in one go",
                  line: "“Get the car ready” means: run the car's warm-up script.",
                  benefit: "Say “get the car ready” and it's warming up, no questions asked.",
                  cause: "Assist didn't know what getting the car ready means in this household, so it had to ask what to turn on."}],
    [/\bfans?\b/i, {kind: "instructions", title: "Know which fan you mean",
                     line: "“My fan” means the desk fan unless another room is named.",
                     benefit: "“My fan” is the desk fan, first time.",
                     cause: "Assist didn't know which fan “my fan” is, so it had to check before acting."}],
    [/\btv\b/i, {kind: "instructions", title: "Know which TV you mean",
                  line: "“My TV” means the den TV unless another room is named.",
                  benefit: "“My TV” is the den TV, first time.",
                  cause: "Assist turned off the wrong TV, then had to put it right."}],
    [/\blights?\b/i, {kind: "instructions", title: "Turn off only the lights",
                       line: "When asked to turn off the lights, turn off only lights: leave plugs, switches and garage doors as they are.",
                       benefit: "“Turn off all the lights” turns off only lights: the fridge and the garage doors stay as they are.",
                       cause: "Assist turned off everything in those rooms, not just the lights: the fridge's plug and the garage door switches too."}],
  ];
  function fix(c) {
    const found = FIXES.find(([pattern]) => pattern.test(said(c)));
    const f = found ? {...found[1]} : {kind: "instructions", title: `Get “${bare(said(c))}” right the first time`,
                                       line: "When a name matches one device, act on it.", cause: "Assist asked a follow-up question it didn't need."};
    if (c.note) f.cause = `${f.cause} You said: “${bare(c.note)}”.`;
    return f;
  }
  function revised(f, note) {
    // The note asked with a revision, taken in: the line says what the note says, or the script gains it.
    const words = bare(note.trim());
    if (f.kind === "script") {
      return {...f, script: [...f.script.slice(0, 1), `description: ${words}`, ...f.script.slice(1)],
              cause: `${f.cause} Revised: ${words}.`};
    }
    return {...f, line: `${f.line.replace(/\.$/, "")}. ${words.charAt(0).toUpperCase()}${words.slice(1)}.`,
            cause: `${f.cause} Revised: ${words}.`};
  }
  // A later turn that put something right, not a new request of its own.
  const CORRECTION = /\b(no|meant|didn'?t|don'?t|wrong|why|not|sorry|horrible|again|still|exactly|other)\b/i;
  const quote = (text) => text.length <= 70 ? bare(text) : `${text.slice(0, 70).replace(/\s+\S*$/, "")}…`;
  const taken = (c) => ["in", "improved"].includes(c.status);  // The improver has it, or an accepted fix covers it.
  // What a row can do (conversations.row): be sent in, unless the improver has it; be picked for a run, if new.
  const can = (c) => ({sendable: !taken(c), runnable: ["waiting", "sent"].includes(c.status)});

  // What Run now would move (Pipeline.preview): every new one, sent in or not; nothing is held here.
  const preview = () => {
    const admits = S.conversations.filter((c) => ["sent", "waiting"].includes(c.status)).length;
    return {stages: {"Your Assist": admits, Triage: 0, Analyze: 0, Propose: 0, "Write up": 0,
                     "Your review": S.improvements.filter((i) => i.status === "awaiting_review").length, "Your household": 0},
            held: {}, admits, releases: {}, which: [], quick: 0, analyses: admits, paused: [], blocked_models: {}, spend: {},
            running: S.run?.status === "running", applied: S.improvements.filter((i) => i.status === "applied").length};
  };
  const listed = (i) => ({id: i.id, title: i.title, status: i.status, pending: i.status === "awaiting_review",
                          enabled: i.fix.kind === "script" ? 1 : 0, removed: i.removed, spared: 0, followed: 0,
                          revision: !!i.revises});

  function startRun(msg) {
    // With nothing picked or sent in it still runs: a revision asked for is made all the same.
    if (S.run?.status === "running") throw {code: "busy", message: "A run is already in progress"};
    for (const id of msg.conversations || []) {
      const c = S.conversations.find((x) => x.id === id);
      if (c && !taken(c)) c.status = "sent";
    }
    const picked = msg.conversations;
    const took = S.conversations.filter((c) => picked ? picked.includes(c.id) && c.status === "sent"
      : ["sent", "waiting"].includes(c.status));
    const run = S.run = {id: `run${S.runs.length + 1}`, kind: "pipeline", trigger: "manual", status: "running", started: now(),
                         finished: null, stage: "triage", progress: {total: took.length, done: 0, taken_in: took.length},
                         error: null, only: null, ...(picked ? {picked} : {})};
    for (const c of took) c.status = "in";
    // Nothing to analyze (only revisions): straight on to Propose, which works from the revisions' conversations too.
    const analyzing = took.length ? 1500 : 0;
    const steps = [[analyzing, () => { run.stage = "analyze"; }],
                   [analyzing, () => { run.progress.analyzed = took.length; run.stage = "propose"; }],
                   [2000, () => { run.stage = "write"; run.progress.done = Math.max(0, took.length - 1); }],
                   [1500, () => finish(took)]];
    let delay = 0;
    for (const [wait, step] of steps) {
      delay += wait;
      setTimeout(() => { if (S.run === run && run.status === "running") step(); }, delay);  // Not once cancelled.
    }
    S.log.unshift({at: now(), kind: "run", status: "running", trigger: "manual", only: null, progress: {}, error: null});
    return {run: {...S.run}};
  }

  function propose(c, revises, previous) {
    // A new improvement for the conversation, or the revision of an earlier one (covering what it covered).
    const id = `imp${S.nextImp++}`, f = previous ? revised(previous.fix, revises.note) : fix(c);
    const removed = c.transcript.slice(1).filter((t) => CORRECTION.test(t.you)).length;
    const item = {id, title: f.title, status: "awaiting_review", removed, from: c.id, fix: f, revises,
                  covers: [...(previous?.covers || [c.id])]};
    S.improvements.unshift(item);
    S.log.unshift({at: now(), kind: "posted", id, title: f.title});
    return item;
  }

  function finish(took) {
    const friction = took.filter((c) => (c.turns || 0) > 1 || c.failed);
    const open = S.improvements.filter((i) => ["awaiting_review", "applied"].includes(i.status));
    const made = [];
    for (const item of S.improvements.filter((i) => i.revising)) {
      Object.assign(item, {status: "revised", revising: false});
      made.push(propose(S.conversations.find((c) => c.id === item.from), {id: item.id, title: item.title, note: item.note}, item));
    }
    for (const c of friction) {
      // One improvement per kind of fix: a second conversation with the same trouble is covered by the first.
      const same = [...made, ...open].find((i) => i.title === fix(c).title);
      if (!same) { made.push(propose(c)); continue; }
      same.covers.push(c.id);
      if (same.status === "applied") c.status = "improved";
    }
    Object.assign(S.run, {status: "done", stage: "write", finished: now(),
                          progress: {...S.run.progress, new_improvements: made.length}});
    ended();
  }

  function ended() {
    // The run, done or cancelled, kept and logged as it ended.
    S.runs.push({...S.run});
    const running = S.log.findIndex((e) => e.kind === "run" && e.status === "running");
    if (running >= 0) S.log[running] = {at: now(), kind: "run", status: S.run.status, trigger: "manual", only: null,
                                        progress: S.run.progress, error: null};
  }

  // When a query says (conversations.when, the same rules): a day, a part of the day, a time; small words beside them.
  const DAYS = {sunday: 0, mon: 1, monday: 1, tue: 2, tues: 2, tuesday: 2, wed: 3, wednesday: 3, thu: 4, thur: 4,
                thurs: 4, thursday: 4, fri: 5, friday: 5, sat: 6, saturday: 6};
  const PARTS = {morning: [5, 12], afternoon: [12, 18], evening: [17, 23], night: [20, 29], tonight: [17, 29]};
  const FILLER = new Set(["this", "last", "on", "at", "around", "about", "in", "the", "early", "late", "ish"]);
  function when(query) {
    const today = new Date(), dayOf = (d) => d.toDateString();
    let q = query.toLowerCase().replace(/(\d)\s+(am|pm|ish)\b/g, "$1$2").replace(/(\d{1,2})\s*o'?clock\b/g, "$1:00")
      .replace(/\blast night\b/g, "yesterday night").replace(/\bthis (morning|afternoon|evening)\b/g, "today $1");
    const days = [], parts = [], times = [], rest = [];
    for (const word of q.split(/\s+/).filter(Boolean)) {
      const t = word.match(/^(\d{1,2})(?:[:.](\d{2}))?(am|pm)?(ish)?$/);
      if (word in DAYS) days.push((d) => d.getDay() === DAYS[word]);
      else if (["today", "tonight", "yesterday"].includes(word)) {
        const back = word === "yesterday" ? 1 : 0;
        days.push((d) => dayOf(d) === dayOf(new Date(today.getTime() - back * 86400000)));
        if (word === "tonight") parts.push(PARTS.tonight);
      } else if (word in PARTS) parts.push(PARTS[word]);
      else if (t && (t[2] || t[3] || t[4]) && Number(t[1]) <= 23) {
        const hour = Number(t[1]), minute = Number(t[2] || 0);
        const hours = t[3] ? [hour % 12 + (t[3] === "pm" ? 12 : 0)]
          : hour >= 1 && hour <= 12 && !t[1].startsWith("0") ? [hour % 12, hour % 12 + 12] : [hour];
        for (const h of hours) times.push([h * 60 + minute, t[2] ? 45 : 60]);
      } else rest.push(word);
    }
    if (!days.length && !parts.length && !times.length) return [null, rest];
    const test = (iso) => {
      const at = new Date(iso), late = at.getHours() + (at.getHours() < 5 ? 24 : 0);
      const night = parts.length && parts.every(([low]) => low >= 17) && at.getHours() < 5;
      const day = night ? new Date(at.getTime() - 86400000) : at, clock = at.getHours() * 60 + at.getMinutes();
      return days.every((check) => check(day)) && (!parts.length || parts.some(([low, high]) => low <= late && late < high))
        && (!times.length || times.some(([m, spread]) => Math.min(Math.abs(clock - m), 1440 - Math.abs(clock - m)) <= spread));
    };
    const off = (clock, m) => Math.min(Math.abs(clock - m), 1440 - Math.abs(clock - m));
    test.off = times.length ? (iso) => { const at = new Date(iso);
      return Math.min(...times.map(([m]) => off(at.getHours() * 60 + at.getMinutes(), m))); } : null;
    return [test, rest.filter((w) => !FILLER.has(w))];
  }
  function snippet(c, words) {
    const shown = (c.transcript[0]?.you || "").slice(0, 160).toLowerCase();
    const unseen = words.filter((w) => !shown.includes(w));
    for (const text of c.transcript.flatMap((t) => [t.you, t.assist])) {
      const found = unseen.map((w) => text.toLowerCase().indexOf(w)).filter((i) => i >= 0).sort((a, b) => a - b);
      if (!found.length) continue;
      const start = Math.max(0, found[0] - 50);
      return (start ? "…" : "") + text.slice(start, start + 160).trim() + (start + 160 < text.length ? "…" : "");
    }
    return null;
  }

  function conversations(msg, plain = false) {
    const [at, words] = plain ? [null, (msg.query || "").toLowerCase().split(/\s+/).filter(Boolean)] : when(msg.query || "");
    const since = msg.days ? Date.now() - msg.days * 86400000 : 0;
    const text = (c) => c.transcript.map((t) => t.you + "\n" + t.assist).join("\n").toLowerCase();
    const matched = (c) => words.filter((w) => text(c).includes(w)).length;
    let found = S.conversations.filter((c) => (!since || Date.parse(c.started) >= since) && (!at || at(c.started))
                                              && (!words.length || matched(c)));
    if (at && !found.length) return conversations(msg, true);
    const close = words.length > 0 && !found.some((c) => matched(c) === words.length);
    if (words.length && !close) found = found.filter((c) => matched(c) === words.length);
    const by = {newest: (a, b) => b.started.localeCompare(a.started), oldest: (a, b) => a.started.localeCompare(b.started),
                effort: (a, b) => (b.turns - a.turns) || (b.failed - a.failed) || b.started.localeCompare(a.started)};
    found = found.sort(by[msg.order || "newest"]);
    const closest = !!at?.off && !msg.order;  // A time said and no order chosen: the closest first.
    if (closest) found = found.sort((a, b) => at.off(a.started) - at.off(b.started));
    if (close) found = found.sort((a, b) => matched(b) - matched(a));
    const offset = msg.offset || 0;
    return {conversations: found.slice(offset, offset + 30).map((c) => {
              const {transcript, ...row} = c;
              return {...row, ...can(c), said: transcript[0]?.you || "", reply: transcript[0]?.assist || "",
                      match: words.length ? snippet(c, words) : null, timed: !!at};
            }),
            every: found.map((c) => ({id: c.id, status: c.status, hidden: false, runnable: can(c).runnable})),
            total: found.length, close, closest};
  }

  function changes(item) {
    // The change as a diff: a script's lines, or the household knowledge as it stands now with the new line.
    const f = item.fix;
    if (f.kind === "script") {
      return [{name: f.name, kind: "script", added: f.script.length, removed: 0,
               rows: f.script.map((text, n) => ({sign: "+", old: null, new: n + 1, text}))}];
    }
    const base = S.knowledge.filter((line) => line !== f.line);
    return [{name: "Household knowledge", kind: "instructions", added: 1, removed: 0,
             rows: [...base.map((text, n) => ({sign: " ", old: n + 1, new: n + 1, text})),
                    {sign: "+", old: null, new: base.length + 1, text: f.line}]}];
  }

  function review(id) {
    const item = S.improvements.find((i) => i.id === id);
    if (!item) throw {code: "not_found", message: "Not found"};
    const c = S.conversations.find((x) => x.id === item.from), f = item.fix;
    // Turns no longer needed, guessed from the later turns' words (the real review gets them from its replays).
    const later = c ? c.transcript.slice(1).filter((t) => CORRECTION.test(t.you)).length : 0;
    const next = S.improvements.find((i) => i.status === "awaiting_review" && i.id !== id);
    const pending = item.status === "awaiting_review";
    return {...S.template, id, title: item.title, status: item.status, pending,
            benefit: f.benefit || (c ? `Say “${quote(said(c))}” once, and it's done.` : null), cause: f.cause, physical: !!f.physical,
            enabled: f.kind === "script" ? 1 : 0, removed: later, changes: changes(item),
            safety: [S.template.safety[0], f.kind === "script" ? "Only settings change: 1 new script. Nothing runs when you accept."
              : "Only settings change: 1 instruction change. Nothing runs when you accept.", S.template.safety[2]],
            tests: c ? [{summary: `“${quote(said(c))}”, replayed`, improved: true,
                         result: later ? `${c.turns} → ${c.turns - later} turns` : "improved"}] : [],
            revises: item.revises || null, next_pending: next ? {id: next.id, title: next.title} : null,
            choices: pending ? S.template.choices : item.status === "applied" ? [{value: "undo", label: "Undo changes"}]
              : item.status === "undone" ? [{value: "accept", label: "Accept changes"}] : []};
  }

  const handlers = {
    improvements: () => ({improvements: S.improvements.map(listed)}),
    household: () => seed.household,
    settings: () => ({...seed.settings}),
    run: () => ({run: S.run ? {...S.run} : null, preview: preview()}),
    log: () => ({events: S.log.slice(0, 50)}),
    conversations,
    conversation: (msg) => {
      const c = S.conversations.find((x) => x.id === msg.recording_id);
      if (!c) throw {code: "not_found", message: "That conversation isn't kept."};
      return {id: c.id, started: c.started, status: c.status, ...can(c), turns: c.transcript, note: c.note || null};
    },
    "conversation/send": (msg) => {
      const c = S.conversations.find((x) => x.id === msg.recording_id);
      if (!c) throw {code: "not_found", message: "That conversation isn't kept."};
      if (taken(c)) throw {code: "already_in", message: "The improver already has that conversation."};
      Object.assign(c, {status: "sent", note: msg.note || null});
      if (S.run?.status !== "running") startRun({picked: true});
      return {status: c.status};
    },
    "run/start": startRun,
    "run/cancel": () => {
      if (S.run?.status === "running") { Object.assign(S.run, {status: "cancelled", finished: now()}); ended(); }
      return {cancelled: true};
    },
    improvement: (msg) => review(msg.improvement_id),
    decide: (msg) => {
      const item = S.improvements.find((i) => i.id === msg.improvement_id);
      if (msg.decision === "revise" && !(msg.note || "").trim()) throw {code: "invalid", message: "Say what to change in the note"};
      // A revision leaves it up for review until the revised version replaces it.
      const status = {accept: "applied", decline: "declined", revise: item.status, undo: "undone"}[msg.decision] || "declined";
      Object.assign(item, {status, note: msg.note || "", revising: msg.decision === "revise"});
      // The conversations it covers stand improved while it's applied: never sent in or taken in again.
      const covered = S.conversations.filter((c) => (item.covers || [item.from]).includes(c.id));
      if (msg.decision === "accept") {
        if (item.fix.line) S.knowledge.push(item.fix.line);
        for (const c of covered) c.status = "improved";
      } else if (msg.decision === "undo") {
        S.knowledge = S.knowledge.filter((line) => line !== item.fix.line);
        const still = S.improvements.filter((i) => i.status === "applied").flatMap((i) => i.covers || [i.from]);
        for (const c of covered) if (!still.includes(c.id)) c.status = "in";
      }
      S.log.unshift({at: now(), kind: "decision", id: item.id, title: item.title, decision: msg.decision,
                     reason: msg.reason || null, outcome: null, auto: false, by: "You"});
      const result = {accept: item.fix.kind === "script" ? S.decided.result : "Applied. Assist uses it from your next request.", undo: "Undone: what was there before is back.",
                      revise: "Thanks. The improver is revising it now.",
                      decline: "Thanks. Declined, and its conversation is archived."}[msg.decision] || "Thanks, your decision is recorded.";
      return {status, ok: true, result, next_pending: review(item.id).next_pending};
    },
    "settings/set": () => ({...seed.settings}),
  };

  return async (msg) => {
    const name = msg.type.replace(/^assist_improver\//, "");
    const handler = handlers[name];
    if (!handler) throw {code: "unknown", message: `The stand-in doesn't do ${name}`};
    await new Promise((r) => setTimeout(r, 120));  // A little latency, as over a network.
    return JSON.parse(JSON.stringify(handler(msg)));
  };
};
