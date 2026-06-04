# Plan — Alt Runner (pola "shannon": tmux + send-keys + tail JSONL)

Status: **DRAFT untuk review Adi. Belum ada kode diubah.**
Referensi: https://github.com/dexhorthy/shannon · pelengkap dari `STREAMING_PLAN.md`.

## 0. Tujuan & batasan dari Adi

- Bikin **runner method ALTERNATIF baru** — bukan ganti yang sekarang.
- Pemicu: kalau **teks awal prompt = `[alt]`**, pesan itu di-route ke runner baru
  ini. Marker `[alt]` **di-strip** sebelum prompt diproses.
- Runner baru pakai pola shannon: `claude` **interaktif** ditahan hidup di **tmux**,
  prompt dikirim via **`send-keys`**, output dibaca dari **tail file JSONL
  transcript** — BUKAN `claude -p` / stream-json-stdout.
- Runner lama (`-p` one-shot, `claude_runner.py`) tetap jadi **default** dan
  tidak disentuh. `[alt]` = jalur eksperimen yang hidup berdampingan, biar Adi
  bisa banding-bandingin live tanpa ngorbanin jalur yang udah proven.

## 1. Fakta yang sudah diverifikasi (bukan asumsi)

Dicek di mesin ini, CLI `2.1.162`:

- `--session-id <uuid>` **ADA** → kita bisa **paksa session id yang kita tentukan
  sendiri** saat spawn. Konsekuensinya path transcript jadi deterministik, gak
  perlu tebak file "paling baru" (hilangkan race).
- `-r, --resume [value]` & `-c, --continue` ADA → buat respawn nyambung lagi
  kalau tmux mati / bridge restart.
- Transcript ditulis ke `$CLAUDE_CONFIG_DIR/projects/<mangled-cwd>/<session-id>.jsonl`.
  Untuk bridge: `CLAUDE_CONFIG_DIR=~/ClaudeConfigs/adi.novriansyah` (di-inject
  systemd) + cwd workspace → dir nyata:
  `~/ClaudeConfigs/adi.novriansyah/projects/-home-scriberion--openclaw-agents-nafutech-workspace/`.
  (`<mangled-cwd>` = path absolut, `/` diganti `-`.)
- `tmux` ada di `/usr/bin/tmux`.
- **Struktur event JSONL** (dari transcript nyata): tiap baris = 1 objek JSON
  dengan `type` ∈ {`user`, `assistant`, `attachment`, `queue-operation`,
  `ai-title`, `last-prompt`, ...}. Yang kepake:
  - `type:"assistant"` → `message.content[]` (blok `text` + blok `tool_use`).
  - tool result nyangkut di field `toolUseResult`.
  - metadata tiap baris: `sessionId`, `cwd`, `uuid`, `parentUuid`, `timestamp`,
    `requestId`, `version`.
  - **DIKOREKSI (cek transcript nyata 2026-06-04):** baris `type:"assistant"`
    nyimpen **full message API** termasuk **`message.stop_reason`**. Jadi sinyal
    "turn selesai" itu **DETERMINISTIK**, bukan heuristik:
    - baris `assistant` terakhir `stop_reason:"end_turn"` → Claude kelar, nunggu
      input user.
    - `stop_reason:"tool_use"` → masih di tengah tool loop (bakal nyusul
      `toolUseResult` lalu baris `assistant` lagi).
    - **Bonus sentinel:** abis `end_turn` muncul baris `last-prompt` + `ai-title`
      yang cuma ke-tulis pas turn beneran tutup → konfirmator kedua.
    Klaim lama "TIDAK punya sinyal terminal" **salah & dicoret**. Konsekuensi:
    Risk #1 di §4 turun kelas — quiescence bukan lagi detektor utama.

## 1.5 Architecture design

### Diagram komponen (alt vs default berdampingan)

