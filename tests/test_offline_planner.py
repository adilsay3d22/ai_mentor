"""The offline planner, through the same validation as the real one. Offline.

The point of these is not that the heuristic picks well -- it often will not.
It is that it goes through ``plan_next_step`` untouched, so the loop it drives
is the real loop, and anything it gets wrong is rejected by the same code that
rejects a model's mistakes.
"""

from __future__ import annotations

import pytest

from mentor.models import AppContext, Element, Rect, Step
from mentor.planner import offline, planner, prompts

pytestmark = pytest.mark.offline

APP = AppContext(
    process_name="notepad.exe",
    window_title="Untitled - Notepad",
    version="11.0",
    hwnd=1,
    bounds=Rect(100, 100, 900, 600),
    is_fullscreen_exclusive=False,
)


def element(eid: str, label: str, kind: str = "menu", y: int = 148) -> Element:
    return Element(
        id=eid, label=label, kind=kind, bounds=Rect(116, y, 40, 22), confidence=1.0, source="uia"
    )


ELEMENTS = [
    element("el_00", "File"),
    element("el_01", "Edit"),
    element("el_02", "View"),
    element("el_03", "Save as", kind="button", y=300),
    element("el_04", "Word wrap", kind="button", y=340),
]


def plan(goal: str, elements=ELEMENTS, history: list[Step] | None = None) -> Step:
    return planner.plan_next_step(offline.OfflinePlanner(), goal, APP, elements, history or [])


def test_it_matches_a_goal_word_to_an_element_label():
    step = plan("open the File menu")
    assert step.target_id == "el_00"
    assert step.action == "open_menu"


def test_it_matches_a_two_word_label():
    step = plan("I want to save as something else")
    assert step.target_id == "el_03"
    assert step.action == "click"


def test_a_menu_becomes_open_menu_and_a_button_becomes_click():
    assert plan("File").action == "open_menu"
    assert plan("word wrap").action == "click"


def test_an_exact_match_beats_a_longer_label_containing_the_word():
    """ "Save" should reach 'Save as' but 'File' should not be dragged along."""
    step = plan("file")
    assert step.target_id == "el_00"


def test_stopwords_alone_match_nothing():
    step = plan("how do I do the thing")
    assert step.action == "wait"
    assert step.target_id is None


def test_an_unmatchable_goal_waits_rather_than_guessing():
    """Same honest answer the real planner gives when it cannot see a way forward."""
    step = plan("render a 3D volumetric cloud")
    assert step.action == "wait"
    assert step.target_id is None
    assert step.confidence < 0.5


def test_an_already_completed_step_loses_to_an_alternative():
    history = [
        Step(
            action="open_menu",
            target_id="el_00",
            instruction="Open File",
            expect="a dropdown",
            confidence=0.8,
        )
    ]
    step = plan("file edit", history=history)
    assert step.target_id == "el_01"


def test_every_step_it_produces_survives_the_real_validation():
    """Invariant 3 applies to the heuristic exactly as it does to the model."""
    for goal in ("File", "edit", "save as", "word wrap", "nonsense xyzzy"):
        step = plan(goal)
        assert step.action in prompts.ACTIONS
        if step.target_id is not None:
            assert step.target_id in {e.id for e in ELEMENTS}


def test_confidence_stays_inside_the_range():
    for goal in ("File", "save as", "xyzzy"):
        assert 0.0 <= plan(goal).confidence <= 1.0


def test_it_reads_elements_back_out_of_the_rendered_prompt():
    """It uses the same narrow interface as the real client -- no privileged channel."""
    message = prompts.build_user_message("open file", APP, ELEMENTS, [])
    parsed = offline.parse_elements(message)
    assert [p["id"] for p in parsed] == [e.id for e in ELEMENTS]
    assert [p["label"] for p in parsed] == [e.label for e in ELEMENTS]


def test_it_recovers_the_goal_from_the_prompt():
    message = prompts.build_user_message("remove the background", APP, ELEMENTS, [])
    assert offline.extract_goal(message) == "remove the background"


def test_an_element_with_a_negative_x_still_parses():
    """A window on a monitor left of the primary renders negative coordinates."""
    on_left = [
        Element(
            id="el_00",
            label="File",
            kind="menu",
            bounds=Rect(-1800, 140, 40, 22),
            confidence=1.0,
            source="uia",
        )
    ]
    message = prompts.build_user_message("file", APP, on_left, [])
    assert offline.parse_elements(message)[0]["id"] == "el_00"
