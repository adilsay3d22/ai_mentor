"""The system prompt, the element rendering, and the step schema.

The framing here is the whole trick, and design.md says why: the planner is not
asked "where is the Gaussian Blur filter". It is asked "given these forty things
that are definitely on screen right now, which one moves us toward the goal".
That reframing is what lets this work on software the model has never seen, and
it only holds if the element list is the *only* source of targets.

The schema is enforced twice on purpose. ``STEP_JSON_SCHEMA`` constrains what the
API may return at all; ``PlannedStep`` validates what came back. Invariant 3 --
that a step naming an element the grounder did not return is rejected -- cannot
be expressed in a JSON schema, because it depends on this frame's element ids, so
it lives in ``planner.py`` as code.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, Field

from mentor.models import AppContext, Element, Step

#: Actions a step may name. Every one of these is something the *user* does.
#: There is no action here that Mentor performs, which is invariant 1 expressed
#: as a vocabulary rather than as a rule to be remembered.
ACTIONS: Final[tuple[str, ...]] = (
    "click",
    "open_menu",
    "type",
    "drag",
    "scroll",
    "wait",
    "done",
)

#: Actions that legitimately have no on-screen target.
TARGETLESS_ACTIONS: Final[frozenset[str]] = frozenset({"wait", "done"})

SYSTEM_PROMPT: Final[str] = """\
You guide a person through software by pointing at one thing at a time.
You never act for them. You never click anything yourself. Your entire output is
one step describing what they should do next.

You will be given the application in focus, the person's goal, the steps already
completed, and a list of the elements visible on their screen at this instant.

Rules, in order of importance:

1. `target_id` MUST be one of the ids in the ELEMENTS list. Never invent an
   element. If the thing the person ultimately needs is not listed, choose the
   step that would reveal it -- open the menu or panel that contains it -- and
   target that instead.
2. If you cannot make progress from what is visible, return action "wait" with
   an instruction explaining what you need to see. This is a legitimate answer
   and is far better than guessing.
3. If the goal is already achieved, return action "done".
4. `instruction` is one imperative sentence, under 12 words. It says *what*, not
   *where* -- the highlight already says where. Write "Open the Filter menu",
   never "click Filter in the top menu bar".
5. Never hedge inside `instruction`. Uncertainty belongs in `confidence`, which
   the interface renders differently. Do not write "maybe try" or "I think".
6. `expect` is written for an automatic verifier, not for the person. It states
   what should visibly change if the step is performed correctly, for example
   "a dropdown appears below the Filter menu". It is never shown to anyone.
7. `confidence` is your honest probability that this step is correct. Use the
   full range. A confident wrong answer is the worst output you can produce.
"""

#: What the API is allowed to return. ``additionalProperties: false`` and an
#: explicit ``required`` list are what make the constraint strict.
STEP_JSON_SCHEMA: Final[dict] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "target_id": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "An id from the ELEMENTS list, or null for wait/done.",
        },
        "instruction": {
            "type": "string",
            "description": "One imperative sentence under 12 words, shown to the person.",
        },
        "expect": {
            "type": "string",
            "description": "What should visibly change. For the verifier, never displayed.",
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["action", "target_id", "instruction", "expect", "confidence"],
    "additionalProperties": False,
}


class PlannedStep(BaseModel):
    """The wire format of one step, validated on arrival.

    Separate from :class:`mentor.models.Step` on purpose: this is what an
    external service said, and it is not trusted until it has been checked
    against this frame's elements. ``to_step`` is the crossing point.
    """

    action: Literal["click", "open_menu", "type", "drag", "scroll", "wait", "done"]
    target_id: str | None
    instruction: str
    expect: str
    confidence: float = Field(ge=0.0, le=1.0)

    def to_step(self) -> Step:
        return Step(
            action=self.action,
            target_id=self.target_id,
            instruction=self.instruction.strip(),
            expect=self.expect.strip(),
            confidence=self.confidence,
        )


def render_element(element: Element) -> str:
    """One element as the planner sees it.

    Position and size are included because they carry real meaning -- a thing at
    the top-left of the window is probably a menu, a wide thing at the bottom is
    probably a status bar -- and because they let the model tell two identically
    labelled elements apart.
    """
    label = f'"{element.label}"' if element.label else '""'
    return (
        f"{element.id} {element.kind:<7} {label:<32} "
        f"at ({element.bounds.x},{element.bounds.y}) {element.bounds.w}x{element.bounds.h}"
    )


def render_history(history: list[Step]) -> str:
    if not history:
        return "nothing yet"
    return "\n".join(f"- {step.instruction}" for step in history)


def build_user_message(
    goal: str,
    app: AppContext,
    elements: list[Element],
    history: list[Step],
    violation: str | None = None,
) -> str:
    """The per-frame half of the request.

    Kept separate from the system prompt so the system prompt stays byte-stable
    and cacheable; everything that changes per frame lives here.

    ``violation`` is set only on a re-request, and states plainly what was wrong
    with the previous answer. Describing the violation is what makes the retry
    worth making at all -- asking again unchanged tends to produce the same
    answer.
    """
    version = f" {app.version}" if app.version else ""
    lines = [
        f"APPLICATION: {app.process_name}{version}",
        f"GOAL: {goal}",
        "",
        "COMPLETED SO FAR:",
        render_history(history),
        "",
        f"ELEMENTS CURRENTLY VISIBLE ({len(elements)}):",
    ]
    lines.extend(render_element(element) for element in elements)

    if not elements:
        lines.append("(none -- nothing was found on screen this frame)")

    if violation is not None:
        lines.extend(
            [
                "",
                "YOUR PREVIOUS ANSWER WAS REJECTED:",
                violation,
                "Answer again, using only the ids listed above.",
            ]
        )
    return "\n".join(lines)
