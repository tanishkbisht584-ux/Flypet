"""Procedural cyborg fly for the tkinter Canvas: chrome segmented body, red LED eyes, six
articulated legs walking in the tripod gait real flies use, translucent veined wings folded
over the back when walking and spread/blurred in flight. No image assets.

Flicker-free: canvas items are created ONCE and then updated in place via coords()/itemconfig()
each frame — never delete('all') + recreate, which is tkinter's classic flicker source and the
main reason the fly looked glitchy. A stable key per item keeps z-order fixed after frame 1.

Body coordinates: x forward, y right; rotated by fly.heading. Run `python sprite.py` for a
demo cycling idle -> walk -> fly (or `python sprite.py fly` to hold one state)."""
import math, sys, time

SIZE, SCALE = 110, 0.70                       # SCALE is the size knob; SIZE is the canvas box
                                              # and must stay >= ~150*SCALE or spread wings clip
STRIDE, LEG = 8.0, 19.0                       # gait stride (px), femur = tibia length
COXA = [(6, 8), (0, 8), (-6, 8)]              # front / mid / hind attachment on the thorax
REST = [(20, 19), (-2, 23), (-20, 21)]        # feet at rest
TUCK = [(-6, 10), (-10, 10), (-14, 10)]       # feet tucked in flight
LED = dict(hunger='#c9a227', fear='#e0201c', fatigue='#3b7dd8', curiosity='#27ae60')
GREYS = ['#8f949a', '#7e838a', '#6d7278', '#5b6066']
WING = [(0, 0), (10, -4.5), (24, -5), (34, -2), (34, 2), (24, 5), (10, 4.5)]   # leaf outline (u, v)
VEINS = [(22, -4), (30, -1.5), (33, 0), (30, 1.5)]


