"""A planner that needs no API key, no network, and no money.

This is not a fallback for the real planner and must never be used as one. It
cannot reason about a goal, only match words in it against element labels, so
the steps it produces are right roughly as often as the words happen to line up.

What it is for: exercising everything around the planner. The hotkey, the input
box, capture, grounding, validation, the ring, the caption, the state machine
and the manual advance are all real in this mode, and all of them can be broken
in ways that have nothing to do with the model. Developing phase 6's verifier
needs a loop that produces steps, not a loop that produces *good* steps.

It implements the same ``LlmClient`` protocol as the real client and returns the
same JSON shape, so it goes through exactly the same validation -- including
invariant 3. A heuristic that named an element that was not on screen would be
rejected just as firmly as a model that did.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Words that carry no signal about which element is wanted.
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "how",
        "do",
        "does",
        "i",
        "my",
        "me",
        "to",
        "in",
        "on",
        "at",
        "of",
        "for",
        "with",
        "and",
        "or",
        "it",
        "this",
        "that",
        "is",
        "are",
        "can",
        "want",
        "need",
        "please",
        "help",
        "make",
        "get",
        "go",
    }
)

#: Element kinds a person is plausibly told to click, best first. Used only to
#: break a tie between two elements that match the goal equally well.
KIND_PREFERENCE = ("menu", "button", "tab", "field", "icon", "text", "panel")

#: Parsed back out of the rendered element list. The renderer's format is
#: ``el_00 menu    "File"    at (x,y) WxH``.
_ELEMENT_LINE = re.compile(
    r'^(?P<id>el_\d+)\s+(?P<kind>\w+)\s+"(?P<label>[^"]*)"\s+at\s+\((?P<x>-?\d+),(?P<y>-?\d+)\)'
)


def words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in STOPWORDS}


def parse_elements(user_message: str) -> list[dict[str, Any]]:
    """Recover the element list from the rendered prompt.

    Reading the prompt back rather than being handed the elements keeps this
    behind the same narrow interface as the real client, which is the point: if
    the offline planner needed a privileged channel, it would stop being a
    faithful stand-in.
    """
    found = []
    for line in user_message.splitlines():
        match = _ELEMENT_LINE.match(line.strip())
        if match:
            found.append(
                {
                    "id": match["id"],
                    "kind": match["kind"],
                    "label": match["label"],
                    "y": int(match["y"]),
                }
            )
    return found


def extract_goal(user_message: str) -> str:
    for line in user_message.splitlines():
        if line.startswith("GOAL:"):
            return line[len("GOAL:") :].strip()
    return ""


def completed_labels(user_message: str) -> set[str]:
    """Instructions already carried out, so the same step is not offered twice."""
    done: set[str] = set()
    in_section = False
    for line in user_message.splitlines():
        if line.startswith("COMPLETED SO FAR:"):
            in_section = True
            continue
        if in_section:
            if not line.strip() or line.startswith("ELEMENTS"):
                break
            done |= words(line)
    return done


def score(element: dict[str, Any], goal_words: set[str], done_words: set[str]) -> float:
    """How well this element matches the goal. Higher is better, 0 means no match."""
    label_words = words(element["label"])
    if not label_words:
        return 0.0

    overlap = goal_words & label_words
    if not overlap:
        return 0.0

    # Proportion of the label that the goal mentions, so a one-word exact match
    # beats a long label that happens to contain the word.
    value = len(overlap) / len(label_words)

    if label_words <= done_words:
        # Already done. Not disqualifying -- a menu may need reopening -- but it
        # should lose to anything else that matches.
        value *= 0.2

    kind = element["kind"]
    preference = KIND_PREFERENCE.index(kind) if kind in KIND_PREFERENCE else len(KIND_PREFERENCE)
    return value * 2 + (len(KIND_PREFERENCE) - preference) / 100.0


class OfflinePlanner:
    """Keyword matching dressed up as a planner. No key, no network, no cost."""

    def complete_json(self, system: str, user: str, schema: dict) -> dict[str, Any]:
        goal = extract_goal(user)
        elements = parse_elements(user)
        goal_words = words(goal)
        done_words = completed_labels(user)

        ranked = sorted(
            ((score(element, goal_words, done_words), element) for element in elements),
            key=lambda pair: pair[0],
            reverse=True,
        )
        best_score, best = ranked[0] if ranked else (0.0, None)

        if best is None or best_score <= 0.0:
            # No word in the goal matches anything on screen. The honest answer
            # is the same one the real planner would give.
            log.info("offline_planner_no_match", goal=goal, elements=len(elements))
            return {
                "action": "wait",
                "target_id": None,
                "instruction": f"Nothing on screen matches {goal!r}",
                "expect": "an element whose label matches the goal becomes visible",
                "confidence": 0.2,
            }

        action = "open_menu" if best["kind"] == "menu" else "click"
        verb = "Open" if action == "open_menu" else "Click"
        # Confidence tracks match quality honestly, and stays below the amber
        # threshold often enough that the unsure rendering gets exercised too.
        confidence = min(0.85, 0.35 + best_score / 3.0)

        log.info(
            "offline_planner_chose",
            goal=goal,
            target=best["id"],
            label=best["label"],
            score=round(best_score, 3),
            considered=len(elements),
        )
        return {
            "action": action,
            "target_id": best["id"],
            "instruction": f"{verb} {best['label']}",
            "expect": f"something changes near {best['label']}",
            "confidence": round(confidence, 2),
        }
