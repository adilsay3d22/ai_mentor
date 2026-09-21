"""System tray icon and capture indicator.

SR3, which design.md calls non-negotiable: a tool that watches your screen must
be visibly honest about when. The tray icon is present for as long as Mentor
runs, and turns accent-coloured for exactly as long as a frame is being taken.

The icon is drawn rather than loaded so there is no asset to lose, and so the
capturing and idle states cannot drift apart visually.
"""

from __future__ import annotations

import structlog
from PySide6.QtCore import QObject, QRectF, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from mentor.overlay import painters

log = structlog.get_logger(__name__)

_ICON_PX = 32
_IDLE_COLOUR = QColor("#9AA0A6")


def _dot_icon(colour: QColor, *, filled: bool) -> QIcon:
    """A dot: hollow and grey when idle, solid and accent-coloured when capturing."""
    pixmap = QPixmap(_ICON_PX, _ICON_PX)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    inset = 5.0
    circle = QRectF(inset, inset, _ICON_PX - 2 * inset, _ICON_PX - 2 * inset)
    if filled:
        painter.setPen(QColor(0, 0, 0, 0))
        painter.setBrush(colour)
    else:
        pen = painter.pen()
        pen.setColor(colour)
        pen.setWidthF(3.0)
        painter.setPen(pen)
        painter.setBrush(QColor(0, 0, 0, 0))
    painter.drawEllipse(circle)
    painter.end()
    return QIcon(pixmap)


class TrayIndicator(QObject):
    """The tray icon, and the honest answer to "is it looking at my screen?"."""

    #: How long the capture dot stays lit after a grab finishes, so that a
    #: 19-millisecond capture is still something a person can actually see.
    MINIMUM_VISIBLE_MS = 450

    def __init__(self, app: QApplication, on_quit: object, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._idle_icon = _dot_icon(_IDLE_COLOUR, filled=False)
        self._capturing_icon = _dot_icon(painters.ACCENT, filled=True)

        self._tray = QSystemTrayIcon(self._idle_icon, app)
        self._tray.setToolTip("Mentor — idle")

        menu = QMenu()
        self._status_action = QAction("Idle", menu)
        self._status_action.setEnabled(False)
        menu.addAction(self._status_action)
        menu.addSeparator()
        quit_action = QAction("Quit Mentor", menu)
        quit_action.triggered.connect(on_quit)
        menu.addAction(quit_action)

        self._menu = menu  # keep a reference or Qt collects it
        self._tray.setContextMenu(menu)

        self._off_timer = QTimer(self)
        self._off_timer.setSingleShot(True)
        self._off_timer.setInterval(self.MINIMUM_VISIBLE_MS)
        self._off_timer.timeout.connect(lambda: self._apply(False))

    def show(self) -> bool:
        """Show the tray icon. False if this system has no usable tray."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.error("system_tray_unavailable")
            return False
        self._tray.show()
        log.info("tray_shown")
        return True

    def hide(self) -> None:
        self._tray.hide()

    def set_capturing(self, capturing: bool) -> None:
        """Flip the indicator. Called around every single frame grab.

        Turning on is immediate; turning off is delayed by
        ``MINIMUM_VISIBLE_MS``. A frame grab takes about 19 milliseconds, and an
        indicator that is on for 19ms is not an indicator -- nobody can see it.
        SR3 asks for a *visible* signal that capture happened, so the dot stays
        lit long enough to register. It is never shortened, only lengthened, so
        it cannot under-report.
        """
        if capturing:
            self._off_timer.stop()
            self._apply(True)
        else:
            self._off_timer.start()

    def _apply(self, capturing: bool) -> None:
        self._tray.setIcon(self._capturing_icon if capturing else self._idle_icon)
        text = "Capturing" if capturing else "Idle"
        self._tray.setToolTip(f"Mentor — {text.lower()}")
        self._status_action.setText(text)
