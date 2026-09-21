"""Plan one step, and refuse to return one that names something that isn't there.

This module is where invariant 3 is enforced. The grounder is the authority on
what exists; the planner may only choose from what it reported this frame. A
step naming an unknown target is rejected and re-requested once with the
violation described, and if it comes back wrong again the honest answer is that
Mentor does not know -- not a highlight on something plausible.

architecture.md: "This is the mechanism that kills hallucinated menu items. Do
not weaken it."
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from pydantic import ValidationError

from mentor import config
from mentor.models import AppContext, Element, Step
from mentor.planner import prompts
from mentor.planner.client import LlmClient, PlannerError

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Rejection:
    """Why a returned step was not acceptable, in words the model can act on."""

    reason: str
    detail: str

    def as_prompt_text(self) -> str:
        return f"{self.reason}: {self.detail}"


def validate(raw: dict, elements: list[Element]) -> tuple[Step | None, Rejection | None]:
    """Check one raw answer against the schema and against this frame's elements.

    Returns the step, or the reason it cannot be used. Never both.
    """
    try:
        planned = prompts.PlannedStep.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "response"
        return None, Rejection("The answer did not match the schema", f"{field}: {first['msg']}")

    known = {element.id for element in elements}

    if planned.action in prompts.TARGETLESS_ACTIONS:
        # "wait" and "done" are about the situation, not about a thing on screen.
        # A target on one of these is harmless, but only if it actually exists.
        if planned.target_id is not None and planned.target_id not in known:
            return None, Rejection(
                f"Action {planned.action!r} named an element that is not on screen",
                f"{planned.target_id!r} is not in the list",
            )
        return planned.to_step(), None

    if planned.target_id is None:
        return None, Rejection(
            f"Action {planned.action!r} needs a target",
            "target_id was null, but only 'wait' and 'done' may omit it",
        )

    if planned.target_id not in known:
        # The case this whole module exists for: a plausible-sounding menu item
        # that is not actually on the screen.
        return None, Rejection(
            "target_id is not an element on screen",
            f"{planned.target_id!r} was not in the list of {len(known)} ids",
        )

    if not planned.instruction.strip():
        return None, Rejection("The instruction was empty", "there is nothing to show the user")

    return planned.to_step(), None


def plan_next_step(
    client: LlmClient,
    goal: str,
    app: AppContext,
    elements: list[Element],
    history: list[Step] | None = None,
    max_retries: int = config.PLANNER_MAX_RETRIES,
) -> Step:
    """Ask for one step, validate it, and re-request once if it was unusable.

    Raises :class:`PlannerError` rather than returning a step Mentor cannot
    honestly render. design.md: "Pointing confidently at the wrong thing is the
    worst possible output."
    """
    history = history or []

    if not elements:
        # Nothing was grounded. Asking the model to choose from an empty list
        # invites it to invent one, so do not ask.
        log.warning("planner_skipped_no_elements", goal=goal, process=app.process_name)
        raise PlannerError("Nothing was found on screen to point at.")

    violation: str | None = None

    for attempt in range(max_retries + 1):
        user_message = prompts.build_user_message(goal, app, elements, history, violation)
        raw = client.complete_json(prompts.SYSTEM_PROMPT, user_message, prompts.STEP_JSON_SCHEMA)

        step, rejection = validate(raw, elements)
        if step is not None:
            log.info(
                "step_planned",
                attempt=attempt,
                action=step.action,
                target_id=step.target_id,
                instruction=step.instruction,
                confidence=step.confidence,
            )
            return step

        assert rejection is not None
        log.warning(
            "step_rejected",
            attempt=attempt,
            reason=rejection.reason,
            detail=rejection.detail,
            raw=raw,
        )
        violation = rejection.as_prompt_text()

    raise PlannerError(
        f"The planner could not name a step that exists on screen ({violation}). "
        "Nothing is being highlighted."
    )


def target_of(step: Step, elements: list[Element]) -> Element | None:
    """The element a step points at, or None for a targetless step.

    Returns None rather than raising for an unknown id: ``validate`` has already
    guaranteed that cannot happen for a rendered step, so this is a belt-and-
    braces lookup at the render boundary.
    """
    if step.target_id is None:
        return None
    return next((element for element in elements if element.id == step.target_id), None)
