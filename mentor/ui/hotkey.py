"""A global hotkey, registered with the OS rather than hooked.

``architecture.md`` lists the ``keyboard`` package for this. I have used
``RegisterHotKey`` instead, and the reason is worth stating: ``keyboard``
installs a low-level hook that sees *every* keystroke on the machine, which is
the mechanism a keylogger uses. Mentor's whole pitch is that it never touches
your input (SR1, invariant 1), and shipping something that reads all of it --
even benignly -- undercuts that in a way no amount of documentation repairs.

``RegisterHotKey`` asks the OS to notify us about exactly one combination and
tells us nothing else. It also removes a dependency. The cost is that the
combination must be free system-wide; if another application already owns it,
registration fails and the user is told, rather than the hotkey silently never
firing.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Final

import structlog
from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

log = structlog.get_logger(__name__)

_WM_HOTKEY: Final[int] = 0x0312

_MOD_ALT: Final[int] = 0x0001
_MOD_CONTROL: Final[int] = 0x0002
_MOD_SHIFT: Final[int] = 0x0004
_MOD_WIN: Final[int] = 0x0008
#: Without this, holding the combination repeats it many times a second.
_MOD_NOREPEAT: Final[int] = 0x4000

_MODIFIER_NAMES: Final[dict[str, int]] = {
    "alt": _MOD_ALT,
    "ctrl": _MOD_CONTROL,
    "control": _MOD_CONTROL,
    "shift": _MOD_SHIFT,
    "win": _MOD_WIN,
    "super": _MOD_WIN,
}

#: Virtual-key codes for the non-modifier keys Mentor's defaults need. Extended
#: deliberately narrowly: a wrong code binds the wrong key silently.
_VIRTUAL_KEYS: Final[dict[str, int]] = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "right": 0x27,
    "left": 0x25,
    "up": 0x26,
    "down": 0x28,
    **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
    **{chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)},
    **{str(digit): ord(str(digit)) for digit in range(10)},
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}


class HotkeyError(RuntimeError):
    """The combination could not be registered."""


def parse(combination: str) -> tuple[int, int]:
    """``"ctrl+shift+space"`` -> (modifier mask, virtual key code)."""
    parts = [part.strip().lower() for part in combination.split("+") if part.strip()]
    if not parts:
        raise HotkeyError(f"empty hotkey {combination!r}")

    modifiers = 0
    key: int | None = None
    for part in parts:
        if part in _MODIFIER_NAMES:
            modifiers |= _MODIFIER_NAMES[part]
        elif part in _VIRTUAL_KEYS:
            if key is not None:
                raise HotkeyError(f"{combination!r} names more than one non-modifier key")
            key = _VIRTUAL_KEYS[part]
        else:
            raise HotkeyError(f"unknown key {part!r} in {combination!r}")

    if key is None:
        raise HotkeyError(f"{combination!r} has no non-modifier key")
    return (modifiers | _MOD_NOREPEAT, key)


class HotkeyManager(QObject, QAbstractNativeEventFilter):
    """Registers hotkeys and turns WM_HOTKEY into a Qt signal.

    Install with ``app.installNativeEventFilter(manager)`` before registering.
    """

    triggered = Signal(str)  # the combination that fired

    def __init__(self, parent: QObject | None = None) -> None:
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self._by_id: dict[int, str] = {}
        self._next_id = 1

    def register(self, combination: str) -> bool:
        """Claim ``combination`` system-wide. False if something else owns it."""
        if sys.platform != "win32":
            log.warning("hotkeys_unsupported_platform", platform=sys.platform)
            return False

        modifiers, key = parse(combination)
        hotkey_id = self._next_id
        try:
            ok = ctypes.windll.user32.RegisterHotKey(
                None, hotkey_id, wintypes.UINT(modifiers), wintypes.UINT(key)
            )
        except OSError as exc:
            log.error("hotkey_registration_error", combination=combination, error=str(exc))
            return False

        if not ok:
            log.error(
                "hotkey_already_taken",
                combination=combination,
                error=ctypes.get_last_error(),
                detail="another application owns this combination",
            )
            return False

        self._by_id[hotkey_id] = combination
        self._next_id += 1
        log.info("hotkey_registered", combination=combination, id=hotkey_id)
        return True

    def unregister_all(self) -> None:
        if sys.platform != "win32":
            return
        for hotkey_id in list(self._by_id):
            ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)
        self._by_id.clear()

    def nativeEventFilter(self, event_type: object, message: object) -> tuple[bool, int]:
        """Qt hands every native message through here. Claim only WM_HOTKEY."""
        if not self._by_id:
            return (False, 0)
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
        except (TypeError, ValueError):
            return (False, 0)

        if msg.message == _WM_HOTKEY:
            combination = self._by_id.get(int(msg.wParam))
            if combination is not None:
                log.info("hotkey_fired", combination=combination)
                self.triggered.emit(combination)
                return (True, 0)
        return (False, 0)
