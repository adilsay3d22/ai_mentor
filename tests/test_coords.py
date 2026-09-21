"""Coordinate conversion, offline.

The mappings here are hand-built rather than read from a live desktop, which is
the point: these are the exact multi-monitor and scaling arrangements that make
highlights land in the wrong place, and none of them need a screen to test.

The scenario used throughout:

* ``DISPLAY1`` -- primary, 2560x1440 physical at 150%, so 1707x960 logical at (0, 0).
* ``DISPLAY2`` -- 1920x1080 physical at 100%, positioned to the **left** of the
  primary, so its physical origin is negative and Qt's logical origin for it is
  negative too, by a different amount.
"""

from __future__ import annotations

import pytest

from mentor.capture import coords
from mentor.models import Point, Rect, ScreenMapping

pytestmark = pytest.mark.offline

PRIMARY = ScreenMapping(
    name="\\\\.\\DISPLAY1",
    physical=Rect(0, 0, 2560, 1440),
    logical=Rect(0, 0, 1707, 960),
    dpr=1.5,
    is_primary=True,
)

LEFT = ScreenMapping(
    name="\\\\.\\DISPLAY2",
    physical=Rect(-1920, 0, 1920, 1080),
    logical=Rect(-1920, 0, 1920, 1080),
    dpr=1.0,
)

ALL = [PRIMARY, LEFT]


def test_unscaled_screen_is_identity_in_local_space():
    assert coords.physical_to_local(Point(-1920, 0), LEFT) == (0.0, 0.0)
    assert coords.physical_to_local(Point(-1000, 40), LEFT) == (920.0, 40.0)


def test_scaled_screen_divides_by_its_own_ratio():
    assert coords.physical_to_local(Point(0, 0), PRIMARY) == (0.0, 0.0)
    assert coords.physical_to_local(Point(300, 150), PRIMARY) == (200.0, 100.0)


def test_local_origin_is_the_screen_not_the_desktop():
    """A point at the left monitor's top-left is (0, 0) in that window, not -1920."""
    x, y = coords.physical_to_local(Point(-1920, 0), LEFT)
    assert (x, y) == (0.0, 0.0)


def test_round_trip_is_stable_on_a_fractional_ratio():
    for physical in (Point(0, 0), Point(1, 1), Point(2559, 1439), Point(300, 150)):
        local_x, local_y = coords.physical_to_local(physical, PRIMARY)
        assert coords.local_to_physical(local_x, local_y, PRIMARY) == physical


def test_round_trip_survives_negative_coordinates():
    for physical in (Point(-1920, 0), Point(-1, 1079), Point(-960, 540)):
        local_x, local_y = coords.physical_to_local(physical, LEFT)
        assert coords.local_to_physical(local_x, local_y, LEFT) == physical


def test_rect_conversion_scales_size_as_well_as_position():
    assert coords.rect_physical_to_local(Rect(300, 150, 240, 90), PRIMARY) == (
        200.0,
        100.0,
        160.0,
        60.0,
    )


def test_rect_conversion_does_not_round_to_whole_logical_pixels():
    """An odd physical width on a 1.5x screen is genuinely fractional. Keep it."""
    _, _, w, _ = coords.rect_physical_to_local(Rect(0, 0, 241, 64), PRIMARY)
    assert w == pytest.approx(160.6667, abs=1e-3)


def test_global_logical_differs_from_local_when_qt_origin_differs():
    """Qt lays screens out in its own space; local and global are not the same number."""
    local = coords.physical_to_local(Point(-1000, 40), LEFT)
    glob = coords.physical_to_global_logical(Point(-1000, 40), LEFT)
    assert local == (920.0, 40.0)
    assert glob == (-1000.0, 40.0)


def test_screen_for_point_picks_the_right_monitor():
    assert coords.screen_for_point(Point(10, 10), ALL) is PRIMARY
    assert coords.screen_for_point(Point(-10, 10), ALL) is LEFT
    assert coords.screen_for_point(Point(-5000, 10), ALL) is None


def test_screens_for_rect_returns_both_when_straddling():
    straddling = Rect(-100, 100, 400, 50)
    assert set(coords.screens_for_rect(straddling, ALL)) == {PRIMARY, LEFT}
    assert coords.screens_for_rect(Rect(100, 100, 50, 50), ALL) == [PRIMARY]
    assert coords.screens_for_rect(Rect(-8000, 0, 10, 10), ALL) == []


def test_virtual_desktop_spans_negative_origin():
    assert coords.virtual_desktop_physical(ALL) == Rect(-1920, 0, 4480, 1440)


