"""Minimal client for the Discord desktop app's local RPC (its Windows IPC pipe)."""
from __future__ import annotations

import _winapi  # PeekNamedPipe — the same one multiprocessing uses
import concurrent.futures
import json
import msvcrt
import queue
import struct
import threading
import time
import traceback
import uuid


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
