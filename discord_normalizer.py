#!/usr/bin/env python3
"""
Normie — Discord voice normalizer
=================================
Automatically evens out the volume of each friend in a Discord call so everyone
sounds equally loud — on your side only.

How it works
------------
1. Connects to the Discord desktop app through its local RPC (IPC pipe), which gives us:
     - who is in the call and each person's current volume;
     - SPEAKING_START / SPEAKING_STOP events (who is talking right now);
     - the SET_USER_VOICE_SETTINGS command (the same slider as
       "right-click a user > User Volume").
2. Captures, via WASAPI loopback, whatever plays on your output device.
3. When exactly ONE friend is speaking, the measured level is theirs.
4. After each window of speech, compute_new_volume() picks their new volume.

Login: the Discord app (CLIENT_ID below) is a "Public Client", so authorization
uses PKCE — there is no client secret.

Requirements: Windows 10/11, Python 3.10+
    pip install -r requirements.txt
"""
from __future__ import annotations

import base64
import concurrent.futures
import ctypes
import hashlib
import json
import math
import os
import queue
import secrets
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

if sys.platform != "win32":
    sys.exit("Normie only runs on Windows (it uses WASAPI and Discord's IPC pipe).")

import _winapi  # noqa: E402  (PeekNamedPipe — the same one multiprocessing uses)
import msvcrt  # noqa: E402

try:
    import numpy as np
    import pyaudiowpatch as pyaudio
except ImportError as _exc:  # pragma: no cover
    tk.Tk().withdraw()
    messagebox.showerror("Missing dependencies",
                         f"{_exc}\n\nInstall them with:\n    pip install -r requirements.txt")
    sys.exit(1)

# System tray and icon (optional: without them the app still works, just without a tray).
try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageTk = None
try:
    import pystray
except ImportError:  # pragma: no cover
    pystray = None


# ─────────────────────────────── Configuration ──────────────────────────────

APP_NAME = "Normie"
APP_VERSION = "dev"  # the release workflow replaces this with the published version

# Public ID of the app in the Discord Developer Portal — not a secret. The app must
# have "Public Client" enabled and REDIRECT_URI listed under OAuth2 → Redirects.
CLIENT_ID = "1574641998332166236"
REDIRECT_URI = "http://localhost"


