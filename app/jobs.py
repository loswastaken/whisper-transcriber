"""Job store, persistence and the single background worker."""

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from typing import Dict, List, Optional

from . import cleanup, fetch
from .config import Settings
from .errors import UserError
from .filenames import is_valid_job_id, sanitize_display_name
from .procs import JobCancelled, check_cancel
from .textfmt import build_srt, build_txt
from .transcribe import Transcriber

log = logging.getLogger(__name__)

ACTIVE = ("queued", "downloading", "extracting", "transcribing", "cancelling")
FINISHED = ("done", "error", "cancelled")
# A cancelled job keeps only its job.json; partial transcripts go with the media.
CANCELLED_KEEP = ("job.json",)
PUBLIC_FIELDS = ("id", "kind", "name", "status", "progress", "created", "finished",
                 "language", "duration", "error")
SAVE_INTERVAL_SECONDS = 2.0


def atomic_write(path: str, data: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Conflict(Exception):
    pass


class AlreadyFinished(Exception):
    pass


class JobStore:
    def __init__(self, settings: Settings, transcriber: Transcriber):
        self.settings = settings
        self.transcriber = transcriber
        self.jobs: Dict[str, dict] = {}
        self.queue: deque = deque()
        self.running_id: Optional[str] = None
        self.running_cancel: Optional[threading.Event] = None
        self.cond = threading.Condition()
        self._last_save: Dict[str, float] = {}
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._sweeper: Optional[threading.Thread] = None

    # ---- paths and persistence -------------------------------------------------

    def job_dir(self, job_id: str) -> str:
        if not is_valid_job_id(job_id):
            raise ValueError("invalid job id")
        return os.path.join(self.settings.jobs_dir, job_id)

    def _save(self, job: dict) -> None:
        path = os.path.join(self.job_dir(job["id"]), "job.json")
        try:
            atomic_write(path, json.dumps(job, ensure_ascii=False))
        except FileNotFoundError:
            pass  # job dir deleted concurrently
        self._last_save[job["id"]] = time.monotonic()

    def recover(self) -> None:
        """Load persisted jobs; anything that was queued or running is failed,
        except a job that was being cancelled, which becomes cancelled."""
        os.makedirs(self.settings.jobs_dir, exist_ok=True)
        for entry in os.scandir(self.settings.jobs_dir):
            if not is_valid_job_id(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            path = os.path.join(entry.path, "job.json")
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "r", encoding="utf-8") as f:
                    job = json.load(f)
            except (OSError, ValueError):
                continue  # left for the retention sweep
            if not isinstance(job, dict) or job.get("id") != entry.name:
                continue
            if job.get("status") == "cancelling":
                job.update(status="cancelled", error=None, finished=time.time())
                self._save(job)
                cleanup.delete_media(entry.path, keep=CANCELLED_KEEP)
            elif job.get("status") not in FINISHED:
                job.update(status="error", error="interrupted by restart",
                           finished=time.time(), queue_position=0)
                self._save(job)
                cleanup.delete_media(entry.path)
            self.jobs[entry.name] = job
        log.info("recovered %d jobs", len(self.jobs))

    # ---- public API ------------------------------------------------------------

    def new_id(self) -> str:
        return uuid.uuid4().hex

    def create(self, job_id: str, kind: str, name: str, url: Optional[str] = None,
               source: Optional[str] = None) -> dict:
        job = {
            "id": job_id, "kind": kind, "name": sanitize_display_name(name),
            "status": "queued", "progress": 0.0, "created": time.time(),
            "finished": None, "language": None, "duration": None, "error": None,
            "url": url, "source": source,
        }
        os.makedirs(self.job_dir(job_id), exist_ok=True)
        with self.cond:
            self.jobs[job_id] = job
            self._save(job)
            self.queue.append(job_id)
            self.cond.notify_all()
        return job

    def _public(self, job: dict) -> dict:
        out = {k: job.get(k) for k in PUBLIC_FIELDS}
        out["progress"] = round(float(job.get("progress") or 0.0), 1)
        if job["status"] == "queued" and job["id"] in self.queue:
            out["queue_position"] = list(self.queue).index(job["id"]) + 1
        else:
            out["queue_position"] = 0
        if job.get("kind") == "url":
            out["url"] = job.get("url")
        return out

    def list(self) -> List[dict]:
        with self.cond:
            jobs = sorted(self.jobs.values(), key=lambda j: j.get("created") or 0, reverse=True)
            return [self._public(j) for j in jobs]

    def get(self, job_id: str) -> Optional[dict]:
        with self.cond:
            job = self.jobs.get(job_id)
            return self._public(job) if job else None

    def delete(self, job_id: str) -> bool:
        """Remove a job now. Raises Conflict for the running job."""
        with self.cond:
            if job_id not in self.jobs:
                return False
            if job_id == self.running_id:
                raise Conflict()
            try:
                self.queue.remove(job_id)
            except ValueError:
                pass
            del self.jobs[job_id]
            self._last_save.pop(job_id, None)
            path = self.job_dir(job_id)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        return True

    def cancel(self, job_id: str) -> Optional[str]:
        """Cancel a job. Returns None if unknown, "cancelled" for a queued job
        (stopped at once), "cancelling" for the running job (the worker stops it
        shortly; repeat calls return "cancelling" again). Raises AlreadyFinished."""
        with self.cond:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if job["status"] in FINISHED:
                raise AlreadyFinished()
            if job_id == self.running_id:
                assert self.running_cancel is not None
                self.running_cancel.set()
                if job["status"] != "cancelling":
                    job["status"] = "cancelling"
                    self._save(job)
                return "cancelling"
            try:
                self.queue.remove(job_id)
            except ValueError:
                pass
            job.update(status="cancelled", error=None, finished=time.time())
            self._save(job)
            path = self.job_dir(job_id)
        cleanup.delete_media(path, keep=CANCELLED_KEEP)
        return "cancelled"

    def health(self) -> dict:
        with self.cond:
            queue_length = len(self.queue)
        return {
            "status": "ok",
            "queue_length": queue_length,
            "worker_alive": bool(self._worker and self._worker.is_alive()),
            "model_loaded": self.transcriber.loaded,
            "backend": self.settings.whisper_backend,
        }

    # ---- worker ----------------------------------------------------------------

    def _update(self, job_id: str, force: bool = False, **fields) -> None:
        with self.cond:
            job = self.jobs.get(job_id)
            if job is None:
                return
            if job.get("status") == "cancelling" and fields.get("status") in ACTIVE:
                fields.pop("status")  # a pending cancel outranks stage changes
            status_changed = "status" in fields and fields["status"] != job.get("status")
            job.update(fields)
            last = self._last_save.get(job_id, 0.0)
            if force or status_changed or time.monotonic() - last >= SAVE_INTERVAL_SECONDS:
                self._save(job)

    def start(self) -> None:
        self._worker = threading.Thread(target=self._work_loop, name="job-worker", daemon=True)
        self._worker.start()
        self._sweeper = threading.Thread(target=self._sweep_loop, name="retention", daemon=True)
        self._sweeper.start()

    def stop(self) -> None:
        self._stop.set()
        with self.cond:
            self.cond.notify_all()

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            with self.cond:
                while not self.queue and not self._stop.is_set():
                    self.cond.wait(timeout=5)
                if self._stop.is_set():
                    return
                job_id = self.queue.popleft()
                if job_id not in self.jobs:
                    continue
                self.running_id = job_id
                self.running_cancel = cancel = threading.Event()
                job = dict(self.jobs[job_id])
            keep = cleanup.KEEP_FILES
            try:
                self._run(job, cancel)
                with self.cond:  # cancel() sets the flag under this lock too
                    check_cancel(cancel)
                    self._update(job_id, force=True, status="done", progress=100.0,
                                 finished=time.time())
            except JobCancelled:
                log.info("job %s cancelled", job_id)
                keep = CANCELLED_KEEP
                self._update(job_id, force=True, status="cancelled", error=None,
                             finished=time.time())
            except UserError as exc:
                log.info("job %s failed: %s", job_id, exc)
                self._update(job_id, force=True, status="error", error=str(exc)[:300],
                             finished=time.time())
            except Exception:
                log.exception("job %s crashed", job_id)
                self._update(job_id, force=True, status="error", error="transcription failed",
                             finished=time.time())
            finally:
                try:
                    cleanup.delete_media(self.job_dir(job_id), keep=keep)
                except OSError:
                    log.exception("media cleanup failed for %s", job_id)
                with self.cond:
                    self.running_id = None
                    self.running_cancel = None

    def _run(self, job: dict, cancel: threading.Event) -> None:
        s = self.settings
        job_id = job["id"]
        jdir = self.job_dir(job_id)
        if job["kind"] == "url":
            self._update(job_id, status="downloading", progress=0.0)
            src = fetch.download(
                job["url"], jdir, s.max_media_seconds,
                on_progress=lambda p: self._update(job_id, progress=p),
                on_title=lambda t: self._update(job_id, name=sanitize_display_name(t)),
                cancel=cancel,
            )
        else:
            src = os.path.join(jdir, job["source"])

        check_cancel(cancel)
        self._update(job_id, status="extracting", progress=0.0)
        duration = fetch.probe_duration(src, cancel=cancel)
        fetch.check_duration(duration, s.max_media_seconds)
        wav = os.path.join(jdir, "audio.wav")
        fetch.to_wav(src, wav, cancel=cancel)
        os.unlink(src)
        wav_duration = fetch.probe_duration(wav, cancel=cancel)
        fetch.check_duration(wav_duration, s.max_media_seconds)
        self._update(job_id, duration=wav_duration or duration)

        check_cancel(cancel)
        self._update(job_id, status="transcribing", progress=0.0)
        t0 = time.monotonic()
        segments, language, model_duration = self.transcriber.transcribe(
            wav, on_progress=lambda p: self._update(job_id, progress=p), cancel=cancel,
        )
        check_cancel(cancel)
        log.info("job %s transcribed %.0f s of audio in %.0f s", job_id,
                 model_duration, time.monotonic() - t0)
        atomic_write(os.path.join(jdir, "transcript.txt"), build_txt(segments))
        atomic_write(os.path.join(jdir, "transcript.srt"), build_srt(segments))
        self._update(job_id, language=language or None,
                     duration=model_duration or wav_duration or duration)

    # ---- retention ---------------------------------------------------------------

    def sweep(self) -> None:
        with self.cond:
            protected = set(self.queue)
            if self.running_id:
                protected.add(self.running_id)
            removed = cleanup.sweep(self.settings.jobs_dir, self.settings.retention_seconds,
                                    protected)
            for job_id in removed:
                self.jobs.pop(job_id, None)
                self._last_save.pop(job_id, None)
        if removed:
            log.info("retention sweep removed %d jobs", len(removed))

    def _sweep_loop(self) -> None:
        while True:
            try:
                self.sweep()
            except Exception:
                log.exception("retention sweep failed")
            if self._stop.wait(600):
                return
