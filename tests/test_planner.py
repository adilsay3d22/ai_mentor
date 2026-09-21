"""The planner, against a fake client. Never calls a live API.

The tests that matter here are the rejections. Invariant 3 says the grounder is
the authority on what exists and the planner may only choose from what it
reported this frame; architecture.md calls that "the mechanism that kills
hallucinated menu items" and says not to weaken it. Every way that mechanism
could be bypassed gets a test.
"""

from __future__ import annotations

import pytest

from mentor.models import AppContext, Element, Rect, Step
from mentor.planner import planner, prompts
from mentor.planner.client import FakeLlmClient, PlannerError

pytestmark = pytest.mark.offline

APP = AppContext(
    process_name="notepad.exe",
    window_title="Untitled - Notepad",
    version="11.0.0.0",
    hwnd=1234,
    bounds=Rect(100, 100, 900, 600),
    is_fullscreen_exclusive=False,
)

ELEMENTS = [
    Element(
        id="el_00",
        label="File",
        kind="menu",
        bounds=Rect(116, 148, 20, 22),
        confidence=1.0,
        source="uia",
    ),
    Element(
        id="el_01",
        label="Edit",
        kind="menu",
        bounds=Rect(164, 148, 28, 21),
        confidence=1.0,
        source="uia",
    ),
    Element(
        id="el_02",
        label="View",
        kind="menu",
        bounds=Rect(218, 148, 26, 23),
        confidence=0.99,
        source="ocr",
    ),
]


def answer(**overrides) -> dict:
    base = {
        "action": "open_menu",
        "target_id": "el_00",
        "instruction": "Open the File menu",
        "expect": "a dropdown appears below File",
        "confidence": 0.95,
    }
    base.update(overrides)
    return base


# --- the happy path ----------------------------------------------------------


def test_a_valid_answer_becomes_a_step():
    client = FakeLlmClient([answer()])
    step = planner.plan_next_step(client, "save the file", APP, ELEMENTS)
    assert isinstance(step, Step)
    assert step.action == "open_menu"
    assert step.target_id == "el_00"
    assert step.instruction == "Open the File menu"
    assert step.confidence == 0.95
    assert len(client.requests) == 1


def test_the_element_list_reaches_the_prompt():
    """The planner's whole premise is that it chooses from a supplied list."""
    client = FakeLlmClient([answer()])
    planner.plan_next_step(client, "save the file", APP, ELEMENTS)
    _system, user = client.requests[0]
    for element in ELEMENTS:
        assert element.id in user
        assert element.label in user
    assert "save the file" in user
    assert "notepad.exe" in user


def test_history_is_sent_so_the_planner_does_not_repeat_itself():
    client = FakeLlmClient([answer()])
    history = [
        Step(
            action="open_menu",
            target_id="el_00",
            instruction="Open the File menu",
            expect="a dropdown",
            confidence=0.9,
        )
    ]
    planner.plan_next_step(client, "save the file", APP, ELEMENTS, history)
    _system, user = client.requests[0]
    assert "Open the File menu" in user


def test_an_empty_history_says_so_rather_than_being_blank():
    client = FakeLlmClient([answer()])
    planner.plan_next_step(client, "save", APP, ELEMENTS)
    _system, user = client.requests[0]
    assert "nothing yet" in user


# --- invariant 3: the rejections ---------------------------------------------


def test_a_hallucinated_target_is_rejected_and_re_requested():
    """The case this exists for: a plausible menu item that is not on screen."""
    client = FakeLlmClient([answer(target_id="el_99"), answer(target_id="el_01")])
    step = planner.plan_next_step(client, "edit something", APP, ELEMENTS)
    assert step.target_id == "el_01"
    assert len(client.requests) == 2


def test_the_re_request_states_the_violation():
    """Asking again unchanged tends to produce the same answer."""
    client = FakeLlmClient([answer(target_id="el_99"), answer()])
    planner.plan_next_step(client, "edit", APP, ELEMENTS)
    _system, retry = client.requests[1]
    assert "REJECTED" in retry
    assert "el_99" in retry


def test_two_bad_answers_fail_rather_than_pointing_at_something_plausible():
    """design.md: pointing confidently at the wrong thing is the worst output."""
    client = FakeLlmClient([answer(target_id="el_99"), answer(target_id="el_98")])
    with pytest.raises(PlannerError, match="could not name a step"):
        planner.plan_next_step(client, "edit", APP, ELEMENTS)
    assert len(client.requests) == 2


def test_an_action_needing_a_target_cannot_omit_it():
    client = FakeLlmClient([answer(target_id=None), answer()])
    step = planner.plan_next_step(client, "edit", APP, ELEMENTS)
    assert step.target_id == "el_00"
    _system, retry = client.requests[1]
    assert "needs a target" in retry


