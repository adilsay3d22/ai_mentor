"""Save a screenshot of the focused window as a test fixture.

    python -m mentor.tools.grab [name] [--countdown 4]

Writes ``tests/fixtures/screens/<name>.png`` plus a ``.json`` sidecar recording
the window's identity and its physical bounds. The sidecar is what makes the
fixture usable offline: without the bounds, an image cannot be turned back into
screen coordinates, and the grounding tests would be checking image-local numbers
that mean nothing.

These are the one exception to SR5 -- deliberate, named, committed fixtures
rather than incidental captures. Look at what is on screen before running it.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict

import structlog

from mentor import config
from mentor.__main__ import set_dpi_awareness

log = structlog.get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mentor.tools.grab", description=__doc__)
    parser.add_argument("name", help="fixture name, without extension")
    parser.add_argument(
        "--countdown",
        type=float,
        default=4.0,
        metavar="SECONDS",
        help="time to focus the window you want (default: 4)",
    )
    args = parser.parse_args(argv)

    set_dpi_awareness()
    config.configure_logging(debug=True)

    from mentor.capture import windows
    from mentor.capture.grabber import create_grabber

    print(f"Focus the window you want in {args.countdown:g}s...")
    time.sleep(args.countdown)

    context = windows.focused_app_context()
    if context is None:
        print("No focused window.")
        return 1
    if config.is_blocked(context.process_name):
        print(f"Refusing: {context.process_name} is on the SR4 blocklist.")
        return 2
    if context.is_fullscreen_exclusive:
        print("Refusing: that window owns the display and cannot be captured.")
        return 3

    grabber = create_grabber()
    try:
        frame = grabber.grab(context.bounds)
    finally:
        grabber.close()

    if frame is None:
        print("Capture failed. See ./debug/mentor.log.")
        return 4

    config.FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    image_path = config.FIXTURES_DIR / f"{args.name}.png"
    meta_path = config.FIXTURES_DIR / f"{args.name}.json"

    import cv2

    cv2.imwrite(str(image_path), frame.image)
    meta_path.write_text(
        json.dumps(
            {
                "process_name": context.process_name,
                "window_title": context.window_title,
                "version": context.version,
                "bounds": asdict(context.bounds),
                "capture_source": frame.source,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Saved {image_path}  ({frame.image.shape[1]}x{frame.image.shape[0]})")
    print(f"      {meta_path}")
    print(f"      from {context.process_name} — {context.window_title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
