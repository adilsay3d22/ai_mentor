"""Win32: which window has focus, what process owns it, and where it really is.

Everything here returns physical screen pixels, the standard from ``models.py``.
Nothing here converts between coordinate spaces -- that is ``coords.py``'s job --
and nothing here draws.

Two Windows details drive most of this module:

* ``GetWindowRect`` has included an invisible drop-shadow border since Windows 10.
  Using it to position a highlight puts the highlight several pixels off on every
  edge. ``DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`` gives the real
  visible frame, and that is what :func:`window_frame_bounds` returns.
* A fullscreen-exclusive application owns the display outright and cannot be
  overlaid at all. Detecting that and saying so is the honest outcome; drawing a
  ring into a surface nobody will ever see is not.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import psutil
import pywintypes
import structlog
import win32api
import win32gui
import win32process

from mentor.models import AppContext, Rect

log = structlog.get_logger(__name__)

_DWMWA_EXTENDED_FRAME_BOUNDS = 9

_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000

_MONITOR_DEFAULTTONEAREST = 2

_ENUM_CHILD_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

#: ``SHQueryUserNotificationState`` result meaning a Direct3D application owns
#: the display in exclusive fullscreen mode.
_QUNS_RUNNING_D3D_FULL_SCREEN = 3


class _MonitorInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    )


# --- Identity ----------------------------------------------------------------


def foreground_hwnd() -> int | None:
    """The window handle with focus, or None if nothing qualifies."""
    hwnd = win32gui.GetForegroundWindow()
    return int(hwnd) if hwnd else None


def is_window(hwnd: int) -> bool:
    """False once the window has been closed."""
    return bool(ctypes.windll.user32.IsWindow(wintypes.HWND(hwnd)))


def is_minimised(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsIconic(wintypes.HWND(hwnd)))


def is_visible(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsWindowVisible(wintypes.HWND(hwnd)))


def window_title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd) or ""
    except pywintypes.error as exc:
        log.debug("window_title_failed", hwnd=hwnd, error=str(exc))
        return ""


def process_for_window(hwnd: int) -> tuple[int, str, str | None]:
    """The (pid, process name, executable path) owning a window.

    Falls back to an empty name rather than raising: a window whose process has
    already exited is an ordinary race, not an error.
    """
    try:
        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
    except pywintypes.error as exc:
        log.debug("window_process_lookup_failed", hwnd=hwnd, error=str(exc))
        return (0, "", None)

    try:
        proc = psutil.Process(pid)
        return (pid, proc.name().lower(), proc.exe())
    except (psutil.Error, OSError) as exc:
        log.debug("process_identity_failed", pid=pid, error=str(exc))
        return (pid, "", None)


def executable_version(path: str | None) -> str | None:
    """The file version of an executable, as ``1.2.3.4``, or None if it has none."""
    if not path:
        return None
    try:
        info = win32api.GetFileVersionInfo(path, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
    except (pywintypes.error, KeyError, OSError) as exc:
        log.debug("executable_version_unavailable", path=path, error=str(exc))
        return None
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


# --- Geometry ----------------------------------------------------------------


def window_rect_raw(hwnd: int) -> Rect | None:
    """``GetWindowRect``: includes the invisible drop-shadow border.

    Kept only so the difference against the true frame can be logged. Do not
    position anything with this.
    """
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def window_frame_bounds(hwnd: int) -> Rect | None:
    """The window's true visible bounds in physical screen pixels.

    Uses ``DWMWA_EXTENDED_FRAME_BOUNDS``, falling back to ``GetWindowRect`` only
    when DWM refuses -- which it does for some windows, and which is logged,
    because the fallback is several pixels wrong on every edge.
    """
    rect = wintypes.RECT()
    try:
        hresult = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(_DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
    except OSError as exc:
        log.warning("dwm_frame_bounds_error", hwnd=hwnd, error=str(exc))
        hresult = -1

    if hresult == 0:
        return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

    raw = window_rect_raw(hwnd)
    log.warning("dwm_frame_bounds_unavailable", hwnd=hwnd, hresult=hresult, falling_back_to=raw)
    return raw


def child_windows_of_class(hwnd: int, class_name: str) -> list[int]:
    """Handles of descendant windows with the given class name.

    Needed for UWP applications: ``ApplicationFrameHost`` owns the frame, while
    the actual content lives in a ``Windows.UI.Core.CoreWindow`` child belonging
    to a different process. Walking only the frame finds the three caption
    buttons and nothing else.
    """
    found: list[int] = []

    def _callback(child: int, _lparam: int) -> bool:
        buffer = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(child, buffer, 256)
        if buffer.value == class_name:
            found.append(int(child))
        return True

    try:
        ctypes.windll.user32.EnumChildWindows(wintypes.HWND(hwnd), _ENUM_CHILD_PROC(_callback), 0)
    except OSError as exc:
        log.debug("child_window_enumeration_failed", hwnd=hwnd, error=str(exc))
    return found


def monitor_bounds_for_window(hwnd: int) -> Rect | None:
    """The physical bounds of the monitor the window is mostly on."""
    monitor = ctypes.windll.user32.MonitorFromWindow(wintypes.HWND(hwnd), _MONITOR_DEFAULTTONEAREST)
    if not monitor:
        return None
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(_MonitorInfo)
    if not ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    r = info.rcMonitor
    return Rect(r.left, r.top, r.right - r.left, r.bottom - r.top)


def covers_its_monitor(hwnd: int) -> bool:
    """True if the window's frame exactly fills the monitor it is on."""
    bounds = window_frame_bounds(hwnd)
    monitor = monitor_bounds_for_window(hwnd)
    if bounds is None or monitor is None:
        return False
    return bounds == monitor


