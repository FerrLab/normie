"""Wires the Discord RPC client, the engine and the audio capture together."""
from __future__ import annotations

import concurrent.futures
import threading
import time
import urllib.error

from normie.capture import LoopbackMeter, discord_output_name
from normie.config import CHANNEL_EVENTS, CLIENT_ID, REDIRECT_URI, SCOPES, app_client_id, save_config
from normie.engine import Engine
from normie.oauth import auth_error_hint, make_pkce_pair, oauth_token
from normie.rpc import DiscordRPC, RPCError


class Controller:
    """Wires RPC, engine and audio together. Commands to Discord run on a serial worker
    thread, so they never block the GUI or the RPC I/O thread.

    set_status(text, kind) — kind is "off", "busy", "ok" or "error".
    on_output_device(name) — Discord switched to another output device ("" = unknown).
    on_access_refused(own_app) — Discord refused the rpc permissions for this account."""

    RETRY_SECONDS = 10

    def __init__(self, cfg: dict, log, set_status, on_output_device=None, on_access_refused=None):
        self.cfg = cfg
        self.log = log
        self._set_status = set_status
        self._on_output_device = on_output_device or (lambda _name: None)
        self._on_access_refused = on_access_refused or (lambda _own_app: None)
        self._connecting: DiscordRPC | None = None  # connection still logging in
        self._generation = 0                         # bumped by use_app() to cancel a login
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

    @property
    def client_id(self) -> str:
        return app_client_id(self.cfg)

    def use_app(self, own_client_id: str) -> None:
        """Log in with another Discord app from now on ("" = Normie's shared app). Tokens
        belong to the app that issued them, so this starts a fresh authorization."""
        self.cfg.update(own_client_id=own_client_id, access_token="", refresh_token="",
                        expires_at=0, token_client_id="")
        save_config(self.cfg)
        self._want_connected = True
        if self._retry_timer:
            self._retry_timer.cancel()
        self._generation += 1
        pending = self._connecting
        if pending is not None:
            pending.close()  # an AUTHORIZE popup may be waiting for minutes: don't wait for it
        self._submit(self._disconnect)
        self._submit(self._connect)

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
        generation = self._generation
        rpc = self._connecting = DiscordRPC(self.client_id, self._on_event, self._on_rpc_closed)
        try:
            rpc.connect()
            self._authenticate(rpc)
            self.rpc = rpc
            rpc.request("SUBSCRIBE", evt="VOICE_CHANNEL_SELECT")
            self._follow_output_device(rpc)
            self._load_selected_channel()
        except Exception as exc:
            rpc.close()
            self.rpc = None
            if generation != self._generation:
                return  # use_app() cut this login short to switch apps; it reconnects itself
            if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
                self.log(f"Discord unavailable: {exc}")
                if self._want_connected:
                    self._schedule_retry()
                return
            self._want_connected = False
            self.log(f"Couldn't connect: {exc}")
            own_app = bool(self.cfg["own_client_id"])
            hint = auth_error_hint(exc, own_app)
            if hint:
                self.log(hint)
                self._on_access_refused(own_app)
            self._set_status("Discord refused access — see Activity" if hint
                             else "Connection failed — see Activity", "error")
        finally:
            self._connecting = None

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
        cfg, client_id = self.cfg, rpc.client_id
        issued_to = cfg["token_client_id"] or CLIENT_ID  # untagged: saved before own apps existed
        if issued_to != client_id:  # tokens from another app are useless here
            cfg.update(access_token="", refresh_token="", expires_at=0)
        if cfg["access_token"] and cfg["expires_at"] > time.time() + 60:
            try:
                return self._auth_with(rpc, cfg["access_token"])
            except RPCError as exc:
                self.log(f"Saved token rejected ({exc}); refreshing…")
        if cfg["refresh_token"]:
            try:
                tok = oauth_token(client_id=client_id, grant_type="refresh_token",
                                  refresh_token=cfg["refresh_token"])
                self._store_token(tok, client_id)
                return self._auth_with(rpc, tok["access_token"])
            except (RPCError, RuntimeError, urllib.error.URLError) as exc:
                self.log(f"Couldn't refresh the token ({exc}); asking for authorization again…")
        self._set_status("Approve the request in the Discord window…", "busy")
        verifier, challenge = make_pkce_pair()
        data = rpc.request("AUTHORIZE", {
            "client_id": client_id, "scopes": SCOPES,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }, timeout=300)
        tok = oauth_token(client_id=client_id, grant_type="authorization_code", code=data["code"],
                          redirect_uri=REDIRECT_URI, code_verifier=verifier)
        self._store_token(tok, client_id)
        self._auth_with(rpc, tok["access_token"])

    def _auth_with(self, rpc: DiscordRPC, token: str) -> None:
        data = rpc.request("AUTHENTICATE", {"access_token": token}) or {}
        user = data.get("user") or rpc.user
        self.engine.set_self(user.get("id"))
        self.user_name = user.get("global_name") or user.get("username") or ""
        missing = set(SCOPES) - set(data.get("scopes") or SCOPES)
        if missing:
            self.log(f"Warning: the token is missing the scopes {sorted(missing)}")

    def _store_token(self, tok: dict, client_id: str) -> None:
        if client_id != self.client_id:
            return  # the user switched apps while this login was finishing
        self.cfg["access_token"] = tok["access_token"]
        self.cfg["refresh_token"] = tok.get("refresh_token", "")
        self.cfg["expires_at"] = time.time() + float(tok.get("expires_in", 0))
        self.cfg["token_client_id"] = client_id
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
