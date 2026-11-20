"""Transcript text builders (stdlib only).

Segments are (start_seconds, end_seconds, text) tuples.
"""

import math
import re
from typing import Iterable, Sequence, Tuple

Segment = Tuple[float, float, str]

PARAGRAPH_GAP_SECONDS = 2.0
PARAGRAPH_SOFT_CHARS = 600
PARAGRAPH_HARD_CHARS = 1500
_SENTENCE_END = (".", "!", "?", "…", "。", "！", "？")
_WS = re.compile(r"\s+")


def format_timestamp(seconds: float) -> str:
    """Format seconds as SRT HH:MM:SS,mmm (rounded to the nearest ms)."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        seconds = 0.0
    total_ms = int(math.floor(seconds * 1000 + 0.5))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _clean(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def build_srt(segments: Iterable[Segment]) -> str:
    blocks = []
    for start, end, text in segments:
        text = _clean(text)
        if not text:
            continue
        if end < start:
            end = start
        n = len(blocks) + 1
        blocks.append(f"{n}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{text}\n")
    return "\n".join(blocks)


def build_paragraphs(segments: Sequence[Segment]) -> list:
    paragraphs = []
    current = []
    length = 0
    prev_end = None
    for start, end, text in segments:
        text = _clean(text)
        if not text:
            continue
        if current:
            gap = prev_end is not None and start - prev_end >= PARAGRAPH_GAP_SECONDS
            sentence_done = current[-1].endswith(_SENTENCE_END)
            if gap or length >= PARAGRAPH_HARD_CHARS or (length >= PARAGRAPH_SOFT_CHARS and sentence_done):
                paragraphs.append(" ".join(current))
                current, length = [], 0
        current.append(text)
        length += len(text) + 1
        prev_end = end
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def build_txt(segments: Sequence[Segment]) -> str:
    paragraphs = build_paragraphs(segments)
    return "\n\n".join(paragraphs) + "\n" if paragraphs else ""
