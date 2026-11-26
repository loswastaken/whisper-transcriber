"""Media download (yt-dlp) and conversion (ffmpeg/ffprobe).

Every external tool runs with list arguments, never through a shell.
"""

import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
from typing import Callable, List, Optional, Tuple

from .errors import UserError
from .procs import Child, JobCancelled

log = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_SECONDS = 2 * 3600
FFMPEG_TIMEOUT_SECONDS = 2 * 3600
FFPROBE_TIMEOUT_SECONDS = 120
TITLE_PREFIX = "TRANSCRIBER-TITLE:"
# Executables; the tests point these at small Python stand-ins.
# YTDLP_CMD stays a bare name so tests can replace the whole argv. download()
# rewrites a leading "yt-dlp" to the copy installed beside this interpreter.
YTDLP_CMD: List[str] = ["yt-dlp"]
FFMPEG_CMD: List[str] = ["ffmpeg"]
FFPROBE_CMD: List[str] = ["ffprobe"]

_PROGRESS = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SKIP_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")


def ytdlp_executable() -> str:
    """yt-dlp installed beside this interpreter, or the bare name for PATH.

    pip puts the console script in ``sys.prefix/bin``. That is the venv ``bin``
    when the Mac app is started with the venv's Python, including under a
    LaunchAgent whose PATH is ``/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin``.
    The absolute path is what ``Popen`` runs. With no script there (a test
    interpreter, or an install that is only on PATH), the bare name is returned
    and the lookup uses PATH.
    """
    bundled = os.path.join(sys.prefix, "bin", "yt-dlp")
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        return bundled
    return "yt-dlp"


def ytdlp_argv() -> List[str]:
    """``YTDLP_CMD``, with a leading bare ``yt-dlp`` passed through ``ytdlp_executable``."""
    argv = list(YTDLP_CMD)
    if argv and argv[0] == "yt-dlp":
        argv[0] = ytdlp_executable()
    return argv


def _ytdlp_error(output_tail: str) -> str:
    text = output_tail.lower()
    if "does not pass filter" in text or "is_live" in text or "live event" in text:
        return "live streams and media over the length limit are not supported"
    if "max-filesize" in text or "larger than max" in text or "file is larger" in text:
        return "media file is too large"
    if "unsupported url" in text:
        return "this link is not supported"
    if "private video" in text or "sign in" in text or "login" in text or "members-only" in text:
        return "this media is private or needs a login"
    if "unavailable" in text or "http error 404" in text or "not found" in text:
        return "this media is unavailable"
    if "geo" in text and "restrict" in text:
        return "this media is not available in this region"
    return "download failed"


def download(url: str, job_dir: str, max_media_seconds: int,
             on_progress: Callable[[float], None],
             on_title: Callable[[str], None],
             timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
             cancel: Optional[threading.Event] = None) -> str:
    """Download best audio for url into job_dir; return the media path.

    Raises JobCancelled if cancel is set while yt-dlp runs.
    """
    cmd = ytdlp_argv() + [
        "--ignore-config",
        "--no-playlist",
        "--progress",
        "--newline",
        "--no-simulate",
        "--print", f"before_dl:{TITLE_PREFIX}%(title)s",
        "-f", "bestaudio/best",
        "-x", "--audio-format", "best",
        "--max-filesize", "2G",
        "--match-filter", f"!is_live & duration <=? {int(max_media_seconds)}",
        "--no-exec",
        "--no-cache-dir",
        "-o", os.path.join(job_dir, "source.%(ext)s"),
        "--",
        url,
    ]
    tail = []
    with Child(cmd, cancel, timeout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
               cwd=job_dir, text=True, encoding="utf-8", errors="replace",
               bufsize=1) as child:
        assert child.proc.stdout is not None
        for raw in child.proc.stdout:
            line = _ANSI.sub("", raw).rstrip("\r\n")
            if line.startswith(TITLE_PREFIX):
                title = line[len(TITLE_PREFIX):].strip()
                if title and title != "NA":
                    on_title(title)
                continue
            m = _PROGRESS.search(line)
            if m:
                on_progress(min(100.0, float(m.group(1))))
                continue
            tail.append(line)
            del tail[:-40]
        rc = child.wait()
    if child.reason == "cancelled":
        raise JobCancelled()
    if child.reason == "timeout":
        raise UserError("download took too long and was stopped")
    output = "\n".join(tail)
    if rc != 0:
        log.warning("yt-dlp exited %s: %s", rc, output[-2000:])
        raise UserError(_ytdlp_error(output))
    path = _find_source(job_dir)
    if path is None:
        log.warning("yt-dlp produced no file: %s", output[-2000:])
        raise UserError(_ytdlp_error(output) if "filter" in output.lower()
                        else "download produced no media file")
    return path


def _find_source(job_dir: str) -> Optional[str]:
    best: Tuple[int, Optional[str]] = (-1, None)
    for entry in os.scandir(job_dir):
        if not entry.name.startswith("source.") or entry.name.endswith(_SKIP_SUFFIXES):
            continue
        if not entry.is_file(follow_symlinks=False):
            continue
        size = entry.stat(follow_symlinks=False).st_size
        if size > best[0]:
            best = (size, entry.path)
    return best[1]


def _run_tool(cmd: List[str], timeout: float, cancel: Optional[threading.Event]
              ) -> Tuple[Optional[int], str, str]:
    """Run ffmpeg/ffprobe; return (exit code or None on timeout, stdout, stderr tail)."""
    with tempfile.TemporaryFile() as err:
        with Child(cmd, cancel, timeout, stdout=subprocess.PIPE, stderr=err) as child:
            assert child.proc.stdout is not None
            out = child.proc.stdout.read()
            rc = child.wait()
        if child.reason == "cancelled":
            raise JobCancelled()
        err.seek(0, os.SEEK_END)
        err.seek(max(0, err.tell() - 4000))
        err_tail = err.read().decode("utf-8", "replace")
    if child.reason == "timeout":
        return None, "", err_tail
    return rc, out.decode("utf-8", "replace"), err_tail


def probe_duration(path: str, cancel: Optional[threading.Event] = None) -> Optional[float]:
    """Container duration in seconds via ffprobe, or None if unknown."""
    cmd = FFPROBE_CMD + [
        "-v", "error", "-protocol_whitelist", "file",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    rc, out, err = _run_tool(cmd, FFPROBE_TIMEOUT_SECONDS, cancel)
    if rc is None:
        raise UserError("could not read the media file")
    if rc != 0:
        log.warning("ffprobe failed: %s", err[-1000:])
        raise UserError("could not read the media file")
    try:
        value = float(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return value if value > 0 else None


def to_wav(src: str, dst: str, timeout: float = FFMPEG_TIMEOUT_SECONDS,
           cancel: Optional[threading.Event] = None) -> None:
    """Convert any input to 16 kHz mono 16-bit WAV."""
    cmd = FFMPEG_CMD + [
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-protocol_whitelist", "file",
        "-i", src,
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        dst,
    ]
    rc, _, err = _run_tool(cmd, timeout, cancel)
    if rc is None:
        raise UserError("audio conversion took too long and was stopped")
    if rc != 0:
        log.warning("ffmpeg failed: %s", err[-1000:])
        raise UserError("could not decode audio from this file")


def check_duration(seconds: Optional[float], max_media_seconds: int) -> None:
    if seconds is not None and seconds > max_media_seconds:
        hours = max_media_seconds / 3600
        raise UserError(f"media is longer than the {hours:g} hour limit")
