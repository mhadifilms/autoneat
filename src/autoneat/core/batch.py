"""Per-clip processing flow + batch runner.

The flow for one clip is:

  1. Move the playhead to the clip on the Edit page.
  2. Add (or find) a Neat OFX node on the clip's Fusion comp, optionally
     wrapped in CST tools for ACES + HDR projects, and fire the OFX
     ``Prepare Profile___`` ButtonControl to open Neat's main window.
  3. Wait for Neat to advance past splash / info-dialog / preparing-input
     into the editor state.
  4. Click "Auto Profile", wait for the profile to be ready, click "Apply".
  5. Wait for the Neat window to close.

There are no fallbacks at any step. If a state machine times out or an OCR
locate misses, the clip is reported as failed with the actual cause.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from autoneat.core import neat_ofx, resolve_client, ui_driver, windows
from autoneat.core.ocr import cache_base
from autoneat.core.recorder import StepRecorder
from autoneat.core.shotid import filter_clips, shot_id_from_name


# ---------------------------------------------------------------------------
# Settings dataclass — used by both CLI and GUI
# ---------------------------------------------------------------------------


@dataclass
class BatchSettings:
    track: int = 1
    all_video_tracks: bool = False
    shot_ids: List[str] = field(default_factory=list)
    start_from: int = 1
    limit: int = 0
    continue_run: bool = False
    retry_failed: bool = False
    reuse_existing_neat: bool = True
    no_color_wrap: bool = False
    open_timeout: float = 18.0
    editor_timeout: float = 60.0
    prepare_timeout: float = 1800.0
    profile_wait: float = 3.0
    ready_timeout: float = 90.0
    apply_delay: float = 5.0
    close_timeout: float = 20.0
    step_delay: float = 1.0
    sidecar_path: Optional[Path] = None

    def sidecar(self) -> Path:
        if self.sidecar_path is not None:
            return self.sidecar_path
        env_override = os.environ.get("AUTONEAT_RESULTS_JSON")
        if env_override:
            return Path(env_override)
        return Path.home() / ".cache" / "autoneat" / "last-run.json"


# ---------------------------------------------------------------------------
# Per-clip state machine
# ---------------------------------------------------------------------------


def _state(work_dir: Path) -> tuple:
    windows.activate_resolve(settle=0)
    time.sleep(0.15)
    return ui_driver.read_screen_state(work_dir)


def _open_neat_state(work_dir: Path) -> tuple:
    state, text, rows = _state(work_dir)
    if state in {
        "demo-splash",
        "information-dialog",
        "preparing-input",
        "editor-unprofiled",
        "editor-profiled",
        "editor",
    }:
        return state, text, rows
    return "unknown", text, rows


def _dismiss_information_dialog(
    work_dir: Path,
    calibration: ui_driver.UiCalibration,
    rec: StepRecorder,
    *,
    prefix: str,
    step_delay: float,
) -> None:
    point, method = ui_driver.click_control("ok", work_dir, calibration)
    rec.add(f"{prefix}-ok:{method}:{round(point[0])},{round(point[1])}")
    time.sleep(step_delay)


def _advance_to_editor(
    work_dir: Path,
    calibration: ui_driver.UiCalibration,
    rec: StepRecorder,
    *,
    timeout: float,
    prepare_timeout: float,
    step_delay: float,
) -> None:
    """Advance Neat past splash / info / preparing-input → editor state.

    Two timeout budgets:
      * ``timeout`` — how long *non*-prepare states can stick.
      * ``prepare_timeout`` — how long the "Resolve is preparing input
        frames…" phase may take. 4K HDR EXR sequences legitimately take
        minutes; we must NOT switch clips while Neat is open or Resolve
        will hang.
    """
    deadline = time.time() + timeout
    prepare_deadline: Optional[float] = None
    last_text = ""
    acted_states: set = set()
    last_polled_state: Optional[str] = None
    while True:
        state, text, _rows = _state(work_dir)
        last_text = text
        if state in {"editor-unprofiled", "editor-profiled", "editor"}:
            rec.add(f"editor:{state}")
            return

        if state == "preparing-input":
            if prepare_deadline is None:
                prepare_deadline = time.time() + prepare_timeout
                rec.add(f"preparing-input:wait-up-to-{prepare_timeout:.0f}s")
            elif time.time() > prepare_deadline:
                raise RuntimeError(
                    f"Frame prep exceeded {prepare_timeout:.0f}s; last_text={last_text!r}"
                )
            deadline = max(deadline, time.time() + timeout)
        else:
            prepare_deadline = None
            if time.time() > deadline:
                raise RuntimeError(
                    f"Timed out waiting for Neat editor (last_state={state}, last_text={last_text!r})"
                )

        if state != last_polled_state:
            rec.add(f"poll-state:{state}")
            last_polled_state = state
        if state == "inspector-prepare":
            raise RuntimeError(
                "Resolve Inspector is showing 'Prepare Noise Profile' — the OFX "
                "`Prepare Profile___` SetInput did not open Neat's window. Fix "
                "the open path; do not OCR the Inspector."
            )
        if state == "demo-splash":
            if state not in acted_states:
                point, method = ui_driver.click_control("continue", work_dir, calibration)
                rec.add(f"continue:{method}:{round(point[0])},{round(point[1])}")
                acted_states.add(state)
                time.sleep(step_delay)
            continue
        if state == "information-dialog":
            if state not in acted_states:
                _dismiss_information_dialog(work_dir, calibration, rec, prefix="info", step_delay=step_delay)
                acted_states.add(state)
            continue
        time.sleep(step_delay)


def _profile_and_apply(
    work_dir: Path,
    calibration: ui_driver.UiCalibration,
    rec: StepRecorder,
    *,
    profile_wait: float,
    ready_timeout: float,
    step_delay: float,
    apply_delay: float,
    close_timeout: float,
) -> None:
    state, _text, _rows = _state(work_dir)
    if state == "editor-unprofiled":
        point, method = ui_driver.click_control("auto-profile", work_dir, calibration)
        rec.add(f"auto-profile:{method}:{round(point[0])},{round(point[1])}")
        time.sleep(max(profile_wait, step_delay))

    rec.add(f"wait-profile-ready:up-to-{ready_timeout:.0f}s")
    deadline = time.time() + ready_timeout
    while time.time() < deadline:
        state, _text, _rows = _state(work_dir)
        if state in {"editor-profiled", "editor"}:
            rec.add(f"profile-ready:{state}")
            break
        if state == "information-dialog":
            _dismiss_information_dialog(work_dir, calibration, rec, prefix="warning", step_delay=step_delay)
            continue
        time.sleep(step_delay)
    else:
        state, text, _rows = _state(work_dir)
        if state not in {"editor-profiled", "editor"}:
            raise RuntimeError(f"Timed out waiting for Neat profile readiness (last_state={state}, last_text={text!r})")

    if apply_delay > 0:
        rec.add(f"apply-delay:{apply_delay:.1f}s")
        time.sleep(apply_delay)

    point, method = ui_driver.click_control("apply", work_dir, calibration)
    rec.add(f"apply:{method}:{round(point[0])},{round(point[1])}")
    time.sleep(step_delay)

    rec.add(f"wait-neat-close:up-to-{close_timeout:.0f}s")
    close_deadline = time.time() + close_timeout
    closed = False
    while time.time() < close_deadline:
        try:
            win_list = windows.list_resolve_windows(activate=False)
        except Exception:
            win_list = []
        if windows.find_neat_window(windows=win_list) is None:
            closed = True
            break
        time.sleep(step_delay)
    if not closed:
        raise RuntimeError(f"Neat window did not close within {close_timeout:.1f}s after Apply")
    rec.add("neat-window-closed")


def _drive_open_neat(
    calibration: ui_driver.UiCalibration,
    rec: StepRecorder,
    settings: BatchSettings,
    *,
    label: str,
) -> Optional[str]:
    """Drive whatever Neat window is currently open to Apply + close.
    Used as a recovery action when a stale Neat window is detected at the
    start of a per-clip run.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="neat-resume-", dir=str(cache_base())) as tmp:
            work_dir = Path(tmp)
            state, text, _rows = _open_neat_state(work_dir)
            if state == "unknown":
                rec.add(f"{label}:already-closed text={text[:60]!r}")
                return None
            rec.add(f"{label}:state={state}")
            if state not in {"editor-unprofiled", "editor-profiled", "editor"}:
                _advance_to_editor(
                    work_dir,
                    calibration,
                    rec,
                    timeout=settings.editor_timeout,
                    prepare_timeout=settings.prepare_timeout,
                    step_delay=settings.step_delay,
                )
            _profile_and_apply(
                work_dir,
                calibration,
                rec,
                profile_wait=settings.profile_wait,
                ready_timeout=settings.ready_timeout,
                step_delay=settings.step_delay,
                apply_delay=settings.apply_delay,
                close_timeout=settings.close_timeout,
            )
        return None
    except Exception as exc:
        rec.add(f"{label}:FAILED {exc}")
        return str(exc)


