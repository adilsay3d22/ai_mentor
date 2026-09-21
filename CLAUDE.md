# CLAUDE.md — Working agreement for Mentor

Read this first, every session. Then read `architecture.md` before touching code,
and `phases.md` to find out what we're currently allowed to build.

## What this project is

Mentor is a desktop AI teaching assistant for Windows. The user asks it how to do
something in whatever application is currently open. Mentor looks at the screen,
figures out the next single step, and draws a transparent overlay on top of the
real application showing where to click. The user clicks it themselves. Mentor
then re-checks the screen and shows the next step.

It is a **pointer**, not an agent. See invariants.

## Non-negotiable invariants

Violating any of these is a bug even if tests pass.

1. **Mentor never controls the target application.** It never synthesises mouse
   clicks, key presses, or window messages into another process. It only reads
   pixels and draws on top. No `SendInput`, no `PostMessage`, no `pyautogui.click`.
   The only input Mentor handles is its own UI.
2. **The overlay is always click-through.** If the user cannot click the thing
   Mentor is pointing at, Mentor is broken.
3. **The grounder is the authority on what exists.** The planner may only choose
   from elements the grounder actually found on screen this frame. A step that
   names a target the grounder did not return must be rejected and re-planned,
   never rendered.
4. **All internal coordinates are physical screen pixels**, origin at the primary
   monitor's top-left, as a `Rect` or `Point`. Convert at the boundaries only.
   Never pass a logical/DPI-scaled coordinate deeper than the capture layer.
5. **One step on screen at a time.** Never render a multi-step path.
6. **Screenshots are never written to disk outside `./debug/`**, and `./debug/` is
   gitignored. Nothing is uploaded to any API unless that call is on the path of
   a step the user explicitly asked for.

## Tech baseline

- Windows 11, Python 3.11+
- PySide6 for the overlay and all UI. No Electron, no web stack in v1.
- Qt event loop is the main loop. Background work runs in `QThread` workers that
  communicate via Qt signals. **Do not introduce asyncio.** Mixing asyncio with
  the Qt loop is the single biggest source of avoidable bugs in this design.
- `uv` for dependency management (`uv add`, `uv run`). **This machine has no `uv`
  and only Python 3.14**, so the project currently runs on a `pip` + `.venv`
  fallback: every `uv run X` below is `.\.venv\Scripts\X` in practice. PySide6
  6.11, pywin32 312, psutil 7.2, opencv 5.0, onnxruntime 1.30 and rapidocr 3.9
  all have 3.14 wheels. `rapidocr-onnxruntime` does **not** — upstream renamed it
  to `rapidocr` at 2.0 and the old name caps at Python <3.13.

## Code conventions

- Type hints on every function signature. `from __future__ import annotations`.
- Dataclasses for all data models (`Element`, `Step`, `AppContext`, …). They live
  in `mentor/models.py` and nowhere else. Never redefine them locally.
- Every layer is behind a `Protocol` so it can be swapped and faked in tests.
  Grounding in particular has three implementations; code against the protocol.
- No bare `except:`. No silently swallowed exceptions in the capture loop — log
  and surface to the session state machine.
- Logging via `structlog` to `./debug/mentor.log`. Log every planner request and
  response verbatim; you will need them.
- Format with `ruff format`, lint with `ruff check`.

## Testing rules

- The capture, grounding and verification layers are testable **offline** using
  saved screenshots in `tests/fixtures/screens/`. Any bug found in those layers
  gets a fixture added before it gets a fix.
- The planner is tested against recorded element lists with a fake LLM client.
  Never call a live API in a test.
- The overlay cannot be meaningfully unit-tested. It gets a manual checklist in
  `phases.md` instead. Be honest about this rather than writing fake coverage.

## Working style I want from you

- **Work one phase at a time.** Check `phases.md`, do the current phase, stop at
  its exit criteria, and tell me. Don't build ahead — a half-finished phase 5 on
  top of an unverified phase 2 is worse than nothing here, because when the arrow
  lands in the wrong place I need to know which layer lied.
- When a phase's exit criteria involve me looking at the screen, stop and say so
  explicitly. Don't mark it done yourself.
- Prefer boring, debuggable code over clever code. This project's difficulty is
  in coordinate math and flaky vision, not in abstractions.
- If you think one of the decisions in `architecture.md` is wrong, say so before
  implementing around it. Don't silently substitute a different approach.
- Ask me before adding any dependency not listed in `architecture.md`.

## Commands

```
uv run mentor              # launch the app
uv run mentor --debug      # launch with the debug HUD (element boxes drawn)
uv run mentor --offline    # keyword-matching planner: no API key, no cost,
                           # no intelligence. Exercises the whole loop.
uv run mentor              # default: OpenRouter + a free model, no charge
uv run mentor --model anthropic/claude-opus-5    # same key, paid, better steps
uv run pytest              # tests
uv run pytest -m offline   # tests that need no model or API
uv run ruff check . && uv run ruff format .
uv run python -m mentor.tools.grab   # save a screenshot fixture of the focused window
```

