"""Vision OCR + cliclick state machine that drives Neat's Qt UI.

This module is intentionally narrow: it knows how to read the current screen
state from OCR text and how to click named buttons inside the Neat plugin
window. Higher-level orchestration (per-clip flow, retries) lives in
``batch.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from autoneat_lib.core import ocr, windows
from autoneat_lib.core.subprocess_utils import require_tool, run_proc


# ---------------------------------------------------------------------------
# State recognition
# ---------------------------------------------------------------------------


def _state_from_text(text: str) -> str:
    text = text.lower()
    if "continue" in text and "neat video" in text:
        return "demo-splash"
    if "information" in text or "not selected" in text or "before opening neat video" in text:
        return "information-dialog"
    if "preparing input" in text or "preparing frames" in text:
        return "preparing-input"
    if "auto profile" in text or "profile not ready" in text:
        return "editor-unprofiled"
    if "noise level" in text or "profile check" in text or "build profile" in text:
        return "editor-profiled"
    if "neat video" in text and "beginner mode" in text:
        return "editor-unprofiled"
    if "prepare noise profile" in text:
        return "inspector-prepare"
    if "apply" in text and "neat video" in text:
        return "editor"
    return "unknown"


def read_screen_state(work_dir: Path) -> Tuple[str, str, List[Dict[str, Any]]]:
    """Capture+OCR the screen and classify what's on it."""
    rows = ocr.ocr_full_screen(work_dir)
    text = ocr.screen_text(rows)
    state = _state_from_text(text)
    if state == "unknown":
        # 4K displays squash small Neat-window text at the default scale; a
        # second pass on a centered region at higher scale picks it up.
        width, height = ocr.screen_size_from_rows(rows)
        crop = (int(width * 0.12), int(height * 0.02), int(width * 0.78), int(height * 0.92))
        region_rows = ocr.ocr_screen_region(work_dir, name="neat-area", rect=crop, scale=4)
        region_text = ocr.screen_text(region_rows)
        region_state = _state_from_text(region_text)
        if region_state != "unknown":
            return region_state, region_text[:500], region_rows
    return state, text[:500], rows


# ---------------------------------------------------------------------------
# Button locator
# ---------------------------------------------------------------------------

CONTROL_LABELS = {"apply", "auto-profile", "continue", "ok"}


def _button_point_from_rows(label: str, rows: Sequence[Dict[str, Any]]) -> Optional[Tuple[float, float]]:
    if label == "auto-profile":
        return ocr.find_phrase_center(rows, ["auto", "profile"])
    if label == "continue":
        return ocr.find_word_center(rows, "continue")
    if label == "ok":
        return ocr.find_word_center(rows, "ok", prefer_bottom_right=True)
    if label == "apply":
        return ocr.find_word_center(rows, "apply", prefer_bottom_right=True)
    return None


def locate_button(label: str, work_dir: Path, *, window: Dict[str, Any]) -> Tuple[float, float]:
    """OCR the appropriate region for ``label`` and return its center.

    There is exactly one OCR pass per call. If it misses, we save diagnostic
    artifacts and raise — no full-screen retry, no keyboard fallback.
    """
    if label == "auto-profile":
        size = window.get("size") or [0, 0]
        toolbar = (0, 35, min(int(size[0]), 520), 90)
        rows = ocr.ocr_window(window, work_dir, name=f"{label}-toolbar", scale=8, rel_box=toolbar)
    elif label == "apply":
        # Apply lives at the bottom of the Neat dialog. OCR'ing a tight
        # bottom strip at high scale gives Vision OCR much better recognition
        # than a full-window pass at scale=4 — at 4K timeline resolutions
        # the dialog is huge and "Apply" gets squashed in a wider crop.
        size = window.get("size") or [0, 0]
        win_w = max(int(size[0]), 1)
        win_h = max(int(size[1]), 1)
        strip_left = int(win_w * 0.4)
        strip_top = max(0, win_h - 200)
        rows = ocr.ocr_window(
            window,
            work_dir,
            name=f"{label}-bottom-strip",
            scale=8,
            rel_box=(strip_left, strip_top, win_w - strip_left, min(200, win_h)),
        )
    else:
        rows = ocr.ocr_window(window, work_dir, name=f"{label}-window", scale=4)

    point = _button_point_from_rows(label, rows)
    if point is None:
        artifacts = ocr.save_locate_failure(label, work_dir, rows, window)
        text = ocr.screen_text(rows)[:500]
        raise RuntimeError(
            f"Could not OCR-locate `{label}` button. "
            f"window pos={window.get('position')} size={window.get('size')}. "
            f"Diagnostic artifacts: {artifacts}. "
            f"OCR text: {text!r}"
        )
    return point


