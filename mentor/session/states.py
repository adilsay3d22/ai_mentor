"""The states one guidance session can be in.

Listed in `architecture.md`. Phase 5 reaches IDLE, PLANNING, SHOWING, FAILED and
DONE; WAITING and VERIFYING arrive with the verifier in phase 6 and are declared
here now so the transition table does not have to be rewritten around them.
"""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    """A StrEnum so a state logs and compares as its own name."""

    IDLE = "idle"
    PLANNING = "planning"
    SHOWING = "showing"
    WAITING = "waiting"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"


#: Which states may follow which. The controller asserts against this rather
#: than trusting itself, because a state machine that can silently take an
#: impossible transition is how a session ends up stuck with no way to tell why.
ALLOWED: dict[State, frozenset[State]] = {
    State.IDLE: frozenset({State.PLANNING}),
    State.PLANNING: frozenset({State.SHOWING, State.PLANNING, State.DONE, State.FAILED}),
    State.SHOWING: frozenset({State.WAITING, State.PLANNING, State.DONE, State.FAILED, State.IDLE}),
    State.WAITING: frozenset({State.VERIFYING, State.PLANNING, State.FAILED, State.IDLE}),
    State.VERIFYING: frozenset({State.PLANNING, State.DONE, State.FAILED, State.IDLE}),
    State.DONE: frozenset({State.IDLE, State.PLANNING}),
    State.FAILED: frozenset({State.IDLE, State.PLANNING}),
}


def may_move(source: State, target: State) -> bool:
    return target in ALLOWED.get(source, frozenset())
