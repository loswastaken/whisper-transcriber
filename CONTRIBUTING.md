# Contributing: conventions

These are the design rules for changes to this project (for people and coding assistants alike).

- **No auth by design.** Meant for a trusted LAN or localhost, on port 8090. Do not add logins,
  accounts, cookies, CORS, analytics or telemetry in the core app; put access control in front of it (reverse proxy) instead.
- **Never hardcode IPs or hostnames** (no LAN ranges, no host addresses). Use
  placeholders like `example.invalid` or `192.0.2.x` in docs and tests. Link
  validation uses the `ipaddress` module properties, not hardcoded ranges.
  `tests/` builds private addresses from integers so the repo-wide private-IP grep stays clean.
- **Frontend swap point:** `app/static/` (index.html, app.css, app.js). These are static
  files with no build step, framework, CDN or external fonts. Render job data with
  `textContent` only. The API contract is in README.md; keep it in sync with `app/main.py`.
- **Subprocesses** (yt-dlp, ffmpeg, ffprobe) always use list args, never `shell=True`.
  yt-dlp gets `--` before the URL. Kill only the child you started.
- **Errors to clients** are short `UserError` messages. Never send stack traces or server paths.
- **Filesystem:** validate job ids (32 hex chars) before touching disk. Never follow
  symlinks when deleting. Only delete inside `DATA_DIR/jobs`.
- **One uvicorn worker process.** The job queue is in-process, and one job runs at a time.
- **Cancel** (`POST /api/jobs/{id}/cancel`, `JobStore.cancel`): a queued job becomes
  `cancelled` at once; the running job becomes `cancelling` and its per-job
  `threading.Event` is set. Every child process goes through `procs.Child`
  (`start_new_session=True`); its watcher thread stops only that process group
  (`os.killpg`: SIGTERM, SIGKILL after 3 s) on cancel or hard timeout, and the
  leader is reaped only after the last group signal. Its exit is seen without reaping
  via `os.waitid(WNOWAIT)` on Linux and a kqueue `NOTE_EXIT` watch on macOS (no
  `waitid` there); `Popen.poll()`/`wait()` is only the last-resort fallback. Keep `app/`
  free of Linux-only calls (`/proc`, `waitid` without a fallback, `sched_*`, `prctl`).
  Transcription checks the flag
  before starting and between segments. A cancel raises `procs.JobCancelled`, never
  a `UserError`, and ends as `cancelled` with `error: null`, keeping only `job.json`.
  `cancelled` is a finished state everywhere (`FINISHED` in `app/jobs.py`); a
  `cancelling` job found at startup becomes `cancelled`.
- **Transcription backends:** `WHISPER_BACKEND` = `faster-whisper` (default; Docker,
  CPU) or `mlx` (native Apple Silicon via `scripts/run-mac.sh`). `make_transcriber()` in
  `app/transcribe.py` picks one; both keep the same interface (`loaded`,
  `transcribe(wav_path, on_progress, cancel) -> (segments, language, duration)`).
  `mlx_whisper` is imported only inside `app/transcribe_mlx.py` on the first job (it
  does not install on Linux; the Docker image must not need it). Its progress and
  cancel come from a tqdm shim swapped into `mlx_whisper.transcribe` per call, checked
  against the version pinned in `requirements-mac.txt`; re-check before bumping it. The
  MLX backend reads the WAV itself so mlx_whisper never starts its own ffmpeg.
- **Mac native run:** `requirements-mac.txt` (no faster-whisper/av) and
  `scripts/run-mac.sh` / `scripts/stop-mac.sh`. Default bind is `127.0.0.1`; `HOST`
  overrides it. Keep its yt-dlp pin in step with the Dockerfile's `YTDLP_VERSION`.
- Pure logic stays stdlib-only (`textfmt`, `urlcheck`, `cleanup`, `filenames`, `procs`) with
  `unittest` tests in `tests/`. There is no package.json; do not add one.
- yt-dlp and deno are pinned via Dockerfile build args (`YTDLP_VERSION`, `DENO_VERSION`).
  See README "Bumping yt-dlp".
- Update README.md (API, settings, limits) and this file when behaviour changes.