# ---------------------------------------------------------------------------
# Click + calibration cache
# ---------------------------------------------------------------------------


def _click_at(x: float, y: float) -> None:
    cliclick = require_tool("cliclick")
    windows.activate_resolve(settle=0.25)
    xi = int(round(float(x)))
    yi = int(round(float(y)))
    proc = run_proc([cliclick, f"m:{xi},{yi}", f"c:{xi},{yi}"], timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "cliclick failed")


def click_at_window_offset(window: Dict[str, Any], offset_x: float, offset_y: float) -> Tuple[float, float]:
    windows.raise_window(window)
    point = windows.window_point(window, offset_x, offset_y)
    _click_at(point[0], point[1])
    return point


def click_ocr_button(label: str, work_dir: Path, *, window: Dict[str, Any]) -> Tuple[float, float]:
    point = locate_button(label, work_dir, window=window)
    _click_at(point[0], point[1])
    return point


@dataclass
class UiCalibration:
    """Run-local cache of OCR-derived window-relative button offsets.

    Once a label's offset is known for a given window scope, subsequent
    clicks reuse it without OCR — the only effective speed-up across many
    similar clips on the same machine.
    """

    neat_offsets: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    modal_offsets: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    hits: Dict[str, int] = field(default_factory=dict)
    misses: Dict[str, int] = field(default_factory=dict)

    def _bucket(self, scope: str) -> Dict[str, Tuple[float, float]]:
        return self.modal_offsets if scope == "modal" else self.neat_offsets

    def cached_offset(self, scope: str, label: str) -> Optional[Tuple[float, float]]:
        return self._bucket(scope).get(label)

    def record_hit(self, scope: str, label: str) -> None:
        key = f"{scope}:{label}"
        self.hits[key] = self.hits.get(key, 0) + 1

    def record_miss(self, scope: str, label: str) -> None:
        key = f"{scope}:{label}"
        self.misses[key] = self.misses.get(key, 0) + 1

    def store_offset(self, scope: str, label: str, window: Dict[str, Any], point: Tuple[float, float]) -> None:
        position = window.get("position") or [0, 0]
        offset = (float(point[0]) - float(position[0]), float(point[1]) - float(position[1]))
        self._bucket(scope)[label] = offset

    def as_dict(self) -> Dict[str, Any]:
        def fmt(values: Dict[str, Tuple[float, float]]) -> Dict[str, List[float]]:
            return {label: [round(x, 2), round(y, 2)] for label, (x, y) in sorted(values.items())}

        return {
            "method": "dynamic-ocr-with-run-cache",
            "neat_offsets": fmt(self.neat_offsets),
            "modal_offsets": fmt(self.modal_offsets),
            "hits": dict(sorted(self.hits.items())),
            "misses": dict(sorted(self.misses.items())),
        }


# ---------------------------------------------------------------------------
# High-level click-control: dispatch a label to its window scope
# ---------------------------------------------------------------------------


def click_control(
    label: str,
    work_dir: Path,
    calibration: UiCalibration,
) -> Tuple[Tuple[float, float], str]:
    """Click a named control. Picks the correct OS window for the label,
    consults the run-local OCR offset cache, and falls through to OCR on
    cache miss. Each label has exactly one window source — no fallbacks."""
    win_list = windows.list_resolve_windows(activate=True)
    if label in {"continue", "ok"}:
        # Continue/OK are on a separate modal window (demo splash, info
        # dialog). The Neat editor itself is a different OS window.
        window = windows.find_modal_window(win_list) or windows.find_neat_window(win_list)
        if window is None:
            raise RuntimeError(f"Could not locate modal/Neat window for {label}")
        scope = "modal"
    else:
        window = windows.find_neat_window(win_list)
        if window is None:
            raise RuntimeError(f"Could not locate Neat window for {label}")
        scope = "neat"

    cached = calibration.cached_offset(scope, label)
    if cached is not None:
        point = click_at_window_offset(window, cached[0], cached[1])
        calibration.record_hit(scope, label)
        return point, f"cached-{scope}-window"

    point = click_ocr_button(label, work_dir, window=window)
    calibration.store_offset(scope, label, window, point)
    calibration.record_miss(scope, label)
    return point, f"ocr-{scope}-window"
