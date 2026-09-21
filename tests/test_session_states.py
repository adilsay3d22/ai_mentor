"""The state machine's transition table and the hotkey parser. Offline."""

from __future__ import annotations

import pytest

from mentor.session.states import ALLOWED, State, may_move
from mentor.ui import hotkey

pytestmark = pytest.mark.offline


def test_every_state_has_a_transition_rule():
    """A state missing from the table can never be left."""
    assert set(ALLOWED) == set(State)


def test_a_session_always_starts_by_planning():
    assert may_move(State.IDLE, State.PLANNING)
    assert not may_move(State.IDLE, State.SHOWING)


def test_planning_may_re_plan_itself():
    """A rejected step is re-planned without leaving the state."""
    assert may_move(State.PLANNING, State.PLANNING)


def test_showing_may_be_cancelled_straight_back_to_idle():
    """FR1: the hotkey or Esc dismisses everything immediately."""
    assert may_move(State.SHOWING, State.IDLE)


def test_every_working_state_can_reach_failed():
    for state in (State.PLANNING, State.SHOWING, State.WAITING, State.VERIFYING):
        assert may_move(state, State.FAILED), state


def test_a_finished_session_can_start_another():
    assert may_move(State.DONE, State.PLANNING)
    assert may_move(State.FAILED, State.PLANNING)


def test_states_compare_and_log_as_their_own_names():
    assert State.SHOWING == "showing"
    assert f"{State.PLANNING}" == "planning"


# --- the hotkey parser -------------------------------------------------------


def test_parsing_the_default_hotkey():
    modifiers, key = hotkey.parse("ctrl+shift+space")
    assert modifiers & 0x0002  # MOD_CONTROL
    assert modifiers & 0x0004  # MOD_SHIFT
    assert key == 0x20


def test_repeat_is_always_suppressed():
    """Without MOD_NOREPEAT, holding the keys fires many times a second."""
    modifiers, _key = hotkey.parse("ctrl+shift+space")
    assert modifiers & 0x4000


def test_parsing_is_case_and_space_insensitive():
    assert hotkey.parse("Ctrl + Shift + Space") == hotkey.parse("ctrl+shift+space")


def test_letters_and_function_keys_parse():
    assert hotkey.parse("ctrl+m")[1] == ord("M")
    assert hotkey.parse("ctrl+f4")[1] == 0x73


def test_a_combination_with_no_real_key_is_rejected():
    with pytest.raises(hotkey.HotkeyError, match="no non-modifier key"):
        hotkey.parse("ctrl+shift")


def test_two_non_modifier_keys_are_rejected():
    with pytest.raises(hotkey.HotkeyError, match="more than one"):
        hotkey.parse("ctrl+a+b")


def test_an_unknown_key_is_rejected_rather_than_silently_bound_wrong():
    with pytest.raises(hotkey.HotkeyError, match="unknown key"):
        hotkey.parse("ctrl+shift+nonsense")


def test_an_empty_combination_is_rejected():
    with pytest.raises(hotkey.HotkeyError, match="empty hotkey"):
        hotkey.parse("   ")
