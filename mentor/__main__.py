"""Entry point: DPI awareness, then the QApplication, then the overlay.

Nothing here is the session controller. Until phase 5 exists, this module wires
the overlay up directly so that the coordinate maths can be checked by eye.
"""

from __future__ import annotations

import argparse
import ctypes
import signal
import sys
from types import FrameType

import structlog

from mentor import config
from mentor.models import Rect, ScreenMapping

log = structlog.get_logger(__name__)

#: The hardcoded phase-1 ring: physical screen pixels, near the primary monitor's
#: top-left where it can be compared against a known landmark. ``--at`` overrides it.
DEFAULT_RING = Rect(100, 100, 240, 64)

#: ``SetProcessDpiAwarenessContext`` handle for per-monitor v2 (Windows 10 1703+).
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

#: ``shcore.SetProcessDpiAwareness`` value. Named PER_MONITOR, but it is v1 only.
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def set_dpi_awareness() -> None:
    """Declare per-monitor-v2 DPI awareness.

    Must run before the QApplication exists and before anything captures or
    measures. Without it Windows silently reports virtualised coordinates on any
    scaled display and every rectangle in the system is quietly wrong.

    The call has to be ``user32.SetProcessDpiAwarenessContext``, not
    ``shcore.SetProcessDpiAwareness(2)``. The latter is named
    PROCESS_PER_MONITOR_DPI_AWARE and reads like v2, but it sets v1: no
    non-client-area scaling, and a different set of rules for child windows.
    Because awareness can only be set once per process, using it also locks Qt
    out of setting the v2 context it expects, which is where the
    "SetProcessDpiAwarenessContext() failed: Access is denied" warning on
    startup comes from. It is kept only as a fallback for pre-1703 Windows.
    """
    if sys.platform != "win32":
        return

    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        ):
            log.info("dpi_awareness_set", context="per_monitor_v2")
            return
        log.warning("dpi_awareness_v2_refused", error=ctypes.get_last_error())
    except (AttributeError, OSError) as exc:
        log.warning("dpi_awareness_v2_unavailable", error=str(exc))

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE)
        log.warning("dpi_awareness_set", context="per_monitor_v1_fallback")
    except OSError as exc:
        # E_ACCESSDENIED means awareness was already set for this process, which
        # is fine. Anything else is worth knowing about.
        log.warning("dpi_awareness_not_set", error=str(exc))


def install_qt_message_handler() -> None:
    """Send Qt's own diagnostics to ``./debug/mentor.log`` instead of the console.

    Nothing is discarded -- Qt's messages are more useful in the log next to the
    rectangles they explain than scrolling past on startup.
    """
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    levels = {
        QtMsgType.QtDebugMsg: "debug",
        QtMsgType.QtInfoMsg: "info",
        QtMsgType.QtWarningMsg: "warning",
        QtMsgType.QtCriticalMsg: "error",
        QtMsgType.QtFatalMsg: "critical",
    }

    def handler(mode: object, context: object, message: str) -> None:
        level = levels.get(mode, "info")  # type: ignore[arg-type]
        if "SetProcessDpiAwarenessContext() failed" in message:
            # Expected: set_dpi_awareness already put the process in exactly the
            # context Qt is asking for, and Windows allows it to be set once.
            level = "debug"
        getattr(log, level)(
            "qt_message", message=message.strip(), category=getattr(context, "category", None)
        )

    qInstallMessageHandler(handler)


def _parse_rect(value: str) -> Rect:
    parts = value.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("expected X,Y,W,H in physical screen pixels")
    try:
        x, y, w, h = (int(part.strip()) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("X,Y,W,H must all be integers") from exc
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("W and H must be positive")
    return Rect(x, y, w, h)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mentor", description=__doc__)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="verbose logging, plus the HUD drawing every grounded element",
    )
    parser.add_argument(
        "--provider",
        choices=("anthropic", "openrouter", "offline"),
        default=config.PLANNER_PROVIDER,
        help="where steps come from: the Anthropic API, OpenRouter, or the"
        " keyword-matching stand-in that needs no key (default: %(default)s)",
    )
    parser.add_argument(
        "--offline",
        dest="provider",
        action="store_const",
        const="offline",
        help="shorthand for --provider offline",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="override the model for the chosen provider",
    )
    parser.add_argument(
        "--ring",
        action="store_true",
        help="just draw a static ring at --at and nothing else (phase 1 check)",
    )
    parser.add_argument(
        "--at",
        type=_parse_rect,
        default=DEFAULT_RING,
        metavar="X,Y,W,H",
        help="ring this rectangle, in physical screen pixels (default: 100,100,240,64)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help="quit automatically after this long, instead of waiting for Ctrl+C",
    )
    parser.add_argument(
        "--list-screens",
        action="store_true",
        help="print the physical and logical geometry of every monitor, then exit",
    )
    parser.add_argument(
        "--track",
        action="store_true",
        help="attach to whichever window has focus after --countdown and follow it",
    )
    parser.add_argument(
        "--track-at",
        type=_parse_rect,
        default=Rect(24, 24, 160, 44),
        metavar="X,Y,W,H",
        help="rectangle to ring, in window space: pixels from the window's top-left"
        " (default: 24,24,160,44)",
    )
    parser.add_argument(
        "--countdown",
        type=float,
        default=4.0,
        metavar="SECONDS",
        help="how long to wait before grabbing the focused window (default: 4)",
    )
    return parser


