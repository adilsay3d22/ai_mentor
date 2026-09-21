"""Renderers for everything the overlay draws.

Painters receive geometry already converted to a screen's **local logical**
coordinates and never touch physical pixels themselves. Conversion happens in
``mentor.capture.coords``; this module is pure drawing.

The numbers below are the visual specification from design.md. They are logical
pixels, not physical ones, so the ring keeps the same apparent weight on a 150%
display as on a 100% one.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen

#: Primary accent.
ACCENT: Final[QColor] = QColor("#7F77DD")
#: Used instead when the planner's confidence is below ``config.UNSURE_CONFIDENCE``.
ACCENT_UNSURE: Final[QColor] = QColor("#EF9F27")

RING_STROKE: Final[float] = 3.0
RING_PADDING: Final[float] = 6.0
RING_RADIUS: Final[float] = 8.0

#: The pulse runs between these opacities over ``RING_PULSE_SECONDS``.
RING_PULSE_MIN: Final[float] = 0.85
RING_PULSE_MAX: Final[float] = 1.0
RING_PULSE_SECONDS: Final[float] = 1.5


def ring_geometry(target: QRectF) -> QRectF:
    """The ring's path rectangle: the target padded by ``RING_PADDING`` on every side."""
    return target.adjusted(-RING_PADDING, -RING_PADDING, RING_PADDING, RING_PADDING)


def ring_dirty_rect(target: QRectF) -> QRectF:
    """The region a ring on ``target`` actually paints into.

    The stroke straddles the path, so it reaches half a stroke width beyond the
    ring geometry. One extra pixel covers antialiasing. Repainting only this much
    is what keeps a pulsing full-screen translucent window cheap.
    """
    bleed = RING_STROKE / 2.0 + 1.0
    return ring_geometry(target).adjusted(-bleed, -bleed, bleed, bleed)


def draw_target_ring(
    painter: QPainter,
    target: QRectF,
    *,
    accent: QColor = ACCENT,
    opacity: float = RING_PULSE_MAX,
) -> None:
    """Draw the target ring around ``target``, in the painter's logical coordinates."""
    colour = QColor(accent)
    colour.setAlphaF(max(0.0, min(1.0, opacity)))

    pen = QPen(colour, RING_STROKE)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(ring_geometry(target), RING_RADIUS, RING_RADIUS)
    painter.restore()


# --- Debug HUD ---------------------------------------------------------------
# These are development affordances, not part of the product's visual language.
# They are allowed to be loud, because their whole job is to make a wrong box
# obvious at a glance.

#: One colour per grounding provider, so a misplaced box names its own culprit.
SOURCE_COLOURS: Final[dict[str, QColor]] = {
    "ocr": QColor("#3BB3A0"),
    "uia": QColor("#5B8DEF"),
    "vision": QColor("#D96BC8"),
    "cache": QColor("#9AA0A6"),
}
UNKNOWN_SOURCE_COLOUR: Final[QColor] = QColor("#EF6060")

HUD_STROKE: Final[float] = 1.0
HUD_LABEL_SIZE: Final[int] = 9


def source_colour(source: str) -> QColor:
    return SOURCE_COLOURS.get(source, UNKNOWN_SOURCE_COLOUR)


def draw_element_box(
    painter: QPainter,
    box: QRectF,
    *,
    element_id: str,
    label: str,
    source: str,
    confidence: float,
) -> None:
    """Draw one grounded element for the debug HUD, in logical coordinates.

    The caption goes above the box, or inside it when the box is at the very top
    of the screen, so a menu bar element does not lose its label off-screen.
    """
    colour = source_colour(source)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    outline = QColor(colour)
    outline.setAlphaF(0.9)
    painter.setPen(QPen(outline, HUD_STROKE))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(box)

    text = f"{element_id} {label}" if label else element_id
    if confidence < 1.0:
        text = f"{text} ({confidence:.2f})"

    font = painter.font()
    font.setPixelSize(HUD_LABEL_SIZE)
    painter.setFont(font)
    metrics = painter.fontMetrics()
    text_width = metrics.horizontalAdvance(text) + 6
    text_height = metrics.height() + 2

    top = box.top() - text_height
    if top < 0:
        top = box.top()
    backdrop = QRectF(box.left(), top, text_width, text_height)

    fill = QColor(colour)
    fill.setAlphaF(0.85)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    painter.drawRect(backdrop)

    painter.setPen(QPen(QColor("#0E0E10")))
    painter.drawText(backdrop.adjusted(3, 1, 0, 0), int(Qt.AlignmentFlag.AlignLeft), text)
    painter.restore()
