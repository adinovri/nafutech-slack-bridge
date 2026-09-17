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

## Prerequisites

Both `run_linux.sh` and `run_mac.sh` check these for you and print an
install hint if anything is missing. If you'd rather install first, here's the
full list:

| tool | version | why |
|---|---|---|
| **python3** | **≥ 3.10** | source uses PEP 585 generics (`dict[str, tuple[...]]`) |
| python3-venv | matching | to create `.venv/` |
| **tmux** | any recent | `[alt]` and `[bg]` runners drive Claude in a tmux TUI |
| **curl** | any | Telegram notify (`nafu-notify`) uses it |
| **claude** CLI | latest | the actual worker — see [Claude Code install docs](https://docs.anthropic.com/en/docs/claude-code) or `npm i -g @anthropic-ai/claude-code` |
| **systemctl** (Linux) | any | to run as a user service — skip with `--no-service` |
| **launchctl** (macOS) | any | to install as a LaunchAgent — skip with `--no-service` |
| jq (optional) | any | pretty-print `~/.openclaw/bg_registry.json` in debug commands |

Quick install lines:

- **Ubuntu/Debian:** `sudo apt-get install -y python3 python3-venv tmux curl jq`
- **macOS (Homebrew):** `brew install python@3.12 tmux jq`  (curl ships with macOS)
- **claude CLI:** `npm i -g @anthropic-ai/claude-code` (needs Node 18+) — or follow the official doc

On Linux, also enable **linger** once so your user services survive logout and
start on boot:

```bash
sudo loginctl enable-linger "$USER"
```

## Quickstart — fresh clone to running service

Same three steps on both platforms. The bootstrap script does the venv + deps +
runtime dirs + systemd/launchd install for you.

```bash
git clone <this-repo> ~/Codes/nafutech-slack-bridge
cd ~/Codes/nafutech-slack-bridge

# 1. copy the env template + fill in your real tokens
cp .env.example .env
chmod 600 .env
$EDITOR .env
#   SLACK_BOT_TOKEN   ← BW item NANOTECH_OAUTH_TOKEN (xoxb-...)
#   SLACK_APP_TOKEN   ← BW item NANOTECH_APP_TOKEN   (xapp-...)
#   NAFUTECH_WORKSPACE, THREAD_STORE_DIR, BG_REGISTRY — adjust to your paths

# 2. one-shot install (Linux)
bash run_linux.sh
#    …or macOS
bash run_mac.sh
```

After the installer finishes, the bridge is running as a user-level service
that auto-restarts on crash and starts on boot. Tokens in `.env` are used
via `run.sh` (which the service invokes).

What each script installs:

| | Linux (`run_linux.sh`) | macOS (`run_mac.sh`) |
|---|---|---|
| bridge | systemd user unit `nafutech-slack-bridge.service` (enabled + started) | LaunchAgent `io.nanovest.nafutech-slack-bridge` (bootstrapped + started) |
| `[bg]` watchdog | systemd oneshot `nafu-bg-watchdog.service` + `.timer` (fires every 15s) | LaunchAgent `io.nanovest.nafu-bg-watchdog` with `StartInterval=15` (bootstrapped + started) |
| Python venv | `.venv/` under repo root | same |
| script permissions | `chmod +x scripts/*` | same |
| `.env` guard | rejects the sample placeholders; refuses to enable service until real tokens are in place | same |

### Modes

```bash
bash run_linux.sh --check        # verify prerequisites, do nothing else
bash run_linux.sh --no-service   # everything except systemd install (run foreground with ./run.sh)
bash run_linux.sh                # full install + start service (default)
```

`run_mac.sh` takes the same three modes.

### Service ops (after install)

**Linux — systemd:**

```bash
# logs
journalctl --user -fu nafutech-slack-bridge
journalctl --user -fu nafu-bg-watchdog

# lifecycle
systemctl --user restart nafutech-slack-bridge
systemctl --user disable --now nafutech-slack-bridge nafu-bg-watchdog.timer
```

**macOS — launchd:**

```bash
# logs
tail -f logs/bot.log         # bridge
tail -f logs/watchdog.log    # [bg] watchdog (fires every 15s)

# lifecycle — bridge
launchctl kickstart -k gui/$(id -u)/io.nanovest.nafutech-slack-bridge
launchctl bootout   gui/$(id -u) \
  ~/Library/LaunchAgents/io.nanovest.nafutech-slack-bridge.plist

# lifecycle — [bg] watchdog
launchctl kickstart -k gui/$(id -u)/io.nanovest.nafu-bg-watchdog
launchctl bootout   gui/$(id -u) \
  ~/Library/LaunchAgents/io.nanovest.nafu-bg-watchdog.plist
```

### Templates you may need to inspect

Everything the installer copies lives in the repo under version control:

```
deploy/systemd/
  nafutech-slack-bridge.service   # bridge — uses %h for $HOME
  nafu-bg-watchdog.service        # oneshot — reads $BG_REGISTRY + optional Telegram env file
  nafu-bg-watchdog.timer          # 15-second cadence
deploy/launchd/
  io.nanovest.nafutech-slack-bridge.plist   # bridge — installer sed-substitutes USERNAME + brew prefix
  io.nanovest.nafu-bg-watchdog.plist        # [bg] watchdog — StartInterval=15s, sources optional Telegram env file
```

Change these in the repo, rerun `bash run_linux.sh` / `bash run_mac.sh`, and
they're re-copied.

### Optional — Telegram notify for `[bg]` fallback

> **If you only use the Slack bridge, SKIP this whole section.** Nothing to
> install, nothing to configure. Every Slack path (default, `[alt]`, `[bg]`)
> notifies Slack — Telegram is never touched. `nafu-notify` gracefully exits
> when the env vars are absent, so unused code stays quiet.

Telegram is only used when:
1. You run `scripts/nafu-bg <desc> <cmd> …` directly — the generic "run any
   shell command in background" helper always notifies via Telegram.
2. You manually invoke `scripts/nafu-bg-claude` with
   `--notify-json '{"type":"telegram"}'` (or with no `--notify-json`, since
   telegram is the default there when called outside the bridge).

To enable it, drop a private env file at `~/.config/nafutech-slack-bridge.env`:

```bash
umask 077
cat > ~/.config/nafutech-slack-bridge.env <<'EOF'
NAFU_TG_BOT_TOKEN=123456:AA…      # from @BotFather
NAFU_TG_CHAT_ID=1234567890         # your chat id
EOF
```

The watchdog systemd unit reads this file via
`EnvironmentFile=-%h/.config/nafutech-slack-bridge.env` (the `-` prefix means
the file is optional — no error if it's absent).

## File layout

```
src/                                    Python source (bridge process)
  app.py            slack-bolt listener + dispatch (three runners, reaper loop, shutdown flush)
  claude_runner.py  default runner: spawns `claude -p --output-format json [--resume]`
  alt_runner.py     [alt] runner: TmuxSession per thread, JSONL transcript tail, live updates
  thread_store.py   read/write threads/<thread_ts>.json (session_id + runner type)
  config.py         env var parsing

scripts/                                [bg] runtime — bridge spawns nafu-bg-claude; watchdog polls
  nafu-bg-claude    launch a Claude task in a detached tmux session, append to $BG_REGISTRY
  nafu-bg-watchdog  poll the registry every 15s, notify (Slack/Telegram) on end_turn, reap orphans
  nafu-bg           run an arbitrary shell command in background via systemd-run + Telegram notify
  nafu-notify       curl → Telegram (reads NAFU_TG_BOT_TOKEN + NAFU_TG_CHAT_ID; NO hardcoded secrets)

deploy/                                 templates copied by run_linux.sh / run_mac.sh
  systemd/          bridge service, watchdog service, watchdog timer (%h-based, portable)
  launchd/          bridge LaunchAgent, watchdog LaunchAgent (StartInterval=15s)

run.sh              foreground entrypoint (sources .env, execs `python -m src.app`) — used by systemd/launchd
run_linux.sh        one-shot: prereq check + venv + systemd install + start
run_mac.sh          one-shot: prereq check + venv + LaunchAgent install + start
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
BG_REGISTRY          ~/.openclaw/bg_registry.json
                                 shared registry file — bridge, nafu-bg-claude,
                                 and nafu-bg-watchdog MUST agree on this path.
                                 Bridge passes it through to the subprocess.

# Optional — Telegram fallback for nafu-bg / non-Slack nafu-bg-claude callers.
# Keep these OUT of .env; put them in ~/.config/nafutech-slack-bridge.env
# (mode 600). The watchdog systemd unit reads that file via EnvironmentFile=-.
NAFU_TG_BOT_TOKEN    (unset)     Telegram bot token (from @BotFather)
NAFU_TG_CHAT_ID      (unset)     Telegram chat id

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
