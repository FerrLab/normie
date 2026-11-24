"""OAuth2 login with PKCE (Public Client, no secret) and Application ID checks."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from normie.config import CLIENT_ID, OAUTH_TOKEN_URL, REDIRECT_URI
from normie.rpc import DiscordRPC, RPCError


def pkce_challenge(verifier: str) -> str:
    """PKCE (RFC 7636): base64url(sha256(verifier)), without '=' padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge). The 43-char base64url verifier never leaves the PC
    until the code exchange; Discord only sees the challenge in AUTHORIZE."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    return verifier, pkce_challenge(verifier)


def oauth_token(*, client_id: str = CLIENT_ID, **fields) -> dict:
    """Exchange a code (or refresh_token) for an access_token with Discord's API.
    The app is a "Public Client": there is no client_secret; the code exchange carries
    the code_verifier instead."""
    body = urllib.parse.urlencode({"client_id": client_id, **fields}).encode()
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
        elif "redirect_uri" in detail:
            hint = f" — add {REDIRECT_URI} under OAuth2 → Redirects in the Developer Portal"
        raise RuntimeError(f"OAuth failed (HTTP {exc.code}): {detail}{hint}") from None


def auth_error_hint(exc: BaseException, own_app: bool = False) -> str:
    """What the user can do about a refused authorization ('' when there's nothing to add).

    Discord answers "invalid_scope" when the account clicking Authorize may not grant the
    rpc scopes: until Discord approves an app for RPC, only accounts on its App Testers list
    may. The app's owner is allowed implicitly, but once the app belongs to a team, even the
    team's members have to be added as testers."""
    if not (isinstance(exc, RPCError) and "invalid_scope" in str(exc)):
        return ""
    if own_app:
        return ("Discord refused the voice permissions for your own app. It must belong to your "
                "personal account, not a team: apps owned by a team only work for accounts on "
                "their App Testers list.")
    return ("Discord refused the voice permissions for this account. Until Discord approves "
            "Normie, its shared app only works for accounts on its App Testers list. Use your "
            "own Discord app instead (Advanced settings → Discord app, about 2 minutes), or ask "
            "Normie's developer to add you as an App Tester.")


def check_client_id(client_id: str) -> str:
    """Ask the local Discord app whether this Discord app exists ('' = it does, otherwise
    what went wrong). Only the IPC handshake is sent: no login, nothing leaves the PC."""
    rpc = DiscordRPC(client_id, lambda *_: None, lambda *_: None)
    try:
        rpc.connect(timeout=10)
    except RPCError as exc:
        if exc.code == 4000:  # "Invalid Client ID"
            return ("Discord doesn't know this Application ID. Copy it again from your app's "
                    "General Information page.")
        return str(exc)
    except ConnectionError:
        return "Open the Discord desktop app first, then try again."
    except (TimeoutError, OSError) as exc:
        return f"Discord didn't answer ({exc}). Try again in a few seconds."
    finally:
        rpc.close()
    return ""
