"""System tray icon (pystray), bridged to the Tkinter thread."""
from __future__ import annotations

import threading

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only: importing App for real would be circular
    from normie.gui.app import App

from normie import APP_NAME
from normie.gui.optional import Image, pystray
from normie.gui.theme import make_icon_image


class Tray:
    """Windows system tray icon. pystray runs its own loop on a thread; since tkinter
    can't be called from another thread, every click becomes a function in the
    app.ui_calls queue, which the GUI runs in _tick."""

    def __init__(self, app: App):
        self.app = app
        self.icon = None
        self._tooltip = ""
        self._notified = False
        if pystray is None or Image is None:
            app.log("Tray unavailable — install it with:  pip install pystray pillow")
            return
        menu = pystray.Menu(
            pystray.MenuItem(f"Open {APP_NAME}", self._post(app.show_window), default=True),
            pystray.MenuItem("Auto-normalize", self._post(app.toggle_enabled),
                             checked=lambda _item: bool(app.cfg["enabled"])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._post(app.quit_app)),
        )
        self.icon = pystray.Icon("normie", make_icon_image(64), APP_NAME, menu)
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    def _post(self, fn):
        return lambda _icon=None, _item=None: self.app.ui_calls.put(fn)

    @property
    def available(self) -> bool:
        return self.icon is not None

    def set_tooltip(self, text: str) -> None:
        if self.icon is not None and text != self._tooltip:
            self._tooltip = text
            self.icon.title = text[:127]  # Windows limit

    def notify_once(self, message: str) -> None:
        if self.icon is not None and not self._notified:
            self._notified = True
            try:
                self.icon.notify(message, APP_NAME)
            except Exception:
                pass

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
