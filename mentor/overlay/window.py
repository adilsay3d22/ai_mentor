"""The transparent, always-on-top, click-through overlay.

One ``OverlayWindow`` per monitor, coordinated by ``Overlay``. One window per
monitor rather than one window spanning the virtual desktop, because a single
window has a single device pixel ratio -- whichever screen Qt decides it mostly
lives on -- and would therefore paint at the wrong scale on every other monitor.
Per-screen windows make mixed-DPI setups ordinary instead of special.

Invariant 2 of CLAUDE.md is the whole point of this file: if the user cannot
click through the overlay, Mentor is broken. The window-level
``WindowTransparentForInput`` flag is what delivers that; the
``WA_TransparentForMouseEvents`` widget attribute alone is not enough, and on
Windows the extended style bits are set directly as well so that the invariant
does not rest on one framework detail.
"""

from __future__ import annotations

import ctypes
import math
import sys
import time
from ctypes import wintypes

import structlog
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QScreen
from PySide6.QtWidgets import QApplication, QWidget

from mentor.capture import coords
from mentor.models import Element, Rect, ScreenMapping
from mentor.overlay import painters

log = structlog.get_logger(__name__)

#: Repaint rate for the ring pulse. Only the ring's own rectangle is repainted.
_PULSE_FPS = 60

_SPI_GETCLIENTAREAANIMATION = 0x1042
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080

#: ``SetWindowDisplayAffinity``: the window stays visible to the user but is
#: removed entirely from anything that captures the screen. Windows 10 2004+.
_WDA_EXCLUDEFROMCAPTURE = 0x00000011


def prefers_reduced_motion() -> bool:
    """True if the OS asks for reduced motion, in which case the ring does not pulse."""
    if sys.platform != "win32":
        return False
    enabled = ctypes.c_int(1)
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(
            _SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0
        )
    except OSError as exc:
        log.warning("reduced_motion_query_failed", error=str(exc))
        return False
    return bool(ok) and enabled.value == 0


def _enforce_click_through(widget: QWidget) -> None:
    """Belt and braces on the click-through invariant, on Windows.

    Qt already sets WS_EX_TRANSPARENT for ``WindowTransparentForInput``. Setting
    it again costs nothing and means a future change to the flags cannot silently
    turn the overlay into a wall.
    """
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        user32 = ctypes.windll.user32
        current = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd,
            _GWL_EXSTYLE,
            current | _WS_EX_TRANSPARENT | _WS_EX_LAYERED | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW,
        )
    except OSError as exc:
        log.error("click_through_enforcement_failed", error=str(exc))


def _exclude_from_capture(widget: QWidget) -> bool:
    """Make the overlay invisible to screen capture while leaving it visible to the user.

    Without this, Mentor grounds its own annotations. The overlay draws boxes and
    labels on top of the target window; the next capture includes them; OCR reads
    them back as if they were part of the application. Observed directly: a static
    Notepad went from 18 elements to 23 to 28 to 32 over four passes, each one
    feeding on the last.

    That matters well beyond a messy HUD. Invariant 3 makes the grounder the sole
    authority on what exists, so anything it hallucinates becomes something the
    planner is allowed to point at.

    ``WDA_EXCLUDEFROMCAPTURE`` needs Windows 10 2004 or later. On anything older
    the call fails and the caller is told, because the alternative -- hiding the
    overlay around every grab -- makes the ring flicker and is worth choosing
    deliberately rather than falling into.
    """
    if sys.platform != "win32":
        return False
    try:
        ok = ctypes.windll.user32.SetWindowDisplayAffinity(
            wintypes.HWND(int(widget.winId())), _WDA_EXCLUDEFROMCAPTURE
        )
    except OSError as exc:
        log.error("capture_exclusion_failed", error=str(exc))
        return False
    if not ok:
        log.error("capture_exclusion_refused", error=ctypes.get_last_error())
    return bool(ok)


