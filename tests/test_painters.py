"""Ring geometry, offline.

These check the arithmetic in ``painters`` rather than the appearance. Whether
the ring *looks* right is a manual check in phases.md; whether it is 6px larger
than its target on every side is not.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRectF

from mentor.overlay import painters

pytestmark = pytest.mark.offline


def test_ring_pads_the_target_on_every_side():
    ring = painters.ring_geometry(QRectF(100.0, 100.0, 40.0, 20.0))
    assert ring.x() == 100.0 - painters.RING_PADDING
    assert ring.y() == 100.0 - painters.RING_PADDING
    assert ring.width() == 40.0 + 2 * painters.RING_PADDING
    assert ring.height() == 20.0 + 2 * painters.RING_PADDING


def test_dirty_rect_covers_the_stroke_and_its_antialiasing():
    target = QRectF(100.0, 100.0, 40.0, 20.0)
    dirty = painters.ring_dirty_rect(target)
    assert dirty.contains(painters.ring_geometry(target))
    bleed = painters.RING_STROKE / 2.0 + 1.0
    assert dirty.x() == pytest.approx(100.0 - painters.RING_PADDING - bleed)
    assert dirty.width() == pytest.approx(40.0 + 2 * (painters.RING_PADDING + bleed))


def test_pulse_stays_within_the_specified_opacity_band():
    assert 0.0 < painters.RING_PULSE_MIN < painters.RING_PULSE_MAX <= 1.0