def _app_dir() -> Path:
    """Folder the app lives in. In the PyInstaller --onefile .exe, __file__ points to a
    temporary folder that is deleted on exit, so the config must sit next to the .exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = _app_dir() / "discord_normalizer_config.json"
OAUTH_TOKEN_URL = "https://discord.com/api/oauth2/token"
SCOPES = ["rpc", "rpc.voice.read", "rpc.voice.write"]
CHANNEL_EVENTS = ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE", "VOICE_STATE_DELETE",
                  "SPEAKING_START", "SPEAKING_STOP")

BLOCK_SECONDS = 0.1       # length of each analysed audio block
SPEAKING_SETTLE_S = 0.3   # audio arrives after the speaking event: skip the very start
MAX_STEP_DB = 6.0         # largest correction applied in a single window
CHANGE_FLASH_S = 4.0      # how long the ▲/▼ arrow stays visible after an adjustment
AUDIO_RETRY_S = 5.0       # wait between attempts to open the audio loopback
SILENCE_DB = -120.0
AUTO_DEVICE_LABEL = "Same as Discord (automatic)"
DEFAULT_DEVICE_LABEL = "Windows default output"
LOOPBACK_SUFFIX = " [Loopback]"

DEFAULT_CONFIG: dict = {
    "access_token": "",
    "refresh_token": "",
    "expires_at": 0,
    "enabled": True,
    "target_db": -24,          # target RMS speech level, in dBFS
    "speed": 0.5,              # fraction of the error corrected per window (0..1)
    "deadband_db": 1.5,        # error tolerated without touching the volume
    "min_volume": 25,
    "max_volume": 200,
    "window_s": 2.0,           # seconds of "solo" speech per measurement
    "gate_db": -55.0,          # blocks below this are pauses, not speech
    "device_mode": "discord",  # "discord" = capture the device Discord plays on; "manual"
    "device": "",              # manual choice: loopback device name ("" = Windows default)
    "locked": [],              # user_ids that must not be adjusted
    "minimize_to_tray": True,  # minimizing hides the window in the tray
}

_config_lock = threading.Lock()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as exc:
        print(f"Ignoring config file ({exc})", file=sys.stderr)
        return cfg
    # Older versions stored client_id/secret in the config. Tokens issued to a
    # different app are useless for this one, so they are dropped.
    old_client = saved.pop("client_id", CLIENT_ID)
    saved.pop("client_secret", None)
    saved.pop("redirect_uri", None)
    if old_client and old_client != CLIENT_ID:
        saved.update(access_token="", refresh_token="", expires_at=0)
    # Before "device_mode" existed, a non-empty "device" meant someone picked it by hand.
    if "device_mode" not in saved and saved.get("device"):
        saved["device_mode"] = "manual"
    cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    return cfg


def save_config(cfg: dict) -> None:
    with _config_lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_PATH)


# ──────────────────────────────── Audio math ────────────────────────────────

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


# ──────────────────────────────── Engine ────────────────────────────────────

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


# ──────────────────────────── Discord local RPC ─────────────────────────────

class RPCError(Exception):
    def __init__(self, message: str, code=None):
        super().__init__(f"{message} (code {code})" if code else message)
        self.code = code


class DiscordRPC:
    r"""Minimal client for Discord's local RPC (\\?\pipe\discord-ipc-N).

    Each frame = opcode (uint32 LE) + length (uint32 LE) + UTF-8 JSON.

    A single thread does all pipe I/O: on synchronous Windows handles, a pending
    ReadFile would block any WriteFile issued from another thread. So we use
    PeekNamedPipe and only read when bytes are already waiting in the pipe.
    """
    OP_HANDSHAKE, OP_FRAME, OP_CLOSE, OP_PING, OP_PONG = range(5)

    def __init__(self, client_id: str, on_event, on_close, pipe_paths: list[str] | None = None):
        self.client_id = str(client_id)
        self._on_event = on_event
        self._on_close = on_close
        self._pipe_paths = pipe_paths or [rf"\\?\pipe\discord-ipc-{i}" for i in range(10)]
        self._pipe = None
        self._handle = None
        self._outq: queue.Queue = queue.Queue()
        self._pending: dict[str, concurrent.futures.Future] = {}
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.user: dict = {}

    # ── lifecycle ──
    def connect(self, timeout: float = 10.0) -> None:
        last_error = None
        for path in self._pipe_paths:
            try:
                self._pipe = open(path, "r+b", buffering=0)  # noqa: SIM115
                break
            except OSError as exc:
                last_error = exc
        else:
            raise ConnectionError("Couldn't find Discord. Is the desktop app running?") from last_error
        self._handle = msvcrt.get_osfhandle(self._pipe.fileno())
        try:
            self._write(self.OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
            op, data = self._read_frame(time.monotonic() + timeout)
        except Exception:
            self._close_pipe()
            raise
        if op == self.OP_CLOSE or data.get("evt") != "READY":
            self._close_pipe()
            msg = data.get("message") or (data.get("data") or {}).get("message") or "handshake refused"
            raise RPCError(f"Discord refused the connection: {msg}", data.get("code"))
        self.user = (data.get("data") or {}).get("user") or {}
        self._thread = threading.Thread(target=self._io_loop, name="discord-rpc", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is None:
            self._close_pipe()
        elif self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    # ── API ──
    def request(self, cmd: str, args: dict | None = None, evt: str | None = None, timeout: float = 10.0):
        if not self.alive:
            raise ConnectionError("Not connected to Discord")
        nonce = str(uuid.uuid4())
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._pending_lock:
            self._pending[nonce] = fut
        payload = {"cmd": cmd, "args": args or {}, "nonce": nonce}
        if evt:
            payload["evt"] = evt
        self._outq.put((self.OP_FRAME, payload))
        try:
            data = fut.result(timeout)
        except concurrent.futures.TimeoutError:
            with self._pending_lock:
                self._pending.pop(nonce, None)
            raise TimeoutError(f"{cmd}: Discord didn't respond") from None
        if data.get("evt") == "ERROR":
            err = data.get("data") or {}
            raise RPCError(f"{cmd}: {err.get('message', 'unknown error')}", err.get("code"))
        return data.get("data")

    # ── I/O (only on the RPC thread, except for the handshake) ──
    def _io_loop(self) -> None:
        error: BaseException | None = None
        try:
            while not self._stop.is_set():
                idle = True
                while True:
                    try:
                        op, payload = self._outq.get_nowait()
                    except queue.Empty:
                        break
                    self._write(op, payload)
                    idle = False
                if self._available():
                    op, data = self._read_frame(time.monotonic() + 5)
                    self._dispatch(op, data)
                    idle = False
                if idle:
                    time.sleep(0.01)
        except Exception as exc:  # broken pipe, Discord closed, etc.
            error = exc
        finally:
            stopped_by_us = self._stop.is_set()
            self._stop.set()
            self._close_pipe()
            self._fail_pending(error or ConnectionError("Connection closed"))
            if not stopped_by_us:
                self._on_close(error)

    def _dispatch(self, op: int, data: dict) -> None:
        if op == self.OP_PING:
            self._write(self.OP_PONG, data)
            return
        if op == self.OP_CLOSE:
            raise ConnectionError(data.get("message") or "Discord closed the connection")
        nonce = data.get("nonce")
        if nonce:
            with self._pending_lock:
                fut = self._pending.pop(nonce, None)
            if fut is not None:
                fut.set_result(data)
                return
        if data.get("cmd") == "DISPATCH" and data.get("evt"):
            try:
                self._on_event(data["evt"], data.get("data") or {})
            except Exception:  # one bad event must not drop the connection
                traceback.print_exc()

    def _available(self) -> int:
        return _winapi.PeekNamedPipe(self._handle, 0)[0]

    def _read_exact(self, n: int, deadline: float) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            avail = self._available()
            if avail:
                chunk = self._pipe.read(min(avail, n - len(buf)))
                if not chunk:
                    raise ConnectionError("pipe closed")
                buf += chunk
            elif time.monotonic() > deadline:
                raise TimeoutError("Discord didn't respond")
            else:
                time.sleep(0.005)
        return bytes(buf)

    def _read_frame(self, deadline: float) -> tuple[int, dict]:
        op, length = struct.unpack("<II", self._read_exact(8, deadline))
        body = self._read_exact(length, deadline + 5) if length else b"{}"
        return op, json.loads(body.decode("utf-8"))

    def _write(self, op: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        view = memoryview(struct.pack("<II", op, len(body)) + body)
        while view:
            view = view[self._pipe.write(view):]

    def _close_pipe(self) -> None:
        if self._pipe is not None:
            try:
                self._pipe.close()
            except OSError:
                pass
            self._pipe = None

    def _fail_pending(self, exc: BaseException) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)


# ───────────────────────────── OAuth2 (PKCE) ────────────────────────────────

def pkce_challenge(verifier: str) -> str:
    """PKCE (RFC 7636): base64url(sha256(verifier)), without '=' padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge). The 43-char base64url verifier never leaves the PC
    until the code exchange; Discord only sees the challenge in AUTHORIZE."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    return verifier, pkce_challenge(verifier)


