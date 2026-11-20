"""mlx-whisper backend for Apple Silicon (WHISPER_BACKEND=mlx).

mlx_whisper is imported only here, on the first job; it does not exist on Linux.
mlx_whisper.transcribe() returns everything at once (no segment iterator, no VAD),
so progress and cancel come from its progress bar: the module `mlx_whisper.transcribe`
does `import tqdm` and wraps its decode loop in
`tqdm.tqdm(total=content_frames, unit="frames", ...)`, calling `update(n)` after
every window of up to 30 s (checked against mlx-whisper 0.4.3). While a job runs,
that module's `tqdm` name points at a shim whose update() reports progress and
raises JobCancelled when the cancel flag is set. If the hook cannot be installed
there is no intermediate progress and cancel is checked only before and after.
"""

import importlib
import logging
import sys
import threading
import time
import wave
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Callable, Iterator, List, Optional, Tuple

from .config import Settings
from .procs import check_cancel
from .textfmt import Segment

log = logging.getLogger(__name__)

TRANSCRIBE_MODULE = "mlx_whisper.transcribe"
SAMPLE_RATE = 16000
# Without VAD, silence or music can start a repetition loop; not feeding the
# previous window's text back as a prompt keeps one bad window from spreading.
CONDITION_ON_PREVIOUS_TEXT = False


class ProgressBar:
    """The part of tqdm.tqdm that mlx_whisper.transcribe uses: a context manager
    with update(n). `disable` and the display options are ignored."""

    def __init__(self, on_update: Callable[[int, int], None], iterable=None, desc=None,
                 total=None, **kwargs):
        self._on_update = on_update
        self.total = int(total or 0)
        self.n = 0

    def __enter__(self) -> "ProgressBar":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def update(self, n=1) -> None:
        self.n += int(n or 0)
        self._on_update(self.n, self.total)

    def close(self) -> None:
        pass


@contextmanager
def progress_hook(on_update: Callable[[int, int], None]) -> Iterator[bool]:
    """Point mlx_whisper.transcribe's `tqdm` at ProgressBar for the duration.
    Yields False (and changes nothing) if that module or name is not there."""
    try:
        module = importlib.import_module(TRANSCRIBE_MODULE)
    except ImportError:
        module = None
    original = getattr(module, "tqdm", None)
    if original is None or not callable(getattr(original, "tqdm", None)):
        log.warning("mlx_whisper progress hook unavailable: no progress or mid-run cancel")
        yield False
        return
    module.tqdm = SimpleNamespace(
        tqdm=lambda *args, **kwargs: ProgressBar(on_update, *args, **kwargs))
    try:
        yield True
    finally:
        module.tqdm = original


def read_pcm16(wav_path: str) -> Tuple[bytes, float]:
    """Raw samples and duration of the 16 kHz mono 16-bit WAV made by fetch.to_wav.

    Reading it here instead of handing mlx_whisper the path avoids the ffmpeg it
    would otherwise start outside procs.Child."""
    with wave.open(wav_path, "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, SAMPLE_RATE):
            raise RuntimeError("expected 16 kHz mono 16-bit WAV")
        frames = w.readframes(w.getnframes())
    return frames, len(frames) / 2 / SAMPLE_RATE


def pcm16_to_float32(frames: bytes):
    """Same scaling as mlx_whisper.audio.load_audio."""
    import numpy as np

    return np.frombuffer(frames, np.int16).astype(np.float32) / 32768.0


class MlxTranscriber:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._mlx_whisper = None
        self._loaded = False
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._loaded

    def _load(self):
        """Import mlx_whisper and load the model into its ModelHolder cache, once."""
        if self._mlx_whisper is None:
            import mlx_whisper

            self._mlx_whisper = mlx_whisper
        if not self._loaded:
            holder = getattr(sys.modules.get(TRANSCRIBE_MODULE), "ModelHolder", None)
            if holder is not None:
                import mlx.core as mx

                repo = self._settings.mlx_model
                t0 = time.monotonic()
                log.info("loading model %s (mlx)", repo)
                # float16 is what transcribe() uses by default; the cache is keyed
                # on the repo only, so the job below reuses this model.
                holder.get_model(repo, mx.float16)
                log.info("model loaded in %.1f s", time.monotonic() - t0)
                self._loaded = True
        return self._mlx_whisper

    def transcribe(self, wav_path: str, on_progress: Callable[[float], None],
                   cancel: Optional[threading.Event] = None
                   ) -> Tuple[List[Segment], str, float]:
        """Return (segments, language, duration_seconds).

        Raises JobCancelled if cancel is set before starting, between decode
        windows (when the progress hook is installed) or by the time it ends.
        """
        def on_update(done: int, total: int) -> None:
            check_cancel(cancel)
            if total > 0:
                on_progress(min(100.0, 100.0 * done / total))

        with self._lock:  # the hook swaps a module global
            check_cancel(cancel)
            mlx_whisper = self._load()
            check_cancel(cancel)
            frames, duration = read_pcm16(wav_path)
            audio = pcm16_to_float32(frames)
            del frames
            with progress_hook(on_update):
                result = mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=self._settings.mlx_model,
                    language=None,
                    word_timestamps=False,
                    verbose=None,
                    condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
                )
            self._loaded = True
            check_cancel(cancel)
        segments: List[Segment] = [
            (float(seg["start"]), float(seg["end"]), seg["text"])
            for seg in result.get("segments") or []
        ]
        return segments, result.get("language") or "", duration
