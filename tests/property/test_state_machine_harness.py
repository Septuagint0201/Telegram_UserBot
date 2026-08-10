import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.support.state_machine import DeterministicStateMachine


def transition(state: int, event: int) -> int:
    return max(0, state + event)


@pytest.mark.property
@given(st.lists(st.integers(min_value=-3, max_value=3), max_size=100))
def test_state_machine_replay_is_deterministic(events: list[int]) -> None:
    machine = DeterministicStateMachine(0, transition)
    for event in events:
        machine.apply(event)
    assert machine.state == machine.replay(0, events)
    assert machine.history == events
