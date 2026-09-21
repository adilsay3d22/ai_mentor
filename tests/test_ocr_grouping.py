"""Word regrouping and the glyph filter. Pure logic, no OCR engine, offline."""

from __future__ import annotations

import pytest

from mentor.grounding import base
from mentor.grounding.ocr import group_words, is_meaningful_label, quad_to_rect
from mentor.models import Frame, Rect

pytestmark = pytest.mark.offline


def word(text: str, x: int, width: int, *, y: int = 40, height: int = 20, score: float = 0.99):
    return (text, score, Rect(x, y, width, height))


def test_quad_to_rect_takes_the_bounding_box():
    quad = [[21, 16], [39, 17], [39, 35], [21, 34]]
    assert quad_to_rect(quad) == Rect(21, 16, 18, 19)


def test_tight_words_become_one_label():
    """'Gaussian Blur' is one menu item, not two."""
    words = [word("Gaussian", 10, 60), word("Blur", 75, 30)]
    grouped = group_words(words, gap_ratio=0.55)
    assert [g[0] for g in grouped] == ["Gaussian Blur"]
    assert grouped[0][2] == Rect(10, 40, 95, 20)


def test_widely_spaced_words_stay_separate():
    """A menu bar is separate controls, not a sentence."""
    words = [word("File", 10, 30), word("Edit", 80, 30), word("View", 150, 30)]
    grouped = group_words(words, gap_ratio=0.55)
    assert [g[0] for g in grouped] == ["File", "Edit", "View"]


def test_the_threshold_scales_with_line_height():
    """The same pixel gap means different things in 10px text and 30px text."""
    small = [word("A", 0, 20, height=10), word("B", 30, 20, height=10)]
    large = [word("A", 0, 20, height=40), word("B", 30, 20, height=40)]
    assert len(group_words(small, 0.55)) == 2
    assert len(group_words(large, 0.55)) == 1


def test_a_group_takes_the_lowest_confidence_of_its_words():
    """A label is only as trustworthy as its least certain part."""
    words = [word("Gaussian", 10, 60, score=0.99), word("Blur", 75, 30, score=0.61)]
    assert group_words(words, 0.55)[0][1] == pytest.approx(0.61)


def test_words_are_ordered_before_grouping():
    """Providers do not promise left-to-right order."""
    words = [word("Blur", 75, 30), word("Gaussian", 10, 60)]
    assert group_words(words, 0.55)[0][0] == "Gaussian Blur"


def test_empty_input_gives_nothing():
    assert group_words([], 0.55) == []


def test_a_single_word_survives_intact():
    assert group_words([word("File", 10, 30)], 0.55) == [("File", 0.99, Rect(10, 40, 30, 20))]


@pytest.mark.parametrize("label", ["File", "7", "Save as...", "M+", "H1"])
def test_real_labels_are_kept(label):
    assert is_meaningful_label(label)


# RUF001: the ambiguous characters are the point -- these are what OCR returns
# for toolbar icons, and the filter has to tell them from ordinary letters.
@pytest.mark.parametrize("label", ["×", "□", ">", ")", "...", "—", ""])  # noqa: RUF001
def test_glyph_misreads_are_rejected(label):
    """Every one of these came out of OCR over a real toolbar icon.

    Note the first is U+00D7 MULTIPLICATION SIGN, which is what a close button
    actually reads as -- not a Latin 'x'. A Latin 'x' is kept, because it could
    be a real one-letter label and the filter must not hide real text.
    """
    assert not is_meaningful_label(label)


def test_a_latin_x_is_not_treated_as_a_close_button():
    assert is_meaningful_label("x")


def test_frame_rect_to_screen_offsets_by_the_window_origin():
    frame = Frame(image=None, bounds=Rect(705, 218, 948, 511), captured_at=0.0, source="fixture")
    assert base.frame_rect_to_screen(Rect(16, 47, 20, 22), frame) == Rect(721, 265, 20, 22)


def test_frame_rect_to_screen_handles_a_window_at_negative_x():
    frame = Frame(image=None, bounds=Rect(-1800, 100, 800, 600), captured_at=0.0, source="fixture")
    assert base.frame_rect_to_screen(Rect(10, 10, 40, 20), frame) == Rect(-1790, 110, 40, 20)


def test_make_element_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="unknown element kind"):
        base.make_element(
            label="File", kind="widget", bounds=Rect(0, 0, 1, 1), confidence=0.9, source="ocr"
        )


def test_make_element_leaves_the_id_for_fusion():
    """A provider that invented ids would collide with another provider's."""
    created = base.make_element(
        label="File", kind="text", bounds=Rect(0, 0, 1, 1), confidence=0.9, source="ocr"
    )
    assert created.id == ""


def test_make_element_clamps_confidence():
    created = base.make_element(
        label="File", kind="text", bounds=Rect(0, 0, 1, 1), confidence=1.4, source="ocr"
    )
    assert created.confidence == 1.0
