"""Private Neat Video UI helpers for ``autoneat profile``.

This module intentionally has no public probe/diagnostic command. Its only
subprocess entry point is ``--_open-neat-helper``, which adds/selects the Neat
OFX node and exits. It must not open Neat's blocking profile UI through the
Fusion API; the batch command drives visible UI controls itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from autoneat import _neat_vision as neat_vision
from autoneat.core import click

NEAT_REG_ID = "ofx.com.absoft.NeatVideo6.rs"
RESOLVE_PROCESS = "Resolve"
RESOLVE_APP = "DaVinci Resolve"

# Fusion ColorSpaceTransform tool — wraps Neat so it sees display-referred
# pixels for analysis instead of scene-linear AP0 (which looks near-black on
# any monitor and breaks Neat's Auto Profile). Round-trip preserves working
# space at MediaOut.
CST_TOOL_ID = "ColorSpaceTransform"
CST_IN_NAME = "NeatCstIn"
CST_OUT_NAME = "NeatCstOut"
SCALE_TOOL_ID = "BrightnessContrast"
SCALE_DOWN_NAME = "NeatWrapScaleDown"
SCALE_UP_NAME = "NeatWrapScaleUp"

# Map a DaVinci YRGB Color Managed (DRCM) timeline gamut → the matching Fusion
# CST color-space FuID. In DRCM, Resolve linearises the image into Fusion, so
# Neat sees the timeline gamut at LINEAR gamma (near-black). We re-encode that
# same gamut to PQ for analysis and back to linear at MediaOut.
_DRCM_GAMUT_TO_CST = {
    "Rec.2020": "REC2020_COLORSPACE",
    "Rec.709": "REC709_COLORSPACE",
    "P3-D65": "P3D65_COLORSPACE",
    "P3-DCI": "DCIP3_COLORSPACE",
}

# Map HDR mastering luminance → matching PQ-peak gamma FuID. These are the
# IDs Resolve's CST exposes; verified live via comp.AddTool("ColorSpaceTransform").
_PQ_GAMMA_BY_NITS = {
    300: "PQ300_GAMMA",
    500: "PQ500_GAMMA",
    800: "PQ800_GAMMA",
    1000: "PQ1000_GAMMA",
    2000: "PQ2000_GAMMA",
    3000: "PQ3000_GAMMA",
    4000: "PQ4000_GAMMA",
}

_SCREEN_SIZE_CACHE: Optional[Tuple[int, int]] = None


def _run_proc(
    args: Sequence[str], *, timeout: Optional[float] = None
) -> subprocess.CompletedProcess[str]:
    # Force utf-8 so we don't blow up when the parent shell's locale is C/ASCII
    # and a child (tesseract, JXA, etc.) emits unicode in stdout.
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def _run_jxa(script: str, *, timeout: float = 8.0) -> Dict[str, Any]:
    proc = _run_proc(["osascript", "-l", "JavaScript", "-e", script], timeout=timeout)
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": proc.stderr.strip() or proc.stdout.strip() or "osascript failed",
        }
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"osascript returned non-JSON: {proc.stdout.strip()}"}
    return (
        data
        if isinstance(data, dict)
        else {"ok": False, "error": "osascript returned non-object JSON"}
    )


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    raise RuntimeError(
        f"Required tool not found: {name}. Install it first, e.g. `brew install {name}`."
    )


def _safe_int(value: Any) -> int:
    return int(round(float(value)))


def _cache_base() -> Path:
    base = Path.home() / ".cache" / "autoneat" / "neat_ui"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _activate_resolve(*, settle: float = 0.35) -> None:
    # `open -a` already raises Resolve to the front. Adding `osascript ... to
    # activate` was a belt-and-suspenders that hangs whenever Resolve's
    # AppleScript runloop is busy (e.g. while Neat is committing a profile),
    # which then fails every step that depends on activation. Skip it.
    try:
        _run_proc(["open", "-a", RESOLVE_APP], timeout=10)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(settle)


def _resolve_windows(*, activate: bool = False) -> List[Dict[str, Any]]:
    script = f"""
const se = Application("System Events");
const proc = se.processes.byName({json.dumps(RESOLVE_PROCESS)});
if (!proc.exists()) {{
  JSON.stringify({{ok: false, error: "Resolve process not found"}});
}} else {{
  if ({str(bool(activate)).lower()}) {{
    try {{ proc.frontmost = true; }} catch (err) {{}}
  }}
  function pair(fn) {{
    try {{ return Array.from(fn()).map(x => Number(x)); }} catch (err) {{ return null; }}
  }}
  function text(fn) {{
    try {{
      const value = fn();
      return value === null || value === undefined ? "" : String(value);
    }} catch (err) {{ return ""; }}
  }}
  const wins = proc.windows();
  const rows = [];
  for (let i = 0; i < wins.length; i++) {{
    rows.push({{
      index: i + 1,
      name: text(() => wins[i].name()),
      role: text(() => wins[i].role()),
      position: pair(() => wins[i].position()),
      size: pair(() => wins[i].size())
    }});
  }}
  JSON.stringify({{ok: true, windows: rows}});
}}
"""
    data = _run_jxa(script)
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "could not inspect Resolve windows"))
    return list(data.get("windows") or [])


def _window_area(window: Dict[str, Any]) -> float:
    size = window.get("size") or [0, 0]
    if len(size) < 2:
        return 0.0
    return float(size[0]) * float(size[1])


def _main_resolve_window(windows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return max(windows, key=_window_area, default=None)


def _find_neat_window(
    windows: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    rows = list(windows) if windows is not None else _resolve_windows(activate=True)
    for window in rows:
        if "neat video" in str(window.get("name") or "").lower():
            return window
    return None


def _find_modal_window(
    windows: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    rows = list(windows) if windows is not None else _resolve_windows(activate=True)
    main = _main_resolve_window(rows)
    candidates = []
    for window in rows:
        if window is main:
            continue
        size = window.get("size") or [0, 0]
        if len(size) >= 2 and size[0] >= 180 and size[1] >= 60:
            candidates.append(window)
    return max(candidates, key=_window_area, default=None)


def _raise_window(window: Dict[str, Any]) -> None:
    index = int(window.get("index") or 1)
    script = f"""
const se = Application("System Events");
const proc = se.processes.byName({json.dumps(RESOLVE_PROCESS)});
if (proc.exists()) {{
  try {{ proc.frontmost = true; }} catch (err) {{}}
  try {{ proc.windows()[{index - 1}].actions.byName("AXRaise").perform(); }} catch (err) {{}}
}}
JSON.stringify({{ok: true}});
"""
    _run_jxa(script)
    time.sleep(0.2)


def _window_point(window: Dict[str, Any], x: float, y: float) -> Tuple[float, float]:
    position = window.get("position") or [0, 0]
    size = window.get("size") or [0, 0]
    if x < 0:
        x = float(size[0]) + x
    if y < 0:
        y = float(size[1]) + y
    return (float(position[0]) + x, float(position[1]) + y)


def _window_fraction_point(
    window: Dict[str, Any], x_frac: float, y_frac: float
) -> Tuple[float, float]:
    size = window.get("size") or [0, 0]
    return _window_point(window, float(size[0]) * x_frac, float(size[1]) * y_frac)


def _click_window_point(window: Dict[str, Any], x: float, y: float) -> Tuple[float, float]:
    _raise_window(window)
    point = _window_point(window, x, y)
    _click_at_quartz(point[0], point[1])
    return point


def _click_window_fraction(
    window: Dict[str, Any], x_frac: float, y_frac: float
) -> Tuple[float, float]:
    _raise_window(window)
    point = _window_fraction_point(window, x_frac, y_frac)
    _click_at_quartz(point[0], point[1])
    return point


def capture_screen_via_bridge(output: Path, *, timeout: float = 30.0) -> bool:
    """Capture the screen from a non-GUI (SSH) context via the console GUI session.

    Why this is needed: plain ``screencapture`` over SSH fails with "could not
    create image from display" because the SSH session has no window-server
    connection, and ``launchctl asuser`` (which would attach to the Aqua
    session) requires root to switch audit sessions. The one mechanism that
    works without root is to run ``screencapture`` *inside* the logged-in user's
    GUI session by ``open``-ing a ``.command`` script: macOS hands it to
    Terminal, which holds the Screen-Recording TCC grant, so the capture
    succeeds against the real display.

    ``open -g`` keeps the helper Terminal in the background so it does not steal
    focus from Resolve (important when a batch is mid-click). Returns True on a
    real, non-empty capture; False otherwise. Best-effort cleanup of the
    scratch script/marker files.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    base = _cache_base() / "captures" / f"cap-{stamp}"
    base.parent.mkdir(parents=True, exist_ok=True)
    done = base.with_suffix(".done")
    script = base.with_suffix(".command")
    # The script captures, records the exit code, then closes its own Terminal
    # window via AppleScript targeting ITS OWN window id (no broad Automation
    # prompt, no osascript that drives Terminal globally). Best-effort close.
    script.write_text(
        "#!/bin/bash\n"
        f"/usr/sbin/screencapture -x {shlex.quote(str(output))} >/dev/null 2>&1\n"
        f'echo "$?" > {shlex.quote(str(done))}\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    try:
        proc = _run_proc(["open", "-g", "-a", "Terminal", str(script)], timeout=20)
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if done.exists():
            break
        time.sleep(0.3)
    ok = output.exists() and output.stat().st_size > 0
    for path in (script, done):
        try:
            path.unlink()
        except OSError:
            pass
    return ok


def _capture_screen(output: Path) -> None:
    _activate_resolve(settle=0.2)
    proc = _run_proc(["screencapture", "-x", str(output)], timeout=10)
    if proc.returncode == 0 and Path(output).exists() and Path(output).stat().st_size > 0:
        return
    # Direct capture failed — almost always a non-GUI (SSH) context with no
    # window server. Capture inside the console user's Aqua session instead.
    if capture_screen_via_bridge(output):
        return
    raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "screencapture failed")


_TSV_COLUMNS = (
    "level",
    "page_num",
    "block_num",
    "par_num",
    "line_num",
    "word_num",
    "left",
    "top",
    "width",
    "height",
    "conf",
    "text",
)


def _tesseract_ocr_image(image: Path, *, psm: int) -> List[Dict[str, Any]]:
    """OCR via the cross-platform ``tesseract`` CLI, returning one dict per line.

    Tesseract's TSV output yields per-word bounding boxes; this function
    groups level-5 (word) rows by ``(block_num, par_num, line_num)``
    and emits one dict per line with the joined text and a combined
    bbox covering every word in that line. Output schema matches what
    the Neat UI helpers downstream expect: ``text``, ``conf`` (0.0-1.0),
    ``left`` / ``top`` / ``width`` / ``height`` (pixels), plus the
    ``page_num`` / ``block_num`` / ``par_num`` / ``line_num`` /
    ``word_num`` placeholders.

    ``psm`` is forwarded to tesseract's ``--psm`` flag (page segmentation
    mode). The default ``6`` ("assume a single uniform block of text")
    works well for the contrast-boosted, tightly-cropped Neat UI regions
    we OCR. Install tesseract via ``brew install tesseract`` (macOS) or
    ``apt-get install tesseract-ocr`` (Debian/Ubuntu); the helper raises
    a clear error pointing at those commands if the binary is missing.
    """
    import shutil

    if shutil.which("tesseract") is None:
        raise RuntimeError(
            "tesseract binary not on PATH — install with "
            "`brew install tesseract` (macOS) or "
            "`apt-get install tesseract-ocr` (Debian/Ubuntu)."
        )

    proc = _run_proc(
        [
            "tesseract",
            str(image),
            "stdout",
            "-l",
            "eng",
            "--psm",
            str(psm),
            "tsv",
        ],
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "tesseract OCR failed")

    raw_lines = proc.stdout.splitlines()
    if not raw_lines:
        return []
    header = raw_lines[0].split("\t")
    try:
        idx = {col: header.index(col) for col in _TSV_COLUMNS}
    except ValueError:
        return []

    by_line: Dict[Tuple[int, int, int], List[Dict[str, Any]]] = {}
    for raw in raw_lines[1:]:
        fields = raw.split("\t")
        if len(fields) <= max(idx.values()):
            continue
        try:
            level = int(fields[idx["level"]])
        except ValueError:
            continue
        if level != 5:
            continue
        text = fields[idx["text"]].strip()
        if not text:
            continue
        try:
            conf_int = int(fields[idx["conf"]])
        except ValueError:
            conf_int = -1
        key = (
            int(fields[idx["block_num"]]),
            int(fields[idx["par_num"]]),
            int(fields[idx["line_num"]]),
        )
        by_line.setdefault(key, []).append(
            {
                "text": text,
                "conf": max(0.0, conf_int) / 100.0,
                "left": float(fields[idx["left"]]),
                "top": float(fields[idx["top"]]),
                "width": float(fields[idx["width"]]),
                "height": float(fields[idx["height"]]),
            }
        )

    rows: List[Dict[str, Any]] = []
    for (block, par, line), words in by_line.items():
        words.sort(key=lambda word: word["left"])
        left = min(word["left"] for word in words)
        top = min(word["top"] for word in words)
        right = max(word["left"] + word["width"] for word in words)
        bottom = max(word["top"] + word["height"] for word in words)
        rows.append(
            {
                "text": " ".join(word["text"] for word in words),
                "conf": sum(word["conf"] for word in words) / len(words),
                "left": left,
                "top": top,
                "width": right - left,
                "height": bottom - top,
                # Per-word boxes are retained so callers can click an exact word
                # (e.g. "Apply") instead of the merged line center — clicking the
                # center of a "Cancel Apply" line lands between the two buttons.
                "words": [
                    {
                        "text": word["text"],
                        "left": word["left"],
                        "top": word["top"],
                        "width": word["width"],
                        "height": word["height"],
                    }
                    for word in words
                ],
                "page_num": "1",
                "block_num": str(block),
                "par_num": str(par),
                "line_num": str(line),
                "word_num": "1",
            }
        )
    return rows


