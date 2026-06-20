"""DaVinci Resolve helpers for timeline iteration and playhead control."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple


def current_project_and_timeline(resolve: Any) -> Tuple[Any, Any]:
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        raise RuntimeError("No current Resolve project")
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        raise RuntimeError("No current Resolve timeline")
    return project, timeline


def timeline_clips(timeline: Any, *, track: int = 1, all_video_tracks: bool = False) -> List[Any]:
    """Return timeline items in start-frame order, on one track or every video track."""
    if all_video_tracks:
        count = int(timeline.GetTrackCount("video") or 0)
        track_indices = range(1, count + 1)
    else:
        track_indices = range(track, track + 1)

    clips: List[Any] = []
    for idx in track_indices:
        for item in timeline.GetItemListInTrack("video", idx) or []:
            clips.append(item)
    clips.sort(key=lambda clip: (int(clip.GetStart()), clip.GetName() or ""))
    return clips


def clip_name(clip: Any) -> str:
    return clip.GetName() or "<unnamed>"


def clip_track_index(clip: Any) -> int:
    """Best-effort video track index for a TimelineItem (defaults to 1)."""
    track_type, track_index = clip.GetTrackTypeAndIndex()
    if str(track_type).lower() == "video":
        return int(track_index)
    return 1


def frame_to_timecode(frame: int, timeline: Any) -> str:
    """SMPTE TC for a timeline frame, honouring the timeline's start TC."""
    fps = round(float(timeline.GetSetting("timelineFrameRate")))
    start_tc = timeline.GetStartTimecode()
    parts = start_tc.split(":")
    start_frame = (
        int(parts[0]) * 3600 * fps
        + int(parts[1]) * 60 * fps
        + int(parts[2]) * fps
        + int(parts[3])
    )
    target = start_frame + (frame - int(timeline.GetStartFrame()))
    h = int(target // (3600 * fps))
    rem = target - h * 3600 * fps
    m = int(rem // (60 * fps))
    rem -= m * 60 * fps
    s = int(rem // fps)
    f = int(rem - s * fps)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def set_playhead_to_clip(resolve: Any, timeline: Any, clip: Any) -> Dict[str, Any]:
    """Move the Resolve playhead to the middle of ``clip`` so it becomes the
    current Edit-page video item. Returns a small status dict for logging."""
    resolve.OpenPage("edit")
    time.sleep(0.2)
    duration = int(clip.GetDuration())
    mid_frame = int(clip.GetStart()) + max(1, duration // 2)
    tc = frame_to_timecode(mid_frame, timeline)
    set_ok = timeline.SetCurrentTimecode(tc)
    time.sleep(0.35)
    current = timeline.GetCurrentVideoItem()
    return {
        "set_timecode": bool(set_ok),
        "timecode": tc,
        "current": current.GetName() if current else None,
        "matched": bool(current and current.GetName() == clip.GetName()),
    }
