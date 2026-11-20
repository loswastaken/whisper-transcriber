import json
import os
import tempfile
import time
import unittest
import uuid

from app.cleanup import delete_media, sweep

DAY = 24 * 3600


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="transcriber-test-")
        self.root = self.tmp.name
        self.jobs = os.path.join(self.root, "jobs")
        os.makedirs(self.jobs)
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def make_job(self, age, with_json=True):
        job_id = uuid.uuid4().hex
        d = os.path.join(self.jobs, job_id)
        os.makedirs(d)
        if with_json:
            with open(os.path.join(d, "job.json"), "w") as f:
                json.dump({"id": job_id, "created": self.now - age}, f)
        else:
            os.utime(d, (self.now - age, self.now - age))
        return job_id

    def test_removes_only_old_non_running(self):
        old = self.make_job(2 * DAY)
        new = self.make_job(60)
        running = self.make_job(3 * DAY)
        old_nojson = self.make_job(2 * DAY, with_json=False)
        new_nojson = self.make_job(60, with_json=False)
        removed = sweep(self.jobs, DAY, protected={running}, now=self.now)
        self.assertEqual(sorted(removed), sorted([old, old_nojson]))
        remaining = set(os.listdir(self.jobs))
        self.assertEqual(remaining, {new, running, new_nojson})

    def test_ignores_non_job_names(self):
        other = os.path.join(self.jobs, "not-a-job")
        os.makedirs(other)
        os.utime(other, (0, 0))
        sweep(self.jobs, DAY, now=self.now)
        self.assertTrue(os.path.isdir(other))

    def test_does_not_follow_symlinked_job_dir(self):
        victim = os.path.join(self.root, "victim")
        os.makedirs(victim)
        with open(os.path.join(victim, "keep.txt"), "w") as f:
            f.write("x")
        os.utime(victim, (0, 0))
        link = os.path.join(self.jobs, uuid.uuid4().hex)
        os.symlink(victim, link)
        removed = sweep(self.jobs, DAY, now=self.now)
        self.assertEqual(removed, [])
        self.assertTrue(os.path.exists(os.path.join(victim, "keep.txt")))

    def test_does_not_follow_symlinks_inside_job(self):
        outside = os.path.join(self.root, "outside")
        os.makedirs(outside)
        target = os.path.join(outside, "keep.txt")
        with open(target, "w") as f:
            f.write("x")
        old = self.make_job(2 * DAY)
        os.symlink(outside, os.path.join(self.jobs, old, "dirlink"))
        os.symlink(target, os.path.join(self.jobs, old, "filelink"))
        self.assertEqual(sweep(self.jobs, DAY, now=self.now), [old])
        self.assertTrue(os.path.exists(target))

    def test_symlinked_job_json_not_read(self):
        fake = os.path.join(self.root, "fake.json")
        with open(fake, "w") as f:
            json.dump({"created": 0}, f)
        job_id = uuid.uuid4().hex
        d = os.path.join(self.jobs, job_id)
        os.makedirs(d)
        os.symlink(fake, os.path.join(d, "job.json"))
        # job.json is a symlink, so the (fresh) dir mtime is used instead.
        self.assertEqual(sweep(self.jobs, DAY, now=self.now), [])
        self.assertTrue(os.path.exists(fake))

    def test_missing_jobs_dir(self):
        self.assertEqual(sweep(os.path.join(self.root, "nope"), DAY), [])


class DeleteMediaTest(unittest.TestCase):
    def test_keeps_transcripts_only(self):
        with tempfile.TemporaryDirectory(prefix="transcriber-test-") as root:
            d = os.path.join(root, uuid.uuid4().hex)
            os.makedirs(d)
            outside = os.path.join(root, "outside.txt")
            with open(outside, "w") as f:
                f.write("x")
            for name in ("job.json", "transcript.txt", "transcript.srt",
                         "upload.mp3", "audio.wav", "source.webm.part"):
                with open(os.path.join(d, name), "w") as f:
                    f.write("x")
            os.symlink(outside, os.path.join(d, "source.m4a"))
            delete_media(d)
            self.assertEqual(sorted(os.listdir(d)),
                             ["job.json", "transcript.srt", "transcript.txt"])
            self.assertTrue(os.path.exists(outside))


if __name__ == "__main__":
    unittest.main()