def _ocr_image(image: Path, output_base: Path, *, psm: int = 6) -> List[Dict[str, Any]]:
    """OCR a screen-grab via tesseract. Raises on failure.

    ``output_base`` is retained for call-site compatibility but unused
    — tesseract writes its TSV directly to stdout (no intermediate
    file needed).
    """
    del output_base  # unused
    return _tesseract_ocr_image(image, psm=psm)


def _ocr_screen_region(
    work_dir: Path,
    *,
    name: str,
    rel_box: Optional[Tuple[int, int, int, int]] = None,
    scale: int = 2,
    binarize: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """OCR a (cropped) screen grab.

    ``binarize`` selects the preprocessing. ``None`` (default) uses grayscale +
    contrast enhancement, which reads large/high-contrast UI text well. A small
    integer threshold instead hard-binarizes (pixels brighter than the threshold
    → white, else black), which is what makes Resolve/Neat's small *grey-on-grey*
    toolbar buttons ("Auto Profile", "Apply") OCR-readable — contrast
    enhancement leaves their anti-aliased strokes too faint, but a threshold near
    the midpoint between the ~110 button fill and the ~200 text yields crisp
    glyphs.
    """
    raw = work_dir / f"{name}-screen.png"
    region = work_dir / (f"{name}.png" if binarize is not None else f"{name}.jpg")
    _capture_screen(raw)

    try:
        from PIL import Image, ImageEnhance, ImageOps
    except Exception as exc:  # pragma: no cover - local dependency
        raise RuntimeError("Pillow is required for OCR preprocessing") from exc

    image = Image.open(raw).convert("RGB")
    screen_w, screen_h = image.size
    offset_x = 0
    offset_y = 0
    if rel_box is not None:
        offset_x, offset_y, box_w, box_h = rel_box
        image = image.crop((offset_x, offset_y, offset_x + box_w, offset_y + box_h))

    image = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
    gray = ImageOps.grayscale(image)
    if binarize is not None:
        threshold = binarize
        gray = gray.point(lambda value: 255 if value > threshold else 0)
        gray.save(region)
    else:
        gray = ImageEnhance.Contrast(gray).enhance(3.0)
        gray.save(region, quality=95)

    rows = _ocr_image(region, work_dir / f"{name}-ocr", psm=6)
    for row in rows:
        row["_scale"] = scale
        row["_screen_width"] = screen_w
        row["_screen_height"] = screen_h
        row["_offset_x"] = offset_x
        row["_offset_y"] = offset_y
    return rows


def _ocr_screen(work_dir: Path, *, scale: int = 2) -> List[Dict[str, Any]]:
    return _ocr_screen_region(work_dir, name="screen", scale=scale)


def _text_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    notification_tokens = {
        "managed",
        "login",
        "items",
        "added",
        "background",
        "extensions",
        "notifications",
    }
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        if "\n" in text or "\t" in text or len(text) > 80:
            continue
        try:
            scale = int(row.get("_scale") or 1)
            offset_x = int(row.get("_offset_x") or 0)
            offset_y = int(row.get("_offset_y") or 0)
            screen_w = int(row.get("_screen_width") or 0)
            screen_h = int(row.get("_screen_height") or 0)
            left = offset_x + float(row.get("left") or 0) / scale
            top = offset_y + float(row.get("top") or 0) / scale
            normalized = text.lower().strip(".,:;()[]{}'\"|>").replace("-", "")
            if (
                screen_w
                and screen_h
                and left > screen_w * 0.45
                and top < screen_h * 0.20
                and normalized in notification_tokens
            ):
                continue
        except Exception:
            pass
        filtered.append(row)
    return filtered


def _word_text(row: Dict[str, Any]) -> str:
    return str(row.get("text") or "").strip()


def _screen_text(rows: Sequence[Dict[str, Any]]) -> str:
    return " ".join(_word_text(row) for row in _text_rows(rows)).lower()


def _is_neat_information_text(text: str) -> bool:
    normalized = text.lower()
    overlay_tokens = (
        "doppler",
        "managed login items",
        "system private window picker",
        "window picker",
        "directly access",
        "access your screen",
        "login items & extensions",
    )
    if any(token in normalized for token in overlay_tokens):
        return False
    if "information" in normalized and any(
        token in normalized
        for token in (
            "dynamic range",
            "input data gain",
            "not selected",
            "before opening",
            "do not show again",
        )
    ):
        return True
    return any(token in normalized for token in _MODAL_INFO_TOKENS)


def _screen_size_from_rows(rows: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
    for row in rows:
        width = int(row.get("_screen_width") or 0)
        height = int(row.get("_screen_height") or 0)
        if width and height:
            return width, height
    return 1920, 1080


def _row_center(row: Dict[str, Any]) -> Tuple[float, float]:
    scale = int(row.get("_scale") or 1)
    offset_x = int(row.get("_offset_x") or 0)
    offset_y = int(row.get("_offset_y") or 0)
    left = float(row.get("left") or 0)
    top = float(row.get("top") or 0)
    width = float(row.get("width") or 0)
    height = float(row.get("height") or 0)
    return (offset_x + (left + width / 2.0) / scale, offset_y + (top + height / 2.0) / scale)


def _word_box_center(row: Dict[str, Any], box: Dict[str, Any]) -> Tuple[float, float]:
    scale = int(row.get("_scale") or 1)
    offset_x = int(row.get("_offset_x") or 0)
    offset_y = int(row.get("_offset_y") or 0)
    left = float(box.get("left") or 0)
    top = float(box.get("top") or 0)
    width = float(box.get("width") or 0)
    height = float(box.get("height") or 0)
    return (offset_x + (left + width / 2.0) / scale, offset_y + (top + height / 2.0) / scale)


def _find_word_center(
    rows: Sequence[Dict[str, Any]],
    word: str,
    *,
    prefer_bottom_right: bool = False,
) -> Optional[Tuple[float, float]]:
    needle = word.lower()
    # (score, x, y). Prefer the exact word's own box over the merged line center
    # so clicks land on the actual button (e.g. "Apply", not midway to "Cancel").
    candidates: List[Tuple[float, float, float]] = []
    for row in _text_rows(rows):
        boxes = row.get("words") or []
        matched = False
        for box in boxes:
            token = str(box.get("text") or "").lower().strip(".,:;()[]{}'\"|")
            if not token:
                continue
            if token == needle or needle in token:
                cx, cy = _word_box_center(row, box)
                exact = token == needle
                score = (100000.0 if exact else 0.0) + (
                    cy + cx / 10000.0 if prefer_bottom_right else 0.0
                )
                candidates.append((score, cx, cy))
                matched = True
        if matched:
            continue
        # Fallback for rows without per-word boxes.
        text = _word_text(row).lower().strip(".,:;()[]{}'\"|")
        if needle in text:
            cx, cy = _row_center(row)
            exact = text == needle
            score = (100000.0 if exact else 0.0) + (
                cy + cx / 10000.0 if prefer_bottom_right else 0.0
            )
            candidates.append((score, cx, cy))
    if not candidates:
        return None
    best = max(candidates, key=lambda item: (item[0], item[1]))
    return (best[1], best[2])


def _find_adjacent_words_center(
    rows: Sequence[Dict[str, Any]], first: str, second: str
) -> Optional[Tuple[float, float]]:
    """Center of an exact adjacent word pair (e.g. "Auto" then "Profile").

    Unlike :func:`_find_phrase_center`, which spans from the first needle to the
    last, this anchors on two *consecutive* per-word boxes — so "Auto Profile"
    is located precisely instead of the matcher running from "Auto" all the way
    to the "Profile" in a neighbouring "Generic Profile" / "Load Profile" button
    and landing on the gap between them.
    """
    f = first.lower()
    s = second.lower()

    def _clean(value: Any) -> str:
        return str(value or "").lower().strip(".,:;()[]{}'\"|")

    for row in _text_rows(rows):
        boxes = row.get("words") or []
        tokens = [_clean(box.get("text")) for box in boxes]
        for i in range(len(tokens) - 1):
            if tokens[i] == f and tokens[i + 1].startswith(s):
                c1 = _word_box_center(row, boxes[i])
                c2 = _word_box_center(row, boxes[i + 1])
                return ((c1[0] + c2[0]) / 2.0, (c1[1] + c2[1]) / 2.0)
    return None


def _find_phrase_center(
    rows: Sequence[Dict[str, Any]], words: Sequence[str]
) -> Optional[Tuple[float, float]]:
    needles = [word.lower() for word in words]
    phrase = " ".join(needles)
    for row in _text_rows(rows):
        text = _word_text(row).lower().strip(".,:;()[]{}'\"|")
        if phrase in text:
            return _row_center(row)
        pos = 0
        all_matched = True
        for needle in needles:
            found = text.find(needle, pos)
            if found < 0:
                all_matched = False
                break
            pos = found + len(needle)
        if all_matched:
            return _row_center(row)

    lines: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    for row in _text_rows(rows):
        key = tuple(
            str(row.get(part) or "") for part in ("page_num", "block_num", "par_num", "line_num")
        )
        lines.setdefault(key, []).append(row)

    for line in lines.values():
        line.sort(key=lambda row: float(row.get("left") or 0))
        tokens = [_word_text(row).lower().strip(".,:;()[]{}'\"|") for row in line]
        for start in range(len(tokens)):
            matched: List[Dict[str, Any]] = []
            pos = start
            for needle in needles:
                while pos < len(tokens) and needle not in tokens[pos]:
                    pos += 1
                if pos >= len(tokens):
                    matched = []
                    break
                matched.append(line[pos])
                pos += 1
            if matched:
                xs: List[float] = []
                ys: List[float] = []
                scale = int(matched[0].get("_scale") or 1)
                offset_x = int(matched[0].get("_offset_x") or 0)
                offset_y = int(matched[0].get("_offset_y") or 0)
                for row in matched:
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


def _screen_state_from_text(text: str) -> str:
    text = text.lower()
    # The Neat editor window is open when any of its chrome is visible. The
    # phrase "prepare noise profile" alone does NOT mean the window is closed —
    # it's also the editor's active tab AND the Fusion Inspector button behind
    # the window — so the open editor must be distinguished by its own chrome,
    # otherwise an open window gets misread as "inspector-prepare" (window
    # closed) and the driver wrongly re-clicks the open button.
    strong_editor_chrome = any(
        token in text
        for token in (
            "plug-in for resolve",
            "beginner mode",
            "generic profile",
            "load profile",
        )
    )
    # "Adjust and preview" and "Device Noise Profile" also appear in Resolve's
    # Fusion Inspector for the Neat OFX node, so they are only editor signals
    # when paired with profile UI text that cannot come from the Inspector
    # button panel.
    weak_editor_chrome = "adjust and preview" in text and any(
        token in text
        for token in (
            "profile not ready",
            "noise level",
            "profile check",
            "build profile",
            "profile ready",
        )
    )
    neat_window_open = strong_editor_chrome or weak_editor_chrome
    # Neat's modal "Information" dialog (e.g. the Input-Data-Gain / dynamic-range
    # notice, or the "select a frame first" notice) sits on top of everything
    # and must be dismissed before the editor underneath is usable.
    if _is_neat_information_text(text):
        return "information-dialog"
    # Neat's "Confirm" modal after Auto Profile when the auto-selected sample
    # area is below the recommended 128x128 — sits on top of the editor, so it
    # must be matched before the editor-chrome branch. Proceed by accepting the
    # small area (the equivalent of "Continue building profile").
    if "use the small area" in text or (
        "selected area is small" in text or "recommended size is 128" in text
    ):
        return "confirm-small-area"
    if "preparing input" in text or "preparing frames" in text:
        return "preparing-input"
    if "continue" in text and "neat video" in text and "trial" in text:
        return "demo-splash"
    # Resolve's Fusion Inspector can contain Neat-looking labels ("Device Noise
    # Profile", "Adjust and preview") while the Neat editor is not open. This
    # explicit Inspector signature must win before the editor-chrome branch.
    if "prepare noise profile" in text and "controls" in text and "settings" in text:
        return "inspector-prepare"
    if neat_window_open:
        if "profile not ready" in text:
            return "editor-unprofiled"
        if any(
            token in text
            for token in ("noise level", "profile check", "build profile", "profile ready")
        ):
            return "editor-profiled"
        # Open editor with no explicit "profile not ready" — default to
        # unprofiled; the driver always Auto-Profiles before Apply regardless.
        return "editor-unprofiled"
    if "prepare noise profile" in text:
        return "inspector-prepare"
    # Fallback: the Neat node is selected in the Fusion Inspector (its node name
    # plus the inspector sections are visible) but "prepare noise profile" OCR'd
    # poorly this poll. The window is not open (checked above), so the only
    # action is to open it — treat as inspector-prepare. The prepare-profile
    # *click* locator re-OCRs its own high-scale region, so a garbled
    # state-detection pass doesn't prevent the click from landing.
    if "reducenoise" in text and (
        "adjust filter" in text or ("controls" in text and "settings" in text)
    ):
        return "inspector-prepare"
    # Node 2 sometimes OCRs the selected Neat inspector as only the surrounding
    # Fusion panes ("Tools", "Modifiers", "Templates", "Ambient Light") and
    # misses the actual button text. Treat that as the same reopenable inspector
    # state instead of waiting for the generic unknown-state timeout.
    if "tools" in text and "modifiers" in text and ("templates" in text or "ambient light" in text):
        return "inspector-prepare"
    return "unknown"


def _read_screen_state(work_dir: Path) -> Tuple[str, str, List[Dict[str, Any]]]:
    rows = _ocr_screen(work_dir)
    text = _screen_text(rows)
    state = _screen_state_from_text(text)
    # A Neat "Confirm" modal (e.g. "cannot find a large uniform area" →
    # Cancel | Continue building profile) sits on top of the editor as small
    # grey text the full-screen pass garbles, so it mis-classifies as an editor
    # state. Confirm it with a tight, high-scale, binarized crop of the modal
    # band — the only read reliable enough on this dialog. Checked from EVERY
    # editor state, including editor-profiled: the modal can be up while the
    # editor already shows a partial profile ("Noise Level: N.N" + analysis box),
    # so the full-screen pass reads editor-profiled even though Apply is blocked
    # by the modal. Skipping profiled here once let that modal slip through —
    # the code clicked the modal-blocked Apply, the window stayed open, and the
    # wedged window crashed Resolve. The extra per-poll capture is the price of
    # never missing it.
    if state in ("editor-unprofiled", "editor-profiled", "editor", "unknown", "inspector-prepare"):
        band_rows = _ocr_modal_band(work_dir)
        band_text = _screen_text(band_rows)
        if _is_neat_information_text(band_text):
            return "information-dialog", band_text[:500], band_rows
        if any(tok in band_text for tok in _MODAL_CONFIRM_TOKENS):
            return "confirm-build-profile", band_text[:500], band_rows
    if state == "unknown":
        width, height = _screen_size_from_rows(rows)
        crop = (int(width * 0.12), int(height * 0.02), int(width * 0.78), int(height * 0.92))
        region_rows = _ocr_screen_region(work_dir, name="neat-area", rel_box=crop, scale=4)
        region_text = _screen_text(region_rows)
        region_state = _screen_state_from_text(region_text)
        if region_state != "unknown":
            return region_state, region_text[:500], region_rows
    return state, text[:500], rows


# Noise-profile panels used to decide profiled-vs-building. The right "Device
# Noise Profile" panel shows "profile not ready" while building and device/frame
# details once a profile exists; the top-centre overlay shows "Noise Level: NN.N"
# only once profiled. Both are small grey text the full-screen OCR pass misses,
# so we crop+binarize them directly.
_PROFILE_PANEL_CROP = (0.64, 0.11, 0.25, 0.26)
_NOISE_LEVEL_CROP = (0.36, 0.12, 0.30, 0.09)


def editor_profile_ready(work_dir: Path) -> Optional[bool]:
    """Region-OCR Neat's noise-profile panels to classify the editor precisely.

    Returns ``True`` when a freshly-built profile is present (ready to Apply),
    ``False`` while Neat is still building (so the driver waits instead of
    re-clicking Build Profile and restarting the analysis), or ``None`` when the
    crops are inconclusive (caller keeps the full-screen state). This exists
    because the full-screen OCR pass reads only the large menu-bar text and
    misses the small grey "Noise Level"/"profile not ready" strings — the root
    cause of the editor-profiled/​unprofiled mis-classification."""
    if _neat_editor_window() is None:
        return None
    try:
        raw = work_dir / "profile-probe.png"
        _capture_screen(raw)
        from PIL import Image

        screen_w, screen_h = Image.open(raw).size
    except Exception:
        return None

    def _crop_text(frac: Tuple[float, float, float, float], name: str) -> str:
        fx, fy, fw, fh = frac
        box = (
            int(screen_w * fx),
            int(screen_h * fy),
            int(screen_w * fw),
            int(screen_h * fh),
        )
        collected = ""
        for scale, threshold in ((4, 160), (4, None)):
            try:
                rows = _ocr_screen_region(
                    work_dir, name=name, rel_box=box, scale=scale, binarize=threshold
                )
                collected += " " + _screen_text(rows)
            except Exception:
                continue
        return collected.lower()

    panel = _crop_text(_PROFILE_PANEL_CROP, "profile-panel")
    if "not ready" in panel:
        return False
    level = _crop_text(_NOISE_LEVEL_CROP, "noise-level")
    if "noise level" in level and any(ch.isdigit() for ch in level):
        return True
    if "noise level" in panel and any(ch.isdigit() for ch in panel):
        return True
    # A device/frame-size readout (e.g. "3840x2160") only renders once a profile
    # has been built — a reliable "profiled" signal even if the level OCR is weak.
    if re.search(r"\d{3,4}\s*[xX]\s*\d{3,4}", panel):
        return True
    return None


def _ocr_window(
    window: Dict[str, Any],
    work_dir: Path,
    *,
    name: str,
    scale: int = 4,
    rel_box: Optional[Tuple[int, int, int, int]] = None,
    binarize: Optional[int] = None,
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
    return _ocr_screen_region(
        work_dir, name=name, rel_box=screen_box, scale=scale, binarize=binarize
    )


def _button_point_from_rows(
    label: str, rows: Sequence[Dict[str, Any]]
) -> Optional[Tuple[float, float]]:
    if label == "auto-profile":
        # "Auto Profile" sits immediately left of "Generic Profile" / "Load
        # Profile". Anchor on the unique "auto" token. NEVER fall back to a
        # phrase span over "...Profile ... Profile ... Profile" — its center
        # lands squarely on the *Generic Profile* button and silently applies a
        # generic (non-frame) profile. Returning None instead lets the locator
        # retry at other scales / fail loudly rather than mis-click.
        return _find_adjacent_words_center(rows, "auto", "profile") or _find_word_center(
            rows, "auto"
        )
    if label == "prepare-profile":
        return _find_phrase_center(rows, ["prepare", "noise", "profile"]) or _find_phrase_center(
            rows, ["prepare", "profile"]
        )
    if label == "continue":
        return _find_word_center(rows, "continue")
    if label == "ok":
        return _find_word_center(rows, "ok", prefer_bottom_right=True)
    if label == "apply":
        return _find_word_center(rows, "apply", prefer_bottom_right=True)
    if label == "cancel":
        # Cancel closes the Neat editor (discarding the profile) and is also the
        # "reject" button on Neat's modals. It is grey-on-grey like Apply, sits
        # to its left in the editor's bottom action row, and may also appear
        # centered in a modal — so prefer the bottom-most "cancel" word.
        return _find_word_center(rows, "cancel", prefer_bottom_right=True)
    if label == "use-small-area":
        # Button "Use the small area" (bottom-right of the Confirm modal). Anchor
        # on the adjacent "small area" pair; the bottom-most "small"/"area" word
        # is the button, not the "...area is small." message line above it.
        return _find_adjacent_words_center(rows, "small", "area") or _find_word_center(
            rows, "small", prefer_bottom_right=True
        )
    return None


def _save_locate_failure(
    label: str,
    work_dir: Path,
    rows: Sequence[Dict[str, Any]],
    window: Optional[Dict[str, Any]],
) -> Path:
    """Copy the OCR'd screenshot + raw rows + meta into ~/.cache/autoneat/neat_ui/failures.

    Returns the directory where artifacts were written.
    """
    base = _cache_base() / "failures" / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
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
            "row_count_filtered": len(_text_rows(rows)),
            "screen_text_full": _screen_text(rows),
        }
        (base / "meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass
    return base


def _locate_button(label: str, work_dir: Path, *, window: Dict[str, Any]) -> Tuple[float, float]:
    """Window-relative OCR locate for a Neat control.

    The crop is anchored to the editor window rect, and the grey-on-grey editor
    buttons (Auto Profile / Apply / Cancel) use the SAME threshold-binarize
    passes as the full-screen path — contrast enhancement alone leaves their
    anti-aliased strokes too faint to OCR. A high-scale, tightly-cropped,
    binarized window region is the most reliable read we have for these buttons.
    """
    size = window.get("size") or [0, 0]
    win_w = max(int(size[0]), 1)
    win_h = max(int(size[1]), 1)
    if label == "auto-profile":
        rel_box: Optional[Tuple[int, int, int, int]] = (0, 35, min(win_w, 520), 90)
        scale = 8
        passes: Sequence[Optional[int]] = (150, 170, None)
    elif label in ("apply", "cancel"):
        strip_left = int(win_w * 0.4)
        strip_top = max(0, win_h - 200)
        rel_box = (strip_left, strip_top, win_w - strip_left, min(200, win_h))
        scale = 8
        passes = (150, 170, None)
    else:
        rel_box = None
        scale = 4
        passes = (None,)

    rows: List[Dict[str, Any]] = []
    for threshold in passes:
        rows = _ocr_window(
            window,
            work_dir,
            name=f"{label}-window",
            scale=scale,
            rel_box=rel_box,
            binarize=threshold,
        )
        point = _button_point_from_rows(label, rows)
        if point is not None:
            return point

    artifacts = _save_locate_failure(label, work_dir, rows, window)
    text = _screen_text(rows)[:500]
    raise RuntimeError(
        f"Could not OCR-locate `{label}` button. "
        f"window pos={window.get('position')} size={window.get('size')}. "
        f"Diagnostic artifacts: {artifacts}. "
        f"OCR text: {text!r}"
    )


def _click_ocr_button(label: str, work_dir: Path, *, window: Dict[str, Any]) -> Tuple[float, float]:
    point = _locate_button(label, work_dir, window=window)
    _click_at_quartz(point[0], point[1])
    return point


# Fractional screen-region crops (x, y, w, h as fractions of screen size) for
# each control. Neat's editor and Resolve's Inspector lay out consistently, and
# a full-screen OCR pass (a) garbles the editor's small toolbar text at low
# scale and (b) merges the Inspector button onto a viewer text line, throwing
# the click point off. Cropping to the control's region and OCR'ing it at high
# scale isolates the text so its line-center maps to the real control.
# Temporal-stability gate for the Locator's authoritative frame. A control is
# located only once two consecutive screenshots differ by <= _STABLE_TOLERANCE
# mean grayscale (a tiny budget for cursor blink / compression noise), so we
# never click a window that's still animating open. Bounded by _STABLE_MAX_FRAMES
# so a persistent spinner can't hang the locate.
_STABLE_TOLERANCE = 0.004
_STABLE_SLEEP = 0.2
_STABLE_MAX_FRAMES = 2

_REGION_CROPS: Dict[str, Tuple[float, float, float, float]] = {
    # Right-edge Fusion Inspector strip ("Prepare Noise Profile" button).
    "prepare-profile": (0.78, 0.04, 0.22, 0.55),
    # Neat editor top-left profile buttons ("Auto Profile"/"Generic"/"Load").
    "auto-profile": (0.10, 0.09, 0.45, 0.12),
    # Neat editor bottom-right action buttons ("Apply"/"Cancel").
    "apply": (0.55, 0.88, 0.45, 0.12),
    # Neat editor bottom action row ("Cancel" sits left of "Apply"). The strip
    # is widened to the left of the Apply crop so the Cancel word is included.
    "cancel": (0.40, 0.86, 0.50, 0.14),
    # Bottom-left Neat editor toolbar. The "Input Data Gamma" control is an icon
    # (γ / linear curve), so OCR cannot find it; this crop scopes template
    # matching around the stable toolbar area.
    "input-gamma": (0.02, 0.78, 0.22, 0.22),
    # Centered modal dialogs ("Information"/"OK", demo "Continue").
    "ok": (0.28, 0.36, 0.48, 0.42),
    "continue": (0.18, 0.28, 0.64, 0.52),
    # "Confirm: selected area is small" modal — crop ONLY the button row at the
    # dialog's bottom (Cancel | Use the small area). Excludes the body text
    # ("The selected area is small.") so the locate can't latch onto the word
    # "small" in the message instead of the button.
    "use-small-area": (0.42, 0.54, 0.40, 0.16),
}


def _neat_editor_window() -> Optional[Dict[str, Any]]:
    try:
        return _find_neat_window(windows=_resolve_windows(activate=False))
    except Exception:
        return None


def _input_gamma_button_point() -> Optional[Tuple[float, float]]:
    """Return the Input Data Gamma icon center in window-relative geometry.

    Neat renders this control as an icon, not text. Use a stable offset from the
    Neat editor window's bottom-left toolbar as the bootstrap locator; once a
    clip succeeds, the normal template-learning path records the exact local
    appearance for subsequent clips.
    """
    window = _neat_editor_window()
    if window is None:
        return None
    # From Neat v6 Resolve editor chrome: bottom status bar, immediately after
    # the scan-type "P" button at the far left. This is window-relative, not
    # screen absolute, and serves only as the first-run bootstrap before
    # templates.
    return _window_point(window, 52, -14)


def _screen_size(work_dir: Path, *, name: str) -> Tuple[int, int]:
    global _SCREEN_SIZE_CACHE
    if _SCREEN_SIZE_CACHE is not None:
        return _SCREEN_SIZE_CACHE
    raw = work_dir / name
    _capture_screen(raw)
    try:
        from PIL import Image

        _SCREEN_SIZE_CACHE = Image.open(raw).size
        return _SCREEN_SIZE_CACHE
    except Exception:
        _SCREEN_SIZE_CACHE = (1920, 1080)
        return _SCREEN_SIZE_CACHE


def _input_gamma_button_points_from_screen(work_dir: Path) -> List[Tuple[float, float]]:
    """Fallback Input Data Gamma points when AX cannot enumerate the Neat window.

    The driver only calls this after OCR has confirmed Neat's editor chrome is
    visible. On the render nodes the editor opens full-screen-like, and this
    icon sits in the stable bottom-left toolbar. Use screen fractions rather
    than absolute pixels so the fallback follows resolution changes.
    """
    screen_w, screen_h = _screen_size(work_dir, name="input-gamma-screen-size.png")
    candidates = (
        # Neat editor is centered over Resolve on the render nodes; its bottom
        # toolbar begins around 17% of screen width. Sweep the gamma/icon group,
        # not Resolve's far-left Effects panel.
        (0.197, 0.972),
        (0.215, 0.972),
        (0.235, 0.972),
        (0.255, 0.972),
        (0.275, 0.972),
        (0.197, 0.948),
        (0.215, 0.948),
        (0.235, 0.948),
        (0.255, 0.948),
        (0.275, 0.948),
    )
    return [(float(screen_w) * x, float(screen_h) * y) for x, y in candidates]


def _input_gamma_button_point_from_screen(work_dir: Path) -> Tuple[float, float]:
    return _input_gamma_button_points_from_screen(work_dir)[0]


def _linear_menu_option_point_from_screen(work_dir: Path) -> Tuple[float, float]:
    screen_w, screen_h = _screen_size(work_dir, name="input-gamma-linear-screen-size.png")
    return (float(screen_w) * 0.215, float(screen_h) * 0.922)


def fast_control_point(label: str, work_dir: Path) -> Optional[Tuple[float, float]]:
    """Known stable Neat/Resolve control centers on the render-node layout."""
    fractions = {
        "prepare-profile": (0.891, 0.216),
    }
    frac = fractions.get(label)
    if frac is None:
        return None
    screen_w, screen_h = _screen_size(work_dir, name=f"fast-{label}-screen-size.png")
    return (float(screen_w) * frac[0], float(screen_h) * frac[1])


def capture_control_region(label: str, work_dir: Path, *, name: str) -> Optional[Path]:
    """Capture a tight region around a known control for cheap visual polling."""
    crop_frac = _REGION_CROPS.get(label)
    if crop_frac is None:
        return None
    raw = work_dir / f"{name}-screen.png"
    _capture_screen(raw)
    try:
        from PIL import Image

        img = Image.open(raw)
        screen_w, screen_h = img.size
        fx, fy, fw, fh = crop_frac
        crop_box = (
            int(screen_w * fx),
            int(screen_h * fy),
            int(screen_w * (fx + fw)),
            int(screen_h * (fy + fh)),
        )
        out = work_dir / f"{name}-{label}.png"
        img.crop(crop_box).save(out)
        return out
    except Exception:
        return None


def control_region_changed(
    label: str,
    baseline: Path,
    work_dir: Path,
    *,
    name: str,
    threshold: float = 0.012,
) -> bool:
    current = capture_control_region(label, work_dir, name=name)
    if current is None:
        return False
    return neat_vision.frame_diff(baseline, current) >= threshold


def _linear_menu_option_point(work_dir: Path) -> Optional[Tuple[float, float]]:
    """Locate the "Linear" item in the Input Data Gamma popup."""
    window = _neat_editor_window()
    if window is not None:
        pos = window.get("position") or [0, 0]
        size = window.get("size") or [0, 0]
        # Popup opens above the bottom-left gamma icon. Crop enough of that area
        # to include "Gamma Corrected", "Linear", and "Help" without depending
        # on absolute screen coordinates.
        rel_box = (
            int(pos[0]),
            int(pos[1] + max(0, float(size[1]) - 300)),
            430,
            260,
        )
    else:
        screen_w, screen_h = _screen_size(work_dir, name="input-gamma-menu-screen-size.png")
        rel_box = (
            0,
            int(float(screen_h) * 0.70),
            int(float(screen_w) * 0.45),
            int(float(screen_h) * 0.28),
        )
    rows: List[Dict[str, Any]] = []
    for scale, threshold in ((4, 150), (5, 170), (4, None)):
        rows = _ocr_screen_region(
            work_dir,
            name="input-gamma-menu",
            rel_box=rel_box,
            scale=scale,
            binarize=threshold,
        )
        point = _find_word_center(rows, "linear")
        if point is not None:
            return point
    return None


def _progressive_option_point(work_dir: Path) -> Optional[Tuple[float, float]]:
    """Best-effort locate for Neat's scan-type prompt after choosing Linear."""
    # The prompt/menu may appear as a small popup near the bottom-left or as a
    # modal. Search the screen for the exact word; failing to find it is not an
    # error because Neat only asks once per node/profile state.
    for scale, threshold in ((3, None), (4, 150), (5, 170)):
        rows = _ocr_screen(work_dir, scale=scale)
        point = _find_word_center(rows, "progressive")
        if point is not None:
            return point
    return None


def _locate_button_fullscreen(label: str, work_dir: Path) -> Tuple[float, float]:
    """Locate a control without AX/window enumeration.

    The AX path (``_resolve_windows`` via System Events) stalls when Resolve has
    a modal up — System Events blocks enumerating a modal-bearing process and we
    time out. Instead we OCR a high-scale crop of the control's known screen
    region (see ``_REGION_CROPS``), which isolates its text so the matched
    line's center maps to the real control. On a miss we fall back to a
    full-screen pass (helps when a window opened in an unexpected position).
    """
    rows: List[Dict[str, Any]] = []
    crop_frac = _REGION_CROPS.get(label)
    if crop_frac is not None:
        raw = work_dir / "region-size.png"
        _capture_screen(raw)
        try:
            from PIL import Image

            screen_w, screen_h = Image.open(raw).size
        except Exception:
            screen_w, screen_h = 1920, 1080
        fx, fy, fw, fh = crop_frac
        crop = (
            int(screen_w * fx),
            int(screen_h * fy),
            int(screen_w * fw),
            int(screen_h * fh),
        )
        # (scale, binarize-threshold). Neat's editor buttons are grey-on-grey
        # and only OCR cleanly after threshold binarization; the contrast pass
        # handles the higher-contrast Inspector/modal text.
        if label in {"auto-profile", "apply", "cancel", "use-small-area"}:
            passes = [(4, 150), (5, 170), (4, None)]
        else:
            passes = [(3, None), (5, None)]
        for scale, threshold in passes:
            rows = _ocr_screen_region(
                work_dir, name=f"region-{label}", rel_box=crop, scale=scale, binarize=threshold
            )
            point = _button_point_from_rows(label, rows)
            if point is not None:
                return point

    for scale in (2, 4):
        rows = _ocr_screen(work_dir, scale=scale)
        point = _button_point_from_rows(label, rows)
        if point is not None:
            return point
    artifacts = _save_locate_failure(label, work_dir, rows, None)
    raise RuntimeError(
        f"Could not OCR-locate `{label}` button. "
        f"Diagnostic artifacts: {artifacts}. OCR text: {_screen_text(rows)[:400]!r}"
    )


def _click_ocr_fullscreen(label: str, work_dir: Path) -> Tuple[float, float]:
    point = _locate_button_fullscreen(label, work_dir)
    _click_at_quartz(point[0], point[1])
    return point


def _locate_editor_control(label: str, work_dir: Path) -> Optional[Tuple[float, float]]:
    """Window-anchored locate for Neat *editor* controls (auto-profile/apply/cancel).

    Anchoring to the Neat editor window rect (from the accessibility API)
    instead of screen fractions keeps control geometry stable regardless of
    where the window sits or the screen resolution — the full-screen region
    crops in ``_REGION_CROPS`` assume the editor always opens centered/maximized
    at a fixed resolution, which is exactly the assumption that breaks when it
    doesn't. The editor is a regular (non-modal) window, so AX enumeration reads
    it reliably (unlike Neat's modal dialogs, which can stall System Events).

    Never raises: any AX/OCR failure returns ``None`` so the caller falls back
    to the full-screen region-crop locate and the proven path is preserved.
    """
    try:
        windows = _resolve_windows(activate=False)
    except Exception:
        return None
    try:
        window = _find_neat_window(windows=windows)
    except Exception:
        return None
    if window is None:
        return None
    try:
        return _locate_button(label, work_dir, window=window)
    except Exception:
        return None


class Locator:
    """Capture-once, template-first, OCR-fallback control locator with
    self-calibrating template learning.

    Locate strategy per control, in order:

      1. **Template match** (``_neat_vision``) — deterministic, scoped to the
         control's screen region. Skipped until a template has been learned.
      2. **Window-anchored OCR** (editor controls only) — anchors to the Neat
         editor window's accessibility rect, stable regardless of window
         position or resolution.
      3. **Full-screen region-crop OCR** — the proven fallback.

    On an OCR-path success the located region is remembered. ``commit_templates``
    (called when the *clip* succeeds — Auto Profile → Apply → window closed)
    saves those regions as templates, so the next clip matches them directly.
    ``discard_templates`` drops them on failure, so a misread locate is never
    learned. The net effect: clip 1 bootstraps via OCR, clips 2..N run on fast,
    near-deterministic template matching; if anything in the template path fails
    the locator falls straight back to OCR, so the worst case is the OCR-only
    behavior.
    """

    def __init__(self, work_dir: Path, *, learn: bool = True) -> None:
        self.work_dir = work_dir
        self.learn = bool(learn) and neat_vision.available()
        self._seq = 0
        # label -> (point, frame_path, screen_w, screen_h)
        self._pending: Dict[str, Tuple[Tuple[float, float], Path, int, int]] = {}
        # (label, screen_w, screen_h) located via template match this drive — so
        # a failed clip can distrust exactly the templates it relied on.
        self._used_templates: List[Tuple[str, int, int]] = []

    def _next_frame(self, label: str) -> Path:
        self._seq += 1
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        return self.work_dir / f"loc-{safe}-{self._seq}.png"

    def _capture_stable(self, label: str) -> Optional[Path]:
        """Capture screenshots until two consecutive frames are visually
        settled (temporal smoothing), and return the settled frame.

        Locating/clicking a frame captured mid-animation — a window sliding
        open, a dialog fading in — is a real source of garbled reads and
        misclicks. We hold off until the screen stops changing. Bounded: after
        ``_STABLE_MAX_FRAMES`` we return the last frame regardless (a persistent
        spinner must not hang the locate). Best-effort: a capture failure
        returns the last good frame (or ``None``), and the caller falls back to
        the OCR path, which recaptures internally.
        """
        last: Optional[Path] = None
        for _ in range(_STABLE_MAX_FRAMES):
            frame = self._next_frame(label)
            try:
                _capture_screen(frame)
            except Exception:
                return last
            if last is not None and neat_vision.frame_diff(last, frame) <= _STABLE_TOLERANCE:
                return frame
            last = frame
            time.sleep(_STABLE_SLEEP)
        return last

    def _region(
        self, label: str, screen_w: int, screen_h: int
    ) -> Optional[Tuple[int, int, int, int]]:
        if label == "input-gamma":
            window = _neat_editor_window()
            if window is not None:
                pos = window.get("position") or [0, 0]
                size = window.get("size") or [0, 0]
                return (
                    int(pos[0] + 30),
                    int(pos[1] + max(0, float(size[1]) - 55)),
                    70,
                    55,
                )
        frac = _REGION_CROPS.get(label)
        if frac is None:
            return None
        fx, fy, fw, fh = frac
        return (
            int(screen_w * fx),
            int(screen_h * fy),
            int(screen_w * fw),
            int(screen_h * fh),
        )

    def locate_and_click(self, label: str, *, editor: bool) -> Tuple[Tuple[float, float], str]:
        """Locate ``label`` and click it. Returns ``(point, method)`` where
        ``method`` is ``"template:<score>"``, ``"window"``, or ``"fullscreen"``.
        Raises ``RuntimeError`` only if every strategy misses.
        """
        frame: Optional[Path] = None
        size: Optional[Tuple[int, int]] = None

        # Fast path: if a verified template exists for this resolution, one
        # screenshot is enough. Avoid the temporal-stability gate and OCR cost
        # on learned controls; fall back below if the template misses.
        if self.learn:
            quick = self._next_frame(label)
            try:
                _capture_screen(quick)
                quick_size = neat_vision.image_size(quick)
            except Exception:
                quick_size = None
            if quick_size is not None and neat_vision.has_template(
                label, quick_size[0], quick_size[1]
            ):
                region = self._region(label, quick_size[0], quick_size[1])
                match = neat_vision.match_template(
                    label, quick, quick_size[0], quick_size[1], region=region
                )
                if match is not None:
                    mx, my, score = match
                    self._used_templates.append((label, quick_size[0], quick_size[1]))
                    _click_at_quartz(mx, my)
                    return (mx, my), f"template:{score:.2f}"
            frame = quick
            size = quick_size

        # Authoritative frame for OCR/template fallback: wait briefly for the
        # screen to settle so we never locate against a mid-animation render.
        if frame is None or size is None:
            frame = self._capture_stable(label)
            size = neat_vision.image_size(frame) if frame is not None else None

        if frame is not None and size is not None:
            region = self._region(label, size[0], size[1])
            match = neat_vision.match_template(label, frame, size[0], size[1], region=region)
            if match is not None:
                mx, my, score = match
                self._used_templates.append((label, size[0], size[1]))
                _click_at_quartz(mx, my)
                return (mx, my), f"template:{score:.2f}"

        # 2/3. OCR fallback (window-anchored first for editor controls).
        point: Optional[Tuple[float, float]] = None
        method = "fullscreen"
        if label == "input-gamma":
            point = _input_gamma_button_point()
            if point is not None:
                method = "window-geometry"
        elif editor:
            point = _locate_editor_control(label, self.work_dir)
            if point is not None:
                method = "window"
        if point is None:
            point = _locate_button_fullscreen(label, self.work_dir)  # raises on total miss
            method = "fullscreen"

        # Remember this OCR locate so a *successful* clip can learn it. Latest
        # wins, so the template always reflects the most recent good appearance.
        if self.learn and frame is not None and size is not None:
            self._pending[label] = (point, frame, size[0], size[1])

        _click_at_quartz(point[0], point[1])
        return point, method

    def commit_templates(self) -> List[str]:
        """Persist pending OCR-located regions as templates. Returns the labels
        learned. Best-effort: a save failure is skipped, never raised."""
        learned: List[str] = []
        if not self.learn:
            self._pending.clear()
            return learned
        for label, (point, frame, width, height) in list(self._pending.items()):
            if neat_vision.save_template(label, frame, point, width, height) is not None:
                learned.append(label)
        self._pending.clear()
        return learned

    def discard_templates(self) -> None:
        self._pending.clear()

    def invalidate_used_templates(self) -> List[str]:
        """Delete the templates this drive matched against, so they are
        re-learned via OCR next time. Called after a FAILED clip to self-heal a
        rare poisoned template. Returns the labels invalidated."""
        invalidated: List[str] = []
        for label, width, height in list(self._used_templates):
            if neat_vision.delete_template(label, width, height):
                invalidated.append(label)
        self._used_templates.clear()
        return invalidated


def _click_at_quartz(x: float, y: float) -> None:
    _activate_resolve(settle=0.25)
    click.click_at(float(x), float(y))


def open_prepare_profile_via_api(
    work_dir: Path,
    *,
    timeout: float = 6.0,
    target_env: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """Trigger Neat's Prepare Noise Profile button through the OFX input.

    Neat exposes the inspector button as input ID ``Prepare Profile___``. Calling
    it blocks the scripting process until the plugin window closes, so this
    launches a throwaway helper, waits only until the Neat editor window appears,
    then terminates the blocked helper and lets the parent continue driving UI.
    """
    env = os.environ.copy()
    if target_env:
        env.update(target_env)
    helper = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--_prepare-profile-helper"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    start = time.time()
    dismissed: List[str] = []
    try:
        while time.time() - start < timeout:
            # Do not use AX/JXA window enumeration here. Neat's modal can make
            # System Events hang while the helper is blocked in SetInput().
            # Screenshot/OCR remains responsive and is enough to prove the OFX
            # button opened a Neat UI state.
            try:
                state, _text, _rows = _read_screen_state(work_dir)
            except Exception:
                state = "unknown"
            if state == "information-dialog":
                detail = dismiss_information_dialog(work_dir)
                if detail is not None:
                    dismissed.append(f"information-dialog:{detail}")
                else:
                    _press_return()
                    dismissed.append("information-dialog:return")
                time.sleep(0.4)
                continue
            if state == "confirm-build-profile":
                point = locate_confirm_button(work_dir, "continue") or locate_confirm_continue_button(work_dir)
                if point is not None:
                    _click_at_quartz(point[0], point[1])
                    dismissed.append("confirm-build-profile")
                    time.sleep(0.4)
                    continue
            if state == "confirm-small-area":
                point = locate_modal_button(work_dir, "use")
                if point is not None:
                    _click_at_quartz(point[0], point[1])
                    dismissed.append("confirm-small-area")
                    time.sleep(0.4)
                    continue
            if state in ("preparing-input", "editor-unprofiled", "editor-profiled", "editor"):
                try:
                    helper.terminate()
                    helper.wait(timeout=1)
                except Exception:
                    try:
                        helper.kill()
                    except Exception:
                        pass
                kind = "editor" if state.startswith("editor") else state
                suffix = f";dismissed={','.join(dismissed)}" if dismissed else ""
                return True, f"api-{kind}:{time.time() - start:.1f}s{suffix}"
            if helper.poll() is not None:
                stdout, stderr = helper.communicate(timeout=1)
                detail = (stderr.strip() or stdout.strip() or f"exit {helper.returncode}")[:120]
                return False, f"api-exit:{detail}"
            time.sleep(0.2)
    finally:
        if helper.poll() is None:
            try:
                helper.terminate()
                helper.wait(timeout=1)
            except Exception:
                try:
                    helper.kill()
                except Exception:
                    pass
    return False, f"api-timeout:{timeout:.1f}s"


def choose_input_data_linear(work_dir: Path, locator: Optional[Locator] = None) -> List[str]:
    """Set Neat's bottom-left Input Data Gamma control to Linear.

    Both the icon and its popup are non-text UI elements in practice; use the
    render node's stable screen geometry and avoid the old multi-pass OCR scan.
    """
    del locator  # This icon is not text; direct geometry is faster and proved reliable.
    steps: List[str] = []
    point = _input_gamma_button_point_from_screen(work_dir)
    _click_at_quartz(point[0], point[1])
    steps.append(f"input-gamma:screen-geometry:{round(point[0])},{round(point[1])}")
    time.sleep(0.12)

    linear = _linear_menu_option_point_from_screen(work_dir)
    linear_method = "screen-geometry"
    _click_at_quartz(linear[0], linear[1])
    steps.append(f"input-gamma-linear:{linear_method}:{round(linear[0])},{round(linear[1])}")
    time.sleep(0.15)
    return steps


def _press_escape() -> None:
    """Send a single Escape key to the frontmost (Resolve/Neat) window.

    Neat's editor and its modal dialogs are Qt windows whose default
    "reject"/close action is bound to Escape, so this is the most reliable
    universal dismiss when OCR can't pin a Cancel button. Best-effort: a
    Quartz click failure must never abort the caller's cleanup loop.
    """
    _activate_resolve(settle=0.2)
    try:
        click.press_key("escape")
    except Exception:
        return


def _press_return() -> None:
    """Send a single Return key to the frontmost (Resolve/Neat) window.

    On a focused Qt "Confirm" modal this triggers the default (highlighted)
    button — for Neat's "cannot find a large uniform area" dialog that is
    "Continue building profile" — a deterministic fallback when OCR-locating
    the button is unavailable. Best-effort, like :func:`_press_escape`.
    """
    _activate_resolve(settle=0.2)
    try:
        click.press_key("return")
    except Exception:
        return


# Safe "dismiss" button labels on Resolve's blocking startup dialogs (Software
# Update "Skip | Download", restore-projects prompts). Ordered by preference;
# all are non-destructive cancels. Deliberately excludes "Download", "Save",
# "Delete", "OK" so we never confirm a destructive/irreversible action.
_DISMISS_TOKENS: Tuple[str, ...] = ("skip", "later", "not now", "notnow", "cancel", "close")


def dismiss_blocking_dialog() -> bool:
    """Click a non-destructive dismiss button on a blocking startup dialog.

    A fresh Resolve launch can pop a Cocoa modal (Software Update, restore
    projects) that has no Escape binding and freezes scripting PM navigation
    (GetCurrentProject stays None, OpenFolder returns None). Escape can't clear
    it, so we OCR the screen for a safe cancel label and click it. Two OCR passes
    — contrast (large white-on-dark labels) and binarized (grey-on-grey buttons)
    — cover both dialog styles. Returns True if a dismiss button was clicked.
    Best-effort: never raises into the caller's recovery loop.
    """
    work = _cache_base() / "dismiss"
    try:
        work.mkdir(parents=True, exist_ok=True)
        probe = work / "probe.png"
        _capture_screen(probe)
        from PIL import Image

        screen_w, screen_h = Image.open(probe).size
    except Exception:
        return False
    # Resolve's startup dialogs are centered; their button row sits in the
    # lower-center of the screen. OCR just that band at high scale with a low
    # binarize threshold — the buttons are small grey-on-grey labels that a
    # plain full-screen contrast pass can't resolve (only the dialog title
    # reads). scale=4 + threshold ~120/140 makes "Skip" crisp (validated on the
    # Software Update dialog at 1920x1080).
    box = (
        int(screen_w * 0.42),
        int(screen_h * 0.48),
        int(screen_w * 0.34),
        int(screen_h * 0.22),
    )
    rows: List[Dict[str, Any]] = []
    for binarize in (120, 140):
        try:
            rows += _ocr_screen_region(
                work, name=f"dismiss-{binarize}", scale=4, binarize=binarize, rel_box=box
            )
        except Exception:
            continue
    for token in _DISMISS_TOKENS:
        point = _find_word_center(rows, token, prefer_bottom_right=True)
        if point is None:
            continue
        try:
            _activate_resolve(settle=0.2)
            _click_at_quartz(point[0], point[1])
            return True
        except Exception:
            return False
    return False


# Central screen band where Neat's "Confirm" modals render (centered over the
# editor). They are small grey-on-grey Qt dialogs the full-screen OCR pass
# garbles (e.g. "Cancel | Continue building profile" → "[meares
# [eontinuelburingipronie)"). OCR'ing a tight, high-scale, BINARIZED crop of
# this band — the same recipe that makes the editor's grey buttons readable —
# reads the dialog text and button labels cleanly.
_MODAL_BAND_CROP = (0.16, 0.18, 0.68, 0.42)

# Modal-specific tokens (NOT present in the normal editor chrome — the editor
# has "Build Profile"/"Generic Profile" tabs but never "uniform area",
# "continue building", or "quality may be low").
_MODAL_CONFIRM_TOKENS = ("continue building", "uniform area", "quality may be low")
_MODAL_INFO_TOKENS = ("not selected", "before opening", "frames from the beginning")


def _ocr_modal_band(
    work_dir: Path,
    *,
    passes: Sequence[Tuple[int, Optional[int]]] = ((6, 170),),
) -> List[Dict[str, Any]]:
    """High-scale, binarized OCR of the central modal band.

    Returns the first pass whose text contains a modal/confirm token (so
    detection short-circuits cheaply), else the richest pass seen. Each pass is
    ``(scale, binarize_threshold)``; ``None`` threshold uses contrast-enhanced
    grayscale.
    """
    try:
        raw = work_dir / "modal-band-probe.png"
        _capture_screen(raw)
        from PIL import Image

        sw, sh = Image.open(raw).size
    except Exception:
        return []
    fx, fy, fw, fh = _MODAL_BAND_CROP
    box = (int(sw * fx), int(sh * fy), int(sw * fw), int(sh * fh))
    best: List[Dict[str, Any]] = []
    for scale, threshold in passes:
        try:
            rows = _ocr_screen_region(
                work_dir, name="modal-band", rel_box=box, scale=scale, binarize=threshold
            )
        except Exception:
            continue
        txt = _screen_text(rows)
        if any(tok in txt for tok in _MODAL_CONFIRM_TOKENS) or "cancel" in txt:
            return rows
        if len(rows) > len(best):
            best = rows
    return best


def locate_modal_button(work_dir: Path, which: str = "continue") -> Optional[Tuple[float, float]]:
    """Locate a button on a Neat modal via high-scale band OCR.

    ``which`` is ``"continue"`` (the "Continue building profile" accept button),
    ``"cancel"``, or ``"ok"``. Both the dialog body and the button can contain words
    ("...continue building profile using the selected area..."), so we anchor on
    the BOTTOM-most occurrence — the button sits below the message. Returns a
    screen-coordinate click point, or ``None`` if OCR can't find it.
    """
    rows = _ocr_modal_band(work_dir, passes=((6, 170), (6, 150), (8, None)))
    if which == "ok":
        return _find_word_center(rows, "ok", prefer_bottom_right=True)
    if which == "cancel":
        return _find_word_center(rows, "cancel", prefer_bottom_right=True)
    return _find_word_center(rows, "continue", prefer_bottom_right=True)


def locate_information_ok_button(work_dir: Path) -> Optional[Tuple[float, float]]:
    """Locate the OK button on Neat's centered Information modal by geometry.

    The modal's "OK" glyph is tiny and OCR often locks onto the icon/body text
    instead. Detect the modal rectangle in the central screen area and click the
    standard bottom-right button position relative to that rectangle.
    """
    raw = work_dir / "information-modal-screen.png"
    try:
        _capture_screen(raw)
        from PIL import Image
    except Exception:
        return None

    image = Image.open(raw).convert("RGB")
    width, height = image.size
    x0, x1 = int(width * 0.20), int(width * 0.80)
    y0, y1 = int(height * 0.15), int(height * 0.70)
    visited: set[Tuple[int, int]] = set()
    best: Optional[Tuple[int, int, int, int, int]] = None
    pix = image.load()
    if pix is None:
        return None

    def is_modal_pixel(x: int, y: int) -> bool:
        rgb = pix[x, y]
        if not isinstance(rgb, tuple):
            return False
        r, g, b = rgb[:3]
        # Neat's modal body/border is a brighter neutral gray than Resolve's
        # background. Require low chroma so colored thumbnails/icons do not seed
        # the component.
        return (
            58 <= r <= 130
            and 58 <= g <= 130
            and 58 <= b <= 130
            and max(r, g, b) - min(r, g, b) <= 18
        )

    for sy in range(y0, y1, 3):
        for sx in range(x0, x1, 3):
            if (sx, sy) in visited or not is_modal_pixel(sx, sy):
                continue
            stack = [(sx, sy)]
            visited.add((sx, sy))
            xs: List[int] = []
            ys: List[int] = []
            while stack:
                x, y = stack.pop()
                xs.append(x)
                ys.append(y)
                for nx, ny in ((x + 3, y), (x - 3, y), (x, y + 3), (x, y - 3)):
                    if nx < x0 or nx >= x1 or ny < y0 or ny >= y1 or (nx, ny) in visited:
                        continue
                    visited.add((nx, ny))
                    if is_modal_pixel(nx, ny):
                        stack.append((nx, ny))
            if not xs:
                continue
            left, right = min(xs), max(xs)
            top, bottom = min(ys), max(ys)
            area = len(xs)
            if right - left < 180 or bottom - top < 45:
                continue
            candidate = (area, left, top, right, bottom)
            if best is None or candidate[0] > best[0]:
                best = candidate

    if best is None:
        ocr_point = locate_modal_button(work_dir, "ok")
        if ocr_point is None:
            return None
        # The OCR point on Neat's Information dialog has proven to sit on the
        # lower button edge on render nodes. Shift into the button body.
        return (float(ocr_point[0]), float(max(0.0, ocr_point[1] - 22.0)))
    _, left, top, right, bottom = best
    del left, top
    return (float(right - 31), float(bottom - 28))


def _click_ok_button_via_ax() -> bool:
    """Click a visible Resolve/Neat OK button through macOS Accessibility."""
    script = f"""
const se = Application("System Events");
const proc = se.processes.byName({json.dumps(RESOLVE_PROCESS)});
if (!proc.exists()) {{
  JSON.stringify({{ok: false, clicked: false, error: "Resolve process not found"}});
}} else {{
  try {{ proc.frontmost = true; }} catch (err) {{}}
  function text(fn) {{
    try {{
      const value = fn();
      return value === null || value === undefined ? "" : String(value);
    }} catch (err) {{ return ""; }}
  }}
  let clicked = false;
  const wins = proc.windows();
  for (let i = 0; i < wins.length && !clicked; i++) {{
    let buttons = [];
    try {{ buttons = wins[i].buttons(); }} catch (err) {{ buttons = []; }}
    for (let j = 0; j < buttons.length; j++) {{
      const label = (
        text(() => buttons[j].name()) + " " +
        text(() => buttons[j].title()) + " " +
        text(() => buttons[j].description())
      ).toLowerCase();
      if (label.split(/\\s+/).includes("ok")) {{
        try {{ buttons[j].click(); clicked = true; break; }} catch (err) {{}}
      }}
    }}
  }}
  JSON.stringify({{ok: true, clicked}});
}}
"""
    try:
        data = _run_jxa(script, timeout=2.0)
    except Exception:
        return False
    return bool(data.get("clicked"))


def dismiss_information_dialog(work_dir: Path) -> Optional[str]:
    """Dismiss Neat's Information modal and confirm it disappeared.

    Neat's OK hit target varies between modal layouts and OCR can lock onto the
    button edge. Try a small lower-right sweep around the geometry/OCR anchors
    and verify the next screen state before declaring success.
    """
    if _click_ok_button_via_ax():
        time.sleep(0.2)
        try:
            state, _text, _rows = _read_screen_state(work_dir)
        except Exception:
            state = "unknown"
        if state != "information-dialog":
            return "ax-ok"

    anchors: List[Tuple[str, Tuple[float, float]]] = []
    geom = locate_information_ok_button(work_dir)
    if geom is not None:
        anchors.append(("geometry", geom))
    ocr = locate_modal_button(work_dir, "ok")
    if ocr is not None:
        anchors.append(("modal", ocr))

    # Known Neat information dialogs place OK in the modal's lower-right action
    # row. Sweep that area quickly; the modal consumes clicks until it closes,
    # and these points do not overlap Neat's destructive Apply button.
    _activate_resolve(settle=0.15)
    for method, point in anchors:
        del method
        for dx in (0.0, -32.0, -64.0, -96.0, -128.0, -160.0):
            for dy in (0.0, -18.0, -36.0, 18.0):
                click.click_at(max(0.0, point[0] + dx), max(0.0, point[1] + dy))
                time.sleep(0.03)
    _press_return()
    time.sleep(0.35)
    try:
        state, _text, _rows = _read_screen_state(work_dir)
    except Exception:
        state = "unknown"
    if state != "information-dialog":
        return "sweep"
    return None


def locate_confirm_continue_button(work_dir: Path) -> Optional[Tuple[float, float]]:
    """Geometry fallback for Neat's Confirm modal accept button."""
    raw = work_dir / "confirm-modal-screen.png"
    try:
        _capture_screen(raw)
        from PIL import Image
    except Exception:
        return None

    image = Image.open(raw).convert("RGB")
    width, height = image.size
    x0, x1 = int(width * 0.20), int(width * 0.85)
    y0, y1 = int(height * 0.18), int(height * 0.62)
    visited: set[Tuple[int, int]] = set()
    best: Optional[Tuple[int, int, int, int, int]] = None
    pix = image.load()
    if pix is None:
        return None

    def is_modal_pixel(x: int, y: int) -> bool:
        rgb = pix[x, y]
        if not isinstance(rgb, tuple):
            return False
        r, g, b = rgb[:3]
        return (
            58 <= r <= 135
            and 58 <= g <= 135
            and 58 <= b <= 135
            and max(r, g, b) - min(r, g, b) <= 20
        )

    for sy in range(y0, y1, 3):
        for sx in range(x0, x1, 3):
            if (sx, sy) in visited or not is_modal_pixel(sx, sy):
                continue
            stack = [(sx, sy)]
            visited.add((sx, sy))
            xs: List[int] = []
            ys: List[int] = []
            while stack:
                x, y = stack.pop()
                xs.append(x)
                ys.append(y)
                for nx, ny in ((x + 3, y), (x - 3, y), (x, y + 3), (x, y - 3)):
                    if nx < x0 or nx >= x1 or ny < y0 or ny >= y1 or (nx, ny) in visited:
                        continue
                    visited.add((nx, ny))
                    if is_modal_pixel(nx, ny):
                        stack.append((nx, ny))
            if not xs:
                continue
            left, right = min(xs), max(xs)
            top, bottom = min(ys), max(ys)
            area = len(xs)
            if right - left < 220 or bottom - top < 60:
                continue
            candidate = (area, left, top, right, bottom)
            if best is None or candidate[0] > best[0]:
                best = candidate

    if best is None:
        return None
    _, left, top, right, bottom = best
    del left, top
    return (float(right - 120), float(bottom - 14))


def locate_confirm_button(work_dir: Path, which: str = "continue") -> Optional[Tuple[float, float]]:
    return locate_modal_button(work_dir, which)


def _set_timeline_to_item_midpoint(timeline: Any, item: Any) -> None:
    try:
        mid_frame = int(item.GetStart()) + max(1, int(item.GetDuration()) // 2)
        fps = round(float(timeline.GetSetting("timelineFrameRate")))
        parts = str(timeline.GetStartTimecode()).split(":")
        start_tc_frame = (
            int(parts[0]) * 3600 * fps
            + int(parts[1]) * 60 * fps
            + int(parts[2]) * fps
            + int(parts[3])
        )
        target = start_tc_frame + (mid_frame - int(timeline.GetStartFrame()))
        h = int(target // (3600 * fps))
        rem = target - h * 3600 * fps
        m = int(rem // (60 * fps))
        rem -= m * 60 * fps
        s = int(rem // fps)
        f = int(rem - s * fps)
        timeline.SetCurrentTimecode(f"{h:02d}:{m:02d}:{s:02d}:{f:02d}")
    except Exception:
        pass


def _num_attr(attrs: Dict[str, Any], key: str) -> Optional[float]:
    try:
        value = attrs.get(key)
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _comp_range_for_item(comp: Any, item: Any) -> Dict[str, Any]:
    duration = int(item.GetDuration())
    clip_start = int(item.GetStart())
    clip_end = clip_start + max(1, duration) - 1
    local_start = 0.0
    local_end = float(max(0, duration - 1))
    local_mid = local_start + ((local_end - local_start) / 2.0)
    clip_mid_abs = clip_start + max(1, duration // 2)
    try:
        attrs = comp.GetAttrs() or {}
    except Exception:
        attrs = {}

    for start_key, end_key in (
        ("COMPN_RenderStart", "COMPN_RenderEnd"),
        ("COMPN_GlobalStart", "COMPN_GlobalEnd"),
    ):
        start = _num_attr(attrs, start_key)
        end = _num_attr(attrs, end_key)
        if start is None or end is None or end < start:
            continue
        return {
            "start": start,
            "end": end,
            "frame": start + ((end - start) / 2.0),
            "range_keys": [start_key, end_key],
            "clip_start": clip_start,
            "clip_end": clip_end,
            "clip_mid_abs": clip_mid_abs,
            "local_start": local_start,
            "local_end": local_end,
            "local_mid": local_mid,
        }

    return {
        "start": float(clip_start),
        "end": float(clip_end),
        "frame": float(clip_mid_abs),
        "range_keys": None,
        "clip_start": clip_start,
        "clip_end": clip_end,
        "clip_mid_abs": clip_mid_abs,
        "local_start": local_start,
        "local_end": local_end,
        "local_mid": local_mid,
    }


def _set_tool_inputs(tool: Any, values: Dict[str, float]) -> List[str]:
    applied: List[str] = []
    try:
        available = set((tool.GetInputList() or {}).keys())
    except Exception:
        available = set()
    for key, value in values.items():
        if available and key not in available:
            continue
        try:
            tool.SetInput(key, value)
            applied.append(key)
        except Exception:
            continue
    return applied


def _prime_comp_media_range(comp: Any, item: Any) -> Dict[str, Any]:
    """Set the clip comp and MediaIn to an in-range frame before opening Neat."""
    info = _comp_range_for_item(comp, item)
    start = float(info["start"])
    end = float(info["end"])
    frame = float(info["frame"])

    try:
        comp.SetAttrs(
            {
                "COMPN_GlobalStart": start,
                "COMPN_GlobalEnd": end,
                "COMPN_RenderStart": start,
                "COMPN_RenderEnd": end,
                "COMPN_CurrentTime": frame,
            }
        )
    except Exception:
        comp.SetAttrs({"COMPN_CurrentTime": frame})

    media_in = _find_tool(comp, "MediaIn1")
    media_out = _find_tool(comp, "MediaOut1")
    if media_in is None or media_out is None:
        raise RuntimeError("Fusion comp missing MediaIn1/MediaOut1; Neat would open on black")

    info["media_in_set"] = _set_tool_inputs(
        media_in,
        {
            "GlobalIn": start,
            "GlobalOut": end,
            "ClipTimeStart": float(info["local_start"]),
            "ClipTimeEnd": float(info["local_end"]),
        },
    )
    return info


def _find_tool(comp: Any, name: str) -> Any:
    try:
        return comp.FindTool(name)
    except Exception:
        return None


def _comp_time_for_item(comp: Any, item: Any) -> Tuple[float, Dict[str, Any]]:
    """Pick a Fusion comp frame that is guaranteed to lie inside the clip comp."""
    duration = int(item.GetDuration())
    clip_mid_abs = int(item.GetStart()) + max(1, duration // 2)
    local_mid = max(1, duration // 2)
    try:
        attrs = comp.GetAttrs() or {}
    except Exception:
        attrs = {}

    for start_key, end_key in (
        ("COMPN_RenderStart", "COMPN_RenderEnd"),
        ("COMPN_GlobalStart", "COMPN_GlobalEnd"),
    ):
        start = _num_attr(attrs, start_key)
        end = _num_attr(attrs, end_key)
        if start is None or end is None or end < start:
            continue
        if start <= clip_mid_abs <= end:
            chosen = float(clip_mid_abs)
        elif start <= local_mid <= end:
            chosen = float(local_mid)
        else:
            chosen = float(start + ((end - start) / 2.0))
        return chosen, {
            "frame": chosen,
            "range": [start, end],
            "range_keys": [start_key, end_key],
            "clip_mid_abs": clip_mid_abs,
            "local_mid": local_mid,
        }

    return float(clip_mid_abs), {
        "frame": float(clip_mid_abs),
        "range": None,
        "range_keys": None,
        "clip_mid_abs": clip_mid_abs,
        "local_mid": local_mid,
    }


def _set_comp_to_item_midpoint(comp: Any, item: Any) -> Dict[str, Any]:
    return _prime_comp_media_range(comp, item)


def _load_item_fusion_comp(item: Any) -> Any:
    """Load the target timeline item's comp into Resolve's visible Fusion context."""
    comp = None
    try:
        names = item.GetFusionCompNameList() or []
    except Exception:
        names = []
    for name in names:
        try:
            comp = item.LoadFusionCompByName(name)
        except Exception:
            comp = None
        if comp is not None:
            break
    if comp is None:
        try:
            comp = item.AddFusionComp()
        except Exception:
            comp = None
    if comp is None:
        try:
            comp = item.GetFusionCompByIndex(1) if item.GetFusionCompCount() else None
        except Exception:
            comp = None
    if comp is not None:
        _set_comp_to_item_midpoint(comp, item)
    return comp


def _open_neat_helper_current() -> int:
    # Never import DaVinciResolveScript directly — connect through dvr
    # (local-only). The Neat helper iterates Resolve's raw OFX nodes, so it
    # uses the raw fusionscript handle.
    try:
        from autoneat.resolve import connect_resolve_raw
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"Could not import Resolve connection helper: {exc}"}
            ),
            flush=True,
        )
        return 1

    try:
        resolve = connect_resolve_raw()
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"Could not connect to local Resolve via dvr: {exc}"}
            ),
            flush=True,
        )
        return 1
    # TODO: upstream to dvr — Neat OFX node manipulation needs the raw handle.

    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    item = None
    target_track = os.environ.get("AUTONEAT_TARGET_TRACK")
    target_start = os.environ.get("AUTONEAT_TARGET_START")
    target_name = os.environ.get("AUTONEAT_TARGET_NAME")
    have_target = bool(timeline and target_track and target_start)
    if have_target:
        try:
            assert timeline is not None and target_track is not None and target_start is not None
            for candidate in timeline.GetItemListInTrack("video", int(target_track)) or []:
                if int(candidate.GetStart()) != int(target_start):
                    continue
                if target_name and candidate.GetName() != target_name:
                    continue
                item = candidate
                break
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"Failed to enumerate track {target_track!r}: {exc}",
                    }
                ),
                flush=True,
            )
            return 1
        if item is None:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": (
                            f"No clip on video track {target_track} starts at frame {target_start}"
                            + (f" with name {target_name!r}" if target_name else "")
                        ),
                    }
                ),
                flush=True,
            )
            return 1
    else:
        item = timeline.GetCurrentVideoItem() if timeline else None
    if item is None:
        print(json.dumps({"ok": False, "error": "No target video item"}), flush=True)
        return 1

    if timeline is not None:
        _set_timeline_to_item_midpoint(timeline, item)
    resolve.OpenPage("fusion")
    try:
        comp = _load_item_fusion_comp(item)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 1
    if comp is None:
        print(json.dumps({"ok": False, "error": "Could not get/add Fusion comp"}), flush=True)
        return 1
    media_range = _set_comp_to_item_midpoint(comp, item)

    def find_tool_by_id(tool_id: str) -> Any:
        for tool in (comp.GetToolList(False) or {}).values():
            try:
                if tool.ID == tool_id:
                    return tool
            except Exception:
                continue
        return None

    def find_tool(name: str) -> Any:
        try:
            return comp.FindTool(name)
        except Exception:
            return None

    def connect(dst: Any, input_name: str, src: Any) -> bool:
        for args in (
            (input_name, src, "Output"),
            (input_name, src),
            (input_name, src, "MainOutput"),
        ):
            try:
                if dst.ConnectInput(*args):
                    return True
            except TypeError:
                continue
            except Exception:
                continue
        return False

    # Reset: delete any existing Neat node + CST wrap tools so the node is
    # re-added from scratch (clears a stale or half-built noise profile). The
    # MediaIn1 → MediaOut1 connections are rebuilt by the connect logic below.
    if os.environ.get("AUTONEAT_RESET") == "1":
        for victim in (
            find_tool(SCALE_DOWN_NAME),
            find_tool(CST_IN_NAME),
            find_tool(CST_OUT_NAME),
            find_tool(SCALE_UP_NAME),
            find_tool_by_id(NEAT_REG_ID),
        ):
            if victim is not None:
                try:
                    victim.Delete()
                except Exception:
                    pass

    reuse_existing = os.environ.get("AUTONEAT_FORCE_NEW") != "1"
    neat = find_tool_by_id(NEAT_REG_ID) if reuse_existing else None
    if neat is None:
        neat = comp.AddTool(NEAT_REG_ID, 1, 0)
    if neat is None:
        print(
            json.dumps({"ok": False, "error": f"Could not add Neat OFX {NEAT_REG_ID}"}), flush=True
        )
        return 1

    # ------------------------------------------------------------------
    # Color-management wrap: in ACES projects MediaIn1 emits AP0 linear,
    # which renders as near-black on display and breaks Neat's Auto Profile.
    # Wrap Neat with two ColorSpaceTransform tools so it sees display-referred
    # PQ-encoded pixels for analysis, then transforms back to AP0 linear at
    # MediaOut1. Round-trip is mathematically lossless (no tone mapping).
    # ------------------------------------------------------------------
    # Color management: color-managed projects (ACES or DaVinci YRGB Color
    # Managed) feed Fusion a scene-LINEAR image from MediaIn1 — AP0 linear for
    # ACES, the timeline gamut at linear gamma for DRCM — which renders
    # near-black on display and breaks Neat's Auto Profile. Wrap Neat with two
    # ColorSpaceTransform tools so it sees display-referred PQ pixels for
    # analysis, and round-trips back to the linear working space at MediaOut.
    # The wrap is enforced for HDR-managed projects — if we can't read the
    # settings or build the CSTs, we fail loudly rather than silently feeding
    # near-black linear into Neat.
    wrap_info: Dict[str, Any] = {"applied": False}
    skip_wrap = os.environ.get("AUTONEAT_NO_COLOR_WRAP") == "1"
    wrap_cfg: Optional[Dict[str, str]] = None
    if skip_wrap:
        wrap_info["skip_reason"] = "AUTONEAT_NO_COLOR_WRAP=1"
    elif project is None:
        wrap_info["skip_reason"] = "no current project (cannot read color settings)"
    else:
        mode = (project.GetSetting("colorScienceMode") or "").lower()
        nits_raw = project.GetSetting("hdrMasteringLuminanceMax") or "0"
        try:
            nits = int(float(nits_raw))
        except (TypeError, ValueError):
            nits = 0
        pq_gamma = _PQ_GAMMA_BY_NITS.get(nits)

        if mode in ("acescc", "acescct"):
            # ACES: Fusion composites in AP0 linear (scene-referred) → near-black.
            # Re-encode AP0 linear → Rec.2020 PQ for Neat, round-trip back at MediaOut.
            if not pq_gamma:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": (
                                f"ACES project ({mode}) but hdrMasteringLuminanceMax="
                                f"{nits_raw!r} is not in {sorted(_PQ_GAMMA_BY_NITS)}; "
                                "set the project's mastering luminance or pass "
                                "--no-color-wrap if the clip is already display-referred."
                            ),
                        }
                    ),
                    flush=True,
                )
                return 1
            wrap_cfg = {
                "in_cs": "ACES_COLORSPACE",
                "in_gamma": "LINEAR_GAMMA",
                "out_cs": "REC2020_COLORSPACE",
                "out_gamma": pq_gamma,
                "nits": str(nits),
                "mode": mode,
            }
        elif mode in ("davinciyrgbcolormanaged", "davinciyrgbcolormanagedv2"):
            # DRCM: Resolve linearises the image into Fusion, so Neat sees the
            # timeline gamut at LINEAR gamma (near-black). Re-encode that same
            # gamut Linear → PQ for analysis, round-trip back to linear at MediaOut.
            tl_gamut = (project.GetSetting("colorSpaceTimeline") or "").strip()
            cst_gamut = _DRCM_GAMUT_TO_CST.get(tl_gamut)
            if cst_gamut is None:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": (
                                f"DRCM project but timeline gamut {tl_gamut!r} has no "
                                f"Fusion CST color space (known: {sorted(_DRCM_GAMUT_TO_CST)}). "
                                "Add it to _DRCM_GAMUT_TO_CST or pass --no-color-wrap."
                            ),
                        }
                    ),
                    flush=True,
                )
                return 1
            if not pq_gamma:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": (
                                f"DRCM project but hdrMasteringLuminanceMax={nits_raw!r} "
                                f"is not in {sorted(_PQ_GAMMA_BY_NITS)}; set the project's "
                                "mastering luminance (HDR) or pass --no-color-wrap if the "
                                "clip is already display-referred."
                            ),
                        }
                    ),
                    flush=True,
                )
                return 1
            wrap_cfg = {
                "in_cs": cst_gamut,
                "in_gamma": "LINEAR_GAMMA",
                "out_cs": cst_gamut,
                "out_gamma": pq_gamma,
                "nits": str(nits),
                "mode": mode,
            }
        else:
            wrap_info["skip_reason"] = f"colorScienceMode={mode!r} (not color-managed)"

    def _ensure_scale(name: str, *, x: float, gain: float) -> Any:
        existing = find_tool(name)
        if existing is None:
            tool = comp.AddTool(SCALE_TOOL_ID, x, 0)
            if tool is None:
                raise RuntimeError(f"comp.AddTool({SCALE_TOOL_ID!r}) returned None for {name}")
            tool.SetAttrs({"TOOLS_Name": name})
        else:
            tool = existing
        tool.SetInput("Gain", gain)
        return tool

    def _ensure_cst(name: str, *, x: float, forward: bool) -> Any:
        existing = find_tool(name)
        if existing is None:
            tool = comp.AddTool(CST_TOOL_ID, x, 0)
            if tool is None:
                raise RuntimeError(f"comp.AddTool({CST_TOOL_ID!r}) returned None for {name}")
            tool.SetAttrs({"TOOLS_Name": name})
        else:
            tool = existing
        if wrap_cfg is None:
            return tool
        cfg = (
            (
                ("InputColorSpace", wrap_cfg["in_cs"]),
                ("InputGamma", wrap_cfg["in_gamma"]),
                ("OutputColorSpace", wrap_cfg["out_cs"]),
                ("OutputGamma", wrap_cfg["out_gamma"]),
            )
            if forward
            else (
                ("InputColorSpace", wrap_cfg["out_cs"]),
                ("InputGamma", wrap_cfg["out_gamma"]),
                ("OutputColorSpace", wrap_cfg["in_cs"]),
                ("OutputGamma", wrap_cfg["in_gamma"]),
            )
        )
        for key, value in cfg:
            tool.SetInput(key, value)
        # Tone mapping must stay off so the round-trip is lossless.
        tool.SetInput("ToneMapping", 0)
        # Gamut mapping compresses/clips out-of-gamut values and is not a
        # reversible Neat wrapper. Keep the wrap transfer-only.
        tool.SetInput("GamutMapping", 0)
        return tool

    wrap_scale = 1.0
    scale_down: Any = None
    scale_up: Any = None
    cst_in: Any = None
    cst_out: Any = None
    if wrap_cfg is not None:
        raw_scale = os.environ.get("AUTONEAT_COLOR_WRAP_SCALE", "0.125")
        try:
            wrap_scale = float(raw_scale)
        except (TypeError, ValueError):
            raise RuntimeError(f"Invalid AUTONEAT_COLOR_WRAP_SCALE={raw_scale!r}")
        if not (0 < wrap_scale <= 1):
            raise RuntimeError(f"AUTONEAT_COLOR_WRAP_SCALE must be >0 and <=1, got {wrap_scale!r}")
        scale_down = _ensure_scale(SCALE_DOWN_NAME, x=0.25, gain=wrap_scale)
        cst_in = _ensure_cst(CST_IN_NAME, x=0.75, forward=True)
        cst_out = _ensure_cst(CST_OUT_NAME, x=1.75, forward=False)
        scale_up = _ensure_scale(SCALE_UP_NAME, x=2.25, gain=1.0 / wrap_scale)

    media_in = find_tool("MediaIn1")
    media_out = find_tool("MediaOut1")
    if wrap_cfg is not None:
        # MediaIn1 → ScaleDown → CstIn → Neat → CstOut → ScaleUp → MediaOut1.
        #
        # Fusion's stock ColorSpaceTransform clips scene-linear values above
        # 1.0 when encoding Linear → PQ. HDR plate pulls routinely carry values
        # above 1.0 (e.g. 1.0 == 203 nits), so normalize internally before the
        # CST and restore the exact scale after the inverse CST. This gives Neat
        # display-referred pixels without baking a 100-nit plateau into output.
        if media_in is not None:
            connect(scale_down, "Input", media_in)
        connect(cst_in, "Input", scale_down)
        connect(neat, "Source", cst_in)
        connect(cst_out, "Input", neat)
        connect(scale_up, "Input", cst_out)
        if media_out is not None:
            connect(media_out, "Input", scale_up)
        wrap_info = {"applied": True, "scale": str(wrap_scale), **wrap_cfg}
    else:
        # Direct: MediaIn1 → Neat → MediaOut1
        if media_in is not None:
            connect(neat, "Source", media_in)
        if media_out is not None:
            connect(media_out, "Input", neat)

    comp.SetActiveTool(neat)

    # Do NOT open Neat's window from here. Triggering the OFX "Prepare Profile"
    # button via SetInput blocks this scripting call until the modal is
    # dismissed (the fusionscript call holds the GIL), and from a second
    # scripting session — while the parent owns the GUI — it racily fails to
    # open the window at all. Instead we leave the Neat node added + selected so
    # its Fusion Inspector shows the "Prepare Noise Profile" button, and the
    # batch command (parent) opens the window by clicking that button via OCR +
    # Quartz click, then drives Neat's visible UI adaptively. The helper returns
    # immediately, so there is no blocking call to wedge Resolve.
    print(
        json.dumps(
            {
                "ok": True,
                "item": item.GetName(),
                "tool": getattr(neat, "Name", "Reduce Noise v6"),
                "color_wrap": wrap_info,
                "media_range": media_range,
            }
        ),
        flush=True,
    )
    return 0


