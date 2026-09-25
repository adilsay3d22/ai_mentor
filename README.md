# Mentor

A desktop assistant for Windows that shows you where to click — in the application
you already have open, on your actual screen.

You press a hotkey, type what you want to do, and Mentor draws a ring around the
next thing to click. You click it. Mentor looks again and rings the next thing.

**It points. It never clicks.** Mentor cannot control the applications it guides
you through — it only reads pixels and draws on top of them. That is a product
decision as much as a technical one: it means the tool teaches you where things
are instead of doing the task while you watch, and it means there is no category
of bug where it clicks the wrong thing on your behalf.

> **Status: in development.** Phases 0–5 of 10 are built. It works end to end —
> it will genuinely ring the right control — but it has no verification step yet,
> so you advance manually with a key. See [Status](#status).

---

## The problem

Learning unfamiliar software is slow because the gap between *knowing what you
want* and *knowing where it lives in the interface* is enormous.

Video tutorials solve this badly: they're linear, they're for someone else's
version and layout, and you spend the whole time pausing and hunting for a panel
that has moved. A text answer from an AI has the same gap — "go to Filter → Blur
→ Gaussian Blur" is only useful if you can find the Filter menu.

Mentor closes that gap by pointing at the pixel.

---

## How it works

Four layers, one step at a time:

```
  hotkey ──▶ you type a goal
                  │
                  ▼
        ┌───────────────────┐
        │ 1. capture        │  the focused window only, never the desktop
        └─────────┬─────────┘
                  ▼
        ┌───────────────────┐
        │ 2. ground         │  what is actually on screen right now?
        │    UIA  ~20ms     │  the accessibility tree, when the app has one
        │    OCR  ~500ms    │  local neural net, for everything else
        └─────────┬─────────┘
                  ▼
        ┌───────────────────┐
        │ 3. plan           │  "given these 43 things, which one moves us
        │                   │   toward the goal?"  → one step
        └─────────┬─────────┘
                  ▼
        ┌───────────────────┐
        │ 4. point          │  a click-through ring on the real application
        └───────────────────┘
                  │
                  ▼
         you click it, and it starts again
```

**The framing in step 3 is the whole trick.** The planner is never asked "where
is the Gaussian Blur filter". It is asked "given these forty things that are
definitely on screen right now, which one moves us toward the goal". That
reframing is what lets Mentor work on software the model has never seen.

It also means hallucinated menu items are structurally impossible to render: a
step naming an element the grounder did not report is rejected, re-requested once
with the violation explained, and then failed honestly. Pointing confidently at
the wrong thing is the worst output this tool can produce, so it is the one
outcome the design refuses.

---

## Privacy

Mentor watches your screen, so it should be precise about what that means.

**Only one thing in Mentor ever touches the network**, and it is the step
planner. Everything else runs locally:

| Layer | Where it runs |
|---|---|
| Screen capture | local (`mss`) |
| Accessibility tree | local (Win32 UI Automation) |
| Text recognition | **local** — PP-OCRv6 ONNX models on CPU, bundled, no download |
| Fusion, overlay, state machine | local |
| Step planning | remote — *or* local, see `--offline` |

When the planner does run, **what leaves the machine is text** — the element
labels, your goal, and the application name. The screenshot never leaves the
process.

Also true by design:

- **Focused window only.** There is no "capture the whole desktop" call in the
  codebase, on purpose.
- **A visible capture indicator.** The tray dot lights whenever a frame is taken,
  and stays lit long enough to actually see.
- **A blocklist.** Password managers are refused by default and never captured.
- **Nothing written to disk** outside `./debug/`, which is gitignored.
- **No keyboard hook.** The global hotkey uses `RegisterHotKey`, which claims one
  key combination and sees nothing else — not the `keyboard` package, which hooks
  every keystroke on the machine.

---

## Install

Windows 11, Python 3.11+.

```bash
git clone https://github.com/adilsay3d22/ai_mentor.git
cd ai_mentor
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

`uv` works too if you have it (`uv sync`), but the project currently runs on a
plain `pip` + `.venv` setup.

---

## Run

### Without an API key

```bash
.\.venv\Scripts\mentor.exe --offline
```

A keyword-matching planner stands in for the model. It cannot reason, so it picks
well only when the words happen to line up — but capture, grounding, validation,
the overlay, the hotkey and the state machine are all real. Good for seeing the
thing work, and for development.

### With an API key

Copy `.env.example` to `.env` and fill in one key:

```
OPENROUTER_API_KEY=sk-or-...
```

Then:

```bash
.\.venv\Scripts\mentor.exe
```

Press **`Ctrl+Shift+Space`** over any application, type what you want to do, and
press Enter. **`Ctrl+Shift+Right`** shows the next step. **`Esc`** dismisses.

### Planner backends

Three, behind one `Protocol`, so they are interchangeable:

| Flag | Backend | Cost |
|---|---|---|
| `--provider openrouter` | OpenRouter (default) | free model by default |
| `--provider anthropic` | Anthropic Messages API | ~2¢ per step on Opus 5 |
| `--offline` | keyword matching | none, no key, no network |

The OpenRouter default is a **free** model deliberately — a paid default means
one forgotten flag turns a test run into a bill. `--model` overrides it:

```bash
.\.venv\Scripts\mentor.exe --model anthropic/claude-opus-5
```

Free models scored on four goals against a five-element list, most recently
measured:

| Model | Correct | Median |
|---|---|---|
| `nex-agi/nex-n2.5-mini:free` *(default)* | 4/4 | 2.0s |
| `liquid/lfm-2.5-2.6b:free` | 4/4 | 3.0s |
| `nex-agi/nex-n2.5-pro:free` | 4/4 | 3.2s |
| `dots-studio/dots-3-note-preview:free` | 4/4 | 3.9s |

Free models share an upstream pool, so availability moves hour to hour. If one is
rate-limited, Mentor tells you and names an alternative.

### Other modes

```bash
.\.venv\Scripts\mentor.exe --debug          # HUD: draw every grounded element
.\.venv\Scripts\mentor.exe --list-screens   # monitor geometry as Mentor sees it
.\.venv\Scripts\mentor.exe --ring --at 0,0,200,100   # static ring at a coordinate
```

`--debug` is the most useful tool in the project. When a highlight lands wrong,
it tells you whether the grounder found the wrong box or the planner picked the
wrong element.

---

## Status

Built in phase order, each verified before the next, so that when a highlight
lands in the wrong place it is clear which layer lied.

| Phase | | |
|---|---|---|
| 0 | Skeleton | ✅ |
| 1 | Click-through overlay, DPI-correct coordinates | ✅ verified at 100% and 150% |
| 2 | Window tracking, true frame bounds | ✅ verified |
| 3 | Capture, OCR, debug HUD, tray indicator | ✅ verified |
| 4 | Accessibility fast path, fused with OCR | ✅ |
| 5 | Planner, hotkey, input box, state machine | working, pending review |
| 6 | Verification and auto-advance | — |
| 7 | Vision grounding (icons) | — |
| 8 | Application memory (SQLite) | — |
| 9 | Unknown applications | — |
| 10 | Hardening and packaging | — |

**204 tests, all offline.** No test calls a live API, and the capture and
grounding layers are tested against saved screenshots of real applications.

### Known limitations

- **Latency misses its target.** ~4.8s from goal to highlight against a 3s
  budget, and OCR is two thirds of it. The grounding chain is meant to stop when
  confident and currently never does — Notepad returns 38 usable accessibility
  elements before OCR is asked for anything. That is the next fix.
- **No verification yet.** Mentor does not know whether you did the step; you
  advance manually. Phase 6.
- **Icon-only controls are hit and miss.** OCR reads text, and the accessibility
  tree only helps when an app exposes one. Phase 7.
- **Multi-monitor is tested but not witnessed.** The coordinate maths is covered
  by offline tests including negative-origin monitors; it has not been run on
  real second-monitor hardware.
- **Fullscreen-exclusive apps cannot be overlaid at all.** Mentor detects this
  and says so rather than drawing into a surface nobody will see.

---

## Development

```bash
.\.venv\Scripts\python.exe -m pytest              # all tests
.\.venv\Scripts\python.exe -m pytest -m offline   # no model, no API, no screen
.\.venv\Scripts\ruff.exe check . ; .\.venv\Scripts\ruff.exe format .
.\.venv\Scripts\python.exe -m mentor.tools.grab NAME   # save a test fixture
```

Four documents carry the design, and they are worth reading before changing
anything:

- **`CLAUDE.md`** — the working agreement, the non-negotiable invariants, and a
  list of hard-won traps that cost real debugging time
- **`architecture.md`** — module layout, the three coordinate spaces, and the
  decisions on record
- **`requirement.md`** — what it must do, and what is out of scope
- **`design.md`** — how it looks and how the planner is prompted
- **`phases.md`** — build order and exit criteria, with results recorded

### Layout

```
mentor/
  capture/     windows, coordinates, screen grabbing
  grounding/   UIA, OCR, fusion — what is on screen
  planner/     three backends, prompts, validation
  overlay/     the click-through window and its painters
  session/     the state machine — the only orchestrator
  ui/          hotkey, input box, debug HUD, tray
```

---

## Licence

Not yet chosen.
