"""Screen capture, behind a protocol so the backend can be swapped or faked.

Two backends, both taking a physical-screen rectangle and returning a
:class:`~mentor.models.Frame` that knows where on the desktop it came from.

SR2 is the reason everything here takes an explicit region: Mentor captures the
focused window, never the whole desktop. There is no "grab everything" call in
this module on purpose.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

import numpy as np
import structlog
from mss.exception import ScreenShotError

from mentor.models import Frame, Rect

log = structlog.get_logger(__name__)


@runtime_checkable
class Grabber(Protocol):
    """Anything that can turn a screen rectangle into pixels."""

    name: str

    def grab(self, region: Rect) -> Frame | None:
        """Capture ``region`` in physical screen pixels, or None if it failed."""
        ...

    def close(self) -> None: ...


class MssGrabber:
    """Capture via mss. Slower than dxcam but correct everywhere.

    Handles negative coordinates and multiple monitors without being told which
    output a rectangle belongs to, which is what makes it the safe default.
    """

    name = "mss"

    def __init__(self) -> None:
        import mss

        self._mss = mss.mss()

    def grab(self, region: Rect) -> Frame | None:
        if region.w <= 0 or region.h <= 0:
            log.warning("grab_empty_region", region=region)
            return None
        try:
            shot = self._mss.grab(
                {"left": region.x, "top": region.y, "width": region.w, "height": region.h}
            )
        except (ScreenShotError, OSError) as exc:
            log.error("mss_grab_failed", region=region, error=str(exc))
            return None

        # mss hands back BGRA; drop alpha without copying the whole thing twice.
        image = np.asarray(shot, dtype=np.uint8)[:, :, :3]
        return Frame(image=image, bounds=region, captured_at=time.monotonic(), source=self.name)

    def close(self) -> None:
        self._mss.close()


class DxcamGrabber:
    """Capture via dxcam (Desktop Duplication). Fast, but per-output and fussy.

    dxcam addresses one display output at a time and returns coordinates relative
    to that output, so a region has to be translated out of virtual-desktop space
    first. It also returns None when the frame has not changed since the last
    grab, which is sensible for video capture and a trap for one-shot use.
    """

    name = "dxcam"

    def __init__(self, output_idx: int = 0) -> None:
        import dxcam

        self._camera = dxcam.create(output_idx=output_idx, output_color="BGR")
        if self._camera is None:
            raise RuntimeError(f"dxcam has no output {output_idx}")
        left, top, right, bottom = self._camera.region
        self._output = Rect(left, top, right - left, bottom - top)

    def grab(self, region: Rect) -> Frame | None:
        if region.w <= 0 or region.h <= 0:
            log.warning("grab_empty_region", region=region)
            return None
        if not self._output.intersects(region):
            log.debug("dxcam_region_off_output", region=region, output=self._output)
            return None

        local = (
            region.x - self._output.x,
            region.y - self._output.y,
            region.right - self._output.x,
            region.bottom - self._output.y,
        )
        try:
            image = self._camera.grab(region=local)
        except Exception as exc:  # dxcam surfaces raw DirectX errors
            log.error("dxcam_grab_failed", region=region, error=str(exc))
            return None

        if image is None:
            # Nothing changed since the last call. Not an error, but the caller
            # asked for pixels and got none, so it has to be told.
            return None
        return Frame(image=image, bounds=region, captured_at=time.monotonic(), source=self.name)

    def close(self) -> None:
        self._camera.release()


def create_grabber(prefer: str = "mss") -> Grabber:
    """Build a grabber, falling back to mss if the preferred backend will not start.

    Default is mss rather than dxcam, which reverses what ``architecture.md``
    assumed. That reversal is measured, not guessed: over 30 grabs of a 900x700
    region on this machine, mss returned a frame every time at a median of 6.9ms,
    while dxcam returned None on 29 of 30 because Desktop Duplication only hands
    over a frame when the screen has *changed*. That is exactly Mentor's normal
    case -- a window the user is looking at and not touching -- so dxcam would
    mean no pixels precisely when we need them. 6.9ms is also negligible beside
    the ~600ms OCR pass that follows it.

    dxcam stays implemented and selectable behind the same protocol, for a phase
    that genuinely needs a sustained high frame rate and can cope with change-only
    delivery. Pass ``prefer="dxcam"`` to try it.
    """
    if prefer == "dxcam":
        try:
            grabber = DxcamGrabber()
            log.info("grabber_created", backend=grabber.name)
            return grabber
        except Exception as exc:
            log.warning("dxcam_unavailable", error=str(exc), falling_back_to="mss")

    grabber = MssGrabber()
    log.info("grabber_created", backend=grabber.name)
    return grabber