def oauth_token(**fields) -> dict:
    """Exchange a code (or refresh_token) for an access_token with Discord's API.
    The app is a "Public Client": there is no client_secret; the code exchange carries
    the code_verifier instead."""
    body = urllib.parse.urlencode({"client_id": CLIENT_ID, **fields}).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        # urllib's default User-Agent is blocked by Discord's Cloudflare (error 1010).
        "User-Agent": "Normie/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        hint = ""
        if "invalid_client" in detail or "unauthorized_client" in detail:
            hint = " — check that “Public Client” is enabled under OAuth2 in the Developer Portal"
        raise RuntimeError(f"OAuth failed (HTTP {exc.code}): {detail}{hint}") from None


# ───────────────────────────── Audio capture ────────────────────────────────

def discord_output_name(device_id: str | None, devices: list[dict]) -> str:
    """Name of the output device Discord plays on ("" when unknown).

    When Discord is set to "Default", that entry is named like
    "Default (Headphones (Elgato Wave:3))". That device is Discord's own idea of the
    default and may differ from the Windows default PortAudio reports, so the real device
    is read from inside the outer parentheses (this works in any Discord language)."""
    name = next((d.get("name") or "" for d in devices if d.get("id") == device_id), "")
    if device_id == "default":
        name = name[name.index("(") + 1:-1] if "(" in name and name.endswith(")") else ""
    return name.strip()


