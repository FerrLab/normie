"""Starts Normie. `python -m normie`, normie.pyz and normie.exe all enter through main()."""
from __future__ import annotations

import ctypes
import os
import sys
import tkinter as tk
from tkinter import messagebox

from normie import APP_NAME


_instance_mutex = None


def _claim_single_instance() -> bool:
    """With the window hidden in the tray it's easy to launch the app again by accident;
    two copies would fight over Discord's RPC. A named mutex guarantees a single one."""
    global _instance_mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\Normie.DiscordVoiceNormalizer")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def main() -> None:
    if sys.platform != "win32":
        sys.exit("Normie only runs on Windows (it uses WASAPI and Discord's IPC pipe).")
    try:  # required dependencies, checked before anything imports them
        import numpy  # noqa: F401
        import pyaudiowpatch  # noqa: F401
    except ImportError as exc:
        tk.Tk().withdraw()
        messagebox.showerror("Missing dependencies",
                             f"{exc}\n\nInstall them with:\n    pip install -r requirements.txt")
        sys.exit(1)
    # Imported only after the checks: the GUI pulls numpy and pyaudiowpatch in via the controller.
    from normie.gui.app import App

    if sys.stderr is None:  # .exe without a console (PyInstaller --windowed): print/traceback would fail
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
    except Exception:
        pass
    if not _claim_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, f"{APP_NAME} is already running — look for its icon in the tray, "
                                      "near the clock.")
        return
    app = App()
    app.mainloop()
    if app.shutdown_thread is not None:
        app.shutdown_thread.join(timeout=3)
    # The config is saved and Discord is disconnected. If the audio driver hung in a
    # native thread, it would keep the process alive with no window: exit for good.
    os._exit(0)


if __name__ == "__main__":
    main()
