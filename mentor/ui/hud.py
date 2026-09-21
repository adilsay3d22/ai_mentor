"""The debug HUD: capture, ground, and draw every element the grounder found.

This is the most valuable development tool in the project (design.md says so,
and it is right): when a highlight lands in the wrong place, the HUD is what
tells you whether the grounder found the wrong box or the planner picked the
wrong element. Building it in phase 3 rather than at the end is deliberate.

The grounding pass runs in a QThread worker, because OCR takes the better part
of a second and the overlay has to stay responsive while it does. Results come
back over a Qt signal, per the threading rule in CLAUDE.md.
"""

from __future__ import annotations

import time

import structlog
from PySide6.QtCore import QObject, QThread, QTimer, Signal

from mentor.capture import windows
from mentor.capture.grabber import create_grabber
from mentor.grounding import fusion
from mentor.grounding.ocr import OcrGrounder
from mentor.grounding.uia import UiaGrounder
from mentor.models import AppContext, Element

log = structlog.get_logger(__name__)


class GroundingWorker(QThread):
    """Captures the focused window and grounds it, off the UI thread.

    One pass per ``request``. The worker owns its grabber and grounder because
    both hold native resources that dislike being used from two threads.
    """

    grounded = Signal(object, object)  # list[Element], AppContext
    failed = Signal(str)
    capturing = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pending: AppContext | None = None
        self._stopping = False

    def request(self, context: AppContext) -> None:
        """Queue a grounding pass for this window, replacing any pass not yet started."""
        self._pending = context
        if not self.isRunning():
            self.start()

    def stop(self) -> None:
        self._stopping = True
        self.requestInterruption()
        self.wait(2000)

    def run(self) -> None:
        grabber = create_grabber()
        ocr = OcrGrounder()
        uia = UiaGrounder()
        try:
            while not self.isInterruptionRequested() and not self._stopping:
                context = self._pending
                self._pending = None
                if context is None:
                    self.msleep(30)
                    continue

                started = time.perf_counter()
                # SR3: say so for exactly as long as a frame is being taken.
                self.capturing.emit(True)
                try:
                    frame = grabber.grab(context.bounds)
                finally:
                    self.capturing.emit(False)

                if frame is None:
                    self.failed.emit("capture returned no pixels")
                    continue

                # Cheapest provider first. UIA costs milliseconds and returns
                # nothing at all for most applications, so it is always worth
                # asking before paying for OCR.
                uia_started = time.perf_counter()
                from_uia = uia.ground(frame, context)
                uia_seconds = time.perf_counter() - uia_started

                from_ocr = ocr.ground(frame, context)
                elements = fusion.fuse(from_uia, from_ocr)

                log.info(
                    "grounding_pass_complete",
                    process=context.process_name,
                    elements=len(elements),
                    from_uia=len(from_uia),
                    from_ocr=len(from_ocr),
                    uia_seconds=round(uia_seconds, 3),
                    seconds=round(time.perf_counter() - started, 3),
                )
                self.grounded.emit(elements, context)
        finally:
            grabber.close()


class DebugHud(QObject):
    """Drives the grounding worker and pushes its results onto the overlay."""

    #: How often to re-ground while the HUD is on. Slow on purpose: a pass costs
    #: most of a second, and the HUD is for looking at, not for tracking.
    REFRESH_SECONDS = 3.0

    def __init__(self, overlay: object, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._overlay = overlay
        self._worker = GroundingWorker(self)
        self._worker.grounded.connect(self._on_grounded)
        self._worker.failed.connect(self._on_failed)

        self._timer = QTimer(self)
        self._timer.setInterval(int(self.REFRESH_SECONDS * 1000))
        self._timer.timeout.connect(self.refresh)
        self._last_process: str | None = None

    def start(self) -> None:
        self.refresh()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._worker.stop()
        self._overlay.clear_elements()

    @property
    def capturing(self) -> Signal:
        return self._worker.capturing

    def refresh(self) -> None:
        """Ground whatever window has focus now, unless Mentor may not look at it."""
        context = windows.focused_app_context()
        if context is None:
            return

        from mentor import config

        if config.is_blocked(context.process_name):
            if self._last_process != context.process_name:
                print(f"HUD: {context.process_name} is on the SR4 blocklist — not capturing.")
                self._last_process = context.process_name
            self._overlay.clear_elements()
            return

        if context.is_fullscreen_exclusive:
            if self._last_process != context.process_name:
                print(f"HUD: {context.process_name} owns the display — cannot overlay it.")
                self._last_process = context.process_name
            self._overlay.clear_elements()
            return

        self._last_process = context.process_name
        self._worker.request(context)

    def _on_grounded(self, elements: list[Element], context: AppContext) -> None:
        self._overlay.show_elements(elements)
        by_source: dict[str, int] = {}
        for element in elements:
            by_source[element.source] = by_source.get(element.source, 0) + 1
        breakdown = " ".join(f"{source}={count}" for source, count in sorted(by_source.items()))
        print(
            f"HUD: {len(elements):3d} elements ({breakdown or 'none'}) in "
            f"{context.process_name} — {context.window_title[:40]}"
        )

    def _on_failed(self, reason: str) -> None:
        log.warning("hud_grounding_failed", reason=reason)
        print(f"HUD: grounding failed — {reason}")
