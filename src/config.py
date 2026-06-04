import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val  # type: ignore[return-value]


SLACK_BOT_TOKEN = _env("SLACK_BOT_TOKEN", required=True)
SLACK_APP_TOKEN = _env("SLACK_APP_TOKEN", required=True)
SLACK_TEAM_ID = _env("SLACK_TEAM_ID", "T02M409AZV4")
TRIGGER_USER_ID = _env("TRIGGER_USER_ID", "U051UM31HDF")

NAFUTECH_WORKSPACE = Path(
    _env("NAFUTECH_WORKSPACE", "/home/scriberion/.openclaw/agents/nafutech/workspace")
)
THREAD_STORE_DIR = Path(
    _env("THREAD_STORE_DIR", "/home/scriberion/.openclaw/agents/nafutech/slack-threads")
)
CLAUDE_CLI = _env("CLAUDE_CLI", "claude")
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_PERMISSION_MODE = _env("CLAUDE_PERMISSION_MODE", "bypassPermissions")
CLAUDE_TIMEOUT = int(_env("CLAUDE_TIMEOUT", "600"))

# --- alt runner (pola shannon: tmux + tail JSONL) ---
ALT_MARKER        = _env("ALT_MARKER",        "[alt]")
ALT_TMUX_SOCKET   = _env("ALT_TMUX_SOCKET",   "nafutech")
ALT_IDLE_TTL      = int(_env("ALT_IDLE_TTL",      "1800"))
ALT_QUIESCE_SECS  = float(_env("ALT_QUIESCE_SECS",  "10.0"))
# how many consecutive idle polls (×0.4s) confirm a turn is closed before we
# fall back to quiescence-break — debounces transient tool-latency gaps
ALT_QUIESCE_STABLE_POLLS = int(_env("ALT_QUIESCE_STABLE_POLLS", "3"))
ALT_FLUSH_SECS    = float(_env("ALT_FLUSH_SECS",    "1.5"))
ALT_TUI_BOOT_SECS = float(_env("ALT_TUI_BOOT_SECS", "8"))
ALT_PASTE_SETTLE_SECS = float(_env("ALT_PASTE_SETTLE_SECS", "0.6"))  # paste→Enter gap
# injected by systemd; fallback to ~/.claude for local dev
CLAUDE_CONFIG_DIR = Path(_env("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))

LOG_LEVEL = _env("LOG_LEVEL", "INFO")
