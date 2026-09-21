"""The input box, and the caption that replaces it.

design.md: "That's the whole widget." A single line, centred on the focused
window's top third, `Enter` submits, `Esc` dismisses. It is the one piece of
Mentor's UI that must accept input, which means it is the one window here that
is deliberately *not* click-through.

The caption is in this module rather than with the overlay painters because it
is a real focusable-adjacent widget with text layout, not something drawn into
the click-through surface. Keeping it a widget means Qt does the text wrapping.
"""

from __future__ import annotations

import structlog
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget

from mentor.models import Rect

log = structlog.get_logger(__name__)

_SURFACE = "rgba(20, 20, 22, 0.92)"
_BORDER = "rgba(255, 255, 255, 0.12)"
_TEXT = "rgba(255, 255, 255, 0.92)"

_BOX_WIDTH = 520
_BOX_HEIGHT = 52


class PromptBox(QWidget):
    """A single-line ask, centred over the target window."""

    submitted = Signal(str)
    dismissed = Signal()

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._edit = QLineEdit(self)
        self._edit.setPlaceholderText("Ask about this app…")
        self._edit.returnPressed.connect(self._submit)
        self._edit.setStyleSheet(
            f"QLineEdit {{"
            f"  background: {_SURFACE};"
            f"  border: 1px solid {_BORDER};"
            f"  border-radius: 10px;"
            f"  padding: 12px 16px;"
            f"  color: {_TEXT};"
            f"  font-size: 15px;"
            f"}}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._edit)

    def ask(self, window: Rect, app_name: str) -> None:
        """Show the box over ``window``, naming the application it will guide."""
        pretty = app_name.removesuffix(".exe") or "this app"
        self._edit.setPlaceholderText(f"Ask about {pretty}…")
        self._edit.clear()

        # Centred on the window's top third, per design.md.
        x = window.x + (window.w - _BOX_WIDTH) // 2
        y = window.y + window.h // 3 - _BOX_HEIGHT // 2
        self.setGeometry(x, y, _BOX_WIDTH, _BOX_HEIGHT)

        self.show()
        self.raise_()
        self.activateWindow()
        self._edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            self.dismissed.emit()
            return
        super().keyPressEvent(event)

    def _submit(self) -> None:
        text = self._edit.text().strip()
        if not text:
            return
        self.hide()
        log.info("goal_submitted", goal=text)
        self.submitted.emit(text)


class Caption(QWidget):
    """The one-sentence instruction, shown under the ring.

    Click-through, like the overlay: it is something to read, never something to
    interact with.
    """

    #: How far below the target the caption sits.
    GAP = 14

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._label = QLabel(self)
        self._label.setWordWrap(True)
        self._label.setStyleSheet(
            f"QLabel {{"
            f"  background: {_SURFACE};"
            f"  border: 1px solid {_BORDER};"
            f"  border-radius: 10px;"
            f"  padding: 12px;"
            f"  color: {_TEXT};"
            f"  font-size: 14px;"
            f"}}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def say(self, text: str, near: Rect | None = None, *, auto_hide_ms: int | None = None) -> None:
        """Show ``text``, below ``near`` if given, otherwise centred on screen."""
        self._label.setText(text)
        self._label.adjustSize()

        width = min(max(self._label.sizeHint().width() + 4, 220), 560)
        height = self._label.heightForWidth(width) or self._label.sizeHint().height()
        height += 4

        if near is not None:
            x = near.x + (near.w - width) // 2
            y = near.bottom + self.GAP
        else:
            screen = self.screen().geometry()
            x = screen.x() + (screen.width() - width) // 2
            y = screen.y() + int(screen.height() * 0.75)

        self.setGeometry(x, y, width, height)
        self.show()
        self.raise_()

        self._hide_timer.stop()
        if auto_hide_ms is not None:
            self._hide_timer.start(auto_hide_ms)

    def clear(self) -> None:
        self._hide_timer.stop()
        self.hide()
