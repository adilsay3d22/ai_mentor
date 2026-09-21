# Phases — Mentor

Build in this order. Each phase has **exit criteria** that must be demonstrably
met before the next begins. Phases marked 👁 require me to look at the screen and
confirm — Claude cannot mark those done alone.

The ordering exists because when a highlight lands in the wrong place, the cause
is one of: wrong window bounds, wrong DPI conversion, wrong monitor, wrong
element, or wrong step. Building in this order means only one of those is ever
new.

---

## Phase 0 — Skeleton ✅ done
Project scaffold, no features.

- `uv` project, `mentor/` package, `__main__.py` launching an empty QApplication
- DPI awareness call in place, before QApplication
- `models.py` with all dataclasses from `architecture.md`
- `config.py`, `structlog` to `./debug/mentor.log`, `.gitignore` covering `debug/`
- `ruff` + `pytest` configured, one trivial passing test

**Exit:** `uv run mentor` starts and exits cleanly. `uv run pytest` passes.

*Met.* No `uv` on this machine, so it runs on `pip` + `.venv` — see the tech
baseline in `CLAUDE.md`.

---

## Phase 1 — The overlay 👁 ✅ verified
The hardest-looking part, first, because everything else is drawn through it.

- Frameless, transparent, always-on-top, **click-through** window
- Paints a hardcoded ring at a hardcoded physical-screen coordinate
- `coords.py` with physical ↔ Qt logical conversion, per-screen DPR
- Covers the full virtual desktop including negative-coordinate monitors

**Exit 👁:** Ring appears exactly where specified. I can click straight through it
into whatever is underneath. Correct on a scaled display and on a second monitor.
Verified by pointing it at a known screen position and checking by eye.

*Confirmed by eye at 100% and at 150% display scaling: ring lands exactly, clicks
pass through.* **Outstanding:** no second monitor exists on this machine, so the
negative-coordinate case is covered only by the offline tests in
`tests/test_coords.py`. Re-check if hardware ever allows it.

---

## Phase 2 — Window tracking 👁 ✅ verified
- `capture/windows.py`: focused window, process name, version, real frame bounds
  via `DWMWA_EXTENDED_FRAME_BOUNDS`
- Detect fullscreen-exclusive windows and report them
- Overlay glues the ring to a point *inside* the target window
- Ring follows as the window is dragged, resized, maximised, moved between
  monitors

**Exit 👁:** Ring stays locked to the same visual point while I drag a window
around, across both monitors, at ≥30fps with no visible lag. Blocklist from SR4
is enforced.

*Automated so far:* the drop-shadow trap is confirmed real (GetWindowRect is 7px
out on three edges of an ordinary window) and avoided; anchoring holds under
move, resize, maximise, minimise and close against a real window; `GetWindowRect`
polls cost ~2µs, which is 0.01% of a 60Hz frame; SR4 refusal and
fullscreen-exclusive refusal have offline tests. Fullscreen refusal also fired
correctly against a real fullscreen VLC.

*Confirmed by eye: the ring stays locked to the window while dragging.* Second
monitor still untested for want of hardware, as in phase 1.

---

## Phase 3 — Capture + OCR + debug HUD 👁 ✅ verified
No LLM yet. Nothing intelligent yet.

- `grabber.py` capture worker (dxcam, mss fallback), focused window only
- `grounding/ocr.py` producing `Element`s from on-screen text
- `grounding/fusion.py` with a single provider for now
- `ui/hud.py` debug HUD drawing every element box with id and label
- `tools/grab.py` saving fixtures to `tests/fixtures/screens/`
- Offline tests over 3+ saved fixtures from different applications

**Exit 👁:** `uv run mentor --debug` draws accurate boxes around visible text in
any app I open. Test: ask it to ring the literal word "File" and it lands on the
File menu. Tray capture indicator works.

*Automated so far:* fixtures captured from Notepad, Calculator and File Explorer,
with `.json` sidecars recording true window bounds; 61 offline tests over them,
including the "File" criterion as an assertion. Live end to end against a real
Notepad: capture 19ms, grounding 1.09s, "File" found at window offset (16, 48),
which is the menu bar. Two bugs found and fixed — the overlay was being captured
and grounded (elements climbing 18→23→28→32 on a static window), and the tray
indicator flashed for 19ms, too short to see.

*Confirmed by eye across Terminal, Notepad, Explorer, Claude, Windows Search
and Riot Client.* One fix came out of that run: the Start menu briefly makes a
0x0 window the foreground window, which produced an empty capture; a window
with no area is now rejected in `app_context`.

---

## Phase 4 — Accessibility fast path ✅ done
- `grounding/uia.py` via `uiautomation`
- Fusion merges UIA + OCR, dedupes by IoU, prefers higher confidence
- Graceful and fast when the app exposes nothing (the common case)

**Exit:** In Notepad or Settings, elements carry `source="uia"` with exact bounds.
In an app with no tree, results are identical to phase 3 with no added latency
beyond ~20ms. Fusion has offline tests for overlap and dedupe.

*Met.* Notepad: 50 UIA elements in 212ms with exact bounds (`Close` at
1062,225,47,30). Settings: 143 in 218ms, once the UWP `CoreWindow` child was
walked as well as the frame — the frame alone gave 3. A window with no useful
tree costs 21ms median, inside the budget. Live fused pass on Settings: 143 UIA
+ 41 OCR → 83 after dedupe, stable across passes. Fusion has 15 offline tests.

**Carried forward:** a full grounding pass on a maximised window costs 1.4–2.4s.
NFR1 allows 3s from the user acting to the next highlight, and the planner round
trip has to fit in what is left. Phase 5 should measure end to end before
assuming it fits.

