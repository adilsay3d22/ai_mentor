"""Coordinate space conversion. The only module that does arithmetic across spaces.

Three spaces exist (architecture.md):

1. **Physical screen space** -- raw device pixels across the whole virtual desktop,
   origin at the primary monitor's top-left, possibly negative. Mentor's internal
   standard; everything in ``models.py`` is in this space.
2. **Window space** -- physical pixels relative to a target window's top-left.
   What survives a window being moved, and what the application map stores.
3. **Qt logical space** -- what Qt reports for widget geometry, which is physical
   divided by the device pixel ratio of whichever screen the widget is on.

The conversion is per-screen and cannot be a single global divide, because Qt
lays monitors out in its own logical coordinate system whose origins do not
match the physical ones once any screen is scaled.

Physical monitor rectangles come from Win32 rather than Qt, because Qt only
exposes logical geometry. The process must already be per-monitor-v2 DPI aware
by the time this runs (see ``mentor.__main__.set_dpi_awareness``), otherwise
Windows lies to us about every rectangle.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

import structlog

from mentor.models import Point, Rect, ScreenMapping

log = structlog.get_logger(__name__)


# --- Pure conversion math ----------------------------------------------------
# Everything below takes a ScreenMapping and touches neither Qt nor Win32, so it
# is fully testable offline against hand-built mappings.


def physical_to_local(point: Point, mapping: ScreenMapping) -> tuple[float, float]:
    """Physical screen pixel -> logical coordinates local to that screen's window.

    This is what a painter wants: the overlay window covering ``mapping`` has its
    own origin at the screen's top-left, so (0, 0) here is that corner.
    """
    return (
        (point.x - mapping.physical.x) / mapping.dpr,
        (point.y - mapping.physical.y) / mapping.dpr,
    )


def local_to_physical(x: float, y: float, mapping: ScreenMapping) -> Point:
    """Inverse of :func:`physical_to_local`."""
    return Point(
        round(mapping.physical.x + x * mapping.dpr),
        round(mapping.physical.y + y * mapping.dpr),
    )


def rect_physical_to_local(rect: Rect, mapping: ScreenMapping) -> tuple[float, float, float, float]:
    """Physical rect -> (x, y, w, h) in logical coordinates local to that screen.

    Returned as floats: a rect that is an odd number of physical pixels wide on a
    1.5x screen genuinely does not land on a whole logical pixel, and rounding it
    here would move the ring by up to a pixel for no reason.
    """
    x, y = physical_to_local(Point(rect.x, rect.y), mapping)
    return (x, y, rect.w / mapping.dpr, rect.h / mapping.dpr)


def point_to_window_space(point: Point, window: Rect) -> Point:
    """Physical screen pixel -> pixels relative to a window's top-left corner.

    The same physical scale, only a different origin: this is what makes a
    remembered element position survive the window being dragged somewhere else.
    """
    return Point(point.x - window.x, point.y - window.y)


def point_from_window_space(point: Point, window: Rect) -> Point:
    """Inverse of :func:`point_to_window_space`."""
    return Point(point.x + window.x, point.y + window.y)


def rect_to_window_space(rect: Rect, window: Rect) -> Rect:
    """Physical rect -> the same rect relative to a window's top-left corner."""
    return Rect(rect.x - window.x, rect.y - window.y, rect.w, rect.h)


def rect_from_window_space(rect: Rect, window: Rect) -> Rect:
    """Window-space rect -> physical screen pixels, for a window now at ``window``.

    Anchored to the top-left, which is right for menus, toolbars and anything
    else that stays put when a window is resized. It is wrong for controls
    anchored to a window's right or bottom edge, which drift under a resize;
    re-grounding after the resize is what corrects that, not cleverer maths here.
    """
    return Rect(rect.x + window.x, rect.y + window.y, rect.w, rect.h)


def physical_to_global_logical(point: Point, mapping: ScreenMapping) -> tuple[float, float]:
    """Physical screen pixel -> Qt's *global* logical coordinates.

    Used for positioning a window, since ``QWidget.setGeometry`` speaks global
    logical space.
    """
    local_x, local_y = physical_to_local(point, mapping)
    return (mapping.logical.x + local_x, mapping.logical.y + local_y)


def screen_for_point(point: Point, mappings: list[ScreenMapping]) -> ScreenMapping | None:
    """The screen containing ``point``, or None if it falls in a gap between monitors."""
    for mapping in mappings:
        if mapping.physical.contains(point):
            return mapping
    return None


def screens_for_rect(rect: Rect, mappings: list[ScreenMapping]) -> list[ScreenMapping]:
    """Every screen the rect touches. A ring straddling two monitors must paint on both."""
    return [m for m in mappings if m.physical.intersects(rect)]


def virtual_desktop_physical(mappings: list[ScreenMapping]) -> Rect:
    """The bounding box of every monitor, in physical pixels. Origin may be negative."""
    if not mappings:
        return Rect(0, 0, 0, 0)
    left = min(m.physical.left for m in mappings)
    top = min(m.physical.top for m in mappings)
    right = max(m.physical.right for m in mappings)
    bottom = max(m.physical.bottom for m in mappings)
    return Rect(left, top, right - left, bottom - top)


