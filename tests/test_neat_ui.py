from pathlib import Path

import pytest

from autoneat import _neat_ui as neat_ui


def _row(text: str) -> dict:
    return {
        "text": text,
        "left": 0,
        "top": 0,
        "width": 100,
        "height": 20,
        "_scale": 1,
        "_screen_width": 1920,
        "_screen_height": 1080,
    }


def test_doppler_window_picker_text_is_not_neat_information_modal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        neat_ui,
        "_ocr_screen",
        lambda work_dir: [
            _row("Load Profile"),
            _row("Noise Level"),
            _row("Profile Check"),
        ],
    )
    monkeypatch.setattr(
        neat_ui,
        "_ocr_modal_band",
        lambda work_dir: [
            _row("doppler is requesting to bypass the system private window picker"),
            _row("not selected"),
        ],
    )

    state, text, _rows = neat_ui._read_screen_state(tmp_path)

    assert state == "editor-profiled"
    assert "doppler" not in text


def test_neat_information_modal_still_matches():
    assert (
        neat_ui._screen_state_from_text(
            "Information frames from the beginning of the clip are not selected before opening"
        )
        == "information-dialog"
    )


def test_editor_control_geometry_uses_neat_window(monkeypatch):
    monkeypatch.setattr(
        neat_ui,
        "_neat_editor_window",
        lambda: {"position": [230, 84], "size": [1460, 880]},
    )

    assert neat_ui._editor_control_geometry_point("auto-profile") == (390.0, 166.0)
    assert neat_ui._editor_control_geometry_point("apply") == pytest.approx((1386.32, 870.72))


def test_editor_profile_ready_requires_neat_editor_window(monkeypatch, tmp_path: Path):
    def fail_capture(_path: Path):
        raise AssertionError("screen capture should not run without a Neat editor window")

    monkeypatch.setattr(neat_ui, "_neat_editor_window", lambda: None)
    monkeypatch.setattr(neat_ui, "_capture_screen", fail_capture)

    assert neat_ui.editor_profile_ready(tmp_path) is None