```
                            Slack (Socket Mode)
                                   │  app_mention / message(im)
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│ app.py                                                                 │
│   handle_app_mention / handle_message  → gate sender==Adi              │
│   _dispatch():                                                         │
│     ├─ is_alt, clean = _detect_alt(raw_text)   ← marker [alt]?         │
│     ├─ prompt = _build_prompt(event, clean)                            │
│     ├─ ack = chat_postMessage("thinking…"); _pending[thread]=…         │
│     ├─ state = thread_store.get(thread_ts)                             │
│     │                                                                  │
│     │   is_alt == False                  is_alt == True                │
│     │       │                                  │                       │
│     ▼       ▼                                  ▼                       │
│  ┌─────────────────────┐          ┌──────────────────────────────┐    │
│  │ claude_runner.run() │          │ alt_runner.run_alt()         │    │
│  │ (DEFAULT, zero-     │          │ (jalur [alt], eksperimen)    │    │
│  │  change)            │          │                              │    │
│  │  claude -p          │          │  on_update callback ─────────┼──┐ │
│  │  --output-format    │          │  → throttled chat_update     │  │ │
│  │  json (blocking)    │          │                              │  │ │
│  └─────────┬───────────┘          └──────────────┬───────────────┘  │ │
│            │ (result, sid)                       │ (final, sid)      │ │
│            └───────────────┬──────────────────────┘                 │ │
│                            ▼                                         │ │
│         thread_store.save(thread_ts, {session_id, …, alt fields})    │ │
│                            ▼                                         │ │
│         _chunk_text() → chat_update(ack) + chat_postMessage(cont.)   │ │
└────────────────────────────────────────────────────────────────────┼─┘
                                                                       │
   alt_runner internal (per-thread, hidup di luar request)            │
   ┌───────────────────────────────────────────────────────────┐     │
   │ tmux -L nafutech  (server independen, selamat dari restart) │     │
   │   session nafu_<thread_ts>                                  │     │
   │     └─ claude (interaktif TUI)                              │     │
   │          --session-id <uuid> --disallowed-tools … <model>   │     │
   │                          │ nulis transcript                 │     │
   │                          ▼                                   │     │
   │  $CLAUDE_CONFIG_DIR/projects/<mangled-cwd>/<uuid>.jsonl      │     │
   └───────────┬──────────────────────────────┬────────────────┘     │
               │ tail (seek tail_offset)       │ capture-pane (guard)  │
               ▼                               ▼                       │
        parse stop_reason ────────────────────────────────── on_update┘
        (end_turn=selesai)            (liveness/permission/crash)
```

### Tanggung jawab per komponen

| Komponen | Peran | Berubah? |
|---|---|---|
| `app.py::_detect_alt` | Deteksi & strip marker `[alt]` dari teks mentah | **Baru** (kecil) |
| `app.py::_dispatch` | Pilih runner; ack/`_pending`/`thread_store`/chunking dipakai ulang | Edit kecil |
| `claude_runner.py` | Runner default `-p` one-shot | **Zero change** |
| `alt_runner.py` | Lifecycle tmux, kirim prompt, tail JSONL, deteksi turn, callback | **Baru** |
| `alt_runner::TmuxSession` | Spawn/respawn/reap 1 proses claude interaktif per-thread | **Baru** |
| `alt_runner::TranscriptTail` | Byte-offset tail + parse `stop_reason`/text/tool_use | **Baru** |
| `thread_store.py` | Persist mapping thread→session (+ field alt opsional) | Field opsional |
| `config.py` | Konstanta `ALT_*` + `CLAUDE_CONFIG_DIR` | Tambah var |

### Sequence satu turn `[alt]`

1. `_dispatch` deteksi `[alt]`, post ack `thinking…`, taruh di `_pending`.
2. `alt_runner.run_alt(prompt, thread_ts, session_id, on_update)`:
   - `TmuxSession.ensure()` → `has-session`? reuse : spawn (`--session-id` baru / `--resume` lama).
   - `tail_offset = os.stat(jsonl).st_size` **sebelum** kirim (file mungkin belum ada → offset 0).
   - kirim prompt via bracketed paste (§3.2) + `Enter`.
   - loop: tail baris baru → `on_update(partial, breadcrumbs)` ter-throttle; berhenti saat `stop_reason:"end_turn"` (§4) / quiescence fallback / hard timeout.
   - return `(final_text, session_id)`.
3. `_dispatch` simpan state (termasuk `tmux_name`/`jsonl_path`), chunk + render final ke Slack, pop `_pending`.

### Keputusan desain (dan kenapa)

- **Berdampingan, bukan ganti** → `claude_runner` proven; `[alt]` boleh gagal tanpa nyentuh jalur produksi.
- **`--session-id` di-generate sendiri** → path transcript deterministik, hilangkan race "file terbaru".
- **Socket tmux terdedikasi `-L nafutech`** → isolasi dari tmux pribadi Adi; env systemd (`CLAUDE_CONFIG_DIR`) kebawa.
- **`stop_reason` detektor utama, pane guard** → deterministik dulu, heuristik cuma buat hal yang JSONL gak bisa liat (§4).
- **State per-thread hidup di luar request** → multi-turn nyambung; reaper cegah proses claude numpuk.

## 2. Routing `[alt]` (perubahan kecil di `app.py`)

Deteksi marker dilakukan di **teks user mentah** (setelah buang `<@bot>` mention),
SEBELUM dibungkus template prompt NafuTech — karena template nempelin banyak teks
di depan.

```
ALT_MARKER = "[alt]"

def _detect_alt(user_text: str) -> tuple[bool, str]:
    s = user_text.lstrip()
    if s[:len(ALT_MARKER)].lower() == ALT_MARKER:        # case-insensitive
        return True, s[len(ALT_MARKER):].lstrip()        # strip marker
    return False, user_text
```

