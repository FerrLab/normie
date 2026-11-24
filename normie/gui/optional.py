"""Optional dependencies: Pillow (window/tray icon) and pystray (system tray).
Without them Normie still works, just without a tray; each name is None when missing."""
from __future__ import annotations

__all__ = ["Image", "ImageDraw", "ImageTk", "pystray"]


# System tray and icon (optional: without them the app still works, just without a tray).
try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageTk = None
try:
    import pystray
except ImportError:  # pragma: no cover
    pystray = None
