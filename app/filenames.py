"""Upload filename helpers (stdlib only)."""

import os
import re
import unicodedata
from typing import Optional
from urllib.parse import quote

ALLOWED_EXTENSIONS = frozenset({
    ".mp3", ".mp4", ".wav", ".m4a", ".mkv", ".webm", ".ogg", ".flac",
    ".mov", ".aac", ".opus",
})
MAX_DISPLAY_NAME = 200
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_WS = re.compile(r"\s+")


def is_valid_job_id(job_id: str) -> bool:
    return isinstance(job_id, str) and bool(_JOB_ID.match(job_id))


def upload_extension(filename: Optional[str]) -> Optional[str]:
    """Return the lower-case allowed extension of filename, or None."""
    if not filename:
        return None
    base = re.split(r"[\\/]", filename)[-1]
    ext = os.path.splitext(base)[1].lower()
    return ext if ext in ALLOWED_EXTENSIONS else None


def sanitize_display_name(name: Optional[str], fallback: str = "untitled") -> str:
    """Make a user-supplied name safe for display and download headers."""
    if not name:
        return fallback
    name = re.split(r"[\\/]", str(name))[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(" " if unicodedata.category(c)[0] in ("C", "Z") else c for c in name)
    name = _WS.sub(" ", name).strip().lstrip(".").strip()
    if len(name) > MAX_DISPLAY_NAME:
        name = name[:MAX_DISPLAY_NAME].rstrip()
    return name or fallback


def download_filename(display_name: str, ext: str) -> str:
    """Download name: display name minus a known media extension, plus ext."""
    stem = sanitize_display_name(display_name, "transcript")
    root, old_ext = os.path.splitext(stem)
    if old_ext.lower() in ALLOWED_EXTENSIONS and root:
        stem = root
    return f"{stem}{ext}"


def content_disposition(filename: str) -> str:
    """RFC 6266 attachment header with an ASCII fallback and UTF-8 filename*."""
    ascii_name = "".join(
        c if 0x20 <= ord(c) < 0x7F and c not in '"\\;%' else "_" for c in filename
    ) or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
