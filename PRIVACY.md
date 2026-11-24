# Normie Privacy Policy

**Effective date:** November 24, 2026

Normie ("the app") is a free, open-source desktop application published by FerrLab at
<https://github.com/FerrLab/normie>. It evens out the volume of the people you hear in a
Discord voice call. This policy explains what data Normie handles and why.

**In short:** everything happens on your computer. Normie has no servers, no accounts of
its own, no analytics, no telemetry and no ads. It never records audio. The only time it
uses the internet is to exchange or refresh your Discord login token with Discord.

## What Normie accesses

| Data | Where it comes from | What Normie does with it | Saved to disk? |
|---|---|---|---|
| Your Discord user ID and username | Your Discord app, through its local RPC connection | Shows "Connected as …" and excludes your own voice from measurements | No |
| The voice channel you are in: its name and the people in it (user IDs, usernames or nicknames, the volume and mute state *you* set for them) | Your Discord app | Shows the list of people and adjusts their volume on your side | Only the user IDs of people you choose to lock |
| Who is speaking right now | Speaking events from your Discord app | Decides whose voice is being measured | No |
| Discord's audio output setting: the device it plays on and the names of your audio devices | Your Discord app | Captures the same device Discord plays on, and follows it when you change it | Only the name of a device you pick by hand |
| Audio playing on Discord's output device (or the one you select) | Windows audio loopback (WASAPI) | Every 100 ms the sound is reduced to a single loudness number. **The audio itself is never recorded, saved or sent anywhere.** | No |
| Discord login tokens (access token and refresh token) | Discord, after you authorize Normie | Lets Normie reconnect without asking for authorization every time | Yes, in the configuration file |
| Your settings (target level, speed, limits, device, tray option, and the Application ID of your own Discord app if you set one up) | You | Applies your preferences | Yes, in the configuration file |

The activity log and all measurements live only in memory and disappear when you close
the app.

## Permissions requested from Discord

When you click **Connect** for the first time, Discord asks you to authorize Normie with
these scopes:

- `rpc` — connect to the Discord app running on your computer;
- `rpc.voice.read` — see the voice channel you are in, who is in it, who is speaking and
  which audio output device Discord uses;
- `rpc.voice.write` — change the per-user volume on your side (the same control as
  *right-click a user → User Volume*).

Normie does not read your messages, servers, friends list, email or anything else.
Volume changes only affect what **you** hear. Discord restores the previous volumes when
Normie disconnects.

## Network connections

Normie makes exactly these connections:

1. **Your local Discord app**, through the Discord IPC pipe on your computer. This
   connection never leaves your machine. Checking the Application ID of your own Discord
   app, during its setup, also happens over this connection.
2. **`https://discord.com/api/oauth2/token`**, only to exchange the authorization code
   for a token the first time you authorize Normie, and to refresh that token when it
   expires. The request contains the public client ID of the Discord app in use (Normie's,
   or your own), the code or refresh token, and a one-time PKCE verifier.

The **Open Developer Portal** button in the own-app setup opens Discord's website in your
web browser. That visit is between your browser and Discord; Normie itself sends nothing.

There are no other connections: no update checks, analytics, crash reports, advertising
or third-party services. This was verified by reviewing the source code and its
dependencies and by monitoring the app's network activity while it ran connected to
Discord. The source code is public, so you can check it yourself.

The Discord app itself talks to Discord's servers as usual; that traffic is governed by
[Discord's Privacy Policy](https://discord.com/privacy), not this one.

## Data stored on your computer

Normie keeps one file, `discord_normalizer_config.json`, in the same folder as
`normie.exe` or `normie.pyz` (or in the project folder when run from its source code).
It contains your Discord tokens, their expiry
time, your settings and the user IDs of locked people. It is stored as plain text:
**treat it like a password and do not share it.**

## Sharing

Normie does not sell, share or transfer any data. Apart from the token exchange with
Discord described above, no data leaves your computer.

## Your choices

- **Restore volumes:** close Normie. Discord restores the volumes it changed.
- **Delete your data:** delete `discord_normalizer_config.json`.
- **Revoke access:** in Discord, open *User Settings → Authorized Apps* and deauthorize
  Normie.
- **Uninstall:** delete `normie.exe` (or `normie.pyz`) and the configuration file.

## Children

Normie is intended for people who are old enough to use Discord under
[Discord's Terms of Service](https://discord.com/terms). Normie does not knowingly
collect information from anyone.

## Changes to this policy

Updates will be published in this file in the repository, with a new effective date.
The full history of changes is visible in the repository's commit history.

## Contact

Questions or requests: open an issue at <https://github.com/FerrLab/normie/issues>.