---

## Phase 5 — Planner and the single-step loop 👁 — working, awaiting eye check
First intelligence. First end-to-end usefulness.

- `planner/client.py` + `prompts.py` + pydantic validation
- Hard rejection of steps naming unlisted `target_id`, re-request once
- `ui/prompt_box.py` and the global hotkey
- `session/controller.py` state machine: IDLE → PLANNING → SHOWING
- Manual advance only for now ("next" key). No verification yet.

**Exit 👁:** I press the hotkey in a text-heavy app, type a goal, and get a correct
highlight on a correct first step. Pressing next gives a sensible second step.
Every planner request and response is in the log verbatim.

*Automated so far:* 40 offline planner tests against a fake client, covering
every way invariant 3 could be bypassed — hallucinated target id, null target on
an action that needs one, unknown action, out-of-range confidence, empty
instruction, missing field, and a targetless action naming a non-existent
element. Two bad answers in a row fail honestly rather than pointing at something
plausible. Full pipeline verified live against Notepad with a *scripted* planner
(no API call): 43 fused elements, step validated, ring landed on the File menu at
window offset (5, 42).

**Live and working** on OpenRouter with a free model. A real end-to-end run
against Notepad: goal "make the text bigger" -> 44 grounded elements -> the ring
landed on Notepad's `100%` zoom control in the status bar, instruction "Open the
zoom menu" (4 words), confidence 0.99. That is the correct answer.

Free models scored on four goals against a five-element list, counting picks a
competent guide could defensibly make:

    nex-agi/nex-n2.5-mini:free              4/4   2.0s median   <- default
    liquid/lfm-2.5-2.6b:free                4/4   3.0s
    nex-agi/nex-n2.5-pro:free               4/4   3.2s
    dots-studio/dots-3-note-preview:free    4/4   3.9s
    nvidia/nemotron-3-super-120b-a12b:free  3/4   4.2s   (one empty response)
    openrouter/free                         2/4   6.7s   (two non-JSON)
    qwen/qwen3.8-27b:free                   0/4    --    (upstream pool limited)

**NFR1 is not met and this is the phase that proves it.** Measured on an
86-element Notepad window: UIA 0.29s + OCR 2.64s + planner 1.9s = **~4.8s**
against a 3s budget, and a cold start adds engine init on top. OCR is two thirds
of it. The concrete fix is already written down in `architecture.md` -- the
grounding chain is meant to "stop when confident", and it currently never does:
Notepad returned 38 usable UIA elements before OCR was asked for anything. Making
OCR conditional on UIA coming back thin is the single biggest lever and does not
need a new phase.

**Anthropic path still untested** -- no `ANTHROPIC_API_KEY` here. `--offline`
runs the same loop with a keyword-matching planner (`planner/offline.py`), which
needs no key and no network, so the hotkey, input box, capture, grounding,
validation, ring, caption and state machine are all testable without one. It
goes through the same `plan_next_step`, so invariant 3 applies to it too.
Verified live against Notepad: "open the view menu" rings View, and two goals
with nothing matching on screen return an honest `wait` rather than guessing.

Model is `claude-opus-5` at `effort: "low"` (`config.PLANNER_MODEL` /
`PLANNER_EFFORT`). Low effort because NFR1 allows 3s end to end and grounding
already spends 1.4–2.4s; choosing one element from a supplied list is not a
problem that rewards deep reasoning. Measure before trusting that.

---

## Phase 6 — Verification and auto-advance 👁
- `verify/change.py` SSIM polling at 2fps while waiting
- `verify/judge.py` checking `step.expect` against the re-grounded screen
- Full state machine including re-plan on unexpected change
- Loop detection: same step three times → stop and say so
- Waiting/unsure/lost/done visual states from `design.md`

**Exit 👁:** I complete a real 4+ step task without touching the keyboard between
steps. Deliberately clicking the wrong thing causes a sensible re-plan, not a
stuck or repeated step.

---

## Phase 7 — Vision grounding
Now icons work, and generality becomes real.

- `grounding/vision.py` behind the same protocol (OmniParser local, or VLM API)
- Invoked **only** when cheaper providers miss the planner's intended target
- Latency and cost logged per call

**Exit:** In an icon-heavy application where phase 3–4 grounding finds nothing
useful, Mentor correctly highlights an icon-only tool. Vision is confirmed by the
logs to be skipped on text-only steps.

---

## Phase 8 — Application memory
- `knowledge/appmap.py` SQLite schema from `architecture.md`
- Cache hits consulted before every grounding pass
- Confidence decay and eviction when cached positions stop matching
- `route` caching of completed goal sequences
- User-facing cache inspect and clear

**Exit:** Repeating a previously completed goal in the same app produces the first
step measurably faster with no vision call. Moving the app's panels causes stale
entries to decay rather than producing wrong highlights.

---

## Phase 9 — Unknown applications
- `knowledge/docs.py` retrieval when the model has no priors
- Optional guided exploration: walk the menu bar, ground each panel, seed the
  app map

**Exit:** Mentor gives correct first steps in genuinely obscure software it can't
have memorised.

---

## Phase 10 — Hardening
- Blocklist UI, capture indicator audit against SR1–SR5
- Crash recovery, worker restart, clean shutdown
- Packaging to a single executable
- Cold start under 2s (NFR6)

**Exit:** Runs for a week of real use without a restart.

---

## Explicitly deferred

macOS/Linux ports · voice · browser DOM integration · multi-app workflows ·
performing actions for the user · anything in "Out of scope" in `requirement.md`.

Don't build these. If one starts to feel necessary, that's a conversation, not a
commit.