def has_chrome(hwnd: int) -> bool:
    """True if the window has a caption bar or a resize frame.

    A borderless window filling its monitor is the shape a fullscreen game takes.
    """
    style = ctypes.windll.user32.GetWindowLongW(wintypes.HWND(hwnd), _GWL_STYLE)
    return bool(style & (_WS_CAPTION | _WS_THICKFRAME))


def d3d_fullscreen_active() -> bool:
    """True if *some* application holds the display in exclusive fullscreen.

    ``SHQueryUserNotificationState`` is process-wide, not per-window: it answers
    "is anything fullscreen right now", so it has to be combined with a check
    that the window in question is the one filling the screen.
    """
    state = ctypes.c_int()
    try:
        hresult = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
    except OSError as exc:
        log.debug("notification_state_unavailable", error=str(exc))
        return False
    return hresult == 0 and state.value == _QUNS_RUNNING_D3D_FULL_SCREEN


def is_fullscreen_exclusive(hwnd: int) -> bool:
    """True if this window cannot be overlaid because it owns the display.

    Deliberately conservative. A false positive means refusing to help someone we
    could have helped; a false negative means drawing into the void and telling
    them to click something they cannot see.
    """
    if not covers_its_monitor(hwnd):
        return False
    if d3d_fullscreen_active():
        return True
    # A borderless window filling its monitor is the other common shape. Maximised
    # ordinary windows keep their caption and resize frame, so they are excluded.
    return not has_chrome(hwnd)


# --- The assembled context ---------------------------------------------------


def app_context(hwnd: int) -> AppContext | None:
    """Everything Mentor knows about one window, or None if it is not usable.

    "Not usable" includes windows with no area. Opening the Start menu briefly
    makes a zero-sized ``explorer.exe`` window the foreground window, and passing
    that on produced a capture of nothing and a grounding failure further down.
    There is nothing on a 0x0 window to point at, so it is rejected here rather
    than allowed to fail somewhere less obvious.
    """
    if not is_window(hwnd):
        return None
    bounds = window_frame_bounds(hwnd)
    if bounds is None:
        return None
    if bounds.w <= 0 or bounds.h <= 0:
        log.debug("window_has_no_area", hwnd=hwnd, bounds=bounds)
        return None

    _pid, process_name, exe_path = process_for_window(hwnd)
    return AppContext(
        process_name=process_name,
        window_title=window_title(hwnd),
        version=executable_version(exe_path),
        hwnd=hwnd,
        bounds=bounds,
        is_fullscreen_exclusive=is_fullscreen_exclusive(hwnd),
    )


def focused_app_context() -> AppContext | None:
    """The focused window as an AppContext, or None if there isn't one."""
    hwnd = foreground_hwnd()
    if hwnd is None:
        return None
    context = app_context(hwnd)
    if context is not None:
        raw = window_rect_raw(hwnd)
        log.info(
            "window_focused",
            process=context.process_name,
            version=context.version,
            title=context.window_title,
            hwnd=hwnd,
            frame_bounds=context.bounds,
            raw_rect=raw,
            shadow_inset=_shadow_inset(raw, context.bounds),
            fullscreen_exclusive=context.is_fullscreen_exclusive,
        )
    return context


def _shadow_inset(raw: Rect | None, frame: Rect) -> tuple[int, int, int, int] | None:
    """How far the drop shadow extends past the real frame, for the log.

    If this is ever (0, 0, 0, 0) on a normal window, the DWM call silently fell
    back and every highlight is about to be a few pixels out.
    """
    if raw is None:
        return None
    return (frame.x - raw.x, frame.y - raw.y, raw.right - frame.right, raw.bottom - frame.bottom)
