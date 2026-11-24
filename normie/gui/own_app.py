"""Dialog that walks the user through using their own Discord app."""
from __future__ import annotations

import threading
import tkinter as tk
import webbrowser

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only: importing App for real would be circular
    from normie.gui.app import App

from normie.config import CLIENT_ID, DEV_PORTAL_URL, REDIRECT_URI, parse_client_id
from normie.gui.theme import Theme
from normie.gui.widgets import FlatButton, use_dark_titlebar
from normie.oauth import check_client_id


class OwnAppDialog(tk.Toplevel):
    """Walks the user through creating their own Discord app and saves its Application ID.

    Until Discord approves Normie for RPC, its shared app only works for accounts on its
    App Testers list. An app's owner needs no list, so an app of one's own works at once."""

    def __init__(self, app: App, reason: str = ""):
        super().__init__(app, bg=Theme.BG)
        self.app = app
        self._checking = False
        px = app.px
        wrap = px(470)
        self.title("Use your own Discord app")
        self.resizable(False, False)
        self.transient(app)
        self.bind("<Escape>", lambda _e: self.destroy())

        outer = tk.Frame(self, bg=Theme.BG, padx=px(24), pady=px(20))
        outer.pack(fill="both", expand=True)
        app._label(outer, "Use your own Discord app", font=app.f_big).pack(anchor="w")
        if reason:
            tk.Label(outer, text=reason, bg=Theme.BG, fg=Theme.YELLOW, font=app.f_body,
                     wraplength=wrap + px(40), justify="left").pack(anchor="w", pady=(px(8), 0))
        app._label(outer, "Until Discord approves Normie, its shared app only works for accounts on "
                          "its tester list. An app of your own works for you right away. It's free "
                          "and takes about 2 minutes.", muted=True,
                   wraplength=wrap + px(40), justify="left").pack(anchor="w", pady=(px(6), px(14)))

        step = self._step(outer, 1, "Create the app",
                          "Open the Developer Portal, click New Application, name it (for example "
                          "“Normie”) and create it. Leave Team on Personal: apps owned by a team "
                          "need testers too.")
        FlatButton(step, "Open Developer Portal", lambda: webbrowser.open(DEV_PORTAL_URL),
                   "accent", app.f_body).pack(anchor="w", pady=(px(10), 0))

        step = self._step(outer, 2, "Let Normie log in",
                          "In your new app, open OAuth2. Under Redirects, add the address below. "
                          "Turn on Public Client, then click Save Changes.")
        row = tk.Frame(step, bg=Theme.CARD)
        row.pack(anchor="w", pady=(px(10), 0))
        redirect = tk.Entry(row, width=20, font=("Consolas", 10), relief="flat", bd=px(5),
                            readonlybackground=Theme.INPUT, fg=Theme.TEXT, highlightthickness=0)
        redirect.insert(0, REDIRECT_URI)
        redirect.configure(state="readonly")
        redirect.pack(side="left")
        self.btn_copy = FlatButton(row, "Copy", self._copy_redirect, "ghost", app.f_body)
        self.btn_copy.pack(side="left", padx=(px(8), 0))

        step = self._step(outer, 3, "Paste the Application ID",
                          "Open General Information, copy the Application ID and paste it here.")
        row = tk.Frame(step, bg=Theme.CARD)
        row.pack(fill="x", pady=(px(10), 0))
        self.var_id = tk.StringVar(value=app.cfg["own_client_id"])
        self.entry = tk.Entry(row, textvariable=self.var_id, font=app.f_body, relief="flat",
                              bd=px(5), bg=Theme.INPUT, fg=Theme.TEXT, insertbackground=Theme.TEXT,
                              highlightthickness=1, highlightbackground=Theme.INPUT,
                              highlightcolor=Theme.ACCENT)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e: self._check())
        self.var_id.trace_add("write", lambda *_: self._checking or self._say(""))  # stale error
        FlatButton(row, "Check and use", self._check, "accent", app.f_body).pack(
            side="left", padx=(px(8), 0))
        self.lbl_status = tk.Label(step, bg=Theme.CARD, fg=Theme.MUTED, font=app.f_small,
                                   wraplength=wrap, justify="left")
        self.lbl_status.pack(anchor="w", pady=(px(8), 0))

        bottom = tk.Frame(outer, bg=Theme.BG)
        bottom.pack(fill="x", pady=(px(6), 0))
        FlatButton(bottom, "Close", self.destroy, "ghost", app.f_body).pack(side="right")
        if app.cfg["own_client_id"]:
            FlatButton(bottom, "Go back to Normie's shared app", self._use_shared, "ghost",
                       app.f_body).pack(side="left")

        use_dark_titlebar(self)
        self.update_idletasks()
        x = app.winfo_rootx() + max(0, (app.winfo_width() - self.winfo_reqwidth()) // 2)
        self.geometry(f"+{x}+{app.winfo_rooty() + px(30)}")
        self.entry.focus_set()

    def _step(self, parent, number: int, title: str, text: str) -> tk.Frame:
        """A numbered card; returns the frame under its text for the step's controls."""
        app, px = self.app, self.app.px
        card = tk.Frame(parent, bg=Theme.CARD, padx=px(16), pady=px(14))
        card.pack(fill="x", pady=(0, px(10)))
        head = tk.Frame(card, bg=Theme.CARD)
        head.pack(fill="x")
        size = px(22)
        badge = tk.Canvas(head, width=size, height=size, bg=Theme.CARD, highlightthickness=0)
        badge.create_oval(1, 1, size - 1, size - 1, fill=Theme.ACCENT, outline="")
        badge.create_text(size / 2, size / 2, text=str(number), fill="#ffffff", font=app.f_chip)
        badge.pack(side="left")
        app._label(head, title, font=app.f_bold).pack(side="left", padx=(px(10), 0))
        body = tk.Frame(card, bg=Theme.CARD)
        body.pack(fill="x", padx=(size + px(10), 0))  # lines up with the title
        app._label(body, text, muted=True, wraplength=px(440), justify="left").pack(
            anchor="w", pady=(px(4), 0))
        return body

    def _say(self, text: str, color: str = Theme.MUTED) -> None:
        self.lbl_status.configure(text=text, fg=color)

    def _copy_redirect(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(REDIRECT_URI)
        self.btn_copy.configure(text="Copied ✓")
        self.after(1500, lambda: self.winfo_exists() and self.btn_copy.configure(text="Copy"))

    def _check(self) -> None:
        if self._checking:
            return
        client_id = parse_client_id(self.var_id.get())
        if client_id is None:
            self._say("That doesn't look like an Application ID: it's a long number "
                      "(17 to 20 digits).", Theme.RED)
            return
        if client_id == CLIENT_ID:
            self._say("That's Normie's shared app. Paste the ID of the app you created.", Theme.RED)
            return
        self.var_id.set(client_id)
        self._checking = True
        self._say("Checking with Discord…")

        def run():  # the handshake can take a moment: keep it off the GUI thread
            problem = check_client_id(client_id)
            self.app.ui_calls.put(lambda: self._checked(client_id, problem))

        threading.Thread(target=run, name="check-app", daemon=True).start()

    def _checked(self, client_id: str, problem: str) -> None:
        self._checking = False
        if not self.winfo_exists():
            return
        if problem:
            self._say(problem, Theme.RED)
            return
        self.app.use_own_app(client_id)
        self.destroy()

    def _use_shared(self) -> None:
        self.app.use_own_app("")
        self.destroy()