def test_virtual_desktop_of_nothing_is_empty():
    assert coords.virtual_desktop_physical([]) == Rect(0, 0, 0, 0)


def test_a_ring_straddling_two_monitors_lands_at_the_seam():
    """The same physical point converts to each screen's own local space correctly."""
    seam = Point(-1, 500)
    assert coords.physical_to_local(seam, LEFT) == (1919.0, 500.0)
    just_right = Point(0, 500)
    assert coords.physical_to_local(just_right, PRIMARY) == (0.0, pytest.approx(333.3333, abs=1e-3))


# --- Physical rectangle measurement ------------------------------------------
# Qt reports monitors by friendly EDID name, not GDI device name, so screens
# cannot be matched to Win32 monitors by name. The overlay measures each screen
# from its own window instead. These cover the arithmetic around that.


def test_expected_physical_size_uses_the_screens_own_ratio():
    # 1707 * 1.5 is exactly 2560.5, and round() breaks that tie to even.
    assert coords.expected_physical_size(Rect(0, 0, 1707, 960), 1.5) == (2560, 1440)
    assert coords.expected_physical_size(Rect(0, 0, 1920, 1080), 1.0) == (1920, 1080)


def test_measurement_is_plausible_absorbs_qt_rounding():
    """2560/1.5 rounds to 1707, and 1707*1.5 is 2560.5. A pixel of slack is required."""
    logical = Rect(0, 0, 1707, 960)
    assert coords.measurement_is_plausible(logical, 1.5, Rect(0, 0, 2560, 1440))
    assert coords.measurement_is_plausible(logical, 1.5, Rect(-2560, 300, 2560, 1440))


def test_measurement_is_rejected_when_the_size_is_wrong():
    """A window that failed to cover its screen must not be trusted for the origin."""
    logical = Rect(0, 0, 1707, 960)
    assert not coords.measurement_is_plausible(logical, 1.5, Rect(0, 0, 1707, 960))
    assert not coords.measurement_is_plausible(logical, 1.5, Rect(0, 0, 2560, 1400))


def test_measurement_plausibility_ignores_position():
    """Position is the unknown being measured; only the size is checked."""
    logical = Rect(0, 0, 1920, 1080)
    assert coords.measurement_is_plausible(logical, 1.0, Rect(-5000, -3000, 1920, 1080))


def test_estimated_physical_is_the_pre_measurement_guess():
    assert coords.estimated_physical(Rect(0, 0, 1920, 1080), 1.0) == Rect(0, 0, 1920, 1080)
    assert coords.estimated_physical(Rect(-1920, 0, 1920, 1080), 1.0) == Rect(-1920, 0, 1920, 1080)


def test_provisional_mapping_carries_logical_geometry_through():
    mapping = coords.provisional_mapping(
        "LG ULTRAGEAR", Rect(0, 0, 1707, 960), 1.5, is_primary=True
    )
    assert mapping.logical == Rect(0, 0, 1707, 960)
    assert mapping.dpr == 1.5
    assert mapping.is_primary
    assert mapping.physical.w == 2560


class _FakeWindowRect:
    """Stands in for GetWindowRect so measurement can be tested with no screen."""

    def __init__(self, rect: Rect | None) -> None:
        self.rect = rect

    def __call__(self, _hwnd: int) -> Rect | None:
        return self.rect


def test_measured_mapping_replaces_the_estimate(monkeypatch):
    provisional = coords.provisional_mapping(
        "DISPLAY2", Rect(-1920, 0, 1920, 1080), 1.0, is_primary=False
    )
    monkeypatch.setattr(
        coords, "window_physical_rect", _FakeWindowRect(Rect(-2000, 40, 1920, 1080))
    )
    assert coords.measured_mapping(provisional, 1).physical == Rect(-2000, 40, 1920, 1080)


def test_measured_mapping_keeps_the_estimate_when_measurement_is_unavailable(monkeypatch):
    provisional = coords.provisional_mapping(
        "DISPLAY1", Rect(0, 0, 1920, 1080), 1.0, is_primary=True
    )
    monkeypatch.setattr(coords, "window_physical_rect", _FakeWindowRect(None))
    assert coords.measured_mapping(provisional, 1) == provisional


def test_measured_mapping_keeps_the_estimate_when_measurement_is_implausible(monkeypatch):
    """An honest estimate beats a measurement that would put every ring somewhere wrong."""
    provisional = coords.provisional_mapping(
        "DISPLAY1", Rect(0, 0, 1707, 960), 1.5, is_primary=True
    )
    monkeypatch.setattr(coords, "window_physical_rect", _FakeWindowRect(Rect(0, 0, 800, 600)))
    assert coords.measured_mapping(provisional, 1) == provisional
