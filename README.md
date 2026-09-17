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
                          ├─> detect markers "[alt]" and "[bg]" (any order, case-insensitive)
                          │     ├─> [bg]        → spawn nafu-bg-claude subprocess    (fire-and-forget)
                          │     ├─> [alt]       → alt_runner.run_alt()               (tmux + JSONL tail)
                          │     └─> (none)      → claude_runner.run()                (claude -p, ephemeral)
                          │
                          └─> post result back to thread via chat_update / chat_postMessage
                              ([bg]: the subprocess posts directly via notify-json config)
```

### Three runners

| | Default | `[alt]` | `[bg]` |
|---|---|---|---|
| **Trigger** | any message | prefix `[alt]` | prefix `[bg]` (wins if both markers present) |
| **How** | `claude -p --output-format json` in-process subprocess | `claude` TUI in tmux, JSONL transcript tailed | detached `nafu-bg-claude` subprocess, watchdog-managed |
| **Bridge behavior** | blocks worker until done | blocks worker until done | fire-and-forget — returns immediately |
| **Session** | ephemeral per request | persistent tmux session per thread | persistent tmux session per task |
| **Live updates** | none | progressive Slack edits every `ALT_FLUSH_SECS` | none — one ack, then final result when done |
| **Tool crumbs** | none | `🔧 tool_name…` shown while Claude works | none |
| **Concurrency per thread** | queued (default bolt behavior) | second request rejected while one is running | multiple parallel tasks allowed; siblings listed in prompt |
| **Survives bridge restart** | no — ack marked "retrigger" on SIGTERM | no — tmux killed on shutdown | **yes** — subprocess is detached, watchdog updates Slack when done |
| **Ack text** | `⏳ thinking…` | `⏳ thinking…` (edited live) | `⏳ berjalan di background — aku update saat selesai.` |
| **Best for** | short questions, one-shot | long interactive task where you want to watch progress | long-running deploys / batch scans / research you can walk away from |

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

### `[bg]` runner — per-request flow

```
[bg] prefix detected  (or [bg][alt] / [alt][bg] — bg wins)
  │
  ├─> read BG_REGISTRY (~/.openclaw/bg_registry.json) for sibling bg tasks
  │   in the SAME Slack thread; inject their descriptions into the prompt
  │   so Claude knows what other bg work is already running here
  │
  ├─> post ack: "⏳ berjalan di background — aku update saat selesai."
  │   (NOT tracked in _pending, so a bridge restart won't touch it)
  │
  └─> subprocess.Popen(["python3", nafu-bg-claude, "slack:<thread>", prompt,
                        "--notify-json", {channel, thread_ts, ack_ts, bot_token}])
        │
        └─> nafu-bg-claude
              ├─> registers task in BG_REGISTRY with description + started_at
              ├─> spawns its OWN tmux session (independent of bridge's alt sessions)
              ├─> runs the Claude turn to completion — bridge is long gone
              └─> on finish: posts result to Slack via chat_update on ack_ts
                    (posting is done by the subprocess, not the bridge)
        │
        └─> nafu-bg-watchdog (systemd-managed sibling service)
              reaps stale registry entries, kills orphaned tmux sessions
```

Because the subprocess is detached and posts its own result, a bridge restart
mid-run does not interrupt or notify. The bg task keeps going and updates Slack
when it finishes — exactly as if the bridge were still up.

### Graceful shutdown

On `SIGTERM`/`SIGINT`:
1. Flush all pending `"thinking…"` acks → update to restart message
   (only default/`[alt]` acks; `[bg]` acks are intentionally not tracked)
2. `alt_runner.kill_all()` → kill all `nafu_*` tmux sessions
   (bg tasks run in their OWN tmux sessions, not touched by this)
3. Exit — any `[bg]` subprocess keeps running and posts when done

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

Make sure the log dir exists before running detached / as a service:

```bash
mkdir -p logs
chmod 600 .env
```

## Deploy as a service (auto-restart on reboot)

Pick your platform. Both variants run as the current user (no root needed), tail
into `logs/bot.log`, and restart on crash.

### Linux — systemd user unit

1. **Enable linger** so user services keep running after logout and start on
   boot without a login session:

   ```bash
   sudo loginctl enable-linger "$USER"
   ```

2. **Write the unit** to `~/.config/systemd/user/nafutech-slack-bridge.service`:

   ```ini
   [Unit]
   Description=NafuTech Slack Bridge (Slack -> Claude Code)
   After=network-online.target
   Wants=network-online.target
   StartLimitBurst=5
   StartLimitIntervalSec=60

   [Service]
   Type=simple
   WorkingDirectory=%h/Codes/nafutech-slack-bridge
   ExecStart=%h/Codes/nafutech-slack-bridge/run.sh
   Restart=on-failure
   RestartSec=5
   TimeoutStopSec=30
   KillMode=process
   StandardOutput=append:%h/Codes/nafutech-slack-bridge/logs/bot.log
   StandardError=append:%h/Codes/nafutech-slack-bridge/logs/bot.log
   Environment=HOME=%h
   Environment=PATH=%h/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin
   Environment=CLAUDE_CONFIG_DIR=%h/ClaudeConfigs/adi.novriansyah

   [Install]
   WantedBy=default.target
   ```

   > `%h` expands to `$HOME`. Adjust `PATH` and `CLAUDE_CONFIG_DIR` if your
   > setup differs. `KillMode=process` keeps the `[alt]` tmux sessions alive
   > across bridge restarts (the bridge kills them itself on graceful shutdown).

3. **Enable + start**:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now nafutech-slack-bridge
   ```

4. **Verify**:

   ```bash
   systemctl --user status nafutech-slack-bridge
   journalctl --user -fu nafutech-slack-bridge
   ```

   Common ops:

   ```bash
   systemctl --user restart nafutech-slack-bridge
   systemctl --user stop nafutech-slack-bridge
   systemctl --user disable --now nafutech-slack-bridge   # stop autostart
   ```

### macOS — launchd (LaunchAgent)

1. **Write the plist** to
   `~/Library/LaunchAgents/io.nanovest.nafutech-slack-bridge.plist`. Replace
   `USERNAME` with your Mac username (`$(whoami)`) and adjust `PATH` for
   Intel (`/usr/local/bin`) vs Apple Silicon (`/opt/homebrew/bin`):

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key>
     <string>io.nanovest.nafutech-slack-bridge</string>

     <key>ProgramArguments</key>
     <array>
       <string>/Users/USERNAME/Codes/nafutech-slack-bridge/run.sh</string>
     </array>

     <key>WorkingDirectory</key>
     <string>/Users/USERNAME/Codes/nafutech-slack-bridge</string>

     <key>EnvironmentVariables</key>
     <dict>
       <key>HOME</key>
       <string>/Users/USERNAME</string>
       <key>PATH</key>
       <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
       <key>CLAUDE_CONFIG_DIR</key>
       <string>/Users/USERNAME/ClaudeConfigs/adi.novriansyah</string>
     </dict>

     <key>RunAtLoad</key>
     <true/>

     <key>KeepAlive</key>
     <dict>
       <key>SuccessfulExit</key>
       <false/>
       <key>Crashed</key>
       <true/>
     </dict>

     <key>ThrottleInterval</key>
     <integer>10</integer>

     <key>StandardOutPath</key>
     <string>/Users/USERNAME/Codes/nafutech-slack-bridge/logs/bot.log</string>
     <key>StandardErrorPath</key>
     <string>/Users/USERNAME/Codes/nafutech-slack-bridge/logs/bot.log</string>
   </dict>
   </plist>
   ```

   > LaunchAgents do **not** support `~` — every path must be absolute. A
   > LaunchAgent runs when the user is logged in on the console; if you need it
   > to start before login (e.g. headless server), promote to a `LaunchDaemon`
   > under `/Library/LaunchDaemons/` (requires `sudo`).

2. **Load + start**:

   ```bash
   launchctl bootstrap gui/$(id -u) \
     ~/Library/LaunchAgents/io.nanovest.nafutech-slack-bridge.plist
   launchctl enable gui/$(id -u)/io.nanovest.nafutech-slack-bridge
   launchctl kickstart -k gui/$(id -u)/io.nanovest.nafutech-slack-bridge
   ```

3. **Verify**:

   ```bash
   launchctl print gui/$(id -u)/io.nanovest.nafutech-slack-bridge | head -30
   tail -f ~/Codes/nafutech-slack-bridge/logs/bot.log
   ```

   Common ops:

   ```bash
   # restart
   launchctl kickstart -k gui/$(id -u)/io.nanovest.nafutech-slack-bridge

   # stop (until next login/reboot)
   launchctl kill SIGTERM gui/$(id -u)/io.nanovest.nafutech-slack-bridge

   # unload / disable autostart
   launchctl bootout gui/$(id -u) \
     ~/Library/LaunchAgents/io.nanovest.nafutech-slack-bridge.plist
   ```

   > Older `launchctl load -w …` / `unload -w …` still works but is deprecated
   > since macOS 10.11 — prefer the `bootstrap` / `bootout` verbs above.

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

# [bg] runner
BG_MARKER            [bg]        prefix to select the bg runner (case-insensitive)
                                 (registry lives at ~/.openclaw/bg_registry.json;
                                  worker binary: $NAFUTECH_WORKSPACE/nafu-bg-claude;
                                  supervisor: $NAFUTECH_WORKSPACE/nafu-bg-watchdog)

LOG_LEVEL            INFO
```

## Debugging

```bash
# live logs (systemd)
journalctl --user -fu nafutech-slack-bridge

# active [alt] tmux sessions (bridge-owned)
tmux -L nafutech ls

# thread state files
ls ~/.openclaw/agents/nafutech/slack-threads/

# attach to a running [alt] session (read-only)
tmux -L nafutech attach -t nafu_<thread_ts> -r

# --- [bg] runner ---

# in-flight bg tasks (task id, slack thread, description, started_at, pid)
cat ~/.openclaw/bg_registry.json | jq .

# watchdog logs (if installed as a sibling systemd service)
journalctl --user -fu nafu-bg-watchdog

# bg tasks run in their OWN tmux sessions — list them
tmux ls 2>/dev/null | grep -E '^bg_|^nafu-bg-'
```

## Security notes

- Bot runs with `--permission-mode bypassPermissions` → full tool access including Bash/Edit/MCP.
  The sender-check (`event.user == U051UM31HDF`) is the only gate.
- Non-Adi senders are silently dropped — bot stays quiet so it's safe to invite
  it to shared channels.
- Bot token in `.env` (mode 600) — do NOT commit. `.gitignore` covers `.env` + `threads/`.
