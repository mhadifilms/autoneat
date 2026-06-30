"""``autoneat profile`` — batch Neat Video Auto Profile on Resolve timeline clips.

This is intentionally separate from ``grain regrain`` so the current Neat
profiling path can later be replaced by an in-house degrain plugin without
folding the commands together.
Neat Video exposes no public profiling API, so this leaf drives Neat's Resolve
OFX **UI**: the Fusion API adds and selects the plugin node, then OCR + bounded
``Quartz click`` automation presses Neat's Qt controls (Prepare Profile → Auto
Profile → Apply).

Why the architecture looks the way it does — the hard-won invariants:

1. **A Neat window left open WEDGES Resolve.** While Neat's OFX editor (or any
   of its modal dialogs) is on screen, a *second* Resolve scripting session —
   the per-clip node-add helper — blocks indefinitely, and Resolve's whole
   scripting interface eventually freezes. The single most important rule in
   this file is therefore: **every clip closes its Neat window before the next
   clip starts, success or failure.** ``_process_clip`` enforces this with a
   ``try/finally`` that always calls :meth:`NeatDriver.abort`.

2. **The node-add helper must never block.** It adds + selects the Neat node
   and exits immediately (no blocking ``SetInput`` that would open the modal
   and hold the GIL). The visible UI is driven from *this* (parent) process,
   which owns the GUI connection.

3. **Only the parent can switch the Fusion page.** A second scripting session's
   ``OpenPage`` is ignored while the parent holds the connection, so the parent
   calls ``OpenPage("fusion")`` for each clip before adding the node.

4. **Neat appears in many transient states; drive adaptively.** Rather than a
   fixed open→profile→apply march that fails the moment the racy window isn't
   in the exact expected state, :class:`NeatDriver` polls the screen each tick
   and reacts to whatever is showing (demo splash, "select a frame" info modal,
   "area is small" confirm modal, unprofiled/profiled editor). Progress is
   tracked by *state change*; a state that sits unchanged past its budget fails
   fast — and the ``finally`` still closes the window.

5. **Button locates are non-fatal while a profile is building.** After Auto
   Profile is clicked the button text disappears while Neat analyses, so a
   failed re-locate means "still building", not an error.

6. **GUI session required.** screencapture/OCR/Quartz click need a macOS Aqua
   session. Over SSH we re-exec inside one reused Terminal window in the
   logged-in desktop session and mirror its log back to the dispatcher.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from autoneat import _neat_ui as neat_ui
from autoneat import _neat_vision as neat_vision

# States in which a Neat window or modal is on screen. While any of these is
# showing, Resolve scripting must NOT be touched (it wedges) and the batch must
# NOT advance to the next clip. ``inspector-prepare`` and ``unknown`` are
# deliberately excluded: they mean the Neat editor window is not open.
_WINDOW_OPEN_STATES = frozenset(
    {
        "editor-unprofiled",
        "editor-profiled",
        "editor",
        "information-dialog",
        "confirm-small-area",
        "confirm-build-profile",
        "demo-splash",
        "preparing-input",
    }
)

# Standalone dialogs that can be on screen WITHOUT a Neat editor window behind
# them (so AX "no editor window" is not sufficient proof of a clean state — a
# modal could still be up). Everything else in _WINDOW_OPEN_STATES is an editor
# state, for which the Neat editor window IS the thing being checked.
_ABORT_MODAL_STATES = frozenset(
    {
        "information-dialog",
        "confirm-small-area",
        "confirm-build-profile",
        "demo-splash",
    }
)

_EDITOR_STATES = frozenset({"editor-unprofiled", "editor-profiled", "editor"})


def _load_sidecar(path: Path) -> Optional[Dict[str, Any]]:
    """Read a previous run's sidecar if it exists. Returns None on any failure."""
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _progress_path(project_name: Optional[str], timeline_name: Optional[str]) -> Path:
    """Durable, per-(project, timeline) progress file.

    Resolution order:
      1. ``AUTONEAT_RESULTS_JSON`` env var — explicit override (verbatim path).
      2. ``<cache>/progress/<project>__<timeline>.json`` — the default. Keyed by
         project+timeline so two timelines never clobber each other, and stored
         under the persistent cache (NOT ``/tmp``, which clears on reboot and was
         shared across every run — the source of the "it wiped my completed
         clips" problem).
      3. ``<cache>/progress/_unkeyed.json`` — only if project/timeline are
         unknown (should not happen in normal runs).

    The file is ALWAYS merged into, never overwritten wholesale, so a completed
    clip's record survives every subsequent run (see ``_write_sidecar``). The
    env override is intentionally NOT propagated through the GUI-Terminal
    relaunch, which is why the default must be self-describing from the
    project/timeline rather than an env var the orchestrator sets."""
    override = os.environ.get("AUTONEAT_RESULTS_JSON")
    if override:
        return Path(override)
    base = neat_ui._cache_base() / "progress"
    base.mkdir(parents=True, exist_ok=True)

    def _slug(value: Optional[str]) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in (value or "")).strip("_")

    proj = _slug(project_name)
    tl = _slug(timeline_name)
    if proj or tl:
        return base / f"{proj or 'project'}__{tl or 'timeline'}.json"
    return base / "_unkeyed.json"