def process_clip(
    resolve: Any,
    timeline: Any,
    project: Any,
    clip: Any,
    calibration: ui_driver.UiCalibration,
    settings: BatchSettings,
    sink: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    name = resolve_client.clip_name(clip)
    rec = StepRecorder(sink=sink)

    # If a stale Neat window is open from a prior run, drive it to apply
    # and close before touching the new target. (User-requested behavior:
    # don't panic and refuse — finish what's open first.)
    try:
        win_list = windows.list_resolve_windows(activate=False)
    except Exception:
        win_list = []
    stale = windows.find_neat_window(windows=win_list)
    if stale is not None:
        rec.add(f"stale-neat-detected:{stale.get('name')!r}")
        _drive_open_neat(calibration, rec, settings, label="resume-stale")

    with tempfile.TemporaryDirectory(prefix="neat-batch-", dir=str(cache_base())) as tmp:
        work_dir = Path(tmp)

        playhead = resolve_client.set_playhead_to_clip(resolve, timeline, clip)
        rec.add(f"playhead:tc={playhead.get('timecode')} matched={playhead.get('matched')}")
        time.sleep(settings.step_delay)

        rec.add("attach-neat:add-or-select-node")
        opened = neat_ofx.attach_neat_to_clip(
            clip,
            project,
            reuse_existing=settings.reuse_existing_neat,
            no_color_wrap=settings.no_color_wrap,
        )
        rec.add(f"attach-neat:OK tool={opened.get('tool')}")
        wrap = opened.get("color_wrap") or {}
        if wrap.get("applied"):
            rec.add(
                f"color-wrap:applied {wrap.get('in_cs')}/{wrap.get('in_gamma')} "
                f"\u2194 {wrap.get('out_cs')}/{wrap.get('out_gamma')} (nits={wrap.get('nits')})"
            )
        elif wrap:
            rec.add(f"color-wrap:skipped reason={wrap.get('skip_reason') or 'unknown'}")
        time.sleep(settings.step_delay)

        _advance_to_editor(
            work_dir,
            calibration,
            rec,
            timeout=settings.editor_timeout,
            prepare_timeout=settings.prepare_timeout,
            step_delay=settings.step_delay,
        )
        _profile_and_apply(
            work_dir,
            calibration,
            rec,
            profile_wait=settings.profile_wait,
            ready_timeout=settings.ready_timeout,
            step_delay=settings.step_delay,
            apply_delay=settings.apply_delay,
            close_timeout=settings.close_timeout,
        )

    return {
        "ok": True,
        "clip": name,
        "steps": rec.steps,
        "playhead": playhead,
        "open": opened,
        "elapsed_seconds": round(rec.elapsed(), 1),
    }


# ---------------------------------------------------------------------------
# Sidecar helpers (resume / continue support)
# ---------------------------------------------------------------------------


def load_sidecar(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_sidecar(
    path: Path,
    results: Sequence[Dict[str, Any]],
    *,
    partial: bool,
    skipped_via_continue: Sequence[str],
    calibration: ui_driver.UiCalibration,
) -> None:
    succ = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    snap = {
        "ok": (not fail) and not partial,
        "partial": partial,
        "processed": len(succ),
        "failed_count": len(fail),
        "succeeded": [r.get("clip", "") for r in succ],
        "failed": [r.get("clip", "") for r in fail],
        "succeeded_ids": [shot_id_from_name(r.get("clip", "")) for r in succ],
        "failed_ids": [shot_id_from_name(r.get("clip", "")) for r in fail],
        "skipped_via_continue": list(skipped_via_continue),
        "calibration": calibration.as_dict(),
        "results": list(results),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Top-level batch runner
# ---------------------------------------------------------------------------


def run_batch(
    resolve: Any,
    project: Any,
    timeline: Any,
    settings: BatchSettings,
    *,
    sink: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run the batch end-to-end against a Resolve handle that's already
    been resolved by the caller (the WFI script's pre-injected globals).

    ``sink`` receives every step description (one line at a time) and every
    summary line. ``cancel_event`` is any object with an ``is_set()`` method
    (e.g. ``threading.Event``); the runner checks it between clips.
    """
    log = sink or (lambda s: print(s, flush=True))
    log(f"Connected to Resolve: project={project.GetName()!r} timeline={timeline.GetName()!r}")

    clips = filter_clips(
        resolve_client.timeline_clips(
            timeline,
            track=settings.track,
            all_video_tracks=settings.all_video_tracks,
        ),
        settings.shot_ids,
        name_of=resolve_client.clip_name,
    )

    sidecar = settings.sidecar()
    skipped_continue: List[str] = []
    if settings.continue_run:
        prev = load_sidecar(sidecar)
        if prev is None:
            log(f"  warning: continue requested but no readable sidecar at {sidecar}")
        else:
            skip_names = set(prev.get("succeeded") or [])
            if not settings.retry_failed:
                skip_names.update(prev.get("failed") or [])
            kept: List[Any] = []
            for clip in clips:
                name = resolve_client.clip_name(clip)
                if name in skip_names:
                    skipped_continue.append(name)
                else:
                    kept.append(clip)
            clips = kept
            extra = " (failed clips will be retried)" if settings.retry_failed else ""
            log(
                f"  continue: skipping {len(skipped_continue)} previously-processed clip(s); "
                f"{len(clips)} remain{extra}"
            )

    if settings.start_from > 1:
        clips = clips[settings.start_from - 1 :]
    if settings.limit:
        clips = clips[: settings.limit]

    calibration = ui_driver.UiCalibration()
    results: List[Dict[str, Any]] = []
    batch_start = time.time()

    for idx, clip in enumerate(clips, 1):
        if cancel_event is not None and cancel_event.is_set():
            log(f"  cancel requested — stopping after {idx-1} clip(s)")
            break

        name = resolve_client.clip_name(clip)
        sid = shot_id_from_name(name)
        log(
            f"\n[{idx}/{len(clips)}] {sid}  (clip={name}, "
            f"track={resolve_client.clip_track_index(clip)}, "
            f"start={int(clip.GetStart())}, dur={int(clip.GetDuration())})"
        )
        clip_start = time.time()
        try:
            result = process_clip(resolve, timeline, project, clip, calibration, settings, sink=log)
        except Exception as exc:
            result = {
                "ok": False,
                "clip": name,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "elapsed_seconds": round(time.time() - clip_start, 1),
            }
        results.append(result)
        elapsed = result.get("elapsed_seconds", round(time.time() - clip_start, 1))
        if result.get("ok"):
            log(f"  → OK ({elapsed:.1f}s, {len(result.get('steps') or [])} steps)")
        else:
            log(f"  → FAIL ({elapsed:.1f}s): {result.get('error')}")
        write_sidecar(
            sidecar,
            results,
            partial=idx < len(clips),
            skipped_via_continue=skipped_continue,
            calibration=calibration,
        )

    log(f"\nBatch elapsed: {time.time() - batch_start:.1f}s")
    succeeded = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    write_sidecar(
        sidecar,
        results,
        partial=False,
        skipped_via_continue=skipped_continue,
        calibration=calibration,
    )

    summary = {
        "ok": not failed,
        "processed": len(succeeded),
        "failed": len(failed),
        "succeeded_ids": [shot_id_from_name(r.get("clip", "")) for r in succeeded],
        "failed_ids": [shot_id_from_name(r.get("clip", "")) for r in failed],
        "skipped_via_continue": skipped_continue,
        "calibration": calibration.as_dict(),
        "sidecar_path": str(sidecar),
        "results": results,
    }
    return summary
