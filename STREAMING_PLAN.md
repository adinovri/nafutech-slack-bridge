# Plan Change — Bridge NafuTech: dari one-shot `claude -p` ke streaming multi-turn

Status: DRAFT untuk review Adi. Belum ada kode diubah.

## 1. Bridge kita sekarang (baseline)

File-file inti (`src/`):

- `app.py` — Slack socket-mode listener. Gate: cuma Adi (`TRIGGER_USER_ID`)
  yang bisa trigger; DM = no-mention, channel = perlu `@mention`. Per pesan:
  1. Post `:hourglass: thinking…` (ack placeholder), simpan di `_pending`.
  2. Ambil `session_id` dari `thread_store` (per `thread_ts`).
  3. Panggil `claude_runner.run(prompt, session_id)` — **blocking**.
  4. Hasil final → `chat_update` placeholder + chunk sisanya (`SLACK_CHUNK_SIZE=3800`).
  5. Error → update placeholder jadi pesan "retrigger".
- `claude_runner.py` — `subprocess.run([claude, -p, prompt, --output-format json,
  --resume <sid>, --disallowed-tools ...])`, timeout 600s, parse `result` +
  `session_id` dari JSON tunggal di akhir.
- `thread_store.py` — map `thread_ts → {session_id, channel, last_user}`, 1 JSON/thread.
- Jalan via systemd `--user` (`nafutech-slack-bridge.service`), inject
  `CLAUDE_CONFIG_DIR=adi.novriansyah`.

### Keterbatasan yang mau dibenerin
1. **Zero progress visibility** — Adi liat "thinking…" sampai 10 menit, lalu
   sekonyong-konyong wall of text. Langsung tabrakan sama prinsip
   `execution-visibility` (status harus selalu kebaca).
2. **Gak keliatan NafuTech lagi ngapain** — gak ada jejak tool-use (lagi baca
   file? query DB? post Slack?).
3. Cold start full CLI tiap pesan (~1-2s) — minor, bukan masalah utama.

## 2. Implementasi referensi (yang kita bandingin)

Pola: `claude` **interaktif** ditahan hidup di dalam **tmux**, lalu **tail file
transcript JSONL** (`~/.claude/projects/<hash>/<session>.jsonl`) buat nangkep
output multi-turn yang streaming.

**Insight penting (anti-cargo-cult):** baca-transcript-JSONL di referensi itu
sebenarnya *workaround* — karena mode interaktif/tmux gak ngasih event-stream
yang rapi, mereka kepaksa intip file JSONL yang ditulis Claude ke disk. tmux =
mekanisme buat nahan REPL hidup + nyuntik input.

Jadi yang berharga itu **outcome-nya (streaming multi-turn)**, BUKAN cara baca
JSONL-nya. CLI versi kita (2.1.162) udah expose event-stream itu langsung di
stdout lewat `--output-format stream-json`. Artinya kita bisa dapet "flow yang
mirip" **tanpa tmux DAN tanpa ngintip file JSONL** — dua-duanya dependency/hack
yang gak perlu kita warisi.

## 3. Tiga opsi tujuan

| | A. `-p` + stream-json (spawn/turn) | B. Persistent session (stdin/stdout pipe) | C. Claude Agent SDK (Python) |
|---|---|---|---|
| Drop one-shot blocking? | ✅ (streaming) | ✅ | ✅ |
| Drop tmux? | ✅ | ✅ | ✅ |
| Masih pakai flag `-p`? | ⚠️ ya | ⚠️ ya (`--print` + stream-json I/O) | ❌ tidak |
| Multi-turn | `--resume <sid>` (udah proven) | 1 proses hidup/thread, feed turn via stdin | native session |
| Blast radius | **kecil** (cuma `claude_runner` + callback di `app`) | sedang (lifecycle proses/thread, reaper idle) | besar (async rewrite, dep baru) |
| Risiko | rendah | bocor proses kalau reaper salah | SDK tetap manggil CLI; perlu verif `disallowed_tools` + config dir |

### Catatan soal "gak pake claude -p lagi"
Literalnya, A & B masih pakai flag `-p`/`--print` (itu cara CLI jalan headless
non-interaktif — alternatifnya cuma mode REPL interaktif yang justru butuh
tmux/pty). Yang benar-benar kita buang di A & B adalah **pola one-shot blocking
"tunggu sampai kelar baru balas"**, diganti streaming. Kalau yang Adi maksud
"hilangin `-p`" itu = "stop nungguin hasil final doang", A/B udah memenuhi.
Kalau hard-requirement-nya **gak boleh ada flag `-p` sama sekali**, berarti
wajib C (SDK) — tapi SDK pun di balik layar tetap nge-spawn CLI.

