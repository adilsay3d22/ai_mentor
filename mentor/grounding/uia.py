"""Accessibility-tree grounding via UI Automation.

When an application exposes a UIA tree, this is the best grounding there is: it
reports a control's *actual* rectangle and its *actual* role, rather than
inferring both from where ink happened to land. A button whose label is an icon
is invisible to OCR and obvious to UIA.

Most applications do not expose a useful tree. That is the normal case, not the
edge case (FR2 says so), so the design priority here is not richness — it is
being cheap and undramatic when there is nothing to find. Two guards enforce
that: a wall-clock budget on the whole walk, and a cap on how many controls are
collected. Both are hit before a pathological tree can stall a grounding pass.

Threading note: UIA is COM. The grounding worker is a QThread, and COM must be
initialised on the thread that uses it, so :meth:`UiaGrounder.ground` does that
once per thread rather than assuming the main thread did it.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import structlog

from mentor.capture import windows
from mentor.grounding.base import KINDS, is_meaningful_label, make_element
from mentor.models import AppContext, Element, Frame, Rect

log = structlog.get_logger(__name__)

#: Give up walking after this long. A pass that takes longer than this has
#: already cost more than the OCR it was supposed to save.
DEFAULT_TIME_BUDGET = 0.5

#: Stop after this many controls. A document with ten thousand list items is not
#: ten thousand things the user might be told to click.
DEFAULT_MAX_ELEMENTS = 400

#: How deep to descend. Real controls live near the top; the depths below this
#: are document content in every application checked.
DEFAULT_MAX_DEPTH = 12

#: UIA ControlType names mapped onto the small vocabulary in ``base.KINDS``.
#: Anything not listed is skipped rather than guessed at -- an element with the
#: wrong kind is worse than one the planner never sees.
CONTROL_TYPE_KINDS: dict[str, str] = {
    "ButtonControl": "button",
    "SplitButtonControl": "button",
    "HyperlinkControl": "button",
    "CheckBoxControl": "button",
    "RadioButtonControl": "button",
    "MenuItemControl": "menu",
    "MenuControl": "menu",
    "MenuBarControl": "menu",
    "TabItemControl": "tab",
    "TabControl": "tab",
    "EditControl": "field",
    "ComboBoxControl": "field",
    "DocumentControl": "field",
    "ListItemControl": "text",
    "TreeItemControl": "text",
    "TextControl": "text",
    "ImageControl": "icon",
    "ToolBarControl": "panel",
    "PaneControl": "panel",
    "GroupControl": "panel",
}

#: Containers are worth descending into but not worth offering as targets: the
#: user is never told to click a pane.
NON_TARGET_KINDS = frozenset({"panel"})

#: UWP applications put their real control tree in a child window of this class,
#: owned by a different process from the ApplicationFrameHost that owns the
#: frame. Walking only the frame finds the three caption buttons and nothing else.
UWP_CONTENT_CLASS = "Windows.UI.Core.CoreWindow"

_com_initialised = threading.local()


def _ensure_com() -> None:
    """Initialise COM for this thread, once."""
    if getattr(_com_initialised, "done", False):
        return
    try:
        import comtypes

        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
    except OSError as exc:
        # RPC_E_CHANGED_MODE means the thread already has a different apartment,
        # which is fine -- COM is initialised either way.
        log.debug("com_already_initialised", error=str(exc))
    _com_initialised.done = True


def control_kind(control_type_name: str) -> str | None:
    """Map a UIA ControlType onto one of our kinds, or None to skip it."""
    kind = CONTROL_TYPE_KINDS.get(control_type_name)
    if kind is not None and kind not in KINDS:  # pragma: no cover - guards a typo
        raise ValueError(f"{control_type_name} maps to unknown kind {kind!r}")
    return kind


def usable_rect(rect: Any, within: Rect) -> Rect | None:
    """Turn a UIA BoundingRectangle into a Rect, or None if it is not pointable.

    Rejects empty rectangles and anything outside the window being grounded.
    UIA happily reports controls belonging to the window that are scrolled out of
    view or on another monitor entirely.
    """
    try:
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    except AttributeError:
        return None
    if right <= left or bottom <= top:
        return None
    candidate = Rect(int(left), int(top), int(right - left), int(bottom - top))
    if not within.intersects(candidate):
        return None
    return candidate


class UiaGrounder:
    """UI Automation behind the Grounder protocol."""

    source = "uia"

    def __init__(
        self,
        time_budget: float = DEFAULT_TIME_BUDGET,
        max_elements: int = DEFAULT_MAX_ELEMENTS,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        self.time_budget = time_budget
        self.max_elements = max_elements
        self.max_depth = max_depth

    def ground(self, frame: Frame, context: AppContext) -> list[Element]:
        """Walk the target window's control tree.

        The walk starts at the window's own control rather than the desktop
        root, so this can never report controls belonging to another
        application. The captured pixels are not used at all — that is the whole
        appeal of this provider.
        """
        return self.ground_window(context.hwnd, context.bounds)

    def ground_window(self, hwnd: int, window_bounds: Rect) -> list[Element]:
        """Collect pointable controls belonging to ``hwnd`` and its UWP content.

        A UWP window is two windows: the frame, and a ``CoreWindow`` child in a
        different process holding everything that matters. Measured on Settings,
        the frame's tree has 3 controls and the child's has 81. Both are walked,
        and fusion removes anything reported twice.
        """
        roots = [hwnd, *windows.child_windows_of_class(hwnd, UWP_CONTENT_CLASS)]
        collected: list[Element] = []
        for root_hwnd in roots:
            collected.extend(self._walk(root_hwnd, window_bounds))
        return collected

    def _walk(self, hwnd: int, window_bounds: Rect) -> list[Element]:
        """Walk one window's control tree, within the time and size budgets."""
        _ensure_com()
        started = time.perf_counter()

        try:
            import uiautomation as auto
        except ImportError as exc:  # pragma: no cover - dependency is declared
            log.error("uiautomation_unavailable", error=str(exc))
            return []

        try:
            root = auto.ControlFromHandle(hwnd)
        except (OSError, LookupError, ValueError) as exc:
            log.debug("uia_root_unavailable", hwnd=hwnd, error=str(exc))
            return []
        if root is None:
            log.debug("uia_no_root", hwnd=hwnd)
            return []

        elements: list[Element] = []
        visited = 0
        truncated = False

        stack: list[tuple[Any, int]] = [(root, 0)]
        while stack:
            if time.perf_counter() - started > self.time_budget:
                truncated = True
                break
            if len(elements) >= self.max_elements:
                truncated = True
                break

            control, depth = stack.pop()
            visited += 1

            try:
                kind = control_kind(control.ControlTypeName)
                name = (control.Name or "").strip()
                offscreen = bool(control.IsOffscreen)
                bounds = usable_rect(control.BoundingRectangle, window_bounds)
                children = control.GetChildren() if depth < self.max_depth else []
            except (OSError, LookupError, ValueError, AttributeError) as exc:
                # A control can vanish mid-walk. That is ordinary, not an error.
                log.debug("uia_control_read_failed", depth=depth, error=str(exc))
                continue

            for child in children:
                stack.append((child, depth + 1))

            if kind is None or kind in NON_TARGET_KINDS or offscreen or bounds is None:
                continue
            # Icon fonts give controls a private-use character for a name.
            if not is_meaningful_label(name):
                continue

            elements.append(
                make_element(
                    label=name,
                    kind=kind,
                    # UIA reports a control's declared rectangle, so it is exact
                    # in a way an ink bounding box never is.
                    bounds=bounds,
                    confidence=0.99,
                    source=self.source,
                )
            )

        elapsed = time.perf_counter() - started
        log.info(
            "uia_grounded",
            hwnd=hwnd,
            seconds=round(elapsed, 3),
            visited=visited,
            elements=len(elements),
            truncated=truncated,
        )
        return elements