# --- Measuring a screen's physical rectangle ---------------------------------
#
# Qt only ever reports logical geometry, so the physical origin of each monitor
# has to come from somewhere else. Matching Qt's screens against Win32's monitor
# list by name does not work: Qt 6 reports the monitor's friendly EDID name
# ("LG ULTRAGEAR") while Win32 reports the GDI device name, and friendly names
# are not even unique when two identical monitors are attached.
#
# So instead of matching, we measure. An overlay window is positioned to cover
# exactly one screen using Qt's logical geometry, and GetWindowRect then reports
# that same window in physical pixels, because the process is per-monitor-v2 DPI
# aware. The window's physical rectangle *is* the screen's physical rectangle.
# No name matching, no heuristics, and the result can be checked against the
# logical size for sanity.
#
# GetWindowRect rather than DWMWA_EXTENDED_FRAME_BOUNDS here, deliberately: the
# invisible drop-shadow border that makes GetWindowRect wrong for ordinary
# windows does not exist on a frameless, shadow-suppressed tool window. Target
# windows in phase 2 are a different matter and will need the DWM call.

#: How far a measured physical size may differ from logical * dpr before the
#: measurement is treated as untrustworthy. Qt rounds logical sizes, so a pixel
#: or two of disagreement is expected and harmless.
MEASUREMENT_TOLERANCE_PX = 2


def expected_physical_size(logical: Rect, dpr: float) -> tuple[int, int]:
    """The physical size a screen of this logical size and ratio should have."""
    return (round(logical.w * dpr), round(logical.h * dpr))


def measurement_is_plausible(
    logical: Rect, dpr: float, measured: Rect, tolerance: int = MEASUREMENT_TOLERANCE_PX
) -> bool:
    """True if a measured physical rectangle is the right size for this screen.

    Position is not checked -- that is the unknown we are measuring. Size is,
    because a window that failed to cover its screen would otherwise hand us a
    plausible-looking origin that is silently wrong.
    """
    want_w, want_h = expected_physical_size(logical, dpr)
    return abs(measured.w - want_w) <= tolerance and abs(measured.h - want_h) <= tolerance


def estimated_physical(logical: Rect, dpr: float) -> Rect:
    """The physical rectangle to assume before a window has been measured.

    Correct for a single monitor at the origin, and a reasonable starting point
    otherwise. Always replaced by a measurement once a window exists.
    """
    return Rect(
        round(logical.x * dpr), round(logical.y * dpr), *expected_physical_size(logical, dpr)
    )


def window_physical_rect(hwnd: int) -> Rect | None:
    """The physical screen rectangle of one of our own frameless windows."""
    if sys.platform != "win32":
        return None
    rect = wintypes.RECT()
    try:
        ok = ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    except OSError as exc:
        log.warning("window_rect_failed", hwnd=hwnd, error=str(exc))
        return None
    if not ok:
        log.warning("window_rect_returned_false", hwnd=hwnd)
        return None
    return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def provisional_mapping(name: str, logical: Rect, dpr: float, *, is_primary: bool) -> ScreenMapping:
    """A mapping built from Qt alone, with the physical rectangle estimated."""
    return ScreenMapping(
        name=name,
        physical=estimated_physical(logical, dpr),
        logical=logical,
        dpr=dpr,
        is_primary=is_primary,
    )


def measured_mapping(provisional: ScreenMapping, hwnd: int) -> ScreenMapping:
    """Replace a provisional mapping's estimate with the measured physical rectangle.

    Returns the provisional mapping unchanged if the measurement is unavailable
    or implausible, having logged why -- a wrong measurement would put every ring
    in the wrong place, so an honest estimate is the safer failure.
    """
    measured = window_physical_rect(hwnd)
    if measured is None:
        log.warning("screen_physical_bounds_unmeasured", screen=provisional.name)
        return provisional

    if not measurement_is_plausible(provisional.logical, provisional.dpr, measured):
        log.error(
            "screen_physical_bounds_implausible",
            screen=provisional.name,
            measured=measured,
            expected_size=expected_physical_size(provisional.logical, provisional.dpr),
            keeping=provisional.physical,
        )
        return provisional

    if measured != provisional.physical:
        log.info(
            "screen_physical_bounds_corrected",
            screen=provisional.name,
            estimated=provisional.physical,
            measured=measured,
        )
    return ScreenMapping(
        name=provisional.name,
        physical=measured,
        logical=provisional.logical,
        dpr=provisional.dpr,
        is_primary=provisional.is_primary,
    )


def qt_screen_mappings() -> list[ScreenMapping]:
    """A provisional mapping for every connected monitor, from Qt alone.

    The overlay replaces each of these with a measured one as soon as it has a
    window on that screen.
    """
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.instance() is None:
        raise RuntimeError("qt_screen_mappings() needs a QGuiApplication to exist first")

    primary = QGuiApplication.primaryScreen()
    mappings: list[ScreenMapping] = []
    for screen in QGuiApplication.screens():
        geom = screen.geometry()
        mappings.append(
            provisional_mapping(
                screen.name(),
                Rect(geom.x(), geom.y(), geom.width(), geom.height()),
                float(screen.devicePixelRatio()) or 1.0,
                is_primary=screen is primary,
            )
        )
    return mappings