class OverlayWindow(QWidget):
    """A click-through window covering exactly one monitor."""

    def __init__(self, mapping: ScreenMapping, screen: QScreen) -> None:
        super().__init__(None)
        self._mapping = mapping
        self._target: Rect | None = None
        self._accent: QColor = painters.ACCENT
        self._opacity: float = painters.RING_PULSE_MAX
        self._elements: list[Element] = []
        self._excluded_from_capture = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        self.setScreen(screen)
        self.setGeometry(mapping.logical.x, mapping.logical.y, mapping.logical.w, mapping.logical.h)

    @property
    def mapping(self) -> ScreenMapping:
        return self._mapping

    @property
    def excluded_from_capture(self) -> bool:
        """False means the grounder will see this overlay's own drawings."""
        return self._excluded_from_capture

    def show_overlay(self) -> None:
        """Show without stealing focus, re-assert click-through, then measure the screen.

        The measurement is the whole reason this window is created before any
        ring is drawn: it turns the estimated physical rectangle of this monitor
        into a measured one. See ``coords.measured_mapping``.
        """
        self.show()
        _enforce_click_through(self)
        self._excluded_from_capture = _exclude_from_capture(self)
        self._mapping = coords.measured_mapping(self._mapping, int(self.winId()))

    def set_target(self, target: Rect | None, accent: QColor | None = None) -> None:
        """Set the physical-screen rectangle to ring on this monitor, or None to clear."""
        if target == self._target and (accent is None or accent == self._accent):
            return
        previous = self._dirty_rect()
        self._target = target
        if accent is not None:
            self._accent = accent
        self.update(previous.united(self._dirty_rect()).toAlignedRect())

    def set_opacity(self, opacity: float) -> None:
        """Set the pulse opacity and repaint only the ring."""
        if opacity == self._opacity:
            return
        self._opacity = opacity
        if self._target is not None:
            self.update(self._dirty_rect().toAlignedRect())

    def set_elements(self, elements: list[Element]) -> None:
        """Replace the debug HUD's element boxes on this screen.

        A full repaint rather than a dirty rectangle: the element list changes
        wholesale once per grounding pass, not sixty times a second.
        """
        self._elements = elements
        self.update()

    def _local_target(self) -> QRectF | None:
        """The current target in this window's local logical coordinates."""
        if self._target is None:
            return None
        x, y, w, h = coords.rect_physical_to_local(self._target, self._mapping)
        return QRectF(x, y, w, h)

    def _dirty_rect(self) -> QRectF:
        local = self._local_target()
        if local is None:
            return QRectF()
        return painters.ring_dirty_rect(local)

    def paintEvent(self, event: QPaintEvent) -> None:
        local = self._local_target()
        if local is None and not self._elements:
            return

        painter = QPainter(self)
        # Debug boxes first, so the ring is never obscured by one.
        for element in self._elements:
            x, y, w, h = coords.rect_physical_to_local(element.bounds, self._mapping)
            painters.draw_element_box(
                painter,
                QRectF(x, y, w, h),
                element_id=element.id,
                label=element.label,
                source=element.source,
                confidence=element.confidence,
            )
        if local is not None:
            painters.draw_target_ring(painter, local, accent=self._accent, opacity=self._opacity)
        painter.end()


