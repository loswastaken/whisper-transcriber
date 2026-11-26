#!/usr/bin/env bash
# Run Transcriber natively on an Apple Silicon Mac with the MLX backend (no Docker).
#
# Creates a venv under APP_HOME, installs requirements-mac.txt when it changes,
# then runs uvicorn in the foreground. Overridable: APP_HOME, DATA_DIR, HF_HOME,
# HOST (default 127.0.0.1; HOST=0.0.0.0 for the LAN), PORT (default 8090),
# MLX_MODEL and the other settings in README.md. Stop it with scripts/stop-mac.sh.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "run-mac.sh needs macOS on Apple Silicon (arm64). Elsewhere use Docker." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_HOME="${APP_HOME:-$HOME/Apps/transcriber}"
VENV="$APP_HOME/venv"
REQS="$REPO_DIR/requirements-mac.txt"
STAMP="$VENV/.requirements-mac.sha256"
PIDFILE="$APP_HOME/transcriber.pid"

# Homebrew and user-installed ffmpeg, ffprobe, deno and uv.
# $VENV/bin is prepended below, after the venv exists, so yt-dlp resolves under
# a LaunchAgent PATH of /opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin.
# app/fetch.py resolves that same script from sys.prefix as well.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

if [[ -f "$PIDFILE" ]]; then
  old="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ "$old" =~ ^[0-9]+$ ]] &&
      [[ "$(ps -o command= -p "$old" 2>/dev/null || true)" == *uvicorn*app.main:app* ]]; then
    echo "Transcriber is already running (pid $old). Stop it with scripts/stop-mac.sh." >&2
    exit 1
  fi
fi

mkdir -p "$APP_HOME"

find_python() {
  local c
  for c in /opt/homebrew/bin/python3.12 "$(command -v python3.12 || true)"; do
    if [[ -n "$c" && -x "$c" ]] &&
        "$c" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

# A Homebrew Python upgrade can leave an old venv pointing at a removed interpreter.
if [[ -e "$VENV" ]] && ! "$VENV/bin/python" -c '' 2>/dev/null; then
  echo "The venv at $VENV no longer runs; recreating it."
  rm -rf "$VENV"
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  if py="$(find_python)"; then
    echo "Creating venv with $py"
    "$py" -m venv "$VENV"
  elif command -v uv >/dev/null 2>&1; then
    echo "Creating venv with uv (Python 3.12)"
    uv venv --seed --python 3.12 "$VENV"
  else
    echo "Python 3.12 not found. Install it with: brew install python@3.12" >&2
    exit 1
  fi
fi

if ! "$VENV/bin/python" -c 'import platform, sys; sys.exit(platform.machine() != "arm64")'; then
  echo "The Python in $VENV is not an arm64 build (Rosetta?); MLX needs arm64." >&2
  echo "Remove $VENV and install Python with Homebrew in /opt/homebrew." >&2
  exit 1
fi

want="$(shasum -a 256 "$REQS" | awk '{print $1}')"
if [[ ! -f "$STAMP" || "$(cat "$STAMP")" != "$want" ]]; then
  echo "Installing requirements-mac.txt into $VENV"
  "$VENV/bin/python" -m pip install --disable-pip-version-check -r "$REQS"
  echo "$want" > "$STAMP"
fi

# Prepend after the venv is in place so console scripts (yt-dlp) win over any
# older copy elsewhere on PATH. The directory is created above.
export PATH="$VENV/bin:$PATH"

if [[ ! -x "$VENV/bin/yt-dlp" ]]; then
  echo "yt-dlp is not installed in $VENV. Remove $VENV and run this script again." >&2
  exit 1
fi

for tool in ffmpeg ffprobe; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool not found. Install it with: brew install ffmpeg" >&2
    exit 1
  fi
done
if ! command -v deno >/dev/null 2>&1; then
  echo "Warning: deno not found. YouTube links may fail without it: brew install deno" >&2
fi

export WHISPER_BACKEND=mlx
export DATA_DIR="${DATA_DIR:-$APP_HOME/data}"
export HF_HOME="${HF_HOME:-$APP_HOME/models}"
export HF_HUB_DISABLE_TELEMETRY="${HF_HUB_DISABLE_TELEMETRY:-1}"
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8090}"
export PYTHONUNBUFFERED=1
mkdir -p "$DATA_DIR" "$HF_HOME"

echo "Transcriber (MLX) on http://$HOST:$PORT/  data: $DATA_DIR  models: $HF_HOME"
cd "$REPO_DIR"
# exec keeps this pid, so stop-mac.sh can signal uvicorn directly.
echo "$$" > "$PIDFILE"
exec "$VENV/bin/python" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --workers 1