def _print_screens(mappings: list[ScreenMapping]) -> None:
    from mentor.capture.coords import virtual_desktop_physical

    desktop = virtual_desktop_physical(mappings)
    print(f"virtual desktop (physical): x={desktop.x} y={desktop.y} w={desktop.w} h={desktop.h}")
    for m in mappings:
        primary = " [primary]" if m.is_primary else ""
        print(f"\n{m.name}{primary}")
        print(f"  physical : x={m.physical.x} y={m.physical.y} w={m.physical.w} h={m.physical.h}")
        print(f"  logical  : x={m.logical.x} y={m.logical.y} w={m.logical.w} h={m.logical.h}")
        print(f"  dpr      : {m.dpr}")


def _start_tracking(app: object, overlay: object, args: argparse.Namespace) -> object | None:
    """Attach to the window focused after a countdown and glue the ring inside it.

    The countdown exists because the focused window at launch is the terminal
    Mentor was started from, which is not the interesting case.
    """
    from PySide6.QtCore import QTimer

    from mentor.capture import windows as win
    from mentor.overlay.tracker import WindowTracker

    tracker = WindowTracker()
    tracker.moved.connect(overlay.show_ring)
    tracker.hidden.connect(lambda reason: (overlay.clear(), print(f"Mentor: hidden ({reason})")))
    tracker.lost.connect(lambda reason: (overlay.clear(), print(f"Mentor: lost window ({reason})")))

    def attach() -> None:
        context = win.focused_app_context()
        if context is None:
            print("Mentor: no focused window found.")
            return

        print(f"\nMentor: attached to {context.process_name or '<unknown>'}")
        print(f"  title   : {context.window_title}")
        print(f"  version : {context.version or '<none>'}")
        print(
            f"  bounds  : x={context.bounds.x} y={context.bounds.y} "
            f"w={context.bounds.w} h={context.bounds.h}  (true frame, shadow excluded)"
        )
        raw = win.window_rect_raw(context.hwnd)
        if raw is not None and raw != context.bounds:
            print(
                f"  shadow  : GetWindowRect would have said x={raw.x} y={raw.y} w={raw.w} h={raw.h}"
            )
        if context.is_fullscreen_exclusive:
            print("  NOTE    : this window owns the display and cannot be overlaid.")

        if not tracker.track(context, args.track_at):
            print("Mentor: refusing to track this window. See ./debug/mentor.log.")
            return
        print("  Drag, resize or maximise it -- the ring should stay put. Ctrl+C to quit.")

    print(f"Mentor: focus the window you want in {args.countdown:g}s...")
    QTimer.singleShot(int(args.countdown * 1000), attach)
    return tracker


class Session:
    """Everything a live guidance session needs, wired together.

    Kept out of ``main`` so the ownership is legible: the hotkey opens the box,
    the box gives the controller a goal, the controller drives the overlay, and
    the caption says the sentence. Nothing here decides anything -- the state
    machine in ``session/controller.py`` does.
    """

    def __init__(
        self,
        app: object,
        overlay: object,
        tray: object,
        *,
        provider: str = "anthropic",
        model: str | None = None,
    ) -> None:
        from mentor.session.controller import SessionController
        from mentor.session.states import State
        from mentor.ui.hotkey import HotkeyManager
        from mentor.ui.prompt_box import Caption, PromptBox

        self._State = State
        self._client = _build_client(provider, model)
        self._overlay = overlay
        self._box = PromptBox()
        self._caption = Caption()
        self._controller = SessionController(overlay, self._client)

        self._controller.message.connect(self._say)
        self._controller.capturing.connect(tray.set_capturing)
        self._box.submitted.connect(self._controller.start)
        self._box.dismissed.connect(self._controller.cancel)

        self._hotkeys = HotkeyManager()
        app.installNativeEventFilter(self._hotkeys)
        self._hotkeys.triggered.connect(self._on_hotkey)

    def register_hotkeys(self) -> bool:
        """Claim the ask and advance keys. False if either is already taken."""
        ok = self._hotkeys.register(config.HOTKEY)
        ok = self._hotkeys.register(config.NEXT_HOTKEY) and ok
        return ok

    def _on_hotkey(self, combination: str) -> None:
        if combination == config.NEXT_HOTKEY:
            self._controller.next_step()
            return

        # The ask key. FR1: pressing it during a session reopens the box so the
        # goal can be redirected mid-task.
        from mentor.capture import windows as win

        context = win.focused_app_context()
        if context is None:
            print("Mentor: no window is focused.")
            return
        self._caption.clear()
        self._box.ask(context.bounds, context.process_name)

    def _say(self, text: str) -> None:
        """Put the controller's sentence under the ring, or centred if there is none."""
        session = self._controller.session
        near = None
        if session is not None and session.current is not None:
            from mentor.planner import planner as planner_module

            target = planner_module.target_of(session.current, self._controller_elements())
            near = target.bounds if target is not None else None

        auto_hide = 3000 if self._controller.state is self._State.DONE else None
        self._caption.say(text, near, auto_hide_ms=auto_hide)
        print(f"Mentor: {text}")

    def _controller_elements(self) -> list:
        return getattr(self._controller, "_elements", [])

    def shutdown(self) -> None:
        self._hotkeys.unregister_all()
        self._caption.clear()
        self._box.hide()