class Overlay:
    """The overlay as a whole: one window per monitor, one target at a time.

    Invariant 5 lives here -- ``show_ring`` replaces whatever was shown rather
    than adding to it, so a multi-step path cannot be rendered even by mistake.
    """

    def __init__(self, app: QApplication) -> None:
        self._app = app
        self._windows: list[OverlayWindow] = []
        self._target: Rect | None = None
        self._accent: QColor = painters.ACCENT
        self._elements: list[Element] = []
        self._reduced_motion = prefers_reduced_motion()

        self._pulse_timer = QTimer()
        self._pulse_timer.setInterval(max(1, 1000 // _PULSE_FPS))
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self._pulse_started_at = time.monotonic()

        self._build_windows()
        app.screenAdded.connect(self._on_screens_changed)
        app.screenRemoved.connect(self._on_screens_changed)

    # --- lifecycle -----------------------------------------------------------

    def _build_windows(self) -> None:
        for window in self._windows:
            window.close()
            window.deleteLater()
        self._windows = []

        for screen, mapping in zip(self._app.screens(), coords.qt_screen_mappings(), strict=True):
            window = OverlayWindow(mapping, screen)
            window.show_overlay()
            self._windows.append(window)

        mappings = self.mappings
        if self._windows and not all(w.excluded_from_capture for w in self._windows):
            log.error(
                "overlay_visible_to_capture",
                detail="the grounder will read Mentor's own drawings back as elements",
            )
        log.info(
            "overlay_windows_built",
            count=len(self._windows),
            virtual_desktop=coords.virtual_desktop_physical(mappings),
            screens=[
                {"name": m.name, "physical": m.physical, "logical": m.logical, "dpr": m.dpr}
                for m in mappings
            ],
        )
        self._apply_target()
        if self._elements:
            self.show_elements(self._elements)

    def _on_screens_changed(self, _screen: QScreen) -> None:
        log.info("screen_configuration_changed")
        self._build_windows()

    def close(self) -> None:
        self._pulse_timer.stop()
        for window in self._windows:
            window.close()
            window.deleteLater()
        self._windows = []

    # --- the one public verb -------------------------------------------------

    def show_ring(self, target: Rect, *, accent: QColor | None = None) -> None:
        """Ring one rectangle given in physical screen pixels, replacing any previous one."""
        self._target = target
        if accent is not None:
            self._accent = accent
        showing_on = [m.name for m in coords.screens_for_rect(target, self.mappings)]
        if not showing_on:
            log.warning("ring_target_off_screen", target=target)
        log.info("ring_shown", target=target, screens=showing_on)
        self._apply_target()
        if not self._reduced_motion and not self._pulse_timer.isActive():
            self._pulse_started_at = time.monotonic()
            self._pulse_timer.start()

    def clear(self) -> None:
        """Remove the ring. Debug element boxes are left alone."""
        self._pulse_timer.stop()
        self._target = None
        self._apply_target()

    def show_elements(self, elements: list[Element]) -> None:
        """Draw every grounded element's box. Debug HUD only.

        Not a violation of invariant 5: these are not steps, and this is only
        ever reachable under --debug. The single ring above is still the only
        thing that says "click here".
        """
        self._elements = elements
        for window in self._windows:
            window.set_elements(
                [e for e in elements if window.mapping.physical.intersects(e.bounds)]
            )
        log.info("hud_elements_shown", count=len(elements))

    def clear_elements(self) -> None:
        self._elements = []
        for window in self._windows:
            window.set_elements([])

    @property
    def mappings(self) -> list[ScreenMapping]:
        return [window.mapping for window in self._windows]

    # --- internals -----------------------------------------------------------

    def _apply_target(self) -> None:
        for window in self._windows:
            visible = (
                self._target
                if self._target is not None and window.mapping.physical.intersects(self._target)
                else None
            )
            window.set_target(visible, self._accent)

    def _tick_pulse(self) -> None:
        """Ease between the two pulse opacities on a ``RING_PULSE_SECONDS`` cycle."""
        elapsed = time.monotonic() - self._pulse_started_at
        phase = (elapsed % painters.RING_PULSE_SECONDS) / painters.RING_PULSE_SECONDS
        # A cosine gives a smooth there-and-back with no discontinuity at the wrap.
        eased = (1.0 - math.cos(phase * 2.0 * math.pi)) / 2.0
        opacity = painters.RING_PULSE_MIN + eased * (
            painters.RING_PULSE_MAX - painters.RING_PULSE_MIN
        )
        for window in self._windows:
            window.set_opacity(opacity)