- `_dispatch` panggil `_detect_alt` di awal. Kalau `is_alt`:
  - pakai teks yang udah di-strip buat `_build_prompt` (marker gak ikut ke Claude).
  - route ke `alt_runner.run_alt(...)` (bukan `claude_runner.run`).
- Kalau bukan alt → persis seperti sekarang.
- Placeholder/ack, `_pending`, `thread_store`, chunking, gate keamanan: **dipakai
  ulang apa adanya** untuk dua-duanya.

## 3. Modul baru: `src/alt_runner.py`

Signature dibikin mirror callback-style (sejajar rencana streaming):

```
def run_alt(prompt: str, thread_ts: str, session_id: str | None,
            on_update: Callable[[str, list[str]], None]) -> tuple[str, str]:
    # return (final_text, session_id); on_update(partial_text, tool_breadcrumbs)
```

Komponen internal:

### 3.1 Lifecycle tmux session (per-thread)
- Pakai **socket tmux terdedikasi**: `tmux -L nafutech ...` — biar gak nabrak tmux
  pribadi Adi & env-nya kebawa dari proses bridge (systemd udah inject
  `CLAUDE_CONFIG_DIR`).
- Nama session = `nafu_<sanitized thread_ts>` (titik → underscore).
- **Spawn** (kalau belum ada / udah mati):
  - Generate `session_uuid` baru (uuid4) → simpan.
  - `tmux -L nafutech new-session -d -s <name> -x 200 -y 50 \
       claude --model <m> --permission-mode <pm> \
              --disallowed-tools <CSV> --session-id <session_uuid>`
    (turunan thread yang sudah punya session → tambah `--resume <session_id>`
    ganti `--session-id`).
  - **`--disallowed-tools` & `CLAUDE_CONFIG_DIR` WAJIB tetap ke-pass** ke proses
    interaktif ini — sama persis jaminan keamanan runner lama (lihat
    `claude_runner.DISALLOWED_TOOLS`). Ini titik paling gampang ke-skip kalau
    pindah dari `-p`.
- **Reaper**: thread background yang `tmux kill-session` untuk session idle
  > `ALT_IDLE_TTL` (mis. 30 menit) + saat shutdown bridge. Map state:
  `thread_ts → {tmux_name, session_id, jsonl_path, tail_offset, last_active}`.
  Risiko utama = session nyangkut (tiap session = 1 proses claude hidup nahan
  konteks); reaper kudu rapi.

### 3.2 Kirim prompt (gotcha multi-line)
- Prompt NafuTech itu **multi-line**. `send-keys -l "...\n..."` bakal nerjemahin
  tiap `\n` jadi Enter → REPL submit kepotong di tengah. Solusi:
  **bracketed paste** via buffer:
  - `tmux -L nafutech set-buffer -- "<prompt>"`
  - `tmux -L nafutech paste-buffer -p -t <name>`  (`-p` = bracketed, REPL gak
    submit per baris)
  - lalu `tmux -L nafutech send-keys -t <name> Enter` (submit sekali).
- Escaping/`--` dipakai biar prompt yang ada `-` di depan gak ke-parse jadi flag.

### 3.3 Tail JSONL + parse
- Path transcript deterministik dari `session_uuid` (§1).
- **Byte-offset WAJIB (transcript numpuk lintas-turn):** transcript itu append
  terus dari turn pertama — jadi JANGAN cari `end_turn` mana aja, nanti
  ke-trigger sama jawaban turn SEBELUMNYA. Caranya: `os.stat().st_size` **sebelum**
  kirim prompt → simpan jadi `tail_offset`. Abis kirim, `seek(tail_offset)` lalu
  scan cuma baris baru, cari `end_turn` **pertama** setelah offset itu. Karena
  `--session-id` udah bikin path deterministik, ini gampang.
- Tiap baris baru → `json.loads`, lihat `type`:
  - `assistant` + blok `text` → **akumulasi** teks (buat live-update).
  - `assistant` + blok `tool_use` → breadcrumb (`🔧 Bash…`, `📖 Read…`).
  - `assistant` + `message.stop_reason` → **sinyal selesai/lanjut** (lihat §4).
  - `toolUseResult` → opsional tanda tool kelar.
- Parser **defensif**: `type` gak dikenal → skip; jangan crash kalau format
  berubah antar versi.

### 3.4 Update ke Slack (throttled)
- Sama prinsip `STREAMING_PLAN.md` §5.2: jangan `chat_update` tiap delta.
  Flush ke placeholder tiap ~1.5–2s **atau** pas ada milestone tool.
- Final turn → render teks final, chunk pakai `_chunk_text` yang udah ada
  kalau > `SLACK_CHUNK_SIZE`.

## 4. Deteksi "turn selesai" — DETERMINISTIK via `stop_reason` (turun kelas dari Risk #1)