## 4. Rekomendasi: **Opsi A**

Alasan: tujuan sebenarnya = *visibility streaming*, dan A ngasih itu dengan
perubahan paling kecil + jalur multi-turn (`--resume`) yang udah terbukti di
bridge sekarang. B/C nambah kompleksitas lifecycle/async demi gain marginal
(hemat ~1-2s cold start). Mulai dari A; kalau nanti kerasa perlu sesi
benar-benar persistent, upgrade ke B gampang karena parser stream-json-nya sama.

## 5. Rencana perubahan (Opsi A)

### 5.1 `claude_runner.py` — ganti core
- Command: `claude -p <prompt> --output-format stream-json --include-partial-messages
  --verbose --resume <sid> --disallowed-tools ...`.
- Pakai `subprocess.Popen` (bukan `run`), baca **stdout line-by-line** (tiap baris
  = 1 JSON event). Signature baru:
  `run(prompt, session_id, on_event: Callable[[dict], None]) -> tuple[str, str]`.
- Event yang dipeduliin:
  - `{"type":"system","subtype":"init","session_id":...}` → tangkap session_id awal.
  - `{"type":"stream_event", ...content_block_delta...}` (dari `--include-partial-messages`)
    → potongan teks asisten yang lagi diketik → akumulasi buat live-update.
  - `{"type":"assistant","message":{content:[...tool_use...]}}` → ekstrak nama
    tool buat breadcrumb progress ("🔧 Bash…", "📖 Read…").
  - `{"type":"result","subtype":"success","result":...,"session_id":...,"is_error":...}`
    → teks final + session_id final.
- Tetap pertahankan: penanganan error (`is_error`/exit non-zero → `RuntimeError`
  pesan human-readable), `DISALLOWED_TOOLS`, `cwd`, env/config dir.
- Timeout: ganti dari `subprocess.run(timeout=)` ke watchdog manual (kill kalau
  gak ada event > N detik), karena Popen gak punya timeout bawaan.

### 5.2 `app.py` — sambungin streaming ke Slack
- `_dispatch` lewatin callback `on_event` ke runner.
- **Throttle update** (Slack rate limit ±1 msg/detik/channel): jangan
  `chat_update` tiap delta. Pakai timer — flush teks terakumulasi ke placeholder
  tiap ~1.5–2s **atau** pas ada milestone (tool baru dipanggil). Util kecil
  `ThrottledUpdater(client, channel, ts)`.
- Tampilan: placeholder "thinking…" jadi live transcript (teks tumbuh +
  baris breadcrumb tool opsional). Pas event `result`: render final, lalu chunk
  kalau > 3800 char (logika `_chunk_text` dipakai ulang apa adanya).
- `_pending` + flush-on-shutdown tetep jalan; tambahan: simpan teks parsial
  terakhir biar pas restart placeholder gak ke-reset kosong.

### 5.3 Yang TIDAK berubah
- `thread_store.py` (tetap simpan `session_id`; resume jalan sama persis).
- Gate keamanan, `DISALLOWED_TOOLS`, systemd unit, identitas bot.
- `_chunk_text`, `SLACK_CHUNK_SIZE`.

### 5.4 Risiko & mitigasi
- **Rate limit Slack** → throttle wajib (5.2), jangan per-delta.
- **Format event berubah antar versi CLI** → parser defensif: kalau `type` gak
  dikenal, skip; selalu andalkan event `result` sebagai source-of-truth final.
- **Telegram bridge (OpenClaw)**: pola streaming/breadcrumb ini buat **Slack**.
  Aturan Telegram (max 3-4 pesan, ringkas) tetap beda kanal — gak kena.
- **Aturan ≤3-4 pesan**: itu aturan Telegram NafuTech, bukan Slack bridge ini.
  Tapi tetap jaga: breadcrumb cukup di-update ke 1 placeholder (chat_update),
  bukan spam pesan baru per tool.

## 6. Test plan
1. DM bot pesan pendek → placeholder live-update teks → final benar, 1 pesan.
2. Pesan yang mancing tool-use (mis. "cek status VPN") → breadcrumb tool muncul.
3. Output panjang (>3800) → chunking tetap jalan.
4. Restart bridge pas mid-stream → flush placeholder "ke-restart" muncul.
5. Multi-turn di thread sama → konteks nyambung (verif `--resume`).
6. Error API → pesan human-readable, bukan raw JSON.
