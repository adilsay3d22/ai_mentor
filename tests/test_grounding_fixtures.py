"""Grounding against real application screenshots, offline.

These run the actual OCR engine over the fixtures in
``tests/fixtures/screens/``, so they are slower than the rest of the suite --
but they need no live screen, no window, and no API, which is what ``offline``
means here. They are the regression net for the whole capture-to-element path:
if a highlight ever lands on the wrong word, this is where the fixture goes.

The fixtures were captured from applications launched for the purpose, not from
whatever happened to be open, and each has a ``.json`` sidecar recording the
window's true physical bounds. Without those bounds an image cannot be turned
back into screen coordinates, and the assertions below would be checking
image-local numbers that mean nothing.
"""

from __future__ import annotations

import json

import pytest

from mentor import config
from mentor.grounding import fusion
from mentor.grounding.ocr import OcrGrounder
from mentor.models import Element, Frame, Rect

pytestmark = pytest.mark.offline

FIXTURES = ("notepad", "calculator", "explorer_windows")


def load_frame(name: str) -> tuple[Frame, dict]:
    import cv2

    image_path = config.FIXTURES_DIR / f"{name}.png"
    meta_path = config.FIXTURES_DIR / f"{name}.json"
    if not image_path.exists():
        pytest.skip(f"fixture {name} not captured; run python -m mentor.tools.grab {name}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    image = cv2.imread(str(image_path))
    assert image is not None, f"could not decode {image_path}"
    return (
        Frame(image=image, bounds=Rect(**meta["bounds"]), captured_at=0.0, source="fixture"),
        meta,
    )


@pytest.fixture(scope="module")
def grounder() -> OcrGrounder:
    """One engine for the whole module; building it costs a third of a second."""
    return OcrGrounder()


@pytest.fixture(scope="module")
def grounded(grounder) -> dict[str, tuple[list[Element], Frame]]:
    results = {}
    for name in FIXTURES:
        frame, _meta = load_frame(name)
        results[name] = (fusion.fuse(grounder.ground(frame)), frame)
    return results


def find(elements: list[Element], label: str) -> Element | None:
    for element in elements:
        if element.label == label:
            return element
    return None


@pytest.mark.parametrize("name", FIXTURES)
def test_every_fixture_produces_elements(grounded, name):
    elements, _frame = grounded[name]
    assert len(elements) > 5, f"{name} produced almost nothing"


@pytest.mark.parametrize("name", FIXTURES)
def test_bounds_are_screen_coordinates_not_image_coordinates(grounded, name):
    """The single easiest way to put every highlight in the wrong place.

    A frame's image starts at the window's top-left, not the screen's. If a
    provider returns image-local coordinates unchanged, every element lands near
    the desktop origin instead of on the window.
    """
    elements, frame = grounded[name]
    for element in elements:
        assert frame.bounds.intersects(element.bounds), (
            f"{element.id} {element.label!r} at {element.bounds} "
            f"is outside the captured window {frame.bounds}"
        )


@pytest.mark.parametrize("name", FIXTURES)
def test_element_ids_are_unique_within_a_frame(grounded, name):
    """Invariant 3 depends on the planner naming an id that means exactly one thing."""
    elements, _frame = grounded[name]
    ids = [element.id for element in elements]
    assert len(ids) == len(set(ids))
    assert all(element.id for element in elements), "fusion left an element without an id"


@pytest.mark.parametrize("name", FIXTURES)
def test_ids_are_assigned_in_reading_order(grounded, name):
    elements, _frame = grounded[name]
    tops = [(e.bounds.y, e.bounds.x) for e in elements]
    assert tops == sorted(tops)


def test_the_word_file_lands_on_notepads_file_menu(grounded):
    """Phase 3's stated exit criterion, as a test rather than a promise."""
    elements, frame = grounded["notepad"]
    file_menu = find(elements, "File")
    assert file_menu is not None, (
        f"no element labelled 'File'; got {sorted({e.label for e in elements})}"
    )

    # Notepad's menu bar is in the top quarter of the window and hard left.
    assert file_menu.bounds.y < frame.bounds.y + frame.bounds.h * 0.25
    assert file_menu.bounds.x < frame.bounds.x + 200
    # A menu label is a small box, not a swallowed-up line of the whole menu bar.
    assert file_menu.bounds.w < 80, f"'File' box is {file_menu.bounds.w}px wide — too greedy"
    assert file_menu.confidence > 0.9


def test_notepad_menu_items_stay_separate(grounded):
    """The regrouping must not merge a menu bar back into one blob."""
    elements, _frame = grounded["notepad"]
    labels = {e.label for e in elements}
    assert {"File", "Edit", "View"} <= labels
    assert not any(" " in label and "File" in label for label in labels)


def test_multi_word_labels_survive_as_one_element(grounded):
    """The other half of the same trade-off: don't split a real two-word label."""
    elements, _frame = grounded["explorer_windows"]
    multi_word = [e for e in elements if " " in e.label]
    assert multi_word, "every label is a single word — the gap grouping is splitting too eagerly"


def test_calculator_digits_are_found_individually(grounded):
    """An icon-free, text-light app: the digits are the whole UI."""
    elements, _frame = grounded["calculator"]
    labels = {e.label for e in elements}
    digits = {d for d in "0123456789" if d in labels}
    assert len(digits) >= 7, f"only found digits {sorted(digits)}"


@pytest.mark.parametrize("name", FIXTURES)
def test_glyph_misreads_are_filtered_out(grounded, name):
    """Close buttons and chevrons come back as 'x' and '>'. They are not options."""
    elements, _frame = grounded[name]
    for element in elements:
        assert any(c.isalnum() for c in element.label), (
            f"{element.id} kept a non-alphanumeric label {element.label!r}"
        )


@pytest.mark.parametrize("name", FIXTURES)
def test_every_element_is_well_formed(grounded, name):
    elements, _frame = grounded[name]
    for element in elements:
        assert element.source == "ocr"
        assert element.kind == "text"
        assert 0.0 <= element.confidence <= 1.0
        assert element.bounds.w > 0 and element.bounds.h > 0
        assert element.label == element.label.strip()