Temuan §1 (cek transcript nyata 2026-06-04) bikin ini bukan lagi risiko terbesar.
JSONL nyimpen `message.stop_reason` per baris assistant → sinyal selesai
deterministik. Pembagian peran jelas:

1. **DETEKTOR UTAMA — `stop_reason` (deterministik):** scan baris baru setelah
   `tail_offset` (§3.3). Baris `assistant` pertama dengan
   `stop_reason:"end_turn"` = turn selesai → flush final. `stop_reason:"tool_use"`
   = masih jalan, terus tail. Sentinel `last-prompt`/`ai-title` abis `end_turn` =
   konfirmator kedua kalau mau ekstra yakin.
2. **PANE tmux = liveness + edge-case guard (BUKAN buat deteksi-selesai):**
   `tmux capture-pane -p` dipegang sebagai safety net untuk hal yang JSONL gak
   bisa liat:
   - **Permission prompt nge-block** — tool nunggu approval → gak ada `end_turn`
     DAN gak ada progress, JSONL diem. (Sebagian ke-mitigasi `--disallowed-tools`
     + permission-mode, tapi gak 100%.)
   - **Crash sebelum nulis JSONL** — flag salah / auth 401 / OOM → file gak pernah
     muncul atau berhenti mendadak. Pane nangkep, JSONL enggak.
   - **Mastiin TUI udah boot/ready** sebelum kita paste prompt.
3. **Quiescence `ALT_QUIESCE_SECS` → turun jadi FALLBACK timeout doang**, bukan
   detektor utama. Cuma kepake kalau `stop_reason` gak kebaca (format berubah /
   parse gagal).
4. **Hard timeout** keseluruhan turn (`CLAUDE_TIMEOUT`) → kalau lewat, flush apa
   yang ada + tandain "kepotong".

Kalibrasi `ALT_QUIESCE_SECS` jadi gak kritikal lagi (cuma fallback). Yang masih
perlu dites empiris: timing sentinel + handling permission-block via pane.

## 5. Restart resilience
- tmux server independen dari bridge → session **selamat** saat bridge restart;
  tapi state tail in-memory ilang.
- `thread_store` ditambah field alt (`tmux_name`, `session_id`, `jsonl_path`).
  Saat `[alt]` berikutnya di thread itu: cek `tmux has-session` → kalau hidup,
  reuse + re-derive offset dari ujung file; kalau mati, respawn `--resume
  <session_id>` (transcript di disk tetap ada).

## 6. Config baru (`config.py` / `.env`)
- `ALT_MARKER` (default `[alt]`)
- `ALT_TMUX_SOCKET` (default `nafutech`)
- `ALT_IDLE_TTL` (detik, default `1800`)
- `ALT_QUIESCE_SECS` (default `3.5`)
- `CLAUDE_CONFIG_DIR` dibaca buat nyusun path projects (fallback `~/.claude`).

## 7. Yang TIDAK berubah
- `claude_runner.py` (runner default `-p`) — **zero change**.
- `thread_store.py` core (cuma nambah field opsional), gate keamanan, systemd
  unit, identitas bot, `_chunk_text`, `SLACK_CHUNK_SIZE`, `DISALLOWED_TOOLS`.

## 8. Test plan
1. `[alt] halo` (DM) → marker ke-strip, spawn tmux, live-update teks, final 1 pesan.
2. `[alt] cek status VPN` → breadcrumb tool muncul.
3. Pesan tanpa `[alt]` → tetap lewat runner lama (`-p`), gak ada tmux kebikin.
4. Multi-turn `[alt]` di thread sama → reuse session tmux, konteks nyambung.
5. Output > 3800 char → chunking jalan.
6. Idle > TTL → reaper kill session; verif gak ada proses claude nyangkut
   (`tmux -L nafutech ls`).
7. Restart bridge mid-turn → respawn/resume jalan, placeholder gak stuck.
8. Verif `--disallowed-tools` aktif di proses interaktif (coba mancing Claude
   manggil Tier-2 Slack write → harus ditolak harness).

## 9. Urutan kerja (kalau di-ACC)
1. `alt_runner.py` skeleton: tmux spawn + send-keys + tail (tanpa Slack dulu),
   diuji standalone.
2. Deteksi turn-selesai (§4): byte-offset + scan `stop_reason:"end_turn"` (jalur
   utama, straightforward) + pane-guard buat permission-block/crash. (Bukan lagi
   item paling makan waktu — `stop_reason` deterministik.)
3. Routing `[alt]` + callback throttled di `app.py`.
4. Reaper + restart resilience.
5. Test plan §8 → demo ke Adi.

## 10. Lampiran — sketsa kode (ilustratif, BUKAN final)

> Disesuaikan sama API kodebase asli: gaya `config._env`, `thread_store.get/save`,
> dan callback `_dispatch` di `app.py`. Masih DRAFT — belum dites, belum di-commit.

