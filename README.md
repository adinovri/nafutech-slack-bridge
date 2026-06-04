# nafutech-slack-bridge

Thin Slack listener: when **Adi (`U051UM31HDF`) messages the bot** — either via
@-mention in a channel where the bot is invited, or via direct DM — this process
spawns a `claude` CLI session inside the NafuTech workspace and posts the result
back in-thread. Followup messages in the same thread resume the same Claude
session.

Identity: `nanotech_bot` (B0B1ME870DP), workspace `T02M409AZV4`.

## Architecture

```
Slack message (any channel/DM the bot is in)
  ├─> channel/group/mpim: bot @-mentioned  → app_mention event
  └─> DM (im)            : any message     → message event (channel_type=im)
        │
        └─> sender == Adi (U051UM31HDF)?
              ├─> yes → spawn `claude -p` in NAFUTECH_WORKSPACE
              │         ├─> first msg in thread → new session
              │         └─> followup           → --resume <session_id>
              │       → post result back to thread
              └─> no  → ignore silently (log only)
```

- **Runtime**: same server as NafuTech remote agent (gcloud auth, VPN, BW, ~/.env all available)
- **Transport**: Slack Socket Mode (no public URL needed)
- **Session persistence**: file-based, one JSON per thread_ts under `THREAD_STORE_DIR`
- **Tool scope**: full — runs `claude --permission-mode bypassPermissions` inside NafuTech workspace
- **Security gate**: sender must equal `TRIGGER_USER_ID` (Adi). Anyone else mentioning
  the bot or DMing it is ignored. This is the only access control.

## Slack app setup (one-time, in api.slack.com)

App: `nanotech_bot` (existing).

1. **Socket Mode** → Enable
2. **App-Level Tokens** → Generate token with scope `connections:write` → copy `xapp-...`
   → store in BW as `NANOTECH_APP_TOKEN` → load into `SLACK_APP_TOKEN` env var
3. **OAuth & Permissions** → Bot Token Scopes, ensure these are present:
   - `chat:write`
   - `app_mentions:read`
   - `im:history` (for DMs from Adi)
   - `users:read`
4. **Event Subscriptions** → Enable Events, subscribe bot to:
   - `app_mention`           (channel/group/mpim @-mentions of the bot)
   - `message.im`            (Adi DMs the bot)
5. **Reinstall App** to workspace after scope changes
6. **Invite bot to channels** Adi wants reachable from (`/invite @nanotech_bot`)

> Note: `message.channels`/`message.groups`/`message.mpim` are intentionally NOT
> subscribed — channel mentions go through `app_mention` only. This keeps Slack's
> event volume low and removes the risk of accidentally processing non-mention
> messages.

## Local setup

```bash
cd ~/Codes/nafutech-slack-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# SLACK_BOT_TOKEN   ← BW NANOTECH_OAUTH_TOKEN (xoxb-...)
# SLACK_APP_TOKEN   ← BW NANOTECH_APP_TOKEN   (xapp-...)
$EDITOR .env

./run.sh
```

Logs go to stdout; redirect to `logs/bot.log` if running detached.

## File layout

```
src/
  app.py            slack-bolt listener + dispatch
  claude_runner.py  spawns `claude -p --output-format json [--resume]`
  thread_store.py   read/write threads/<thread_ts>.json
  config.py         env var parsing
```

Per-thread state lives at `$THREAD_STORE_DIR/<thread_ts>.json`:

```json
{ "session_id": "uuid", "channel": "C0...", "last_user": "U0..." }
```

## Security notes

- Bot runs with `--permission-mode bypassPermissions` → full tool access including Bash/Edit/MCP.
  The sender-check (`event.user == U051UM31HDF`) is the only gate.
- Non-Adi senders are silently dropped — bot stays quiet so it's safe to invite
  it to shared channels.
- Bot token in `.env` (mode 600) — do NOT commit. `.gitignore` covers `.env` + `threads/`.

## Open items / future work

- Rate limiting per thread (currently none)
- Streaming partial responses (currently single edit-after-complete)
- Slack `/nafu` slash command for explicit invocation without mention
- Health endpoint / systemd unit
