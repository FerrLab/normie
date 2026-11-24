"""Who is in the call, who is speaking, and which volume each person should get."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from normie.config import CHANGE_FLASH_S, SILENCE_DB, SPEAKING_SETTLE_S
from normie.levels import compute_new_volume, power_to_db, volume_to_db


@dataclass
class Member:
    user_id: str
    name: str
    volume: int = 100
    muted: bool = False            # you muted this person locally
    locked: bool = False           # never adjust automatically
    speaking: bool = False
    heard_db: float | None = None  # level heard in the last window
    source_db: float | None = None  # estimated "source" level (without your volume)
    damping: float = 1.0           # multiplies cfg["speed"]; drops when an adjustment overshoots
    last_error: float | None = None
    adjusted_last: bool = False
    change: int = 0                # +1/-1: direction of the last adjustment (UI arrow)
    changed_at: float = -1e9
    acc_energy: float = 0.0
    acc_seconds: float = 0.0

    def reset_window(self) -> None:
        self.acc_energy = self.acc_seconds = 0.0


def _display_name(voice_state: dict) -> str:
    user = voice_state.get("user") or {}
    return (voice_state.get("nick") or user.get("global_name")
            or user.get("username") or str(user.get("id", "?")))


class Engine:
    """Attributes measured audio to whoever is speaking and decides the adjustments.

    Receives Discord state (RPC thread) and audio blocks (PortAudio thread); everything
    is protected by a single lock.
    """

    def __init__(self, cfg: dict, apply_volume, log, clock=time.monotonic):
        self.cfg = cfg
        self._apply_volume = apply_volume
        self._log = log
        self._clock = clock
        self.lock = threading.Lock()
        self.self_id: str | None = None
        self.channel_name: str | None = None
        self.members: dict[str, Member] = {}
        self._speaking: set[str] = set()
        self._speaking_changed = 0.0
        self.output_db = SILENCE_DB
        self._last_block_at = 0.0

    # ── state coming from Discord ──
    def set_self(self, user_id: str | None) -> None:
        with self.lock:
            self.self_id = str(user_id) if user_id else None
            if self.self_id:
                self.members.pop(self.self_id, None)

    def set_channel(self, name: str | None, voice_states: list[dict]) -> None:
        with self.lock:
            self.channel_name = name
            self.members.clear()
            self._speaking.clear()
            for vs in voice_states:
                self._upsert(vs)

    def upsert_member(self, voice_state: dict) -> None:
        with self.lock:
            self._upsert(voice_state)

    def _upsert(self, vs: dict) -> None:
        uid = str((vs.get("user") or {}).get("id") or "")
        if not uid or uid == self.self_id:
            return
        m = self.members.get(uid)
        is_new = m is None
        if m is None:
            m = self.members[uid] = Member(uid, _display_name(vs), locked=uid in self.cfg["locked"])
        m.name = _display_name(vs)
        if vs.get("volume") is not None:
            volume = int(round(float(vs["volume"])))
            if volume != m.volume:      # changed elsewhere (e.g. you moved the slider)
                if not is_new:
                    self._log(f"{m.name}: volume changed outside the app to {volume}%")
                m.volume = volume
                m.reset_window()
                m.last_error, m.adjusted_last = None, False
        m.muted = bool(vs.get("mute"))

    def remove_member(self, user_id: str | None) -> None:
        with self.lock:
            self.members.pop(str(user_id), None)
            self._speaking.discard(str(user_id))
            self._speaking_changed = self._clock()

    def set_speaking(self, user_id: str, speaking: bool) -> None:
        user_id = str(user_id)
        with self.lock:
            (self._speaking.add if speaking else self._speaking.discard)(user_id)
            self._speaking_changed = self._clock()
            if user_id in self.members:
                self.members[user_id].speaking = speaking

    def toggle_lock(self, user_id: str) -> bool | None:
        with self.lock:
            m = self.members.get(user_id)
            if m is None:
                return None
            m.locked = not m.locked
            locked = set(self.cfg["locked"])
            (locked.add if m.locked else locked.discard)(user_id)
            self.cfg["locked"] = sorted(locked)
            return m.locked

    def reset_all(self) -> list[str]:
        with self.lock:
            for m in self.members.values():
                m.volume = 100
                m.reset_window()
                m.damping, m.last_error, m.adjusted_last, m.change = 1.0, None, False, 0
            return list(self.members)

    # ── audio ──
    def feed(self, mean_square: float, seconds: float) -> None:
        """Called from the audio thread for every block (~100 ms)."""
        now = self._clock()
        db = power_to_db(mean_square)
        decision = None
        with self.lock:
            self.output_db = db
            self._last_block_at = now
            m = self._solo_speaker(now)
            if m is None or db < self.cfg["gate_db"]:
                return
            m.acc_energy += mean_square * seconds
            m.acc_seconds += seconds
            if m.acc_seconds >= self.cfg["window_s"]:
                decision = self._close_window(m, now)
        if decision:
            uid, name, old, new, heard = decision
            self._log(f"{name}: heard {heard:.1f} dBFS → volume {old}% → {new}%")
            self._apply_volume(uid, new)

    def _solo_speaker(self, now: float) -> Member | None:
        if now - self._speaking_changed < SPEAKING_SETTLE_S:
            return None
        # Your own voice never plays on your output, so you don't count.
        others = [uid for uid in self._speaking if uid != self.self_id]
        if len(others) != 1:
            return None
        m = self.members.get(others[0])
        return None if m is None or m.muted else m

    def _close_window(self, m: Member, now: float):
        heard = power_to_db(m.acc_energy / m.acc_seconds)
        m.reset_window()
        m.heard_db = heard
        m.source_db = heard - volume_to_db(m.volume)
        if m.locked or not self.cfg["enabled"]:
            return None
        self._update_damping(m, heard - self.cfg["target_db"])
        new = compute_new_volume(heard, m.volume, dict(self.cfg, speed=self.cfg["speed"] * m.damping))
        new = None if new is None else int(max(0, min(200, round(new))))
        m.adjusted_last = new is not None and new != m.volume
        if not m.adjusted_last:
            return None
        old, m.volume = m.volume, new
        m.change, m.changed_at = (1 if new > old else -1), now
        return m.user_id, m.name, old, new, heard

    def _update_damping(self, m: Member, error: float) -> None:
        """Adaptive brake. Discord's docs don't say whether the RPC 0–200 volume is linear
        or perceptual; if it's perceptual, the real gain per step is up to ~5x larger than
        estimated and the loop oscillates. If the last adjustment overshot the target (the
        error flipped sign), halve that person's speed; while it's still short in the same
        direction, give the speed back gradually."""
        if m.adjusted_last and m.last_error is not None:
            if error * m.last_error < 0 and abs(error) > self.cfg["deadband_db"]:
                m.damping = max(0.125, m.damping * 0.5)
            elif error * m.last_error > 0:
                m.damping = min(1.0, m.damping * 1.25)
        m.last_error = error

    def snapshot(self):
        """State for the UI: (channel name, current output level, list of people)."""
        with self.lock:
            now = self._clock()
            rows = [{
                "user_id": m.user_id, "name": m.name, "speaking": m.speaking,
                "heard_db": m.heard_db, "source_db": m.source_db, "volume": m.volume,
                "locked": m.locked, "muted": m.muted,
                "change": m.change if now - m.changed_at < CHANGE_FLASH_S else 0,
            } for m in self.members.values()]
            fresh = now - self._last_block_at < 0.5
            output = self.output_db if fresh else SILENCE_DB
            channel = self.channel_name
        rows.sort(key=lambda r: r["name"].casefold())
        return channel, output, rows