### 10.1 `config.py` (tambahan var, pola `_env` yang udah ada)

```python
# --- alt runner (pola shannon: tmux + tail JSONL) ---
ALT_MARKER = _env("ALT_MARKER", "[alt]")
ALT_TMUX_SOCKET = _env("ALT_TMUX_SOCKET", "nafutech")
ALT_IDLE_TTL = int(_env("ALT_IDLE_TTL", "1800"))        # detik; reaper kill idle session
ALT_QUIESCE_SECS = float(_env("ALT_QUIESCE_SECS", "3.5"))  # fallback, bukan detektor utama
ALT_FLUSH_SECS = float(_env("ALT_FLUSH_SECS", "1.5"))   # throttle chat_update
ALT_TUI_BOOT_SECS = float(_env("ALT_TUI_BOOT_SECS", "8"))  # tunggu TUI ready sebelum paste
# CLAUDE_CONFIG_DIR di-inject systemd; fallback ~/.claude buat run lokal.
CLAUDE_CONFIG_DIR = Path(_env("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
```

### 10.2 `app.py` — deteksi marker + pilih runner (diff inti)

```python
from . import alt_runner          # baru
from .config import ALT_MARKER    # baru

def _detect_alt(user_text: str) -> tuple[bool, str]:
    """True + teks tanpa marker kalau diawali [alt] (case-insensitive)."""
    s = user_text.lstrip()
    if s[: len(ALT_MARKER)].lower() == ALT_MARKER.lower():
        return True, s[len(ALT_MARKER) :].lstrip()
    return False, user_text

def _dispatch(event: dict, client, bot_user_id: str | None) -> None:
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    user = event.get("user")
    raw = event.get("text") or ""
    if bot_user_id:
        raw = raw.replace(f"<@{bot_user_id}>", "").strip()

    is_alt, clean = _detect_alt(raw)                       # <-- baru
    prompt = _build_prompt(event, clean, bot_user_id)      # marker udah ke-strip

    # §11 #1: alt = REPL tunggal per-thread → REJECT (bukan queue) kalau turn
    # sebelumnya di thread ini belum kelar. Paste prompt kedua ke REPL yang
    # lagi jalan = output interleave / korup.
    if is_alt and thread_ts in _pending:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=":warning: Masih ngerjain pesan sebelumnya di thread ini. "
                 "Tunggu kelar dulu ya, baru kirim lagi.",
        )
        return

    ack = client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=":hourglass_flowing_sand: thinking…",
    )
    _pending[thread_ts] = (channel, ack["ts"])
    state = thread_store.get(thread_ts) or {}
    session_id = state.get("session_id")

    try:
        if is_alt:
            # callback throttled: live-update placeholder ack pas streaming
            def on_update(partial: str, crumbs: list[str]) -> None:
                preview = (("\n".join(crumbs) + "\n") if crumbs else "") + partial
                try:
                    client.chat_update(channel=channel, ts=ack["ts"],
                                       text=_chunk_text(preview)[0])
                except Exception:
                    log.debug("alt on_update chat_update skipped", exc_info=True)

            result, new_session_id = alt_runner.run_alt(
                prompt, thread_ts, session_id, on_update
            )
        else:
            result, new_session_id = claude_runner.run(prompt, session_id=session_id)

        thread_store.save(thread_ts, {
            "session_id": new_session_id,
            "channel": channel,
            "last_user": user,
            "runner": "alt" if is_alt else "default",   # field opsional
        })
        # … chunking + chat_update/chat_postMessage persis seperti sekarang …
    except Exception:
        log.exception("run failed (alt=%s)", is_alt)
        client.chat_update(channel=channel, ts=ack["ts"],
                           text=":warning: Maaf, lagi gak bisa proses. Retrigger 1-2 menit lagi.")
    finally:
        _pending.pop(thread_ts, None)
```

### 10.3 `alt_runner.py` (sketsa modul baru)

