"""Keeps the overlay glued to a point inside the target window.

The tracker holds an anchor in **window space** -- physical pixels relative to
the target window's top-left -- and converts it back to physical screen pixels
every tick. That is what makes the ring survive the window being dragged,
maximised, or moved to another monitor: the anchor never changes, only the
window's position does.

Polling rather than ``SetWinEventHook``, deliberately. A ``GetWindowRect`` costs
microseconds, the tracker only runs while a session is active (NFR3 asks for a
stopped loop when idle, not a throttled one), and a timer is far easier to reason
about than a ctypes callback delivered into the Qt event loop. If polling ever
fails to keep up with a drag, the hook is the upgrade -- but measure first.
"""

from __future__ import annotations

import structlog
from PySide6.QtCore import QObject, QTimer, Signal

from mentor import config
from mentor.capture import coords, windows
from mentor.models import AppContext, Rect

log = structlog.get_logger(__name__)

#: NFR2 wants no visible lag while dragging. 60Hz gives a frame of headroom over
#: the 30fps floor without costing anything measurable.
DEFAULT_POLL_HZ = 60


class WindowTracker(QObject):
    """Follows one window and reports where its anchored rectangle now is.

    Signals:
        moved: the anchor's new position in physical screen pixels.
        hidden: the ring cannot be shown right now, with a reason. Recoverable --
            the window may be restored or leave fullscreen.
        lost: the window is gone for good, with a reason. The tracker stops.
    """

    moved = Signal(object)
    hidden = Signal(str)
    lost = Signal(str)

    def __init__(self, poll_hz: int = DEFAULT_POLL_HZ, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, 1000 // poll_hz))
        self._timer.timeout.connect(self._tick)

        self._hwnd: int | None = None
        self._anchor: Rect | None = None
        self._last_emitted: Rect | None = None
        self._last_hidden_reason: str | None = None

    # --- lifecycle -----------------------------------------------------------

    def track(self, context: AppContext, anchor_window_space: Rect) -> bool:
        """Start following ``context``'s window, ringing ``anchor_window_space``.

        Returns False without starting if the window may not be tracked: SR4
        blocks the process, or the window owns the display and cannot be
        overlaid at all.
        """
        if config.is_blocked(context.process_name):
            log.warning("tracking_refused_blocklisted", process=context.process_name)
            self.lost.emit("blocked")
            return False

        if context.is_fullscreen_exclusive:
            log.warning("tracking_refused_fullscreen", process=context.process_name)
            self.lost.emit("fullscreen_exclusive")
            return False

        self._hwnd = context.hwnd
        self._anchor = anchor_window_space
        self._last_emitted = None
        self._last_hidden_reason = None
        log.info(
            "tracking_started",
            process=context.process_name,
            hwnd=context.hwnd,
            window_bounds=context.bounds,
            anchor_window_space=anchor_window_space,
        )
        # Start before the first tick, not after. That tick can discover the
        # window is already gone and call _finish, and _finish stopping a timer
        # that had not yet been started left the tracker polling a dead handle.
        self._timer.start()
        self._tick()
        return self._hwnd is not None

    def stop(self) -> None:
        self._timer.stop()
        if self._hwnd is not None:
            log.info("tracking_stopped", hwnd=self._hwnd)
        self._hwnd = None
        self._anchor = None
        self._last_emitted = None

    @property
    def is_tracking(self) -> bool:
        return self._timer.isActive()

    # --- the poll ------------------------------------------------------------

    def _tick(self) -> None:
        hwnd, anchor = self._hwnd, self._anchor
        if hwnd is None or anchor is None:
            return

        if not windows.is_window(hwnd):
            self._finish("closed")
            return

        if windows.is_minimised(hwnd):
            self._hide("minimised")
            return

        if not windows.is_visible(hwnd):
            self._hide("not_visible")
            return

        if windows.is_fullscreen_exclusive(hwnd):
            # Entered fullscreen while we were pointing at it. Recoverable: the
            # user may leave fullscreen again, so this is hidden, not lost.
            self._hide("fullscreen_exclusive")
            return

        bounds = windows.window_frame_bounds(hwnd)
        if bounds is None:
            self._hide("no_bounds")
            return

        target = coords.rect_from_window_space(anchor, bounds)
        if target != self._last_emitted:
            self._last_emitted = target
            self._last_hidden_reason = None
            log.debug("tracked_target_moved", hwnd=hwnd, window=bounds, target=target)
            self.moved.emit(target)

    def _hide(self, reason: str) -> None:
        self._last_emitted = None
        if reason != self._last_hidden_reason:
            self._last_hidden_reason = reason
            log.info("tracked_window_hidden", hwnd=self._hwnd, reason=reason)
            self.hidden.emit(reason)

    def _finish(self, reason: str) -> None:
        hwnd = self._hwnd
        self._timer.stop()
        self._hwnd = None
        self._anchor = None
        self._last_emitted = None
        log.info("tracked_window_lost", hwnd=hwnd, reason=reason)
        self.lost.emit(reason)
