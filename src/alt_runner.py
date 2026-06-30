"""Runner ALTERNATIF (jalur [alt]): claude interaktif di tmux + tail JSONL.

Hidup berdampingan dgn claude_runner.py (-p). Dipilih dari app.py._dispatch
saat teks user diawali ALT_MARKER. Jaminan keamanan identik runner lama:
--disallowed-tools + CLAUDE_CONFIG_DIR di-inject systemd, bukan hard-coded.
"""
import json
import logging
import re
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
    ALT_RESUME_FORK_SECS,
    ALT_SUBMIT_RETRIES,
    ALT_SUBMIT_VERIFY_SECS,
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


def _project_dir() -> Path:
    return CLAUDE_CONFIG_DIR / "projects" / _mangled_cwd()


def _transcript_path(session_uuid: str) -> Path:
    return _project_dir() / f"{session_uuid}.jsonl"


def _snapshot_transcripts() -> set[str]:
    """Filenames of all *.jsonl transcripts in the project dir right now."""
    pdir = _project_dir()
    return {p.name for p in pdir.glob("*.jsonl")} if pdir.exists() else set()


# Status-bar token-count cell, e.g. "0/200.0K" or "20.0K/200.0K". Only renders on
# the live main-session chrome, never on a blocking interstitial — a reliable
# "real input box is up and accepting" signal across CLI versions.
_STATUSBAR_RE = re.compile(r"\d[\d.]*K?/\d[\d.]*K")

# A blocking full-screen startup menu (folder-trust, settings-warning, theme
# picker, update notice) renders a numbered selection whose cursor is "❯ N." at
# the start of a line. The real input prompt is "❯ " followed by free text,
# never "❯ <digit>.".
_MENU_CURSOR_RE = re.compile(r"^\s*❯\s*\d+\.", re.M)


def _real_prompt_marker(pane: str) -> bool:
    """True iff the live main-session input chrome is on screen. Kept broad to
    survive CLI version churn (v2.1.x moved footer text)."""
    return (
        any(kw in pane for kw in ("shift+tab to cycle", "for shortcuts", "│ >"))
        or bool(_STATUSBAR_RE.search(pane))
    )


def _is_interstitial(pane: str) -> bool:
    """A blocking startup menu is in front of the input box. Pasting a prompt
    into one gets it swallowed by the menu's Enter handler. Detected by the
    "❯ N." menu cursor or the "Enter to confirm" hint AND the absence of every
    real-prompt marker."""
    if _real_prompt_marker(pane):
        return False
    return bool(_MENU_CURSOR_RE.search(pane)) or "Enter to confirm" in pane


def _paste_probe(prompt: str) -> str:
    """A short, distinctive slice of the prompt to look for in the input box as
    proof the paste actually landed. First non-trivial line, capped — the wide
    pane (220 cols) means it won't wrap within this length."""
    for ln in prompt.splitlines():
        ln = ln.strip()
        if len(ln) >= 4:
            return ln[:40]
    return prompt.strip()[:40]


