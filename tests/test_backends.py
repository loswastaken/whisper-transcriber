"""Backend selection and the MLX progress/cancel hook, with a fake mlx_whisper.

Needs neither faster-whisper nor mlx: the fake mlx_whisper.transcribe module
drives its progress bar the way mlx-whisper 0.4.3 does (`import tqdm`, then
`tqdm.tqdm(total=content_frames, unit="frames", disable=...)` and one
`update(n)` per window).
"""

import os
import sys
import tempfile
import threading
import types
import unittest
import wave
from unittest import mock

from app import config, procs, transcribe_mlx
from app.config import Settings
from app.transcribe import FasterWhisperTranscriber, make_transcriber
from app.transcribe_mlx import MlxTranscriber

WINDOW = 3000  # mel frames in a 30 s window


def make_settings(**kwargs):
    base = dict(data_dir="data", model_name="fake", compute_type="int8", device="cpu",
                cpu_threads=1, retention_hours=24, max_upload_bytes=1 << 20,
                max_media_seconds=21600, port=0, hf_home="models")
    base.update(kwargs)
    return Settings(**base)


def write_wav(path, seconds=1.0, channels=1, rate=16000):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * channels * int(rate * seconds))


class FakeMlx:
    """Fake `mlx_whisper` package, `mlx_whisper.transcribe` module and `mlx.core`."""

    def __init__(self, windows=4, with_tqdm=True):
        self.windows = windows
        self.done_windows = 0
        self.calls = []
        self.loads = []
        self.during_window = None  # callable(i), run before each update

        self.real_tqdm = types.SimpleNamespace(tqdm=self._never_used)
        self.module = types.ModuleType("mlx_whisper.transcribe")
        if with_tqdm:
            self.module.tqdm = self.real_tqdm
        self.module.ModelHolder = types.SimpleNamespace(
            get_model=lambda path, dtype: self.loads.append((path, dtype)))
        self.package = types.ModuleType("mlx_whisper")
        self.package.transcribe = self.transcribe  # shadows the submodule, as in 0.4.3
        self.mlx = types.ModuleType("mlx")
        self.core = types.ModuleType("mlx.core")
        self.core.float16 = "float16"
        self.mlx.core = self.core

    def _never_used(self, *args, **kwargs):
        raise AssertionError("real tqdm used while the hook should be installed")

    def transcribe(self, audio, *, path_or_hf_repo, verbose=None, **kwargs):
        self.calls.append(dict(kwargs, audio=audio, path_or_hf_repo=path_or_hf_repo,
                               verbose=verbose))
        total = self.windows * WINDOW
        bar = getattr(self.module, "tqdm", None)
        if bar is not None:
            with bar.tqdm(total=total, unit="frames", disable=verbose is not False) as pbar:
                for i in range(self.windows):
                    if self.during_window:
                        self.during_window(i)
                    self.done_windows += 1
                    pbar.update(WINDOW)
        else:
            for i in range(self.windows):
                if self.during_window:
                    self.during_window(i)
                self.done_windows += 1
        return {"text": "hello world", "language": "de",
                "segments": [{"start": 0, "end": 1.5, "text": " hello"},
                             {"start": 1.5, "end": 3, "text": " world"}]}

    def installed(self):
        return mock.patch.dict(sys.modules, {
            "mlx_whisper": self.package, "mlx_whisper.transcribe": self.module,
            "mlx": self.mlx, "mlx.core": self.core,
        })


class ConfigTest(unittest.TestCase):
    def load(self, **env):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("WHISPER_BACKEND", "MLX_MODEL")}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            return config.load()

    def test_defaults(self):
        s = self.load()
        self.assertEqual(s.whisper_backend, "faster-whisper")
        self.assertEqual(s.mlx_model, "mlx-community/whisper-large-v3-turbo")

    def test_mlx_selected(self):
        s = self.load(WHISPER_BACKEND=" MLX ", MLX_MODEL="mlx-community/whisper-tiny")
        self.assertEqual(s.whisper_backend, "mlx")
        self.assertEqual(s.mlx_model, "mlx-community/whisper-tiny")

    def test_empty_means_default(self):
        self.assertEqual(self.load(WHISPER_BACKEND="").whisper_backend, "faster-whisper")

    def test_unknown_backend_refused(self):
        with self.assertRaises(SystemExit):
            self.load(WHISPER_BACKEND="cuda")

    def test_settings_defaults_keep_old_constructor_working(self):
        self.assertEqual(make_settings().whisper_backend, "faster-whisper")


class FactoryTest(unittest.TestCase):
    def test_faster_whisper_default(self):
        t = make_transcriber(make_settings())
        self.assertIsInstance(t, FasterWhisperTranscriber)
        self.assertFalse(t.loaded)

    def test_mlx_does_not_import_mlx_whisper(self):
        with mock.patch.dict(sys.modules):
            sys.modules.pop("mlx_whisper", None)
            t = make_transcriber(make_settings(whisper_backend="mlx"))
            self.assertIsInstance(t, MlxTranscriber)
            self.assertFalse(t.loaded)
            self.assertNotIn("mlx_whisper", sys.modules)


class MlxTranscriberTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="transcriber-test-")
        self.addCleanup(self.tmp.cleanup)
        self.wav = os.path.join(self.tmp.name, "audio.wav")
        write_wav(self.wav, seconds=2.5)
        p = mock.patch.object(transcribe_mlx, "pcm16_to_float32", lambda frames: frames)
        p.start()
        self.addCleanup(p.stop)
        self.fake = FakeMlx()
        installed = self.fake.installed()
        installed.start()
        self.addCleanup(installed.stop)
        self.t = MlxTranscriber(make_settings(whisper_backend="mlx",
                                              mlx_model="example/whisper-test"))
        self.progress = []

    def run_job(self, cancel=None):
        return self.t.transcribe(self.wav, self.progress.append, cancel=cancel)

    def test_result_progress_and_restore(self):
        segments, language, duration = self.run_job(threading.Event())
        self.assertEqual(segments, [(0.0, 1.5, " hello"), (1.5, 3.0, " world")])
        self.assertEqual(language, "de")
        self.assertAlmostEqual(duration, 2.5)
        self.assertEqual(self.progress, [25.0, 50.0, 75.0, 100.0])
        self.assertIs(self.fake.module.tqdm, self.fake.real_tqdm)
        self.assertTrue(self.t.loaded)
        self.assertEqual(self.fake.loads, [("example/whisper-test", "float16")])

    def test_call_arguments(self):
        self.run_job()
        call = self.fake.calls[0]
        self.assertEqual(call["path_or_hf_repo"], "example/whisper-test")
        self.assertIsNone(call["language"])
        self.assertFalse(call["word_timestamps"])
        self.assertIsNone(call["verbose"])
        self.assertEqual(len(call["audio"]), 2 * 16000 * 2.5)  # raw PCM, patched conversion

    def test_model_loaded_once(self):
        self.run_job()
        self.run_job()
        self.assertEqual(len(self.fake.loads), 1)
        self.assertEqual(len(self.fake.calls), 2)

    def test_cancel_between_windows(self):
        cancel = threading.Event()

        def during(i):
            if i == 1:
                cancel.set()

        self.fake.during_window = during
        with self.assertRaises(procs.JobCancelled):
            self.run_job(cancel)
        self.assertEqual(self.fake.done_windows, 2)
        self.assertEqual(self.progress, [25.0])
        self.assertIs(self.fake.module.tqdm, self.fake.real_tqdm)

    def test_cancel_before_start(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(procs.JobCancelled):
            self.run_job(cancel)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.fake.loads, [])
        self.assertFalse(self.t.loaded)

    def test_error_restores_tqdm(self):
        def during(i):
            raise RuntimeError("boom")

        self.fake.during_window = during
        with self.assertRaises(RuntimeError):
            self.run_job()
        self.assertIs(self.fake.module.tqdm, self.fake.real_tqdm)

    def test_without_hook_still_checks_cancel_after(self):
        fake = FakeMlx(with_tqdm=False)
        cancel = threading.Event()
        fake.during_window = lambda i: cancel.set()
        with fake.installed():
            t = MlxTranscriber(make_settings(whisper_backend="mlx"))
            with self.assertLogs("app.transcribe_mlx", "WARNING"):
                with self.assertRaises(procs.JobCancelled):
                    t.transcribe(self.wav, self.progress.append, cancel=cancel)
        self.assertEqual(fake.done_windows, 4)
        self.assertEqual(self.progress, [])

    def test_without_hook_completes(self):
        fake = FakeMlx(with_tqdm=False)
        with fake.installed():
            t = MlxTranscriber(make_settings(whisper_backend="mlx"))
            with self.assertLogs("app.transcribe_mlx", "WARNING"):
                segments, language, _ = t.transcribe(self.wav, self.progress.append)
        self.assertEqual(len(segments), 2)
        self.assertEqual(language, "de")
        self.assertTrue(t.loaded)

    def test_wrong_wav_format_refused(self):
        for kwargs in ({"channels": 2}, {"rate": 44100}):
            write_wav(self.wav, **kwargs)
            with self.assertRaises(RuntimeError):
                self.run_job()
        self.assertEqual(self.fake.calls, [])


class ProgressBarTest(unittest.TestCase):
    def test_positional_and_keyword_total(self):
        seen = []
        with transcribe_mlx.ProgressBar(lambda n, t: seen.append((n, t)), None, None, 10) as bar:
            bar.update(4)
            bar.update(0)
        bar.close()
        self.assertEqual(seen, [(4, 10), (4, 10)])
        bar = transcribe_mlx.ProgressBar(lambda n, t: None, total=None, disable=True)
        self.assertEqual(bar.total, 0)


if __name__ == "__main__":
    unittest.main()
