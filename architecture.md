# Architecture — Mentor

## Shape of the system

Mentor is a single Python process running a Qt event loop. Everything that could
block — capture, OCR, vision, LLM calls — happens in `QThread` workers that emit
Qt signals back to the main thread. The main thread only ever does UI.

```
mentor/
  __main__.py          entry point, DPI awareness, QApplication
  models.py            ALL dataclasses live here
  config.py            settings, paths, blocklist
  session/
    controller.py      the state machine — the only place that orchestrates
    states.py          IDLE, PLANNING, SHOWING, WAITING, VERIFYING, DONE, FAILED
  capture/
    windows.py         Win32: focus tracking, window bounds, process identity
    grabber.py         dxcam/mss screen capture worker
    coords.py          coordinate space conversions — the only place that does
  grounding/
    base.py            Grounder protocol, Element construction, label filter
    uia.py             accessibility tree provider (fast path)
    ocr.py             RapidOCR text provider
    vision.py          OmniParser / VLM provider (fallback, most general)
    fusion.py          merges providers, dedupes, ranks
  planner/
    client.py          Anthropic client + the LlmClient protocol, retries, logging
    openrouter.py      OpenRouter client (OpenAI chat-completions format)
    prompts.py         system prompt + step schema
    planner.py         plan_next_step(goal, elements, history) -> Step
    offline.py         keyword-matching stand-in; no key, no network, no cost
  overlay/
    window.py          the transparent click-through window
    painters.py        ring, arrow, caption, dim-mask renderers
    tracker.py         keeps the overlay glued to the target window
  verify/
    change.py          SSIM / perceptual diff, did-the-screen-change
    judge.py           did the RIGHT thing change
  knowledge/
    appmap.py          SQLite cache of learned elements per app+version
    docs.py            documentation retrieval for unknown apps
  ui/
    hotkey.py          global hotkey via RegisterHotKey (no input hooking)
    prompt_box.py      the input box, and the instruction caption
    tray.py            system tray, capture indicator
    hud.py             debug HUD (--debug), and the QThread that runs a
                       capture+grounding pass off the UI thread
  tools/
    grab.py            save a screenshot fixture
```

## The loop

```
IDLE
 └─ hotkey → user types goal → PLANNING
PLANNING
 ├─ capture focused window
 ├─ ground elements (cache → uia → ocr → vision, stop when confident)
 ├─ plan_next_step(goal, elements, history)
 └─ step valid? → SHOWING : re-plan (max 2 retries) → FAILED
SHOWING
 ├─ overlay draws highlight on step.target
 └─ → WAITING
WAITING
 ├─ poll for screen change (2 fps is plenty)
 ├─ no change after 60s → gentle nudge, stay
 └─ change detected → VERIFYING
VERIFYING
 ├─ re-ground, judge whether the expected change happened
 ├─ expected → history.append(step); goal complete? DONE : PLANNING
 └─ unexpected → PLANNING (fresh, from current screen)
```

`session/controller.py` is the **only** module that moves between states. Nothing
else calls into the overlay or the planner directly.

## Data models (`models.py`)

```python
@dataclass(frozen=True)
class Point:   x: int; y: int              # physical screen pixels
@dataclass(frozen=True)
class Rect:    x: int; y: int; w: int; h: int

@dataclass(frozen=True)
class AppContext:
    process_name: str                      # "photoshop.exe"
    window_title: str
    version: str | None
    hwnd: int
    bounds: Rect                           # true frame bounds, shadow excluded
    is_fullscreen_exclusive: bool

@dataclass(frozen=True)
class Element:
    id: str                                # stable within a frame: "el_07"
    label: str                             # "Filter", "Lasso tool", ""
    kind: str                              # menu|button|icon|field|tab|panel|text
    bounds: Rect                           # physical screen pixels
    confidence: float
    source: str                            # uia|ocr|vision|cache

@dataclass(frozen=True)
class Step:
    action: str                            # click|open_menu|type|drag|scroll|wait|done
    target_id: str | None                  # MUST exist in the frame's elements
    instruction: str                       # shown to the user, one short sentence
    expect: str                            # what should change, for the verifier
    confidence: float

@dataclass
class SessionState:
    goal: str
    app: AppContext
    history: list[Step]
    current: Step | None
    attempts: int
```

## Coordinate spaces — read this twice

Three spaces exist. Confusing them is the number one source of "the circle is in
the wrong place":

1. **Physical screen space** — raw device pixels across the whole virtual desktop.
   Origin is the primary monitor's top-left. **Can be negative** on monitors
   positioned left of or above the primary. *This is Mentor's internal standard.*
2. **Window space** — physical pixels relative to the target window's top-left.
   Used when caching learned element positions, because they survive window moves.
3. **Qt logical space** — what Qt gives you for widget geometry, divided by the
   device pixel ratio of whichever screen the widget is on.

