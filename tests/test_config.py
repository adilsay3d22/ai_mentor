"""Settings and the SR4 capture blocklist, offline."""

from __future__ import annotations

import pytest

from mentor import config

pytestmark = pytest.mark.offline


def test_password_managers_are_blocked_by_default():
    assert config.is_blocked("1password.exe")
    assert config.is_blocked("bitwarden.exe")
    assert config.is_blocked("keepassxc.exe")


def test_blocklist_ignores_case_and_surrounding_space():
    assert config.is_blocked("  1Password.EXE ")


def test_ordinary_applications_are_not_blocked():
    assert not config.is_blocked("notepad.exe")
    assert not config.is_blocked("photoshop.exe")


def test_an_unknown_process_is_not_blocked():
    """process_for_window returns an empty name when the process has already gone."""
    assert not config.is_blocked("")


def test_debug_directory_is_inside_the_project():
    """SR5: screenshots are never written outside ./debug/."""
    assert config.DEBUG_DIR.parent == config.PROJECT_ROOT
    assert config.LOG_PATH.parent == config.DEBUG_DIR
