"""Normie's main window."""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from normie import APP_NAME, APP_VERSION
from normie.capture import match_loopback
from normie.config import (AUDIO_RETRY_S, AUTO_DEVICE_LABEL, DEFAULT_DEVICE_LABEL, LOOPBACK_SUFFIX,
                           load_config, save_config)
from normie.controller import Controller
from normie.gui.optional import Image, ImageTk
from normie.gui.own_app import OwnAppDialog
from normie.gui.theme import STATUS_COLORS, Theme, make_icon_image
from normie.gui.tray import Tray
from normie.gui.widgets import (Collapsible, FlatButton, LevelBar, MemberRow, ScrollList, Slider, Toggle,
                                bring_to_front, ellipsize, use_dark_titlebar)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME if APP_VERSION == "dev" else f"{APP_NAME} {APP_VERSION}")
        self.configure(bg=Theme.BG)
        self._scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        self.geometry(f"{self.px(880)}x{self.px(780)}")
        self.minsize(self.px(740), self.px(600))
        self.cfg = load_config()
        self._logq: queue.Queue[str] = queue.Queue()
        self._status: tuple[str, str] = ("Disconnected", "off")
        self._dot_kind = ""
        self._save_job = None
        self._rows: dict[str, MemberRow] = {}
        self._row_order: list[str] = []
        self._empty_shown = False
        self._closing = False
        self._meter_generation = 0
        self._meter_device: str | None = None     # loopback device last asked for
        self._discord_output: str | None = None   # device Discord plays on (None = unknown)
        self.shutdown_thread: threading.Thread | None = None
        self._setup_dialog: OwnAppDialog | None = None
        self.ui_calls: queue.Queue = queue.Queue()  # functions coming from other threads (tray, RPC)
        self.ctrl = Controller(self.cfg, self.log, self.set_status,
                               on_output_device=lambda name: self.ui_calls.put(
                                   lambda: self._discord_output_changed(name)),
                               on_access_refused=lambda own_app: self.ui_calls.put(
                                   lambda: self._access_refused(own_app)))
        self._init_style()
        self._build()
        self._set_window_icon()
        self._dark_titlebar()
        if self.cfg["refresh_token"]:
            self.ctrl.connect()  # authorized before: connect right away
        self._start_meter()  # after connect(): in automatic mode it waits for Discord's device
        self.tray = Tray(self)
        self.bind("<Unmap>", self._on_unmap)
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.after(150, self._tick)

    def px(self, n: float) -> int:
        """Convert design pixels (100% scaling) to the monitor's real scale."""
        return int(round(n * self._scale))

    # called from any thread
    def log(self, msg: str) -> None:
        self._logq.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def set_status(self, text: str, kind: str = "busy") -> None:
        self._status = (text, kind)

    # ── look and feel ──
    def _init_style(self) -> None:
        self.f_body = (Theme.FONT, 10)
        self.f_small = (Theme.FONT, 9)
        self.f_bold = (Theme.FONT_BOLD, 10)
        self.f_chip = (Theme.FONT_BOLD, 8)
        self.f_section = (Theme.FONT_BOLD, 9)
        self.f_big = (Theme.FONT_BOLD, 14)
        self.f_title = (Theme.FONT_BOLD, 17)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=Theme.CARD, foreground=Theme.TEXT, font=self.f_body,
                        bordercolor=Theme.BORDER, lightcolor=Theme.CARD, darkcolor=Theme.CARD,
                        troughcolor=Theme.INPUT, fieldbackground=Theme.INPUT, focuscolor=Theme.CARD,
                        selectbackground=Theme.INPUT, selectforeground=Theme.TEXT,
                        insertcolor=Theme.TEXT, arrowcolor=Theme.TEXT)
        style.configure("TCombobox", background=Theme.INPUT, bordercolor=Theme.INPUT,
                        lightcolor=Theme.INPUT, darkcolor=Theme.INPUT, padding=self.px(5))
        style.map("TCombobox", fieldbackground=[("readonly", Theme.INPUT)],
                  foreground=[("readonly", Theme.TEXT)], background=[("active", Theme.BORDER)],
                  selectbackground=[("readonly", Theme.INPUT)], selectforeground=[("readonly", Theme.TEXT)])
        style.configure("TSpinbox", background=Theme.INPUT, bordercolor=Theme.INPUT,
                        lightcolor=Theme.INPUT, darkcolor=Theme.INPUT, padding=self.px(4))
        style.map("TSpinbox", background=[("active", Theme.BORDER)])
        style.configure("Vertical.TScrollbar", background=Theme.INPUT, troughcolor=Theme.BG,
                        bordercolor=Theme.BG, lightcolor=Theme.INPUT, darkcolor=Theme.INPUT,
                        arrowcolor=Theme.MUTED, gripcount=0)
        style.map("Vertical.TScrollbar", background=[("active", Theme.BORDER)])
        self.option_add("*TCombobox*Listbox.background", Theme.INPUT)
        self.option_add("*TCombobox*Listbox.foreground", Theme.TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", Theme.ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", f"{{{Theme.FONT}}} 10")

    def _set_window_icon(self) -> None:
        if Image is not None:
            self._icon_photo = ImageTk.PhotoImage(make_icon_image(64))  # keep a reference
            self.iconphoto(True, self._icon_photo)

    def _dark_titlebar(self) -> None:
        use_dark_titlebar(self)

    def _label(self, parent, text="", muted=False, font=None, **kw) -> tk.Label:
        return tk.Label(parent, text=text, bg=parent["bg"], fg=Theme.MUTED if muted else Theme.TEXT,
                        font=font or self.f_body, **kw)

    # ── layout ──
    def _build(self) -> None:
        px = self.px
        side = {"padx": px(22)}

        # header: logo, title, status and the single connect button
        head = tk.Frame(self, bg=Theme.BG)
        head.pack(fill="x", pady=(px(20), px(14)), **side)
        if Image is not None:
            self._header_logo = ImageTk.PhotoImage(make_icon_image(px(46)))  # keep a reference
            tk.Label(head, image=self._header_logo, bg=Theme.BG).pack(side="left", padx=(0, px(14)))
        titles = tk.Frame(head, bg=Theme.BG)
        titles.pack(side="left")
        self._label(titles, APP_NAME, font=self.f_title).pack(anchor="w")
        self._label(titles, "Evens out each person's volume in your Discord call — just for you.",
                    muted=True, font=self.f_small).pack(anchor="w")
        self.btn_connect = FlatButton(head, "Connect", self._toggle_connect, "accent",
                                      self.f_bold, px(20), px(9))
        self.btn_connect.pack(side="right")
        status = tk.Frame(head, bg=Theme.BG)
        status.pack(side="right", padx=px(16))
        self.status_dot = tk.Canvas(status, width=px(10), height=px(10), bg=Theme.BG, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, px(7)))
        self.status_lbl = self._label(status, muted=True)
        self.status_lbl.pack(side="left")

        # main card: on/off, target and output meter
        card = tk.Frame(self, bg=Theme.CARD, padx=px(18), pady=px(16))
        card.pack(fill="x", **side)
        card.columnconfigure(1, weight=1)
        self.var_enabled = tk.BooleanVar(value=self.cfg["enabled"])
        top = tk.Frame(card, bg=Theme.CARD)
        top.grid(row=0, column=0, columnspan=2, sticky="w")
        Toggle(top, self.var_enabled, self._settings_changed, px, Theme.CARD).pack(side="left")
        about = tk.Frame(top, bg=Theme.CARD)
        about.pack(side="left", padx=px(14))
        self._label(about, "Auto-normalize", font=self.f_bold).pack(anchor="w")
        self._label(about, "Adjusts whoever speaks alone, every ~2 s of speech.",
                    muted=True, font=self.f_small).pack(anchor="w")
        FlatButton(card, "Reset everyone to 100%", self._reset, "ghost", self.f_body).grid(
            row=0, column=2, sticky="e")

        tk.Frame(card, bg=Theme.BORDER, height=1).grid(row=1, column=0, columnspan=3, sticky="we",
                                                       pady=px(14))

        self._label(card, "Target level").grid(row=2, column=0, sticky="w")
        self.var_target = tk.DoubleVar(value=self.cfg["target_db"])
        Slider(card, self.var_target, -40, -10, self._target_moved, px, Theme.CARD).grid(
            row=2, column=1, sticky="we", padx=px(14))
        self.lbl_target = self._label(card, width=10, anchor="e")
        self.lbl_target.grid(row=2, column=2, sticky="e")

        self._label(card, "Output now").grid(row=3, column=0, sticky="w", pady=(px(12), 0))
        self.out_bar = LevelBar(card, px, Theme.CARD, height=10)
        self.out_bar.grid(row=3, column=1, sticky="we", padx=px(14 + 8), pady=(px(12), 0))  # aligns with the slider
        self.lbl_out = self._label(card, muted=True, width=10, anchor="e")
        self.lbl_out.grid(row=3, column=2, sticky="e", pady=(px(12), 0))
        self._target_moved(save=False)

        # advanced settings (closed by default)
        self.advanced = Collapsible(self, self, "Advanced settings")
        self.advanced.pack(fill="x", pady=(px(14), 0), **side)
        self._build_advanced(self.advanced.body)

        # activity (log), pinned to the bottom
        self.activity = Collapsible(self, self, "Activity")
        self.activity.pack(side="bottom", fill="x", pady=(px(10), px(18)), **side)
        body = self.activity.body
        self.txt_log = tk.Text(body, height=6, bg=Theme.CARD, fg=Theme.MUTED, font=("Consolas", 9),
                               relief="flat", bd=0, wrap="word", state="disabled", highlightthickness=0)
        scroll = ttk.Scrollbar(body, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True)

        # people in the call
        members_head = tk.Frame(self, bg=Theme.BG)
        members_head.pack(fill="x", pady=(px(18), px(8)), **side)
        self.lbl_members = self._label(members_head, "IN CALL", muted=True, font=self.f_section)
        self.lbl_members.pack(side="left")
        self._label(members_head, "click AUTO to lock someone", muted=True,
                    font=self.f_small).pack(side="right")
        self.list = ScrollList(self, Theme.BG)
        self.list.pack(fill="both", expand=True, **side)
        self.empty = tk.Label(self.list.inner, bg=Theme.CARD, fg=Theme.MUTED, font=self.f_body,
                              pady=px(36))

    def _build_advanced(self, body: tk.Frame) -> None:
        px = self.px
        body.columnconfigure(1, weight=1)
        self._label(body, "Speed").grid(row=0, column=0, sticky="w")
        self.var_speed = tk.DoubleVar(value=self.cfg["speed"])
        Slider(body, self.var_speed, 0.1, 1.0, self._speed_moved, px, Theme.CARD).grid(
            row=0, column=1, sticky="we", padx=px(14))
        self.lbl_speed = self._label(body, muted=True, width=16, anchor="e")
        self.lbl_speed.grid(row=0, column=2, sticky="e")
        self._speed_moved(save=False)

        self._label(body, "Allowed volume").grid(row=1, column=0, sticky="w", pady=px(12))
        box = tk.Frame(body, bg=Theme.CARD)
        box.grid(row=1, column=1, sticky="w", padx=px(14))
        self.var_min = tk.IntVar(value=self.cfg["min_volume"])
        self.var_max = tk.IntVar(value=self.cfg["max_volume"])
        for i, var in enumerate((self.var_min, self.var_max)):
            spin = ttk.Spinbox(box, from_=0, to=200, increment=5, width=5, textvariable=var,
                               command=self._settings_changed)
            spin.bind("<FocusOut>", self._settings_changed)
            spin.bind("<Return>", self._settings_changed)
            spin.pack(side="left")
            self._label(box, "%   to   " if i == 0 else "%", muted=True).pack(side="left")

        self._label(body, "Monitored output").grid(row=2, column=0, sticky="w")
        if self.cfg["device_mode"] == "discord":
            current = AUTO_DEVICE_LABEL
        else:
            current = self.cfg["device"] or DEFAULT_DEVICE_LABEL
        self.var_device = tk.StringVar(value=current)
        combo = ttk.Combobox(body, textvariable=self.var_device, state="readonly",
                             values=[AUTO_DEVICE_LABEL, DEFAULT_DEVICE_LABEL, *self.ctrl.meter.loopback_devices()])
        combo.grid(row=2, column=1, columnspan=2, sticky="we", padx=(px(14), 0))
        combo.bind("<<ComboboxSelected>>", lambda _e: self._device_changed())
        self.lbl_device_note = self._label(body, muted=True, font=self.f_small, anchor="w")
        self.lbl_device_note.grid(row=3, column=1, columnspan=2, sticky="w", padx=(px(14), 0), pady=(px(4), 0))

        self._label(body, "Discord app").grid(row=4, column=0, sticky="w", pady=(px(14), 0))
        app_row = tk.Frame(body, bg=Theme.CARD)
        app_row.grid(row=4, column=1, columnspan=2, sticky="we", padx=(px(14), 0), pady=(px(14), 0))
        self.lbl_app = self._label(app_row, muted=True, anchor="w")
        self.lbl_app.pack(side="left")
        self.btn_app = FlatButton(app_row, "", self.open_app_setup, "ghost", self.f_body)
        self.btn_app.pack(side="right")
        self._show_app()

        tray_row = tk.Frame(body, bg=Theme.CARD)
        tray_row.grid(row=5, column=0, columnspan=3, sticky="w", pady=(px(14), 0))
        self.var_tray = tk.BooleanVar(value=self.cfg["minimize_to_tray"])
        Toggle(tray_row, self.var_tray, self._settings_changed, px, Theme.CARD).pack(side="left")
        self._label(tray_row, "Minimizing hides Normie in the tray (icon near the clock)").pack(
            side="left", padx=px(12))

        self._label(body, "Tip: put Discord on one output and games/music on another — the "
                          "measurements get cleaner.", muted=True, font=self.f_small).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(px(12), 0))

    # ── actions ──
    def _toggle_connect(self) -> None:
        if self.ctrl.active:
            self.ctrl.disconnect()
        else:
            self.ctrl.connect()

    def toggle_lock(self, user_id: str) -> None:
        if self.ctrl.engine.toggle_lock(user_id) is not None:
            self._schedule_save()

    # ── which Discord app to log in with ──
    def _show_app(self) -> None:
        own = self.cfg["own_client_id"]
        self.lbl_app.configure(text=f"Your own app · {own}" if own
                               else "Normie's shared app (testers only, for now)")
        self.btn_app.configure(text="Change…" if own else "Use my own app…")

    def open_app_setup(self, reason: str = "") -> None:
        dialog = self._setup_dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.destroy()  # reopen, so a new reason is shown
        # After a refusal the user is still in Discord's popup, so Normie is usually behind it
        if self.state() in ("withdrawn", "iconic"):
            self.show_window()
        else:
            bring_to_front(self)
        dialog = self._setup_dialog = OwnAppDialog(self, reason)
        bring_to_front(dialog)
        dialog.entry.focus_set()

    def _access_refused(self, own_app: bool) -> None:
        if own_app:
            reason = ("Discord refused your own app for this account. Apps owned by a team need "
                      "testers too: create one under your personal account and paste its ID here.")
        else:
            reason = "Discord refused Normie's shared app for your account."
        self.open_app_setup(reason)

    def use_own_app(self, client_id: str) -> None:
        """Switch to the user's own Discord app ("" = back to Normie's shared app)."""
        self.ctrl.use_app(client_id)
        self._show_app()
        if client_id:
            self.log(f"Now using your own Discord app ({client_id}). "
                     "Approve the request in Discord to connect.")
        else:
            self.log("Now using Normie's shared app. Approve the request in Discord to connect.")

    def toggle_enabled(self) -> None:
        self.var_enabled.set(not self.var_enabled.get())
        self._settings_changed()

    # ── tray ──
    def _on_unmap(self, event) -> None:
        # A binding on the root Tk also receives Unmap from EVERY child widget (bindtags):
        # only react when the window itself was minimized.
        if (event.widget is self and self.state() == "iconic"
                and self.cfg["minimize_to_tray"] and self.tray.available):
            self.after_idle(self._hide_to_tray)

    def _hide_to_tray(self) -> None:
        self.withdraw()
        self.tray.notify_once(f"{APP_NAME} is still running here. Click the icon to open it.")

    def show_window(self) -> None:
        self.deiconify()
        self.state("normal")
        bring_to_front(self)

    def _target_moved(self, _=None, save=True) -> None:
        self.lbl_target.configure(text=f"{round(self.var_target.get())} dBFS")
        if save:
            self._settings_changed()

    def _speed_moved(self, _=None, save=True) -> None:
        self.lbl_speed.configure(text=f"{self.var_speed.get():.0%} per window")
        if save:
            self._settings_changed()

    def _settings_changed(self, *_):
        try:
            enabled = bool(self.var_enabled.get())
            target = round(float(self.var_target.get()))
            speed = round(float(self.var_speed.get()), 2)
            lo, hi = int(self.var_min.get()), int(self.var_max.get())
            to_tray = bool(self.var_tray.get())
        except (tk.TclError, ValueError):
            return
        lo = max(0, min(200, lo))
        hi = max(lo, min(200, hi))
        self.cfg.update(enabled=enabled, target_db=target, speed=speed, min_volume=lo, max_volume=hi,
                        minimize_to_tray=to_tray)
        self._schedule_save()

    def _schedule_save(self) -> None:
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(800, lambda: save_config(self.cfg))

    def _wanted_device(self) -> str | None:
        """Loopback device to capture ("" = Windows default, None = wait for Discord)."""
        if self.cfg["device_mode"] != "discord":
            return self.cfg["device"]
        if self._discord_output is None:
            # Still connecting: wait to learn Discord's device instead of opening a wrong one.
            return None if self.ctrl.active else ""
        if not self._discord_output:  # Discord couldn't tell us: Windows default
            return ""
        match = match_loopback(self._discord_output, self.ctrl.meter.loopback_devices())
        if match is None:
            self.log(f"No loopback device matches Discord's output “{self._discord_output}”; "
                     "using the Windows default.")
            return ""
        return match

    def _discord_output_changed(self, name: str) -> None:
        self._discord_output = name
        if name and self.cfg["device_mode"] == "discord":
            self.log(f"Discord plays on “{name}” — capturing that device")
        self._start_meter()

    def _start_meter(self) -> None:
        # The device may be missing or busy (unplugged, exclusive mode…): keep retrying off
        # the GUI thread. A newer call (e.g. a different device was picked) makes older
        # retry loops give up.
        device = self._wanted_device()
        auto = self.cfg["device_mode"] == "discord"
        if device is None:
            self.lbl_device_note.configure(text="Waiting for Discord to say which device it uses…")
            return
        shown = device.removesuffix(LOOPBACK_SUFFIX) if device else DEFAULT_DEVICE_LABEL
        self.lbl_device_note.configure(text=f"Following Discord → {shown}" if auto else "")
        if device == self._meter_device:
            return  # already capturing (or trying to capture) this device
        self._meter_device = device
        self._meter_generation += 1
        generation = self._meter_generation

        def run():
            failures = 0
            while not self._closing and generation == self._meter_generation:
                try:
                    self.ctrl.meter.start(device)
                    return
                except Exception as exc:
                    failures += 1
                    if failures == 1:
                        self.log(f"Couldn't open the audio loopback ({exc}); "
                                 f"retrying every {AUDIO_RETRY_S:.0f} s…")
                time.sleep(AUDIO_RETRY_S)

        threading.Thread(target=run, name="audio-open", daemon=True).start()

    def _device_changed(self) -> None:
        value = self.var_device.get()
        if value == AUTO_DEVICE_LABEL:
            self.cfg["device_mode"] = "discord"
        else:
            self.cfg["device_mode"] = "manual"
            self.cfg["device"] = "" if value == DEFAULT_DEVICE_LABEL else value
        self._schedule_save()
        self._start_meter()

    def _reset(self) -> None:
        n = self.ctrl.reset_volumes()
        self.log(f"Volume reset to 100% for {n} people")

    def quit_app(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.withdraw()  # disappear right away, even if the rest takes a moment
        save_config(self.cfg)
        self.tray.stop()
        # Closing the WASAPI stream sometimes hangs in the driver; running it off the GUI
        # means the app can never get stuck open (main() waits up to 3 s, then exits anyway).
        self.shutdown_thread = threading.Thread(target=self.ctrl.shutdown, name="shutdown", daemon=True)
        self.shutdown_thread.start()
        self.destroy()

    # ── periodic screen refresh ──
    def _tick(self) -> None:
        while True:  # tray clicks
            try:
                fn = self.ui_calls.get_nowait()
            except queue.Empty:
                break
            fn()
            if self._closing:
                return
        self._drain_log()

        text, kind = self._status
        self.status_lbl.configure(text=text)
        if kind != self._dot_kind:
            self._dot_kind = kind
            s = self.px(10)
            self.status_dot.delete("all")
            self.status_dot.create_oval(1, 1, s - 1, s - 1, fill=STATUS_COLORS.get(kind, Theme.FAINT),
                                        outline="")
        active = self.ctrl.active
        self.btn_connect.configure(text="Disconnect" if active else "Connect")
        self.btn_connect.set_kind("ghost" if active else "accent")

        channel, out_db, rows = self.ctrl.engine.snapshot()
        target, deadband = self.cfg["target_db"], self.cfg["deadband_db"]
        live = out_db > -90
        self.out_bar.set(out_db if live else None, target, Theme.ACCENT)
        self.lbl_out.configure(text=f"{out_db:.0f} dBFS" if live else "silence")
        self.lbl_members.configure(
            text=f"IN CALL  ·  {ellipsize(channel, 40).upper()}  ·  {len(rows)}" if channel else "IN CALL")
        self._sync_rows(rows, target, deadband, channel, active)
        tip = f"{APP_NAME} — {text}"
        if channel:
            tip += f"\n{channel}: {len(rows)} people"
        if not self.cfg["enabled"]:
            tip += "\nnormalization paused"
        self.tray.set_tooltip(tip)

        self.after(150, self._tick)

    def _drain_log(self) -> None:
        lines = []
        while True:
            try:
                lines.append(self._logq.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", "\n".join(lines) + "\n")
        excess = int(self.txt_log.index("end-1c").split(".")[0]) - 500
        if excess > 0:
            self.txt_log.delete("1.0", f"{excess + 1}.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")
        self.activity.set_hint(ellipsize(lines[-1].split("] ", 1)[-1], 90))

    def _sync_rows(self, rows: list[dict], target, deadband, channel, active) -> None:
        ids = [r["user_id"] for r in rows]
        for uid in [u for u in self._rows if u not in ids]:
            self._rows.pop(uid).destroy()
        for r in rows:
            row = self._rows.get(r["user_id"])
            if row is None:
                row = self._rows[r["user_id"]] = MemberRow(self.list.inner, self, r["user_id"])
            row.update_row(r, target, deadband)
        if ids != self._row_order:
            for uid in ids:
                self._rows[uid].pack_forget()
            for uid in ids:
                self._rows[uid].pack(fill="x", pady=(0, self.px(6)))
            self._row_order = ids

        if rows:
            if self._empty_shown:
                self.empty.pack_forget()
                self._empty_shown = False
            return
        if not active:
            msg = "Click Connect to get started."
        elif not channel:
            msg = "Join a voice call in Discord."
        else:
            msg = "Just you in the call so far."
        self.empty.configure(text=msg)
        if not self._empty_shown:
            self.empty.pack(fill="x")
            self._empty_shown = True
