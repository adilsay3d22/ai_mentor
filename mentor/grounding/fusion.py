"""Merges what several providers found into one list the planner can choose from.

Fusion owns element ids, because ids must be unique across the merged result and
no single provider can guarantee that. It also owns deduplication: UIA and OCR
will both report the same File menu, and the planner should be offered it once.

Only OCR exists so far, so the merge is a pass-through with ids attached. The
dedupe rules are implemented and tested now anyway -- they are pure geometry, and
writing them while there is one provider is cheaper than writing them while
debugging two.
"""

from __future__ import annotations

from dataclasses import replace

import structlog

from mentor.models import Element, Rect

log = structlog.get_logger(__name__)

#: Boxes overlapping more than this are treated as the same thing.
DEFAULT_IOU_THRESHOLD = 0.6

#: Later providers in this order win a tie at equal confidence. Ordered by how
#: precise the source's bounds are, not by how clever the provider is: an
#: accessibility tree reports a control's exact rectangle, OCR reports wherever
#: the ink happened to be.
SOURCE_PRIORITY = ("vision", "ocr", "cache", "uia")

#: Later kinds win a tie between two elements from the same source at the same
#: confidence. UIA routinely reports one control twice -- "Get help" arrives as
#: both a button and the text inside it, at almost the same rectangle -- and the
#: planner should be offered the thing that can be clicked, not its caption.
KIND_PRIORITY = ("panel", "text", "icon", "field", "tab", "menu", "button")


def intersection_over_union(a: Rect, b: Rect) -> float:
    """Standard IoU. 0.0 when the rectangles do not overlap at all."""
    left = max(a.x, b.x)
    top = max(a.y, b.y)
    right = min(a.right, b.right)
    bottom = min(a.bottom, b.bottom)
    if right <= left or bottom <= top:
        return 0.0
    overlap = (right - left) * (bottom - top)
    union = a.w * a.h + b.w * b.h - overlap
    return overlap / union if union > 0 else 0.0


def _rank(element: Element) -> tuple[float, int, int]:
    """How strong a claim this element has, for resolving a duplicate."""
    source = SOURCE_PRIORITY.index(element.source) if element.source in SOURCE_PRIORITY else -1
    kind = KIND_PRIORITY.index(element.kind) if element.kind in KIND_PRIORITY else -1
    return (element.confidence, source, kind)


def deduplicate(
    elements: list[Element], iou_threshold: float = DEFAULT_IOU_THRESHOLD
) -> list[Element]:
    """Drop elements that are the same thing seen twice, keeping the best claim.

    Strongest first, so a weaker duplicate is always the one discarded.
    """
    kept: list[Element] = []
    for element in sorted(elements, key=_rank, reverse=True):
        if any(intersection_over_union(element.bounds, k.bounds) > iou_threshold for k in kept):
            continue
        kept.append(element)
    return kept


def assign_ids(elements: list[Element]) -> list[Element]:
    """Give every element a stable id for this frame, in reading order.

    Reading order -- top to bottom, then left to right -- so that the list the
    planner reads resembles the screen it describes. Ids are only meaningful
    within one frame; nothing may store one.
    """
    ordered = sorted(elements, key=lambda e: (e.bounds.y, e.bounds.x))
    return [replace(element, id=f"el_{index:02d}") for index, element in enumerate(ordered)]


def fuse(
    *provider_results: list[Element], iou_threshold: float = DEFAULT_IOU_THRESHOLD
) -> list[Element]:
    """Merge every provider's findings into the frame's element list."""
    combined: list[Element] = [element for result in provider_results for element in result]
    deduped = deduplicate(combined, iou_threshold)
    fused = assign_ids(deduped)
    log.info(
        "elements_fused",
        providers=len(provider_results),
        found=len(combined),
        after_dedupe=len(deduped),
        by_source={
            source: sum(1 for e in fused if e.source == source)
            for source in {e.source for e in fused}
        },
    )
    return fused
