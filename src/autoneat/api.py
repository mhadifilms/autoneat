"""Public Python API for standalone autoneat runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from autoneat.core.batch import BatchSettings, run_batch
from autoneat.resolve import connect_resolve


@dataclass
class ProfileOptions(BatchSettings):
    """Options for a standalone Neat Video Auto Profile batch."""

    project_name: Optional[str] = None
    timeline_name: Optional[str] = None


def run_profile(
    options: ProfileOptions,
    *,
    resolve: Any = None,
    project: Any = None,
    timeline: Any = None,
    sink: Optional[Callable[[str], None]] = None,
    cancel_event: Any = None,
) -> dict:
    """Run Auto Profile against a Resolve project/timeline.

    Tests and embedding callers may pass explicit Resolve handles. Normal CLI
    usage lets autoneat connect through ``dvr`` and select the requested
    project/timeline.
    """
    if resolve is not None and project is not None and timeline is not None:
        return run_batch(
            resolve,
            project,
            timeline,
            options,
            sink=sink,
            cancel_event=cancel_event,
        )

    with connect_resolve(
        project_name=options.project_name,
        timeline_name=options.timeline_name,
    ) as session:
        return run_batch(
            session.resolve,
            session.project,
            session.timeline,
            options,
            sink=sink,
            cancel_event=cancel_event,
        )
