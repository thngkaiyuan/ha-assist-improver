// The settings view's pipeline, drawn as a track: requests merge onto the improvement loop (analyze, propose, test,
// write up), a small loop joins the proposer and its reviewer, and you review at the far end; revisions go round
// again and what you accept peels off to the household. layout() says where each step's card sits and the track runs
// (left to right when wide, top to bottom when narrow); trackSvg() draws the track under the cards.

export function layout(W, sizes, heights) {
  return W < 820 ? layoutVertical(W, heights) : layoutWide(W, sizes);
}

function layoutWide(W, sizes) {
  // Your Assist and triage on the intake, the loop's steps along its top, your review at its far end, and your
  // household below that, where the track peels off. Each card is as big as its content (measured after it's drawn,
  // see the settings view's measure()); the layout spaces them by those sizes, and turns vertical when they don't fit.
  const size = (id) => sizes?.[id] || (["you", "household"].includes(id) ? {w: 140, h: 42} : {w: id === "requests" ? 150 : 120, h: 68});
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
  if (fixed + span(tight) > W) return layoutVertical(W);
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

function layoutVertical(W, heights) {
  // On a phone: one wide band straight down through the steps; the proposer and reviewer go back and forth along the
  // left edge, and a revision goes back up the right edge to the proposer.
  // The side lanes run midway between the panel's edge (its 16px padding outside the track) and the cards' sides.
  // Each card is one line high (min), or as high as its content when that takes two lines (its title, then its
  // controls): heights, measured once it's drawn (see the settings view's measure()).
  const g = 14, h = 52, hs = 46, pad = 16, cx = 32, cw = W - 2 * cx, eL = (cx - pad) / 2, eR = W - eL;
  const at = {}, place = (id, gap, inset = 0, min = h) => {
    const height = Math.max(min, heights?.[id] || 0);
    at[id] = {x: cx + inset, y, w: cw - 2 * inset, h: height, min}; y += height + gap;
  };
  let y = 0;
  place("requests", g); place("triage", g); place("analyze", g); place("propose", 24); place("review", 24, 0, hs);
  place("test", g); place("write", g); place("you", g); place("household", 0);
  const mid = cx + cw / 2, centre = (id) => at[id].y + at[id].h / 2, end = at.household.y + at.household.h;
  const pc = centre("propose"), rc = centre("review"), yc = centre("you");
  const side = (x, from, to, inner) => {  // A return lane: out of one card's edge, up the side, into another's.
    const s = Math.sign(inner - x), r = 12;
    return `M${inner},${from}H${x + s * r}Q${x},${from} ${x},${from - r}V${to + r}Q${x},${to} ${x + s * r},${to}H${inner}`;
  };
  return {W, H: end + 4, at, vertical: true,
    paths: [{d: `M${mid},${at.requests.y + 10}V${end - 10}`, w: Math.min(150, cw * 0.44), cap: "butt"},
            {d: side(eL, rc, pc, at.review.x + 6), w: 18}, {d: side(eR, yc, pc, cx + cw - 6), w: 18}],
    flow: `M${mid},${centre("requests")}V${centre("household")}`,
    label: [eR, (pc + yc) / 2], labelVertical: true,
    chevrons: [[mid, (at.propose.y + at.propose.h + at.review.y) / 2, "down"], [eL, (pc + rc) / 2, "up"],
               [eR, pc + (yc - pc) * 0.22, "up"], [eR, pc + (yc - pc) * 0.78, "up"]]};
}

export function trackSvg(L, running) {
  // One translucent layer of solid strokes (overlaps don't darken), colors blending along it; while a run is in
  // progress, dashes flow round the loop.
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
