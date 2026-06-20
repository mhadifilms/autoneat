"""DaVinci Resolve connection helpers for standalone autoneat."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional


@dataclass
class ResolveSession:
    resolve: Any
    project: Any
    timeline: Any


def _timeline_by_name(project: Any, name: str) -> Any:
    getter = getattr(project, "GetTimelineByName", None)
    if callable(getter):
        timeline = getter(name)
        if timeline:
            return timeline

    for index in range(1, int(project.GetTimelineCount() or 0) + 1):
        timeline = project.GetTimelineByIndex(index)
        if timeline and timeline.GetName() == name:
            return timeline
    return None


def _select_project(resolve: Any, project_name: Optional[str]) -> Any:
    manager = resolve.GetProjectManager()
    project = manager.GetCurrentProject()
    if project_name:
        if project is None or project.GetName() != project_name:
            project = manager.LoadProject(project_name)
    if project is None:
        expected = f" named {project_name!r}" if project_name else ""
        raise RuntimeError(f"No current Resolve project{expected}")
    return project


def _select_timeline(project: Any, timeline_name: Optional[str]) -> Any:
    timeline = project.GetCurrentTimeline()
    if timeline_name:
        timeline = _timeline_by_name(project, timeline_name)
        if timeline:
            project.SetCurrentTimeline(timeline)
    if timeline is None:
        expected = f" named {timeline_name!r}" if timeline_name else ""
        raise RuntimeError(f"No current Resolve timeline{expected}")
    return timeline


@contextmanager
def connect_resolve(
    *,
    project_name: Optional[str] = None,
    timeline_name: Optional[str] = None,
) -> Iterator[ResolveSession]:
    """Connect to Resolve through ``dvr`` and yield raw scripting handles."""
    try:
        from dvr import Resolve
    except ImportError as exc:
        raise RuntimeError(
            "The dvr package is required for standalone autoneat. "
            "Install autoneat into an environment with dvr available."
        ) from exc

    with Resolve() as resolve_ctx:
        resolve = getattr(resolve_ctx, "raw", resolve_ctx)
        project = _select_project(resolve, project_name)
        timeline = _select_timeline(project, timeline_name)
        yield ResolveSession(resolve=resolve, project=project, timeline=timeline)
