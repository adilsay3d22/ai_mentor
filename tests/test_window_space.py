"""Window-space conversion, offline.

Window space is physical pixels relative to a window's top-left corner. It is
the space a remembered element position is stored in, because it is the one that
survives the window being dragged somewhere else.
"""

from __future__ import annotations

import pytest

from mentor.capture import coords
from mentor.models import Point, Rect

pytestmark = pytest.mark.offline

WINDOW = Rect(300, 200, 800, 600)


def test_window_space_is_a_translation_not_a_scale():
    element = Rect(340, 230, 60, 20)
    in_window = coords.rect_to_window_space(element, WINDOW)
    assert in_window == Rect(40, 30, 60, 20)
    assert coords.rect_from_window_space(in_window, WINDOW) == element


def test_an_anchor_survives_the_window_moving():
    """The whole point: same anchor, moved window, correctly moved highlight."""
    anchor = coords.rect_to_window_space(Rect(340, 230, 60, 20), WINDOW)
    moved_window = Rect(1000, 50, 800, 600)
    assert coords.rect_from_window_space(anchor, moved_window) == Rect(1040, 80, 60, 20)


def test_an_anchor_survives_a_move_onto_a_negative_monitor():
    anchor = coords.rect_to_window_space(Rect(340, 230, 60, 20), WINDOW)
    on_left_monitor = Rect(-1800, 100, 800, 600)
    assert coords.rect_from_window_space(anchor, on_left_monitor) == Rect(-1760, 130, 60, 20)


def test_resizing_keeps_top_left_anchored_elements_correct():
    """A menu stays put when a window is resized; the anchor must too."""
    anchor = coords.rect_to_window_space(Rect(324, 224, 60, 20), WINDOW)
    resized = Rect(300, 200, 1400, 900)
    assert coords.rect_from_window_space(anchor, resized) == Rect(324, 224, 60, 20)


def test_points_round_trip_through_window_space():
    for point in (Point(300, 200), Point(1099, 799), Point(700, 500)):
        assert (
            coords.point_from_window_space(coords.point_to_window_space(point, WINDOW), WINDOW)
            == point
        )
