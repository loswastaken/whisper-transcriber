"""Job cancel: queue, worker, child process groups, recovery and retention.

Runs without fastapi or faster-whisper: real child processes are small Python
scripts started through the same helper as yt-dlp/ffmpeg, and the model is a fake.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from app import fetch, procs
from app.config import Settings
from app.errors import UserError
from app.jobs import AlreadyFinished, Conflict, JobStore
from app.transcribe import FasterWhisperTranscriber

DAY = 24 * 3600
REAL_TO_WAV = fetch.to_wav


def group_alive(pgid):
    """True if any non-zombie process is in group pgid (Linux /proc)."""
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[1].split()
        if fields[0] != "Z" and int(fields[2]) == pgid:
            return True
    return False


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def sleeper_script(pid_file, ignore_term=False):
    """A child that starts a grandchild, records both pids and sleeps."""
    return (
        "import os, signal, subprocess, sys, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        + f"open({pid_file!r}, 'w').write('%d %d' % (os.getpid(), g.pid))\n"
        + "print('[download]  12.5% of 1.00MiB', flush=True)\n"
        + "time.sleep(60)\n"
    )


class FakeModel:
    """Yields one segment per `step` seconds; `gate` (if set) must be open to go on."""

    def __init__(self, count=200, step=0.05, gate=None):
        self.count = count
        self.step = step
        self.gate = gate
        self.yielded = 0

    def transcribe(self, wav_path, **kwargs):
        def gen():
            for i in range(self.count):
                if self.gate is not None:
                    self.gate.wait(10)
                time.sleep(self.step)
                self.yielded += 1
                yield SimpleNamespace(start=float(i), end=float(i + 1), text=f"word {i}.")
        return gen(), SimpleNamespace(duration=float(self.count), language="en")


def fake_to_wav(src, dst, timeout=None, cancel=None):
    with open(dst, "wb") as f:
        f.write(b"RIFF")


@unittest.skipUnless(os.path.isdir("/proc") and hasattr(os, "killpg"), "needs Linux")
class CancelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="transcriber-test-")
        self.root = self.tmp.name
        self.settings = Settings(
            data_dir=os.path.join(self.root, "data"), model_name="fake", compute_type="int8",
            device="cpu", cpu_threads=1, retention_hours=24, max_upload_bytes=1 << 20,
            max_media_seconds=21600, port=0, hf_home=os.path.join(self.root, "models"),
        )
        os.makedirs(self.settings.jobs_dir)
        self.transcriber = FasterWhisperTranscriber(self.settings)
        self.model = FakeModel()
        self.transcriber._model = self.model
        self.store = JobStore(self.settings, self.transcriber)
        self.pid_file = os.path.join(self.root, "pids")
        patches = [
            mock.patch.object(fetch, "probe_duration", lambda path, cancel=None: 10.0),
            mock.patch.object(fetch, "to_wav", fake_to_wav),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.store.stop()
        if self.store._worker is not None:
            self.store._worker.join(10)
        self.tmp.cleanup()

    def start_worker(self):
        # Worker only; the retention loop is exercised directly via sweep().
        self.store._worker = threading.Thread(target=self.store._work_loop, daemon=True)
        self.store._worker.start()

    def upload_job(self, name="talk.mp3"):
        job_id = self.store.new_id()
        os.makedirs(self.store.job_dir(job_id))
        with open(os.path.join(self.store.job_dir(job_id), "upload.mp3"), "wb") as f:
            f.write(b"media")
        self.store.create(job_id, "upload", name, source="upload.mp3")
        return job_id

    def url_job(self):
        job_id = self.store.new_id()
        self.store.create(job_id, "url", "example.invalid", url="https://example.invalid/v")
        return job_id

    def status(self, job_id):
        return self.store.get(job_id)["status"]

    def files(self, job_id):
        return sorted(os.listdir(self.store.job_dir(job_id)))

    def wait_status(self, job_id, status, timeout=5.0):
        return wait_for(lambda: self.status(job_id) == status, timeout)

    def read_pids(self):
        self.assertTrue(wait_for(lambda: os.path.exists(self.pid_file) and
                                 os.path.getsize(self.pid_file) > 0))
        with open(self.pid_file) as f:
            return [int(x) for x in f.read().split()]

    # ---- queued -------------------------------------------------------------

    def test_cancel_queued_job(self):
        job_id = self.upload_job()
        self.assertEqual(self.store.cancel(job_id), "cancelled")
        self.assertNotIn(job_id, self.store.queue)
        job = self.store.get(job_id)
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNone(job["error"])
        self.assertIsNotNone(job["finished"])
        self.assertEqual(self.files(job_id), ["job.json"])
        with open(os.path.join(self.store.job_dir(job_id), "job.json")) as f:
            self.assertEqual(json.load(f)["status"], "cancelled")
        self.assertIn(job_id, [j["id"] for j in self.store.list()])

    def test_unknown_and_finished(self):
        self.assertIsNone(self.store.cancel(uuid.uuid4().hex))
        job_id = self.upload_job()
        self.store.cancel(job_id)
        with self.assertRaises(AlreadyFinished):
            self.store.cancel(job_id)
        for status in ("done", "error"):
            other = self.upload_job()
            self.store.queue.remove(other)
            self.store.jobs[other]["status"] = status
            with self.assertRaises(AlreadyFinished):
                self.store.cancel(other)

    def test_cancelled_job_can_be_deleted(self):
        job_id = self.upload_job()
        self.store.cancel(job_id)
        self.assertTrue(self.store.delete(job_id))
        self.assertFalse(os.path.exists(self.store.job_dir(job_id)))

    # ---- running --------------------------------------------------------------

    def test_cancel_during_download_kills_process_group(self):
        script = sleeper_script(self.pid_file)
        with mock.patch.object(fetch, "YTDLP_CMD", [sys.executable, "-c", script]):
            job_id = self.url_job()
            # Leftover yt-dlp temp files must go too.
            with open(os.path.join(self.store.job_dir(job_id), "source.webm.part"), "wb") as f:
                f.write(b"partial")
            self.start_worker()
            leader, grandchild = self.read_pids()
            self.assertTrue(wait_for(lambda: self.store.get(job_id)["progress"] == 12.5))
            t0 = time.monotonic()
            self.assertEqual(self.store.cancel(job_id), "cancelling")
            self.assertEqual(self.status(job_id), "cancelling")
            self.assertTrue(self.wait_status(job_id, "cancelled"))
            self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(wait_for(lambda: not group_alive(leader)))
        job = self.store.get(job_id)
        self.assertIsNone(job["error"])
        self.assertEqual(job["progress"], 12.5)
        self.assertEqual(self.files(job_id), ["job.json"])
        self.assertIsNone(self.store.running_id)

    def test_cancel_child_that_ignores_sigterm(self):
        cancel = threading.Event()
        script = sleeper_script(self.pid_file, ignore_term=True)
        with procs.Child([sys.executable, "-c", script], cancel, None) as child:
            leader, _ = self.read_pids()
            self.assertEqual(child.pid, leader)
            t0 = time.monotonic()
            threading.Timer(0.1, cancel.set).start()
            child.wait()
        self.assertEqual(child.reason, "cancelled")
        self.assertLess(time.monotonic() - t0, procs.TERM_GRACE_SECONDS + 2)
        self.assertTrue(wait_for(lambda: not group_alive(leader), 2))

    def test_download_timeout_still_stops_group(self):
        script = sleeper_script(self.pid_file)
        with mock.patch.object(fetch, "YTDLP_CMD", [sys.executable, "-c", script]):
            job_dir = os.path.join(self.root, "dl")
            os.makedirs(job_dir)
            with self.assertRaises(UserError) as ctx:
                fetch.download("https://example.invalid/v", job_dir, 60,
                               on_progress=lambda p: None, on_title=lambda t: None,
                               timeout=0.5)
        self.assertIn("too long", str(ctx.exception))
        leader, _ = self.read_pids()
        self.assertTrue(wait_for(lambda: not group_alive(leader), 2))

    def test_cancel_during_extracting(self):
        script = sleeper_script(self.pid_file)
        # The real to_wav, with a fake ffmpeg.
        with mock.patch.object(fetch, "to_wav", REAL_TO_WAV), \
                mock.patch.object(fetch, "FFMPEG_CMD", [sys.executable, "-c", script]):
            job_id = self.upload_job()
            self.start_worker()
            leader, _ = self.read_pids()
            self.assertEqual(self.status(job_id), "extracting")
            self.assertEqual(self.store.cancel(job_id), "cancelling")
            self.assertTrue(self.wait_status(job_id, "cancelled"))
        self.assertTrue(wait_for(lambda: not group_alive(leader), 2))
        self.assertEqual(self.files(job_id), ["job.json"])

    def test_cancel_during_transcription(self):
        self.model.step = 0.1
        job_id = self.upload_job()
        self.start_worker()
        self.assertTrue(wait_for(lambda: self.model.yielded >= 3))
        self.assertEqual(self.status(job_id), "transcribing")
        t0 = time.monotonic()
        self.assertEqual(self.store.cancel(job_id), "cancelling")
        self.assertTrue(self.wait_status(job_id, "cancelled", timeout=3))
        self.assertLess(time.monotonic() - t0, 3)
        stopped_at = self.model.yielded
        time.sleep(0.5)
        self.assertEqual(self.model.yielded, stopped_at)  # iteration really stopped
        self.assertLess(stopped_at, self.model.count)
        self.assertGreater(self.store.get(job_id)["progress"], 0)
        self.assertEqual(self.files(job_id), ["job.json"])

    def test_cancel_is_idempotent_while_pending(self):
        gate = threading.Event()
        gate.set()
        self.model.gate = gate
        job_id = self.upload_job()
        self.start_worker()
        self.assertTrue(wait_for(lambda: self.model.yielded >= 1))
        gate.clear()  # hold the model before its next segment
        time.sleep(0.2)
        self.assertEqual(self.store.cancel(job_id), "cancelling")
        self.assertEqual(self.store.cancel(job_id), "cancelling")
        self.assertEqual(self.status(job_id), "cancelling")
        with self.assertRaises(Conflict):
            self.store.delete(job_id)  # still running
        gate.set()
        self.assertTrue(self.wait_status(job_id, "cancelled"))
        with self.assertRaises(AlreadyFinished):
            self.store.cancel(job_id)

    def test_worker_runs_next_job_after_cancel(self):
        self.model.count = 30
        self.model.step = 0.1
        first = self.upload_job("first.mp3")
        second = self.upload_job("second.mp3")
        self.start_worker()
        self.assertTrue(wait_for(lambda: self.status(first) == "transcribing"))
        self.store.cancel(first)
        self.assertTrue(self.wait_status(first, "cancelled"))
        self.model.step = 0.0
        self.assertTrue(self.wait_status(second, "done", timeout=10))
        self.assertEqual(sorted(self.files(second)),
                         ["job.json", "transcript.srt", "transcript.txt"])

    def test_check_before_transcribing(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(procs.JobCancelled):
            self.transcriber.transcribe("audio.wav", lambda p: None, cancel=cancel)
        self.assertEqual(self.model.yielded, 0)

    # ---- restart and retention --------------------------------------------------

    def write_job(self, status, age=0.0):
        job_id = uuid.uuid4().hex
        d = os.path.join(self.settings.jobs_dir, job_id)
        os.makedirs(d)
        job = {"id": job_id, "kind": "upload", "name": "x", "status": status,
               "progress": 40.0, "created": time.time() - age, "finished": None,
               "language": None, "duration": None, "error": None, "url": None,
               "source": "upload.mp3"}
        with open(os.path.join(d, "job.json"), "w") as f:
            json.dump(job, f)
        for name in ("upload.mp3", "audio.wav", "transcript.txt"):
            with open(os.path.join(d, name), "w") as f:
                f.write("x")
        return job_id

    def test_recovery_maps_cancelling_to_cancelled(self):
        cancelling = self.write_job("cancelling")
        running = self.write_job("transcribing")
        self.store.recover()
        job = self.store.get(cancelling)
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNone(job["error"])
        self.assertIsNotNone(job["finished"])
        self.assertEqual(job["progress"], 40.0)
        self.assertEqual(self.files(cancelling), ["job.json"])
        self.assertEqual(self.store.get(running)["status"], "error")

    def test_retention_sweeps_cancelled_jobs(self):
        old = self.write_job("cancelled", age=DAY + 60)
        fresh = self.write_job("cancelled", age=60)
        self.store.recover()
        self.store.sweep()
        self.assertIsNone(self.store.get(old))
        self.assertFalse(os.path.exists(os.path.join(self.settings.jobs_dir, old)))
        self.assertEqual(self.store.get(fresh)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