```python
"""Runner ALTERNATIF (jalur [alt]): claude interaktif di tmux + tail JSONL.

Hidup berdampingan dgn claude_runner.py (-p). Dipilih dari app.py._dispatch
saat teks user diawali ALT_MARKER. Jaminan keamanan identik runner lama:
--disallowed-tools (Tier-2 Slack writes) + CLAUDE_CONFIG_DIR WAJIB ke-pass.
"""
import json, os, subprocess, time, uuid
from pathlib import Path
from typing import Callable

from .claude_runner import DISALLOWED_TOOLS          # reuse daftar yg sama
from .config import (
    ALT_FLUSH_SECS, ALT_QUIESCE_SECS, ALT_TMUX_SOCKET, ALT_TUI_BOOT_SECS,
    CLAUDE_CLI, CLAUDE_CONFIG_DIR, CLAUDE_MODEL, CLAUDE_PERMISSION_MODE,
    CLAUDE_TIMEOUT, NAFUTECH_WORKSPACE,
)
import logging
log = logging.getLogger(__name__)

OnUpdate = Callable[[str, list[str]], None]


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", "-L", ALT_TMUX_SOCKET, *args],
                          capture_output=True, text=True)

def _session_name(thread_ts: str) -> str:
    return "nafu_" + thread_ts.replace(".", "_").replace("/", "_")

def _mangled_cwd() -> str:
    # path absolut, '/' → '-' (lihat §1)
    return str(NAFUTECH_WORKSPACE).replace("/", "-")

def _transcript_path(session_uuid: str) -> Path:
    return CLAUDE_CONFIG_DIR / "projects" / _mangled_cwd() / f"{session_uuid}.jsonl"


class TmuxSession:
    """1 proses claude interaktif per thread, ditahan hidup di tmux."""

    def __init__(self, thread_ts: str, session_id: str | None):
        self.name = _session_name(thread_ts)
        self.session_uuid = session_id or str(uuid.uuid4())
        self.jsonl = _transcript_path(self.session_uuid)

    def _alive(self) -> bool:
        return _tmux("has-session", "-t", self.name).returncode == 0

    def ensure(self) -> None:
        if self._alive():
            return
        id_flag = (["--resume", self.session_uuid]
                   if self.jsonl.exists() else
                   ["--session-id", self.session_uuid])
        cmd = [CLAUDE_CLI, "--model", CLAUDE_MODEL,
               "--permission-mode", CLAUDE_PERMISSION_MODE,
               "--disallowed-tools", ",".join(DISALLOWED_TOOLS), *id_flag]
        # new-session bawa env proses bridge (CLAUDE_CONFIG_DIR dari systemd)
        _tmux("new-session", "-d", "-s", self.name, "-x", "200", "-y", "50",
              "-c", str(NAFUTECH_WORKSPACE), *cmd)
        self._wait_tui_ready()

    def _wait_tui_ready(self) -> None:
        # tunggu prompt TUI muncul sebelum paste (hindari ketik ke layar boot)
        deadline = time.monotonic() + ALT_TUI_BOOT_SECS
        while time.monotonic() < deadline:
            pane = _tmux("capture-pane", "-p", "-t", self.name).stdout
            if "│ >" in pane or "Welcome" in pane or "? for shortcuts" in pane:
                return
            time.sleep(0.3)
        log.warning("alt: TUI boot timeout utk %s, lanjut optimistik", self.name)

    def send_prompt(self, prompt: str) -> None:
        # multi-line → bracketed paste biar REPL gak submit per baris (§3.2)
        _tmux("set-buffer", "--", prompt)
        _tmux("paste-buffer", "-p", "-t", self.name)
        _tmux("send-keys", "-t", self.name, "Enter")

    def pane(self) -> str:
        return _tmux("capture-pane", "-p", "-t", self.name).stdout

    def kill(self) -> None:
        _tmux("kill-session", "-t", self.name)


def _parse_line(obj: dict, text_acc: list[str], crumbs: list[str]) -> str | None:
    """Update akumulator dari 1 baris JSONL. Return stop_reason kalau ada."""
    if obj.get("type") != "assistant":
        return None
    msg = obj.get("message") or {}
    for block in msg.get("content", []):
        if block.get("type") == "text":
            text_acc.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            crumbs.append(f"🔧 {block.get('name','tool')}…")
    return msg.get("stop_reason")     # 'end_turn' | 'tool_use' | None


def run_alt(prompt: str, thread_ts: str, session_id: str | None,
            on_update: OnUpdate) -> tuple[str, str]:
    sess = TmuxSession(thread_ts, session_id)
    sess.ensure()
    touch(thread_ts)                       # §11 #2: tandai aktif (cegah reaper)

    # byte-offset WAJIB: snapshot ukuran SEBELUM kirim (§3.3)
    tail_offset = sess.jsonl.stat().st_size if sess.jsonl.exists() else 0
    sess.send_prompt(prompt)

    text_acc: list[str] = []
    crumbs: list[str] = []
    last_flush = 0.0
    last_change = time.monotonic()
    hard_deadline = time.monotonic() + CLAUDE_TIMEOUT
    pos = tail_offset

    while True:
        if time.monotonic() > hard_deadline:
            text_acc.append("\n_(⏱ kepotong: lewat batas waktu)_")
            break

        if sess.jsonl.exists() and sess.jsonl.stat().st_size > pos:
            with sess.jsonl.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                for raw in fh:                       # baris BARU only
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue                     # parser defensif (§3.3)
                    stop = _parse_line(obj, text_acc, crumbs)
                    if stop == "end_turn":           # DETEKTOR UTAMA (§4)
                        pos = fh.tell()
                        _flush(on_update, text_acc, crumbs)
                        return "".join(text_acc).strip(), sess.session_uuid
                pos = fh.tell()
            last_change = time.monotonic()
            touch(thread_ts)               # §11 #2: ada progres → refresh TTL

        now = time.monotonic()
        if now - last_flush >= ALT_FLUSH_SECS and text_acc:
            _flush(on_update, text_acc, crumbs)
            last_flush = now

        # §11 #3: alive-check pas idle — proses claude MATI sebelum/saat nulis
        # JSONL (flag salah / 401 / OOM / crash) → has-session False. Tangkap
        # di sini biar gak ngegantung sampe hard timeout. CATATAN: ini cuma
        # nangkep proses MATI; proses HIDUP tapi stuck (permission-block) =
        # ranah pane-guard §4, beda problem — jangan dicampur.
        if now - last_change > ALT_QUIESCE_SECS and not sess._alive():
            text_acc.append("\n_(⚠️ proses berhenti mendadak — kemungkinan crash)_")
            break

        # FALLBACK quiescence: stop_reason gak kebaca + diem lama + pane gak progres
        if now - last_change > ALT_QUIESCE_SECS and text_acc:
            if _pane_idle(sess):
                break
        time.sleep(0.4)

    final = "".join(text_acc).strip() or "_(empty response)_"
    return final, sess.session_uuid


def _flush(on_update: OnUpdate, text_acc: list[str], crumbs: list[str]) -> None:
    try:
        on_update("".join(text_acc).strip(), list(crumbs))
    except Exception:
        log.debug("alt on_update raised; ignored", exc_info=True)

def _pane_idle(sess: TmuxSession) -> bool:
    # heuristik fallback: prompt input balik muncul = turn kemungkinan tutup.
    # juga tempat deteksi permission-block (pane minta approval) → bisa di-flag.
    pane = sess.pane()
    return "│ >" in pane.splitlines()[-3:] if pane else False
```

