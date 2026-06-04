"""Runner ALTERNATIF (jalur [alt]): claude interaktif di tmux + tail JSONL.

Hidup berdampingan dgn claude_runner.py (-p). Dipilih dari app.py._dispatch
saat teks user diawali ALT_MARKER. Jaminan keamanan identik runner lama:
--disallowed-tools + CLAUDE_CONFIG_DIR di-inject systemd, bukan hard-coded.
"""
import json
import logging
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

from .claude_runner import DISALLOWED_TOOLS
from .config import (
    ALT_FLUSH_SECS,
    ALT_PASTE_SETTLE_SECS,
    ALT_QUIESCE_SECS,
    ALT_QUIESCE_STABLE_POLLS,
    ALT_TMUX_SOCKET,
    ALT_TUI_BOOT_SECS,
    CLAUDE_CLI,
    CLAUDE_CONFIG_DIR,
    CLAUDE_MODEL,
    CLAUDE_PERMISSION_MODE,
    CLAUDE_TIMEOUT,
    NAFUTECH_WORKSPACE,
)

log = logging.getLogger(__name__)

OnUpdate = Callable[[str, list[str]], None]

# tmux_name → last_active (monotonic). In-memory; orphans adopted on restart.
_SESSIONS: dict[str, float] = {}


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", ALT_TMUX_SOCKET, *args],
        capture_output=True,
        text=True,
    )


def _session_name(thread_ts: str) -> str:
    return "nafu_" + thread_ts.replace(".", "_").replace("/", "_")


def _mangled_cwd() -> str:
    # same rule as claude CLI: absolute path, '/' AND '.' → '-'
    # (e.g. /home/scriberion/.openclaw/... → -home-scriberion--openclaw-...)
    return str(NAFUTECH_WORKSPACE).replace("/", "-").replace(".", "-")


def _transcript_path(session_uuid: str) -> Path:
    return CLAUDE_CONFIG_DIR / "projects" / _mangled_cwd() / f"{session_uuid}.jsonl"


# ---------------------------------------------------------------------------
# TmuxSession
# ---------------------------------------------------------------------------

class TmuxSession:
    """One interactive claude process per Slack thread, kept alive in tmux."""

    def __init__(self, thread_ts: str, session_uuid: str) -> None:
        self.name = _session_name(thread_ts)
        self.session_uuid = session_uuid
        self.jsonl = _transcript_path(session_uuid)

    def _alive(self) -> bool:
        return _tmux("has-session", "-t", self.name).returncode == 0

    def ensure(self) -> None:
        if self._alive():
            return
        # resume preserves context; --session-id starts fresh with deterministic path
        id_flag = (
            ["--resume", self.session_uuid]
            if self.jsonl.exists()
            else ["--session-id", self.session_uuid]
        )
        cmd = [
            CLAUDE_CLI,
            "--model", CLAUDE_MODEL,
            "--permission-mode", CLAUDE_PERMISSION_MODE,
            "--disallowed-tools", ",".join(DISALLOWED_TOOLS),
            *id_flag,
        ]
        r = _tmux(
            "new-session", "-d", "-s", self.name,
            "-x", "220", "-y", "50",
            "-c", str(NAFUTECH_WORKSPACE),
            *cmd,
        )
        if r.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {r.stderr.strip()}")
        self._wait_tui_ready()

    def _wait_tui_ready(self) -> None:
        # Real TUI footer/prompt markers (CLI 2.1.x): the input prompt is "❯",
        # and the footer shows the permission/shortcut hint. Old keywords
        # ("│ >", "> ") never matched → boot detection always timed out.
        deadline = time.monotonic() + ALT_TUI_BOOT_SECS
        while time.monotonic() < deadline:
            pane = _tmux("capture-pane", "-p", "-t", self.name).stdout
            if any(kw in pane for kw in ("❯", "shift+tab to cycle", "? for shortcuts")):
                return
            time.sleep(0.4)
        log.warning("alt: TUI boot timeout for %s, continuing optimistically", self.name)

    def send_prompt(self, prompt: str) -> None:
        # multi-line via bracketed paste — avoids REPL submitting per line
        _tmux("set-buffer", "--", prompt)
        _tmux("paste-buffer", "-p", "-t", self.name)
        # The TUI debounces bracketed paste; an Enter sent in the same instant
        # gets absorbed and the prompt sits unsent in the input box. Give the
        # paste a moment to settle, THEN submit as a distinct key event.
        time.sleep(ALT_PASTE_SETTLE_SECS)
        _tmux("send-keys", "-t", self.name, "Enter")

    def pane_text(self) -> str:
        return _tmux("capture-pane", "-p", "-t", self.name).stdout

    def kill(self) -> None:
        _tmux("kill-session", "-t", self.name)


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def _parse_line(
    obj: dict, text_acc: list[str], crumbs: list[str]
) -> tuple[str | None, bool]:
    """Update accumulators from one JSONL line.

    Returns (stop_reason, had_text) where had_text is True iff THIS assistant
    message carried a visible text block. With extended thinking enabled, the
    CLI writes the thinking block as its own assistant record that already
    carries stop_reason='end_turn', ~seconds before the visible-text record
    (also end_turn). Reporting had_text lets the caller ignore that premature
    thinking-only end_turn and wait for the real answer.
    """
    if obj.get("type") != "assistant":
        return None, False
    msg = obj.get("message") or {}
    # Skip CLI-internal placeholders. When a turn is processed but nothing needs
    # answering (e.g. a "Continue from where you left off." flush with no pending
    # work), the CLI emits a synthetic assistant record (model="<synthetic>",
    # stop_reason="stop_sequence", text="No response requested."). It is not a
    # real answer — accumulating its text leaks it to Telegram via _flush and the
    # quiescence fallback. Drop it whole: no text, no crumbs, no stop_reason.
    if msg.get("model") == "<synthetic>":
        return None, False
    had_text = False
    for block in msg.get("content", []):
        btype = block.get("type")
        if btype == "text":
            had_text = True
            text_acc.append(block.get("text", ""))
        elif btype == "tool_use":
            crumbs.append(f"🔧 {block.get('name', 'tool')}…")
    return msg.get("stop_reason"), had_text  # 'end_turn' | 'tool_use' | None