def _recall_target_from_progress() -> Tuple[Optional[str], Optional[str]]:
    """Best-effort (project, timeline) of the most recent run.

    Progress files are named ``<project>__<timeline>.json`` (see
    ``_progress_path``), so the most-recently-modified one tells us which
    project/timeline this node was last driving. Used by the startup
    self-heal to reopen the right project/timeline when Resolve comes up cold
    (no project) or wedged — without needing the operator to pass a target the
    ``autoneat profile`` CLI otherwise infers from whatever timeline is already open.
    """
    try:
        base = neat_ui._cache_base() / "progress"
        files = sorted(
            (p for p in base.glob("*.json") if not p.stem.startswith("_")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return None, None
    for f in files:
        if "__" in f.stem:
            proj, tl = f.stem.split("__", 1)
            return (proj or None), (tl or None)
    return None, None


# ---------------------------------------------------------------------------
# GUI-session relaunch (SSH support)
# ---------------------------------------------------------------------------
# autoneat profile drives Neat Video's OFX UI via screencapture + Quartz click, both of
# which require a macOS Aqua (GUI) session. Over SSH (or from a LaunchDaemon)
# there is no window server: screencapture returns black frames and clicks are
# dropped. When we are NOT in an Aqua session, re-exec the exact same command
# inside a Terminal window in the logged-in GUI session and mirror that
# window's log back to our stdout so the dispatcher still sees live progress.


def _display_capturable() -> bool:
    """Ground-truth check that this process can screen-capture the desktop.

    Neat's UI automation needs real window-server access. The previous
    ``launchctl managername`` heuristic gave false positives under the render
    agent — it reports a GUI-ish session, but the dispatched job has no
    window-server connection, so ``screencapture`` fails with "could not create
    image from display" on the very first poll. Rather than trust a heuristic
    that can't see TCC/Screen-Recording or console-session attachment, we test
    the actual capability with a throwaway capture. A success means we can drive
    Neat here; a failure means we must relaunch into a Terminal in the console
    user's Aqua session (which does have display access).
    """
    probe: Optional[Path] = None
    try:
        fd, name = tempfile.mkstemp(suffix=".png", prefix="neat-capprobe-")
        os.close(fd)
        probe = Path(name)
        proc = subprocess.run(
            ["screencapture", "-x", str(probe)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode == 0 and probe.exists() and probe.stat().st_size > 0
    except Exception:
        return False
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass


def _gui_run_dir() -> Path:
    base = Path.home() / ".cache" / "autoneat" / "neat_ui" / "gui-runs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _relaunch_in_gui_terminal(neat_args: Sequence[str]) -> int:
    """Re-run ``autoneat profile`` in a GUI Terminal and mirror its log to stdout.

    Writes a self-contained ``.command`` script that re-invokes ``autoneat``
    with the same args (re-entry guarded by ``AUTONEAT_IN_GUI``), launches it
    in the console user's Aqua session inside ONE reused Terminal window, then
    tails the script's log until it writes a sidecar ``.done`` file containing
    the exit code. Returns that exit code.
    """
    work_dir = Path.cwd()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = _gui_run_dir() / f"neat-{stamp}"
    log_path = base.with_suffix(".log")
    done_path = base.with_suffix(".done")
    script_path = base.with_suffix(".command")

    inner = " ".join(
        shlex.quote(s) for s in [sys.executable, "-m", "autoneat", "profile", *neat_args]
    )
    # The relaunched run writes output ONLY to a log file (which the orchestrator
    # tails) — never to the visible Terminal — because the run uses full-screen
    # screencapture + OCR and would otherwise read its own log text off the
    # screen (the log contains "prepare", "profile", "neat", "ok", …), mis-detect
    # Neat's state, and Quartz click its own output. One window is opened per
    # invocation; the shell exits cleanly when done.
    script = (
        "#!/bin/bash\n"
        f"cd {shlex.quote(str(work_dir))}\n"
        "export AUTONEAT_IN_GUI=1\n"
        # Bring Resolve to the front from inside the GUI session (the reliable
        # `open -a` path — DaVinci Resolve does not support AppleScript
        # `activate`, which errors -609). Best-effort; per-click activation in
        # the run handles focus anyway.
        '/usr/bin/open -a "DaVinci Resolve" >/dev/null 2>&1\n'
        f"{inner} > {shlex.quote(str(log_path))} 2>&1\n"
        f'echo "$?" > {shlex.quote(str(done_path))}\n'
    )
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o755)
    log_path.touch()

    print(
        "autoneat profile: no GUI (Aqua) session — relaunching in a Terminal window "
        "on this node so Neat's UI is screen-capturable.",
        flush=True,
    )
    print(f"  script: {script_path}", flush=True)
    print(f"  log:    {log_path}", flush=True)
    print("  --- mirrored GUI Terminal output ---", flush=True)

    # Launch the script in Terminal via LaunchServices (`open`), NOT Apple
    # Events. Driving Terminal with osascript from the farm worker (launchd)
    # context hangs on a TCC "control Terminal" prompt that can't be answered in
    # a headless context and times out (-1712). `open` bridges to the console
    # user's GUI session the same way the `open -a "DaVinci Resolve"` inside the
    # script does — no Automation permission required.
    launch = subprocess.run(
        ["open", "-a", "Terminal", str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if launch.returncode != 0:
        msg = launch.stderr.strip() or launch.stdout.strip() or "open failed"
        print(f"ERROR: could not launch the GUI Terminal: {msg}", file=sys.stderr)
        print(
            "  A user must be logged into the desktop on this machine for "
            "autoneat profile to drive Neat Video's UI.",
            file=sys.stderr,
        )
        return 1

    # Mirror the GUI Terminal's log to our stdout until it finishes.
    guard_seconds = 6 * 3600  # absolute safety cap, exceeds the longest batch
    start = time.time()
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        while True:
            chunk = fh.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            if done_path.exists():
                while True:
                    tail = fh.read()
                    if not tail:
                        break
                    sys.stdout.write(tail)
                    sys.stdout.flush()
                break
            if time.time() - start > guard_seconds:
                print(
                    "\nERROR: GUI Terminal run exceeded the 6h safety cap; "
                    f"abandoning log mirror. Check {log_path} on the node.",
                    file=sys.stderr,
                )
                return 1
            time.sleep(0.5)

    try:
        return int((done_path.read_text(encoding="utf-8").strip() or "1"))
    except Exception:
        return 1


class StepRecorder:
    """Collect step descriptors for the JSON sidecar AND stream them live."""

    def __init__(self, *, prefix: str = "  ", echo: bool = True):
        self.prefix = prefix
        self.echo = echo
        self.steps: List[str] = []
        self.started_at = time.time()

    def add(self, step: str) -> None:
        self.steps.append(step)
        if self.echo:
            elapsed = time.time() - self.started_at
            print(f"{self.prefix}[{elapsed:6.1f}s] {step}", flush=True)

    def extend(self, steps: Iterable[str]) -> None:
        for step in steps:
            self.add(step)

    def elapsed(self) -> float:
        return time.time() - self.started_at


# ---------------------------------------------------------------------------
# Resolve / timeline / clip helpers
# ---------------------------------------------------------------------------


def _frame_to_timecode(frame: int, timeline: Any) -> str:
    fps = round(float(timeline.GetSetting("timelineFrameRate")))
    start_tc = timeline.GetStartTimecode()
    parts = start_tc.split(":")
    start_frame = (
        int(parts[0]) * 3600 * fps + int(parts[1]) * 60 * fps + int(parts[2]) * fps + int(parts[3])
    )
    target = start_frame + (frame - int(timeline.GetStartFrame()))
    h = int(target // (3600 * fps))
    rem = target - h * 3600 * fps
    m = int(rem // (60 * fps))
    rem -= m * 60 * fps
    s = int(rem // fps)
    f = int(rem - s * fps)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def _connect_resolve() -> Any:
    """Return the raw Resolve scripting handle via ``dvr``.

    Never imports ``DaVinciResolveScript`` directly — it connects through
    ``dvr`` (local-only, no auto-launch) and returns the raw fusionscript
    handle, which the Neat OFX UI automation needs to iterate Resolve's
    timeline clips and OFX nodes.
    """
    try:
        from autoneat.resolve import connect_resolve_raw
    except Exception as exc:
        raise RuntimeError(f"Could not import Resolve connection helper: {exc}") from exc

    try:
        return connect_resolve_raw(auto_launch=False)
    except Exception as exc:
        raise RuntimeError(f"Could not connect to local DaVinci Resolve via dvr: {exc}") from exc


def _current_timeline(connect_timeout: float = 20.0, attempts: int = 3) -> Tuple[Any, Any]:
    """Connect and return ``(resolve, current_timeline)``, retrying briefly.

    A healthy Resolve can be slow to answer the first scripting call right after
    launch, so we retry a few times before declaring it unusable — otherwise the
    startup self-heal needlessly force-quits a perfectly good Resolve.
    """
    last_err = "could not connect to Resolve"
    for _ in range(max(1, attempts)):
        finished, resolve = _call_with_timeout(_connect_resolve, connect_timeout)
        if not finished:
            last_err = f"Resolve connect timed out after {connect_timeout:.0f}s"
        elif resolve is None:
            last_err = "could not connect to Resolve"
        else:
            project = resolve.GetProjectManager().GetCurrentProject()
            timeline = project.GetCurrentTimeline() if project else None
            if timeline is not None:
                return resolve, timeline
            last_err = "project open but no current timeline" if project else "no current project"
        time.sleep(2.0)
    raise RuntimeError(last_err)


# ---------------------------------------------------------------------------
# Frozen-Resolve recovery (auto force-quit + relaunch + reopen)
# ---------------------------------------------------------------------------
# Neat's OFX UI can wedge Resolve hard enough that BOTH the scripting interface
# and the GUI stop responding — a clip's ``preparing-input`` never advances and
# any subsequent scripting call (OpenPage / SetCurrentTimecode) blocks forever.
# The per-clip watchdog below detects that as a wall-clock hang, force-quits
# Resolve, relaunches it, reopens the same project/timeline, and retries the
# clip. A project save after every successful clip means a kill loses at most
# the in-flight clip (already retried).


def _call_with_timeout(fn: Any, timeout: float) -> Tuple[bool, Any]:
    """Run ``fn()`` in a daemon thread; return ``(finished, value)``.

    ``finished`` is False if it did not complete within ``timeout`` (the thread
    is abandoned — used only for calls we expect to either return fast or block
    on a frozen Resolve, which a pending restart will unblock). A raised
    exception counts as finished with value ``None``.
    """
    box: Dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 — surfaced as finished/None
            box["error"] = exc

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return False, None
    return True, box.get("value")


def _resolve_running() -> bool:
    try:
        from autoneat.resolve import resolve_running

        return bool(resolve_running())
    except Exception:
        # Fall back to a direct pgrep so recovery never depends on the import.
        try:
            return (
                subprocess.run(
                    ["pgrep", "-x", "Resolve"], capture_output=True, timeout=5
                ).returncode
                == 0
            )
        except Exception:
            return False


def _force_quit_resolve(timeout: float = 25.0) -> bool:
    """SIGKILL the Resolve GUI and wait for the process to disappear.

    A frozen Resolve won't service SIGTERM (and TERM can raise a blocking
    "save before quit" modal), so we kill -9 outright. Safe because the batch
    saves the project after every good clip — a kill loses at most the clip
    currently in flight, which the watchdog retries.
    """
    subprocess.run(["pkill", "-9", "-x", "Resolve"], capture_output=True)
    end = time.time() + timeout
    while time.time() < end:
        if not _resolve_running():
            return True
        time.sleep(1.0)
    return not _resolve_running()


def _launch_resolve() -> None:
    subprocess.run(["open", "-a", "DaVinci Resolve"], capture_output=True, timeout=30)


def _dump_neat_inputs() -> int:
    helper = Path(neat_ui.__file__).resolve()
    proc = subprocess.run(
        [sys.executable, str(helper), "--_dump-neat-inputs"],
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return int(proc.returncode)


def _find_timeline_by_name(project: Any, name: str) -> Optional[Any]:
    if not name:
        return None
    try:
        count = int(project.GetTimelineCount() or 0)
    except Exception:
        return None
    for i in range(1, count + 1):
        try:
            tl = project.GetTimelineByIndex(i)
            if tl and (tl.GetName() == name or tl.GetName().endswith(name)):
                return tl
        except Exception:
            continue
    return None


def _load_project_anywhere(pm: Any, name: str, depth: int = 0) -> bool:
    """Load ``name`` from the current folder or any descendant folder.

    Resolve usually restores the last project on launch, but if it comes up at
    the Project Manager (or in the wrong folder) we walk the project-library
    folder tree to find and open it. Bounded depth so a deep library can't spin.
    """
    if depth == 0:
        try:
            if pm.LoadProject(name):
                return True
        except Exception:
            pass
        try:
            pm.GotoRootFolder()
        except Exception:
            pass
    if depth > 4:
        return False
    try:
        if name in (pm.GetProjectListInCurrentFolder() or []):
            if pm.LoadProject(name):
                return True
    except Exception:
        pass
    try:
        folders = list(pm.GetFolderListInCurrentFolder() or [])
    except Exception:
        folders = []
    for folder in folders:
        try:
            if pm.OpenFolder(folder):
                if _load_project_anywhere(pm, name, depth + 1):
                    return True
                pm.GotoParentFolder()
        except Exception:
            continue
    return False


def _wait_for_resolve_ready(
    project_name: Optional[str], timeline_name: Optional[str], timeout: float
) -> Tuple[Any, Any]:
    """Poll until Resolve is back, the right project is open, and a timeline set.

    Returns fresh ``(resolve, timeline)`` handles. Raises ``RuntimeError`` if
    Resolve doesn't become usable within ``timeout``.
    """
    deadline = time.time() + timeout
    last_err: Optional[str] = None
    # A blocking startup modal (Software Update "Skip | Download", a
    # restore-projects prompt, etc.) appears on a fresh Resolve launch and makes
    # the scripting PM navigation a no-op — GetCurrentProject stays None and
    # OpenFolder returns None, so the project never loads and we'd burn the whole
    # timeout. These Cocoa dialogs have no Escape binding, so we OCR-click a safe
    # dismiss button. This runs in the GUI session during a real batch, so
    # Quartz click works; it's a harmless no-op if no modal is up.
    modal_dismiss_left = 6
    while time.time() < deadline:
        try:
            finished, resolve = _call_with_timeout(_connect_resolve, 6.0)
            if not finished:
                last_err = "Resolve connect timed out after 6s"
                time.sleep(3.0)
                continue
            if resolve is None:
                last_err = "could not connect to Resolve"
                time.sleep(3.0)
                continue
            pm = resolve.GetProjectManager()
            project = pm.GetCurrentProject()
            wrong = bool(project_name and project is not None and project.GetName() != project_name)
            if project is None or wrong:
                if modal_dismiss_left > 0:
                    try:
                        if neat_ui.dismiss_blocking_dialog():
                            print("  startup: dismissed a blocking Resolve dialog", flush=True)
                    except Exception:
                        pass
                    modal_dismiss_left -= 1
                    time.sleep(1.0)
                if wrong:
                    try:
                        pm.CloseProject(project)
                    except Exception:
                        pass
                if project_name:
                    _load_project_anywhere(pm, project_name)
                last_err = (
                    "no current project yet"
                    if project is None
                    else f"wrong project open ({project.GetName()})"
                )
                time.sleep(3.0)
                continue
            if timeline_name:
                tl = _find_timeline_by_name(project, timeline_name)
                if tl is not None:
                    try:
                        project.SetCurrentTimeline(tl)
                    except Exception:
                        pass
            timeline = project.GetCurrentTimeline()
            if timeline is not None:
                return resolve, timeline
            last_err = "project open but no current timeline"
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
        time.sleep(3.0)
    raise RuntimeError(f"Resolve did not become ready within {timeout:.0f}s (last: {last_err})")


def _restart_resolve(
    project_name: Optional[str], timeline_name: Optional[str], cfg: "DriveConfig"
) -> Tuple[Any, Any]:
    """Force-quit + relaunch Resolve, then reopen the project/timeline."""
    _force_quit_resolve(timeout=cfg.restart_grace)
    time.sleep(3.0)
    _launch_resolve()
    return _wait_for_resolve_ready(project_name, timeline_name, cfg.restart_launch_timeout)


def _save_project(resolve: Any) -> bool:
    """Best-effort project save, bounded so a wedged save can't hang the batch."""
    finished, value = _call_with_timeout(lambda: resolve.GetProjectManager().SaveProject(), 30.0)
    return bool(finished and value)


def _establish_timeline(
    cfg: "DriveConfig",
    project_hint: Optional[str] = None,
    timeline_hint: Optional[str] = None,
) -> Tuple[Any, Any]:
    """Connect to Resolve at startup, self-healing a not-ready/wedged/cold state.

    The happy path is simply "use the timeline that's already open". But Resolve
    can be in any of three bad states when the batch starts — especially right
    after a previous run crashed:

    * **Wedged** — a leftover Neat OFX window from a crashed run blocks the
      scripting socket, so the connect times out.
    * **Cold** — Resolve was just (re)launched and has no project open yet, or is
      still booting and not yet scriptable.
    * **Wrong/empty** — Resolve is up at the Project Manager with no timeline.

    All three are recoverable with the SAME machinery the freeze watchdog uses:
    force-quit (which clears a wedging Neat window), relaunch, and wait — a clean
    relaunch restores the last project/timeline, and if it doesn't we reopen the
    remembered target explicitly. Doing this here means the operator never has to
    babysit a cold/wedged Resolve before resuming a batch.
    """
    try:
        return _current_timeline()
    except Exception as exc:  # noqa: BLE001
        if not cfg.auto_restart:
            raise
        # Prefer the caller-provided target (the project/timeline this run was
        # explicitly asked to drive). Only fall back to the most-recent progress
        # file when no hint was given — never reopen some unrelated project a
        # previous run happened to leave behind.
        proj, tl = (project_hint, timeline_hint)
        if not proj and not tl:
            proj, tl = _recall_target_from_progress()
        print(
            f"  startup: Resolve not usable yet ({exc}); self-healing — "
            f"force-quit + relaunch"
            + (
                f", reopening project={proj!r} timeline={tl!r}…"
                if (proj or tl)
                else " (restoring last session)…"
            ),
            flush=True,
        )
        return _restart_resolve(proj, tl, cfg)


def _timeline_clips(timeline: Any, *, track: int, all_tracks: bool) -> List[Any]:
    clips: List[Any] = []
    if all_tracks:
        count = int(timeline.GetTrackCount("video") or 0)
        tracks = range(1, count + 1)
    else:
        tracks = range(track, track + 1)

    for idx in tracks:
        for item in timeline.GetItemListInTrack("video", idx) or []:
            clips.append(item)
    clips.sort(key=lambda clip: (int(clip.GetStart()), clip.GetName() or ""))
    return clips


def _clip_name(clip: Any) -> str:
    try:
        return clip.GetName() or "<unnamed>"
    except Exception:
        return "<unnamed>"


def _shot_id_from_name(name: str) -> str:
    """Best-effort short ID for a timeline clip name.

    `MyShow_0000590.[00001001-00001066].exr` → `MyShow_0000590`
    `MYSHOW_101_054_idcritical_v002.mov`              → `MYSHOW_101_054_idcritical_v002`
    Falls back to the raw name if no obvious suffix to strip.
    """
    head = name.split(".[", 1)[0]
    head = head.rsplit(".", 1)[0] if "." in head else head
    return head or name


def _normalize_shot_id(shot_id: str) -> str:
    """Match the convention in lib/common/shot_utils.py — strip leading zeros."""
    s = str(shot_id or "").strip().lower()
    if not s:
        return ""
    stripped = s.lstrip("0")
    return stripped or "0"


def _shot_id_tokens(name: str) -> List[str]:
    """Extract candidate shot-id tokens from a clip name for matching.

    For `MyShow_0000590` we yield `myshow_0000590`, `0000590`,
    and `590` — so any of `-s MyShow_0000590 / 0000590 / 590` matches.
    For `MYSHOW_101_054_idcritical` we yield the full id plus each numeric chunk
    (`101`, `054`, normalized to `101`/`54`).
    """
    head = _shot_id_from_name(name)
    tokens: List[str] = [head.lower()]
    for segment in head.replace("-", "_").split("_"):
        seg = segment.strip().lower()
        if seg and seg != head.lower():
            tokens.append(seg)
            normalized = _normalize_shot_id(seg)
            if normalized and normalized != seg:
                tokens.append(normalized)
    return tokens


def _clip_track_index(clip: Any) -> int:
    try:
        track_type, track_index = clip.GetTrackTypeAndIndex()
        if str(track_type).lower() == "video":
            return int(track_index)
    except Exception:
        pass
    return 1


def _clip_start(clip: Any) -> Optional[int]:
    """Frame-start of a clip, or ``None`` if the handle is stale/invalid.

    After a Resolve relaunch the old timeline-item handles are dead — every
    method returns ``None``, so ``clip.GetStart()`` becomes ``None()`` and raises
    ``TypeError: 'NoneType' object is not callable``. This swallows that so
    callers can detect a dead handle instead of crashing.
    """
    try:
        return int(clip.GetStart())
    except Exception:
        return None


def _clip_duration(clip: Any) -> Optional[int]:
    """Frame-duration of a clip, or ``None`` on a stale/dead handle.

    Like :func:`_clip_start`, this guards against a relaunched-Resolve handle
    whose ``GetDuration`` is ``None`` (``None()`` → TypeError) so the loop
    header never crashes on a dead handle.
    """
    try:
        return int(clip.GetDuration())
    except Exception:
        return None


def _refetch_clip(
    timeline: Any,
    *,
    name: str,
    start: Optional[int],
    track: int,
    all_tracks: bool,
) -> Optional[Any]:
    """Re-fetch a fresh timeline-item handle after a Resolve relaunch.

    Resolve scripting handles do not survive a force-quit + relaunch: the
    timeline content is identical, but every old item proxy is dead. Match the
    clip on the freshly-opened timeline by name (and ``start`` as a tiebreak when
    a name repeats) so the retry uses a live handle. Returns ``None`` if the clip
    can't be located on the reopened timeline.
    """
    candidates = [
        c
        for c in _timeline_clips(timeline, track=track, all_tracks=all_tracks)
        if _clip_name(c) == name
    ]
    if not candidates:
        return None
    if len(candidates) == 1 or start is None:
        return candidates[0]
    for c in candidates:
        if _clip_start(c) == int(start):
            return c
    return candidates[0]


def _refetch_all_clips(
    timeline: Any,
    idents: Sequence[Tuple[str, Optional[int], int]],
    *,
    track: int,
    all_tracks: bool,
) -> List[Optional[Any]]:
    """Rebuild the whole clip list (fresh handles) in the original order.

    Called after a Resolve relaunch invalidates every handle in the working
    ``clips`` list. ``idents`` is the ``(name, start, track)`` identity captured
    while the handles were still alive. Preserves order/length so the batch loop
    index ``i`` stays meaningful; a slot is ``None`` if that clip can't be found
    on the reopened timeline (handled as a clean per-clip failure, not a crash).
    """
    fresh = _timeline_clips(timeline, track=track, all_tracks=all_tracks)
    by_name: Dict[str, List[Any]] = {}
    for c in fresh:
        by_name.setdefault(_clip_name(c), []).append(c)
    out: List[Optional[Any]] = []
    for name, start, _trk in idents:
        cands = by_name.get(name) or []
        if not cands:
            out.append(None)
        elif len(cands) == 1 or start is None:
            out.append(cands[0])
        else:
            chosen = next((c for c in cands if _clip_start(c) == int(start)), cands[0])
            out.append(chosen)
    return out


def _filter_clips(clips: Sequence[Any], shot_ids: Sequence[str]) -> List[Any]:
    """Filter timeline clips by user-supplied shot IDs.

    Numeric needles (e.g. `590`, `0000590`) match only as discrete tokens or
    leading-zero-normalized forms — never as raw substrings, otherwise frame
    ranges like `[00001001-00001066]` would over-match every clip.

    Non-numeric needles (e.g. `MyShow`, `idcritical`) additionally fall
    back to a case-insensitive substring match against the cleaned shot-id head
    (the part of the clip name before any `.[...]` frame range or extension).
    """
    if not shot_ids:
        return list(clips)

    needles = [str(s).strip() for s in shot_ids if str(s).strip()]
    if not needles:
        return list(clips)

    numeric_raw = {n.lower() for n in needles if n.isdigit()}
    numeric_norm = {_normalize_shot_id(n) for n in numeric_raw}
    text_needles = {n.lower() for n in needles if not n.isdigit()}

    out: List[Any] = []
    for clip in clips:
        name = _clip_name(clip)
        tokens = set(_shot_id_tokens(name))
        norm_tokens = {_normalize_shot_id(t) for t in tokens}
        head_lower = _shot_id_from_name(name).lower()

        if tokens & numeric_raw or norm_tokens & numeric_norm:
            out.append(clip)
            continue
        if tokens & text_needles or any(n in head_lower for n in text_needles):
            out.append(clip)
    return out


def _set_current_clip(resolve: Any, timeline: Any, clip: Any) -> Dict[str, Any]:
    resolve.OpenPage("edit")
    time.sleep(0.2)
    duration = int(clip.GetDuration())
    mid_frame = int(clip.GetStart()) + max(1, duration // 2)
    tc = _frame_to_timecode(mid_frame, timeline)
    set_ok = timeline.SetCurrentTimecode(tc)
    time.sleep(0.35)
    current = timeline.GetCurrentVideoItem()
    return {
        "set_timecode": bool(set_ok),
        "timecode": tc,
        "current": current.GetName() if current else None,
        "matched": bool(current and current.GetName() == clip.GetName()),
    }


def _terminate_helper(helper: Optional[subprocess.Popen]) -> None:
    """Best-effort teardown of the open-Neat helper process group."""
    if helper is None or helper.poll() is not None:
        return
    try:
        os.killpg(helper.pid, signal.SIGTERM)
        helper.communicate(timeout=2)
    except Exception:
        try:
            os.killpg(helper.pid, signal.SIGKILL)
            helper.communicate(timeout=2)
        except Exception:
            pass


def _add_neat_node(
    clip: Any,
    timeout: float,
    *,
    reuse_existing: bool,
    no_color_wrap: bool = False,
    color_wrap_scale: float = 0.125,
    reset: bool = False,
) -> Dict[str, Any]:
    """Add (or reuse) + select the Neat node on the current clip's Fusion comp.

    The helper does NOT open Neat's window (no blocking ``SetInput``): it adds
    the node, selects it so the Fusion Inspector shows the "Prepare Noise
    Profile" button, and exits immediately. :class:`NeatDriver` opens and drives
    the window from the visible UI. Because the helper never blocks, there is no
    risk of leaving a modal open or wedging Resolve scripting, and we can simply
    wait for it to finish.
    """
    neat_ui._run_proc(["open", "-a", "DaVinci Resolve"], timeout=10)
    env = os.environ.copy()
    env.update(
        {
            "AUTONEAT_TARGET_TRACK": str(_clip_track_index(clip)),
            "AUTONEAT_TARGET_START": str(int(clip.GetStart())),
            "AUTONEAT_TARGET_NAME": _clip_name(clip),
            "AUTONEAT_FORCE_NEW": "0" if (reuse_existing and not reset) else "1",
            "AUTONEAT_NO_COLOR_WRAP": "1" if no_color_wrap else "0",
            "AUTONEAT_COLOR_WRAP_SCALE": str(color_wrap_scale),
            "AUTONEAT_RESET": "1" if reset else "0",
        }
    )
    helper = subprocess.Popen(
        [sys.executable, str(Path(neat_ui.__file__).resolve()), "--_open-neat-helper"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        # Adding/selecting the OFX node should be effectively immediate. If this
        # helper blocks for multiple seconds, Resolve scripting is wedged or a
        # stale Neat UI is still blocking the second scripting session.
        node_timeout = min(max(float(timeout), 3.0), 5.0)
        stdout, stderr = helper.communicate(timeout=node_timeout)
    except subprocess.TimeoutExpired:
        _terminate_helper(helper)
        return {
            "ok": False,
            "helper": {},
            "stdout": "",
            "stderr": (
                f"helper timed out adding Neat node after {node_timeout:.0f}s; "
                "Resolve scripting is likely wedged"
            ),
        }

    data: Dict[str, Any] = {}
    if stdout.strip():
        try:
            data = json.loads(stdout.strip().splitlines()[0])
        except json.JSONDecodeError:
            pass
    ok = helper.returncode == 0 and data.get("ok") is not False
    return {"ok": ok, "helper": data, "stdout": stdout.strip(), "stderr": stderr.strip()}


# ---------------------------------------------------------------------------
# Neat UI driver
# ---------------------------------------------------------------------------


@dataclass
class DriveConfig:
    """Bounded timing budgets for the Neat state machine.

    All derived from CLI args once, so the driver never re-reads ``argparse``.
    """

    step_delay: float
    profile_wait: float
    apply_delay: float
    prepare_timeout: float
    stuck_timeout: float
    ready_timeout: float
    close_timeout: float
    profile_cooldown: float
    open_timeout: float
    learn_templates: bool = True
    max_open_attempts: int = 5
    abort_timeout: float = 30.0
    abort_max_attempts: int = 10
    # Frozen-Resolve watchdog (per-clip wall-clock cap + auto restart).
    clip_timeout: float = 360.0
    auto_restart: bool = True
    max_restarts: int = 15
    max_clip_restarts: int = 3
    restart_grace: float = 25.0
    restart_launch_timeout: float = 240.0
    # Consecutive per-clip failures (e.g. node-add timeouts) that signal a
    # wedged Resolve and trigger a restart + retry of the failed streak.
    restart_after_failures: int = 3
    # Neat's own Input Data Gamma selector is sticky inside the plugin. Only
    # touch it when explicitly requested; otherwise avoid the popup entirely.
    set_linear: bool = False
    # Fresh/reset Neat nodes cannot show a stale already-built profile, so the
    # editor can Apply on the first concrete ready read and should not re-click
    # Auto Profile while Neat is still building.
    fresh_profile: bool = False

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "DriveConfig":
        return cls(
            learn_templates=not getattr(args, "no_templates", False),
            step_delay=max(args.step_delay, 0.1),
            profile_wait=max(args.profile_wait, 0.3),
            apply_delay=max(args.apply_delay, 0.0),
            prepare_timeout=args.prepare_timeout,
            # Any non-prepare state must change within this budget or we bail
            # (and the finally still closes the window).
            stuck_timeout=max(args.editor_timeout, args.ready_timeout, 60.0),
            # Hard cap on the profiling phase (Auto Profile clicked → profile
            # ready). Bounds a clip even when OCR flickers between profiled and
            # 'unknown' (which keeps the per-state stuck timer from firing).
            ready_timeout=max(args.ready_timeout, 30.0),
            close_timeout=max(args.close_timeout, 3.0),
            # Don't spam Auto Profile while a profile is building.
            profile_cooldown=max(args.profile_wait * 3, 15.0),
            open_timeout=args.open_timeout,
            clip_timeout=max(getattr(args, "clip_timeout", 360.0), 60.0),
            auto_restart=getattr(args, "auto_restart", True),
            max_restarts=max(getattr(args, "max_restarts", 15), 0),
            max_clip_restarts=max(getattr(args, "max_clip_restarts", 3), 1),
            restart_launch_timeout=max(getattr(args, "restart_launch_timeout", 240.0), 30.0),
            restart_after_failures=max(getattr(args, "restart_after_failures", 3), 1),
            set_linear=bool(getattr(args, "set_linear", False)),
            fresh_profile=bool(
                getattr(args, "reset_neat", False) or not getattr(args, "reuse_existing_neat", True)
            ),
        )


class NeatDriver:
    """Drives the visible Neat UI for a single clip, with guaranteed cleanup.

    Two public entry points:

      * :meth:`drive` — open Neat, Auto Profile, Apply, confirm the window
        closed. Raises ``RuntimeError`` on any timeout / stuck state.
      * :meth:`abort` — close whatever Neat window/modal is on screen, by any
        means (Cancel click, Escape). Always called from ``_process_clip``'s
        ``finally`` so a failed clip can never leave a window open to wedge the
        next one. Idempotent and fast when nothing is open.
    """

    def __init__(
        self,
        work_dir: Path,
        rec: StepRecorder,
        cfg: DriveConfig,
        *,
        target_env: Optional[Dict[str, str]] = None,
    ):
        self.work_dir = work_dir
        self.rec = rec
        self.cfg = cfg
        self.target_env = dict(target_env or {})
        # Self-calibrating locator: template-match first (deterministic), OCR
        # fallback, learns control templates from clips that succeed.
        self.locator = neat_ui.Locator(work_dir, learn=cfg.learn_templates)

    # -- screen reads / clicks -------------------------------------------

    def detect(self) -> Tuple[str, str]:
        """Return ``(state, text)`` for the current screen.

        ``neat_ui._read_screen_state`` brings Resolve to the front, screen-grabs,
        OCRs, and classifies — so the driver never has to activate separately.
        """
        state, text, _rows = neat_ui._read_screen_state(self.work_dir)
        return state, text

    def _click(
        self, label: str, *, editor: bool = False, tag: Optional[str] = None
    ) -> Tuple[float, float]:
        fast = neat_ui.fast_control_point(label, self.work_dir)
        if fast is not None:
            neat_ui._click_at_quartz(fast[0], fast[1])
            self.rec.add(f"{tag or label}:fast-geometry:{round(fast[0])},{round(fast[1])}")
            return fast
        point, method = self.locator.locate_and_click(label, editor=editor)
        self.rec.add(f"{tag or label}:{method}:{round(point[0])},{round(point[1])}")
        return point

    def commit_templates(self) -> None:
        """Persist control templates learned during a SUCCESSFUL clip, so the
        next clip locates them deterministically. No-op when learning is off."""
        learned = self.locator.commit_templates()
        if learned:
            self.rec.add(f"templates:learned {','.join(sorted(learned))}")

    def discard_templates(self) -> None:
        self.locator.discard_templates()

    def invalidate_templates(self) -> None:
        """Distrust templates this clip relied on (called after a failure)."""
        invalidated = self.locator.invalidate_used_templates()
        if invalidated:
            self.rec.add(f"templates:invalidated {','.join(sorted(invalidated))}")

    def _try_click_fresh(self, label: str, *, tag: Optional[str] = None) -> bool:
        """Click a control using fresh OCR only, bypassing learned templates.

        Cleanup is the wrong place to use cached templates: one stale template
        can keep clicking the same bad point forever while the recovery loop
        thinks it is making progress.
        """
        try:
            point = neat_ui._click_ocr_fullscreen(label, self.work_dir)
            self.rec.add(f"{tag or label}:fresh-ocr:{round(point[0])},{round(point[1])}")
            return True
        except Exception as exc:
            self.rec.add(f"{tag or label}:fresh-ocr-miss {str(exc)[:60]}")
            return False

    def _try_click(self, label: str, *, editor: bool = False, tag: Optional[str] = None) -> bool:
        try:
            self._click(label, editor=editor, tag=tag)
            return True
        except Exception as exc:
            self.rec.add(f"{tag or label}:locate-miss {str(exc)[:60]}")
            return False

    def _open_prepare_profile(self, *, tag: str) -> bool:
        """Open Neat from the selected node, preferring the OFX API helper."""
        cfg = self.cfg
        self._open_attempts = getattr(self, "_open_attempts", 0)
        if self._open_attempts >= cfg.max_open_attempts:
            raise RuntimeError("Clicked 'Prepare Noise Profile' but Neat's window never opened")
        ok, detail = neat_ui.open_prepare_profile_via_api(
            self.work_dir,
            timeout=max(cfg.open_timeout, 6.0),
            target_env=self.target_env,
        )
        self.rec.add(f"{tag}:{detail}")
        if not ok:
            self._click("prepare-profile", tag=f"{tag}-geometry")
        self._open_attempts += 1
        time.sleep(max(cfg.step_delay, 0.25 if ok else 0.6))
        return True

    def _retry_open_from_inspector(self, *, tag: str) -> bool:
        """Open Neat again when the selected node did not open it."""
        return self._open_prepare_profile(tag=tag)

    def neat_window_present(self) -> bool:
        """True if a Neat *editor* window currently exists (AX/window probe).

        Authoritative for the non-modal editor window. Best-effort: if window
        enumeration is unavailable (or stalls and raises), treat as not present
        so cleanup loops can't spin forever. Modal dialogs are detected via OCR
        state instead, so this is only consulted when OCR says no modal is up.
        """
        try:
            windows = neat_ui._resolve_windows(activate=False)
        except Exception:
            return False
        try:
            return neat_ui._find_neat_window(windows=windows) is not None
        except Exception:
            return False

    # -- main drive ------------------------------------------------------

    def _reset_drive_state(self) -> None:
        """Per-drive bookkeeping. One :class:`NeatDriver` drives one clip, and
        each :meth:`drive` call starts from a clean slate.
        """
        self._opened = False  # ever seen the Neat window (any open editor/modal)?
        self._applied = False
        self._open_attempts = 0
        self._profile_clicked_at: Optional[float] = None
        self._apply_clicked_at: Optional[float] = None
        self._input_data_set = False
        self._confirm_build_attempts = 0
        self._information_dialog_attempts = 0
        self._require_observed_profile = False
        self._auto_profile_baseline: Optional[Path] = None
        self._auto_profile_change_seen = False
        # Consecutive polls in which a built profile has been visible, and when
        # the current profiled streak started. Used to gate Apply on a STABLE
        # freshly-built profile rather than a blind timer.
        self._profiled_streak = 0
        self._first_profiled_at: Optional[float] = None

    def _profile_modal_handled(self, state: str) -> bool:
        """Handle post-Auto-Profile modals before any Apply click.

        Neat can raise "selected area is small" or "continue building profile"
        after Auto Profile. The fast Apply path must not click Apply over those
        modals; doing so leaves the editor in a half-applied state that poisons
        the next clip.
        """
        if state == "confirm-small-area":
            self._click("use-small-area")
        elif state == "confirm-build-profile":
            self._confirm_build_attempts += 1
            if self._confirm_build_attempts > 3:
                raise RuntimeError("Neat confirm-build modal did not dismiss after 3 clicks")
            point = neat_ui.locate_confirm_button(self.work_dir, "continue")
            if point is None:
                point = neat_ui.locate_confirm_continue_button(self.work_dir)
            if point is None:
                raise RuntimeError("Could not locate Neat confirm-build continue button")
            neat_ui._click_at_quartz(point[0], point[1])
            self.rec.add(f"confirm-build:continue:{round(point[0])},{round(point[1])}")
        elif state == "information-dialog":
            self._information_dialog_attempts += 1
            if self._information_dialog_attempts > 4:
                raise RuntimeError("Neat information modal did not dismiss after 4 clicks")
            detail = neat_ui.dismiss_information_dialog(self.work_dir)
            if detail is not None:
                self.rec.add(f"info-ok:pre-apply:{detail}")
            else:
                self._click("ok", tag="info-ok")
        else:
            return False

        if self._apply_clicked_at is not None or self._applied:
            self.rec.add("apply:retracted-after-profile-modal")
        self._apply_clicked_at = None
        self._applied = False
        self._profile_clicked_at = time.time()
        self._require_observed_profile = True
        self._profiled_streak = 0
        self._first_profiled_at = None
        self._auto_profile_change_seen = False
        time.sleep(self.cfg.step_delay)
        return True

    def drive(self, *, prepare_clicked: bool = False) -> None:
        """Open + Auto Profile + Apply the current clip; return when closed.

        Raises ``RuntimeError`` if Neat gets stuck. The caller's ``finally``
        guarantees the window is closed regardless of how this returns.
        """
        cfg = self.cfg
        self._reset_drive_state()
        if prepare_clicked:
            self._open_attempts = 1
        # Absolute safety net so a pathological loop can't run forever.
        deadline = time.time() + cfg.prepare_timeout + cfg.stuck_timeout * 2 + 120.0

        prepare_started: Optional[float] = None
        last_state: Optional[str] = None
        last_change = time.time()

        while True:
            if time.time() > deadline:
                raise RuntimeError("Neat driver exceeded absolute time cap")

            if (
                self._profile_clicked_at is not None
                and self._apply_clicked_at is None
                and not self._require_observed_profile
            ):
                elapsed_since_profile = time.time() - self._profile_clicked_at
                if (
                    self._auto_profile_baseline is not None
                    and elapsed_since_profile >= 0.35
                    and not self._auto_profile_change_seen
                    and neat_ui.control_region_changed(
                        "auto-profile",
                        self._auto_profile_baseline,
                        self.work_dir,
                        name="auto-profile-fast-after",
                    )
                ):
                    self._auto_profile_change_seen = True
                    self.rec.add("profile-ready:button-region-changed")
                    self._click("apply", editor=True)
                    self._apply_clicked_at = time.time()
                    self._applied = True
                    time.sleep(cfg.step_delay)
                    continue
                if elapsed_since_profile >= 1.5:
                    self.rec.add("profile-ready:optimistic-delay")
                    self._click("apply", editor=True)
                    self._apply_clicked_at = time.time()
                    self._applied = True
                    time.sleep(cfg.step_delay)
                    continue
                else:
                    time.sleep(0.15)
                    continue

            state, text = self.detect()
            if state != last_state:
                self.rec.add(f"neat-state:{state}")
                last_state = state
                last_change = time.time()
            if state in _WINDOW_OPEN_STATES:
                self._opened = True

            # Stuck detection. preparing-input gets its own (long) budget; every
            # other state must change within stuck_timeout or we bail.
            if state == "preparing-input":
                if prepare_started is None:
                    prepare_started = time.time()
                elif time.time() - prepare_started > cfg.prepare_timeout:
                    raise RuntimeError(f"Neat stuck preparing input > {cfg.prepare_timeout:.0f}s")
            else:
                prepare_started = None
                if time.time() - last_change > cfg.stuck_timeout:
                    raise RuntimeError(
                        f"Neat stuck in state={state!r} for >{cfg.stuck_timeout:.0f}s "
                        f"(last_text={text[:80]!r})"
                    )

            if state == "demo-splash":
                self._click("continue")
                time.sleep(cfg.step_delay)

            elif state == "information-dialog":
                # Neat's "select a frame first" / dynamic-range notice. Dismissing
                # it lets Neat auto-pick frames and continue.
                self._information_dialog_attempts += 1
                self.rec.add(f"info-text:{text[:120]!r}")
                if self._information_dialog_attempts > 2:
                    raise RuntimeError("Neat information modal did not dismiss after 2 attempts")
                detail = neat_ui.dismiss_information_dialog(self.work_dir)
                if detail is not None:
                    self.rec.add(f"info-ok:{detail}")
                else:
                    ready = neat_ui.editor_profile_ready(self.work_dir)
                    if ready is not None or self.neat_window_present():
                        drive_state = "editor-profiled" if ready else "editor-unprofiled"
                        self.rec.add(f"info-bypass:{drive_state}")
                        self._drive_editor(self._editor_state_for_drive(drive_state))
                    else:
                        self._click("ok", tag="info-ok")
                time.sleep(cfg.step_delay)

            elif state == "confirm-small-area":
                # Auto Profile picked a sub-128px sample area; accept it.
                self._click("use-small-area")
                if self._apply_clicked_at is not None or self._applied:
                    self.rec.add("apply:retracted-after-small-area")
                self._apply_clicked_at = None
                self._applied = False
                self._profile_clicked_at = time.time()
                self._profiled_streak = 0
                self._first_profiled_at = None
                self._require_observed_profile = True
                time.sleep(cfg.step_delay)

            elif state == "confirm-build-profile":
                # Neat's "cannot automatically find a large uniform area" modal.
                # Accept it ("Continue building profile") so a (lower-quality)
                # profile builds from the selected area and the editor proceeds
                # to Apply rather than wedging open. Prefer geometry: OCR misses
                # this modal on node 2 and Return does not activate the default
                # button reliably.
                self._confirm_build_attempts += 1
                if self._confirm_build_attempts > 3:
                    raise RuntimeError("Neat confirm-build modal did not dismiss after 3 clicks")
                point = neat_ui.locate_confirm_button(self.work_dir, "continue")
                if point is None:
                    point = neat_ui.locate_confirm_continue_button(self.work_dir)
                if point is not None:
                    neat_ui._click_at_quartz(point[0], point[1])
                    self.rec.add(f"confirm-build:continue:{round(point[0])},{round(point[1])}")
                else:
                    raise RuntimeError("Could not locate Neat confirm-build continue button")
                if self._apply_clicked_at is not None or self._applied:
                    self.rec.add("apply:retracted-after-confirm-build")
                self._apply_clicked_at = None
                self._applied = False
                self._profile_clicked_at = time.time()
                self._profiled_streak = 0
                self._first_profiled_at = None
                self._require_observed_profile = True
                time.sleep(cfg.step_delay)

            elif state == "preparing-input":
                # Frame rendering for Neat — slow on 4K HDR EXR. Don't let it
                # count against the post-Auto-Profile readiness bound: push the
                # profiling clock forward so the ready timer measures only
                # in-editor profiling time, not frame prep.
                if self._profile_clicked_at is not None:
                    self._profile_clicked_at = time.time()
                time.sleep(cfg.step_delay)

            elif state in _EDITOR_STATES:
                # If OCR still sees Neat's editor chrome, the window is still
                # actionable. Do not let a stale/blocked AX window probe declare
                # success over visible editor state.
                self._drive_editor(self._editor_state_for_drive(state))

            elif state == "inspector-prepare":
                # Node selected, editor (apparently) not open. If we've already
                # applied, the editor closed → done.
                if self._applied:
                    self.rec.add("neat-window-closed")
                    return
                # AX window topology is ground truth for whether the editor
                # actually opened — far more reliable than the full-screen OCR
                # classifier, which routinely misreads the open editor as the
                # Inspector behind it. Three cases:
                if self.neat_window_present():
                    # Editor IS open; OCR misclassified it. Drive it rather than
                    # waiting (the old flaky `_opened` flag would latch and stall
                    # here until the stuck timeout).
                    self._opened = True
                    self.rec.add("neat-open-confirmed:ax→drive")
                    self._drive_editor(self._editor_state_for_drive("editor-unprofiled"))
                else:
                    # Neat requires the host's *actively selected current frame*
                    # to be inside this clip when Prepare is called. Triggering
                    # the OFX input from a background scripting helper bypasses
                    # that visible selection state and can produce Neat's
                    # "no frame selected within the clip" warning. Always open
                    # from the visible Inspector button after the parent has
                    # moved Resolve's playhead to the target clip.
                    self._retry_open_from_inspector(tag="prepare-profile-retry")

            else:  # unknown
                if self._applied:
                    self.rec.add("neat-window-closed")
                    return
                if self._open_attempts > 0:
                    if self.neat_window_present():
                        self._opened = True
                        self.rec.add("neat-open-confirmed:ax-after-unknown")
                        self._drive_editor(self._editor_state_for_drive("editor-unprofiled"))
                        continue
                    self._retry_open_from_inspector(tag="prepare-profile-retry-unknown")
                    continue
                time.sleep(cfg.step_delay)

    def _editor_state_for_drive(self, fallback: str) -> str:
        """Return the editor state needed by ``_drive_editor``.

        Before Auto Profile, the only useful action is clicking Auto Profile.
        Running the expensive profile-readiness OCR probe here adds dead time on
        every clip and cannot affect that first click.
        """
        if self._profile_clicked_at is None:
            return "editor-unprofiled"
        return self._refine_editor_state(fallback)

    def _refine_editor_state(self, fallback: str) -> str:
        """Resolve the true editor state via region-crop OCR of the noise-profile
        panels, falling back to the caller's full-screen guess.

        Full-screen OCR reads only the large menu-bar text and misses the small
        grey "Noise Level"/"profile not ready" strings, so it can't tell a
        freshly-built profile from one still building. A wrong "unprofiled" makes
        the driver re-click Build Profile — which, on small auto-areas, re-opens
        the "selected area is small" dialog every cycle (an infinite loop). This
        refinement runs in BOTH editor paths (direct ``editor-*`` states and the
        AX-recovered "OCR misread the open editor as the Inspector" path) so the
        Apply gate sees the profile no matter how the frame OCR'd."""
        ready = neat_ui.editor_profile_ready(self.work_dir)
        if ready is True:
            return "editor-profiled"
        if ready is False:
            return "editor-unprofiled"
        return fallback

    def _drive_editor(self, state: str) -> None:
        """One tick of editor handling. Mutates per-drive state on ``self``.

        Sequence: Auto Profile (once) → wait for a STABLE freshly-built profile
        → Apply (once) → wait for the window to close.

        The Apply gate is observation-based, not a blind timer. Fresh/reset nodes
        can apply on the first concrete profile-ready read; reused nodes require
        a second read to avoid applying a stale profile that flashes before the
        fresh Auto Profile rebuild starts.
        """
        cfg = self.cfg

        # Track profile-ready stability for the Apply gate.
        if state in ("editor-profiled", "editor"):
            self._profiled_streak += 1
            if self._first_profiled_at is None:
                self._first_profiled_at = time.time()
        else:  # editor-unprofiled — profile (re)building; reset the streak.
            self._profiled_streak = 0
            self._first_profiled_at = None

        if self._profile_clicked_at is None:
            # Always Auto Profile first — even when the editor opens already
            # showing a (possibly generic/stale) profile, we want a fresh AUTO
            # profile for this frame. Discard any streak accrued from the stale
            # pre-click profile so the gate only trusts the rebuild.
            if cfg.set_linear and not self._input_data_set:
                try:
                    for step in neat_ui.choose_input_data_linear(self.work_dir, self.locator):
                        self.rec.add(step)
                except RuntimeError as exc:
                    exc_text = str(exc).lower()
                    fresh_state, _fresh_text = self.detect()
                    if fresh_state == "inspector-prepare" or (
                        "input-gamma" in exc_text
                        and "prepare noise profile" in exc_text
                        and "controls settings" in exc_text
                    ):
                        self.rec.add("input-data:editor-not-open-after-state-misread; reopening")
                        self._opened = False
                        self._profile_clicked_at = None
                        return
                    raise
                self._input_data_set = True
                time.sleep(cfg.step_delay)
            # Capturing a before-image costs about a second on node displays,
            # and current runs already use the fixed optimistic apply delay.
            self._auto_profile_baseline = None
            self._click("auto-profile", editor=True)
            self._profile_clicked_at = time.time()
            self._require_observed_profile = True
            self._profiled_streak = 0
            self._first_profiled_at = None
            time.sleep(max(cfg.profile_wait, cfg.step_delay))
            return

        if self._apply_clicked_at is None:
            # Hard bound on the profiling phase so a flickering OCR read (which
            # keeps the per-state stuck timer alive by oscillating profiled ↔
            # unknown) can't hang the clip until the absolute cap.
            if (time.time() - self._profile_clicked_at) > (cfg.ready_timeout + cfg.apply_delay):
                raise RuntimeError(
                    f"Neat profile not ready within {cfg.ready_timeout:.0f}s of Auto Profile"
                )
            fast_ready = False
            if (
                not self._auto_profile_change_seen
                and self._auto_profile_baseline is not None
                and (time.time() - self._profile_clicked_at) >= 0.45
                and neat_ui.control_region_changed(
                    "auto-profile",
                    self._auto_profile_baseline,
                    self.work_dir,
                    name="auto-profile-after",
                )
            ):
                self._auto_profile_change_seen = True
                self.rec.add("profile-ready:button-region-changed")
                fast_ready = True
            profile_ready = fast_ready or (
                self._profiled_streak >= (1 if cfg.fresh_profile else 2)
                and self._first_profiled_at is not None
                and (time.time() - self._first_profiled_at) >= cfg.apply_delay
            )
            if profile_ready:
                self._click("apply", editor=True)
                self._apply_clicked_at = time.time()
                self._applied = True
                time.sleep(cfg.step_delay)
            elif state == "editor-unprofiled":
                # Still building (or the click missed). Re-click Auto Profile
                # only after a cooldown; a failed re-locate means "still
                # building", NOT an error.
                if (
                    not cfg.fresh_profile
                    and (time.time() - self._profile_clicked_at) > cfg.profile_cooldown
                ):
                    if not self._try_click("auto-profile", editor=True, tag="auto-profile-retry"):
                        self.rec.add("auto-profile-retry:building")
                    self._profile_clicked_at = time.time()
                time.sleep(cfg.step_delay)
            else:
                # Profiled but not yet stable / apply_delay not elapsed — wait.
                time.sleep(cfg.step_delay)
            return

        # Apply already clicked; wait for the window to close, retry on timeout.
        if (time.time() - self._apply_clicked_at) > cfg.close_timeout:
            self._click("apply", editor=True, tag="apply-retry")
            self._apply_clicked_at = time.time()
        time.sleep(cfg.step_delay)

    # -- guaranteed cleanup ----------------------------------------------

    def abort(self) -> bool:
        """Close any Neat window/modal on screen. Returns True once clean.

        This is the linchpin of reliability: a Neat window left open wedges the
        next clip's Resolve scripting and ultimately freezes Resolve. Called
        from ``_process_clip``'s ``finally`` for EVERY clip — fast no-op on a
        clean success, forceful close after a failure.

        Strategy each tick: dismiss whatever is showing with its own button
        (Cancel / OK / Continue / Use-small-area) AND press Escape as a belt-
        and-suspenders universal "reject". A mis-located Cancel that lands on
        Apply is acceptable — the window still closes. Bounded by attempts and a
        wall-clock deadline so it can never spin forever.
        """
        cfg = self.cfg
        end = time.time() + cfg.abort_timeout
        for attempt in range(cfg.abort_max_attempts):
            if time.time() > end:
                break
            state, _text = self.detect()
            # Visible Neat states win over AX: Accessibility can hang or miss
            # Qt child windows while Neat modals are up, but a fresh screenshot
            # that classifies as editor/modal means there is still UI to close.
            if state not in _WINDOW_OPEN_STATES and not self.neat_window_present():
                self.rec.add(f"abort:clean state={state} ({attempt} attempt(s))")
                return True

            if state == "information-dialog":
                detail = neat_ui.dismiss_information_dialog(self.work_dir)
                if detail is not None:
                    self.rec.add(f"abort-ok:{detail}")
                else:
                    self._try_click("ok", tag="abort-ok")
                neat_ui._press_return()
            elif state == "demo-splash":
                self._try_click("continue", tag="abort-continue")
            elif state == "confirm-build-profile":
                # Dismiss the "uniform area" confirm so the editor underneath is
                # reachable. Cancel located via high-scale band OCR; Escape (the
                # universal reject below) covers an OCR miss.
                point = neat_ui.locate_confirm_button(self.work_dir, "cancel")
                if point is not None:
                    neat_ui._click_at_quartz(point[0], point[1])
                    self.rec.add(f"abort-confirm:cancel:{round(point[0])},{round(point[1])}")
                else:
                    self.rec.add("abort-confirm:locate-miss")
            elif state == "confirm-small-area":
                # Cancel the small-area confirm so the profile build is rejected
                # and we fall back to the editor, which we then cancel too.
                self._try_click_fresh("cancel", tag="abort-cancel")
            elif state in _EDITOR_STATES:
                self._try_click_fresh("cancel", tag="abort-cancel")
            # Escape always, as the universal Qt "reject" — closes the editor or
            # whatever modal is up even if the Cancel locate missed.
            neat_ui._press_escape()
            self.rec.add("abort:escape")
            time.sleep(cfg.step_delay)

        self.rec.add("abort:FAILED — Neat window may remain open")
        return False


# ---------------------------------------------------------------------------
# Per-clip processing
# ---------------------------------------------------------------------------


def _locate_method_counts(results: List[Dict[str, Any]]) -> Dict[str, int]:
    """Tally how each control was located across the batch, so the operator can
    see the self-calibrating sensor at work: ``template`` should dominate once
    the first clip has bootstrapped, with ``window``/``fullscreen`` (OCR) tail.
    Step format is ``{tag}:{method}:{coords}`` (method = ``template:<score>`` |
    ``window`` | ``fullscreen``)."""
    counts = {"template": 0, "window": 0, "fullscreen": 0}
    for result in results:
        for step in result.get("steps") or []:
            if ":template:" in step:
                counts["template"] += 1
            elif ":window:" in step:
                counts["window"] += 1
            elif ":fullscreen:" in step:
                counts["fullscreen"] += 1
    return counts


def _save_failure_capture(name: str, rec: "StepRecorder") -> None:
    """Persist a screenshot of the failing state for offline inspection.

    Written under the neat_ui cache (which survives the per-clip temp dir) and
    the absolute path is recorded as a step so the orchestrator's log shows
    exactly where to fetch it. Best-effort — never let diagnostics break the
    failure path."""
    try:
        dest_dir = neat_ui._cache_base() / "failures"
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        dest = dest_dir / f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        neat_ui._capture_screen(dest)
        size = neat_vision.image_size(dest)
        dims = f" {size[0]}x{size[1]}px" if size else ""
        rec.add(f"diagnostic-capture:{dest}{dims}")
    except Exception as exc:  # diagnostics must never mask the real failure
        rec.add(f"diagnostic-capture:failed {str(exc)[:80]}")


def _process_clip(
    resolve: Any,
    timeline: Any,
    clip: Any,
    cfg: DriveConfig,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    name = _clip_name(clip)
    rec = StepRecorder()
    with tempfile.TemporaryDirectory(prefix="neat-batch-", dir=str(neat_ui._cache_base())) as tmp:
        work_dir = Path(tmp)
        target_env = {
            "AUTONEAT_TARGET_TRACK": str(_clip_track_index(clip)),
            "AUTONEAT_TARGET_START": str(int(clip.GetStart())),
            "AUTONEAT_TARGET_NAME": _clip_name(clip),
        }
        driver = NeatDriver(work_dir, rec, cfg, target_env=target_env)

        # Defensive: close any Neat window left open by a prior clip BEFORE we
        # touch Resolve scripting (the per-clip finally below normally prevents
        # this, but a hard crash mid-clip could leave one).
        if driver.neat_window_present():
            rec.add("stale-neat:closing-before-clip")
            driver.abort()

        opened: Dict[str, Any] = {"ok": False}
        playhead: Dict[str, Any] = {}
        succeeded = False
        try:
            playhead = _set_current_clip(resolve, timeline, clip)
            rec.add(f"playhead:tc={playhead.get('timecode')} matched={playhead.get('matched')}")
            time.sleep(cfg.step_delay)

            # Switch the GUI to Fusion for THIS clip so Neat's OFX "Prepare
            # Profile" button opens its window over the visible comp. The
            # node-add helper runs in a SECOND scripting session whose own
            # OpenPage is ignored while we hold the connection — the parent (we)
            # must switch the page.
            resolve.OpenPage("fusion")
            rec.add("page:fusion")
            time.sleep(cfg.step_delay)

            rec.add("neat-node:add-or-select")
            added = _add_neat_node(
                clip,
                cfg.open_timeout,
                reuse_existing=args.reuse_existing_neat,
                no_color_wrap=args.no_color_wrap,
                color_wrap_scale=args.color_wrap_scale,
                reset=args.reset_neat,
            )
            if not added.get("ok"):
                rec.add(f"neat-node:FAILED stderr={added.get('stderr', '')[:200]!r}")
                return {
                    "ok": False,
                    "clip": name,
                    "steps": rec.steps,
                    "error": "Could not add Neat node",
                    "open": {k: v for k, v in added.items() if k != "helper"},
                    "playhead": playhead,
                    "elapsed_seconds": round(rec.elapsed(), 1),
                }
            helper_data = added.get("helper", {}) or {}
            rec.add(f"neat-node:OK tool={helper_data.get('tool', '?')}")
            media_range = helper_data.get("media_range")
            if media_range:
                rec.add(f"media-range:{media_range}")

            # Adaptively open + drive Neat to a finished, applied, closed state.
            driver._open_prepare_profile(tag="prepare-profile")
            driver.drive(prepare_clicked=True)
            opened = {"ok": True, "stdout": added.get("stdout", "")}

            wrap = helper_data.get("color_wrap") or {}
            if wrap.get("applied"):
                rec.add(
                    f"color-wrap:applied {wrap.get('in_cs')}/{wrap.get('in_gamma')} "
                    f"↔ {wrap.get('out_cs')}/{wrap.get('out_gamma')} (nits={wrap.get('nits')})"
                )
            elif wrap:
                rec.add(
                    f"color-wrap:skipped reason={wrap.get('skip_reason') or wrap.get('error') or 'unknown'}"
                )

            # Clip succeeded end-to-end (opened → Auto Profile → Apply →
            # closed), so every control located via OCR this clip was correct.
            # Persist them as templates so the next clip locates deterministically.
            driver.commit_templates()
            succeeded = True
            return {
                "ok": True,
                "clip": name,
                "steps": rec.steps,
                "playhead": playhead,
                "open": opened,
                "elapsed_seconds": round(rec.elapsed(), 1),
            }
        finally:
            # On failure, persist a screenshot of the *stuck* state BEFORE abort
            # (escape closes windows and destroys the evidence). Saved outside the
            # temp dir so it survives; the path is logged so the orchestrator can
            # retrieve it. This is the difference between fixing the real state
            # and guessing from garbled OCR text.
            if not succeeded:
                _save_failure_capture(name, rec)
            # THE invariant: never leave a Neat window open. A clean success has
            # already observed `neat-window-closed`, so skip the expensive
            # screen/OCR abort probe. On any failure, forcefully close the window
            # so the next clip's scripting isn't wedged and Resolve doesn't freeze.
            if not succeeded:
                driver.abort()
            # On failure, distrust any templates this clip relied on so a rare
            # poisoned template self-heals (re-learned from OCR next success)
            # instead of misclicking every subsequent clip.
            if not succeeded:
                driver.invalidate_templates()


def _process_clip_watchdog(
    resolve: Any,
    timeline: Any,
    clip: Any,
    cfg: DriveConfig,
    args: argparse.Namespace,
    ctx: Dict[str, Any],
) -> Tuple[Dict[str, Any], Any, Any]:
    """Run :func:`_process_clip` under a wall-clock watchdog.

    A single clip should finish (success OR a clean bounded failure) well within
    ``cfg.clip_timeout``. If it doesn't, Resolve's scripting/UI is wedged — the
    only reliable recovery is to force-quit and relaunch Resolve. We do exactly
    that, reopen the project/timeline, and retry the SAME clip (up to
    ``cfg.max_clip_restarts`` restarts for this clip, and ``cfg.max_restarts``
    across the whole batch). Returns ``(result, resolve, timeline)`` because the
    Resolve handles are replaced by a restart.
    """
    name = _clip_name(clip)
    # Capture identity while the handle is alive so we can re-fetch a fresh one
    # after a relaunch (the old handle is dead post-restart — see _refetch_clip).
    clip_start = _clip_start(clip)
    clip_track = _clip_track_index(clip)
    clip_restarts = 0
    while True:
        box: Dict[str, Any] = {"done": False, "result": None}

        def _runner(b: Dict[str, Any] = box, rv: Any = resolve, tl: Any = timeline) -> None:
            try:
                b["result"] = _process_clip(rv, tl, clip, cfg, args)
            except Exception as exc:  # noqa: BLE001
                res: Dict[str, Any] = {"ok": False, "clip": name, "error": str(exc)}
                if args.debug:
                    res["traceback"] = traceback.format_exc()
                b["result"] = res
            finally:
                b["done"] = True

        t0 = time.time()
        th = threading.Thread(target=_runner, daemon=True)
        th.start()
        th.join(timeout=cfg.clip_timeout)

        if box["done"]:
            return box["result"], resolve, timeline

        # --- Hung. Resolve is almost certainly frozen. ---
        if not cfg.auto_restart:
            return (
                {
                    "ok": False,
                    "clip": name,
                    "error": f"clip exceeded {cfg.clip_timeout:.0f}s watchdog (auto-restart disabled)",
                    "elapsed_seconds": round(time.time() - t0, 1),
                },
                resolve,
                timeline,
            )
        if ctx["restarts_used"] >= cfg.max_restarts:
            return (
                {
                    "ok": False,
                    "clip": name,
                    "error": f"hung; batch restart budget ({cfg.max_restarts}) exhausted",
                    "elapsed_seconds": round(time.time() - t0, 1),
                },
                resolve,
                timeline,
            )
        if clip_restarts >= cfg.max_clip_restarts:
            return (
                {
                    "ok": False,
                    "clip": name,
                    "error": f"hung; Resolve restarted {clip_restarts}x for this clip, skipping",
                    "elapsed_seconds": round(time.time() - t0, 1),
                },
                resolve,
                timeline,
            )

        clip_restarts += 1
        ctx["restarts_used"] += 1
        print(
            f"\n  WATCHDOG: '{name}' exceeded {cfg.clip_timeout:.0f}s — Resolve appears "
            f"frozen. Force-restarting Resolve (restart {ctx['restarts_used']}/"
            f"{cfg.max_restarts}, attempt {clip_restarts}/{cfg.max_clip_restarts} for "
            "this clip) and retrying the clip…",
            flush=True,
        )
        try:
            resolve, timeline = _restart_resolve(
                ctx.get("project_name"), ctx.get("timeline_name"), cfg
            )
            print("  WATCHDOG: Resolve relaunched and project/timeline reopened.", flush=True)
        except Exception as exc:  # noqa: BLE001
            return (
                {
                    "ok": False,
                    "clip": name,
                    "error": f"hung; Resolve restart failed: {exc}",
                    "elapsed_seconds": round(time.time() - t0, 1),
                },
                resolve,
                timeline,
            )
        # The pre-restart `clip` handle is now dead. Re-fetch a live one from the
        # reopened timeline before retrying, or the retry just crashes on the
        # stale handle ('NoneType' object is not callable).
        fresh = _refetch_clip(
            timeline,
            name=name,
            start=clip_start,
            track=clip_track,
            all_tracks=args.all_video_tracks,
        )
        if fresh is None:
            return (
                {
                    "ok": False,
                    "clip": name,
                    "error": "hung; clip not found on timeline after Resolve relaunch",
                    "elapsed_seconds": round(time.time() - t0, 1),
                },
                resolve,
                timeline,
            )
        clip = fresh
        # Loop: retry the clip with the fresh handles.


def _needs_resolve_restart(result: Dict[str, Any]) -> bool:
    """True when a bounded failure indicates Resolve scripting is wedged."""
    if result.get("ok"):
        return False
    error = str(result.get("error") or "").lower()
    raw_open = result.get("open")
    open_info: Dict[str, Any] = raw_open if isinstance(raw_open, dict) else {}
    stderr = str(open_info.get("stderr") or "").lower()
    return (
        "could not add neat node" in error
        and "timed out adding neat node" in stderr
        and "resolve scripting is likely wedged" in stderr
    )


def _process_open_neat_ui(cfg: DriveConfig, args: argparse.Namespace) -> Dict[str, Any]:
    """Recovery: close whatever Neat window/modal is currently open.

    This command is used after a failed or interrupted run. It must not try to
    Auto Profile/Apply a stale editor because the timeline item/context may no
    longer be the clip that opened the Neat UI. Its only job is to return
    Resolve to a clean, scriptable state.
    """
    rec = StepRecorder()
    cleanup_cfg = DriveConfig(**{**cfg.__dict__, "abort_timeout": 12.0, "abort_max_attempts": 3})
    with tempfile.TemporaryDirectory(prefix="neat-resume-", dir=str(neat_ui._cache_base())) as tmp:
        work_dir = Path(tmp)
        driver = NeatDriver(work_dir, rec, cleanup_cfg)
        attempted_close = False
        try:
            state, text = driver.detect()
            if state not in _WINDOW_OPEN_STATES and not driver.neat_window_present():
                rec.add(f"resume:nothing-open state={state} text={text[:60]!r}")
                return {
                    "ok": True,
                    "clip": "<open-neat-ui>",
                    "steps": rec.steps,
                    "playhead": {"skipped": "open-neat-ui"},
                    "elapsed_seconds": round(rec.elapsed(), 1),
                }
            rec.add(f"resume:closing state={state}")
            attempted_close = True
            closed = driver.abort()
            recovered_by = "normal-close" if closed else "none"
            if not closed and cleanup_cfg.auto_restart:
                rec.add("resume:force-quit-resolve-after-close-fail")
                _force_quit_resolve(timeout=cleanup_cfg.restart_grace)
                closed = True
                recovered_by = "force-quit-resolve"
            return {
                "ok": bool(closed),
                "clip": "<open-neat-ui>",
                "steps": rec.steps,
                "playhead": {"skipped": "open-neat-ui"},
                **(
                    {"recovered_by": recovered_by}
                    if closed
                    else {"error": "Could not close open Neat UI"}
                ),
                "elapsed_seconds": round(rec.elapsed(), 1),
            }
        except Exception as exc:
            result = {
                "ok": False,
                "clip": "<open-neat-ui>",
                "steps": rec.steps,
                "playhead": {"skipped": "open-neat-ui"},
                "error": str(exc),
                "elapsed_seconds": round(rec.elapsed(), 1),
            }
            if args.debug:
                result["traceback"] = traceback.format_exc()
            return result
        finally:
            if not attempted_close:
                driver.abort()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autoneat profile",
        description="Batch Auto Profile Neat Video on Resolve timeline clips.",
    )
    parser.add_argument("--track", type=int, default=1, help="Video track to process (default: 1)")
    parser.add_argument("--all-video-tracks", action="store_true", help="Process every video track")
    parser.add_argument(
        "-s",
        "--shot-ids",
        nargs="+",
        default=[],
        metavar="ID",
        help=(
            "Only process clips matching these shot IDs. Matches against the "
            "extracted shot-id token (e.g. '590' matches MyShow_0000590), "
            "a leading-zero-normalized form, or a case-insensitive substring of "
            "the clip name."
        ),
    )
    parser.add_argument(
        "--start-from", type=int, default=1, help="1-based target index to start from"
    )
    parser.add_argument("--limit", type=int, default=0, help="Maximum clips to process")
    parser.add_argument(
        "--continue",
        dest="continue_run",
        action="store_true",
        help=(
            "Skip clips that already appear in the previous run's sidecar "
            "(AUTONEAT_RESULTS_JSON or /tmp/autoneat/last-run.json). "
            "Use --retry-failed to re-attempt failed clips alongside skipping succeeded ones."
        ),
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="When --continue is set, also retry clips that failed in the previous run (default: skip both).",
    )
    parser.add_argument(
        "--reset-progress",
        action="store_true",
        help=(
            "Clear this (project, timeline)'s durable progress file before "
            "running, so the batch starts over from clip 1. Without this, "
            "progress is cumulative and never wiped — completed clips are "
            "always remembered across runs."
        ),
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=5,
        help=(
            "Stop the batch early after this many clips fail in a row (0 disables). "
            "Consecutive failures almost always mean Resolve is wedged — continuing "
            "just burns through the remaining clips failing the same way. The per-clip "
            "checkpoint lets you fix Resolve and resume with --continue."
        ),
    )
    parser.add_argument(
        "--clip-timeout",
        type=float,
        default=360.0,
        help=(
            "Per-clip wall-clock watchdog (seconds). A clip should finish or fail "
            "cleanly well within this; if it doesn't, Resolve is wedged and the "
            "watchdog force-quits + relaunches Resolve and retries the clip. "
            "Typical clips take ~40s; default 360s leaves wide margin for slow "
            "4K HDR EXR frame prep."
        ),
    )
    parser.set_defaults(auto_restart=True)
    parser.add_argument(
        "--no-auto-restart",
        dest="auto_restart",
        action="store_false",
        help=(
            "Disable the frozen-Resolve watchdog. A clip that exceeds --clip-timeout "
            "is then recorded as a failure instead of triggering a Resolve restart."
        ),
    )
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=15,
        help="Maximum total Resolve auto-restarts across the whole batch (0 disables).",
    )
    parser.add_argument(
        "--max-clip-restarts",
        type=int,
        default=3,
        help="Maximum Resolve restarts spent retrying a single clip/failed streak before skipping.",
    )
    parser.add_argument(
        "--restart-after-failures",
        type=int,
        default=3,
        help=(
            "Consecutive per-clip failures (e.g. 'helper timed out adding Neat node') "
            "that trigger a Resolve restart + retry of the failed streak. A run of "
            "clip failures means Resolve is wedged even when no single clip exceeds "
            "--clip-timeout; restarting recovers it. Must be < --max-consecutive-failures."
        ),
    )
    parser.add_argument(
        "--restart-launch-timeout",
        type=float,
        default=240.0,
        help="Seconds to wait for Resolve to relaunch and reopen the project/timeline.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List target clips without opening Neat"
    )
    parser.add_argument(
        "--open-timeout", type=float, default=18.0, help="Seconds to wait for Neat to open"
    )
    parser.add_argument(
        "--editor-timeout",
        type=float,
        default=60.0,
        help="Seconds to get past Resolve/Neat splash/dialogs",
    )
    parser.add_argument(
        "--prepare-timeout",
        type=float,
        default=1800.0,
        help=(
            "Maximum seconds to wait while Resolve is preparing input frames for Neat. "
            "Defaults to 30 minutes — frame caching for 4K HDR EXR can be slow and we MUST "
            "NOT switch clips while Neat is open or Resolve will hang."
        ),
    )
    parser.add_argument(
        "--profile-wait", type=float, default=0.5, help="Seconds to wait after Auto Profile click"
    )
    parser.add_argument(
        "--ready-timeout", type=float, default=90.0, help="Seconds to wait for profile readiness"
    )
    parser.add_argument(
        "--apply-delay",
        type=float,
        default=0.0,
        help=(
            "Seconds to pause after profile readiness before clicking Apply. "
            "Fresh/reset Neat nodes apply on the first concrete ready read; "
            "reused nodes still require a second profiled read to avoid applying "
            "a stale profile that flashes before the fresh rebuild starts."
        ),
    )
    parser.add_argument(
        "--close-timeout",
        type=float,
        default=6.0,
        help="Seconds to wait for the Neat window to close after Apply",
    )
    parser.add_argument(
        "--step-delay", type=float, default=0.2, help="Seconds to pause between UI actions"
    )
    parser.add_argument(
        "--resume-open-neat",
        action="store_true",
        help="If a Neat UI is already open, close it instead of iterating timeline clips (recovery)",
    )
    parser.add_argument("--_dump-neat-inputs", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(reuse_existing_neat=True)
    parser.add_argument(
        "--reuse-existing-neat",
        dest="reuse_existing_neat",
        action="store_true",
        help="Reuse an existing Neat node instead of adding a fresh one (default)",
    )
    parser.add_argument(
        "--fresh-neat",
        dest="reuse_existing_neat",
        action="store_false",
        help="Add a fresh Neat node even if the clip already has one",
    )
    parser.add_argument(
        "--reset",
        dest="reset_neat",
        action="store_true",
        help=(
            "Delete any existing Neat node (and its CST wrap tools) on each "
            "clip's Fusion comp, then re-add a fresh one. Use this to clear a "
            "stale or half-built noise profile and re-run from scratch."
        ),
    )
    parser.set_defaults(no_color_wrap=True)
    parser.add_argument(
        "--color-wrap",
        dest="no_color_wrap",
        action="store_false",
        help=(
            "Wrap Neat in a Linear ↔ PQ ColorSpaceTransform pair so it sees "
            "display-referred pixels for Auto Profile on HDR color-managed "
            "projects. Fires for ACES (AP0 linear ↔ Rec.2020 PQ) and DaVinci "
            "YRGB Color Managed (timeline gamut Linear ↔ PQ at the mastering "
            "luminance). Default is OFF — only the Neat node is added (MediaIn1 "
            "→ Neat → MediaOut1). Enable this when Auto Profile reads near-black "
            "because Fusion is feeding Neat a scene-linear image."
        ),
    )
    parser.add_argument(
        "--color-wrap-scale",
        type=float,
        default=0.125,
        help=(
            "Internal normalization scale used only by --color-wrap before the "
            "Linear→PQ CST. The inverse scale is restored after the PQ→Linear "
            "CST so final output keeps the original linear scale. Default 0.125 "
            "keeps HDR plate values below Fusion CST's 1.0 clip point."
        ),
    )
    parser.add_argument(
        "--set-linear",
        action="store_true",
        help=(
            "Before Auto Profile, explicitly set Neat Video's bottom-left Input "
            "Data Gamma selector to Linear. Default is off because Neat keeps "
            "this preference between clips/runs."
        ),
    )
    parser.add_argument(
        "--no-templates",
        dest="no_templates",
        action="store_true",
        help=(
            "Disable self-calibrating template matching. By default the driver "
            "learns each control's appearance from clips that succeed and then "
            "locates it by deterministic OpenCV template match (OCR fallback). "
            "Pass this to force OCR-only locating."
        ),
    )
    parser.add_argument(
        "--debug", action="store_true", help="Include Python tracebacks in failed JSON results"
    )
    parser.add_argument("--json", action="store_true", help="Print JSON results")
    # autoneat profile drives whatever timeline is already open, so it does not
    # need a project. ``--project`` is accepted (and its basename used) only as
    # the cold-start / self-heal reopen target if Resolve has to be relaunched.
    parser.add_argument("--project", "-p", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--timeline",
        default=None,
        help="Cold-start/self-heal target timeline name (otherwise the open timeline is used)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    # Neat's UI automation needs real screen-capture access. When invoked over
    # SSH or from the farm worker (no window-server connection), re-run this
    # exact command inside a Terminal in the logged-in desktop session and
    # mirror its output back here. We probe the actual capture capability rather
    # than guess from launchctl; AUTONEAT_IN_GUI guards against re-entry so the
    # relaunched run (which has display access) doesn't probe-and-relaunch again.
    if os.environ.get("AUTONEAT_IN_GUI") != "1" and not _display_capturable():
        return _relaunch_in_gui_terminal(list(argv) if argv is not None else sys.argv[1:])

    cfg = DriveConfig.from_args(args)

    if args._dump_neat_inputs and not args.dry_run:
        return _dump_neat_inputs()

    if args.resume_open_neat and not args.dry_run:
        result = _process_open_neat_ui(cfg, args)
        summary: Dict[str, Any] = {
            "ok": bool(result.get("ok")),
            "processed": 1 if result.get("ok") else 0,
            "failed": 0 if result.get("ok") else 1,
            "results": [result],
        }
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            status = "OK" if result.get("ok") else f"FAIL: {result.get('error')}"
            print(f"[resume-open-neat] {status}")
        return 0 if result.get("ok") else 1

    project_hint = None
    if args.project:
        project_hint = str(args.project).rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or None
    try:
        resolve, timeline = _establish_timeline(
            cfg, project_hint=project_hint, timeline_hint=args.timeline
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Capture project + timeline names up front so the freeze watchdog can
    # reopen them after a forced Resolve restart.
    project_name: Optional[str] = None
    timeline_name: Optional[str] = None
    try:
        _proj = resolve.GetProjectManager().GetCurrentProject()
        project_name = _proj.GetName() if _proj else None
        timeline_name = timeline.GetName()
    except Exception:
        pass

    clips = _filter_clips(
        _timeline_clips(timeline, track=args.track, all_tracks=args.all_video_tracks),
        args.shot_ids,
    )

    # Durable, per-(project, timeline) progress file. Survives reboots and is
    # never wiped by a later run — completed clips stay recorded so resuming is
    # always correct (the old shared /tmp/last-run.json was overwritten by every
    # run, which silently discarded earlier completions).
    sidecar = _progress_path(project_name, timeline_name)
    print(f"  progress file: {sidecar}", flush=True)

    skipped_continue: List[str] = []
    # Always carry the existing progress forward so the file stays CUMULATIVE and
    # a completed clip is never re-wiped — independent of --continue, which only
    # controls whether we SKIP already-done clips this run.
    carried_succeeded: set = set()
    carried_failed: set = set()
    if args.reset_progress:
        try:
            sidecar.unlink()
        except OSError:
            pass
        print("  --reset-progress: cleared prior progress; starting fresh", flush=True)
    else:
        prev = _load_sidecar(sidecar)
        if prev is not None:
            carried_succeeded = set(prev.get("succeeded") or [])
            carried_failed = set(prev.get("failed") or [])
            if carried_succeeded or carried_failed:
                print(
                    f"  progress: {len(carried_succeeded)} done, "
                    f"{len(carried_failed)} failed recorded so far",
                    flush=True,
                )
        if args.continue_run:
            skip_names = set(carried_succeeded)
            if not args.retry_failed:
                skip_names.update(carried_failed)
            kept: List[Any] = []
            for clip in clips:
                name = _clip_name(clip)
                if name in skip_names:
                    skipped_continue.append(name)
                else:
                    kept.append(clip)
            clips = kept
            print(
                f"  --continue: skipping {len(skipped_continue)} previously-processed clip(s); "
                f"{len(clips)} remain"
                + (" (failed clips will be retried)" if args.retry_failed else ""),
                flush=True,
            )

    if args.start_from > 1:
        clips = clips[args.start_from - 1 :]
    if args.limit:
        clips = clips[: args.limit]

    # Identity of each clip (name, start, track) captured while the handles are
    # alive. A Resolve relaunch (watchdog or failed-streak restart) kills every
    # handle in `clips`, so we re-fetch fresh ones in this exact order by
    # identity — see _refetch_all_clips.
    clip_idents: List[Tuple[str, Optional[int], int]] = [
        (_clip_name(c), _clip_start(c), _clip_track_index(c)) for c in clips
    ]

    if args.dry_run:
        for idx, clip in enumerate(clips, 1):
            print(f"[{idx}] {_clip_name(clip)} start={clip.GetStart()} dur={clip.GetDuration()}")
        return 0

    def _write_sidecar(results_so_far: List[Dict[str, Any]], *, partial: bool) -> None:
        succ = [r for r in results_so_far if r.get("ok")]
        fail = [r for r in results_so_far if not r.get("ok")]
        # Fold this run's results into the carried tallies so the sidecar is the
        # cumulative done-set across all --continue runs, not just this one.
        succ_names = carried_succeeded | {r.get("clip", "") for r in succ}
        fail_names = (carried_failed | {r.get("clip", "") for r in fail}) - succ_names
        succ_names.discard("")
        fail_names.discard("")
        snap = {
            "ok": (not fail_names) and not partial,
            "partial": partial,
            "processed": len(succ_names),
            "failed_count": len(fail_names),
            "succeeded": sorted(succ_names),
            "failed": sorted(fail_names),
            "succeeded_ids": [_shot_id_from_name(n) for n in sorted(succ_names)],
            "failed_ids": [_shot_id_from_name(n) for n in sorted(fail_names)],
            "skipped_via_continue": skipped_continue,
            "results": results_so_far,
        }
        try:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps(snap, indent=2, sort_keys=True))
        except Exception as exc:
            print(f"  warning: could not write sidecar {sidecar}: {exc}", flush=True)

    # Per-index result slots so a restart can rewind and overwrite the failed
    # streak's results in place (rather than appending duplicates).
    results_by_idx: List[Optional[Dict[str, Any]]] = [None] * len(clips)
    batch_start = time.time()
    consecutive_failures = 0
    breaker_tripped = False
    streak_start: Optional[int] = None  # 0-based index of first failure in the current streak
    streak_restarts = 0  # Resolve restarts already spent on the current streak
    watchdog_ctx: Dict[str, Any] = {
        "project_name": project_name,
        "timeline_name": timeline_name,
        "restarts_used": 0,
    }

    def _compact() -> List[Dict[str, Any]]:
        return [r for r in results_by_idx if r is not None]

    i = 0
    while i < len(clips):
        idx = i + 1
        # Resolve can die on its OWN between clips (e.g. a wedged Neat window
        # crashes it). The per-clip watchdog only fires on a 360s in-clip hang,
        # and dereferencing a dead handle below would block forever — so probe
        # the process here BEFORE touching clips[i]. If it's gone, relaunch +
        # re-fetch handles, exactly like the watchdog path. (This is the gap that
        # let a self-crashed Resolve hang the whole batch with <3 failures.)
        if cfg.auto_restart and not _resolve_running():
            if watchdog_ctx["restarts_used"] >= cfg.max_restarts:
                print(
                    "  WATCHDOG: Resolve not running and restart budget exhausted — stopping.",
                    file=sys.stderr,
                    flush=True,
                )
                breaker_tripped = True
                break
            watchdog_ctx["restarts_used"] += 1
            print(
                f"\n  WATCHDOG: Resolve is not running (crashed between clips) — relaunching "
                f"(restart {watchdog_ctx['restarts_used']}/{cfg.max_restarts}) before clip {idx}…",
                flush=True,
            )
            try:
                resolve, timeline = _restart_resolve(project_name, timeline_name, cfg)
                clips = _refetch_all_clips(
                    timeline, clip_idents, track=args.track, all_tracks=args.all_video_tracks
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  WATCHDOG: relaunch failed: {exc}", file=sys.stderr, flush=True)
                breaker_tripped = True
                break

        clip = clips[i]
        name = _clip_name(clip) if clip is not None else clip_idents[i][0]
        sid = _shot_id_from_name(name)
        # A None slot means a fresh handle couldn't be located after a relaunch.
        # Record a clean per-clip failure rather than crashing the whole batch on
        # a dead handle (the old failure mode: 'NoneType' object is not callable).
        if clip is None:
            result = {
                "ok": False,
                "clip": name,
                "error": "clip handle missing after Resolve relaunch (not found on reopened timeline)",
                "elapsed_seconds": 0.0,
            }
            results_by_idx[i] = result
            consecutive_failures += 1
            if streak_start is None:
                streak_start = i
            print(f"\n[{idx}/{len(clips)}] {sid}  → FAIL (handle lost after restart)", flush=True)
            _write_sidecar(_compact(), partial=True)
            i += 1
            continue
        print(
            f"\n[{idx}/{len(clips)}] {sid}  (clip={name}, track={_clip_track_index(clip)}, "
            f"start={_clip_start(clip)}, dur={_clip_duration(clip)})",
            flush=True,
        )
        clip_start = time.time()
        restarts_before = watchdog_ctx["restarts_used"]
        result, resolve, timeline = _process_clip_watchdog(
            resolve, timeline, clip, cfg, args, watchdog_ctx
        )
        # If the watchdog relaunched Resolve mid-clip, every remaining handle in
        # `clips` is now dead — re-fetch the whole list (fresh handles, same
        # order) from the reopened timeline before touching clips[i+1...].
        if watchdog_ctx["restarts_used"] > restarts_before:
            clips = _refetch_all_clips(
                timeline, clip_idents, track=args.track, all_tracks=args.all_video_tracks
            )
        result.setdefault("elapsed_seconds", round(time.time() - clip_start, 1))
        results_by_idx[i] = result
        elapsed = result.get("elapsed_seconds", round(time.time() - clip_start, 1))

        if result.get("ok"):
            consecutive_failures = 0
            streak_start = None
            streak_restarts = 0
            print(f"  → OK ({elapsed:.1f}s, {len(result.get('steps') or [])} steps)", flush=True)
            # Persist after every good clip so a later forced restart loses at
            # most the in-flight clip (which the watchdog retries anyway).
            if cfg.auto_restart:
                _save_project(resolve)
            i += 1
            _write_sidecar(_compact(), partial=i < len(clips))
            continue

        # --- Failure path ---
        consecutive_failures += 1
        if streak_start is None:
            streak_start = i
        print(f"  → FAIL ({elapsed:.1f}s): {result.get('error')}", flush=True)
        if args.debug and result.get("traceback"):
            for line in str(result["traceback"]).rstrip().splitlines():
                print(f"    {line}", flush=True)
        _write_sidecar(_compact(), partial=True)

        # A run of per-clip failures (e.g. node-add timeouts) almost always
        # means Resolve's scripting got wedged — even when no single clip
        # exceeded the wall-clock watchdog. Rather than stop, force-restart
        # Resolve and retry the whole failed streak. Bounded by the per-streak
        # and whole-batch restart budgets.
        should_restart = (
            cfg.auto_restart
            and (
                consecutive_failures >= cfg.restart_after_failures or _needs_resolve_restart(result)
            )
            and watchdog_ctx["restarts_used"] < cfg.max_restarts
            and streak_restarts < cfg.max_clip_restarts
        )
        if should_restart:
            watchdog_ctx["restarts_used"] += 1
            streak_restarts += 1
            print(
                f"\n  WATCHDOG: {consecutive_failures} clips failed in a row — Resolve "
                f"is likely wedged. Force-restarting Resolve (restart "
                f"{watchdog_ctx['restarts_used']}/{cfg.max_restarts}) and retrying from "
                f"clip {streak_start + 1}…",
                flush=True,
            )
            try:
                resolve, timeline = _restart_resolve(
                    watchdog_ctx.get("project_name"), watchdog_ctx.get("timeline_name"), cfg
                )
                # The relaunch killed every handle in `clips`; re-fetch fresh ones
                # before rewinding so the retried streak uses live items.
                clips = _refetch_all_clips(
                    timeline, clip_idents, track=args.track, all_tracks=args.all_video_tracks
                )
                print("  WATCHDOG: Resolve relaunched; retrying the failed streak.", flush=True)
                for j in range(streak_start, i + 1):
                    results_by_idx[j] = None
                i = streak_start
                consecutive_failures = 0
                streak_start = None
                _write_sidecar(_compact(), partial=True)
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"  WATCHDOG: Resolve restart failed: {exc}", file=sys.stderr, flush=True)
                # Fall through to the hard breaker.

        # Hard breaker: restarts disabled/exhausted and still failing in a run.
        if (
            args.max_consecutive_failures
            and consecutive_failures >= args.max_consecutive_failures
            and idx < len(clips)
        ):
            breaker_tripped = True
            print(
                f"\nERROR: {consecutive_failures} clips failed in a row and auto-restart "
                "could not recover — stopping the batch early. Fix Resolve (restart it "
                "if needed), then resume with: autoneat profile --continue --retry-failed",
                file=sys.stderr,
                flush=True,
            )
            break
        i += 1

    print(f"\nBatch elapsed: {time.time() - batch_start:.1f}s", flush=True)

    results = _compact()
    succeeded = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    succeeded_ids = [_shot_id_from_name(r.get("clip", "")) for r in succeeded]
    failed_ids = [_shot_id_from_name(r.get("clip", "")) for r in failed]

    _write_sidecar(results, partial=breaker_tripped)

    locate_methods = _locate_method_counts(results)
    summary = {
        "ok": (not failed) and not breaker_tripped,
        "processed": len(succeeded),
        "failed": len(failed),
        "stopped_early": breaker_tripped,
        "succeeded_ids": succeeded_ids,
        "failed_ids": failed_ids,
        "skipped_via_continue": skipped_continue,
        "locate_methods": locate_methods,
        "results": results,
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print()
        print(
            "Neat batch stopped early (circuit breaker)."
            if breaker_tripped
            else "Neat batch complete:"
        )
        if skipped_continue:
            print(f"  Skipped (--continue): {len(skipped_continue)}")
        print(f"  Processed: {len(succeeded)}")
        if succeeded_ids:
            print(f"  Succeeded IDs: {', '.join(succeeded_ids)}")
        print(f"  Failed: {len(failed)}")
        if failed_ids:
            print(f"  Failed IDs: {', '.join(failed_ids)}")
            for r in failed:
                sid = _shot_id_from_name(r.get("clip", ""))
                print(f"    {sid}: {r.get('error')}")
        print(
            f"  Locates: {locate_methods['template']} template, "
            f"{locate_methods['window']} window, {locate_methods['fullscreen']} OCR"
        )
        print(f"  Results JSON: {sidecar}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
