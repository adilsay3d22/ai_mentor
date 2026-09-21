"""The Grounder protocol and the rules for building Elements.

Invariant 3 lives downstream of this file: the planner may only choose from what
a grounder actually returned for this frame. That only works if every provider
produces the same shape of answer, so all of them implement :class:`Grounder` and
all of them return physical-screen-pixel bounds.

Providers do *not* assign element ids. Ids are frame-local and have to be unique
across the merged result, so :mod:`mentor.grounding.fusion` assigns them after
merging. A provider that invented its own ids would collide with another's.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mentor.models import AppContext, Element, Frame, Rect

#: The element kinds the planner is allowed to see. Kept small on purpose: the
#: planner reasons about what a thing *is* for clicking, not about widget taxonomy.
KINDS = frozenset({"menu", "button", "icon", "field", "tab", "panel", "text"})


@runtime_checkable
class Grounder(Protocol):
    """Turns a captured frame into the things visible on it.

    ``source`` names the provider and ends up on every Element it produces, which
    is what makes the debug HUD legible and what lets fusion break ties.
    """

    source: str

    def ground(self, frame: Frame, context: AppContext) -> list[Element]:
        """Find elements in ``frame``. Bounds are physical screen pixels.

        ``context`` carries the window's identity as well as its geometry, which
        pixels alone cannot give. The accessibility provider needs the handle to
        walk that window's control tree; the cache provider will need the process
        name and version to look anything up. Providers that only need pixels —
        OCR, and vision later — simply ignore it.
        """
        ...


def is_meaningful_label(text: str) -> bool:
    """Reject labels that are really an icon rather than words.

    Two sources feed this. OCR misreads a close button as the multiplication
    sign, a chevron as ">", a maximise box as a square glyph. UIA hands back
    private-use-area characters from icon fonts such as Segoe Fluent Icons --
    a control genuinely named "". Offering either to the planner as
    something it may point at is worse than not seeing it, because it cannot
    tell them apart from real controls.

    The rule is deliberately narrow -- one alphanumeric character is enough to
    survive -- so it cannot silently hide real text. Private-use characters are
    not alphanumeric, so they fall out for free. It does not catch misreads that
    come back as plausible letters; icon grounding proper is phase 7's job.
    """
    return any(character.isalnum() for character in text)


def frame_rect_to_screen(rect: Rect, frame: Frame) -> Rect:
    """Image-local pixels -> physical screen pixels.

    Every provider needs this and none of them should reinvent it. A frame's
    image starts at ``frame.bounds``, never at the screen origin, so a provider
    that returns image coordinates unchanged puts every highlight at the top-left
    of the desktop.
    """
    return Rect(rect.x + frame.bounds.x, rect.y + frame.bounds.y, rect.w, rect.h)


def make_element(
    *,
    label: str,
    kind: str,
    bounds: Rect,
    confidence: float,
    source: str,
) -> Element:
    """Build an Element with a placeholder id, validating the parts we control.

    The id is deliberately empty: fusion assigns it. Anything that renders or
    plans against an element with an empty id is using it before it was merged.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown element kind {kind!r}; expected one of {sorted(KINDS)}")
    return Element(
        id="",
        label=label.strip(),
        kind=kind,
        bounds=bounds,
        confidence=max(0.0, min(1.0, confidence)),
        source=source,
    )
