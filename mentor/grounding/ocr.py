"""Text grounding via RapidOCR.

OCR is the provider that works on anything with words on it, which is most of
what Mentor needs to point at. It cannot tell a button from a caption, so every
element it produces has ``kind="text"``; deciding what a thing *is* is the
planner's job, and pretending otherwise here would be inventing information.

The interesting problem is granularity. RapidOCR detects a *line* -- a menu bar
comes back as the single string "File Edit View Filter Help", which is useless
for pointing at one menu. Asking for word boxes instead splits "Gaussian Blur"
into two elements, which is equally useless. So words are regrouped by the gap
between them: the space inside a label is small, the space between two separate
controls is large. See :func:`group_words`.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from mentor.grounding.base import frame_rect_to_screen, is_meaningful_label, make_element
from mentor.models import AppContext, Element, Frame, Rect

log = structlog.get_logger(__name__)

#: A gap wider than this fraction of the line's height starts a new element.
#:
#: Measured against the real fixtures in ``tests/fixtures/screens/`` rather than
#: synthetic text. Anything from 0.35 to 0.7 gives identical results on Notepad
#: and Calculator; at 0.9 and above the File Explorer toolbar starts merging
#: "View" into a neighbouring label, and at 0.35 Explorer over-splits by three.
#: 0.55 is the middle of the flat region, not a guess.
DEFAULT_GAP_RATIO = 0.55

#: Words below this are dropped rather than shown to the planner as real options.
DEFAULT_MIN_CONFIDENCE = 0.5


def quad_to_rect(quad: Any) -> Rect:
    """Collapse RapidOCR's four-corner polygon to an axis-aligned rectangle.

    Screen text is never rotated, so the bounding box loses nothing, and every
    coordinate downstream is an axis-aligned Rect.
    """
    xs = [float(point[0]) for point in quad]
    ys = [float(point[1]) for point in quad]
    left, top = round(min(xs)), round(min(ys))
    return Rect(left, top, round(max(xs)) - left, round(max(ys)) - top)


def group_words(
    words: list[tuple[str, float, Rect]], gap_ratio: float = DEFAULT_GAP_RATIO
) -> list[tuple[str, float, Rect]]:
    """Merge words of one line into the labels a person would call separate things.

    Words are joined while the horizontal gap between them stays below
    ``gap_ratio`` times the line height. "Gaussian Blur" survives as one label;
    "File    Edit" becomes two.

    Confidence of a group is the *lowest* of its words: a label is only as
    trustworthy as its least certain part.
    """
    if not words:
        return []

    ordered = sorted(words, key=lambda w: w[2].x)
    line_height = max(w[2].h for w in ordered) or 1
    threshold = line_height * gap_ratio

    groups: list[list[tuple[str, float, Rect]]] = [[ordered[0]]]
    for word in ordered[1:]:
        previous = groups[-1][-1][2]
        if word[2].x - previous.right > threshold:
            groups.append([word])
        else:
            groups[-1].append(word)

    merged: list[tuple[str, float, Rect]] = []
    for group in groups:
        text = " ".join(w[0] for w in group).strip()
        if not text:
            continue
        left = min(w[2].x for w in group)
        top = min(w[2].y for w in group)
        right = max(w[2].right for w in group)
        bottom = max(w[2].bottom for w in group)
        merged.append((text, min(w[1] for w in group), Rect(left, top, right - left, bottom - top)))
    return merged


class OcrGrounder:
    """RapidOCR behind the Grounder protocol."""

    source = "ocr"

    def __init__(
        self,
        gap_ratio: float = DEFAULT_GAP_RATIO,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ) -> None:
        self.gap_ratio = gap_ratio
        self.min_confidence = min_confidence
        self._engine: Any | None = None

    def _get_engine(self) -> Any:
        """Build the engine on first use.

        Deferred because importing RapidOCR and loading three ONNX models costs
        about a third of a second, and NFR6 gives the whole cold start two.
        """
        if self._engine is None:
            from rapidocr import RapidOCR

            started = time.perf_counter()
            self._engine = RapidOCR()
            log.info("ocr_engine_ready", seconds=round(time.perf_counter() - started, 3))
        return self._engine

    def ground(self, frame: Frame, context: AppContext | None = None) -> list[Element]:
        """``context`` is unused: OCR needs only pixels."""
        engine = self._get_engine()
        started = time.perf_counter()
        # use_cls is orientation correction for photographed text. Screen pixels
        # are never rotated, and it costs a model pass.
        result = engine(frame.image, return_word_box=True, use_cls=False)
        elapsed = time.perf_counter() - started

        elements: list[Element] = []
        dropped = 0
        for line in result.word_results or ():
            words = [
                (text, float(score), quad_to_rect(quad))
                for text, score, quad in line
                if text and text.strip()
            ]
            for text, confidence, rect in group_words(words, self.gap_ratio):
                if confidence < self.min_confidence:
                    continue
                if not is_meaningful_label(text):
                    dropped += 1
                    continue
                elements.append(
                    make_element(
                        label=text,
                        kind="text",
                        bounds=frame_rect_to_screen(rect, frame),
                        confidence=confidence,
                        source=self.source,
                    )
                )

        log.info(
            "ocr_grounded",
            seconds=round(elapsed, 3),
            region=frame.bounds,
            lines=len(result.word_results or ()),
            elements=len(elements),
            dropped_as_glyphs=dropped,
        )
        return elements