def _norm_device(name: str) -> str:
    return " ".join(name.removesuffix(LOOPBACK_SUFFIX).split()).casefold()


def match_loopback(output_name: str, loopback_names: list[str]) -> str | None:
    """The loopback device that captures `output_name` (a Discord output device name),
    or None. WASAPI loopback devices are named "<output device> [Loopback]"."""
    want = _norm_device(output_name)
    if not want:
        return None
    for name in loopback_names:
        if _norm_device(name) == want:
            return name
    for name in loopback_names:  # some APIs shorten long device names
        have = _norm_device(name)
        if have and (want.startswith(have) or have.startswith(want)):
            return name
    return None


class LoopbackMeter:
    """Captures, via WASAPI loopback, whatever plays on an output device.

    WASAPI is COM-based and PortAudio only works on threads where COM is initialized:
    opening a stream from any other thread fails with "-9999 Unanticipated host error".
    So every PortAudio call (init, device list, open, close, terminate) runs on one
    dedicated audio thread, and the public methods just hand work to it."""

    def __init__(self, on_block, log):
        self._on_block = on_block
        self._log = log
        self._pa = None
        self._stream = None
        self._devices: list[str] = []
        self._callback_failed = False
        self._jobs: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        threading.Thread(target=self._run, name="audio", daemon=True).start()
        self._ready.wait(10)

    def _run(self) -> None:
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED, for WASAPI
        try:
            self._pa = pyaudio.PyAudio()
            self._devices = [d["name"] for d in self._pa.get_loopback_device_info_generator()]
        except Exception as exc:
            self._log(f"Couldn't initialize audio: {exc}")
        finally:
            self._ready.set()
        while True:
            job, fut = self._jobs.get()
            if job is None:
                break
            try:
                fut.set_result(job())
            except Exception as exc:
                fut.set_exception(exc)

    def _call(self, job, timeout: float | None = None):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((job, fut))
        return fut.result(timeout)

    def loopback_devices(self) -> list[str]:
        return list(self._devices)

    def start(self, name: str = "") -> None:
        self._call(lambda: self._open(name))

    def stop(self) -> None:
        self._call(self._close)

    def terminate(self, timeout: float = 2.5) -> None:
        try:
            self._call(self._shutdown, timeout)
        except Exception:  # driver stuck: main() still exits the process
            pass
        self._jobs.put((None, None))

    # ── everything below runs on the audio thread ──
    def _pick(self, name: str) -> dict:
        devices = list(self._pa.get_loopback_device_info_generator())
        if not devices:
            raise RuntimeError("No WASAPI loopback device found.")
        if name:
            for d in devices:
                if d["name"] == name:
                    return d
            self._log(f"Device '{name}' not found; using the default output.")
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        for d in devices:
            if default_out["name"] in d["name"]:
                return d
        return devices[0]

    def _open(self, name: str) -> None:
        if self._pa is None:
            raise RuntimeError("audio isn't initialized")
        self._close()
        dev = self._pick(name)
        rate = int(dev["defaultSampleRate"])
        channels = max(1, int(dev["maxInputChannels"]))

        def callback(in_data, frame_count, _time_info, _status):
            try:
                samples = np.frombuffer(in_data, dtype=np.float32)
                if samples.size:
                    self._on_block(float(np.mean(samples * samples)), frame_count / rate)
            except Exception as exc:
                if not self._callback_failed:  # only log the first one
                    self._callback_failed = True
                    self._log(f"Error processing audio: {exc!r}")
            return None, pyaudio.paContinue

        self._stream = self._pa.open(
            format=pyaudio.paFloat32, channels=channels, rate=rate, input=True,
            input_device_index=dev["index"], frames_per_buffer=int(rate * BLOCK_SECONDS),
            stream_callback=callback,
        )
        self._log(f"Listening: {dev['name']} ({rate} Hz, {channels} channels)")

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _shutdown(self) -> None:
        self._close()
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None


