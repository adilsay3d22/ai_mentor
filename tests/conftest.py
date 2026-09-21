"""Test configuration.

Qt is imported by some offline tests for its geometry types only. Forcing the
offscreen platform plugin keeps them from trying to touch a real display, so
``pytest -m offline`` stays honest about needing no screen.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
