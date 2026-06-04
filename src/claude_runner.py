"""Spawn `claude -p` subprocess and parse JSON result."""
import json
import logging
import os
import subprocess

from .config import (
    CLAUDE_CLI,
    CLAUDE_MODEL,
    CLAUDE_PERMISSION_MODE,
    CLAUDE_TIMEOUT,
    NAFUTECH_WORKSPACE,
)

log = logging.getLogger(__name__)

# Tier 2 Slack write tools that act as Adi (user OAuth). The bridge already
# posts everything via SLACK_BOT_TOKEN (nanotech_bot), so the subprocess must
# never call these — otherwise replies leak as Adi's identity. CLAUDE.md
# forbids them, but the model occasionally forgets; this is the harness-level
# enforcement.
DISALLOWED_TOOLS = [
    "mcp__claude_ai_Slack__slack_send_message",
    "mcp__claude_ai_Slack__slack_send_message_draft",
    "mcp__claude_ai_Slack__slack_schedule_message",
    "mcp__claude_ai_Slack__slack_add_reaction",
    "mcp__claude_ai_Slack__slack_create_canvas",
    "mcp__claude_ai_Slack__slack_update_canvas",
]


def run(prompt: str, session_id: str | None = None) -> tuple[str, str]:
    """Run claude CLI with the given prompt. Returns (result_text, new_session_id)."""
    cmd = [
        CLAUDE_CLI,
        "-p",
        prompt,
        "--model",
        CLAUDE_MODEL,
        "--output-format",
        "json",
        "--permission-mode",
        CLAUDE_PERMISSION_MODE,
        "--disallowed-tools",
        ",".join(DISALLOWED_TOOLS),
    ]
    if session_id:
        cmd += ["--resume", session_id]

    log.info(
        "spawning claude (cwd=%s, resume=%s, timeout=%s)",
        NAFUTECH_WORKSPACE,
        session_id,
        CLAUDE_TIMEOUT,
    )
    # CLAUDE_CONFIG_DIR must point at the dir holding valid OAuth creds
    # (adi.novriansyah, not the stale `scriberion` one). It's injected by the
    # systemd unit (Environment=CLAUDE_CONFIG_DIR=...), which is the canonical
    # launch path — run via `systemctl --user`, never a manual nohup.
    env = os.environ.copy()

    proc = subprocess.run(
        cmd,
        cwd=str(NAFUTECH_WORKSPACE),
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
        env=env,
    )

    if proc.returncode != 0:
        log.error(
            "claude exited %s; stderr=%s; stdout=%s",
            proc.returncode,
            proc.stderr[:1000],
            proc.stdout[:1000],
        )
        # claude CLI writes its API-error payload to stdout as JSON even when
        # exiting non-zero. Surface the human-readable `result` field instead of
        # raw JSON so Slack doesn't get a wall of `{"is_error":true,...}`.
        try:
            err_payload = json.loads(proc.stdout)
            human = (err_payload.get("result") or "").strip()
            api_status = err_payload.get("api_error_status")
            if human and api_status:
                msg = f"[API {api_status}] {human}"
            elif human:
                msg = human
            else:
                msg = (proc.stderr or proc.stdout or "").strip()[:500] or "unknown error"
        except json.JSONDecodeError:
            msg = (proc.stderr or proc.stdout or "").strip()[:500] or "unknown error"
        raise RuntimeError(msg)

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        log.error("invalid JSON from claude: %s | stdout head=%s", e, proc.stdout[:500])
        raise

    result = payload.get("result") or payload.get("response") or ""
    new_session_id = payload.get("session_id") or session_id or ""
    return result, new_session_id
