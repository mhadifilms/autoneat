"""Self-calibrating template matching for Neat Video UI controls.

OCR is a probabilistic sensor — it mis-reads Neat's grey-on-grey Qt buttons and
its dialogs, which is the root cause of most misclicks in ``autoneat profile``. This
module adds a *deterministic* visual sensor on top: OpenCV template matching
against reference crops of each control.

The reference crops are **learned**, not shipped. When a control is located by
OCR during a clip that then *succeeds* (Auto Profile → Apply → window closed),
the located region is saved as that control's template. The next clip matches
that template directly — fast and near-deterministic — and only falls back to
OCR if the match is weak. So the system self-calibrates on the first clip and
runs on template matching for the rest of the batch, with no manual capture and
no pre-shipped assets.

Everything here is best-effort. If OpenCV is unavailable, no template exists, or
a match falls below threshold, the public functions return ``None`` and the
caller falls back to the existing OCR locate. The worst case is exactly the
OCR-only behavior; the best case is deterministic clicks after the first
success. Templates are keyed by screen resolution because ``matchTemplate`` is
not scale-invariant — within one session the resolution is constant, so an
exact-scale template is both correct and maximally reliable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

# Half-extent (in screen pixels) of the box cropped around a located control to
# form its template. Wide enough to enclose a button label + chrome, tight
# enough not to bleed into neighbouring controls.
_TEMPLATE_HALF_W = 95
_TEMPLATE_HALF_H = 28

# Minimum normalized correlation (TM_CCOEFF_NORMED, range -1..1) to accept a
# match. 0.88 is conservative: Neat's opaque Qt chrome reproduces near-exactly
# frame to frame, so true matches score >0.95; this rejects look-alikes.
_DEFAULT_THRESHOLD = 0.88


def available() -> bool:
    """True if OpenCV (and numpy) can be imported for template matching."""
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except Exception:
        return False
    return True


# Operators can relocate the template cache (e.g. to a fast local disk or a
# clean dir to force re-bootstrapping) and tests point it at a tmp dir.
_TEMPLATE_DIR_ENV = "AUTONEAT_TEMPLATE_DIR"


def _templates_root() -> Path:
    override = os.environ.get(_TEMPLATE_DIR_ENV)
    base = (
        Path(override)
        if override
        else Path.home() / ".cache" / "autoneat" / "neat_ui" / "templates"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def _template_path(label: str, screen_w: int, screen_h: int) -> Path:
    # Keyed by capture resolution. ``matchTemplate`` is not scale-invariant, and
    # the standard remedy (a multi-scale pyramid sweep) is deliberately avoided
    # here: within one session the screen resolution is constant, so the control
    # always renders at exactly the template's scale — an exact-scale single
    # match is both more precise (no off-scale center drift) and cheaper than a
    # sweep. A DPI/resolution change rotates this key, so a fresh template is
    # learned for the new geometry instead of mis-scaling an old one.
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    return _templates_root() / f"{safe}@{int(screen_w)}x{int(screen_h)}.png"


def has_template(label: str, screen_w: int, screen_h: int) -> bool:
    try:
        return _template_path(label, screen_w, screen_h).is_file()
    except Exception:
        return False


def frame_diff(frame_a: Path, frame_b: Path) -> float:
    """Mean absolute grayscale difference between two screenshots, normalized to
    ``0.0`` (identical) .. ``1.0``.

    Used for *temporal stability gating* — never OCR/template-match or click a
    frame captured mid-animation (a window sliding open, a dialog fading in, a
    profile spinner), which is a real source of garbled reads. Returns ``1.0``
    (treated as "not stable") on any error or shape mismatch so the caller waits
    rather than acting on an uncertain frame.
    """
    try:
        import cv2

        img_a = cv2.imread(str(frame_a), cv2.IMREAD_GRAYSCALE)
        img_b = cv2.imread(str(frame_b), cv2.IMREAD_GRAYSCALE)
        if img_a is None or img_b is None or img_a.shape != img_b.shape:
            return 1.0
        return float(cv2.absdiff(img_a, img_b).mean()) / 255.0
    except Exception:
        return 1.0


def delete_template(label: str, screen_w: int, screen_h: int) -> bool:
    """Remove a learned template so it is re-bootstrapped via OCR next time.

    Used to distrust a template after a clip that relied on it failed — a cheap
    self-heal against a rare poisoned template. Best-effort → ``False`` if there
    was nothing to delete or the unlink failed.
    """
    try:
        path = _template_path(label, screen_w, screen_h)
        if path.is_file():
            path.unlink()
            return True
    except Exception:
        pass
    return False


def image_size(frame_path: Path) -> Optional[Tuple[int, int]]:
    """Return ``(width, height)`` of an image on disk, or ``None`` on failure."""
    try:
        import cv2

        img = cv2.imread(str(frame_path))
        if img is None:
            return None
        height, width = img.shape[:2]
        return int(width), int(height)
    except Exception:
        return None


def save_template(
    label: str,
    frame_path: Path,
    point: Tuple[float, float],
    screen_w: int,
    screen_h: int,
) -> Optional[Path]:
    """Crop a box around ``point`` from ``frame_path`` and save it as the
    template for ``label`` at this resolution. Best-effort → ``None`` on any
    failure. Overwrites an existing template so it always reflects the most
    recent verified appearance of the control.
    """
    try:
        import cv2

        img = cv2.imread(str(frame_path))
        if img is None:
            return None
        height, width = img.shape[:2]
        cx = int(round(point[0]))
        cy = int(round(point[1]))
        x0 = max(0, cx - _TEMPLATE_HALF_W)
        x1 = min(width, cx + _TEMPLATE_HALF_W)
        y0 = max(0, cy - _TEMPLATE_HALF_H)
        y1 = min(height, cy + _TEMPLATE_HALF_H)
        if (x1 - x0) < 8 or (y1 - y0) < 8:
            return None
        crop = img[y0:y1, x0:x1]
        out = _template_path(label, screen_w, screen_h)
        if not cv2.imwrite(str(out), crop):
            return None
        return out
    except Exception:
        return None


def match_template(
    label: str,
    frame_path: Path,
    screen_w: int,
    screen_h: int,
    *,
    region: Optional[Tuple[int, int, int, int]] = None,
    threshold: float = _DEFAULT_THRESHOLD,
) -> Optional[Tuple[float, float, float]]:
    """Return ``(x, y, score)`` of the best template match in screen pixels, or
    ``None``.

    ``region`` (``x, y, w, h`` in screen px) scopes the search to the control's
    expected area — fewer false positives and faster. Best-effort: a missing
    template, an OpenCV error, or a sub-threshold score all return ``None`` so
    the caller falls back to OCR.
    """
    try:
        import cv2

        tpl_path = _template_path(label, screen_w, screen_h)
        if not tpl_path.is_file():
            return None
        # Match in grayscale: the consensus default for UI template matching —
        # ~3x faster (1 channel vs 3) and more robust to subtle anti-aliasing /
        # sub-pixel colour shifts in Qt chrome, while TM_CCOEFF_NORMED already
        # handles brightness/contrast changes.
        screen = cv2.imread(str(frame_path), cv2.IMREAD_GRAYSCALE)
        template = cv2.imread(str(tpl_path), cv2.IMREAD_GRAYSCALE)
        if screen is None or template is None:
            return None
        full_h, full_w = screen.shape[:2]

        offset_x = 0
        offset_y = 0
        search = screen
        if region is not None:
            rx, ry, rw, rh = (int(v) for v in region)
            rx = max(0, min(rx, full_w - 1))
            ry = max(0, min(ry, full_h - 1))
            rw = max(1, min(rw, full_w - rx))
            rh = max(1, min(rh, full_h - ry))
            search = screen[ry : ry + rh, rx : rx + rw]
            offset_x, offset_y = rx, ry

        tpl_h, tpl_w = template.shape[:2]
        if search.shape[0] < tpl_h or search.shape[1] < tpl_w:
            return None

        result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)
        if max_val < threshold:
            return None
        match_x, match_y = max_loc
        center_x = offset_x + match_x + tpl_w / 2.0
        center_y = offset_y + match_y + tpl_h / 2.0
        return (float(center_x), float(center_y), float(max_val))
    except Exception:
        return None
