# Design — Mentor

How it looks, how it feels, and how the model is prompted.

## Interaction principles

1. **The application is the interface.** Mentor's own UI should be nearly
   invisible. No window the user has to arrange. No panel stealing screen space.
   Everything happens on top of what they're already looking at.
2. **One thing at a time.** One highlight, one sentence. A numbered list of five
   steps drawn over a screenshot is a diagram; this is a guide.
3. **Never block.** If the overlay ever intercepts a click, the illusion breaks
   and the tool becomes an obstacle.
4. **Honest about uncertainty.** "I think it's this one" renders differently from
   "it's this one", and "I can't find it" is a legitimate output.

## Visual language

The overlay is dark-neutral with a single accent. It must read on top of both a
white spreadsheet and a black video timeline, which rules out light fills.

| Element | Spec |
|---|---|
| Target ring | 3px stroke, accent colour, 6px padding around the element bounds, 8px corner radius. Subtle 1.5s pulse between 85% and 100% opacity. |
| Accent | `#7F77DD` primary. `#EF9F27` when confidence < 0.7. |
| Caption | Rounded rect, `rgba(20,20,22,0.92)` fill, 1px `rgba(255,255,255,0.12)` border, 14px text at 92% white, 12px padding. |
| Caption placement | Below the target, centred. Flips above if it would leave the window. Nudges horizontally to stay on screen. Never covers the target. |
| Arrow | Only when the caption can't sit adjacent to the target. Quadratic curve, 2.5px, same accent, arrowhead at the target end. |
| Dim mask | Off by default. Optional `--focus` mode: 35% black over the window with the target region punched out. |
| Progress | Small pill top-right of the window, "step 3", plus a hairline underline. No percentage — we don't know the total. |

Motion: highlights fade in over 150ms and move between positions with a 200ms
ease-out rather than teleporting — the movement itself tells the user their last
action registered. Respect the OS reduced-motion setting.

## Input box

Appears centred on the focused window's top third on hotkey. Rounded rect, same
dark surface as the caption, single-line, placeholder showing the detected app:
`Ask about Blender…`. `Enter` submits, `Esc` dismisses. That's the whole widget.

While a session is running, the hotkey reopens it so the user can redirect
mid-task ("actually I want it as a PNG").

## States the user can see

| State | What's on screen |
|---|---|
| Thinking | Small pulsing dot near where the input box was. No spinner over the app. |
| Showing | Ring + caption on the target. |
| Waiting (60s+) | Caption softens to "still here — click the highlighted item, or press Esc" |
| Unsure | Ring in amber, caption prefixed "I think — " |
| Lost | No ring. Centred caption: "I can't find that on screen. Has something moved, or is a dialog open?" with a "look again" affordance. |
| Done | Green ring flash on the last target, caption "That's it — done." Auto-dismiss after 3s. |

## Capture indicator

A small dot in the system tray turns accent-coloured whenever a frame is being
captured, and the tray icon is always visible while Mentor runs. Non-negotiable
(SR3) — a tool that watches your screen must be visibly honest about when.

## Planner prompt design

### System prompt shape

```
You guide a person through software by pointing at one thing at a time.
You never act for them.

APPLICATION: {process_name} {version}
GOAL: {goal}
COMPLETED SO FAR: {history as short lines, or "nothing yet"}

ELEMENTS CURRENTLY VISIBLE:
{el_00} menu   "File"        at (12,34) 40x22
{el_01} button "Export"      at (…)
...

Return ONE step as JSON matching the schema.
target_id MUST be one of the ids listed above. If the element the user
needs is not listed, choose the step that reveals it (open the menu or
panel that contains it) and target that instead.
If you cannot make progress from what is visible, return action "wait"
with an instruction explaining what you need to see.
Never invent an element that is not in the list.
```

### Step schema

```json
{
  "action": "click | open_menu | type | drag | scroll | wait | done",
  "target_id": "el_07",
  "instruction": "Open the Filter menu",
  "expect": "a dropdown appears below the Filter menu",
  "confidence": 0.9
}
```

`instruction` rules, enforced in the prompt and checked in review:
- One sentence, imperative, under 12 words.
- Says *what*, not *where* — the ring says where. "Open the Filter menu", never
  "click Filter in the top menu bar at the top of the screen".
- No hedging inside the text. Uncertainty is expressed by the amber ring, not by
  "maybe try clicking…".

`expect` is written for the verifier, not the user. It never renders.

### Why the element list is the whole trick

The planner is not asked "where is the Gaussian Blur filter". It's asked "given
these 40 things that are definitely on screen right now, which one moves us
toward the goal". That reframing is what makes this work on software the model
has never seen, and it's why FR3's constraint is load-bearing rather than a
safety net.

## Failure design

Failure is common here and needs to feel like collaboration, not error.

- **Grounder found nothing** → likely a fullscreen-exclusive app or a blocked
  window. Say which. Don't retry silently.
- **Planner target missing twice** → escalate grounding to vision, try once more,
  then surface "I can't find that" rather than pointing at something plausible.
  Pointing confidently at the wrong thing is the worst possible output.
- **User clicks something else** → not an error. Re-plan from the new state
  without comment. Never scold.
- **Stuck in a loop** (same step three times) → stop, say so, offer to show what
  it's looking at (the debug HUD) rather than repeating.

## Debug HUD (`--debug`)

Draws every grounded element's box with its id, label, source and confidence,
colour-coded by provider. This is the single most valuable development tool in
the project — build it early, in phase 3, not at the end.
