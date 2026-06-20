"""macOS mouse event helpers."""

from __future__ import annotations

import time


def click_at(x: float, y: float) -> None:
    """Move to ``x,y`` and issue a left click through Quartz events."""
    try:
        from Quartz import (  # type: ignore[import-not-found]
            CGEventCreateMouseEvent,
            CGEventPost,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
            kCGEventMouseMoved,
            kCGHIDEventTap,
            kCGMouseButtonLeft,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Quartz mouse automation is unavailable. Install autoneat with its "
            "macOS dependencies, then grant the running terminal Accessibility "
            "permission in System Settings."
        ) from exc

    point = (float(x), float(y))
    for event_type in (kCGEventMouseMoved, kCGEventLeftMouseDown, kCGEventLeftMouseUp):
        event = CGEventCreateMouseEvent(None, event_type, point, kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, event)
        time.sleep(0.04)