## Known traps — do not rediscover these

- Declare DPI awareness **before** creating the QApplication and before any
  capture, or every coordinate on a scaled display is silently wrong. The call is
  `ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)`. It is *not*
  `shcore.SetProcessDpiAwareness(2)` — that constant is named
  PROCESS_PER_MONITOR_DPI_AWARE but sets per-monitor **v1**, and since awareness
  can only be set once per process it also locks Qt out of the v2 context it
  wants, which is what produces the `SetProcessDpiAwarenessContext() failed:
  Access is denied` warning on startup. Keep the shcore call only as a fallback
  for Windows older than 1703. See `mentor/__main__.py:set_dpi_awareness`.
- `GetWindowRect` includes the invisible drop-shadow border on Windows 10+. Use
  `DwmGetWindowAttribute` with `DWMWA_EXTENDED_FRAME_BOUNDS` for the real bounds.
- A second monitor to the left of the primary gives **negative** screen X. Never
  assume coordinates are positive.
- `QScreen.name()` on Qt 6 is the monitor's *friendly* EDID name ("LG ULTRAGEAR"),
  not the GDI device name, and friendly names are not unique across identical
  monitors. Do not try to match Qt screens to Win32 monitors by name. The overlay
  measures instead: a window covering one screen, then `GetWindowRect` on it,
  gives that screen's physical rectangle exactly. See `capture/coords.py`.
- Qt only ever reports **logical** geometry. There is no Qt API for a monitor's
  physical origin, which is why the measurement above exists.
- Qt's `WA_TransparentForMouseEvents` is not the same as the window-level
  `WindowTransparentForInput` flag. The overlay needs the window flag; the widget
  attribute alone leaves the window grabbing clicks.
- Fullscreen exclusive apps (games, some video editors) cannot be overlaid at all.
  Detect and tell the user rather than drawing into the void.
- **The overlay is captured unless you exclude it.** Mentor draws on top of the
  target window, then captures that window, then grounds the capture — so it
  reads its own boxes and captions back as if they were part of the application.
  Measured: a static Notepad went 18 → 23 → 28 → 32 elements over four passes.
  Call `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)` (0x11) on every
  overlay window; it stays visible to the user and vanishes from capture.
  Windows 10 2004+. See `overlay/window.py:_exclude_from_capture`.
- RapidOCR detects a *line*, so a menu bar arrives as one string,
  `"File Edit View"`. Pass `return_word_box=True` and regroup the words by the
  gap between them — see `grounding/ocr.py:group_words`.
- dxcam returns `None` when the screen has not changed. That is Mentor's normal
  case — a window the user is looking at and not touching — so the default
  grabber is mss. Measured: mss 30/30 frames at 6.9ms median, dxcam 1/30.
- A UWP application (Settings, Calculator, anything hosted by
  `ApplicationFrameHost`) is **two** windows. The frame has a UIA tree of three
  caption buttons; everything real lives in a `Windows.UI.Core.CoreWindow` child
  owned by a *different process*. Measured on Settings: 3 controls from the
  frame, 81 from the child. Walk both — see
  `grounding/uia.py:UiaGrounder.ground_window`.
- UIA names icon-font controls with private-use-area characters (`""` from
  Segoe Fluent Icons). Same problem as OCR misreading a close button as `×`, same
  filter: `grounding/base.py:is_meaningful_label`.
- UIA reports one control twice — "Get help" arrives as a button *and* as the
  text inside it, at almost the same rectangle. Fusion's dedupe prefers the
  actionable kind, since the planner should be offered the thing that can be
  clicked, not its caption.
- The global hotkey uses `RegisterHotKey` via ctypes, **not** the `keyboard`
  package. `keyboard` installs a low-level hook that sees every keystroke on the
  machine — the mechanism a keylogger uses — which sits badly beside SR1 and
  invariant 1. `RegisterHotKey` claims exactly one combination and sees nothing
  else. It needs a `QAbstractNativeEventFilter` to catch `WM_HOTKEY` (0x0312),
  and `MOD_NOREPEAT` or holding the keys fires it many times a second.
- There are three planner backends behind one `LlmClient` Protocol:
  `client.py` (Anthropic Messages API), `openrouter.py` (OpenAI
  chat-completions format — OpenRouter does **not** speak Anthropic's format,
  so it cannot be the `anthropic` SDK with a different base URL), and
  `offline.py` (keyword matching, no key). All three go through the same
  `plan_next_step`, so invariant 3 applies to every one of them.
- The planner uses structured outputs (`output_config.format` with a JSON
  schema), not free text. That makes malformed JSON impossible, but it cannot
  enforce invariant 3 — the schema has no way to know which element ids exist
  this frame. That check is code, in `planner/planner.py:validate`.
