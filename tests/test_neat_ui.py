from pathlib import Path

import pytest

from autoneat import _neat_ui as neat_ui
from autoneat import _neat_vision as neat_vision


def test_clear_templates_removes_learned_pngs(monkeypatch, tmp_path: Path):
    """Session reset wipes learned templates so a restart re-learns via OCR."""
    monkeypatch.setenv(neat_vision._TEMPLATE_DIR_ENV, str(tmp_path))
    (tmp_path / "auto-profile@1920x1080.png").write_bytes(b"x")
    (tmp_path / "apply@1920x1080.png").write_bytes(b"x")
    (tmp_path / "keep.txt").write_text("not a template")

    removed = neat_vision.clear_templates()

    assert removed == 2
    assert not list(tmp_path.glob("*.png"))
    assert (tmp_path / "keep.txt").exists()
    # Idempotent: clearing again removes nothing.
    assert neat_vision.clear_templates() == 0


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


def test_editor_controls_locate_via_ocr_not_hardcoded(monkeypatch, tmp_path: Path):
    """auto-profile/apply/cancel must resolve via OCR (window-anchored), never a
    hardcoded window-offset shortcut. The old geometry helper is gone, and
    ``locate_and_click`` falls through to the window-anchored OCR locate."""
    assert not hasattr(neat_ui, "_editor_control_geometry_point")

    loc = neat_ui.Locator(tmp_path, learn=False)
    monkeypatch.setattr(loc, "_capture_stable", lambda label: None)
    monkeypatch.setattr(neat_ui, "_locate_editor_control", lambda label, wd: (501.0, 207.0))
    monkeypatch.setattr(neat_ui, "_click_at_quartz", lambda x, y: None)

    point, method = loc.locate_and_click("auto-profile", editor=True)
    assert point == (501.0, 207.0)
    assert method == "window"


def test_editor_profile_ready_requires_neat_editor_window(monkeypatch, tmp_path: Path):
    def fail_capture(_path: Path):
        raise AssertionError("screen capture should not run without a Neat editor window")

    monkeypatch.setattr(neat_ui, "_neat_editor_window", lambda: None)
    monkeypatch.setattr(neat_ui, "_capture_screen", fail_capture)

    assert neat_ui.editor_profile_ready(tmp_path) is None
