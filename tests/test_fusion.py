"""Fusion: overlap, dedupe and id assignment. Pure geometry, offline."""

from __future__ import annotations

import pytest

from mentor.grounding import fusion
from mentor.models import Element, Rect

pytestmark = pytest.mark.offline


def element(
    label: str,
    bounds: Rect,
    *,
    source: str = "ocr",
    confidence: float = 0.9,
) -> Element:
    return Element(
        id="", label=label, kind="text", bounds=bounds, confidence=confidence, source=source
    )


def test_iou_of_identical_rects_is_one():
    rect = Rect(0, 0, 100, 50)
    assert fusion.intersection_over_union(rect, rect) == pytest.approx(1.0)


def test_iou_of_disjoint_rects_is_zero():
    assert fusion.intersection_over_union(Rect(0, 0, 10, 10), Rect(50, 50, 10, 10)) == 0.0


def test_iou_of_touching_rects_is_zero():
    assert fusion.intersection_over_union(Rect(0, 0, 10, 10), Rect(10, 0, 10, 10)) == 0.0


def test_iou_of_half_overlap():
    a = Rect(0, 0, 10, 10)
    b = Rect(5, 0, 10, 10)
    # overlap 50, union 150
    assert fusion.intersection_over_union(a, b) == pytest.approx(50 / 150)


def test_iou_handles_negative_coordinates():
    a = Rect(-100, -50, 20, 20)
    assert fusion.intersection_over_union(a, a) == pytest.approx(1.0)


def test_dedupe_keeps_the_more_confident_duplicate():
    weak = element("File", Rect(10, 10, 40, 20), confidence=0.6)
    strong = element("File", Rect(11, 10, 40, 20), confidence=0.95)
    kept = fusion.deduplicate([weak, strong])
    assert len(kept) == 1
    assert kept[0].confidence == 0.95


def test_dedupe_prefers_uia_over_ocr_at_equal_confidence():
    """An accessibility tree reports a control's real rectangle; OCR reports the ink."""
    from_ocr = element("File", Rect(10, 10, 40, 20), source="ocr", confidence=0.9)
    from_uia = element("File", Rect(8, 8, 46, 24), source="uia", confidence=0.9)
    kept = fusion.deduplicate([from_ocr, from_uia])
    assert len(kept) == 1
    assert kept[0].source == "uia"


def test_dedupe_leaves_genuinely_different_elements_alone():
    elements = [
        element("File", Rect(10, 10, 40, 20)),
        element("Edit", Rect(60, 10, 40, 20)),
        element("View", Rect(110, 10, 40, 20)),
    ]
    assert len(fusion.deduplicate(elements)) == 3


def test_dedupe_does_not_merge_a_slight_overlap():
    """Adjacent menu items can share a pixel or two without being the same thing."""
    a = element("File", Rect(10, 10, 40, 20))
    b = element("Edit", Rect(48, 10, 40, 20))
    assert len(fusion.deduplicate([a, b])) == 2


def test_ids_are_assigned_in_reading_order():
    elements = [
        element("bottom", Rect(10, 100, 30, 10)),
        element("top-right", Rect(200, 10, 30, 10)),
        element("top-left", Rect(10, 10, 30, 10)),
    ]
    fused = fusion.assign_ids(elements)
    assert [e.label for e in fused] == ["top-left", "top-right", "bottom"]
    assert [e.id for e in fused] == ["el_00", "el_01", "el_02"]


def test_fuse_combines_providers_and_assigns_ids():
    ocr = [element("File", Rect(10, 10, 40, 20), source="ocr")]
    uia = [element("Save", Rect(10, 60, 40, 20), source="uia")]
    fused = fusion.fuse(ocr, uia)
    assert [e.id for e in fused] == ["el_00", "el_01"]
    assert {e.source for e in fused} == {"ocr", "uia"}


def test_fuse_with_no_providers_is_empty():
    assert fusion.fuse() == []


def test_fuse_deduplicates_across_providers():
    """The case this exists for: UIA and OCR both reporting the File menu."""
    ocr = [element("File", Rect(10, 10, 40, 20), source="ocr", confidence=0.92)]
    uia = [element("File", Rect(9, 9, 42, 22), source="uia", confidence=0.99)]
    fused = fusion.fuse(ocr, uia)
    assert len(fused) == 1
    assert fused[0].source == "uia"
    assert fused[0].id == "el_00"


def test_dedupe_prefers_the_actionable_kind_over_its_caption():
    """UIA reports "Get help" as both a button and the text inside it."""
    caption = element("Get help", Rect(644, 726, 53, 19), source="uia", confidence=0.99)
    caption = Element(
        id="", label="Get help", kind="text", bounds=caption.bounds, confidence=0.99, source="uia"
    )
    button = Element(
        id="",
        label="Get help",
        kind="button",
        bounds=Rect(643, 725, 55, 21),
        confidence=0.99,
        source="uia",
    )
    kept = fusion.deduplicate([caption, button])
    assert len(kept) == 1
    assert kept[0].kind == "button"


def test_source_still_outranks_kind():
    """A precise UIA rectangle beats an OCR one even when OCR claims a better kind."""
    from_ocr = Element(
        id="",
        label="File",
        kind="button",
        bounds=Rect(10, 10, 40, 20),
        confidence=0.9,
        source="ocr",
    )
    from_uia = Element(
        id="", label="File", kind="text", bounds=Rect(9, 9, 42, 22), confidence=0.9, source="uia"
    )
    kept = fusion.deduplicate([from_ocr, from_uia])
    assert len(kept) == 1
    assert kept[0].source == "uia"
