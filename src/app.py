"""Slack listener: triggers Claude Code when Adi sends a message to the bot.

Security gate (only Adi can trigger; everyone else is ignored):
  - In channels/groups/mpim: requires bot @-mention + sender == Adi (app_mention event)
  - In DM (im): any message from Adi to the bot (no mention required)
  - Bot messages and non-Adi authors are always ignored
"""
import logging
import signal
import sys
import threading
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import alt_runner, claude_runner, thread_store
from .config import (
    ALT_IDLE_TTL,
    ALT_MARKER,
    BG_MARKER,
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

# Background threads spawned by [alt][bg] dispatches. Tracked for graceful
# shutdown (best-effort join before exit).
_bg_threads: list[threading.Thread] = []


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


def _detect_markers(user_text: str) -> tuple[bool, bool, str]:
    """Return (is_alt, is_bg, clean_text). Strips [alt] and [bg] in any order."""
    s = user_text.lstrip()
    is_alt = is_bg = False
    while True:
        changed = False
        if s[: len(ALT_MARKER)].lower() == ALT_MARKER.lower():
            s = s[len(ALT_MARKER) :].lstrip()
            is_alt = True
            changed = True
        if s[: len(BG_MARKER)].lower() == BG_MARKER.lower():
            s = s[len(BG_MARKER) :].lstrip()
            is_bg = True
            changed = True
        if not changed:
            break
    return is_alt, is_bg, s


def _run_bg_task(
    channel: str,
    thread_ts: str,
    ack_ts: str,
    prompt: str,
    session_id: str | None,
    client,
    user: str | None,
) -> None:
    """Worker for [alt][bg] background tasks. Runs in a daemon thread."""
    try:
        result, new_session_id = alt_runner.run_alt(
            prompt, thread_ts, session_id,
            on_update=lambda *_: None,  # no streaming preview in bg mode
        )
        thread_store.save(
            thread_ts,
            {
                "session_id": new_session_id,
                "channel": channel,
                "last_user": user,
                "runner": "alt",
            },
        )
        body = result or "_(empty response)_"
        chunks = _chunk_text(body)
        log.info(
            "bg task done: chars=%d chunks=%d thread_ts=%s",
            len(body), len(chunks), thread_ts,
        )
        client.chat_update(channel=channel, ts=ack_ts, text=chunks[0])
        for idx, extra in enumerate(chunks[1:], start=2):
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"_(cont. {idx}/{len(chunks)})_\n{extra}",
            )
    except Exception:
        log.exception("bg task failed thread_ts=%s", thread_ts)
        client.chat_update(
            channel=channel,
            ts=ack_ts,
            text=":warning: Maaf, task background gagal. Coba retrigger.",
        )
    finally:
        _pending.pop(thread_ts, None)


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
        f"IMPORTANT: Reply as plain text only. Do NOT post your reply to this "
        f"thread yourself via any Slack tool (e.g. slack_bot / "
        f"conversations_add_message) — the bridge posts your answer for you. "
        f"Just return the text.\n\n"
        f"---\n{stripped}"
    )


def _dispatch(event: dict, client, bot_user_id: str | None) -> None:
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    user = event.get("user")
    text = event.get("text") or ""
    log.info("trigger: channel=%s thread_ts=%s user=%s", channel, thread_ts, user)

    # strip mention before marker detection
    raw = text
    if bot_user_id:
        raw = raw.replace(f"<@{bot_user_id}>", "").strip()

    is_alt, is_bg, clean = _detect_markers(raw)
    prompt = _build_prompt(event, clean, bot_user_id)

    # alt/bg = one REPL per thread; reject concurrent requests rather than queuing
    if (is_alt or is_bg) and thread_ts in _pending:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":warning: Masih ngerjain pesan sebelumnya di thread ini. Tunggu kelar dulu ya.",
        )
        return

    ack_text = (
        ":hourglass_flowing_sand: berjalan di background — aku update saat selesai."
        if is_bg
        else ":hourglass_flowing_sand: thinking…"
    )
    ack = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=ack_text,
    )
    _pending[thread_ts] = (channel, ack["ts"])

    state = thread_store.get(thread_ts) or {}
    # only resume an alt session_id for alt/bg runner (avoids crossing runner types)
    session_id = state.get("session_id") if (not is_alt or state.get("runner") == "alt") else None

    if is_bg:
        t = threading.Thread(
            target=_run_bg_task,
            args=(channel, thread_ts, ack["ts"], prompt, session_id, client, user),
            daemon=True,
            name=f"bg-{thread_ts}",
        )
        _bg_threads.append(t)
        t.start()
        log.info("bg task spawned: thread_ts=%s resume_session=%s", thread_ts, session_id)
        return  # non-blocking; worker handles _pending cleanup and reply

    try:
        if is_alt:
            def on_update(partial: str, crumbs: list[str]) -> None:
                preview = (("\n".join(crumbs) + "\n") if crumbs else "") + partial
                try:
                    client.chat_update(
                        channel=channel, ts=ack["ts"],
                        text=_chunk_text(preview)[0],
                    )
                except Exception:
                    log.debug("alt on_update chat_update skipped", exc_info=True)

            log.info("alt runner: thread_ts=%s resume_session=%s", thread_ts, session_id)
            result, new_session_id = alt_runner.run_alt(
                prompt, thread_ts, session_id, on_update
            )
        else:
            result, new_session_id = claude_runner.run(prompt, session_id=session_id)

        thread_store.save(
            thread_ts,
            {
                "session_id": new_session_id,
                "channel": channel,
                "last_user": user,
                "runner": "alt" if is_alt else "default",
            },
        )
        body = result or "_(empty response)_"
        chunks = _chunk_text(body)
        log.info(
            "claude reply: chars=%d chunks=%d thread_ts=%s runner=%s",
            len(body), len(chunks), thread_ts, "alt" if is_alt else "default",
        )
        client.chat_update(channel=channel, ts=ack["ts"], text=chunks[0])
        for idx, extra in enumerate(chunks[1:], start=2):
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"_(cont. {idx}/{len(chunks)})_\n{extra}",
            )
    except Exception:
        log.exception("claude run failed (alt=%s)", is_alt)
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
    for t in list(_bg_threads):
        t.join(timeout=5)
    alt_runner.kill_all()
    sys.exit(0)


def _reaper_loop() -> None:
    while True:
        time.sleep(60)
        try:
            alt_runner.reap_idle(ALT_IDLE_TTL)
        except Exception:
            log.exception("alt reaper error")


def main() -> None:
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)
    threading.Thread(target=_reaper_loop, daemon=True, name="alt-reaper").start()
    log.info(
        "starting socket mode handler (trigger_sender=%s, workspace=%s)",
        TRIGGER_USER_ID,
        SLACK_APP_TOKEN[:10] + "…",
    )
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
