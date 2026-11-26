# whisper-transcriber

A small self-hosted web app for transcribing audio and video with
[Whisper](https://github.com/openai/whisper). Paste a media link (YouTube or anything
else [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports) or upload a file, and the
page shows the transcript with a one-click **Copy** button plus **TXT** and **SRT**
downloads. Jobs queue up, show progress, and can be stopped with a **Stop** button.

Two transcription backends, same web UI:

- **Docker (Linux, x86_64, CPU):** [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  with `large-v3-turbo`, int8. This is the default.
- **Native on Apple Silicon (GPU):** [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper)
  with the same model converted for MLX, started with `scripts/run-mac.sh`. On an
  M-series Mac this is several times faster than CPU.

## Security: read this first

- **There is no login, by design.** Anyone who can reach the port can see, download
  and delete every job. Run it on `localhost` or a trusted LAN only. **Never expose it
  to the internet** or forward the port unless you put real authentication (for
  example a reverse proxy with auth) in front of it.
- **Link fetching is not SSRF-proof.** Before a job starts, the link's host is checked
  and refused if it resolves to a loopback, private, link-local or other non-public
  address. But yt-dlp then does its own requests: it **follows HTTP redirects** (and
  re-resolves DNS), so a public URL that redirects to an internal address can still
  make the server fetch from your internal network. That is one more reason to keep
  it off the internet and away from untrusted users.
- The Docker image runs as an unprivileged user, and subprocesses (yt-dlp, ffmpeg,
  ffprobe) are always started with argument lists, never a shell.

## Quick start

### Docker (Linux, x86_64)

```sh
git clone https://github.com/loswastaken/whisper-transcriber.git
cd whisper-transcriber
docker compose up -d --build
```

Then open `http://127.0.0.1:8090/`. `compose.yaml` publishes the port on localhost
only; change it to `"8090:8090"` to reach it from other devices on your LAN.

- The first job downloads the model (about 1.6 GB) into the `transcriber-models`
  volume, so it takes noticeably longer. Later jobs, and rebuilds, reuse the cache.
  The model loads lazily on the first job, not at startup.
- Speed depends on your CPU. As a rough guide, 4 cores with no GPU process about
  **10 minutes per hour of audio**. Raise `CPU_THREADS` in `compose.yaml` if you have
  more cores.
- One job runs at a time and any number can wait in the queue.
- The image is built for x86_64 (the deno download is the x86_64 Linux build).
  On an Apple Silicon Mac, Docker cannot use the GPU anyway; use the native Mac
  setup below instead.

### Native on an Apple Silicon Mac

```sh
brew install python@3.12 ffmpeg deno
git clone https://github.com/loswastaken/whisper-transcriber.git
cd whisper-transcriber
scripts/run-mac.sh
```

Then open `http://127.0.0.1:8090/`. Details are in
[Run natively on a Mac](#run-natively-on-a-mac-apple-silicon-mlx).

### yt-dlp and deno

Links are downloaded with yt-dlp, which needs the [deno](https://deno.com/) JavaScript
runtime for YouTube. The Docker image bundles both (pinned, see
[Bumping yt-dlp](#bumping-yt-dlp)). On a Mac, yt-dlp is installed into the venv and
deno comes from Homebrew; without deno, YouTube links will likely fail. Sites change
often, so if links stop working, bump yt-dlp first.

## Health check

`GET /healthz` returns
`{"status":"ok","queue_length":0,"worker_alive":true,"model_loaded":false,"backend":"faster-whisper"}`.
It never triggers a model load. `backend` is the `WHISPER_BACKEND` in use.

## Settings

Set these as environment variables (see `compose.yaml`, or `scripts/run-mac.sh`
on a Mac):

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATA_DIR` | `/data` | Job files live in `DATA_DIR/jobs/<id>/` (Mac script: `~/Apps/transcriber/data`) |
| `HF_HOME` | `/models` | Model cache (Mac script: `~/Apps/transcriber/models`) |
| `WHISPER_BACKEND` | `faster-whisper` | `faster-whisper` (CPU, Docker) or `mlx` (Apple Silicon GPU; the Mac script sets it) |
| `MLX_MODEL` | `mlx-community/whisper-large-v3-turbo` | Hugging Face repo of the MLX model (`mlx` backend only) |
| `MODEL_NAME` | `large-v3-turbo` | faster-whisper model (fixed; there is no picker in the UI) |
| `COMPUTE_TYPE` | `int8` | CTranslate2 compute type |
| `DEVICE` | `cpu` | |
| `CPU_THREADS` | `4` | Threads used by the model (`MODEL_NAME`, `COMPUTE_TYPE`, `DEVICE` and this are faster-whisper only) |
| `RETENTION_HOURS` | `24` | Jobs are deleted this long after they were created |
| `MAX_UPLOAD_BYTES` | `2147483648` | Upload size limit (2 GiB) |
| `MAX_MEDIA_SECONDS` | `21600` | Media length limit (6 h) |
| `PORT` | `8090` | Docker: informational only, the container CMD always listens on 8090; change the host side of `ports:` instead. Mac script: the port it listens on |
| `HOST` | `127.0.0.1` | Mac script only: address uvicorn binds to. `0.0.0.0` serves your LAN |

## Limits and retention

- Uploads: `.mp3 .mp4 .wav .m4a .mkv .webm .ogg .flac .mov .aac .opus` (any case),
  up to 2 GiB. Anything else gets 415; larger files get 413. Uploads are streamed
  to disk and never held in memory.
- Links: only `http`/`https` links with a host name. IP addresses, `localhost`,
  user:password links and hosts that resolve to loopback, private, link-local,
  multicast, reserved or other non-public addresses are refused. Playlists and
  live streams are not supported. yt-dlp's download is capped at 2 GB and 2 hours.
- Media longer than 6 hours is refused.
- Downloaded and uploaded media, and the intermediate 16 kHz WAV, are deleted as
  soon as a job finishes, whether it succeeded or failed. Only `job.json`,
  `transcript.txt` and `transcript.srt` are kept. A cancelled job keeps only
  `job.json` (no partial transcript), so it stays in the list until it expires.
- Every 10 minutes (and at startup) jobs older than `RETENTION_HOURS` are deleted.
  The running job and queued jobs are never swept.
- After a container restart, finished transcripts are still there until they expire.
  Jobs that were queued or running are marked `error` ("interrupted by restart");
  a job that was being cancelled (`cancelling`) is marked `cancelled`.

## Stopping a job

The **Stop** button in the transcript pane (shown for waiting and running jobs)
calls `POST /api/jobs/{id}/cancel`. A waiting job is cancelled at once. A running
job shows "Stopping…" and normally reaches `cancelled` within a few seconds:
yt-dlp, ffmpeg and ffprobe are stopped (SIGTERM to the tool's own process group,
SIGKILL after 3 s), and transcription stops at the next segment the model
produces (with the `mlx` backend: at the end of the current 30-second window). A job cannot be stopped while the model is still loading for the first
time; the stop takes effect right after. The next waiting job then starts.

## Run natively on a Mac (Apple Silicon, MLX)

The app can run directly on an Apple Silicon Mac (M1 or later, macOS 14 or later)
without Docker, using the Apple GPU through mlx-whisper and the same
`large-v3-turbo` model converted for MLX (`mlx-community/whisper-large-v3-turbo`).
The Docker setup above is unchanged and stays the default.

Prerequisites (Homebrew):

```sh
brew install python@3.12 ffmpeg
brew install deno    # optional; YouTube links may need it
```

Start it:

```sh
git clone https://github.com/loswastaken/whisper-transcriber.git
cd whisper-transcriber
scripts/run-mac.sh
```

Then open `http://127.0.0.1:8090/`.

To run it in the background and stop it later:

```sh
mkdir -p ~/Apps/transcriber
nohup scripts/run-mac.sh > ~/Apps/transcriber/run.log 2>&1 &
scripts/stop-mac.sh
```

- The script refuses to run anywhere but macOS on arm64. It uses Homebrew's
  `python3.12` (or `python3.12` on `PATH`, or `uv` if neither exists), creates a
  venv in `~/Apps/transcriber/venv` and installs `requirements-mac.txt` into it.
  It installs again only when that file changes, so after `git pull` just run the
  script again. Before the server starts, the script puts that venv's `bin` on
  `PATH`. A LaunchAgent otherwise leaves `PATH` as
  `/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`, so downloads invoke the
  `yt-dlp` installed next to the app's own Python. The Python packages (mlx,
  torch and others) are a large download.
- Everything lives under `APP_HOME` (default `~/Apps/transcriber`): `data/` (jobs),
  `models/` (Hugging Face cache, `HF_HOME`), `venv/` and `transcriber.pid`.
  `DATA_DIR`, `HF_HOME`, `PORT`, `MLX_MODEL` and the other settings can be
  overridden in the environment.
- By default it listens on `127.0.0.1`, so only this Mac can open it. Start it
  with `HOST=0.0.0.0 scripts/run-mac.sh` to serve your LAN. The same rule
  applies as for Docker: there is no login, so never expose it to the internet.
- The first job downloads the model (about 1.6 GB) into `HF_HOME`, and the model
  loads on the first job, not at startup. To warm it up, transcribe a few
  seconds of audio once after starting; `/healthz` then shows `"model_loaded":true`.
- Differences from the faster-whisper backend: there is no voice activity
  detection (VAD), so long silence or music goes through the model and can now
  and then produce a repeated or invented line; decoding is greedy (mlx-whisper
  has no beam search), and each window is decoded without the previous
  window's text as a prompt to stop such errors from spreading. Progress moves in
  30-second steps and Stop takes effect at the end of the current window.
  `MODEL_NAME`, `COMPUTE_TYPE`, `DEVICE` and `CPU_THREADS` are ignored.
- `scripts/stop-mac.sh` sends SIGTERM to the server only. A yt-dlp download or
  ffmpeg conversion that is running at that moment is not stopped with it; press
  Stop on the job first if you want it gone at once.
- To bump yt-dlp on the Mac, change its pin in `requirements-mac.txt`; the next
  start reinstalls.

## API

All responses are JSON unless noted. No auth, no cookies, no CORS. Write requests
that the browser marks as cross-site (`Sec-Fetch-Site: cross-site`) are refused
with 403. Errors look like `{"detail": "message"}`.

Job object:

```json
{
  "id": "32 lowercase hex chars",
  "kind": "url | upload",
  "name": "display name (video title or original file name)",
  "status": "queued | downloading | extracting | transcribing | cancelling | done | error | cancelled",
  "progress": 0.0,
  "queue_position": 0,
  "created": 1794830000.0,
  "finished": null,
  "language": "en",
  "duration": 3600.0,
  "error": null,
  "url": "https://... (url jobs only)"
}
```

- `progress` is 0-100 for the current stage: download percentage while
  `downloading`, audio position while `transcribing`, 100 when `done`.
- `queue_position` is 1 for the next job to run, 0 if the job is not waiting.
- `done`, `error` and `cancelled` are finished states. `cancelling` is transient:
  the running job has been asked to stop. A cancelled job has `error: null`, keeps
  the `progress` it reached and has a `finished` time; it has no transcript.
- `created` and `finished` are Unix timestamps in seconds. `duration` is in seconds.

| Method and path | Result |
| --- | --- |
| `POST /api/jobs/url` with body `{"url": "..."}` | `202 {"id": "..."}`; 400 for a refused link |
| `POST /api/jobs/upload` (multipart, field `file`) | `202 {"id": "..."}`; 413 too large, 415 wrong type, 400 malformed |
| `GET /api/jobs` | List of job objects, newest first, without transcript text |
| `GET /api/jobs/{id}` | Job object plus `"transcript"` (text, only when `done`, else `null`) |
| `GET /api/jobs/{id}/transcript.txt` | Plain-text download (paragraphs, no timestamps) |
| `GET /api/jobs/{id}/transcript.srt` | SRT download |
| `POST /api/jobs/{id}/cancel` | Queued job: `200 {"status": "cancelled"}`. Running job: `202 {"status": "cancelling"}` (again 202 if repeated while pending). 409 `{"detail": "job already finished"}` for `done`, `error` or `cancelled`; 404 if unknown |
| `DELETE /api/jobs/{id}` | 204. Removes a finished, failed, cancelled or queued job; 409 for the running (or cancelling) job; 404 if unknown |
| `GET /healthz` | Health (see [Health check](#health-check)) |

## Swapping the frontend

The frontend is three static files in `app/static/` (`index.html`, `app.css`,
`app.js`). There is no build step and no framework. To use a new design, replace
those files (keeping `index.html` as the entry point) and talk to the API above.
Everything under `app/static/` is served at `/`. The Content-Security-Policy only
allows same-origin scripts, styles and requests, so do not use CDNs, external fonts
or inline scripts. Render job data with `textContent`, never `innerHTML`.

Note: on plain-HTTP LAN addresses browsers do not expose `navigator.clipboard`,
so the Copy button falls back to selecting the textarea and calling
`document.execCommand("copy")`. Keep a fallback like that in any redesign.

## Bumping yt-dlp

Sites change often and yt-dlp releases often. yt-dlp is installed in its own late
image layer, so a bump rebuilds only that layer:

```sh
docker compose build --build-arg YTDLP_VERSION=<new> && docker compose up -d
```

(or set `YTDLP_VERSION=<new>` in the environment or an `.env` file next to
`compose.yaml`, which the compose build args read.) To see the current version:

```sh
docker compose exec transcriber yt-dlp --version
```

The deno JavaScript runtime that yt-dlp needs for YouTube is pinned the same way
with `DENO_VERSION`. On a Mac, yt-dlp is pinned in `requirements-mac.txt` and deno
comes from Homebrew.

## Development

The pure logic (`app/textfmt.py`, `app/urlcheck.py`, `app/cleanup.py`,
`app/filenames.py`, `app/procs.py`) is stdlib-only and unit-tested without any
third-party packages. `tests/test_cancel.py` drives the job store and worker with
small Python stand-ins for yt-dlp/ffmpeg and a fake model (Linux only, uses `/proc`).
`tests/test_procs_portable.py` runs the child-process and cancel tests again
without `os.waitid`, as on macOS: once with the polling fallback and once with a
stand-in kqueue (Linux has none). `tests/test_backends.py` covers backend selection and the MLX progress/cancel hook
with a fake `mlx_whisper` module, so it runs without MLX:

```sh
python3 -m py_compile $(git ls-files '*.py')
python3 -m unittest discover -s tests -v
node --check app/static/app.js
```

Layout:

- `app/main.py`: routes and the streaming upload parser
- `app/jobs.py`: job store, `job.json` persistence, the single worker thread, retention loop
- `app/fetch.py`: yt-dlp download, ffprobe duration, ffmpeg conversion to 16 kHz mono WAV
- `app/procs.py`: child processes in their own process group, stopped on cancel or timeout
- `app/transcribe.py`: backend factory (`WHISPER_BACKEND`) and the faster-whisper backend
- `app/transcribe_mlx.py`: the mlx-whisper backend (Apple Silicon)
- `app/textfmt.py`: TXT paragraphs and SRT builders
- `app/urlcheck.py`: link validation
- `app/cleanup.py`: retention sweep and media cleanup
- `app/filenames.py`: upload extension allow-list, display names, Content-Disposition
- `app/config.py`: environment settings
- `scripts/run-mac.sh`, `scripts/stop-mac.sh`, `requirements-mac.txt`: native Mac run

## License

MIT, see [LICENSE](LICENSE).

Third-party components are installed at build or run time, not vendored in this
repository, and keep their own licenses:

| Component | License |
| --- | --- |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT |
| [mlx-whisper](https://github.com/ml-explore/mlx-examples) | MIT |
| [Whisper model weights](https://huggingface.co/openai/whisper-large-v3-turbo) (`large-v3-turbo` and its CTranslate2/MLX conversions) | MIT |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause |
| [python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 |
| [PyAV](https://github.com/PyAV-Org/PyAV) | BSD-3-Clause |
| [deno](https://github.com/denoland/deno) | MIT |
| [FFmpeg](https://ffmpeg.org/legal.html) | LGPL/GPL; used as an external program (Debian package in Docker, Homebrew on a Mac), not linked or redistributed by this repo |

Note that the Docker image you build contains Debian's FFmpeg and PyAV's bundled
FFmpeg libraries; if you redistribute a built image, follow their license terms.

Please respect the terms of service of the sites you download from, and copyright,
when transcribing media you do not own.
