"""Audio math: dBFS levels, Discord's volume scale, and the volume-correction rule."""
from __future__ import annotations

import math

from normie.config import MAX_STEP_DB, SILENCE_DB


def power_to_db(mean_square: float) -> float:
    return 10.0 * math.log10(mean_square) if mean_square > 1e-12 else SILENCE_DB


def volume_to_db(volume: float) -> float:
    """Approximate gain (dB) applied by Discord's user volume: 100 → 0 dB, 200 → +6 dB."""
    return 20.0 * math.log10(max(volume, 1.0) / 100.0)


def db_to_volume(db: float) -> float:
    """Inverse of volume_to_db: +6 dB → 200, -6 dB → 50."""
    return 100.0 * 10.0 ** (db / 20.0)


def compute_new_volume(heard_db: float, current_volume: int, cfg: dict) -> int | None:
    """Pick a friend's new volume (0–200) after a window of their speech.

    heard_db        average level (dBFS RMS) of their speech in the last window, the way
                    you HEARD it — i.e. with their current volume already applied.
    current_volume  their current volume in Discord (100 = default).
    cfg             cfg["target_db"]    target level (e.g. -24)
                    cfg["speed"]        0..1 — fraction of the error corrected per window
                    cfg["deadband_db"]  error tolerated without changing anything (e.g. 1.5)
                    cfg["min_volume"], cfg["max_volume"]  volume limits

    Returns the new volume (int), or None to leave it alone.
    Helpers: volume_to_db(v) → dB   |   db_to_volume(db) → volume
    """
    error_db = cfg["target_db"] - heard_db
    if abs(error_db) < cfg["deadband_db"]:
        return None  # close enough: don't touch it, or the volume keeps "breathing"
    # Correct only part of the error (cfg["speed"]) and at most ±6 dB at a time, so a
    # shout or a whispered sentence can't slam the volume down or up.
    step_db = max(-MAX_STEP_DB, min(MAX_STEP_DB, cfg["speed"] * error_db))
    wanted = db_to_volume(volume_to_db(current_volume) + step_db)
    return int(round(min(cfg["max_volume"], max(cfg["min_volume"], wanted))))
