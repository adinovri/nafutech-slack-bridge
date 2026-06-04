"""Slack listener: triggers Claude Code when Adi sends a message to the bot.

Security gate (only Adi can trigger; everyone else is ignored):
  - In channels/groups/mpim: requires bot @-mention + sender == Adi (app_mention event)
  - In DM (im): any message from Adi to the bot (no mention required)
  - Bot messages and non-Adi authors are always ignored
"""
import logging
import signal
import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import claude_runner, thread_store
from .config import (
    LOG_LEVEL,
    SLACK_APP_TOKEN,
    SLACK_BOT_TOKEN,
    TRIGGER_USER_ID,
)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nafutech-bridge")

app = App(token=SLACK_BOT_TOKEN)

# Slack chat.update/chat.postMessage hard limit is 40k chars on `text`,
# tapi rendering & formatting overhead bisa makan budget. Pakai ~3800 biar
# aman dari blok formatting + tetap human-readable per chunk.
SLACK_CHUNK_SIZE = 3800

# In-flight "thinking…" acks awaiting a Claude result, keyed by thread_ts ->
# (channel, ack_ts). On a restart/SIGTERM the worker thread blocked in
# subprocess.run dies before it can chat_update, leaving the placeholder stuck
# forever. The shutdown handler flushes whatever is still here so the user gets
# a clear "retrigger" message instead of a permanent "thinking…".
_pending: dict[str, tuple[str, str]] = {}


def _chunk_text(text: str, size: int = SLACK_CHUNK_SIZE) -> list[str]:
    """Split text into Slack-safe chunks, preferring newline boundaries."""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > size:
        # Split window
        window = remaining[:size]
        # Prefer last double-newline (paragraph), then single newline, then space.
        cut = window.rfind("\n\n")
        if cut < size // 2:
            cut = window.rfind("\n")
        if cut < size // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = size
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _build_prompt(event: dict, raw_text: str, bot_user_id: str | None) -> str:
    channel = event["channel"]
    user = event.get("user", "unknown")
    thread_ts = event.get("thread_ts") or event["ts"]
    stripped = raw_text
    if bot_user_id:
        stripped = stripped.replace(f"<@{bot_user_id}>", "").strip()
    return (
        f"You are NafuTech responding in a Slack thread. Context:\n"
        f"- Channel: {channel}\n"
        f"- Thread: {thread_ts}\n"
        f"- Sender: <@{user}> (this is Adi, your principal)\n\n"
        f"Reply concisely in the same language as the user. "
        f"The text below is the user's message — only respond to that, "
        f"do not invent additional context.\n\n"
        f"---\n{stripped}"
    )


def _dispatch(event: dict, client, bot_user_id: str | None) -> None:
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    user = event.get("user")
    text = event.get("text") or ""
    log.info("trigger: channel=%s thread_ts=%s user=%s", channel, thread_ts, user)

    prompt = _build_prompt(event, text, bot_user_id)

    ack = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":hourglass_flowing_sand: thinking…",
    )
    _pending[thread_ts] = (channel, ack["ts"])

    state = thread_store.get(thread_ts) or {}
    session_id = state.get("session_id")

    try:
        result, new_session_id = claude_runner.run(prompt, session_id=session_id)
        thread_store.save(
            thread_ts,
            {
                "session_id": new_session_id,
                "channel": channel,
                "last_user": user,
            },
        )
        body = result or "_(empty response)_"
        chunks = _chunk_text(body)
        log.info(
            "claude reply: chars=%d chunks=%d thread_ts=%s",
            len(body),
            len(chunks),
            thread_ts,
        )
        # First chunk replaces the "thinking…" message; rest go as new replies in the thread.
        client.chat_update(channel=channel, ts=ack["ts"], text=chunks[0])
        for idx, extra in enumerate(chunks[1:], start=2):
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"_(cont. {idx}/{len(chunks)})_\n{extra}",
            )
    except Exception:
        log.exception("claude run failed")
        client.chat_update(
            channel=channel,
            ts=ack["ts"],
            text=":warning: Maaf, lagi gak bisa proses sekarang. Coba retrigger lagi dalam 1-2 menit.",
        )
    finally:
        _pending.pop(thread_ts, None)


@app.event("app_mention")
def handle_app_mention(event, client, context, logger):
    # Fires when the bot is @-mentioned in a channel/group/mpim.
    # Slack already filters by mention target → we only enforce sender == Adi.
    sender = event.get("user")
    if sender != TRIGGER_USER_ID:
        log.info("ignore app_mention: sender=%s (not Adi)", sender)
        return
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return
    _dispatch(event, client, bot_user_id=context.get("bot_user_id"))


@app.event("message")
def handle_message(event, client, context, logger):
    # Only handle DMs here — channel mentions go through app_mention.
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype") in {
        "bot_message",
        "message_changed",
        "message_deleted",
    }:
        return
    sender = event.get("user")
    if sender != TRIGGER_USER_ID:
        log.info("ignore DM: sender=%s (not Adi)", sender)
        return
    _dispatch(event, client, bot_user_id=context.get("bot_user_id"))


def _flush_pending_acks() -> None:
    """Clear orphaned "thinking…" placeholders on shutdown.

    Runs in the main thread via the signal handler; the dispatch worker may be
    blocked in subprocess.run and about to be SIGKILLed, but chat_update is an
    independent API call so we can still mark the message as interrupted.
    """
    if not _pending:
        return
    log.info("flushing %d pending ack(s) before exit", len(_pending))
    for thread_ts, (channel, ack_ts) in list(_pending.items()):
        try:
            app.client.chat_update(
                channel=channel,
                ts=ack_ts,
                text=":arrows_counterclockwise: Ke-restart pas lagi proses — coba kirim ulang pesannya ya.",
            )
        except Exception:
            log.exception("failed to flush pending ack for thread_ts=%s", thread_ts)
        finally:
            _pending.pop(thread_ts, None)


def _graceful_shutdown(signum, _frame) -> None:
    log.info("received signal %s; shutting down", signum)
    _flush_pending_acks()
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)
    log.info(
        "starting socket mode handler (trigger_sender=%s, workspace=%s)",
        TRIGGER_USER_ID,
        SLACK_APP_TOKEN[:10] + "…",
    )
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