Rules:
- `capture/coords.py` owns every conversion. No other module does arithmetic
  across spaces.
- Everything in `models.py` is physical screen space.
- The overlay converts to Qt logical space at paint time, per-screen, using that
  screen's `devicePixelRatio`.
- `knowledge/appmap.py` stores window space, converts on read.

## Grounding strategy

Four providers behind one protocol, tried cheapest-first and fused:

| Provider | Cost | Coverage | When |
|---|---|---|---|
| `cache` | ~0 | Only what we've seen before in this app | Always first |
| `uia` | ~20ms empty, ~200ms rich | Native/WPF/WinForms/UWP | Always try; often returns nothing |
| `ocr` | ~150ms | Any text on screen | Always |
| `vision` | 0.5–3s | Icons, custom-drawn UI, anything | When the above miss the target |

`fusion.py` merges results, deduplicates overlapping boxes by IoU, prefers the
higher-confidence source, and assigns stable frame-local ids.

Crucially: **we do not always run vision.** We run it when the planner's intended
target isn't in the cheap results. This is what keeps NFR1 achievable and the API
bill survivable.

## Planner contract

The planner receives the goal, the history of completed steps, and the full list
of grounded elements with their ids and labels. It returns exactly one `Step` as
JSON. The system prompt states that `target_id` must be one of the supplied ids.

Validation in `planner.py`, not in the prompt:
- `target_id` must exist in this frame's element list, or reject.
- `action` must be in the allowed set, or reject.
- On reject: re-request once with the violation described, then fail the step and
  tell the user honestly.

This is the mechanism that kills hallucinated menu items. Do not weaken it.

## Verification

Two levels, because they fail differently:

- `change.py` — did *anything* change? Structural similarity (SSIM) on the
  downscaled window capture, threshold-tuned. Cheap, runs at 2 fps while waiting.
- `judge.py` — did the *expected* thing change? Re-ground and check whether
  `step.expect` is satisfied: a new dialog appeared, an element became active, a
  panel opened. Falls back to asking the model with before/after element lists
  when a rule doesn't cover it.

## Application memory

SQLite at `%LOCALAPPDATA%/Mentor/appmap.db`.

```sql
CREATE TABLE app (id INTEGER PRIMARY KEY, process_name TEXT, version TEXT);
CREATE TABLE element (
  id INTEGER PRIMARY KEY, app_id INTEGER, label TEXT, kind TEXT,
  win_x INT, win_y INT, win_w INT, win_h INT,   -- window space
  path TEXT,            -- "Filter > Blur > Gaussian Blur"
  seen_count INT, last_seen TIMESTAMP, confidence REAL
);
CREATE TABLE route (id INTEGER PRIMARY KEY, app_id INTEGER, goal TEXT, steps_json TEXT);
```

`route` is the payoff: once a goal has been completed successfully in an app, the
whole sequence is cached. The second person to ask "how do I export as PNG" gets
an instant first step.

Entries decay: if an element isn't found where cached three times running, its
confidence drops and eventually it's evicted. Applications get updated and move
things.

## Dependencies

Do not add anything outside this list without asking.

```
PySide6          overlay + all UI
dxcam            fast DirectX screen capture   (mss as fallback)
pywin32          window tracking, Win32 calls
uiautomation     accessibility tree provider
rapidocr + onnxruntime OCR   (was rapidocr-onnxruntime; upstream renamed it
                 at 2.0 and the old name caps at Python <3.13)
opencv-python    image diff, preprocessing
scikit-image     SSIM
numpy
anthropic        planner client
structlog        logging
pydantic         planner response validation
(none)           global hotkey -- RegisterHotKey via ctypes, not `keyboard`,
                 which hooks every keystroke on the machine and sits badly
                 beside SR1. See mentor/ui/hotkey.py.
psutil           process identity and version
pytest, ruff     dev
```

Optional, phase 6+: `omniparser` / a local VLM for icon grounding. Keep it behind
the `Grounder` protocol so it's a swap, not a refactor.

## Decisions on record

**Python + PySide6, not Electron + React.** A transparent click-through overlay is
four window flags in Qt and a native-module fight in Electron, and everything
downstream of capture is Python anyway. A React UI can be bolted on over a
WebSocket later once the hard parts work.

**Qt threads, not asyncio.** Mixing asyncio with the Qt event loop is a known
source of subtle bugs and buys us nothing here — our concurrency is a handful of
long-running workers, not thousands of sockets.

**Vision as fallback, not foundation.** Vision grounding is the only fully general
option, but it's also the slowest and priciest. Cheap providers first, vision when
they miss, cache the result so we miss less next time.

**Point, never click.** Removes an entire category of danger, makes the tool
actually teach rather than do, and sidesteps needing any input-injection
permissions. This is a product decision as much as a technical one.

**One step at a time.** We cannot pre-author reliable multi-step paths for
software we've never seen. Re-observing each step also gives error recovery for free.