# ---------------------------------------------------------------------------
# touch / reap / kill_all
# ---------------------------------------------------------------------------

def touch(thread_ts: str) -> None:
    _SESSIONS[_session_name(thread_ts)] = time.monotonic()


def reap_idle(idle_ttl: int) -> None:
    """Kill nafu_* tmux sessions idle longer than idle_ttl seconds."""
    now = time.monotonic()
    result = _tmux("list-sessions", "-F", "#{session_name}")
    for name in result.stdout.splitlines():
        if not name.startswith("nafu_"):
            continue
        last = _SESSIONS.get(name)
        if last is None:
            # orphan after bridge restart — adopt, don't kill immediately
            _SESSIONS[name] = now
            log.info("alt reaper: adopted orphan session %s", name)
            continue
        if now - last > idle_ttl:
            _tmux("kill-session", "-t", name)
            _SESSIONS.pop(name, None)
            log.info("alt reaper: killed idle session %s", name)


def kill_all() -> None:
    """Kill all nafu_* sessions. Called on bridge shutdown."""
    result = _tmux("list-sessions", "-F", "#{session_name}")
    for name in result.stdout.splitlines():
        if name.startswith("nafu_"):
            _tmux("kill-session", "-t", name)
            log.info("alt: killed session %s on shutdown", name)
    _SESSIONS.clear()


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def run_alt(
    prompt: str,
    thread_ts: str,
    session_id: str | None,
    on_update: OnUpdate,
) -> tuple[str, str]:
    """Run prompt via tmux-hosted interactive claude. Returns (final_text, session_uuid)."""
    session_uuid = session_id or str(uuid.uuid4())
    sess = TmuxSession(thread_ts, session_uuid)
    sess.ensure()
    touch(thread_ts)

    # snapshot byte offset BEFORE sending — avoids triggering on previous turns
    tail_offset = sess.jsonl.stat().st_size if sess.jsonl.exists() else 0
    sess.send_prompt(prompt)

    text_acc: list[str] = []
    crumbs: list[str] = []
    last_flush = time.monotonic()
    last_change = time.monotonic()
    hard_deadline = time.monotonic() + CLAUDE_TIMEOUT
    pos = tail_offset
    # last assistant stop_reason seen. 'tool_use' = model paused to run a tool,
    # so the turn is NOT done (a tool_result + further assistant msg follow) —
    # quiescence must never break while this holds. Reset to None when fresh
    # assistant text streams in without a tool pause.
    last_stop_reason: str | None = None
    # consecutive polls where the pane looked idle — debounces tool latency
    idle_pane_streak = 0

    while True:
        now = time.monotonic()

        if now > hard_deadline:
            text_acc.append("\n_(⏱ kepotong: lewat batas waktu)_")
            break

        # --- tail new bytes from transcript ---
        # Binary read + explicit byte offset: text-mode `for line in fh` forbids
        # fh.tell() (read-ahead buffer → OSError), and text-mode seek/tell is not
        # a reliable byte offset under multibyte UTF-8. We read raw bytes from
        # `pos`, consume only up to the last newline, and leave any half-written
        # trailing line for the next poll.
        if sess.jsonl.exists():
            size = sess.jsonl.stat().st_size
            if size > pos:
                with sess.jsonl.open("rb") as fh:
                    fh.seek(pos)
                    data = fh.read(size - pos)
                last_nl = data.rfind(b"\n")
                if last_nl != -1:
                    consumed = data[: last_nl + 1]
                    pos += len(consumed)
                    for raw_line in consumed.decode("utf-8", errors="replace").splitlines():
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            obj = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue  # defensive: skip malformed lines
                        stop, had_text = _parse_line(obj, text_acc, crumbs)
                        if stop is not None:
                            last_stop_reason = stop
                        # primary detector — return ONLY on the end_turn record
                        # that itself carries the visible answer text (had_text).
                        # With extended thinking the CLI emits a thinking-only
                        # record stamped end_turn a few seconds BEFORE the real
                        # answer — and that artifact precedes EVERY answer, not
                        # just the first. So we must NOT fall back to "any text
                        # accumulated so far": if the model emitted interim intent
                        # text earlier in the turn (e.g. "Siap, gue cek sekarang."
                        # with stop_reason=tool_use), that stale text would make
                        # the thinking-only end_turn return prematurely, truncating
                        # the turn to the intent line. If a turn ever ends with no
                        # text in the final record, the quiescence fallback below
                        # (last_stop_reason is now end_turn, so no longer
                        # suppressed) still returns the accumulated text within
                        # ALT_QUIESCE_SECS.
                        if stop == "end_turn" and had_text:
                            _flush(on_update, text_acc, crumbs)
                            return "".join(text_acc).strip(), sess.session_uuid
                    last_change = now
                    idle_pane_streak = 0  # fresh bytes → not idle
                    touch(thread_ts)

        # --- throttled live update ---
        if now - last_flush >= ALT_FLUSH_SECS and text_acc:
            _flush(on_update, text_acc, crumbs)
            last_flush = now

        idle_secs = now - last_change

        # --- alive check: fail-fast on crash (§11 #3) ---
        if idle_secs > ALT_QUIESCE_SECS and not sess._alive():
            text_acc.append("\n_(⚠️ proses berhenti mendadak — kemungkinan crash atau auth gagal)_")
            break

        # --- quiescence fallback: stop_reason unreadable + pane idle ---
        # Only a last resort when end_turn never lands. Suppressed entirely
        # while the last assistant msg paused for a tool ('tool_use') — that
        # turn is mid-flight and a tool_result will resume it. Requires the
        # pane to read idle across several consecutive polls so a transient
        # MCP/tool-latency gap can't be mistaken for a finished turn.
        if (idle_secs > ALT_QUIESCE_SECS and text_acc
                and last_stop_reason != "tool_use"):
            if _pane_idle(sess):
                idle_pane_streak += 1
                if idle_pane_streak >= ALT_QUIESCE_STABLE_POLLS:
                    log.info(
                        "alt: quiescence-break %s (idle %.1fs, stop_reason=%s)",
                        sess.name, idle_secs, last_stop_reason,
                    )
                    break
            else:
                idle_pane_streak = 0

        time.sleep(0.4)

    final = "".join(text_acc).strip() or "_(empty response)_"
    _flush(on_update, text_acc, crumbs)
    return final, sess.session_uuid


def _flush(on_update: OnUpdate, text_acc: list[str], crumbs: list[str]) -> None:
    try:
        on_update("".join(text_acc).strip(), list(crumbs))
    except Exception:
        log.debug("alt on_update raised; ignored", exc_info=True)


# Footer/spinner markers that mean claude is still actively working. The CLI
# cycles random gerunds in the spinner, so the reliable signals are the
# "esc to interrupt" hint, the live token counter, and the run timer — all
# present whenever a tool is executing or the model is generating.
_BUSY_MARKERS = (
    "esc to interrupt",
    "Actioning",
    "Thinking",
    "Working",
    "tokens",
    "⏵⏵",
)


def _pane_idle(sess: TmuxSession) -> bool:
    """Heuristic fallback: empty input prompt "❯" reappeared at the bottom and
    no busy spinner/footer is visible → turn likely closed."""
    pane = sess.pane_text()
    if not pane:
        return False
    last_lines = pane.splitlines()[-6:]
    busy = any(m in ln for ln in last_lines for m in _BUSY_MARKERS)
    has_prompt = any(ln.strip() in ("❯", "❯ ") for ln in last_lines)
    return has_prompt and not busy
