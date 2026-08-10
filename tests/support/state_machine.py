"""Small deterministic replay harness used before domain state machines exist."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field


@dataclass(slots=True)
class DeterministicStateMachine[StateT, EventT]:
    state: StateT
    transition: Callable[[StateT, EventT], StateT]
    history: list[EventT] = field(default_factory=list)

    def apply(self, event: EventT) -> StateT:
        self.state = self.transition(self.state, event)
        self.history.append(event)
        return self.state

    def replay(self, initial: StateT, events: Iterable[EventT]) -> StateT:
        state = initial
        for event in events:
            state = self.transition(state, event)
        return state
