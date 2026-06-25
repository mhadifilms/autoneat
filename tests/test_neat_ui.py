from pathlib import Path

from autoneat import _neat_ui


def test_editor_profile_ready_requires_neat_editor_window(monkeypatch, tmp_path: Path):
    def fail_capture(_path: Path):
        raise AssertionError("screen capture should not run without a Neat editor window")

    monkeypatch.setattr(_neat_ui, "_neat_editor_window", lambda: None)
    monkeypatch.setattr(_neat_ui, "_capture_screen", fail_capture)

    assert _neat_ui.editor_profile_ready(tmp_path) is None
