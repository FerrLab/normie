"""Colors, fonts and Normie's logo (drawn here for the window, tray and .exe icons)."""
from __future__ import annotations

import numpy as np

from normie.gui.optional import Image, ImageDraw


class Theme:
    """Discord-style palette (dark mode)."""
    BG = "#1e1f22"
    CARD = "#2b2d31"
    ROW = "#313338"
    INPUT = "#383a40"
    BORDER = "#43454b"
    TEXT = "#f2f3f5"
    MUTED = "#a3a6aa"
    FAINT = "#6d6f78"
    ACCENT = "#5865f2"
    ACCENT_HOVER = "#4752c4"
    GREEN = "#23a55a"
    YELLOW = "#f0b232"
    RED = "#f23f43"
    AVATARS = ("#5865f2", "#3ba55c", "#faa61a", "#ed4245", "#eb459e", "#00a8fc", "#9b59b6", "#1abc9c")
    FONT = "Segoe UI"
    FONT_BOLD = "Segoe UI Semibold"


# Normie's logo: voice bars — one too loud, one too quiet and three leveled ones filling
# the target band (mint lines). Sizes are fractions of the icon's side.
# Single source of truth: the window, tray and .exe icons and the files in assets/ all
# come from here.
LOGO = {
    "bg": ("#5b5fef", "#9d4edd"),        # diagonal gradient (top-left → bottom-right)
    "line": "#5eead4",
    "bars": (0.66, 0.24, 0.42, 0.42, 0.42),
    "bar_w": 0.095,
    "gap": 0.055,
    "band": 0.42,                         # height of the target band
    "band_half_w": 0.40,
    "line_t": 0.022,
    "radius": 0.225,                      # corner radius of the squircle
}


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))


def make_icon_image(size: int = 64, *, full_bleed: bool = False, mark_scale: float = 1.0):
    """Draw the logo at `size` px (rendered 4x larger and scaled down for smooth edges).

    full_bleed  square background without rounded corners (Discord crops icons to a circle)
    mark_scale  shrinks the bars so they sit comfortably inside that circle
    """
    s = size * 4
    t = np.add.outer(np.arange(s), np.arange(s))[..., None] / (2 * s - 2)
    c1, c2 = (np.array(_rgb(c), dtype=float) for c in LOGO["bg"])
    gradient = Image.fromarray((c1 + (c2 - c1) * t).astype(np.uint8), "RGB")
    mask = Image.new("L", (s, s), 0)
    if full_bleed:
        ImageDraw.Draw(mask).rectangle((0, 0, s, s), fill=255)
    else:
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=s * LOGO["radius"], fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(img)
    k, c = s * mark_scale, s / 2
    line_t = max(1.0, k * LOGO["line_t"])
    half_w = k * LOGO["band_half_w"]
    for y in (c - k * LOGO["band"] / 2, c + k * LOGO["band"] / 2):  # target band, behind the bars
        draw.rounded_rectangle((c - half_w, y - line_t / 2, c + half_w, y + line_t / 2),
                               radius=line_t / 2, fill=LOGO["line"])
    bars = LOGO["bars"]
    w, g = k * LOGO["bar_w"], k * LOGO["gap"]
    x = c - (len(bars) * w + (len(bars) - 1) * g) / 2
    for h in bars:
        draw.rounded_rectangle((x, c - k * h / 2, x + w, c + k * h / 2), radius=w / 2, fill="white")
        x += w + g
    return img.resize((size, size), Image.Resampling.LANCZOS)


STATUS_COLORS = {"off": Theme.FAINT, "busy": Theme.YELLOW, "ok": Theme.GREEN, "error": Theme.RED}
BUTTON_COLORS = {  # kind: (background, hover background, text)
    "accent": (Theme.ACCENT, Theme.ACCENT_HOVER, "#ffffff"),
    "ghost": (Theme.INPUT, Theme.BORDER, Theme.TEXT),
}
