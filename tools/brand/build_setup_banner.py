#!/usr/bin/env python3
"""Draws installer/setup-banner.png, the side panel of the StoryCraft setup app (429 x 1052).

Ink background, soft cyan and violet glows, faint timeline-clip bars, the app icon in the middle.
Run from the repository root: python3 tools/brand/build_setup_banner.py
"""
from PIL import Image, ImageDraw, ImageFilter

W, H = 429, 1052
CYAN, VIOLET, INK = (34, 211, 238), (168, 85, 247), (11, 11, 20)


def glow(size, center, radius, color, alpha):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x, y = center
    d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color + (alpha,))
    return layer.filter(ImageFilter.GaussianBlur(radius * 0.6))


img = Image.new("RGBA", (W, H), INK + (255,))
# a slightly lifted ink towards the top, like the icon tile
top = Image.new("RGBA", (W, H), (26, 27, 46, 255))
mask = Image.linear_gradient("L").resize((W, H)).point(lambda v: 255 - v)
img = Image.composite(top, img, mask)
img.alpha_composite(glow((W, H), (60, 330), 230, CYAN, 70))
img.alpha_composite(glow((W, H), (380, 760), 260, VIOLET, 80))

# timeline-clip bars, like the S mark's strokes, faint behind the icon
bars = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d = ImageDraw.Draw(bars)
rows = [(120, 40, 210), (170, 140, 330), (220, 20, 150), (270, 190, 400), (780, 30, 260), (830, 160, 380), (880, 60, 200), (930, 220, 410)]
for y, x0, x1 in rows:
    d.rounded_rectangle((x0, y, x1, y + 14), radius=7, fill=(217, 219, 238, 26))
img.alpha_composite(bars)
# the playhead
head = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ImageDraw.Draw(head).line((214, 90, 214, 962), fill=(255, 255, 255, 40), width=2)
img.alpha_composite(head)

icon = Image.open("assets/icons/icon_default.png").convert("RGBA").resize((236, 236), Image.LANCZOS)
shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle((104, 424, 324, 644), radius=56, fill=(0, 0, 0, 150))
img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(22)))
img.alpha_composite(icon, ((W - 236) // 2, (H - 236) // 2))

img.convert("RGB").save("installer/setup-banner.png", optimize=True)
print("installer/setup-banner.png", img.size)