def _build_client(provider: str, model: str | None) -> object:
    """The one place that knows which planner backend is in use."""
    if provider == "offline":
        from mentor.planner.offline import OfflinePlanner

        return OfflinePlanner()
    if provider == "openrouter":
        from mentor.planner.openrouter import OpenRouterClient

        return OpenRouterClient(model=model or config.OPENROUTER_MODEL)

    from mentor.planner.client import AnthropicClient

    return AnthropicClient(model=model or config.PLANNER_MODEL)


def _start_session(
    app: object,
    overlay: object,
    tray: object,
    *,
    provider: str = "anthropic",
    model: str | None = None,
) -> Session | None:
    """Build a session, or explain why one cannot start."""
    from mentor.planner.client import PlannerError

    try:
        session = Session(app, overlay, tray, provider=provider, model=model)
    except PlannerError as exc:
        print(f"Mentor: {exc}")
        return None

    if not session.register_hotkeys():
        print(
            f"Mentor: could not claim {config.HOTKEY} or {config.NEXT_HOTKEY} — "
            "another application already owns one of them. See ./debug/mentor.log."
        )
        session.shutdown()
        return None

    if provider == "offline":
        print("Mentor: OFFLINE PLANNER — keyword matching, no API. Steps will often be wrong.")
    else:
        print(f"Mentor: planner = {provider} ({model or 'default model'})")
    print(f"Mentor: ready. Press {config.HOTKEY} over any app and type what you want to do.")
    print(f"        {config.NEXT_HOTKEY} shows the next step. Ctrl+C here to quit.")
    return session


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Order matters: logging first so the DPI call can report what it achieved,
    # then DPI awareness, and only then anything Qt.
    config.configure_logging(debug=args.debug)
    set_dpi_awareness()

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    install_qt_message_handler()

    # Without PassThrough, a 150% display is rounded to 2x and every conversion
    # in coords.py is handed a device pixel ratio that does not match reality.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Mentor")
    app.setApplicationVersion("0.1.0")
    app.setQuitOnLastWindowClosed(False)

    from mentor.overlay.window import Overlay

    log.info("mentor_starting", debug=args.debug, ring=args.at)

    # The overlay is built either way: each screen's physical rectangle is
    # measured from its own window, so there is no cheaper way to report them.
    overlay = Overlay(app)

    if args.list_screens:
        _print_screens(overlay.mappings)
        overlay.close()
        return 0

    from mentor.ui.tray import TrayIndicator

    tracker = _start_tracking(app, overlay, args) if args.track else None
    hud = None
    session = None

    def _quit(*_: object) -> None:
        log.info("mentor_stopping")
        if hud is not None:
            hud.stop()
        if tracker is not None:
            tracker.stop()
        if session is not None:
            session.shutdown()
        tray.hide()
        overlay.close()
        app.quit()

    # SR3: the tray icon exists for as long as Mentor does, not just while capturing.
    tray = TrayIndicator(app, _quit)
    tray.show()

    if args.debug:
        from mentor.ui.hud import DebugHud

        hud = DebugHud(overlay)
        hud.capturing.connect(tray.set_capturing)
        hud.start()
        print("Mentor: debug HUD on — boxes follow whichever window has focus.")
    elif args.ring:
        overlay.show_ring(args.at)
    elif tracker is None:
        session = _start_session(app, overlay, tray, provider=args.provider, model=args.model)
        if session is None:
            overlay.close()
            tray.hide()
            return 2

    def _on_sigint(_signum: int, _frame: FrameType | None) -> None:
        _quit()

    signal.signal(signal.SIGINT, _on_sigint)
    # Qt's event loop does not yield to Python often enough for a signal handler
    # to run on its own. A timer that does nothing gives the interpreter a slot.
    wakeup = QTimer()
    wakeup.setInterval(100)
    wakeup.timeout.connect(lambda: None)
    wakeup.start()

    if args.duration is not None:
        QTimer.singleShot(int(args.duration * 1000), _quit)

    if args.ring:
        print(
            f"Mentor: ringing physical rect x={args.at.x} y={args.at.y} "
            f"w={args.at.w} h={args.at.h}. Ctrl+C here to quit."
        )
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
