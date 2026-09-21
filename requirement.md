# Requirements — Mentor

## Problem

Learning unfamiliar software is slow because the gap between *knowing what you
want* and *knowing where it lives in the interface* is enormous. Video tutorials
solve this badly: they're linear, they're for someone else's version and layout,
and you spend the whole time pausing and hunting for a panel that has moved.

Text answers from an AI have the same gap. "Go to Filter → Blur → Gaussian Blur"
is only useful if you can find the Filter menu.

## Solution

An assistant that lives on the user's machine, sees what they see, and points at
the actual pixel on their actual screen. The user asks in plain language, and
Mentor draws a highlight on the real application showing the next thing to click.

## Primary user story

> I'm a student trying to learn an unfamiliar application. I press a hotkey, type
> "how do I remove the background from this photo", and Mentor circles the tool I
> need on my own screen. I click it. Mentor notices I clicked it and circles the
> next thing. I finish the task and I now know where those things are.

## Functional requirements

### FR1 — Invocation
- Global hotkey opens Mentor's input box from anywhere. Default `Ctrl+Shift+Space`.
- The user types a goal in natural language.
- Mentor determines which application is in focus without being told.
- Hotkey again, or `Esc`, dismisses everything immediately.

### FR2 — Screen understanding
- Mentor captures the focused application's window, not the whole desktop.
- It identifies interactive elements on screen and their bounding boxes: menu
  items, buttons, tool icons, panels, tabs, input fields.
- It must work on applications that expose no accessibility information — this is
  the normal case, not the edge case.
- It must work at any DPI scaling and on any monitor in a multi-monitor setup.

### FR3 — Step planning
- Mentor decomposes the goal into steps, but commits to **one step at a time**,
  re-observing the screen between each.
- Each step names exactly one on-screen target and one action.
- The planner may only target elements the grounder reported this frame. A step
  referencing a non-existent element is discarded and re-planned.
- When Mentor genuinely doesn't know, it says so rather than guessing.

### FR4 — Overlay presentation
- A transparent, always-on-top, click-through layer over the target window.
- It highlights the target element, and shows a short instruction caption.
- The highlight tracks the target window: move or resize the window and the
  highlight stays correct.
- The overlay never blocks interaction with the application underneath.

### FR5 — Progress tracking
- After the user acts, Mentor detects that the screen changed and decides whether
  the step succeeded.
- On success it advances. On no change it waits. On an unexpected change it
  re-plans from the new screen state.
- The user can say "next", "back", or "I'm stuck" at any time.

### FR6 — Application memory
- Mentor caches what it has learned about each application locally: element names,
  where they live, how to reach them, keyed by application and version.
- Subsequent sessions in the same application are faster and cheaper because they
  hit cache before vision.
- The cache is inspectable and deletable by the user.

### FR7 — Knowledge sourcing
The planner draws on, in order of cost:
1. What the model already knows about mainstream software.
2. The cached application map from FR6.
3. Retrieved documentation, for niche or unfamiliar applications.

## Non-functional requirements

| ID | Requirement | Target |
|----|-------------|--------|
| NFR1 | Time from user acting to next highlight appearing | < 3s typical |
| NFR2 | Overlay redraw while dragging the target window | ≥ 30 fps, no visible lag |
| NFR3 | Idle CPU when no session is active | ~0% — capture loop stopped, not throttled |
| NFR4 | Grounding accuracy on text-labelled elements | > 90% |
| NFR5 | Grounding accuracy on icon-only elements | > 70% (v1 honest target) |
| NFR6 | Cold start to usable input box | < 2s |
| NFR7 | Memory footprint idle | < 400 MB without a local vision model |

## Safety and privacy requirements

- **SR1** — Mentor never sends input to another application. It shows; the user does.
- **SR2** — Screen contents leave the machine only on the path of an explicit user
  request, and only the focused window, never the full desktop.
- **SR3** — A visible indicator is present whenever Mentor is capturing.
- **SR4** — A blocklist of applications Mentor will refuse to capture (password
  managers by default), user-editable.
- **SR5** — Screenshots are never persisted except under `./debug/` during
  development, and that directory is gitignored.

## Out of scope for v1

- macOS and Linux. The architecture keeps the platform-specific code isolated so
  this is a port, not a rewrite, but v1 is Windows-only.
- Voice input and output.
- Doing the task for the user.
- Browser-specific understanding (the DOM would be a better source than pixels,
  but it's a separate subsystem).
- Multi-window workflows spanning two applications.
- Teaching content authoring, courses, or progress tracking across sessions.

## Success criteria

v1 succeeds if, on an application I have never used before, I can state a goal in
plain language and be walked to completion in under ten steps without opening a
browser — and if a step ever lands wrong, I can tell from the logs which layer
was responsible.
