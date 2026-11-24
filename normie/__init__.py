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

Login: the Discord app (CLIENT_ID in normie/config.py, or the user's own app set up in
Advanced settings) is a "Public Client", so authorization uses PKCE — there is no
client secret.

Requirements: Windows 10/11, Python 3.10+
    pip install -r requirements.txt
    python -m normie

Package layout
--------------
config → levels → engine     settings, dB math, who-is-speaking bookkeeping
rpc → oauth                  Discord's local RPC pipe and the PKCE login
capture                      WASAPI loopback level meter
controller                   wires RPC, engine and capture together
gui/                         the Tkinter window, widgets and tray icon
__main__                     startup: `python -m normie`, normie.pyz and normie.exe
"""
from __future__ import annotations


APP_NAME = "Normie"
APP_VERSION = "dev"  # the release workflow replaces this with the published version
__version__ = APP_VERSION
