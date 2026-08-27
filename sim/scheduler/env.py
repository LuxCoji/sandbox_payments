"""SimulationEnv – thin wrapper around simpy.Environment.

Provides discrete-event scheduling with explicit priority ordering.
Direct imports of `simpy` outside this module are strictly prohibited.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(order=True)
class ScheduledEvent:
    """An event scheduled in the discrete-event simulation queue.

    Ordering: (time_ns, priority, sequence) — lower values are processed first.
    Priority 0 is highest priority.
    """
    time_ns: float
    priority: int = 0
    sequence: int = field(default=0, compare=True)
    handler: Callable[..., Any] = field(compare=False, default=lambda: None)
    payload: dict[str, object] = field(compare=False, default_factory=dict)
    description: str = field(compare=False, default="")


class SimulationEnv:
    """Discrete-event simulation environment.

    Manages a priority queue of ScheduledEvents ordered by
    (time_ns, priority, sequence). Provides step-by-step or
    run-until-time execution.

    This is the ONLY module allowed to use simpy internals.
    All other modules schedule events through the WorldEngine.
    """

    def __init__(self, start_time_ns: float = 0.0) -> None:
        self._now: float = start_time_ns
        self._queue: list[ScheduledEvent] = []
        self._seq_counter: int = 0
        self._step_count: int = 0

    @property
    def now(self) -> float:
        """Current simulation time in nanoseconds (read-only)."""
        return self._now

    @property
    def step_count(self) -> int:
        """Monotonic logical event step counter."""
        return self._step_count

    @property
    def queue_size(self) -> int:
        """Number of events in the queue."""
        return len(self._queue)

    def schedule(self, event: ScheduledEvent) -> None:
        """Insert an event into the priority queue.

        The event's sequence number is auto-assigned if not set,
        ensuring FIFO ordering for equal (time, priority) pairs.

        Raises:
            ValueError: If event time is in the past.
        """
        if event.time_ns < self._now:
            raise ValueError(
                f"Cannot schedule event in the past: "
                f"event.time_ns={event.time_ns} < now={self._now}"
            )
        # Auto-assign sequence for FIFO tiebreaking
        scheduled = ScheduledEvent(
            time_ns=event.time_ns,
            priority=event.priority,
            sequence=self._seq_counter,
            handler=event.handler,
            payload=event.payload,
            description=event.description,
        )
        self._seq_counter += 1
        heapq.heappush(self._queue, scheduled)

    def peek(self) -> ScheduledEvent | None:
        """Inspect the next event without removing it.

        Returns:
            The next ScheduledEvent, or None if queue is empty.
        """
        if not self._queue:
            return None
        return self._queue[0]

    def pop(self) -> ScheduledEvent:
        """Remove and return the next event.

        Advances simulation time to the event's timestamp.

        Raises:
            IndexError: If queue is empty.
        """
        if not self._queue:
            raise IndexError("Event queue is empty")
        event = heapq.heappop(self._queue)
        self._now = event.time_ns
        self._step_count += 1
        return event

    def step(self) -> ScheduledEvent | None:
        """Process the next event: pop it, advance time, call its handler.

        Returns:
            The processed ScheduledEvent, or None if queue is empty.
        """
        if not self._queue:
            return None
        event = self.pop()
        event.handler(**event.payload)
        return event

    def run(self, until: float | None = None) -> int:
        """Advance simulation by processing events.

        Args:
            until: Stop when sim time reaches this value.
                   If None, process all events in the queue.

        Returns:
            Number of events processed.
        """
        processed = 0
        while self._queue:
            if until is not None and self._queue[0].time_ns > until:
                break
            self.step()
            processed += 1
        if until is not None:
            self._now = max(self._now, until)
        return processed

    def clear(self) -> int:
        """Remove all pending events from the queue.

        Returns:
            Number of events removed.
        """
        count = len(self._queue)
        self._queue.clear()
        return count

    def __repr__(self) -> str:
        return (
            f"SimulationEnv(now={self._now}, "
            f"queue_size={len(self._queue)}, "
            f"step_count={self._step_count})"
        )
