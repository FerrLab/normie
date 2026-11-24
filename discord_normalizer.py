#!/usr/bin/env python3
"""
Discord Voice Normalizer
========================
Ajusta sozinho o volume de cada amigo numa call do Discord para que todos
soem com a mesma altura — só para você.

Como funciona
-------------
1. Conecta no Discord aberto no seu PC via RPC local (pipe IPC). O RPC nos dá:
     - quem está na call e o volume atual de cada um;
     - eventos SPEAKING_START / SPEAKING_STOP (quem está falando agora);
     - o comando SET_USER_VOICE_SETTINGS (o mesmo slider de
       "clique direito > Volume do usuário").
2. Escuta em loopback (WASAPI) o som que sai no seu dispositivo de saída.
3. Quando exatamente UM amigo está falando, o nível medido é dele.
4. A cada janela de fala, compute_new_volume() decide o novo volume dele.

Login: o app do Discord (CLIENT_ID abaixo) é um "Public Client", então a
autorização usa PKCE — não existe Client Secret.

Requisitos: Windows 10/11, Python 3.10+
    pip install pyaudiowpatch numpy
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
    sys.exit("Este script funciona apenas no Windows (usa WASAPI e o pipe IPC do Discord).")

import _winapi  # noqa: E402  (PeekNamedPipe — o mesmo que multiprocessing usa)
import msvcrt  # noqa: E402

try:
    import numpy as np
    import pyaudiowpatch as pyaudio
except ImportError as _exc:  # pragma: no cover
    tk.Tk().withdraw()
    messagebox.showerror("Dependências faltando", f"{_exc}\n\nInstale com:\n    pip install -r requirements.txt")
    sys.exit(1)

# Bandeja do sistema e ícone (opcionais: sem eles o app funciona, só sem bandeja).
try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageTk = None
try:
    import pystray
except ImportError:  # pragma: no cover
    pystray = None


# ─────────────────────────────── Configuração ───────────────────────────────

APP_NAME = "Normie"
APP_VERSION = "dev"  # o workflow de release troca pela versão publicada

# ID público do app no Discord Developer Portal — não é segredo. O app precisa
# estar com "Public Client" ligado e ter o REDIRECT_URI em OAuth2 → Redirects.
CLIENT_ID = "1574641998332166236"
REDIRECT_URI = "http://localhost"

def _app_dir() -> Path:
    """Pasta do app. No .exe do PyInstaller (--onefile), __file__ aponta para uma pasta
    temporária que some ao fechar; a config tem que ficar ao lado do executável."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = _app_dir() / "discord_normalizer_config.json"
OAUTH_TOKEN_URL = "https://discord.com/api/oauth2/token"
SCOPES = ["rpc", "rpc.voice.read", "rpc.voice.write"]
CHANNEL_EVENTS = ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE", "VOICE_STATE_DELETE",
                  "SPEAKING_START", "SPEAKING_STOP")

BLOCK_SECONDS = 0.1       # tamanho de cada bloco de áudio analisado
SPEAKING_SETTLE_S = 0.3   # o áudio chega depois do evento de fala: ignora o começo
MAX_STEP_DB = 6.0         # maior correção aplicada numa única janela
CHANGE_FLASH_S = 4.0      # por quanto tempo a seta ▲/▼ fica visível após um ajuste
SILENCE_DB = -120.0
DEFAULT_DEVICE_LABEL = "Saída padrão do Windows"

DEFAULT_CONFIG: dict = {
    "access_token": "",
    "refresh_token": "",
    "expires_at": 0,
    "enabled": True,
    "target_db": -24,       # nível RMS alvo da fala, em dBFS
    "speed": 0.5,           # fração do erro corrigida a cada janela (0..1)
    "deadband_db": 1.5,     # erro tolerado sem mexer no volume
    "min_volume": 25,
    "max_volume": 200,
    "window_s": 2.0,        # segundos de fala "solo" por medição
    "gate_db": -55.0,       # blocos abaixo disso são pausas, não fala
    "device": "",           # nome do dispositivo de loopback ("" = padrão)
    "locked": [],           # user_ids que não devem ser ajustados
    "minimize_to_tray": True,  # minimizar esconde a janela na bandeja
}

_config_lock = threading.Lock()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as exc:
        print(f"Configuração ignorada ({exc})", file=sys.stderr)
        return cfg
    # Versões antigas guardavam client_id/secret na config. Tokens emitidos para
    # outro app não servem para este, então são descartados.
    old_client = saved.pop("client_id", CLIENT_ID)
    saved.pop("client_secret", None)
    saved.pop("redirect_uri", None)
    if old_client and old_client != CLIENT_ID:
        saved.update(access_token="", refresh_token="", expires_at=0)
    cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    return cfg


def save_config(cfg: dict) -> None:
    with _config_lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_PATH)


# ───────────────────────────── Matemática de áudio ──────────────────────────

def power_to_db(mean_square: float) -> float:
    return 10.0 * math.log10(mean_square) if mean_square > 1e-12 else SILENCE_DB