### 10.4 Reaper (background, dipanggil dari `app.py::main`)

```python
# alt_runner.py
_SESSIONS: dict[str, float] = {}   # tmux_name -> last_active (monotonic)

def touch(thread_ts: str) -> None:
    _SESSIONS[_session_name(thread_ts)] = time.monotonic()

def reap_idle() -> None:
    """Panggil periodik (thread/timer di app.main). Kill session lewat TTL."""
    from .config import ALT_IDLE_TTL
    now = time.monotonic()
    listing = _tmux("list-sessions", "-F", "#{session_name}").stdout.splitlines()
    for name in listing:
        if not name.startswith("nafu_"):
            continue
        last = _SESSIONS.get(name)
        if last is None:
            # §11 #2: orphan — bridge restart bikin _SESSIONS kosong, tapi
            # tmux session selamat. JANGAN langsung kill (last=0 → lewat TTL
            # seketika = bunuh sesi hidup). Adopsi: kasih TTL fresh biar turn
            # [alt] berikutnya di thread itu bisa reuse + resume.
            _SESSIONS[name] = now
            log.info("alt reaper: adopt orphan session %s", name)
            continue
        if now - last > ALT_IDLE_TTL:
            _tmux("kill-session", "-t", name)
            _SESSIONS.pop(name, None)
            log.info("alt reaper: killed idle session %s", name)
```

### 10.5 `thread_store.py` — gak perlu ubah API

Skema JSON cuma nambah key opsional (`runner`, `tmux_name`, `jsonl_path`).
`get`/`save` udah generic `dict`, jadi **zero code change** di module ini —
cukup `_dispatch` nulis field tambahan saat `save`.

> Catatan implementasi yang belum kelar (sengaja, butuh tes empiris):
> deteksi **permission-block** lewat pane (string approval-nya apa), string
> "TUI ready" yang stabil antar versi CLI, dan kalibrasi `ALT_FLUSH_SECS`
> vs rate-limit Slack `chat.update`.

## 11. Concurrency & liveness

Tiga celah yang muncul begitu session tmux **hidup di luar request** (beda dari
runner `-p` lama yang sekali-jalan-langsung-mati, jadi gak punya state hidup).
Ketiganya kecil, lokal, **gak ngubah arsitektur §1.5** — cuma nambal lifecycle
yang udah ada. Patch konkretnya udah masuk ke sketsa §10 (ditandai `§11 #n`);
section ini yang nyimpen alasannya.

### #1 — Pesan barengan di satu thread → REJECT, bukan queue

**Masalah:** `[alt]` = **satu REPL claude interaktif per-thread**. Kalau pesan
`[alt]` kedua masuk ke thread yang turn-nya belum kelar, paste prompt kedua ke
REPL yang lagi jalan = teks nyampur di input box / submit nyasar di tengah turn
= output korup. Beda sama runner `-p` (tiap call proses sendiri, aman paralel).

