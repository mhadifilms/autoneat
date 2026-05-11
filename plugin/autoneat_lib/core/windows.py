"""macOS window enumeration via JXA (System Events accessibility API).

Used by the UI driver to find the Neat plugin window and modal dialogs
by name and to compute screen-relative click points.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from autoneat_lib.core.subprocess_utils import run_proc

RESOLVE_PROCESS = "Resolve"
RESOLVE_APP = "DaVinci Resolve"


def _run_jxa(script: str, *, timeout: float = 8.0) -> Dict[str, Any]:
    proc = run_proc(["osascript", "-l", "JavaScript", "-e", script], timeout=timeout)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or proc.stdout.strip() or "osascript failed"}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"osascript returned non-JSON: {proc.stdout.strip()}"}
    return data if isinstance(data, dict) else {"ok": False, "error": "osascript returned non-object JSON"}


def activate_resolve(*, settle: float = 0.35) -> None:
    """Bring DaVinci Resolve to the front. ``open -a`` is the only path here.

    Adding ``osascript ... to activate`` is a belt-and-suspenders approach
    that hangs whenever Resolve's AppleScript runloop is busy (e.g. while
    Neat is committing a profile), so it's intentionally omitted.
    """
    try:
        run_proc(["open", "-a", RESOLVE_APP], timeout=10)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(settle)


def list_resolve_windows(*, activate: bool = False) -> List[Dict[str, Any]]:
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


def main_resolve_window(windows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return max(windows, key=_window_area, default=None)


def find_neat_window(windows: Optional[Sequence[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    rows = list(windows) if windows is not None else list_resolve_windows(activate=True)
    for window in rows:
        if "neat video" in str(window.get("name") or "").lower():
            return window
    return None


def find_modal_window(windows: Optional[Sequence[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    rows = list(windows) if windows is not None else list_resolve_windows(activate=True)
    main = main_resolve_window(rows)
    candidates = []
    for window in rows:
        if window is main:
            continue
        size = window.get("size") or [0, 0]
        if len(size) >= 2 and size[0] >= 180 and size[1] >= 60:
            candidates.append(window)
    return max(candidates, key=_window_area, default=None)


def raise_window(window: Dict[str, Any]) -> None:
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


def window_point(window: Dict[str, Any], x: float, y: float) -> Tuple[float, float]:
    """Convert a window-relative point to a screen-absolute point.

    Negative ``x``/``y`` are interpreted relative to the window's right/bottom
    edge (handy for buttons in the bottom-right corner).
    """
    position = window.get("position") or [0, 0]
    size = window.get("size") or [0, 0]
    if x < 0:
        x = float(size[0]) + x
    if y < 0:
        y = float(size[1]) + y
    return (float(position[0]) + x, float(position[1]) + y)