# ─────────────────────────────── Controller ─────────────────────────────────

class Controller:
    """Wires RPC, engine and audio together. Commands to Discord run on a serial worker
    thread, so they never block the GUI or the RPC I/O thread.

    set_status(text, kind) — kind is "off", "busy", "ok" or "error".
    on_output_device(name) — Discord switched to another output device ("" = unknown)."""

    RETRY_SECONDS = 10

    def __init__(self, cfg: dict, log, set_status, on_output_device=None):
        self.cfg = cfg
        self.log = log
        self._set_status = set_status
        self._on_output_device = on_output_device or (lambda _name: None)
        self.engine = Engine(cfg, self._queue_volume, log)
        self.meter = LoopbackMeter(self.engine.feed, log)
        self.rpc: DiscordRPC | None = None
        self._channel_id: str | None = None
        self.discord_output: str | None = None  # output device Discord plays on (None = not known yet)
        self._output_devices: list[dict] = []
        self.user_name = ""
        self._want_connected = False
        self._retry_timer: threading.Timer | None = None
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="discord-cmd")

    @property
    def active(self) -> bool:
        return self._want_connected or self.rpc is not None

    def _submit(self, fn, *args) -> None:
        def run():
            try:
                fn(*args)
            except Exception as exc:
                self.log(f"Error: {exc}")
        try:
            self._worker.submit(run)
        except RuntimeError:  # executor already shut down (app closing)
            pass

    # ── connection ──
    def connect(self) -> None:
        self._want_connected = True
        self._submit(self._connect)

    def disconnect(self) -> None:
        self._want_connected = False
        if self._retry_timer:
            self._retry_timer.cancel()
        self._submit(self._disconnect)

    def _connect(self) -> None:
        if self.rpc is not None or not self._want_connected:
            return
        self._set_status("Connecting to Discord…", "busy")
        rpc = DiscordRPC(CLIENT_ID, self._on_event, self._on_rpc_closed)
        try:
            rpc.connect()
            self._authenticate(rpc)
            self.rpc = rpc
            rpc.request("SUBSCRIBE", evt="VOICE_CHANNEL_SELECT")
            self._follow_output_device(rpc)
            self._load_selected_channel()
        except (ConnectionError, TimeoutError, OSError) as exc:
            rpc.close()
            self.rpc = None
            self.log(f"Discord unavailable: {exc}")
            if self._want_connected:
                self._schedule_retry()
        except Exception as exc:
            rpc.close()
            self.rpc = None
            self._want_connected = False
            self.log(f"Couldn't connect: {exc}")
            self._set_status("Connection failed — see Activity", "error")

    def _disconnect(self) -> None:
        rpc, self.rpc = self.rpc, None
        if rpc:
            rpc.close()
        self._channel_id = None
        self.engine.set_channel(None, [])
        self._set_status("Disconnected", "off")

    def _on_rpc_closed(self, error) -> None:  # RPC thread
        self.rpc = None
        self._channel_id = None
        self.engine.set_channel(None, [])
        self.log(f"Lost the connection to Discord ({error})")
        if self._want_connected:
            self._schedule_retry()
        else:
            self._set_status("Disconnected", "off")

    def _schedule_retry(self) -> None:
        if self._retry_timer:
            self._retry_timer.cancel()
        self._set_status(f"Discord is closed — retrying in {self.RETRY_SECONDS}s", "busy")
        self._retry_timer = threading.Timer(self.RETRY_SECONDS, lambda: self._submit(self._connect))
        self._retry_timer.daemon = True
        self._retry_timer.start()

    # ── OAuth2 authentication with PKCE ──
    def _authenticate(self, rpc: DiscordRPC) -> None:
        cfg = self.cfg
        if cfg["access_token"] and cfg["expires_at"] > time.time() + 60:
            try:
                return self._auth_with(rpc, cfg["access_token"])
            except RPCError as exc:
                self.log(f"Saved token rejected ({exc}); refreshing…")
        if cfg["refresh_token"]:
            try:
                tok = oauth_token(grant_type="refresh_token", refresh_token=cfg["refresh_token"])
                self._store_token(tok)
                return self._auth_with(rpc, tok["access_token"])
            except (RPCError, RuntimeError, urllib.error.URLError) as exc:
                self.log(f"Couldn't refresh the token ({exc}); asking for authorization again…")
        self._set_status("Approve the request in the Discord window…", "busy")
        verifier, challenge = make_pkce_pair()
        data = rpc.request("AUTHORIZE", {
            "client_id": CLIENT_ID, "scopes": SCOPES,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }, timeout=300)
        tok = oauth_token(grant_type="authorization_code", code=data["code"],
                          redirect_uri=REDIRECT_URI, code_verifier=verifier)
        self._store_token(tok)
        self._auth_with(rpc, tok["access_token"])

    def _auth_with(self, rpc: DiscordRPC, token: str) -> None:
        data = rpc.request("AUTHENTICATE", {"access_token": token}) or {}
        user = data.get("user") or rpc.user
        self.engine.set_self(user.get("id"))
        self.user_name = user.get("global_name") or user.get("username") or ""
        missing = set(SCOPES) - set(data.get("scopes") or SCOPES)
        if missing:
            self.log(f"Warning: the token is missing the scopes {sorted(missing)}")

    def _store_token(self, tok: dict) -> None:
        self.cfg["access_token"] = tok["access_token"]
        self.cfg["refresh_token"] = tok.get("refresh_token", "")
        self.cfg["expires_at"] = time.time() + float(tok.get("expires_in", 0))
        save_config(self.cfg)

    # ── Discord's output device ──
    def _follow_output_device(self, rpc: DiscordRPC) -> None:
        """Read which device Discord plays on and get told whenever it changes."""
        self.discord_output = None
        try:
            rpc.request("SUBSCRIBE", evt="VOICE_SETTINGS_UPDATE")
            self._apply_voice_settings(rpc.request("GET_VOICE_SETTINGS"))
        except RPCError as exc:
            self.log(f"Couldn't read Discord's output device ({exc}); using the Windows default")
            self.discord_output = ""
            self._on_output_device("")

    def _apply_voice_settings(self, settings: dict | None) -> None:
        out = (settings or {}).get("output")
        if not isinstance(out, dict) or "device_id" not in out:
            return  # partial update without the output device: nothing to follow
        if out.get("available_devices"):
            self._output_devices = out["available_devices"]
        name = discord_output_name(out["device_id"], self._output_devices)
        if name != self.discord_output:
            self.discord_output = name
            self._on_output_device(name)

    # ── voice channel ──
    def _load_selected_channel(self) -> None:
        rpc = self.rpc
        if rpc is None:
            return
        channel = rpc.request("GET_SELECTED_VOICE_CHANNEL")
        if self._channel_id:
            for evt in CHANNEL_EVENTS:
                try:
                    rpc.request("UNSUBSCRIBE", {"channel_id": self._channel_id}, evt=evt)
                except RPCError:
                    pass
        self._channel_id = None
        who = f"Connected as {self.user_name}" if self.user_name else "Connected"
        if not channel:
            self.engine.set_channel(None, [])
            self._set_status(who, "ok")
            return
        states = channel.get("voice_states") or []
        self.engine.set_channel(channel.get("name"), states)
        for evt in CHANNEL_EVENTS:
            rpc.request("SUBSCRIBE", {"channel_id": channel["id"]}, evt=evt)
        self._channel_id = channel["id"]
        self.log(f"In call “{channel.get('name')}” with {len(states)} people")
        self._set_status(who, "ok")

    def _on_event(self, evt: str, data: dict) -> None:
        # Runs on the RPC I/O thread: only update state here, never call request().
        if evt == "VOICE_CHANNEL_SELECT":
            self._submit(self._load_selected_channel)
        elif evt == "VOICE_SETTINGS_UPDATE":
            self._apply_voice_settings(data)
        elif evt in ("SPEAKING_START", "SPEAKING_STOP"):
            self.engine.set_speaking(data.get("user_id"), evt == "SPEAKING_START")
        elif evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE"):
            self.engine.upsert_member(data)
        elif evt == "VOICE_STATE_DELETE":
            self.engine.remove_member((data.get("user") or {}).get("id"))

    # ── volume ──
    def _queue_volume(self, user_id: str, volume: int) -> None:
        self._submit(self._set_volume, user_id, volume)

    def _set_volume(self, user_id: str, volume: int) -> None:
        if self.rpc is not None:
            self.rpc.request("SET_USER_VOICE_SETTINGS", {"user_id": user_id, "volume": volume})

    def reset_volumes(self) -> int:
        ids = self.engine.reset_all()
        for uid in ids:
            self._queue_volume(uid, 100)
        return len(ids)

    def shutdown(self) -> None:
        self._want_connected = False
        if self._retry_timer:
            self._retry_timer.cancel()
        if self.rpc:  # Discord first: it restores the original volumes on disconnect
            self.rpc.close()
        self._worker.shutdown(wait=False, cancel_futures=True)
        self.meter.terminate()  # last: the WASAPI driver is sometimes slow to let go


