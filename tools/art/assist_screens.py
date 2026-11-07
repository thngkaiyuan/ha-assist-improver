"""Three mock Assist dialogs for the README: the same request getting smoother."""

PANELS = [
    ("Today", "3 turns", "#E53935", [
        ("u", "It's cold in here"), ("a", "The living room is 18 °C."),
        ("u", "Turn up the heating"), ("a", "Which thermostat: Living room or Bedroom?"),
        ("u", "Living room, to 22"), ("a", "Set the living room to 22 °C.")]),
    ("After improvement 1", "1 turn, but only with the exact words", "#FB8C00", [
        ("u", "Set the living room thermostat to 22 degrees"), ("a", "Set the living room to 22 °C.")]),
    ("After improvement 2", "1 turn, naturally", "#2FAF5C", [
        ("u", "It's cold in here"), ("a", "Warming the living room to 22 °C, your usual.")]),
]

# Material Design "microphone", as in HA's own Assist dialog.
MIC = ('<svg width="22" height="22" viewBox="0 0 24 24"><path fill="#5F6B76" d="M12,2A3,3 0 0,1 15,5V11A3,3 0 0,1 '
       '12,14A3,3 0 0,1 9,11V5A3,3 0 0,1 12,2M19,11C19,14.53 16.39,17.44 13,17.93V21H11V17.93C7.61,17.44 5,14.53 '
       '5,11H7A5,5 0 0,0 12,16A5,5 0 0,0 17,11H19Z"/></svg>')


def panel(title, verdict, color, messages):
    bubbles = "".join(f'<div class="m {who}">{text}</div>' for who, text in messages)
    return (f'<div class="col"><div class="t">{title}</div><div class="dlg"><div class="hd"><span>✕</span> Assist'
            f'<span class="pipe">Home Assistant ▾</span></div><div class="msgs">{bubbles}</div>'
            f'<div class="in">Enter a request{MIC}</div></div>'
            f'<div class="v" style="color:{color}">{verdict}</div></div>')


HTML = """<html><head><style>
body{margin:0;background:#F5F7FA;font-family:Roboto,Helvetica,Arial,sans-serif;display:flex;gap:28px;padding:28px 32px;align-items:flex-start}
.col{width:340px;display:flex;flex-direction:column;align-items:center;gap:12px}
.t{font-weight:700;font-size:20px;color:#1F2933}
.dlg{width:100%;height:500px;background:#fff;border-radius:24px;box-shadow:0 6px 24px rgba(0,0,0,.12);display:flex;flex-direction:column;overflow:hidden}
.hd{display:flex;align-items:center;gap:14px;padding:16px 18px;font-size:20px;color:#1F2933;border-bottom:1px solid #EEF1F4}
.hd span:first-child{color:#5F6B76}.pipe{margin-left:auto;font-size:14px;color:#5F6B76}
.msgs{flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:10px;padding:16px}
.m{max-width:78%;padding:10px 14px;font-size:16px;line-height:1.35;border-radius:18px}
.u{align-self:flex-end;background:#03A9F4;color:#fff;border-bottom-right-radius:4px}
.a{align-self:flex-start;background:#EEF1F4;color:#1F2933;border-bottom-left-radius:4px}
.in{margin:0 14px 16px;padding:13px 16px;border-radius:12px;background:#F1F3F5;color:#8A949E;font-size:15px;display:flex;justify-content:space-between;align-items:center}
.v{font-weight:700;font-size:17px;text-align:center}
</style></head><body>""" + "".join(panel(*p) for p in PANELS) + "</body></html>"

if __name__ == "__main__":
    import sys
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1148, "height": 640}, device_scale_factor=2)
        pg.set_content(HTML)
        pg.screenshot(path=sys.argv[1], full_page=True)
        b.close()
