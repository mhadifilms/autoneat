"""Apple Vision OCR runner + screen capture helpers.

Pure-stdlib by design: no Pillow, no third-party Python packages. We
rely on macOS's built-in tools — ``screencapture`` for region capture,
``sips`` for the upscale that helps Vision pick up small Qt button text,
and the bundled ``vision_ocr.swift`` script for OCR itself.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from autoneat.core.subprocess_utils import require_tool, resource_dir, run_proc
from autoneat.core.windows import activate_resolve


def cache_base() -> Path:
    base = Path.home() / ".cache" / "autoneat"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _capture_screen(output: Path, *, rect: Optional[Tuple[int, int, int, int]] = None) -> None:
    """Snapshot the screen (or a region) directly into a file.

    Using ``screencapture -R`` instead of capturing the full desktop and
    cropping in Python lets us avoid Pillow entirely.
    """
    activate_resolve(settle=0.2)
    args = ["screencapture", "-x"]
    if rect is not None:
        x, y, w, h = (int(v) for v in rect)
        args.extend(["-R", f"{x},{y},{w},{h}"])
    args.append(str(output))
    proc = run_proc(args, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "screencapture failed")


def _vision_script_path() -> Path:
    """Return the path to the bundled ``vision_ocr.swift`` script."""
    candidate = resource_dir() / "vision_ocr.swift"
    if candidate.is_file():
        return candidate
    raise RuntimeError(f"Bundled vision_ocr.swift not found under {resource_dir()}")


def _sips_upscale(src: Path, dst: Path, *, scale: int) -> None:
    """Upscale an image by ``scale`` factor using macOS's ``sips``.

    Vision OCR is drastically more reliable on small UI text after a 2-8x
    upscale. ``sips`` ships with macOS so we get this preprocessing step
    without any pip-time dependencies.
    """
    if scale <= 1:
        if src != dst:
            shutil.copy2(src, dst)
        return
    sips = require_tool("sips")
    proc = run_proc(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(src)], timeout=10)
    width = height = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            width = int(line.split(":", 1)[1].strip())
        elif line.startswith("pixelHeight:"):
            height = int(line.split(":", 1)[1].strip())
    if not (width and height):
        raise RuntimeError(f"Could not read pixel dimensions of {src} via sips")
    new_w = width * scale
    new_h = height * scale
    proc = run_proc(
        [
            sips,
            "--resampleHeightWidth",
            str(new_h),
            str(new_w),
            str(src),
            "--out",
            str(dst),
        ],
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "sips upscale failed")


def vision_ocr_image(image: Path) -> List[Dict[str, Any]]:
    swift = require_tool("swift")
    proc = run_proc([swift, str(_vision_script_path()), str(image)], timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "Vision OCR failed")
    rows = json.loads(proc.stdout or "[]")
    if not isinstance(rows, list):
        raise RuntimeError("Vision OCR returned non-list JSON")
    return [row for row in rows if isinstance(row, dict)]


def _screen_dimensions(work_dir: Path) -> Tuple[int, int]:
    sample = work_dir / "screen-probe.png"
    _capture_screen(sample)
    sips = require_tool("sips")
    proc = run_proc(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(sample)], timeout=10)
    width = height = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            width = int(line.split(":", 1)[1].strip())
        elif line.startswith("pixelHeight:"):
            height = int(line.split(":", 1)[1].strip())
    sample.unlink(missing_ok=True)
    if not (width and height):
        raise RuntimeError("Could not determine screen dimensions via sips")
    return width, height


def ocr_screen_region(
    work_dir: Path,
    *,
    name: str,
    rect: Optional[Tuple[int, int, int, int]] = None,
    scale: int = 2,
) -> List[Dict[str, Any]]:
    """Capture (a region of) the screen, upscale it, and OCR it."""
    if rect is None:
        screen_w, screen_h = _screen_dimensions(work_dir)
        offset_x = offset_y = 0
        capture_rect: Optional[Tuple[int, int, int, int]] = None
    else:
        offset_x, offset_y, _, _ = rect
        capture_rect = rect
        screen_w = screen_h = 0

    raw = work_dir / f"{name}-raw.png"
    upscaled = work_dir / f"{name}-x{scale}.png"
    _capture_screen(raw, rect=capture_rect)
    _sips_upscale(raw, upscaled, scale=scale)

    rows = vision_ocr_image(upscaled)
    for row in rows:
        row["_scale"] = scale
        row["_offset_x"] = offset_x
        row["_offset_y"] = offset_y
        if screen_w:
            row["_screen_width"] = screen_w
        if screen_h:
            row["_screen_height"] = screen_h
    return rows


def ocr_full_screen(work_dir: Path, *, scale: int = 2) -> List[Dict[str, Any]]:
    return ocr_screen_region(work_dir, name="screen", scale=scale)


def ocr_window(
    window: Dict[str, Any],
    work_dir: Path,
    *,
    name: str,
    scale: int = 4,
    rel_box: Optional[Tuple[int, int, int, int]] = None,
) -> List[Dict[str, Any]]:
    position = window.get("position") or [0, 0]
    size = window.get("size") or [0, 0]
    if len(position) < 2 or len(size) < 2:
        raise RuntimeError("Window has no position/size for OCR")
    if rel_box is None:
        screen_box = (int(position[0]), int(position[1]), int(size[0]), int(size[1]))
    else:
        screen_box = (
            int(position[0]) + int(rel_box[0]),
            int(position[1]) + int(rel_box[1]),
            int(rel_box[2]),
            int(rel_box[3]),
        )
    return ocr_screen_region(work_dir, name=name, rect=screen_box, scale=scale)


def text_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        if "\n" in text or "\t" in text or len(text) > 80:
            continue
        filtered.append(row)
    return filtered


def screen_text(rows: Sequence[Dict[str, Any]]) -> str:
    return " ".join(str(r.get("text") or "").strip() for r in text_rows(rows)).lower()


def screen_size_from_rows(rows: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
    for row in rows:
        width = int(row.get("_screen_width") or 0)
        height = int(row.get("_screen_height") or 0)
        if width and height:
            return width, height
    return 1920, 1080


def row_center(row: Dict[str, Any]) -> Tuple[float, float]:
    scale = int(row.get("_scale") or 1)
    offset_x = int(row.get("_offset_x") or 0)
    offset_y = int(row.get("_offset_y") or 0)
    left = float(row.get("left") or 0)
    top = float(row.get("top") or 0)
    width = float(row.get("width") or 0)
    height = float(row.get("height") or 0)
    return (offset_x + (left + width / 2.0) / scale, offset_y + (top + height / 2.0) / scale)


def find_word_center(
    rows: Sequence[Dict[str, Any]],
    word: str,
    *,
    prefer_bottom_right: bool = False,
) -> Optional[Tuple[float, float]]:
    needle = word.lower()
    candidates: List[Tuple[float, float, Dict[str, Any]]] = []
    for row in text_rows(rows):
        text = str(row.get("text") or "").lower().strip(".,:;()[]{}'\"|")
        if text == needle:
            x, y = row_center(row)
            score = 100000.0 + (y + x / 10000.0 if prefer_bottom_right else 0.0)
            candidates.append((score, x, row))
        elif needle in text:
            x, y = row_center(row)
            score = y + x / 10000.0 if prefer_bottom_right else 0.0
            candidates.append((score, x, row))
    if not candidates:
        return None
    _score, _x, row = max(candidates, key=lambda item: (item[0], item[1]))
    return row_center(row)


def find_phrase_center(rows: Sequence[Dict[str, Any]], words: Sequence[str]) -> Optional[Tuple[float, float]]:
    needles = [word.lower() for word in words]
    phrase = " ".join(needles)
    for row in text_rows(rows):
        text = str(row.get("text") or "").lower().strip(".,:;()[]{}'\"|")
        if phrase in text:
            return row_center(row)
        pos = 0
        matched = True
        for needle in needles:
            found = text.find(needle, pos)
            if found < 0:
                matched = False
                break
            pos = found + len(needle)
        if matched:
            return row_center(row)

    lines: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
    for row in text_rows(rows):
        key = tuple(str(row.get(part) or "") for part in ("page_num", "block_num", "par_num", "line_num"))
        lines.setdefault(key, []).append(row)

    for line in lines.values():
        line.sort(key=lambda row: float(row.get("left") or 0))
        tokens = [str(r.get("text") or "").lower().strip(".,:;()[]{}'\"|") for r in line]
        for start in range(len(tokens)):
            matched_rows: List[Dict[str, Any]] = []
            pos = start
            for needle in needles:
                while pos < len(tokens) and needle not in tokens[pos]:
                    pos += 1
                if pos >= len(tokens):
                    matched_rows = []
                    break
                matched_rows.append(line[pos])
                pos += 1
            if matched_rows:
                xs: List[float] = []
                ys: List[float] = []
                scale = int(matched_rows[0].get("_scale") or 1)
                offset_x = int(matched_rows[0].get("_offset_x") or 0)
                offset_y = int(matched_rows[0].get("_offset_y") or 0)
                for row in matched_rows:
                    left = float(row.get("left") or 0)
                    top = float(row.get("top") or 0)
                    width = float(row.get("width") or 0)
                    height = float(row.get("height") or 0)
                    xs.extend([left, left + width])
                    ys.extend([top, top + height])
                return (
                    offset_x + ((min(xs) + max(xs)) / 2.0) / scale,
                    offset_y + ((min(ys) + max(ys)) / 2.0) / scale,
                )
    return None


def save_locate_failure(
    label: str,
    work_dir: Path,
    rows: Sequence[Dict[str, Any]],
    window: Optional[Dict[str, Any]],
) -> Path:
    """Copy diagnostic artifacts (screenshots, OCR rows, metadata) so the
    user can see exactly what the OCR pass saw at the moment of failure.
    Returns the path to the directory where they were written.
    """
    base = cache_base() / "failures" / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
    base.mkdir(parents=True, exist_ok=True)
    for src in work_dir.iterdir():
        if src.suffix.lower() in {".png", ".jpg"}:
            try:
                shutil.copy2(src, base / src.name)
            except Exception:
                pass
    try:
        (base / "rows.json").write_text(
            json.dumps(list(rows), indent=2, default=str), encoding="utf-8"
        )
    except Exception:
        pass
    try:
        meta = {
            "label": label,
            "window": window,
            "row_count_raw": len(list(rows)),
            "row_count_filtered": len(text_rows(rows)),
            "screen_text_full": screen_text(rows),
        }
        (base / "meta.json").write_text(
            json.dumps(meta, indent=2, default=str), encoding="utf-8"
        )
    except Exception:
        pass
    return base
