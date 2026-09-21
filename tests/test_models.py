"""Geometry on the shared models. Pure data, no Qt, no screen."""

from __future__ import annotations

import pytest

from mentor.models import Point, Rect

pytestmark = pytest.mark.offline


def test_rect_edges_are_exclusive():
    rect = Rect(10, 20, 100, 50)
    assert rect.right == 110
    assert rect.bottom == 70
    assert rect.center == Point(60, 45)


def test_rect_contains_is_half_open():
    rect = Rect(0, 0, 10, 10)
    assert rect.contains(Point(0, 0))
    assert rect.contains(Point(9, 9))
    assert not rect.contains(Point(10, 10))


def test_rect_handles_negative_origin():
    """A monitor left of the primary gives negative x. Nothing may assume positive."""
    rect = Rect(-1920, -200, 1920, 1080)
    assert rect.right == 0
    assert rect.contains(Point(-1000, 0))
    assert rect.center == Point(-960, 340)


def test_rect_intersects_touching_edges_do_not_count():
    a = Rect(0, 0, 100, 100)
    b = Rect(100, 0, 100, 100)
    assert not a.intersects(b)
    assert a.intersects(Rect(99, 0, 100, 100))


def test_rect_padded():
    assert Rect(10, 10, 20, 20).padded(5) == Rect(5, 5, 30, 30)
    assert Rect(10, 10, 20, 20).padded(-5) == Rect(15, 15, 10, 10)
