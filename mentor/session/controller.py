"""The session state machine. The only module that orchestrates.

architecture.md is emphatic that nothing else moves between states and nothing
else calls into the overlay and the planner. Everything here runs on the Qt main
thread; the grounding pass and the planner call both happen in QThread workers
and come back as signals.

Phase 5 covers IDLE -> PLANNING -> SHOWING with manual advance. There is no
verification yet, so "did the user do it" is answered by the user pressing a
key, and `phases.md` says that is the honest scope for now.
"""

from __future__ import annotations

import structlog
from PySide6.QtCore import QObject, QThread, Signal

from mentor import config
from mentor.capture import windows
from mentor.capture.grabber import create_grabber
from mentor.grounding import fusion
from mentor.grounding.ocr import OcrGrounder
from mentor.grounding.uia import UiaGrounder
from mentor.models import AppContext, Element, SessionState, Step
from mentor.overlay import painters
from mentor.planner import planner
from mentor.planner.client import LlmClient, PlannerError
from mentor.session.states import State, may_move

log = structlog.get_logger(__name__)


class PlanningWorker(QThread):
    """One full pass: capture, ground, plan. Off the UI thread, all of it.

    Capture and grounding could be separated from planning, but they are always
    run together and separating them would mean two workers and a handoff for no
    benefit. If the planner is ever given a cached element list, this splits.
    """

    planned = Signal(object, object, object)  # Step, list[Element], AppContext
    failed = Signal(str)
    capturing = Signal(bool)

    def __init__(self, client: LlmClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._client = client
        self._goal = ""
        self._context: AppContext | None = None
        self._history: list[Step] = []

    def plan(self, goal: str, context: AppContext, history: list[Step]) -> None:
        self._goal = goal
        self._context = context
        self._history = list(history)
        self.start()

    def run(self) -> None:
        context = self._context
        if context is None:
            return

        grabber = create_grabber()
        try:
            self.capturing.emit(True)
            try:
                frame = grabber.grab(context.bounds)
            finally:
                self.capturing.emit(False)

            if frame is None:
                self.failed.emit("Could not capture that window.")
                return

            elements = fusion.fuse(
                UiaGrounder().ground(frame, context),
                OcrGrounder().ground(frame, context),
            )

            step = planner.plan_next_step(
                self._client, self._goal, context, elements, self._history
            )
            self.planned.emit(step, elements, context)
        except PlannerError as exc:
            log.warning("planning_failed", error=str(exc))
            self.failed.emit(str(exc))
        finally:
            grabber.close()


class SessionController(QObject):
    """Drives one session from a goal to a highlight, and on to the next step."""

    state_changed = Signal(object)  # State
    message = Signal(str)

    def __init__(self, overlay: object, client: LlmClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._overlay = overlay
        self._state = State.IDLE
        self._session: SessionState | None = None
        self._elements: list[Element] = []

        self._worker = PlanningWorker(client, self)
        self._worker.planned.connect(self._on_planned)
        self._worker.failed.connect(self._on_failed)

    # --- state ---------------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def session(self) -> SessionState | None:
        return self._session

    def _move_to(self, target: State) -> None:
        if not may_move(self._state, target):
            # Refusing loudly rather than taking the transition anyway: a state
            # machine that can go anywhere explains nothing when it gets stuck.
            log.error("illegal_state_transition", source=self._state, target=target)
            return
        log.info("state_changed", source=self._state, target=target)
        self._state = target
        self.state_changed.emit(target)

    # --- the session ---------------------------------------------------------

    def start(self, goal: str) -> bool:
        """Begin guiding towards ``goal`` in whatever window has focus."""
        goal = goal.strip()
        if not goal:
            return False

        context = windows.focused_app_context()
        if context is None:
            self._fail("No window is focused.")
            return False
        if config.is_blocked(context.process_name):
            self._fail(f"{context.process_name} is on the blocklist. Mentor will not look at it.")
            return False
        if context.is_fullscreen_exclusive:
            self._fail(f"{context.process_name} owns the display and cannot be overlaid.")
            return False

        self._session = SessionState(goal=goal, app=context)
        self._elements = []
        log.info("session_started", goal=goal, process=context.process_name)
        self._plan()
        return True

    def next_step(self) -> None:
        """Advance manually. Phase 6 replaces this with the verifier."""
        if self._session is None or self._state not in (State.SHOWING, State.DONE):
            return
        if self._session.current is not None:
            self._session.history.append(self._session.current)
            self._session.current = None
        self._overlay.clear()
        self._plan()

    def cancel(self) -> None:
        """Esc, or the hotkey again. Everything disappears immediately (FR1)."""
        self._overlay.clear()
        self._session = None
        self._elements = []
        if self._state is not State.IDLE:
            self._move_to(State.IDLE)

    # --- internals -----------------------------------------------------------

    def _plan(self) -> None:
        if self._session is None:
            return
        # Re-read the window: it may have moved, or a dialog may have opened,
        # since the previous step was shown.
        context = windows.focused_app_context() or self._session.app
        self._session.app = context
        self._move_to(State.PLANNING)
        self.message.emit("Thinking…")
        self._worker.plan(self._session.goal, context, self._session.history)

    def _on_planned(self, step: Step, elements: list[Element], context: AppContext) -> None:
        if self._session is None:
            return
        self._session.current = step
        self._session.app = context
        self._elements = elements

        if step.action == "done":
            self._move_to(State.DONE)
            self._overlay.clear()
            self.message.emit(step.instruction or "That's it — done.")
            return

        target = planner.target_of(step, elements)
        if target is None:
            # "wait" with no target, or a target that vanished between validation
            # and rendering. Either way there is nothing to ring.
            self._move_to(State.SHOWING)
            self.message.emit(step.instruction)
            return

        accent = (
            painters.ACCENT_UNSURE
            if step.confidence < config.UNSURE_CONFIDENCE
            else painters.ACCENT
        )
        self._overlay.show_ring(target.bounds, accent=accent)
        self._move_to(State.SHOWING)

        prefix = "I think — " if step.confidence < config.UNSURE_CONFIDENCE else ""
        self.message.emit(f"{prefix}{step.instruction}")
        log.info(
            "step_shown",
            instruction=step.instruction,
            target=target.label,
            bounds=target.bounds,
            confidence=step.confidence,
        )

    def _on_failed(self, reason: str) -> None:
        self._fail(reason)

    def _fail(self, reason: str) -> None:
        self._overlay.clear()
        if self._state is not State.FAILED:
            self._move_to(State.FAILED)
        log.warning("session_failed", reason=reason)
        self.message.emit(reason)

    @property
    def capturing(self) -> Signal:
        return self._worker.capturing