def test_an_unknown_action_is_rejected():
    client = FakeLlmClient([answer(action="hack_the_mainframe"), answer()])
    step = planner.plan_next_step(client, "edit", APP, ELEMENTS)
    assert step.action == "open_menu"


def test_an_out_of_range_confidence_is_rejected():
    client = FakeLlmClient([answer(confidence=1.8), answer()])
    step = planner.plan_next_step(client, "edit", APP, ELEMENTS)
    assert step.confidence == 0.95


def test_an_empty_instruction_is_rejected():
    """There would be nothing to show the user."""
    client = FakeLlmClient([answer(instruction="   "), answer()])
    step = planner.plan_next_step(client, "edit", APP, ELEMENTS)
    assert step.instruction == "Open the File menu"


def test_a_missing_field_is_rejected():
    broken = answer()
    del broken["expect"]
    client = FakeLlmClient([broken, answer()])
    step = planner.plan_next_step(client, "edit", APP, ELEMENTS)
    assert step.expect == "a dropdown appears below File"


# --- targetless actions ------------------------------------------------------


def test_wait_may_omit_a_target():
    """'I cannot make progress from what is visible' is a legitimate answer."""
    client = FakeLlmClient(
        [
            answer(
                action="wait",
                target_id=None,
                instruction="Open the document first",
                expect="a document is open",
            )
        ]
    )
    step = planner.plan_next_step(client, "print it", APP, ELEMENTS)
    assert step.action == "wait"
    assert step.target_id is None


def test_done_may_omit_a_target():
    client = FakeLlmClient(
        [answer(action="done", target_id=None, instruction="That's it — done.", expect="")]
    )
    step = planner.plan_next_step(client, "save", APP, ELEMENTS)
    assert step.action == "done"


def test_even_a_targetless_action_may_not_invent_an_element():
    client = FakeLlmClient(
        [
            answer(action="wait", target_id="el_99"),
            answer(action="wait", target_id=None, instruction="Waiting", expect="something"),
        ]
    )
    step = planner.plan_next_step(client, "save", APP, ELEMENTS)
    assert step.target_id is None
    assert len(client.requests) == 2


# --- refusing to ask at all --------------------------------------------------


def test_an_empty_element_list_is_not_sent_to_the_planner():
    """Asking a model to choose from an empty list invites it to invent one."""
    client = FakeLlmClient([answer()])
    with pytest.raises(PlannerError, match="Nothing was found on screen"):
        planner.plan_next_step(client, "save", APP, [])
    assert client.requests == []


# --- the render boundary -----------------------------------------------------


def test_target_of_finds_the_element_a_step_points_at():
    step = Step(action="click", target_id="el_02", instruction="x", expect="y", confidence=0.9)
    found = planner.target_of(step, ELEMENTS)
    assert found is not None
    assert found.label == "View"


def test_target_of_is_none_for_a_targetless_step():
    step = Step(action="done", target_id=None, instruction="x", expect="y", confidence=1.0)
    assert planner.target_of(step, ELEMENTS) is None


def test_target_of_is_none_rather_than_raising_for_an_unknown_id():
    step = Step(action="click", target_id="el_99", instruction="x", expect="y", confidence=0.9)
    assert planner.target_of(step, ELEMENTS) is None


# --- the prompt itself -------------------------------------------------------


def test_the_schema_and_the_model_agree_on_their_fields():
    """Two enforcement points, one shape. They must not drift apart."""
    assert set(prompts.STEP_JSON_SCHEMA["properties"]) == set(prompts.PlannedStep.model_fields)
    assert set(prompts.STEP_JSON_SCHEMA["required"]) == set(prompts.PlannedStep.model_fields)


def test_the_schema_forbids_extra_properties():
    assert prompts.STEP_JSON_SCHEMA["additionalProperties"] is False


def test_the_schema_enumerates_exactly_the_allowed_actions():
    assert prompts.STEP_JSON_SCHEMA["properties"]["action"]["enum"] == list(prompts.ACTIONS)


def test_no_action_in_the_vocabulary_is_something_mentor_does():
    """Invariant 1, expressed as a vocabulary rather than a rule to remember."""
    forbidden = {"press", "send", "invoke", "execute", "run", "automate"}
    assert not forbidden & set(prompts.ACTIONS)


def test_rendering_an_element_includes_its_id_label_and_geometry():
    rendered = prompts.render_element(ELEMENTS[0])
    assert "el_00" in rendered
    assert "File" in rendered
    assert "(116,148)" in rendered
    assert "20x22" in rendered


def test_an_element_with_no_label_still_renders():
    unlabelled = Element(
        id="el_07",
        label="",
        kind="icon",
        bounds=Rect(0, 0, 16, 16),
        confidence=0.7,
        source="uia",
    )
    assert "el_07" in prompts.render_element(unlabelled)
