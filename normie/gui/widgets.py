"""Flat, Discord-style Tkinter widgets drawn on canvases, plus small window helpers."""
from __future__ import annotations

import ctypes
import tkinter as tk
import zlib
from tkinter import ttk

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only: importing App for real would be circular
    from normie.gui.app import App

from normie.gui.theme import BUTTON_COLORS, Theme


def use_dark_titlebar(window: tk.Misc) -> None:
    """Dark title bar on Windows 10/11 (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):  # 20 since Windows 10 20H1; 19 before that
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on),
                                                          ctypes.sizeof(on)) == 0:
                break
    except Exception:
        pass


def bring_to_front(window: tk.Misc) -> None:
    """Raise a window above other programs' windows. Windows won't let a background app
    take focus, but a window that is topmost for a moment still ends up on top."""
    window.lift()
    window.attributes("-topmost", True)
    window.after(100, lambda: window.winfo_exists() and window.attributes("-topmost", False))
    window.focus_force()


def ellipsize(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle (a smoothed polygon with doubled corner points)."""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = (x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
           x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1)
    return canvas.create_polygon(pts, smooth=True, **kw)


class FlatButton(tk.Label):
    """Flat button with hover (the native Windows tk.Button ignores most colors)."""

    def __init__(self, parent, text, command, kind="ghost", font=None, padx=14, pady=6):
        super().__init__(parent, text=text, font=font, padx=padx, pady=pady, cursor="hand2")
        self._command = command
        self._kind = ""
        self._hovering = False
        self.set_kind(kind)
        self.bind("<Button-1>", lambda _e: self._command())
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))

    def set_kind(self, kind: str) -> None:
        if kind != self._kind:
            self._kind = kind
            self._paint()

    def _hover(self, on: bool) -> None:
        self._hovering = on
        self._paint()

    def _paint(self) -> None:
        bg, hover, fg = BUTTON_COLORS[self._kind]
        self.configure(bg=hover if self._hovering else bg, fg=fg)


