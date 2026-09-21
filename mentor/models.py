"""Every data model in Mentor lives here, and nowhere else.

Unless a field says otherwise, all coordinates are **physical screen pixels**
with the origin at the primary monitor's top-left. They may be negative on a
monitor positioned to the left of, or above, the primary one. Conversion to any
other coordinate space happens in ``mentor.capture.coords`` and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Only for the annotation: models.py stays cheap to import, which matters for
    # the cold-start budget in NFR6.
    import numpy as np


@dataclass(frozen=True)
class Point:
    """A single position in physical screen pixels."""

    x: int
    y: int


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in physical screen pixels."""

    x: int
    y: int
    w: int
    h: int

    @property
    def left(self) -> int:
        return self.x

    @property
    def top(self) -> int:
        return self.y

    @property
    def right(self) -> int:
        """Exclusive right edge: a 10px-wide rect at x=0 ends at 10, not 9."""
        return self.x + self.w

    @property
    def bottom(self) -> int:
        """Exclusive bottom edge."""
        return self.y + self.h

    @property
    def center(self) -> Point:
        return Point(self.x + self.w // 2, self.y + self.h // 2)

    def contains(self, point: Point) -> bool:
        return self.x <= point.x < self.right and self.y <= point.y < self.bottom

    def intersects(self, other: Rect) -> bool:
        return (
            self.x < other.right
            and other.x < self.right
            and self.y < other.bottom
            and other.y < self.bottom
        )

    def translated(self, dx: int, dy: int) -> Rect:
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def padded(self, amount: int) -> Rect:
        """Grow by ``amount`` on every side. Negative shrinks."""
        return Rect(self.x - amount, self.y - amount, self.w + 2 * amount, self.h + 2 * amount)


@dataclass(frozen=True)
class ScreenMapping:
    """How one monitor's physical pixels relate to Qt's logical coordinates.

    ``physical`` is the monitor's rectangle in physical screen space.
    ``logical`` is the same monitor as Qt reports it via ``QScreen.geometry()``.
    ``dpr`` is that screen's device pixel ratio: physical = logical * dpr.

    The two rectangles have independent origins — Qt lays monitors out in its own
    logical space — which is exactly why conversion has to be done per-screen and
    cannot be a single global divide.
    """

    name: str
    physical: Rect
    logical: Rect
    dpr: float
    is_primary: bool = False


@dataclass(frozen=True)
class AppContext:
    """The application Mentor is currently pointing at."""

    process_name: str
    window_title: str
    version: str | None
    hwnd: int
    bounds: Rect
    is_fullscreen_exclusive: bool


@dataclass(frozen=True, eq=False)
class Frame:
    """One captured image and the physical screen rectangle it covers.

    ``eq=False`` because numpy arrays do not compare to a single bool, and two
    frames are never usefully "equal" anyway.

    ``image`` is height x width x 3, BGR, matching what OpenCV and the OCR engine
    expect. ``bounds`` is what makes a pixel inside the image addressable on the
    real screen: image pixel (x, y) is physical (bounds.x + x, bounds.y + y).
    Nothing downstream may assume the image starts at the screen origin.
    """

    image: np.ndarray
    bounds: Rect
    captured_at: float
    source: str  # dxcam|mss


@dataclass(frozen=True)
class Element:
    """One thing the grounder found on screen this frame."""

    id: str
    label: str
    kind: str
    bounds: Rect
    confidence: float
    source: str


@dataclass(frozen=True)
class Step:
    """One instruction from the planner. Exactly one is ever on screen."""

    action: str
    target_id: str | None
    instruction: str
    expect: str
    confidence: float


@dataclass
class SessionState:
    """The mutable state of one guidance session."""

    goal: str
    app: AppContext
    history: list[Step] = field(default_factory=list)
    current: Step | None = None
    attempts: int = 0