# ─────────────────────────────────── GUI ────────────────────────────────────

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


def _ellipsize(text: str, limit: int) -> str:
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
        self.name.configure(text=_ellipsize(r["name"], 24))

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
        self.ui_calls: queue.Queue = queue.Queue()  # functions coming from other threads (tray, RPC)
        self.ctrl = Controller(self.cfg, self.log, self.set_status,
                               on_output_device=lambda name: self.ui_calls.put(
                                   lambda: self._discord_output_changed(name)))
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
        """Dark title bar on Windows 10/11 (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            on = ctypes.c_int(1)
            for attr in (20, 19):  # 20 since Windows 10 20H1; 19 before that
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on),
                                                              ctypes.sizeof(on)) == 0:
                    break
        except Exception:
            pass

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

        tray_row = tk.Frame(body, bg=Theme.CARD)
        tray_row.grid(row=4, column=0, columnspan=3, sticky="w", pady=(px(14), 0))
        self.var_tray = tk.BooleanVar(value=self.cfg["minimize_to_tray"])
        Toggle(tray_row, self.var_tray, self._settings_changed, px, Theme.CARD).pack(side="left")
        self._label(tray_row, "Minimizing hides Normie in the tray (icon near the clock)").pack(
            side="left", padx=px(12))

        self._label(body, "Tip: put Discord on one output and games/music on another — the "
                          "measurements get cleaner.", muted=True, font=self.f_small).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(px(12), 0))

    # ── actions ──
    def _toggle_connect(self) -> None:
        if self.ctrl.active:
            self.ctrl.disconnect()
        else:
            self.ctrl.connect()

    def toggle_lock(self, user_id: str) -> None:
        if self.ctrl.engine.toggle_lock(user_id) is not None:
            self._schedule_save()

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
        self.lift()
        self.attributes("-topmost", True)  # Windows won't let us steal focus from another window
        self.after(100, lambda: self.attributes("-topmost", False))
        self.focus_force()

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
            text=f"IN CALL  ·  {_ellipsize(channel, 40).upper()}  ·  {len(rows)}" if channel else "IN CALL")
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
        self.activity.set_hint(_ellipsize(lines[-1].split("] ", 1)[-1], 90))

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
