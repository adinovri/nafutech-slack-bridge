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
              ├─> no  → ignore silently
              └─> yes → _dispatch()
                          │
                          ├─> text starts with "[alt]"?
                          │     ├─> yes → alt_runner.run_alt()   (tmux + JSONL tail)
                          │     └─> no  → claude_runner.run()    (claude -p, ephemeral)
                          │
                          └─> post result back to thread via chat_update / chat_postMessage
```

### Two runners

| | Default runner | `[alt]` runner |
|---|---|---|
| **Trigger** | any message | prefix `[alt]` |
| **How** | `claude -p --output-format json` (subprocess) | `claude` TUI in tmux, JSONL transcript tailed |
| **Session** | ephemeral per request | persistent tmux session per thread |
| **Live updates** | none | progressive Slack edits every `ALT_FLUSH_SECS` |
| **Tool crumbs** | none | `🔧 tool_name…` shown while Claude works |
| **Concurrency** | queue (default bolt behavior) | concurrent requests rejected per thread |

### `[alt]` runner — per-request flow

```
[alt] prefix detected
  │
  └─> TmuxSession.ensure()
        ├─> session alive? → reuse
        └─> dead / new    → spawn claude TUI (--resume <uuid> or --session-id <uuid>)
                             poll pane for "❯" / "? for shortcuts" → TUI ready
  │
  └─> snapshot JSONL transcript byte offset (before send)
  │
  └─> paste prompt via tmux buffer → Enter
  │
  └─> tail JSONL transcript from offset, polling every 0.4s:
        ├─> collect text blocks + tool_use crumbs
        ├─> throttled chat_update to Slack every ALT_FLUSH_SECS
        ├─> stop on end_turn (only if that record carries visible text)
        ├─> fail-fast if session dies + QUIESCE elapsed
        └─> quiescence fallback: pane idle + no tool_use in-flight → break
  │
  └─> final chat_update (full result, chunked if > 3800 chars)
  │
  └─> touch() session TTL; reaper kills idle sessions after ALT_IDLE_TTL
```

### Graceful shutdown

On `SIGTERM`/`SIGINT`:
1. Flush all pending `"thinking…"` acks → update to restart message
2. `alt_runner.kill_all()` → kill all `nafu_*` tmux sessions
3. Exit

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
> subscribed — channel mentions go through `app_mention` only.

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
  app.py            slack-bolt listener + dispatch (two runners, reaper loop, shutdown flush)
  claude_runner.py  default runner: spawns `claude -p --output-format json [--resume]`
  alt_runner.py     [alt] runner: TmuxSession per thread, JSONL transcript tail, live updates
  thread_store.py   read/write threads/<thread_ts>.json (session_id + runner type)
  config.py         env var parsing
```

Per-thread state lives at `$THREAD_STORE_DIR/<thread_ts>.json`:

```json
{ "session_id": "uuid", "channel": "C0...", "last_user": "U0...", "runner": "alt" }
```

The `runner` field ensures session IDs are never crossed between runners on resume.

## Environment variables

```
# Slack
SLACK_BOT_TOKEN      required    xoxb-... (bot OAuth token)
SLACK_APP_TOKEN      required    xapp-... (socket mode app-level token)
TRIGGER_USER_ID      U051UM31HDF Slack user ID allowed to trigger the bot

# Claude
CLAUDE_CLI           claude      path to claude binary
CLAUDE_CONFIG_DIR    ~/.claude   config dir (injected by systemd; controls account/profile)
CLAUDE_MODEL         claude-sonnet-4-6
CLAUDE_PERMISSION_MODE  bypassPermissions
CLAUDE_TIMEOUT       600         max seconds to wait for any response

# Paths
NAFUTECH_WORKSPACE   ~/.openclaw/agents/nafutech/workspace
THREAD_STORE_DIR     ~/.openclaw/agents/nafutech/slack-threads

# [alt] runner
ALT_MARKER           [alt]       prefix to select the alt runner
ALT_TMUX_SOCKET      nafutech    tmux -L socket name (isolates nafu sessions)
ALT_IDLE_TTL         1800        seconds before idle tmux session is reaped
ALT_TUI_BOOT_SECS    8           max seconds to wait for TUI to show prompt
ALT_PASTE_SETTLE_SECS 0.6        pause between paste-buffer and Enter key
ALT_FLUSH_SECS       1.5         interval for progressive Slack updates
ALT_QUIESCE_SECS     10.0        silence before quiescence fallback kicks in
ALT_QUIESCE_STABLE_POLLS 3       consecutive idle pane polls to confirm turn closed

LOG_LEVEL            INFO
```

## Debugging

```bash
# live logs (systemd)
journalctl --user -fu nafutech-slack-bridge

# active [alt] tmux sessions
tmux -L nafutech ls

# thread state files
ls ~/.openclaw/agents/nafutech/slack-threads/

# attach to a running [alt] session (read-only)
tmux -L nafutech attach -t nafu_<thread_ts> -r
```

## Security notes

- Bot runs with `--permission-mode bypassPermissions` → full tool access including Bash/Edit/MCP.
  The sender-check (`event.user == U051UM31HDF`) is the only gate.
- Non-Adi senders are silently dropped — bot stays quiet so it's safe to invite
  it to shared channels.
- Bot token in `.env` (mode 600) — do NOT commit. `.gitignore` covers `.env` + `threads/`.