def volume_to_db(volume: float) -> float:
    """Ganho aproximado (dB) que o volume do Discord aplica: 100 → 0 dB, 200 → +6 dB."""
    return 20.0 * math.log10(max(volume, 1.0) / 100.0)


def db_to_volume(db: float) -> float:
    """Inverso de volume_to_db: +6 dB → 200, -6 dB → 50."""
    return 100.0 * 10.0 ** (db / 20.0)


def compute_new_volume(heard_db: float, current_volume: int, cfg: dict) -> int | None:
    """Decide o novo volume (0–200) de um amigo depois de uma janela de fala.

    heard_db        nível médio (dBFS RMS) da fala dele na última janela, do jeito
                    que você OUVIU — ou seja, já com o volume atual aplicado.
    current_volume  volume atual dele no Discord (100 = padrão).
    cfg             cfg["target_db"]    nível alvo (ex.: -24)
                    cfg["speed"]        0..1 — fração do erro corrigida por janela
                    cfg["deadband_db"]  erro tolerado sem mexer (ex.: 1.5)
                    cfg["min_volume"], cfg["max_volume"]  limites do volume

    Retorne o novo volume (int) ou None para não mexer.
    Helpers: volume_to_db(v) → dB   |   db_to_volume(db) → volume
    """
    error_db = cfg["target_db"] - heard_db
    if abs(error_db) < cfg["deadband_db"]:
        return None  # perto o bastante: não mexe, senão o volume fica "respirando"
    # Corrige só uma fração do erro (cfg["speed"]) e no máximo ±6 dB por vez,
    # para um grito ou uma frase sussurrada não derrubarem/dispararem o volume.
    step_db = max(-MAX_STEP_DB, min(MAX_STEP_DB, cfg["speed"] * error_db))
    wanted = db_to_volume(volume_to_db(current_volume) + step_db)
    return int(round(min(cfg["max_volume"], max(cfg["min_volume"], wanted))))


# ──────────────────────────────── Motor ─────────────────────────────────────