def _prepare_profile_helper_current() -> int:
    """Private subprocess helper: press Neat's Prepare Noise Profile OFX button.

    This call intentionally blocks while Neat's editor window is open; the
    parent process supervises and terminates this helper once the window appears.
    """
    try:
        from autoneat.resolve import connect_resolve_raw
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"Could not import Resolve helper: {exc}"}))
        return 1
    try:
        resolve = connect_resolve_raw()
        project = resolve.GetProjectManager().GetCurrentProject()
        timeline = project.GetCurrentTimeline() if project else None
        item = None
        target_track = os.environ.get("AUTONEAT_TARGET_TRACK")
        target_start = os.environ.get("AUTONEAT_TARGET_START")
        target_name = os.environ.get("AUTONEAT_TARGET_NAME")
        if timeline and target_track and target_start:
            for candidate in timeline.GetItemListInTrack("video", int(target_track)) or []:
                if int(candidate.GetStart()) != int(target_start):
                    continue
                if target_name and candidate.GetName() != target_name:
                    continue
                item = candidate
                break
            if item is None:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": (
                                f"No clip on video track {target_track} starts at frame {target_start}"
                                + (f" with name {target_name!r}" if target_name else "")
                            ),
                        }
                    )
                )
                return 1
            _set_timeline_to_item_midpoint(timeline, item)
        else:
            item = timeline.GetCurrentVideoItem() if timeline else None
        resolve.OpenPage("fusion")
        comp = _load_item_fusion_comp(item) if item is not None else None
        if comp is None:
            print(json.dumps({"ok": False, "error": "No current Fusion comp"}))
            return 1
        neat = None
        for tool in (comp.GetToolList(False) or {}).values():
            if getattr(tool, "ID", "") == NEAT_REG_ID:
                neat = tool
                break
        if neat is None:
            print(json.dumps({"ok": False, "error": "No Neat node in current comp"}))
            return 1
        result = neat.SetInput("Prepare Profile___", 1)
        print(json.dumps({"ok": True, "result": repr(result)}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


def _dump_neat_inputs_current() -> int:
    try:
        from autoneat.resolve import connect_resolve_raw
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"Could not import Resolve helper: {exc}"}))
        return 1
    try:
        resolve = connect_resolve_raw()
        project = resolve.GetProjectManager().GetCurrentProject()
        timeline = project.GetCurrentTimeline() if project else None
        item = timeline.GetCurrentVideoItem() if timeline else None
        if item is None:
            print(json.dumps({"ok": False, "error": "No current video item"}))
            return 1
        comp = _load_item_fusion_comp(item)
        if comp is None:
            print(json.dumps({"ok": False, "error": "No current Fusion comp"}))
            return 1
        neat = None
        for tool in (comp.GetToolList(False) or {}).values():
            if getattr(tool, "ID", "") == NEAT_REG_ID:
                neat = tool
                break
        if neat is None:
            print(json.dumps({"ok": False, "error": "No Neat node in current comp"}))
            return 1
        rows = []
        for key, inp in (neat.GetInputList() or {}).items():
            attrs: Dict[str, Any] = {}
            try:
                attrs = inp.GetAttrs() or {}
            except Exception:
                pass
            rows.append(
                {
                    "key": str(key),
                    "id": str(attrs.get("INPS_ID") or ""),
                    "name": str(attrs.get("INPS_Name") or ""),
                    "data_type": str(attrs.get("INPS_DataType") or ""),
                    "value": repr(neat.GetInput(key))[:200],
                }
            )
        print(json.dumps({"ok": True, "inputs": rows}, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="_neat_ui", description="Private helper for autoneat profile.")
    parser.add_argument("--_open-neat-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_prepare-profile-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_dump-neat-inputs", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._open_neat_helper:
        return _open_neat_helper_current()
    if args._prepare_profile_helper:
        return _prepare_profile_helper_current()
    if args._dump_neat_inputs:
        return _dump_neat_inputs_current()
    print("ERROR: _neat_ui is private; run `autoneat profile` instead.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
