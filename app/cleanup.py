"""Retention sweep and media cleanup (stdlib only). Never follows symlinks."""

import json
import logging
import os
import shutil
import stat
import time
from typing import Iterable, List, Optional

from .filenames import is_valid_job_id

log = logging.getLogger(__name__)

KEEP_FILES = frozenset({"job.json", "transcript.txt", "transcript.srt"})


def _created_time(job_dir: str) -> float:
    """job.json 'created' if readable, else the directory's own mtime."""
    path = os.path.join(job_dir, "job.json")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            created = json.load(f).get("created")
        if isinstance(created, (int, float)) and not isinstance(created, bool):
            return float(created)
    except (OSError, ValueError, AttributeError):
        pass
    return os.lstat(job_dir).st_mtime


def sweep(jobs_dir: str, retention_seconds: float, protected: Iterable[str] = (),
          now: Optional[float] = None) -> List[str]:
    """Delete expired job dirs directly under jobs_dir; return removed ids."""
    now = time.time() if now is None else now
    protected = set(protected)
    removed = []
    try:
        entries = list(os.scandir(jobs_dir))
    except FileNotFoundError:
        return removed
    for entry in entries:
        if not is_valid_job_id(entry.name) or entry.name in protected:
            continue
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            if now - _created_time(entry.path) < retention_seconds:
                continue
            # rmtree unlinks symlinks inside the tree rather than following them,
            # and refuses a symlinked top-level path.
            shutil.rmtree(entry.path)
            removed.append(entry.name)
        except OSError:
            log.exception("retention sweep failed for job %s", entry.name)
    return removed


def delete_media(job_dir: str, keep: Iterable[str] = KEEP_FILES) -> None:
    """Remove everything in a job dir except the names in keep (by default
    job.json and the transcripts)."""
    keep = frozenset(keep)
    try:
        st = os.lstat(job_dir)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        return
    for entry in os.scandir(job_dir):
        if entry.name in keep:
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except OSError:
            log.exception("could not delete media file in %s", os.path.basename(job_dir))
