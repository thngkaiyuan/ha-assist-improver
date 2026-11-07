"""SVG frames: a relaxed driver, a sudden scare, a hard swerve, then relief. The README's assets/intervention.gif:

  PLAYWRIGHT_BROWSERS_PATH=tools/ui/browsers uv run --managed-python --with playwright==1.64.1 --with pillow \
    tools/art/intervention_scene.py assets/intervention.gif
"""
import math

W, H = 640, 360
SKIN, HAIR, SHIRT, CAR, DARK = "#F2C29B", "#3B2A20", "#3DBE6A", "#039BE5", "#26303B"
FPS = 12
N = 52

def ease(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)

def phase(f):
    if f < 18: return "relaxed", f / 18
    if f < 21: return "shock", (f - 18) / 3
    if f < 36: return "swerve", (f - 21) / 15
    if f < 42: return "recover", (f - 36) / 6
    return "relief", (f - 42) / 10

def wheel_point(cx, cy, r, ang):
    a = math.radians(ang)
    return cx + r * math.cos(a), cy + 0.37 * r * math.sin(a)

def frame(f):
    ph, t = phase(f)
    wheel, tilt, shake = 0.0, 0.0, 0.0
    if ph == "swerve":
        wheel = -75 * ease(t / 0.25)
        tilt = -9 * ease(t / 0.35)
        shake = 3 * math.sin(f * 2.3)
    elif ph == "recover":
        wheel = -75 * (1 - ease(t)); tilt = -9 * (1 - ease(t)); shake = 1.5 * math.sin(f * 2.3)
    elif ph == "shock":
        shake = 2 * math.sin(f * 3)
    hx, hy = 320 + shake, 140 + (2 * math.sin(f / 3) if ph == "relaxed" else 0)
    p = []
    # Sky, road and speed lines.
    # The car drives toward us: the road recedes to a vanishing point behind it.
    vx, vy = 320 - tilt * 14, 120
    p.append(f'<rect width="{W}" height="{H}" fill="url(#sky)"/>')
    p.append(f'<rect y="{vy}" width="{W}" height="{H - vy}" fill="#9CCC65"/>')
    p.append(f'<path d="M{vx-18:.1f} {vy} L{vx+18:.1f} {vy} L{W+260} {H} L-260 {H}Z" fill="#8A949E"/>')
    for edge in (-1.3, 1.3):  # Solid road edges.
        p.append(f'<path d="M{vx + edge * 12:.1f} {vy} L{vx + edge * 600:.1f} {H}" stroke="#F4F6F8" stroke-width="6"/>')
    for lane in (-0.5, 0.5):  # Dashed lines between three lanes, passing under the car toward the horizon.
        for i in range(5):
            s0 = ((i / 5 - f / 26) % 1.0) ** 2
            s1 = min(1.0, s0 + 0.05 + 0.1 * s0)
            def pt(s, lane=lane):
                return vx + (lane * 600) * s, vy + (H - vy) * s
            (x0, y0), (x1, y1) = pt(s0), pt(s1)
            w0, w1 = 1 + 9 * s0, 1 + 9 * s1
            p.append(f'<path d="M{x0-w0/2:.1f} {y0:.1f} L{x0+w0/2:.1f} {y0:.1f} L{x1+w1/2:.1f} {y1:.1f} L{x1-w1/2:.1f} {y1:.1f}Z" fill="#F4F6F8"/>')
    car = []
    # Car body and windshield.
    car.append('<path d="M110 250 Q120 190 150 70 Q160 50 185 50 L455 50 Q480 50 490 70 Q520 190 530 250 Z" fill="#0277BD"/>')
    car.append('<path d="M165 245 L180 78 Q184 66 198 66 L442 66 Q456 66 460 78 L475 245 Z" fill="#33414F"/>')
    car.append('<rect x="225" y="88" width="190" height="150" rx="40" fill="#46566A"/>')  # Seat.
    # Arms behind the head when relaxed.
    ls, rs = (hx - 38, 196), (hx + 38, 196)
    if ph == "relaxed":
        for s, e, h in ((ls, (hx - 78, 118), (hx - 24, 122)), (rs, (hx + 78, 118), (hx + 24, 122))):
            car.append(f'<path d="M{s[0]:.1f} {s[1]} L{e[0]:.1f} {e[1]} L{h[0]:.1f} {h[1]}" stroke="{SHIRT}" stroke-width="20" fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
    # Torso and head.
    car.append(f'<path d="M{hx-52:.1f} 250 Q{hx-50:.1f} 182 {hx:.1f} 180 Q{hx+50:.1f} 182 {hx+52:.1f} 250Z" fill="{SHIRT}"/>')
    car.append(f'<rect x="{hx-10:.1f}" y="164" width="20" height="22" fill="{SKIN}"/>')
    car.append(f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="36" fill="{SKIN}"/>')
    scared = ph in ("shock", "swerve", "recover")
    if scared:  # Hair on end.
        spikes = " ".join(f"L{hx-30+i*10:.1f} {hy-58 - (10 if i % 2 else 0):.1f} L{hx-25+i*10:.1f} {hy-30:.1f}" for i in range(6))
        car.append(f'<path d="M{hx-34:.1f} {hy-22:.1f} {spikes} L{hx+34:.1f} {hy-22:.1f} Z" fill="{HAIR}"/>')
    else:
        car.append(f'<path d="M{hx-36:.1f} {hy-6:.1f} Q{hx-34:.1f} {hy-44:.1f} {hx:.1f} {hy-42:.1f} Q{hx+34:.1f} {hy-44:.1f} {hx+36:.1f} {hy-6:.1f} Q{hx+20:.1f} {hy-26:.1f} {hx:.1f} {hy-24:.1f} Q{hx-20:.1f} {hy-26:.1f} {hx-36:.1f} {hy-6:.1f}Z" fill="{HAIR}"/>')
    # Face.
    if ph == "relaxed":
        for dx in (-13, 13):
            car.append(f'<path d="M{hx+dx-7:.1f} {hy-2:.1f} Q{hx+dx:.1f} {hy-8:.1f} {hx+dx+7:.1f} {hy-2:.1f}" stroke="{DARK}" stroke-width="3" fill="none" stroke-linecap="round"/>')
        car.append(f'<path d="M{hx-11:.1f} {hy+14:.1f} Q{hx:.1f} {hy+22:.1f} {hx+11:.1f} {hy+14:.1f}" stroke="{DARK}" stroke-width="3" fill="none" stroke-linecap="round"/>')
    elif scared:
        for dx in (-13, 13):
            car.append(f'<circle cx="{hx+dx:.1f}" cy="{hy-4:.1f}" r="10" fill="white" stroke="{DARK}" stroke-width="2"/><circle cx="{hx+dx:.1f}" cy="{hy-4:.1f}" r="3" fill="{DARK}"/>')
            car.append(f'<path d="M{hx+dx-9:.1f} {hy-22:.1f} L{hx+dx+9:.1f} {hy-25:.1f}" stroke="{HAIR}" stroke-width="3" stroke-linecap="round"/>')
        mouth = 15 if ph != "recover" else 15 - 7 * ease(t)
        car.append(f'<ellipse cx="{hx:.1f}" cy="{hy+18:.1f}" rx="11" ry="{mouth:.1f}" fill="#7A1F2B"/><ellipse cx="{hx:.1f}" cy="{hy+18+mouth*0.5:.1f}" rx="7" ry="{mouth*0.35:.1f}" fill="#E06C75"/>')
    else:  # Relief: awake, supervising, sweating.
        for dx in (-13, 13):
            car.append(f'<circle cx="{hx+dx:.1f}" cy="{hy-4:.1f}" r="7" fill="white" stroke="{DARK}" stroke-width="2"/><circle cx="{hx+dx:.1f}" cy="{hy-3:.1f}" r="3" fill="{DARK}"/>')
        car.append(f'<path d="M{hx-10:.1f} {hy+18:.1f} Q{hx:.1f} {hy+12:.1f} {hx+10:.1f} {hy+18:.1f}" stroke="{DARK}" stroke-width="3" fill="none" stroke-linecap="round"/>')
        car.append(f'<path d="M{hx+34:.1f} {hy-22:.1f} q6 10 0 14 q-6 -4 0 -14z" fill="#7FD3FF"/>')
    # Steering wheel.
    cx, cy, r = 320, 236, 58
    car.append(f'<g transform="translate({cx} {cy}) scale(1 0.37) rotate({wheel:.1f})"><circle r="{r}" fill="none" stroke="#151B22" stroke-width="12"/>'
               f'<path d="M-{r} 0 L{r} 0 M0 0 L0 {r}" stroke="#151B22" stroke-width="9"/><circle r="12" fill="#151B22"/></g>')
    # Arms reaching or on the wheel.
    if ph != "relaxed":
        grip = ease(t) if ph == "shock" else 1.0
        targets = [wheel_point(cx, cy, r, 200 + wheel), wheel_point(cx, cy, r, -20 + wheel)]
        raised = [(hx - 46, 150), (hx + 46, 150)]
        for s, tgt, up in zip((ls, rs), targets, raised):
            hxp = up[0] + (tgt[0] - up[0]) * grip; hyp = up[1] + (tgt[1] - up[1]) * grip
            ex, ey = (s[0] + hxp) / 2 + (-18 if s is ls else 18), (s[1] + hyp) / 2 + 8
            car.append(f'<path d="M{s[0]:.1f} {s[1]} Q{ex:.1f} {ey:.1f} {hxp:.1f} {hyp:.1f}" stroke="{SHIRT}" stroke-width="20" fill="none" stroke-linecap="round"/>')
            car.append(f'<circle cx="{hxp:.1f}" cy="{hyp:.1f}" r="11" fill="{SKIN}"/>')
    # Hood and headlights.
    car.append('<path d="M100 248 L540 248 L560 300 Q560 316 540 316 L100 316 Q80 316 80 300 Z" fill="#039BE5"/>')
    car.append('<ellipse cx="140" cy="284" rx="26" ry="14" fill="#FFF3B0"/><ellipse cx="500" cy="284" rx="26" ry="14" fill="#FFF3B0"/>')
    car.append('<rect x="250" y="276" width="140" height="22" rx="8" fill="#0277BD"/>')
    p.append(f'<g transform="rotate({tilt:.1f} 320 300) translate({tilt*-3:.1f} 0)">{"".join(car)}</g>')
    # Effects.
    if ph == "relaxed":
        y = 100 - (f % 9) * 6
        p.append(f'<text x="{hx+60:.1f}" y="{y}" font-size="30" fill="#3DBE6A" font-family="Helvetica" opacity="{1 - (f % 9) / 9:.2f}">♪</text>')
    if ph in ("shock", "swerve"):
        p.append(f'<text x="{hx+48:.1f}" y="{hy-48:.1f}" font-size="44" font-weight="900" fill="#E53935" font-family="Helvetica">!</text>')
    if ph == "swerve":
        p.append(f'<g transform="translate({hx-210:.1f} 70) rotate(-6)"><rect width="150" height="52" rx="26" fill="white" stroke="{DARK}" stroke-width="3"/>'
                 f'<text x="75" y="35" text-anchor="middle" font-size="26" font-weight="900" fill="#E53935" font-family="Helvetica">AHHHH!</text></g>')
        for i in range(3):
            o = (f * 7 + i * 20) % 60
            p.append(f'<circle cx="{560 + o:.1f}" cy="{322 - i * 8:.1f}" r="{12 + o / 4:.1f}" fill="#E9ECEF" opacity="{0.8 - o / 80:.2f}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
            f'<defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#BDE7FF"/><stop offset="1" stop-color="#EAF7FF"/></linearGradient></defs>'
            + "".join(p) + "</svg>")


if __name__ == "__main__":
    import io
    import sys
    from PIL import Image
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": W, "height": H})
        frames = []
        for f in range(N):
            pg.set_content(f'<body style="margin:0">{frame(f)}</body>')
            frames.append(Image.open(io.BytesIO(pg.screenshot())).convert("RGB"))
        b.close()
    held = {20: 250, N - 1: 900}  # A beat before the swerve; the relief, before it loops.
    frames[0].save(sys.argv[1], save_all=True, append_images=frames[1:], loop=0,
                   duration=[held.get(f, 1000 // FPS) for f in range(N)])
