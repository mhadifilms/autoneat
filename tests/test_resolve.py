import pytest

from autoneat.resolve import _select_project, _select_timeline


class FakeTimeline:
    def __init__(self, name):
        self.name = name

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.name


class FakeProject:
    def __init__(self, name, timelines=()):
        self.name = name
        self.timelines = list(timelines)
        self.current = self.timelines[0] if self.timelines else None

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.name

    def GetCurrentTimeline(self):
        return self.current

    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True

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

    def SaveProject(self):
        return True

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
    resolve = FakeResolve(
        FakeManager(current=FakeProject("Other"), projects={"Wanted": wanted})
    )

    assert _select_project(resolve, "Wanted") is wanted


def test_select_project_errors_when_missing():
    resolve = FakeResolve(FakeManager(current=None, projects={}))

    with pytest.raises(RuntimeError, match="No current Resolve project"):
        _select_project(resolve, "Missing")


def test_select_timeline_selects_named_timeline():
    wanted = FakeTimeline("Wanted")
    project = FakeProject("Show", [FakeTimeline("Other"), wanted])

    assert _select_timeline(FakeManager(project), project, "Wanted") is wanted
    assert project.current is wanted


def test_select_timeline_errors_when_missing():
    project = FakeProject("Show", [])

    with pytest.raises(RuntimeError, match="No current Resolve timeline"):
        _select_timeline(FakeManager(project), project, "Missing")


def test_native_module_is_loaded_from_installed_sdk(tmp_path, monkeypatch):
    from autoneat import resolve as connection

    sdk = tmp_path / "Modules" / "DaVinciResolveScript.py"
    sdk.parent.mkdir()
    sdk.write_text(
        "from types import SimpleNamespace\ndef scriptapp(name):\n    assert name == 'Resolve'\n    return SimpleNamespace(GetVersion=lambda: [21, 1, 0, 14])\n"
    )
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(tmp_path))
    monkeypatch.setattr(connection, "resolve_running", lambda: True)
    assert connection.connect_local_resolve().GetVersion() == [21, 1, 0, 14]


def test_connection_does_not_launch_without_explicit_option(monkeypatch):
    from autoneat import resolve as connection

    monkeypatch.setattr(connection, "resolve_running", lambda: False)
    monkeypatch.setattr(
        connection.subprocess, "run", lambda *a, **k: pytest.fail("launched")
    )
    with pytest.raises(RuntimeError, match="Open Resolve"):
        connection.connect_local_resolve()


@pytest.mark.parametrize("version", [[20, 3], [21, 0]])
def test_native_connection_rejects_retired_api_versions(tmp_path, monkeypatch, version):
    from autoneat import resolve as connection

    sdk = tmp_path / "Modules" / "DaVinciResolveScript.py"
    sdk.parent.mkdir()
    sdk.write_text(
        f"from types import SimpleNamespace\ndef scriptapp(name):\n    return SimpleNamespace(GetVersion=lambda: {version!r})\n"
    )
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(tmp_path))
    monkeypatch.setattr(connection, "resolve_running", lambda: True)
    with pytest.raises(RuntimeError, match="21.1"):
        connection.connect_local_resolve()


def test_failed_save_blocks_project_switch(monkeypatch):
    manager = FakeManager(FakeProject("Other"), {"Wanted": FakeProject("Wanted")})
    monkeypatch.setattr(manager, "SaveProject", lambda: False)
    with pytest.raises(RuntimeError, match="save before switching projects"):
        _select_project(FakeResolve(manager), "Wanted")
    assert manager.current.name == "Other"


def test_failed_save_blocks_timeline_switch(monkeypatch):
    other, wanted = FakeTimeline("Other"), FakeTimeline("Wanted")
    project = FakeProject("Show", [other, wanted])
    manager = FakeManager(project)
    monkeypatch.setattr(manager, "SaveProject", lambda: False)
    with pytest.raises(RuntimeError, match="save before switching timelines"):
        _select_timeline(manager, project, "Wanted")
    assert project.current is other


def test_duplicate_timeline_names_are_rejected():
    project = FakeProject("Show", [FakeTimeline("Wanted"), FakeTimeline("Wanted")])
    with pytest.raises(RuntimeError, match="Ambiguous"):
        _select_timeline(FakeManager(project), project, "Wanted")


def test_batch_saves_result_and_exposes_native_handles(monkeypatch):
    from autoneat import resolve as connection

    timeline = FakeTimeline("Timeline")
    project = FakeProject("Show", [timeline])
    manager = FakeManager(project)
    resolve = FakeResolve(manager)
    saves = []
    monkeypatch.setattr(manager, "SaveProject", lambda: saves.append(project) is None)
    monkeypatch.setattr(connection, "connect_local_resolve", lambda: resolve)
    with connection.connect_resolve() as session:
        assert session.resolve is resolve
        assert session.project is project
        assert session.timeline is timeline
    assert saves == [project]


def test_batch_does_not_save_a_different_project(monkeypatch):
    from autoneat import resolve as connection

    project = FakeProject("Show", [FakeTimeline("Timeline")])
    manager = FakeManager(project)
    monkeypatch.setattr(
        connection, "connect_local_resolve", lambda: FakeResolve(manager)
    )
    monkeypatch.setattr(
        manager, "SaveProject", lambda: pytest.fail("saved wrong project")
    )
    with (
        pytest.raises(RuntimeError, match="project changed"),
        connection.connect_resolve(),
    ):
        manager.current = FakeProject("Different")
