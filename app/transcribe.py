"""Transcription backends behind one interface, chosen by WHISPER_BACKEND.

faster-whisper (default, Docker/CPU) lives here; the MLX backend (Apple Silicon)
is in transcribe_mlx.py. Either model loads lazily on the first job and is reused.
"""

import logging
import os
import threading
import time
from typing import Callable, List, Optional, Protocol, Tuple

from .config import Settings
from .procs import check_cancel
from .textfmt import Segment

log = logging.getLogger(__name__)


class Transcriber(Protocol):
    @property
    def loaded(self) -> bool: ...

    def transcribe(self, wav_path: str, on_progress: Callable[[float], None],
                   cancel: Optional[threading.Event] = None
                   ) -> Tuple[List[Segment], str, float]: ...


def make_transcriber(settings: Settings) -> Transcriber:
    """The backend named by settings.whisper_backend. Imports no model library."""
    if settings.whisper_backend == "mlx":
        from .transcribe_mlx import MlxTranscriber

        return MlxTranscriber(settings)
    return FasterWhisperTranscriber(settings)


class FasterWhisperTranscriber:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _get_model(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                s = self._settings
                t0 = time.monotonic()
                log.info("loading model %s (%s, %s)", s.model_name, s.device, s.compute_type)
                self._model = WhisperModel(
                    s.model_name,
                    device=s.device,
                    compute_type=s.compute_type,
                    cpu_threads=s.cpu_threads,
                    download_root=os.path.join(s.hf_home, "faster-whisper"),
                )
                log.info("model loaded in %.1f s", time.monotonic() - t0)
            return self._model

    def transcribe(self, wav_path: str, on_progress: Callable[[float], None],
                   cancel: Optional[threading.Event] = None
                   ) -> Tuple[List[Segment], str, float]:
        """Return (segments, language, duration_seconds).

        Raises JobCancelled if cancel is set before starting or between segments.
        """
        check_cancel(cancel)
        model = self._get_model()
        check_cancel(cancel)
        segments_iter, info = model.transcribe(
            wav_path, beam_size=5, vad_filter=True, language=None,
        )
        duration = float(info.duration or 0.0)
        segments: List[Segment] = []
        try:
            for seg in segments_iter:
                check_cancel(cancel)
                segments.append((float(seg.start), float(seg.end), seg.text))
                if duration > 0:
                    on_progress(min(100.0, 100.0 * float(seg.end) / duration))
        finally:
            close = getattr(segments_iter, "close", None)
            if close is not None:
                close()
        return segments, info.language or "", duration
