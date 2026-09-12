"""Render the worm as a square PNG logo: the same digit cloud as the site, on black.

  python -m wormhole.logo            -> web/logo.png (1024 px)"""
import math
import random

from PIL import Image, ImageDraw, ImageFont

from . import config as C

FONTS = ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Monaco.ttf", "/Library/Fonts/Courier New.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"]


def _font(size):
    for f in FONTS:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(size=1024, stage=1, path=None, seed=7):
    rnd = random.Random(seed)
    cols = 64
    cell = size / cols
    rows = cols
    W = H = float(size)
    n = 9 + stage * 2
    R = H * 0.21
    sp = R * 0.60
    need = sp * (n - 1) + R * 2.8
    sc = min(1.0, (W - 40) / need)
    r0, s0 = R * sc, sp * sc
    hx = W / 2 + (s0 * (n - 1)) / 2 - r0 * 0.1
    ymid = H * 0.60
    amp = H * 0.09 * sc
    segs = []
    for i in range(n):
        x = hx - i * s0
        y = ymid + amp * math.sin(i * 0.72) + (amp * 0.15 if i == 0 else 0)
        r = r0 * (1 if i == 0 else 0.90 - 0.52 * (i / (n - 1)))
        segs.append((x, y, r))
    hood = (hx - r0 * 0.10, hy_ := ymid + amp * 0.15 + amp * math.sin(0), r0 * 1.10, hy_ - r0 * 0.20)
    hy = segs[0][1]
    hood = (hx - r0 * 0.10, hy - r0 * 0.30, r0 * 1.10, hy - r0 * 0.20)
    tip = (hx - r0 * 0.02, hy - r0 * 1.30, hx + r0 * 1.30, hy - r0 * 2.15)
    fe = (tip[2], tip[3], tip[2] + r0 * 0.5, tip[3] - r0 * 0.4)

    img = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _font(int(cell * 1.15))

    def seg_dist(px, py, x0, y0, x1, y1):
        vx, vy = x1 - x0, y1 - y0
        u = ((px - x0) * vx + (py - y0) * vy) / (vx * vx + vy * vy)
        if u < 0 or u > 1:
            return None, None
        ex, ey = x0 + u * vx - px, y0 + u * vy - py
        return math.hypot(ex, ey), u

    for r in range(rows):
        py = r * cell + cell / 2
        for q in range(cols):
            px = q * cell + cell / 2
            part, shade, lx, ly = None, 0.0, 0.0, 0.0
            bt = 1.0
            for (x, y, rr) in segs:
                t = math.hypot(px - x, py - y) / rr
                if t < bt:
                    bt, part, shade = t, "body", 1 - t * t
                    lx, ly = (px - x) / rr, (py - y) / rr
            hxx, hyy, hr, brim = hood
            t = math.hypot(px - hxx, py - hyy) / hr
            if t < 1 and py < brim:
                part, shade, lx, ly = "hood", 1 - t * t, (px - hxx) / hr, (py - hyy) / hr
            d, u = seg_dist(px, py, *tip)
            if d is not None:
                w = r0 * 0.40 * (1 - u) + cell * 0.7
                if d < w:
                    part, shade = "hood", 1 - (d / w) ** 2
            d, u = seg_dist(px, py, *fe)
            if d is not None and d < cell * 0.95:
                part, shade = "feather", 1.0
            if part == "body" and math.hypot(px - (hx + r0 * 0.40), py - (hy - r0 * 0.02)) < r0 * 0.15:
                part = None
            if not part:
                continue
            b = 0.35 + 0.55 * shade + 0.16 * (-lx - ly)
            b = max(0.22, min(1.0, b))
            if part == "hood":
                col = (int(200 * b), int(255 * b), int(205 * b))
            elif part == "feather":
                col = (int(255 * b), int(255 * b), int(255 * b))
            else:
                col = (0, int(255 * b), int(65 * b))
            draw.text((q * cell, r * cell), rnd.choice("01"), fill=col, font=font)
    out = path or (C.ROOT / "web" / "logo.png")
    img.save(out, "PNG", optimize=True)
    return out


if __name__ == "__main__":
    print("wrote", render())
