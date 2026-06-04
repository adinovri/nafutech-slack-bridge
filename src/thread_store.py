"""File-based thread → claude session_id mapping.

One JSON file per Slack thread (keyed by thread_ts). Survives bot restarts.
"""
import json
from pathlib import Path

from .config import THREAD_STORE_DIR

THREAD_STORE_DIR.mkdir(parents=True, exist_ok=True)


def _path(thread_ts: str) -> Path:
    safe = thread_ts.replace("/", "_")
    return THREAD_STORE_DIR / f"{safe}.json"


def get(thread_ts: str) -> dict | None:
    p = _path(thread_ts)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def save(thread_ts: str, data: dict) -> None:
    _path(thread_ts).write_text(json.dumps(data, indent=2))
