"""UIA control mapping and rectangle filtering. Pure logic, no COM, offline.

Walking a real control tree needs a real desktop, so that part is verified by
hand (see `phases.md`). What is testable here is every decision the walk makes
about a control once it has read it, and those are the decisions that determine
whether the planner is offered a sensible list.
"""

from __future__ import annotations

import pytest

from mentor.grounding import uia
from mentor.grounding.base import KINDS
from mentor.models import Rect

pytestmark = pytest.mark.offline

WINDOW = Rect(0, 26, 1920, 1054)


class FakeRect:
    """Stands in for a UIA BoundingRectangle."""

    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


@pytest.mark.parametrize(
    ("control_type", "expected"),
    [
        ("ButtonControl", "button"),
        ("MenuItemControl", "menu"),
        ("TabItemControl", "tab"),
        ("EditControl", "field"),
        ("ImageControl", "icon"),
        ("TextControl", "text"),
        ("PaneControl", "panel"),
    ],
)
def test_known_control_types_map_to_a_kind(control_type, expected):
    assert uia.control_kind(control_type) == expected


def test_unknown_control_types_are_skipped_not_guessed():
    """An element with the wrong kind is worse than one the planner never sees."""
    assert uia.control_kind("ThumbControl") is None
    assert uia.control_kind("CustomControl") is None
    assert uia.control_kind("") is None


def test_every_mapped_kind_is_one_the_planner_understands():
    assert set(uia.CONTROL_TYPE_KINDS.values()) <= KINDS


def test_panels_are_mapped_but_never_offered_as_targets():
    """Worth descending into, not worth pointing at: nobody clicks a pane."""
    assert uia.control_kind("PaneControl") in uia.NON_TARGET_KINDS


def test_usable_rect_converts_to_physical_pixels():
    assert uia.usable_rect(FakeRect(643, 725, 698, 746), WINDOW) == Rect(643, 725, 55, 21)


def test_empty_rectangles_are_rejected():
    assert uia.usable_rect(FakeRect(100, 100, 100, 200), WINDOW) is None
    assert uia.usable_rect(FakeRect(100, 100, 200, 100), WINDOW) is None
    assert uia.usable_rect(FakeRect(200, 200, 100, 100), WINDOW) is None


def test_controls_outside_the_window_are_rejected():
    """UIA reports controls that are scrolled out of view or on another monitor."""
    assert uia.usable_rect(FakeRect(5000, 5000, 5100, 5100), WINDOW) is None


def test_a_control_straddling_the_window_edge_is_kept():
    assert uia.usable_rect(FakeRect(-20, 100, 40, 140), WINDOW) == Rect(-20, 100, 60, 40)


def test_a_malformed_rectangle_is_rejected_rather_than_raising():
    assert uia.usable_rect(object(), WINDOW) is None


def test_uwp_content_class_is_the_documented_one():
    """UWP content lives in a child window of a different process from the frame."""
    assert uia.UWP_CONTENT_CLASS == "Windows.UI.Core.CoreWindow"


def test_budgets_are_set_to_something_defensible():
    """These exist so a pathological tree cannot stall a grounding pass."""
    assert 0 < uia.DEFAULT_TIME_BUDGET <= 1.0
    assert 0 < uia.DEFAULT_MAX_ELEMENTS <= 2000
    assert 0 < uia.DEFAULT_MAX_DEPTH <= 30