def _tool_display(name: str, input_dict: dict) -> str:
    """Format tool name + brief input preview for Slack display.

    mcp__claude_ai_Atlassian__getJiraIssue → Atlassian:getJiraIssue(HNWI-123)
    Read → Read(src/app.py)
    Bash → Bash(git log --oneline...)
    """
    # Strip MCP namespace prefix
    display_name = name
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            server = parts[1].replace("claude_ai_", "").replace("claude_", "")
            display_name = f"{server}:{parts[2]}"

    # Extract a brief input preview — prefer known field names, then fallback
    preview = ""
    if input_dict:
        for key in ("file_path", "query", "command", "prompt", "issue_key",
                    "channel_id", "path", "page_id", "jql", "cql"):
            val = input_dict.get(key)
            if val and isinstance(val, str):
                val = val.strip().replace("\n", " ")
                preview = val[:45] + ("…" if len(val) > 45 else "")
                break
        if not preview:
            for val in input_dict.values():
                if isinstance(val, str) and val.strip():
                    val = val.strip().replace("\n", " ")
                    preview = val[:45] + ("…" if len(val) > 45 else "")
                    break

    return f"{display_name}({preview})" if preview else display_name


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
            # Live REPL already appends to self.jsonl (the file it forked to on
            # its own spawn). Nothing to rebind — the stored uuid matches it.
            return
        # resume preserves context; --session-id starts fresh with deterministic path
        resuming = self.jsonl.exists()
        # Snapshot existing transcripts BEFORE a resume spawn: --resume writes the
        # replayed history into a NEW <uuid>.jsonl, so the live file is whatever
        # appears that wasn't here before.
        prior = _snapshot_transcripts() if resuming else set()
        id_flag = (
            ["--resume", self.session_uuid]
            if resuming
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
        if resuming:
            self._rebind_to_fork(prior)

    def _rebind_to_fork(self, prior: set[str]) -> None:
        """`claude --resume <uuid>` does NOT append to <uuid>.jsonl — it forks the
        whole conversation into a fresh <new-uuid>.jsonl (fresh sessionId = its
        own filename) and writes the replayed history there at load time. Without
        this, both the submit-verify loop and the streaming tail watch the stale
        resumed file, which never grows → false "prompt gagal terkirim", and the
        returned uuid stays the old one so the NEXT resume re-forks from the
        original and silently drops every intermediate turn.

        After a resume spawn we poll for the file that appeared post-spawn and
        rebind tailing + session_uuid to it. The TUI-ready gate means history
        replay is already flushed, so exactly one new main-session file exists
        (no subagent sidechains yet — no prompt has been sent). On timeout we
        leave self.jsonl on the original file: no worse than the prior bug, and
        the rare append-style CLI build (no fork) keeps working untouched."""
        deadline = time.monotonic() + ALT_RESUME_FORK_SECS
        pdir = _project_dir()
        while time.monotonic() < deadline:
            new = [pdir / n for n in (_snapshot_transcripts() - prior)]
            new = [p for p in new if p.exists() and p.stat().st_size > 0]
            if new:
                live = max(new, key=lambda p: p.stat().st_mtime)
                log.info("alt: resume %s forked → live transcript %s on %s",
                         self.session_uuid, live.stem, self.name)
                self.session_uuid = live.stem
                self.jsonl = live
                return
            if not self._alive():
                return
            time.sleep(0.3)
        log.warning("alt: resume fork file not found for %s within %.0fs; "
                    "tailing original %s", self.name, ALT_RESUME_FORK_SECS,
                    self.session_uuid)

    def _wait_tui_ready(self) -> bool:
        """Poll until the live input prompt is on screen AND quiescent.

        A ready marker alone is NOT enough: on a cold fresh-spawn the markers
        (❯ / shortcut hint / "│ >") flash on the splash screen at ~2s while the
        TUI is still repainting its welcome chrome and the 4 MCP servers are
        still loading — a paste fired into that window gets dropped or wiped on
        the next re-render (the intermittent inject failure). So we additionally
        require the marker-bearing frame to be captured UNCHANGED twice in a row
        (quiescent = done repainting) before declaring ready, and we dismiss any
        blocking startup menu first. Returns True on ready, False on timeout —
        the caller submits optimistically and relies on the post-submit
        verify/resend loop to recover."""
        deadline = time.monotonic() + ALT_TUI_BOOT_SECS
        last_pane = None
        stable_hits = 0
        dismissed = 0
        while time.monotonic() < deadline:
            if not self._alive():
                return False
            pane = _tmux("capture-pane", "-p", "-t", self.name).stdout

            # Clear a blocking startup menu before it can swallow the prompt.
            # Enter accepts the highlighted default (option 1 = the safe/proceed
            # choice). Loop to peel stacked dialogs; the cap stops key-spamming
            # the real prompt if detection ever misfires.
            if _is_interstitial(pane) and dismissed < 5:
                log.warning("alt: interstitial menu on %s — accepting default #%d",
                            self.name, dismissed + 1)
                _tmux("send-keys", "-t", self.name, "Enter")
                dismissed += 1
                stable_hits = 0
                last_pane = None
                time.sleep(0.8)
                continue

            if _real_prompt_marker(pane) and pane == last_pane:
                stable_hits += 1
                if stable_hits >= 2:
                    time.sleep(0.6)
                    return True
            else:
                stable_hits = 0
            last_pane = pane
            time.sleep(0.4)
        log.warning("alt: TUI boot timeout for %s, continuing optimistically", self.name)
        return False

    def send_prompt(self, prompt: str) -> bool:
        """Paste the prompt, CONFIRM its own text rendered in the input box,
        THEN submit. The fresh-spawn race: a paste fired while the TUI is still
        settling (splash chrome, MCP loading) is dropped/wiped on re-render, so
        Enter hits an empty box and the turn never starts (the bug). Verifying
        the probe text appeared before pressing Enter closes that race;
        re-paste up to 3x. Returns True if a confirmed paste was submitted,
        False if we pressed Enter as a last-ditch with no visible paste (the
        caller's transcript-growth verify is the final backstop either way)."""
        probe = _paste_probe(prompt)
        for paste_try in range(3):
            if paste_try > 0:
                self._clear_input()  # drop any stale half-paste before retrying
            # multi-line via bracketed paste — avoids REPL submitting per line
            _tmux("set-buffer", "--", prompt)
            _tmux("paste-buffer", "-p", "-t", self.name)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                time.sleep(0.2)
                pane = self.pane_text()
                # status-bar churn (MCP counts settling, token cell) can't satisfy
                # this — only the prompt's own text or the paste chip does.
                if (probe and probe in pane) or "[Pasted text #" in pane:
                    # The TUI debounces bracketed paste; let the box settle, THEN
                    # submit as a distinct key event so Enter isn't absorbed.
                    time.sleep(ALT_PASTE_SETTLE_SECS)
                    _tmux("send-keys", "-t", self.name, "Enter")
                    return True
            log.warning("alt: paste not visible on %s, re-paste %d/3",
                        self.name, paste_try + 1)
        # Never confirmed after retries — submit anyway so the outer verify loop
        # can observe the non-acceptance and escalate.
        _tmux("send-keys", "-t", self.name, "Enter")
        return False

    def _clear_input(self) -> None:
        """Dismiss any interstitial and clear a half-entered line so a resend
        isn't appended to a stale paste."""
        _tmux("send-keys", "-t", self.name, "Escape")
        time.sleep(0.2)
        _tmux("send-keys", "-t", self.name, "C-u")
        time.sleep(0.2)

    def pane_text(self) -> str:
        return _tmux("capture-pane", "-p", "-t", self.name).stdout

    def kill(self) -> None:
        _tmux("kill-session", "-t", self.name)


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def _parse_line(
    obj: dict,
    text_acc: list[str],
    pending_tools: dict[str, str],
    done_tools: list[str],
) -> tuple[str | None, bool]:
    """Update accumulators from one JSONL line.

    Handles two message types:
    - type=="assistant": extract tool_use (→ pending) and text blocks
    - type=="user": extract tool_result completions (pending → done)

    Returns (stop_reason, had_text) where had_text is True iff THIS assistant
    message carried a visible text block. With extended thinking enabled, the
    CLI writes the thinking block as its own assistant record that already
    carries stop_reason='end_turn', ~seconds before the visible-text record
    (also end_turn). Reporting had_text lets the caller ignore that premature
    thinking-only end_turn and wait for the real answer.
    """
    msg_type = obj.get("type")

    if msg_type == "user":
        # Tool results land here — move pending → done to mark completion
        msg = obj.get("message") or {}
        for block in msg.get("content", []):
            if block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id", "")
                if tool_id and tool_id in pending_tools:
                    done_tools.append(pending_tools.pop(tool_id))
        return None, False

    if msg_type != "assistant":
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
            tool_id = block.get("id") or f"_noid_{len(pending_tools)}"
            tool_name = block.get("name", "tool")
            tool_input = block.get("input") or {}
            pending_tools[tool_id] = _tool_display(tool_name, tool_input)

    return msg.get("stop_reason"), had_text  # 'end_turn' | 'tool_use' | None


def _build_output(
    text_acc: list[str],
    pending_tools: dict[str, str],
    done_tools: list[str],
) -> str:
    """Render structured progress + answer into a single Slack-ready string.

    ✅ Read(src/app.py)
    ✅ Bash(git log --oneline)
    🔄 Atlassian:getJiraIssue(HNWI-123)…

    [answer text]
    """
    from collections import Counter
    lines: list[str] = []
    if done_tools:
        type_counts: Counter = Counter(t.split("(")[0] for t in done_tools)
        parts = [
            f"{k} ×{v}" if v > 1 else k
            for k, v in type_counts.most_common(6)
        ]
        if len(type_counts) > 6:
            parts.append(f"+{len(type_counts) - 6} more")
        lines.append(f":white_check_mark: {len(done_tools)} tools — {', '.join(parts)}")
    for name in pending_tools.values():
        lines.append(f":hourglass_flowing_sand: {name}…")

    progress = "\n".join(lines)
    text = "".join(text_acc).strip()

    if progress and text:
        return f"{progress}\n\n{text}"
    return progress or text or "_(empty response)_"


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


def _wait_for_accept(sess: TmuxSession, tail_offset: int) -> bool:
    """After submit, the CLI appends the user turn to the transcript right away.
    Transcript growth past tail_offset = the prompt landed. Returns False if the
    session dies or nothing lands within ALT_SUBMIT_VERIFY_SECS (→ resend)."""
    deadline = time.monotonic() + ALT_SUBMIT_VERIFY_SECS
    while time.monotonic() < deadline:
        if sess.jsonl.exists() and sess.jsonl.stat().st_size > tail_offset:
            return True
        if not sess._alive():
            return False
        time.sleep(0.3)
    return False


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

    # Submit + verify the prompt actually landed in the transcript. The paste
    # can be lost if the TUI is still settling (cold start, MCP loading), so we
    # resend until the transcript grows past tail_offset. Cause-agnostic: any
    # reason the prompt fails to land triggers a resend, not just one mode.
    accepted = False
    for submit_try in range(1 + ALT_SUBMIT_RETRIES):
        if submit_try > 0:
            if not sess._alive():
                break
            log.warning("alt: prompt not accepted on %s, resend %d/%d",
                        sess.name, submit_try, ALT_SUBMIT_RETRIES)
            sess._clear_input()
            sess._wait_tui_ready()
        sess.send_prompt(prompt)
        if _wait_for_accept(sess, tail_offset):
            accepted = True
            break

    if not accepted:
        # Either the session died, or the prompt never landed after all resends.
        msg = ("_(⚠️ proses berhenti mendadak — kemungkinan crash atau auth gagal)_"
               if not sess._alive()
               else "_(⚠️ prompt gagal terkirim ke TUI setelah beberapa percobaan)_")
        log.error("alt: prompt never accepted on %s after %d tries",
                  sess.name, 1 + ALT_SUBMIT_RETRIES)
        on_update(msg, [])
        return msg, sess.session_uuid

    text_acc: list[str] = []
    pending_tools: dict[str, str] = {}  # tool_use_id → display string (in-flight)
    done_tools: list[str] = []          # ordered list of completed tool displays
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
                        stop, had_text = _parse_line(obj, text_acc, pending_tools, done_tools)
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
                            final = _build_output(text_acc, pending_tools, done_tools)
                            on_update(final, [])
                            return final, sess.session_uuid
                    last_change = now
                    idle_pane_streak = 0  # fresh bytes → not idle
                    touch(thread_ts)

        # --- throttled live update ---
        if now - last_flush >= ALT_FLUSH_SECS and (text_acc or pending_tools or done_tools):
            on_update(_build_output(text_acc, pending_tools, done_tools), [])
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

    final = _build_output(text_acc, pending_tools, done_tools)
    on_update(final, [])
    return final, sess.session_uuid


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
