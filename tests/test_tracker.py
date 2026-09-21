"""The tracker's decision logic, offline.

No real window is involved. ``mentor.capture.windows`` is replaced with a fake,
which is the point of keeping every Win32 call behind named functions: the
tracker's rules about when to move, hide, or give up are ordinary logic and can
be tested without a desktop.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication

from mentor.models import AppContext, Rect
from mentor.overlay import tracker as tracker_module
from mentor.overlay.tracker import WindowTracker

pytestmark = pytest.mark.offline

ANCHOR = Rect(24, 24, 160, 44)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    """QObject and QTimer need an application object to exist, but not a screen."""
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


class FakeWindows:
    """Stands in for ``mentor.capture.windows``."""

    def __init__(self, bounds: Rect | None = Rect(300, 200, 800, 600)) -> None:
        self.bounds = bounds
        self.alive = True
        self.minimised = False
        self.visible = True
        self.fullscreen = False

    def is_window(self, _hwnd: int) -> bool:
        return self.alive

    def is_minimised(self, _hwnd: int) -> bool:
        return self.minimised

    def is_visible(self, _hwnd: int) -> bool:
        return self.visible

    def is_fullscreen_exclusive(self, _hwnd: int) -> bool:
        return self.fullscreen

    def window_frame_bounds(self, _hwnd: int) -> Rect | None:
        return self.bounds


def context(process: str = "notepad.exe", *, fullscreen: bool = False) -> AppContext:
    return AppContext(
        process_name=process,
        window_title="Untitled - Notepad",
        version="11.0.0.0",
        hwnd=4242,
        bounds=Rect(300, 200, 800, 600),
        is_fullscreen_exclusive=fullscreen,
    )


@pytest.fixture
def rig(monkeypatch):
    fake = FakeWindows()
    monkeypatch.setattr(tracker_module, "windows", fake)
    tracker = WindowTracker()
    moved: list[Rect] = []
    hidden: list[str] = []
    lost: list[str] = []
    tracker.moved.connect(moved.append)
    tracker.hidden.connect(hidden.append)
    tracker.lost.connect(lost.append)
    yield tracker, fake, moved, hidden, lost
    tracker.stop()


def test_tracking_emits_the_anchor_in_physical_pixels(rig):
    tracker, _fake, moved, _hidden, _lost = rig
    assert tracker.track(context(), ANCHOR)
    assert moved == [Rect(324, 224, 160, 44)]


def test_the_ring_follows_the_window(rig):
    tracker, fake, moved, _hidden, _lost = rig
    tracker.track(context(), ANCHOR)
    fake.bounds = Rect(1000, 50, 800, 600)
    tracker._tick()
    assert moved[-1] == Rect(1024, 74, 160, 44)


def test_the_ring_follows_onto_a_negative_coordinate_monitor(rig):
    tracker, fake, moved, _hidden, _lost = rig
    tracker.track(context(), ANCHOR)
    fake.bounds = Rect(-1800, 100, 800, 600)
    tracker._tick()
    assert moved[-1] == Rect(-1776, 124, 160, 44)


def test_a_still_window_does_not_repaint(rig):
    """At 60Hz an unchanged position must not emit, or the overlay repaints for nothing."""
    tracker, _fake, moved, _hidden, _lost = rig
    tracker.track(context(), ANCHOR)
    for _ in range(10):
        tracker._tick()
    assert len(moved) == 1


def test_minimising_hides_the_ring_without_losing_the_window(rig):
    tracker, fake, _moved, hidden, lost = rig
    tracker.track(context(), ANCHOR)
    fake.minimised = True
    tracker._tick()
    assert hidden == ["minimised"]
    assert lost == []
    assert tracker.is_tracking


def test_restoring_after_minimise_shows_the_ring_again(rig):
    tracker, fake, moved, hidden, _lost = rig
    tracker.track(context(), ANCHOR)
    fake.minimised = True
    tracker._tick()
    fake.minimised = False
    tracker._tick()
    assert hidden == ["minimised"]
    assert moved[-1] == Rect(324, 224, 160, 44)


def test_a_repeated_hidden_reason_is_reported_once(rig):
    tracker, fake, _moved, hidden, _lost = rig
    tracker.track(context(), ANCHOR)
    fake.minimised = True
    for _ in range(5):
        tracker._tick()
    assert hidden == ["minimised"]


def test_going_fullscreen_mid_session_hides_rather_than_gives_up(rig):
    """Recoverable: the user may leave fullscreen again."""
    tracker, fake, _moved, hidden, lost = rig
    tracker.track(context(), ANCHOR)
    fake.fullscreen = True
    tracker._tick()
    assert hidden == ["fullscreen_exclusive"]
    assert lost == []


def test_closing_the_window_stops_the_tracker(rig):
    tracker, fake, _moved, _hidden, lost = rig
    tracker.track(context(), ANCHOR)
    fake.alive = False
    tracker._tick()
    assert lost == ["closed"]
    assert not tracker.is_tracking


def test_missing_bounds_hides_rather_than_guessing(rig):
    tracker, fake, _moved, hidden, _lost = rig
    tracker.track(context(), ANCHOR)
    fake.bounds = None
    tracker._tick()
    assert hidden == ["no_bounds"]


def test_sr4_blocklisted_process_is_refused(rig):
    """A password manager must never be tracked, let alone captured."""
    tracker, _fake, moved, _hidden, lost = rig
    assert not tracker.track(context("1password.exe"), ANCHOR)
    assert lost == ["blocked"]
    assert moved == []
    assert not tracker.is_tracking


def test_fullscreen_exclusive_window_is_refused_up_front(rig):
    tracker, _fake, moved, _hidden, lost = rig
    assert not tracker.track(context(fullscreen=True), ANCHOR)
    assert lost == ["fullscreen_exclusive"]
    assert moved == []


def test_tracking_a_window_that_is_already_gone_does_not_leave_a_timer_running(rig):
    """Regression: the first tick can give up, and track() must not start anyway.

    ``track`` ran its initial tick before starting the timer, so a window that
    had already closed emitted ``lost``, stopped a timer that had never started,
    and then had one started underneath it -- polling a dead handle at 60Hz for
    the rest of the session while reporting that it was tracking.
    """
    tracker, fake, _moved, _hidden, lost = rig
    fake.alive = False
    assert not tracker.track(context(), ANCHOR)
    assert lost == ["closed"]
    assert not tracker.is_tracking