**Keputusan:** **reject** (bukan antri). Antrian nambah state + risiko stuck;
untuk pemakaian NafuTech (Adi solo, jarang dobel-tembak satu thread) reject jauh
lebih simpel & jujur. `_dispatch` cek `thread_ts in _pending` di awal jalur alt
→ balas singkat "tunggu kelar dulu", `return`.

- Guard reuse `_pending` yang udah ada (di-set abis ack, di-pop di `finally`) —
  gak nambah struktur baru.
- Scope khusus jalur `[alt]`; jalur default `-p` **tidak** kena reject (aman
  paralel).
- Patch: §10.2 (`_dispatch`).

### #2 — `touch()` beneran dipanggil + adopt-orphan pas restart

**Masalah:** `_SESSIONS` (map `tmux_name → last_active`) itu **in-memory**.
Dua bug nyangkut di sketsa awal:
1. `touch()` didefinisiin tapi **gak pernah dipanggil** → `last_active` gak
   pernah ke-update → reaper salah baca "idle" walau session lagi aktif.
2. Pas **bridge restart**, `_SESSIONS` balik kosong, tapi session tmux
   **selamat** (server tmux independen, §5). Reaper lama baca `last = 0` →
   `now - 0 > TTL` selalu true → **session hidup ke-bunuh seketika** abis
   restart. Hilang konteks padahal transcript & proses masih utuh.

**Keputusan:**
- **touch:** panggil `touch(thread_ts)` pas `ensure()` sukses + tiap ada progres
  baca JSONL di loop `run_alt`. TTL jadi cermin aktivitas beneran.
- **adopt-orphan:** di `reap_idle`, session `nafu_*` yang **gak ada di
  `_SESSIONS`** = orphan hasil restart → **adopsi** (set `last_active = now`),
  **jangan kill**. Turn `[alt]` berikutnya di thread itu reuse via
  `has-session` + `--resume` (§5). Reaper baru bunuh kalau betul-betul lewat TTL
  **setelah** diadopsi.
- Patch: §10.3 (`run_alt` panggil `touch`), §10.4 (`reap_idle` cabang
  `last is None` → adopt).

> Konsekuensi yang disengaja: orphan dapet TTL penuh sekali lagi sejak diadopsi
> (bukan sejak last-active asli, yang udah ilang). Worst case satu session idle
> nahan ~1 TTL ekstra abis restart — murah, dan jauh lebih aman ketimbang risiko
> bunuh sesi yang lagi mid-turn.

### #3 — Alive-check pas idle (nangkep CRASH, bukan stuck)

**Masalah:** kalau proses claude **mati** sebelum/saat nulis JSONL (flag salah,
auth 401, OOM, crash), JSONL gak pernah nongol atau berhenti mendadak. Tanpa
cek apa-apa, `run_alt` nge-tail file kosong sampai **hard timeout**
(`CLAUDE_TIMEOUT`) baru nyerah → Adi nungguin placeholder "thinking…" lama
padahal prosesnya udah almarhum.

**Keputusan:** pas loop lagi idle (gak ada byte baru > `ALT_QUIESCE_SECS`), cek
`sess._alive()` (`tmux has-session`). Kalau **mati** → flush apa yang ada +
tandai "proses berhenti mendadak", break. Fail-fast, gak nunggu hard timeout.

**Batas tegas — JANGAN dicampur:** alive-check ini **cuma** nangkep proses
**MATI** (`has-session` False). Kasus proses **HIDUP tapi stuck** — paling
sering **permission prompt nge-block** (tool nunggu approval, JSONL diem, tapi
`has-session` tetap True) — **bukan** wilayah sini. Itu tetap ranah
**pane-guard §4** yang udah jujur ditandai "butuh tes empiris" (string approval
di pane belum diverifikasi). Dua-duanya keliatan sama dari sisi JSONL (diem),
tapi beda total dari sisi proses: mati vs hidup. Alive-check misahin keduanya —
yang mati di-tutup di sini, yang hidup-tapi-stuck dilempar ke §4. Hard timeout
(§4 poin 4) tetap jaring terakhir buat dua-duanya.

- Patch: §10.3 (`run_alt`, cabang `not sess._alive()` sebelum quiescence
  fallback).

### Tambahan test (lanjutan §8)

9. **Reject barengan:** dua `[alt]` cepat di thread sama → yang kedua dibalas
   "tunggu kelar dulu", REPL gak korup, yang pertama kelar normal.
10. **Adopt-orphan:** restart bridge pas ada session `[alt]` idle → reaper
    adopsi (log "adopt orphan"), session gak ke-kill, turn `[alt]` berikut reuse
    + resume nyambung.
11. **Crash fail-fast:** paksa claude mati (mis. flag invalid) → `run_alt`
    nutup via alive-check < quiesc+ε, BUKAN nunggu `CLAUDE_TIMEOUT`; placeholder
    di-update "berhenti mendadak".
