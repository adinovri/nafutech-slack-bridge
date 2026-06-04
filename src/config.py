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

LOG_LEVEL = _env("LOG_LEVEL", "INFO")