class Toggle(tk.Canvas):
    """Discord-style switch (green = on)."""

    def __init__(self, parent, variable: tk.BooleanVar, command, px, bg):
        # (don't use self._w: tkinter keeps the widget's Tcl path name there)
        self._width, self._height = px(40), px(24)
        super().__init__(parent, width=self._width, height=self._height, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._var, self._command = variable, command
        self.bind("<Button-1>", self._click)
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _click(self, _event) -> None:
        self._var.set(not self._var.get())
        self._command()

    def _draw(self) -> None:
        self.delete("all")
        on, w, h = self._var.get(), self._width, self._height
        round_rect(self, 0, 0, w, h, h / 2, fill=Theme.GREEN if on else Theme.FAINT, outline="")
        pad = max(2, h // 8)
        d = h - 2 * pad
        x = w - pad - d if on else pad
        self.create_oval(x, pad, x + d, pad + d, fill="#ffffff", outline="")


class Slider(tk.Canvas):
    """Flat slider: rounded track, blue filled part and a white knob
    (ttk.Scale on Windows can't be styled like this)."""

    def __init__(self, parent, variable: tk.DoubleVar, from_: float, to: float, command, px, bg):
        self._px = px
        super().__init__(parent, height=px(22), bg=bg, highlightthickness=0, cursor="hand2")
        self._var, self._lo, self._hi, self._command = variable, from_, to, command
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        variable.trace_add("write", lambda *_: self._draw())

    def _track(self) -> tuple[int, int, float, float]:
        w, h = self.winfo_width(), self.winfo_height()
        knob = self._px(8)
        return w, h, knob, w - knob

    def _drag(self, event) -> None:
        _w, _h, x0, x1 = self._track()
        if x1 > x0:
            frac = min(1.0, max(0.0, (event.x - x0) / (x1 - x0)))
            self._var.set(self._lo + frac * (self._hi - self._lo))
            self._command()

    def _draw(self) -> None:
        self.delete("all")
        w, h, x0, x1 = self._track()
        if x1 - x0 < 4:
            return
        frac = min(1.0, max(0.0, (self._var.get() - self._lo) / (self._hi - self._lo)))
        x, cy, t, knob = x0 + frac * (x1 - x0), h / 2, self._px(3), self._px(8)
        round_rect(self, x0, cy - t, x1, cy + t, t, fill=Theme.INPUT, outline="")
        round_rect(self, x0, cy - t, max(x0 + 2 * t, x), cy + t, t, fill=Theme.ACCENT, outline="")
        self.create_oval(x - knob, cy - knob, x + knob, cy + knob, fill="#ffffff", outline="")


class LevelBar(tk.Canvas):
    """Level bar (−60…0 dBFS) with the target marked."""
    LO, HI = -60.0, 0.0

    def __init__(self, parent, px, bg, height=8):
        super().__init__(parent, height=px(height), bg=bg, highlightthickness=0)
        self._state: tuple = (None, -24.0, Theme.GREEN)
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, value_db: float | None, target_db: float, color: str) -> None:
        if value_db is not None:
            value_db = round(value_db * 2) / 2  # 0.5 dB steps: avoid pointless redraws
        state = (value_db, target_db, color)
        if state != self._state:
            self._state = state
            self._draw()

    def _x(self, db: float, width: int) -> float:
        return (min(self.HI, max(self.LO, db)) - self.LO) / (self.HI - self.LO) * width

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        value, target, color = self._state
        round_rect(self, 0, 0, w, h, h / 2, fill=Theme.INPUT, outline="")
        if value is not None and value > self.LO:
            round_rect(self, 0, 0, max(h, self._x(value, w)), h, h / 2, fill=color, outline="")
        tx = self._x(target, w)
        self.create_line(tx, 0, tx, h, fill=Theme.TEXT, width=2)


class ScrollList(tk.Frame):
    """Scrollable vertical list (Canvas + inner Frame); the scrollbar only shows when needed."""

    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda _e: self._refresh())
        self.canvas.bind("<Configure>", self._on_resize)
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _on_resize(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        self._refresh()

    def _refresh(self) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        overflow = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1
        if overflow and not self.scrollbar.winfo_ismapped():
            self.scrollbar.pack(side="right", fill="y", before=self.canvas)
        elif not overflow and self.scrollbar.winfo_ismapped():
            self.scrollbar.pack_forget()
            self.canvas.yview_moveto(0)

    def _on_wheel(self, event) -> None:
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget is not None and widget is not self:
            widget = widget.master
        if widget is self and self.scrollbar.winfo_ismapped():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


class Collapsible(tk.Frame):
    """Section that opens/closes when its title is clicked."""

    def __init__(self, parent, app: App, title: str):
        super().__init__(parent, bg=Theme.BG)
        self.app, self.title, self.opened, self.hint = app, title, False, ""
        self.head = tk.Label(self, bg=Theme.BG, fg=Theme.MUTED, font=app.f_section,
                             cursor="hand2", anchor="w")
        self.head.pack(fill="x")
        self.head.bind("<Button-1>", lambda _e: self.toggle())
        self.head.bind("<Enter>", lambda _e: self.head.configure(fg=Theme.TEXT))
        self.head.bind("<Leave>", lambda _e: self.head.configure(fg=Theme.MUTED))
        self.body = tk.Frame(self, bg=Theme.CARD, padx=app.px(16), pady=app.px(14))
        self._render()

    def toggle(self) -> None:
        self.opened = not self.opened
        self._render()

    def set_hint(self, hint: str) -> None:
        if hint != self.hint:
            self.hint = hint
            self._render()

    def _render(self) -> None:
        text = f"{'▾' if self.opened else '▸'}  {self.title.upper()}"
        if self.hint and not self.opened:
            text += f"    {self.hint}"
        self.head.configure(text=text)
        if self.opened:
            self.body.pack(fill="x", pady=(self.app.px(8), 0))
        else:
            self.body.pack_forget()


class MemberRow(tk.Frame):
    """Card for one person in the call: avatar, name, heard level vs. target, volume, lock."""

    def __init__(self, parent, app: App, user_id: str):
        px = app.px
        super().__init__(parent, bg=Theme.ROW, padx=px(14), pady=px(10))
        self.app, self.uid = app, user_id
        self._state: tuple | None = None
        self._avatar_size = px(40)
        self.avatar = tk.Canvas(self, width=self._avatar_size, height=self._avatar_size,
                                bg=Theme.ROW, highlightthickness=0)
        self.avatar.grid(row=0, column=0, rowspan=2, padx=(0, px(12)))
        self.name = tk.Label(self, bg=Theme.ROW, fg=Theme.TEXT, font=app.f_bold, anchor="w")
        self.name.grid(row=0, column=1, sticky="sw")
        self.sub = tk.Label(self, bg=Theme.ROW, fg=Theme.MUTED, font=app.f_small, anchor="w")
        self.sub.grid(row=1, column=1, sticky="nw")
        self.bar = LevelBar(self, px, Theme.ROW, height=8)
        self.bar.grid(row=0, column=2, sticky="sew", padx=px(16), pady=(0, px(3)))
        self.level = tk.Label(self, bg=Theme.ROW, fg=Theme.MUTED, font=app.f_small, anchor="w")
        self.level.grid(row=1, column=2, sticky="nw", padx=px(16))
        self.volume = tk.Label(self, bg=Theme.ROW, fg=Theme.TEXT, font=app.f_big, width=5, anchor="e")
        self.volume.grid(row=0, column=3, rowspan=2, sticky="e")
        self.arrow = tk.Label(self, bg=Theme.ROW, font=app.f_bold, width=2)
        self.arrow.grid(row=0, column=4, rowspan=2)
        self.chip = tk.Label(self, font=app.f_chip, width=9, pady=px(4), cursor="hand2")
        self.chip.grid(row=0, column=5, rowspan=2, padx=(px(6), 0))
        self.chip.bind("<Button-1>", lambda _e: app.toggle_lock(self.uid))
        self.columnconfigure(1, minsize=px(190))
        self.columnconfigure(2, weight=1)

    def update_row(self, r: dict, target: float, deadband: float) -> None:
        state = (r["name"], r["speaking"], r["heard_db"], r["source_db"], r["volume"],
                 r["locked"], r["muted"], r["change"], target, deadband)
        if state == self._state:
            return
        self._state = state
        inactive = r["locked"] or r["muted"]
        self._draw_avatar(r["name"], r["speaking"])
        self.name.configure(text=ellipsize(r["name"], 24))

        if r["muted"]:
            sub, sub_color = "muted by you", Theme.FAINT
        elif r["speaking"]:
            sub, sub_color = "speaking…", Theme.GREEN
        elif r["source_db"] is not None:
            sub, sub_color = f"mic reaches {r['source_db']:.0f} dB", Theme.MUTED
        else:
            sub, sub_color = "waiting to speak alone", Theme.FAINT
        self.sub.configure(text=sub, fg=sub_color)

        heard = r["heard_db"]
        if heard is None:
            color, level = Theme.FAINT, "no reading yet"
        else:
            diff = heard - target
            if abs(diff) <= deadband:
                color, level = Theme.GREEN, f"hearing {heard:.1f} dB · on target"
            elif diff < 0:
                color, level = Theme.YELLOW, f"hearing {heard:.1f} dB · quiet"
            else:
                color, level = Theme.RED, f"hearing {heard:.1f} dB · loud"
        self.bar.set(heard, target, Theme.FAINT if inactive and heard is not None else color)
        self.level.configure(text=level)

        self.volume.configure(text=f"{r['volume']}%", fg=Theme.FAINT if inactive else Theme.TEXT)
        change = r["change"]
        self.arrow.configure(text="▲" if change > 0 else ("▼" if change < 0 else ""),
                             fg=Theme.GREEN if change > 0 else Theme.YELLOW)
        if r["locked"]:
            self.chip.configure(text="LOCKED", bg=Theme.INPUT, fg=Theme.MUTED)
        else:
            self.chip.configure(text="AUTO", bg=Theme.ACCENT, fg="#ffffff")

    def _draw_avatar(self, name: str, speaking: bool) -> None:
        c, s, ring = self.avatar, self._avatar_size, self.app.px(3)
        c.delete("all")
        if speaking:  # green ring, like in Discord
            c.create_oval(0, 0, s, s, fill=Theme.GREEN, outline="")
            c.create_oval(ring, ring, s - ring, s - ring, fill=Theme.ROW, outline="")
        inset = ring + self.app.px(2)
        color = Theme.AVATARS[zlib.crc32(self.uid.encode()) % len(Theme.AVATARS)]
        c.create_oval(inset, inset, s - inset, s - inset, fill=color, outline="")
        c.create_text(s / 2, s / 2, text=(name.strip()[:1] or "?").upper(), fill="#ffffff",
                      font=self.app.f_bold)