def draw_fly(cv, fly, t):
    d, c, state = fly.d, SIZE / 2, fly.state
    air, arousal = getattr(fly, 'airborne', 0.0), fly.arousal
    sc = SCALE * (1 + 0.1 * air)
    ch, sh = math.cos(fly.heading), math.sin(fly.heading)

    if not hasattr(cv, '_items'):
        cv._items = {}                       # key -> canvas id, created once, reused forever
    items, used = cv._items, set()

    def P(x, y, ox=0.0, oy=0.0):
        return (c + ox + (x * ch - y * sh) * sc, c + oy + (x * sh + y * ch) * sc)

    def _el(kind, key, coords, **cfg):
        """Create the item on first sighting, else move/recolor it. Keeps z-order stable."""
        used.add(key)
        if key not in items:
            create = getattr(cv, 'create_' + kind)
            items[key] = create(*coords, **cfg)
        else:
            cv.coords(items[key], *coords)
            if cfg:
                cv.itemconfig(items[key], **{k: v for k, v in cfg.items()
                                             if k in ('fill', 'outline', 'width', 'text', 'state')})
        return items[key]

    def poly(key, pts, **kw):
        _el('polygon', key, [v for p in pts for v in p], **kw)

    def circ(key, x, y, r, **kw):
        px, py = P(x, y); r *= sc
        _el('oval', key, (px - r, py - r, px + r, py + r), **kw)

    def seg(key, a, b, **kw):
        _el('line', key, (*P(*a), *P(*b)), **kw)

    # -- shadow (altitude) ---------------------------------------------------------------
    if air > 0.02:
        poly('shadow', [P(30 * math.cos(a), 12 * math.sin(a), 8 * air, 12 * air)
                        for a in (i / 8 * math.pi for i in range(16))],
             fill='#000000', stipple='gray50', outline='', smooth=True, state='normal')
    elif 'shadow' in items:
        cv.itemconfig(items['shadow'], state='hidden'); used.add('shadow')

    # -- legs: tripod gait ---------------------------------------------------------------
    moving = state not in ('idle', 'rest', 'stay', 'feed')
    for i in range(3):
        for k in (-1, 1):
            p = f'{i}{k}'
            cx, cy = COXA[i][0], k * COXA[i][1]
            g = (i + (k > 0)) % 2
            phi = fly.gait + math.pi * g
            swing = moving and math.cos(phi) > 0
            tx = REST[i][0] - STRIDE * math.sin(phi)
            ty = k * (REST[i][1] - (4 if swing else 0))
            if state == 'idle' and i == 0 and k == 1:
                tx += 3 * math.sin(t * 2)
            tx, ty = tx * (1 - air) + TUCK[i][0] * air, ty * (1 - air) + k * TUCK[i][1] * air
            dx, dy = tx - cx, ty - cy
            dist = max(math.hypot(dx, dy), 1e-6)
            L = max(LEG, dist / 2 + 0.5)
            h = math.sqrt(max(L * L - (dist / 2) ** 2, 0.0))
            nx, ny = -dy / dist, dx / dist
            if ny * k < 0:
                nx, ny = -nx, -ny
            kx, ky = cx + dx / 2 + nx * h, cy + dy / 2 + ny * h
            seg('fem' + p, (cx, cy), (kx, ky), fill='#7d8288', width=max(1, 4 * sc))
            seg('slv' + p, (cx + (kx - cx) * .3, cy + (ky - cy) * .3),
                (cx + (kx - cx) * .6, cy + (ky - cy) * .6), fill='#5b6066', width=max(1, 6 * sc))
            seg('tib' + p, (kx, ky), (tx, ty),
                fill='#b0b5ba' if swing else '#8d9298', width=max(1, (2 if swing else 2.5) * sc))
            circ('coxa' + p, cx, cy, 2.5, fill='#c5cacf', outline='#4a4e52')
            circ('knee' + p, kx, ky, 2.5, fill='#c5cacf', outline='#4a4e52')
            circ('tip' + p, tx, ty, 1.5, fill='#3a3d40', outline='')

    # -- abdomen: 4 chrome plates ----------------------------------------------------------
    for i in range(4):
        x0, x1 = -4 - 8.5 * i, -13 - 8.5 * i
        w0, w1 = 9 - 1.2 * i, 9 - 1.2 * (i + 1)
        poly(f'abd{i}', [P(x0, -w0), P(x1, -w1), P(x1, w1), P(x0, w0)], fill=GREYS[i], outline='#4a4e52')
        seg(f'abds{i}', (x0 - 1, -w0 + 2), (x1 + 1, -w1 + 2), fill='#c5cacf', width=1)
    poly('tail', [P(-38, -3), P(-42, 0), P(-38, 3)], fill='#4a4e52', outline='')

    # -- wings: folded over the back, or spread + blurred in flight -------------------------
    flap = math.sin(t * (40 + 40 * arousal)) * 22 if air > 0.05 else (1.5 * math.sin(t * 6) if arousal > 0.5 else 0)
    ghosts = ((1.0, 'gray25'), (0.6, 'gray12'), (0.3, 'gray12'))   # ghosts 1,2 only shown in flight
    for k in (-1, 1):
        for gi, (gh, stip) in enumerate(ghosts):
            key = f'wing{k}{gi}'
            show = gi == 0 or air > 0.5
            if not show:
                if key in items:
                    cv.itemconfig(items[key], state='hidden'); used.add(key)
                continue
            th = math.radians(k * (8 + 57 * air) + k * flap * gh)
            ax, ay, bx, by = -math.cos(th), math.sin(th), math.sin(th), math.cos(th)
            W = lambda u, v: P(-2 + u * ax + v * bx, k * 2 + u * ay + v * by)
            poly(key, [W(u, v) for u, v in WING], fill='#d6dfe8', stipple=stip,
                 outline='#aab6c4', smooth=True, state='normal')
            if gi == 0:
                # veins use W() which already returns SCREEN coords, so go straight to _el as a
                # line (like poly does) — NOT via seg(), which would apply P() a second time and
                # fling them into the corner.
                for vi, (u, v) in enumerate(VEINS):
                    _el('line', f'vein{k}{vi}', (*W(0, 0), *W(u, v)), fill='#8d9db0')

    # -- thorax --------------------------------------------------------------------------
    thorax = ('#b06060' if d['fear'] > 0.4 else '#8f8f80' if d['hunger'] > 0.6
              else '#5a5e63' if state == 'rest' else '#a8adb3')
    poly('thorax', [P(6 + 13 * math.cos(a), 10 * math.sin(a)) for a in (i / 8 * math.pi for i in range(16))],
         fill=thorax, outline='#4a4e52', smooth=True)
    seg('tmid', (18, 0), (-6, 0), fill='#4a4e52')
    for ri, (rx, ry) in enumerate(((12, 5), (12, -5), (0, 5), (0, -5))):
        circ(f'riv{ri}', rx, ry, 1.5, fill='#3a3d40', outline='')
    mood = max(d, key=d.get)
    circ('led0', 4, 0, 4, fill=LED[mood], stipple='gray50', outline='')
    circ('led1', 4, 0, 2.2, fill=LED[mood], outline='#ffffff')

    # -- head + LED eyes -------------------------------------------------------------------
    circ('head', 24, 0, 8, fill='#b5bac0', outline='#4a4e52')
    for k in (-1, 1):
        circ(f'eyeo{k}', 27, k * 5, 4 + 1.5 * d['fear'], fill='#6e0f0f', outline='')
        circ(f'eyei{k}', 27, k * 5, 2.6 + 1.2 * d['fear'],
             fill='#ff3b30' if d['fear'] > 0.4 else '#e0201c', outline='')
        circ(f'glint{k}', 28, k * 5 - 1, 0.9, fill='#ffffff', outline='')
        seg(f'ant{k}', (30, k * 3), (36, k * 7), fill='#8a8f95')
    seg('proboscis', (30, 0), (38, 0), fill='#c0392b', width=2,
        state='normal' if state == 'feed' else 'hidden')
    _el('text', 'zzz', (c + 26, c - 30), text='z z' if state == 'rest' else '',
        fill='#888', font=('Segoe UI', 10), state='normal' if state == 'rest' else 'hidden')

    # hide any cached item not touched this frame (defensive; keeps stale ghosts invisible)
    for key, iid in items.items():
        if key not in used:
            cv.itemconfig(iid, state='hidden')


if __name__ == '__main__':
    import tkinter as tk, types
    hold = sys.argv[1] if len(sys.argv) > 1 else None
    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f'{SIZE}x{SIZE}+300+300')
    root.attributes('-topmost', True)
    cv = tk.Canvas(root, width=SIZE, height=SIZE, bg='#f4f4f4', highlightthickness=0)
    cv.pack()
    f = types.SimpleNamespace(d=dict(hunger=.3, fear=.1, fatigue=.2, curiosity=.6), heading=-0.5,
                              arousal=0.6, state='idle', gait=0.0, airborne=0.0)
    t0 = time.time()

    def loop():
        t = time.time() - t0
        phase = {'idle': 0, 'walk': 1, 'fly': 2}.get(hold, int(t // 3) % 3)
        f.state = ('idle', 'wander', 'flee')[phase]
        f.airborne += ((1.0 if phase == 2 else 0.0) - f.airborne) * 0.15
        if phase == 1:
            f.gait += 0.35
        draw_fly(cv, f, t)
        root.after(20, loop)

    loop()
    root.mainloop()