@dataclass
class Member:
    user_id: str
    name: str
    volume: int = 100
    muted: bool = False            # você silenciou essa pessoa localmente
    locked: bool = False           # não ajustar automaticamente
    speaking: bool = False
    heard_db: float | None = None  # nível ouvido na última janela
    source_db: float | None = None  # nível estimado "de origem" (sem o seu volume)
    damping: float = 1.0           # multiplica cfg["speed"]; cai quando o ajuste passa do ponto
    last_error: float | None = None
    adjusted_last: bool = False
    change: int = 0                # +1/-1: sentido do último ajuste (para a seta na tela)
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
    """Atribui o áudio medido a quem está falando e decide os ajustes.

    Recebe estado do Discord (thread do RPC) e blocos de áudio (thread do
    PortAudio); tudo protegido por um único lock.
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

    # ── estado vindo do Discord ──
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
            if volume != m.volume:      # mudou por fora (ex.: você mexeu no slider)
                if not is_new:
                    self._log(f"{m.name}: volume mudou fora do app para {volume}%")
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

    # ── áudio ──
    def feed(self, mean_square: float, seconds: float) -> None:
        """Chamado pela thread de áudio a cada bloco (~100 ms)."""
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
            self._log(f"{name}: ouvido {heard:.1f} dBFS → volume {old}% → {new}%")
            self._apply_volume(uid, new)

    def _solo_speaker(self, now: float) -> Member | None:
        if now - self._speaking_changed < SPEAKING_SETTLE_S:
            return None
        # Sua própria voz não toca na sua saída, então você não conta.
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
        """Freio adaptativo. A doc do Discord não diz se o volume 0–200 do RPC é linear
        ou perceptual; se for perceptual, o ganho real por passo é até ~5x maior que o
        estimado e o controle oscila. Se o último ajuste passou do alvo (o erro trocou
        de sinal), corta a velocidade daquela pessoa pela metade; se ainda falta no
        mesmo sentido, devolve aos poucos."""
        if m.adjusted_last and m.last_error is not None:
            if error * m.last_error < 0 and abs(error) > self.cfg["deadband_db"]:
                m.damping = max(0.125, m.damping * 0.5)
            elif error * m.last_error > 0:
                m.damping = min(1.0, m.damping * 1.25)
        m.last_error = error

    def snapshot(self):
        """Estado para a tela: (nome do canal, nível da saída agora, lista de pessoas)."""
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


# ──────────────────────────── RPC local do Discord ──────────────────────────

class RPCError(Exception):
    def __init__(self, message: str, code=None):
        super().__init__(f"{message} (código {code})" if code else message)
        self.code = code


class DiscordRPC:
    r"""Cliente mínimo do RPC local do Discord (\\?\pipe\discord-ipc-N).

    Cada frame = opcode (uint32 LE) + tamanho (uint32 LE) + JSON UTF-8.

    Uma única thread faz toda a E/S do pipe: em handles síncronos do Windows,
    um ReadFile pendente bloquearia qualquer WriteFile vindo de outra thread.
    Por isso usamos PeekNamedPipe e só lemos quando já há bytes no pipe.
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

    # ── ciclo de vida ──
    def connect(self, timeout: float = 10.0) -> None:
        last_error = None
        for path in self._pipe_paths:
            try:
                self._pipe = open(path, "r+b", buffering=0)  # noqa: SIM115
                break
            except OSError as exc:
                last_error = exc
        else:
            raise ConnectionError("Não achei o Discord. O app desktop está aberto?") from last_error
        self._handle = msvcrt.get_osfhandle(self._pipe.fileno())
        try:
            self._write(self.OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
            op, data = self._read_frame(time.monotonic() + timeout)
        except Exception:
            self._close_pipe()
            raise
        if op == self.OP_CLOSE or data.get("evt") != "READY":
            self._close_pipe()
            msg = data.get("message") or (data.get("data") or {}).get("message") or "handshake recusado"
            raise RPCError(f"Discord recusou a conexão: {msg}", data.get("code"))
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
            raise ConnectionError("Não conectado ao Discord")
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
            raise TimeoutError(f"{cmd}: o Discord não respondeu") from None
        if data.get("evt") == "ERROR":
            err = data.get("data") or {}
            raise RPCError(f"{cmd}: {err.get('message', 'erro desconhecido')}", err.get("code"))
        return data.get("data")

    # ── E/S (só na thread do RPC, exceto o handshake) ──
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
        except Exception as exc:  # pipe quebrado, Discord fechou, etc.
            error = exc
        finally:
            stopped_by_us = self._stop.is_set()
            self._stop.set()
            self._close_pipe()
            self._fail_pending(error or ConnectionError("Conexão encerrada"))
            if not stopped_by_us:
                self._on_close(error)

    def _dispatch(self, op: int, data: dict) -> None:
        if op == self.OP_PING:
            self._write(self.OP_PONG, data)
            return
        if op == self.OP_CLOSE:
            raise ConnectionError(data.get("message") or "o Discord encerrou a conexão")
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
            except Exception:  # um evento com problema não derruba a conexão
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
                    raise ConnectionError("pipe fechado")
                buf += chunk
            elif time.monotonic() > deadline:
                raise TimeoutError("o Discord não respondeu")
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
    """PKCE (RFC 7636): base64url(sha256(verifier)), sem o '=' de padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """Gera (verifier, challenge). O verifier tem 43 caracteres base64url e nunca sai do PC
    até a troca do code; o Discord só vê o challenge no AUTHORIZE."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    return verifier, pkce_challenge(verifier)


def oauth_token(**fields) -> dict:
    """Troca um code (ou refresh_token) por um access_token na API do Discord.
    O app é "Public Client": não há client_secret, a troca do code leva o code_verifier."""
    body = urllib.parse.urlencode({"client_id": CLIENT_ID, **fields}).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        # O User-Agent padrão do urllib é bloqueado pelo Cloudflare do Discord (erro 1010).
        "User-Agent": "DiscordVoiceNormalizer/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        hint = ""
        if "invalid_client" in detail or "unauthorized_client" in detail:
            hint = " — confira se “Public Client” está ligado em OAuth2 no Developer Portal"
        raise RuntimeError(f"OAuth falhou (HTTP {exc.code}): {detail}{hint}") from None


# ─────────────────────────── Captura de áudio ───────────────────────────────

class LoopbackMeter:
    """Captura em loopback (WASAPI) o que toca num dispositivo de saída."""

    def __init__(self, on_block, log):
        self._on_block = on_block
        self._log = log
        self._pa = pyaudio.PyAudio()
        self._stream = None
        self._callback_failed = False
        self._lock = threading.Lock()  # start/stop podem vir de threads diferentes

    def loopback_devices(self) -> list[str]:
        return [d["name"] for d in self._pa.get_loopback_device_info_generator()]

    def _pick(self, name: str) -> dict:
        devices = list(self._pa.get_loopback_device_info_generator())
        if not devices:
            raise RuntimeError("Nenhum dispositivo de loopback WASAPI encontrado.")
        if name:
            for d in devices:
                if d["name"] == name:
                    return d
            self._log(f"Dispositivo '{name}' não encontrado; usando a saída padrão.")
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        for d in devices:
            if default_out["name"] in d["name"]:
                return d
        return devices[0]

    def start(self, name: str = "") -> None:
        with self._lock:
            self._start_locked(name)

    def _start_locked(self, name: str) -> None:
        self._stop_locked()
        dev = self._pick(name)
        rate = int(dev["defaultSampleRate"])
        channels = max(1, int(dev["maxInputChannels"]))

        def callback(in_data, frame_count, _time_info, _status):
            try:
                samples = np.frombuffer(in_data, dtype=np.float32)
                if samples.size:
                    self._on_block(float(np.mean(samples * samples)), frame_count / rate)
            except Exception as exc:
                if not self._callback_failed:  # loga só a primeira vez
                    self._callback_failed = True
                    self._log(f"Erro processando áudio: {exc!r}")
            return None, pyaudio.paContinue

        self._stream = self._pa.open(
            format=pyaudio.paFloat32, channels=channels, rate=rate, input=True,
            input_device_index=dev["index"], frames_per_buffer=int(rate * BLOCK_SECONDS),
            stream_callback=callback,
        )
        self._log(f"Ouvindo: {dev['name']} ({rate} Hz, {channels} canais)")

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def terminate(self) -> None:
        with self._lock:
            self._stop_locked()
            self._pa.terminate()


# ─────────────────────────────── Controlador ────────────────────────────────

class Controller:
    """Liga RPC, motor e áudio. Comandos ao Discord rodam numa thread de trabalho
    serial, para nunca bloquear a GUI nem a thread de E/S do RPC.

    set_status(texto, tipo) — tipo é "off", "busy", "ok" ou "error"."""

    RETRY_SECONDS = 10

    def __init__(self, cfg: dict, log, set_status):
        self.cfg = cfg
        self.log = log
        self._set_status = set_status
        self.engine = Engine(cfg, self._queue_volume, log)
        self.meter = LoopbackMeter(self.engine.feed, log)
        self.rpc: DiscordRPC | None = None
        self._channel_id: str | None = None
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
                self.log(f"Erro: {exc}")
        try:
            self._worker.submit(run)
        except RuntimeError:  # executor já encerrado (app fechando)
            pass

    # ── conexão ──
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
        self._set_status("Conectando ao Discord…", "busy")
        rpc = DiscordRPC(CLIENT_ID, self._on_event, self._on_rpc_closed)
        try:
            rpc.connect()
            self._authenticate(rpc)
            self.rpc = rpc
            rpc.request("SUBSCRIBE", evt="VOICE_CHANNEL_SELECT")
            self._load_selected_channel()
        except (ConnectionError, TimeoutError, OSError) as exc:
            rpc.close()
            self.rpc = None
            self.log(f"Discord indisponível: {exc}")
            if self._want_connected:
                self._schedule_retry()
        except Exception as exc:
            rpc.close()
            self.rpc = None
            self._want_connected = False
            self.log(f"Falha ao conectar: {exc}")
            self._set_status("Falha ao conectar — veja Atividade", "error")

    def _disconnect(self) -> None:
        rpc, self.rpc = self.rpc, None
        if rpc:
            rpc.close()
        self._channel_id = None
        self.engine.set_channel(None, [])
        self._set_status("Desconectado", "off")

    def _on_rpc_closed(self, error) -> None:  # thread do RPC
        self.rpc = None
        self._channel_id = None
        self.engine.set_channel(None, [])
        self.log(f"Conexão com o Discord perdida ({error})")
        if self._want_connected:
            self._schedule_retry()
        else:
            self._set_status("Desconectado", "off")

    def _schedule_retry(self) -> None:
        if self._retry_timer:
            self._retry_timer.cancel()
        self._set_status(f"Discord fechado — tentando de novo em {self.RETRY_SECONDS}s", "busy")
        self._retry_timer = threading.Timer(self.RETRY_SECONDS, lambda: self._submit(self._connect))
        self._retry_timer.daemon = True
        self._retry_timer.start()

    # ── autenticação OAuth2 com PKCE ──
    def _authenticate(self, rpc: DiscordRPC) -> None:
        cfg = self.cfg
        if cfg["access_token"] and cfg["expires_at"] > time.time() + 60:
            try:
                return self._auth_with(rpc, cfg["access_token"])
            except RPCError as exc:
                self.log(f"Token salvo recusado ({exc}); renovando…")
        if cfg["refresh_token"]:
            try:
                tok = oauth_token(grant_type="refresh_token", refresh_token=cfg["refresh_token"])
                self._store_token(tok)
                return self._auth_with(rpc, tok["access_token"])
            except (RPCError, RuntimeError, urllib.error.URLError) as exc:
                self.log(f"Não deu para renovar o token ({exc}); pedindo nova autorização…")
        self._set_status("Aprove o pedido na janela do Discord…", "busy")
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
            self.log(f"Aviso: o token não tem os escopos {sorted(missing)}")

    def _store_token(self, tok: dict) -> None:
        self.cfg["access_token"] = tok["access_token"]
        self.cfg["refresh_token"] = tok.get("refresh_token", "")
        self.cfg["expires_at"] = time.time() + float(tok.get("expires_in", 0))
        save_config(self.cfg)

    # ── canal de voz ──
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
        who = f"Conectado como {self.user_name}" if self.user_name else "Conectado"
        if not channel:
            self.engine.set_channel(None, [])
            self._set_status(who, "ok")
            return
        states = channel.get("voice_states") or []
        self.engine.set_channel(channel.get("name"), states)
        for evt in CHANNEL_EVENTS:
            rpc.request("SUBSCRIBE", {"channel_id": channel["id"]}, evt=evt)
        self._channel_id = channel["id"]
        self.log(f"Na call “{channel.get('name')}” com {len(states)} pessoa(s)")
        self._set_status(who, "ok")

    def _on_event(self, evt: str, data: dict) -> None:
        # Roda na thread de E/S do RPC: só atualiza estado, nunca chama request() aqui.
        if evt == "VOICE_CHANNEL_SELECT":
            self._submit(self._load_selected_channel)
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
        if self.rpc:  # primeiro o Discord: ele devolve os volumes originais ao desconectar
            self.rpc.close()
        self._worker.shutdown(wait=False, cancel_futures=True)
        self.meter.terminate()  # por último: o driver WASAPI às vezes demora para soltar


# ─────────────────────────────────── GUI ────────────────────────────────────

class Theme:
    """Paleta no estilo do Discord (modo escuro)."""
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


def make_icon_image(size: int = 64):
    """Ícone do app: quadrado azul do Discord com barras de áudio brancas.
    Desenha 4x maior e reduz, para as bordas ficarem suaves."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 4, fill=Theme.ACCENT)
    heights = (0.30, 0.54, 0.54, 0.30)
    bar_w, gap = s * 0.11, s * 0.07
    x = (s - (len(heights) * bar_w + (len(heights) - 1) * gap)) / 2
    for h in heights:
        draw.rounded_rectangle((x, s * (1 - h) / 2, x + bar_w, s * (1 + h) / 2), radius=bar_w / 2, fill="white")
        x += bar_w + gap
    return img.resize((size, size), Image.Resampling.LANCZOS)


STATUS_COLORS = {"off": Theme.FAINT, "busy": Theme.YELLOW, "ok": Theme.GREEN, "error": Theme.RED}
BUTTON_COLORS = {  # tipo: (fundo, fundo no hover, texto)
    "accent": (Theme.ACCENT, Theme.ACCENT_HOVER, "#ffffff"),
    "ghost": (Theme.INPUT, Theme.BORDER, Theme.TEXT),
}


def _ellipsize(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """Retângulo de cantos arredondados (polígono suavizado com cantos duplicados)."""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = (x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
           x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1)
    return canvas.create_polygon(pts, smooth=True, **kw)


class FlatButton(tk.Label):
    """Botão plano com hover (tk.Button nativo do Windows ignora boa parte das cores)."""

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
    """Interruptor no estilo do Discord (verde = ligado)."""

    def __init__(self, parent, variable: tk.BooleanVar, command, px, bg):
        # (não usar self._w: o tkinter guarda ali o caminho Tcl do widget)
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
    """Slider plano: trilho arredondado, parte preenchida em azul e botão branco
    (o ttk.Scale do Windows não aceita esse visual)."""

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
    """Barra de nível (−60…0 dBFS) com a marca do alvo."""
    LO, HI = -60.0, 0.0

    def __init__(self, parent, px, bg, height=8):
        super().__init__(parent, height=px(height), bg=bg, highlightthickness=0)
        self._state: tuple = (None, -24.0, Theme.GREEN)
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, value_db: float | None, target_db: float, color: str) -> None:
        if value_db is not None:
            value_db = round(value_db * 2) / 2  # 0,5 dB: evita redesenhar à toa
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
    """Lista vertical rolável (Canvas + Frame interno); a barra só aparece quando precisa."""

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
    """Seção que abre/fecha clicando no título."""

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
    """Cartão de uma pessoa na call: avatar, nome, nível ouvido vs. alvo, volume e trava."""

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
            sub, sub_color = "mutado por você", Theme.FAINT
        elif r["speaking"]:
            sub, sub_color = "falando…", Theme.GREEN
        elif r["source_db"] is not None:
            sub, sub_color = f"microfone chega a {r['source_db']:.0f} dB", Theme.MUTED
        else:
            sub, sub_color = "esperando falar sozinho", Theme.FAINT
        self.sub.configure(text=sub, fg=sub_color)

        heard = r["heard_db"]
        if heard is None:
            color, level = Theme.FAINT, "sem medição ainda"
        else:
            diff = heard - target
            if abs(diff) <= deadband:
                color, level = Theme.GREEN, f"ouvindo {heard:.1f} dB · no alvo"
            elif diff < 0:
                color, level = Theme.YELLOW, f"ouvindo {heard:.1f} dB · baixo"
            else:
                color, level = Theme.RED, f"ouvindo {heard:.1f} dB · alto"
        self.bar.set(heard, target, Theme.FAINT if inactive and heard is not None else color)
        self.level.configure(text=level)

        self.volume.configure(text=f"{r['volume']}%", fg=Theme.FAINT if inactive else Theme.TEXT)
        change = r["change"]
        self.arrow.configure(text="▲" if change > 0 else ("▼" if change < 0 else ""),
                             fg=Theme.GREEN if change > 0 else Theme.YELLOW)
        if r["locked"]:
            self.chip.configure(text="TRAVADO", bg=Theme.INPUT, fg=Theme.MUTED)
        else:
            self.chip.configure(text="AUTO", bg=Theme.ACCENT, fg="#ffffff")

    def _draw_avatar(self, name: str, speaking: bool) -> None:
        c, s, ring = self.avatar, self._avatar_size, self.app.px(3)
        c.delete("all")
        if speaking:  # anel verde, como no Discord
            c.create_oval(0, 0, s, s, fill=Theme.GREEN, outline="")
            c.create_oval(ring, ring, s - ring, s - ring, fill=Theme.ROW, outline="")
        inset = ring + self.app.px(2)
        color = Theme.AVATARS[zlib.crc32(self.uid.encode()) % len(Theme.AVATARS)]
        c.create_oval(inset, inset, s - inset, s - inset, fill=color, outline="")
        c.create_text(s / 2, s / 2, text=(name.strip()[:1] or "?").upper(), fill="#ffffff",
                      font=self.app.f_bold)


class Tray:
    """Ícone na bandeja do Windows. O pystray roda o próprio loop numa thread; como o
    tkinter não pode ser chamado de outra thread, cada clique vira uma função na fila
    app.ui_calls, que a GUI executa no _tick."""

    def __init__(self, app: App):
        self.app = app
        self.icon = None
        self._tooltip = ""
        self._notified = False
        if pystray is None or Image is None:
            app.log("Bandeja indisponível — instale com:  pip install pystray pillow")
            return
        menu = pystray.Menu(
            pystray.MenuItem(f"Abrir {APP_NAME}", self._post(app.show_window), default=True),
            pystray.MenuItem("Normalização automática", self._post(app.toggle_enabled),
                             checked=lambda _item: bool(app.cfg["enabled"])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", self._post(app.quit_app)),
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
            self.icon.title = text[:127]  # limite do Windows

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
        self._status: tuple[str, str] = ("Desconectado", "off")
        self._dot_kind = ""
        self._save_job = None
        self._rows: dict[str, MemberRow] = {}
        self._row_order: list[str] = []
        self._empty_shown = False
        self._closing = False
        self.shutdown_thread: threading.Thread | None = None
        self.ui_calls: queue.Queue = queue.Queue()  # funções vindas de outras threads (bandeja)
        self.ctrl = Controller(self.cfg, self.log, self.set_status)
        self._init_style()
        self._build()
        self._set_window_icon()
        self._dark_titlebar()
        self._start_meter()
        self.tray = Tray(self)
        self.bind("<Unmap>", self._on_unmap)
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.after(150, self._tick)
        if self.cfg["refresh_token"]:
            self.ctrl.connect()  # já autorizado antes: conecta sozinho

    def px(self, n: float) -> int:
        """Converte pixels de design (tela 100%) para a escala real do monitor."""
        return int(round(n * self._scale))

    # chamados de qualquer thread
    def log(self, msg: str) -> None:
        self._logq.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def set_status(self, text: str, kind: str = "busy") -> None:
        self._status = (text, kind)

    # ── aparência ──
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
            self._icon_photo = ImageTk.PhotoImage(make_icon_image(64))  # guardar a referência
            self.iconphoto(True, self._icon_photo)

    def _dark_titlebar(self) -> None:
        """Barra de título escura no Windows 10/11 (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            on = ctypes.c_int(1)
            for attr in (20, 19):  # 20 a partir do Windows 10 20H1; 19 antes disso
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

        # cabeçalho: título, status e o único botão de conexão
        head = tk.Frame(self, bg=Theme.BG)
        head.pack(fill="x", pady=(px(20), px(14)), **side)
        titles = tk.Frame(head, bg=Theme.BG)
        titles.pack(side="left")
        self._label(titles, APP_NAME, font=self.f_title).pack(anchor="w")
        self._label(titles, "Equaliza o volume de cada pessoa na sua call do Discord — só para você.",
                    muted=True, font=self.f_small).pack(anchor="w")
        self.btn_connect = FlatButton(head, "Conectar", self._toggle_connect, "accent",
                                      self.f_bold, px(20), px(9))
        self.btn_connect.pack(side="right")
        status = tk.Frame(head, bg=Theme.BG)
        status.pack(side="right", padx=px(16))
        self.status_dot = tk.Canvas(status, width=px(10), height=px(10), bg=Theme.BG, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, px(7)))
        self.status_lbl = self._label(status, muted=True)
        self.status_lbl.pack(side="left")

        # cartão principal: liga/desliga, alvo e medidor de saída
        card = tk.Frame(self, bg=Theme.CARD, padx=px(18), pady=px(16))
        card.pack(fill="x", **side)
        card.columnconfigure(1, weight=1)
        self.var_enabled = tk.BooleanVar(value=self.cfg["enabled"])
        top = tk.Frame(card, bg=Theme.CARD)
        top.grid(row=0, column=0, columnspan=2, sticky="w")
        Toggle(top, self.var_enabled, self._settings_changed, px, Theme.CARD).pack(side="left")
        about = tk.Frame(top, bg=Theme.CARD)
        about.pack(side="left", padx=px(14))
        self._label(about, "Normalização automática", font=self.f_bold).pack(anchor="w")
        self._label(about, "Ajusta o volume de quem fala sozinho, a cada ~2 s de fala.",
                    muted=True, font=self.f_small).pack(anchor="w")
        FlatButton(card, "Voltar todos a 100%", self._reset, "ghost", self.f_body).grid(
            row=0, column=2, sticky="e")

        tk.Frame(card, bg=Theme.BORDER, height=1).grid(row=1, column=0, columnspan=3, sticky="we",
                                                       pady=px(14))

        self._label(card, "Nível alvo").grid(row=2, column=0, sticky="w")
        self.var_target = tk.DoubleVar(value=self.cfg["target_db"])
        Slider(card, self.var_target, -40, -10, self._target_moved, px, Theme.CARD).grid(
            row=2, column=1, sticky="we", padx=px(14))
        self.lbl_target = self._label(card, width=10, anchor="e")
        self.lbl_target.grid(row=2, column=2, sticky="e")

        self._label(card, "Saída agora").grid(row=3, column=0, sticky="w", pady=(px(12), 0))
        self.out_bar = LevelBar(card, px, Theme.CARD, height=10)
        self.out_bar.grid(row=3, column=1, sticky="we", padx=px(14 + 8), pady=(px(12), 0))  # alinha com o slider
        self.lbl_out = self._label(card, muted=True, width=10, anchor="e")
        self.lbl_out.grid(row=3, column=2, sticky="e", pady=(px(12), 0))
        self._target_moved(save=False)

        # ajustes avançados (fechado por padrão)
        self.advanced = Collapsible(self, self, "Ajustes avançados")
        self.advanced.pack(fill="x", pady=(px(14), 0), **side)
        self._build_advanced(self.advanced.body)

        # atividade (log), presa no rodapé
        self.activity = Collapsible(self, self, "Atividade")
        self.activity.pack(side="bottom", fill="x", pady=(px(10), px(18)), **side)
        body = self.activity.body
        self.txt_log = tk.Text(body, height=6, bg=Theme.CARD, fg=Theme.MUTED, font=("Consolas", 9),
                               relief="flat", bd=0, wrap="word", state="disabled", highlightthickness=0)
        scroll = ttk.Scrollbar(body, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True)

        # pessoas na call
        members_head = tk.Frame(self, bg=Theme.BG)
        members_head.pack(fill="x", pady=(px(18), px(8)), **side)
        self.lbl_members = self._label(members_head, "NA CALL", muted=True, font=self.f_section)
        self.lbl_members.pack(side="left")
        self._label(members_head, "clique em AUTO para travar alguém", muted=True,
                    font=self.f_small).pack(side="right")
        self.list = ScrollList(self, Theme.BG)
        self.list.pack(fill="both", expand=True, **side)
        self.empty = tk.Label(self.list.inner, bg=Theme.CARD, fg=Theme.MUTED, font=self.f_body,
                              pady=px(36))

    def _build_advanced(self, body: tk.Frame) -> None:
        px = self.px
        body.columnconfigure(1, weight=1)
        self._label(body, "Velocidade").grid(row=0, column=0, sticky="w")
        self.var_speed = tk.DoubleVar(value=self.cfg["speed"])
        Slider(body, self.var_speed, 0.1, 1.0, self._speed_moved, px, Theme.CARD).grid(
            row=0, column=1, sticky="we", padx=px(14))
        self.lbl_speed = self._label(body, muted=True, width=16, anchor="e")
        self.lbl_speed.grid(row=0, column=2, sticky="e")
        self._speed_moved(save=False)

        self._label(body, "Volume permitido").grid(row=1, column=0, sticky="w", pady=px(12))
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
            self._label(box, "%   até   " if i == 0 else "%", muted=True).pack(side="left")

        self._label(body, "Saída monitorada").grid(row=2, column=0, sticky="w")
        self.var_device = tk.StringVar(value=self.cfg["device"] or DEFAULT_DEVICE_LABEL)
        combo = ttk.Combobox(body, textvariable=self.var_device, state="readonly",
                             values=[DEFAULT_DEVICE_LABEL, *self.ctrl.meter.loopback_devices()])
        combo.grid(row=2, column=1, columnspan=2, sticky="we", padx=(px(14), 0))
        combo.bind("<<ComboboxSelected>>", lambda _e: self._device_changed())

        tray_row = tk.Frame(body, bg=Theme.CARD)
        tray_row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(px(14), 0))
        self.var_tray = tk.BooleanVar(value=self.cfg["minimize_to_tray"])
        Toggle(tray_row, self.var_tray, self._settings_changed, px, Theme.CARD).pack(side="left")
        self._label(tray_row, "Minimizar esconde na bandeja (ícone perto do relógio)").pack(
            side="left", padx=px(12))

        self._label(body, "Dica: ponha o Discord numa saída e o jogo/música em outra — a medição fica "
                          "mais limpa.", muted=True, font=self.f_small).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(px(12), 0))

    # ── ações ──
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

    # ── bandeja ──
    def _on_unmap(self, event) -> None:
        # O bind no Tk raiz também recebe Unmap de TODOS os widgets filhos (bindtags):
        # só interessa quando a própria janela foi minimizada.
        if (event.widget is self and self.state() == "iconic"
                and self.cfg["minimize_to_tray"] and self.tray.available):
            self.after_idle(self._hide_to_tray)

    def _hide_to_tray(self) -> None:
        self.withdraw()
        self.tray.notify_once(f"O {APP_NAME} continua rodando aqui. Clique no ícone para abrir.")

    def show_window(self) -> None:
        self.deiconify()
        self.state("normal")
        self.lift()
        self.attributes("-topmost", True)  # o Windows não deixa roubar o foco de outra janela
        self.after(100, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def _target_moved(self, _=None, save=True) -> None:
        self.lbl_target.configure(text=f"{round(self.var_target.get())} dBFS")
        if save:
            self._settings_changed()

    def _speed_moved(self, _=None, save=True) -> None:
        self.lbl_speed.configure(text=f"{self.var_speed.get():.0%} por janela")
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

    def _start_meter(self) -> None:
        # Abrir o loopback WASAPI às vezes trava no driver: fora da thread da GUI, a janela
        # continua respondendo mesmo assim.
        device = self.cfg["device"]

        def run():
            try:
                self.ctrl.meter.start(device)
            except Exception as exc:
                self.log(f"Não consegui abrir o loopback de áudio: {exc}")

        threading.Thread(target=run, name="audio-open", daemon=True).start()

    def _device_changed(self) -> None:
        value = self.var_device.get()
        self.cfg["device"] = "" if value == DEFAULT_DEVICE_LABEL else value
        self._schedule_save()
        self._start_meter()

    def _reset(self) -> None:
        n = self.ctrl.reset_volumes()
        self.log(f"Volume de {n} pessoa(s) voltou para 100%")

    def quit_app(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.withdraw()  # some na hora, mesmo que o resto demore
        save_config(self.cfg)
        self.tray.stop()
        # Fechar o stream WASAPI às vezes trava no driver; rodando fora da GUI, o app
        # nunca fica preso aberto (main() espera até 3 s e encerra de qualquer jeito).
        self.shutdown_thread = threading.Thread(target=self.ctrl.shutdown, name="shutdown", daemon=True)
        self.shutdown_thread.start()
        self.destroy()

    # ── atualização periódica da tela ──
    def _tick(self) -> None:
        while True:  # cliques da bandeja
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
        self.btn_connect.configure(text="Desconectar" if active else "Conectar")
        self.btn_connect.set_kind("ghost" if active else "accent")

        channel, out_db, rows = self.ctrl.engine.snapshot()
        target, deadband = self.cfg["target_db"], self.cfg["deadband_db"]
        live = out_db > -90
        self.out_bar.set(out_db if live else None, target, Theme.ACCENT)
        self.lbl_out.configure(text=f"{out_db:.0f} dBFS" if live else "silêncio")
        self.lbl_members.configure(
            text=f"NA CALL  ·  {_ellipsize(channel, 40).upper()}  ·  {len(rows)}" if channel else "NA CALL")
        self._sync_rows(rows, target, deadband, channel, active)
        tip = f"{APP_NAME} — {text}"
        if channel:
            tip += f"\n{channel}: {len(rows)} pessoa(s)"
        if not self.cfg["enabled"]:
            tip += "\nnormalização pausada"
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
            msg = "Clique em Conectar para começar."
        elif not channel:
            msg = "Entre numa call de voz no Discord."
        else:
            msg = "Só você na call por enquanto."
        self.empty.configure(text=msg)
        if not self._empty_shown:
            self.empty.pack(fill="x")
            self._empty_shown = True


_instance_mutex = None


def _claim_single_instance() -> bool:
    """Com a janela escondida na bandeja é fácil abrir o app de novo sem querer; duas
    cópias brigariam pelo RPC do Discord. Um mutex nomeado garante uma só."""
    global _instance_mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\Normie.DiscordVoiceNormalizer")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def main() -> None:
    if sys.stderr is None:  # .exe sem console (PyInstaller --windowed): print/traceback iriam falhar
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # texto nítido em telas com escala
    except Exception:
        pass
    if not _claim_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, f"O {APP_NAME} já está aberto — procure o ícone na bandeja, "
                                      "perto do relógio.")
        return
    app = App()
    app.mainloop()
    if app.shutdown_thread is not None:
        app.shutdown_thread.join(timeout=3)
    # A config já foi salva e o Discord desconectado. Se o driver de áudio travou numa
    # thread nativa, ela seguraria o processo vivo sem janela: encerra de vez.
    os._exit(0)


if __name__ == "__main__":
    main()
