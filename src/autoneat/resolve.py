"""Local Resolve Studio 21.1 scripting and exact batch selection."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def native_sdk_path() -> Path:
    base = os.environ.get("RESOLVE_SCRIPT_API")
    if base:
        return Path(base) / "Modules/DaVinciResolveScript.py"
    if sys.platform == "darwin":
        base = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
    elif sys.platform == "win32":
        base = str(
            Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
            / "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting"
        )
    else:
        base = "/opt/resolve/Developer/Scripting"
    return Path(base) / "Modules/DaVinciResolveScript.py"


def resolve_running() -> bool:
    try:
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            name = "Resolve" if sys.platform == "darwin" else "resolve"
            result = subprocess.run(
                ["pgrep", "-x", name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return result.returncode == 0
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Resolve.exe"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return "Resolve.exe" in result.stdout
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return False


def connect_local_resolve(*, launch: bool = False, timeout: float = 30.0) -> Any:
    """Return the native application handle; never discover a remote server."""
    if not resolve_running():
        if not launch:
            raise RuntimeError("Open Resolve on this computer before running AutoNeat")
        if sys.platform == "darwin":
            subprocess.run(["open", "-a", "DaVinci Resolve"], check=True)
        elif sys.platform.startswith("linux"):
            subprocess.Popen(
                ["/opt/resolve/bin/resolve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            raise RuntimeError("Open Resolve manually on this computer")
    path = native_sdk_path()
    spec = importlib.util.spec_from_file_location("DaVinciResolveScript", path)
    if spec is None or spec.loader is None or not path.is_file():
        raise RuntimeError(f"Resolve scripting module is unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    deadline = time.monotonic() + timeout
    while True:
        resolve = module.scriptapp("Resolve")
        if resolve is not None:
            if tuple(int(part) for part in resolve.GetVersion()[:2]) < (21, 1):
                raise RuntimeError("AutoNeat requires Resolve Studio 21.1 or newer")
            return resolve
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Resolve's local scripting API did not respond; enable External scripting: Local"
            )
        time.sleep(0.25)


@dataclass
class ResolveSession:
    resolve: Any
    project: Any
    timeline: Any


def _select_project(resolve: Any, project_name: str | None) -> Any:
    manager = resolve.GetProjectManager()
    project = manager.GetCurrentProject()
    if project_name and (project is None or project.GetName() != project_name):
        if project is not None and not manager.SaveProject():
            raise RuntimeError("Resolve refused to save before switching projects")
        project = manager.LoadProject(project_name)
    if project is None or project_name and project.GetName() != project_name:
        raise RuntimeError(f"No current Resolve project named {project_name!r}")
    return project


def _select_timeline(manager: Any, project: Any, timeline_name: str | None) -> Any:
    timeline = project.GetCurrentTimeline()
    if timeline_name:
        matches = [
            project.GetTimelineByIndex(index)
            for index in range(1, project.GetTimelineCount() + 1)
            if project.GetTimelineByIndex(index).GetName() == timeline_name
        ]
        if len(matches) > 1:
            raise RuntimeError(f"Ambiguous Resolve timeline {timeline_name!r}")
        timeline = matches[0] if matches else None
        if timeline is not None:
            if not manager.SaveProject():
                raise RuntimeError("Resolve refused to save before switching timelines")
            if not project.SetCurrentTimeline(timeline):
                raise RuntimeError("Resolve refused the selected timeline")
            if project.GetCurrentTimeline().GetUniqueId() != timeline.GetUniqueId():
                raise RuntimeError("Resolve activated a different timeline")
    if timeline is None:
        raise RuntimeError(f"No current Resolve timeline named {timeline_name!r}")
    return timeline


@contextmanager
def connect_resolve(
    *, project_name: str | None = None, timeline_name: str | None = None
) -> Iterator[ResolveSession]:
    """Select a batch context containing direct native SDK handles."""
    resolve = connect_local_resolve()
    manager = resolve.GetProjectManager()
    project = _select_project(resolve, project_name)
    timeline = _select_timeline(manager, project, timeline_name)
    try:
        yield ResolveSession(resolve=resolve, project=project, timeline=timeline)
    finally:
        current = manager.GetCurrentProject()
        if current is None or current.GetUniqueId() != project.GetUniqueId():
            raise RuntimeError("Resolve project changed during AutoNeat")
        if not manager.SaveProject():
            raise RuntimeError("Resolve refused to save the AutoNeat result")
