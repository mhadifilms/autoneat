"""Step recorder — collect step descriptors and stream them live to a sink."""

from __future__ import annotations

import time
from typing import Callable, Iterable, List, Optional


class StepRecorder:
    """Collect step descriptors and stream them as they happen.

    ``sink`` is any callable that accepts a single string. The batch
    runner passes a function that pushes the line into the UIManager's
    log TextEdit on the main thread via ``QueueEvent``.
    """

    def __init__(
        self,
        *,
        sink: Optional[Callable[[str], None]] = None,
        prefix: str = "  ",
    ) -> None:
        self.sink = sink
        self.prefix = prefix
        self.steps: List[str] = []
        self.started_at = time.time()

    def add(self, step: str) -> None:
        self.steps.append(step)
        if self.sink is not None:
            elapsed = time.time() - self.started_at
            self.sink(f"{self.prefix}[{elapsed:6.1f}s] {step}")

    def extend(self, steps: Iterable[str]) -> None:
        for step in steps:
            self.add(step)

    def elapsed(self) -> float:
        return time.time() - self.started_at
