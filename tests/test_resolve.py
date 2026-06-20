import pytest

from autoneat.resolve import _select_project, _select_timeline


class FakeTimeline:
    def __init__(self, name):
        self.name = name

    def GetName(self):
        return self.name


class FakeProject:
    def __init__(self, name, timelines=()):
        self.name = name
        self.timelines = list(timelines)
        self.current = self.timelines[0] if self.timelines else None

    def GetName(self):
        return self.name

    def GetCurrentTimeline(self):
        return self.current

    def SetCurrentTimeline(self, timeline):
        self.current = timeline

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, index):
        return self.timelines[index - 1]


class FakeManager:
    def __init__(self, current=None, projects=None):
        self.current = current
        self.projects = projects or {}

    def GetCurrentProject(self):
        return self.current

    def LoadProject(self, name):
        self.current = self.projects.get(name)
        return self.current


class FakeResolve:
    def __init__(self, manager):
        self.manager = manager

    def GetProjectManager(self):
        return self.manager


def test_select_project_loads_requested_project():
    wanted = FakeProject("Wanted")
    resolve = FakeResolve(FakeManager(current=FakeProject("Other"), projects={"Wanted": wanted}))

    assert _select_project(resolve, "Wanted") is wanted


def test_select_project_errors_when_missing():
    resolve = FakeResolve(FakeManager(current=None, projects={}))

    with pytest.raises(RuntimeError, match="No current Resolve project"):
        _select_project(resolve, "Missing")


def test_select_timeline_selects_named_timeline():
    wanted = FakeTimeline("Wanted")
    project = FakeProject("Show", [FakeTimeline("Other"), wanted])

    assert _select_timeline(project, "Wanted") is wanted
    assert project.current is wanted


def test_select_timeline_errors_when_missing():
    project = FakeProject("Show", [])

    with pytest.raises(RuntimeError, match="No current Resolve timeline"):
        _select_timeline(project, "Missing")
