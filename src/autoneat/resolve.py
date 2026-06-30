"""DaVinci Resolve connection helpers for standalone autoneat."""

from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from inspect import Parameter, signature
from typing import Any, Iterator, Optional


def _resolve_kwargs(auto_launch: bool, timeout: float) -> dict:
    """Build dvr ``Resolve(...)`` kwargs, adding ``discover_remote=False`` when
    the installed dvr supports it so autoneat only ever drives local Resolve."""
    kwargs: dict = {"auto_launch": auto_launch, "timeout": timeout}
    try:
        params = signature(_import_resolve()).parameters
    except (TypeError, ValueError):
        return kwargs
    if "discover_remote" in params or any(
        p.kind is Parameter.VAR_KEYWORD for p in params.values()
    ):
        kwargs["discover_remote"] = False
    return kwargs


def _import_resolve() -> Any:
    try:
        from dvr import Resolve
    except ImportError as exc:  # pragma: no cover - import guard
        raise RuntimeError(
            "The dvr package is required for autoneat. "
            "Install autoneat into an environment with dvr available."
        ) from exc
    return Resolve


def connect_resolve_raw(*, auto_launch: bool = False, timeout: float = 30.0) -> Any:
    """Connect to the local Resolve through ``dvr`` and return the raw handle.

    Neat Video's OFX UI automation iterates Resolve's raw scripting objects, so
    callers need the underlying fusionscript handle rather than the dvr wrapper.
    """
    Resolve = _import_resolve()
    resolve = Resolve(**_resolve_kwargs(auto_launch, timeout))
    return getattr(resolve, "raw", resolve)


def resolve_running() -> bool:
    """Best-effort check whether DaVinci Resolve is running on this machine."""
    try:
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            name = "Resolve" if sys.platform == "darwin" else "resolve"
            result = subprocess.run(
                ["pgrep", "-x", name], capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Resolve.exe"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return "Resolve.exe" in result.stdout
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return False


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
