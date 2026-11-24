"""Settings: app constants, the Discord app Normie logs in with, and the JSON config file."""
from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path


# Public ID of the app in the Discord Developer Portal — not a secret. The app must
# have "Public Client" enabled and REDIRECT_URI listed under OAuth2 → Redirects.
# Until Discord approves it for RPC, this shared app only works for accounts on its
# App Testers list; anyone else can set up their own app (Advanced settings → Discord app).
CLIENT_ID = "1574641998332166236"
REDIRECT_URI = "http://localhost"
DEV_PORTAL_URL = "https://discord.com/developers/applications"


def _app_dir() -> Path:
    """Folder the config file lives in — next to whatever the user launched:

    - the PyInstaller --onefile .exe: next to the .exe (its __file__ points to a temporary
      folder that is deleted on exit);
    - normie.pyz: next to the .pyz (__file__ points inside the zip);
    - a clone run with `python -m normie`: the folder that holds the normie/ package.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    root = Path(__file__).resolve().parent.parent
    return root.parent if root.is_file() else root


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
    "token_client_id": "",     # the Discord app the saved tokens were issued to
    "own_client_id": "",       # the user's own Discord app ("" = Normie's shared app)
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


def app_client_id(cfg: dict) -> str:
    """The Discord app Normie logs in with: the user's own, if they set one up."""
    return cfg.get("own_client_id") or CLIENT_ID


def parse_client_id(text: str) -> str | None:
    """Find an Application ID (a 17–20 digit snowflake) in whatever was pasted: the bare
    ID or the Developer Portal's address, which contains it."""
    match = re.search(r"(?<!\d)\d{17,20}(?!\d)", text or "")
    return match.group(0) if match else None


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
    # Tokens saved before own apps existed were issued to the shared app.
    if saved.get("refresh_token") or saved.get("access_token"):
        saved.setdefault("token_client_id", CLIENT_ID)
    # Before "device_mode" existed, a non-empty "device" meant someone picked it by hand.
    if "device_mode" not in saved and saved.get("device"):
        saved["device_mode"] = "manual"
    cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    if cfg["token_client_id"] and cfg["token_client_id"] != app_client_id(cfg):
        cfg.update(access_token="", refresh_token="", expires_at=0, token_client_id="")
    return cfg


def save_config(cfg: dict) -> None:
    with _config_lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
