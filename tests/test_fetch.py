"""yt-dlp resolution: the venv copy must be used when it is not on PATH."""

import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from app import fetch


class YtdlpResolutionTest(unittest.TestCase):
    def _install(self, root, executable=True):
        bindir = os.path.join(root, "bin")
        os.makedirs(bindir)
        path = os.path.join(bindir, "yt-dlp")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        if executable:
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def test_uses_venv_script_when_prefix_has_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._install(tmp)
            with mock.patch.object(sys, "prefix", tmp):
                self.assertEqual(fetch.ytdlp_executable(), script)
                self.assertEqual(fetch.ytdlp_argv(), [script])

    def test_bare_name_when_venv_has_no_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sys, "prefix", tmp):
                self.assertEqual(fetch.ytdlp_executable(), "yt-dlp")
                self.assertEqual(fetch.ytdlp_argv(), ["yt-dlp"])

    def test_ignores_non_executable_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._install(tmp, executable=False)
            with mock.patch.object(sys, "prefix", tmp):
                self.assertEqual(fetch.ytdlp_executable(), "yt-dlp")

    def test_patched_command_is_left_alone(self):
        stand_in = [sys.executable, "-c", "pass"]
        with mock.patch.object(fetch, "YTDLP_CMD", stand_in):
            self.assertEqual(fetch.ytdlp_argv(), stand_in)

    def test_download_invokes_the_venv_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._install(tmp)
            job = os.path.join(tmp, "job")
            os.makedirs(job)
            source = os.path.join(job, "source.m4a")
            with open(source, "wb") as f:
                f.write(b"audio")
            seen = {}

            class FakeChild:
                def __init__(self, cmd, *args, **kwargs):
                    seen["cmd"] = cmd
                    self.reason = None

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                class proc:
                    stdout = []

                def wait(self):
                    return 0

            with mock.patch.object(sys, "prefix", tmp), \
                    mock.patch.object(fetch, "Child", FakeChild):
                got = fetch.download(
                    "https://example.invalid/v", job, 3600,
                    lambda _progress: None, lambda _title: None,
                )
            self.assertEqual(got, source)
            self.assertEqual(seen["cmd"][0], script)
            self.assertEqual(seen["cmd"][-2:], ["--", "https://example.invalid/v"])
