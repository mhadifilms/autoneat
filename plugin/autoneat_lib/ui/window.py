"""UIManager-based GUI — runs inside DaVinci Resolve.

Resolve's UIManager is a Qt-based UI layer exposed by Fusion. We use it
because (a) it's bundled with Resolve, so the plugin installs with zero
Python dependencies, and (b) cross-thread updates from a background
worker thread to UI elements work via ``win.QueueEvent(...)`` — the same
pattern Qt uses for its event loop.

Layout:

  ┌──────────────────────────────────────────────────────────────────┐
  │ autoneat — Batch Auto-Profile for Neat Video                 │
  ├────────────────────────────┬─────────────────────────────────────┤
  │ Resolve / Filter / Options │ Clips on the timeline               │
  │ (sidebar form)             ├─────────────────────────────────────┤
  │                            │ Live log                            │
  │              [Refresh]     │                                     │
  │              [Start]       │                                     │
  │              [Cancel]      │                                     │
  └────────────────────────────┴─────────────────────────────────────┘
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from autoneat_lib.core import resolve_client
from autoneat_lib.core.batch import BatchSettings, run_batch
from autoneat_lib.core.shotid import filter_clips, shot_id_from_name


WIN_ID = "com.mhadifilms.autoneat.window"

# Element IDs
CLIPS_TREE = "clips"
LOG_TEXT = "log"
START_BTN = "start"
CANCEL_BTN = "cancel"
REFRESH_BTN = "refresh"
STATUS_LBL = "status"
PROJECT_LBL = "project"
TIMELINE_LBL = "timeline"
CLIPCOUNT_LBL = "clipCount"

# Form field IDs
SHOT_IDS = "shotIds"
TRACK = "track"
ALL_TRACKS = "allTracks"
START_FROM = "startFrom"
LIMIT = "limit"
CONTINUE = "continueRun"
RETRY_FAILED = "retryFailed"
REUSE = "reuse"
NO_COLOR_WRAP = "noColorWrap"
APPLY_DELAY = "applyDelay"
PREPARE_TIMEOUT = "prepareTimeout"

# Custom event used for thread-safe log appends from the worker thread.
APPEND_LOG_EVENT = "AppendLog"
PROGRESS_EVENT = "Progress"
FINISH_EVENT = "Finish"


# ---------------------------------------------------------------------------
# Stylesheet — applied to the top-level window so it cascades to children
# ---------------------------------------------------------------------------

STYLESHEET = """
QWidget {
    background-color: #1c1f24;
    color: #d8dde6;
    font-family: -apple-system, "SF Pro Text", "Helvetica Neue", "Segoe UI", sans-serif;
    font-size: 13px;
}
QLabel#title {
    font-size: 18px;
    font-weight: 600;
    color: #f5f7fa;
}
QLabel#section {
    font-size: 11px;
    font-weight: 600;
    color: #a5adbb;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
QLabel#status_ok    { color: #6fcf97; font-weight: 600; }
QLabel#status_warn  { color: #f2c94c; font-weight: 600; }
QLabel#status_bad   { color: #eb5757; font-weight: 600; }
QLabel#muted        { color: #8d95a3; font-size: 12px; }
QFrame#card {
    background-color: #21252c;
    border: 1px solid #2c313a;
    border-radius: 8px;
}
QPushButton {
    background-color: #2b313b;
    border: 1px solid #353b46;
    border-radius: 6px;
    padding: 7px 14px;
    color: #e7eaf0;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #333a47;
    border-color: #424a58;
}
QPushButton:disabled {
    color: #5a6271;
    background-color: #21252c;
    border-color: #2a2e36;
}
QPushButton#primary {
    background-color: #3b6cf0;
    border-color: #3b6cf0;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primary:hover { background-color: #4a78f5; border-color: #4a78f5; }
QPushButton#primary:disabled { background-color: #2c3a5a; border-color: #2c3a5a; color: #7d89a4; }
QPushButton#danger {
    background-color: #b8423a;
    border-color: #b8423a;
    color: #ffffff;
}
QPushButton#danger:hover { background-color: #c95048; border-color: #c95048; }
QLineEdit, QSpinBox, QDoubleSpinBox {
    background-color: #181b20;
    border: 1px solid #2a2f38;
    border-radius: 5px;
    padding: 4px 7px;
    color: #e0e4ec;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #3b6cf0; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: 1px solid #3a414c;
    border-radius: 3px;
    background-color: #181b20;
}
QCheckBox::indicator:checked { background-color: #3b6cf0; border-color: #3b6cf0; }
QTreeWidget {
    background-color: #181b20;
    alternate-background-color: #1c2026;
    border: 1px solid #262a32;
    border-radius: 6px;
    color: #d3d9e4;
}
QTreeWidget::item:selected { background-color: #2b3550; color: #ffffff; }
QHeaderView::section {
    background-color: #1c2026;
    color: #a5adbb;
    border: none;
    border-bottom: 1px solid #2a2e36;
    padding: 5px 8px;
    font-weight: 600;
    font-size: 11px;
}
QTextEdit, QPlainTextEdit {
    background-color: #11131a;
    border: 1px solid #1f242c;
    border-radius: 6px;
    color: #d3d9e4;
    font-family: "SF Mono", "Menlo", "Consolas", monospace;
    font-size: 12px;
}
"""


# ---------------------------------------------------------------------------
# Window builder
# ---------------------------------------------------------------------------


class _WindowState:
    """Mutable state shared between event handlers and the worker thread.

    Kept in a plain object instead of module globals so a future
    'open multiple instances per timeline' feature is a refactor away
    rather than a rewrite.
    """

    def __init__(self) -> None:
        self.resolve: Any = None
        self.project: Any = None
        self.timeline: Any = None
        self.fusion: Any = None
        self.bmd: Any = None
        self.dispatcher: Any = None
        self.win: Any = None
        self.cancel_event = threading.Event()
        self.worker_thread: Optional[threading.Thread] = None
        self.clip_index_by_name: Dict[str, int] = {}


def _build_layout(ui: Any) -> Any:
    """Construct the window layout with UIManager VGroup/HGroup primitives."""
    sidebar = ui.VGroup({"Spacing": 12, "Weight": 0}, [
        ui.Label({"ID": "appTitle", "Text": "autoneat", "ObjectName": "title", "Weight": 0}),
        ui.Label({"ID": "appSubtitle", "Text": "Batch Auto-Profile for Neat Video",
                   "ObjectName": "muted", "Weight": 0}),
        ui.VGap(4),

        # ─── Resolve card ───────────────────────────────────────────
        ui.Frame({"ObjectName": "card", "Weight": 0}, [
            ui.VGroup({"Spacing": 6, "MinimumSize": [300, 0]}, [
                ui.Label({"Text": "Resolve", "ObjectName": "section"}),
                ui.Label({"ID": STATUS_LBL, "Text": "Connecting…", "ObjectName": "status_warn"}),
                ui.Label({"ID": PROJECT_LBL, "Text": "Project: —", "ObjectName": "muted"}),
                ui.Label({"ID": TIMELINE_LBL, "Text": "Timeline: —", "ObjectName": "muted"}),
                ui.Label({"ID": CLIPCOUNT_LBL, "Text": "Clips: —", "ObjectName": "muted"}),
                ui.HGroup([
                    ui.Button({"ID": REFRESH_BTN, "Text": "Refresh", "Weight": 0}),
                    ui.HGap(0, 1),
                ]),
            ]),
        ]),

        # ─── Filter card ────────────────────────────────────────────
        ui.Frame({"ObjectName": "card", "Weight": 0}, [
            ui.VGroup({"Spacing": 6}, [
                ui.Label({"Text": "Filter", "ObjectName": "section"}),
                ui.HGroup([
                    ui.Label({"Text": "Shot ids", "Weight": 0}),
                    ui.LineEdit({"ID": SHOT_IDS, "PlaceholderText": "e.g. 590 1003 ABC"}),
                ]),
                ui.HGroup([
                    ui.Label({"Text": "Track", "Weight": 0}),
                    ui.SpinBox({"ID": TRACK, "Minimum": 1, "Maximum": 32, "Value": 1, "Weight": 0}),
                    ui.HGap(8),
                    ui.CheckBox({"ID": ALL_TRACKS, "Text": "All video tracks"}),
                ]),
                ui.HGroup([
                    ui.Label({"Text": "Start from", "Weight": 0}),
                    ui.SpinBox({"ID": START_FROM, "Minimum": 1, "Maximum": 100000, "Value": 1, "Weight": 0}),
                    ui.HGap(8),
                    ui.Label({"Text": "Limit", "Weight": 0}),
                    ui.SpinBox({"ID": LIMIT, "Minimum": 0, "Maximum": 100000, "Value": 0,
                                 "SpecialValueText": "All", "Weight": 0}),
                ]),
            ]),
        ]),

        # ─── Options card ───────────────────────────────────────────
        ui.Frame({"ObjectName": "card", "Weight": 0}, [
            ui.VGroup({"Spacing": 6}, [
                ui.Label({"Text": "Options", "ObjectName": "section"}),
                ui.CheckBox({"ID": CONTINUE, "Text": "Continue from last run"}),
                ui.CheckBox({"ID": RETRY_FAILED, "Text": "Retry previously-failed clips"}),
                ui.CheckBox({"ID": REUSE, "Text": "Reuse existing Neat node", "Checked": True}),
                ui.CheckBox({"ID": NO_COLOR_WRAP, "Text": "Skip ACES color wrap"}),
                ui.HGroup([
                    ui.Label({"Text": "Apply delay", "Weight": 0}),
                    ui.SpinBox({"ID": APPLY_DELAY, "Minimum": 0, "Maximum": 60, "Value": 5,
                                 "Suffix": " s", "Weight": 0}),
                    ui.HGap(8),
                    ui.Label({"Text": "Prepare timeout", "Weight": 0}),
                    ui.SpinBox({"ID": PREPARE_TIMEOUT, "Minimum": 60, "Maximum": 21600, "Value": 1800,
                                 "Suffix": " s", "Weight": 0}),
                ]),
            ]),
        ]),

        ui.VGap(4),
        ui.HGroup([
            ui.Button({"ID": START_BTN, "Text": "Start batch", "ObjectName": "primary"}),
            ui.HGap(6),
            ui.Button({"ID": CANCEL_BTN, "Text": "Cancel", "ObjectName": "danger", "Enabled": False}),
        ]),
        ui.VStretch(0),
    ])

    main_pane = ui.VGroup({"Spacing": 10}, [
        ui.Label({"Text": "Clips on the timeline", "ObjectName": "section"}),
        ui.Tree({
            "ID": CLIPS_TREE,
            "ColumnCount": 4,
            "RootIsDecorated": False,
            "AlternatingRowColors": True,
            "Weight": 3,
        }),
        ui.Label({"Text": "Live log", "ObjectName": "section"}),
        ui.TextEdit({
            "ID": LOG_TEXT,
            "ReadOnly": True,
            "LineWrapMode": "NoWrap",
            "AcceptRichText": True,
            "Weight": 5,
        }),
    ])

    return ui.HGroup({"Spacing": 14}, [
        sidebar,
        main_pane,
    ])


# ---------------------------------------------------------------------------
# Helpers — read settings, populate clip tree, append log lines
# ---------------------------------------------------------------------------


def _settings_from_window(state: _WindowState) -> BatchSettings:
    win = state.win
    raw_ids = win.Find(SHOT_IDS).Text or ""
    shot_ids = [tok for tok in raw_ids.replace(",", " ").split() if tok]
    return BatchSettings(
        track=int(win.Find(TRACK).Value),
        all_video_tracks=bool(win.Find(ALL_TRACKS).Checked),
        shot_ids=shot_ids,
        start_from=int(win.Find(START_FROM).Value),
        limit=int(win.Find(LIMIT).Value),
        continue_run=bool(win.Find(CONTINUE).Checked),
        retry_failed=bool(win.Find(RETRY_FAILED).Checked),
        reuse_existing_neat=bool(win.Find(REUSE).Checked),
        no_color_wrap=bool(win.Find(NO_COLOR_WRAP).Checked),
        apply_delay=float(win.Find(APPLY_DELAY).Value),
        prepare_timeout=float(win.Find(PREPARE_TIMEOUT).Value),
    )


def _refresh_clips(state: _WindowState) -> None:
    """Re-read the timeline and repopulate the clip tree."""
    win = state.win
    win.Find(STATUS_LBL).Text = "Connecting…"
    win.Find(STATUS_LBL).ObjectName = "status_warn"
    try:
        project, timeline = resolve_client.current_project_and_timeline(state.resolve)
        state.project = project
        state.timeline = timeline
        settings = _settings_from_window(state)
        all_clips = resolve_client.timeline_clips(
            timeline,
            track=settings.track,
            all_video_tracks=settings.all_video_tracks,
        )
        filtered = filter_clips(all_clips, settings.shot_ids, name_of=resolve_client.clip_name)
    except Exception as exc:
        win.Find(STATUS_LBL).Text = "Not connected"
        win.Find(STATUS_LBL).ObjectName = "status_bad"
        win.Find(PROJECT_LBL).Text = "Project: —"
        win.Find(TIMELINE_LBL).Text = "Timeline: —"
        win.Find(CLIPCOUNT_LBL).Text = "Clips: —"
        win.Find(LOG_TEXT).Append(f'<span style="color:#eb5757">FAIL probe: {exc}</span>')
        return

    win.Find(STATUS_LBL).Text = "Connected"
    win.Find(STATUS_LBL).ObjectName = "status_ok"
    win.Find(PROJECT_LBL).Text = f"Project: {project.GetName()}"
    win.Find(TIMELINE_LBL).Text = f"Timeline: {timeline.GetName()}"
    win.Find(CLIPCOUNT_LBL).Text = f"Clips: {len(filtered)} / {len(all_clips)}"

    tree = win.Find(CLIPS_TREE)
    tree.Clear()
    tree.SetHeaderLabels(["#", "Track", "Clip", "Frames"])
    state.clip_index_by_name.clear()
    for index, clip in enumerate(filtered, 1):
        item = tree.NewItem()
        name = resolve_client.clip_name(clip)
        item.Text[0] = str(index)
        item.Text[1] = f"V{resolve_client.clip_track_index(clip)}"
        item.Text[2] = name
        item.Text[3] = str(int(clip.GetDuration()))
        tree.AddTopLevelItem(item)
        state.clip_index_by_name[name] = index


_LEVEL_COLORS = (
    ("FATAL", "#ff7676"),
    ("FAIL", "#eb5757"),
    ("→ FAIL", "#eb5757"),
    ("warning", "#f2c94c"),
    ("WARN", "#f2c94c"),
    ("→ OK", "#6fcf97"),
)


def _append_log_line(state: _WindowState, line: str) -> None:
    color = "#d3d9e4"
    stripped = line.strip()
    for token, hex_color in _LEVEL_COLORS:
        if token in stripped:
            color = hex_color
            break
    if stripped.startswith("[") and "/" in stripped[:8]:
        color = "#7fc4ff"
    if stripped.startswith("Connected to Resolve"):
        color = "#7fc4ff"

    safe = (
        line
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace(" ", "&nbsp;")
    )
    state.win.Find(LOG_TEXT).Append(f'<span style="color:{color}">{safe}</span>')


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------


def _run_worker(state: _WindowState, settings: BatchSettings) -> None:
    win = state.win
    cancel = state.cancel_event

    def sink(message: str) -> None:
        # Marshal log lines back to the main thread via QueueEvent so we
        # never touch UIManager widgets from a background thread.
        try:
            win.QueueEvent(win, APPEND_LOG_EVENT, {"line": message})
        except Exception:
            pass

    try:
        summary = run_batch(
            state.resolve,
            state.project,
            state.timeline,
            settings,
            sink=sink,
            cancel_event=cancel,
        )
        win.QueueEvent(win, FINISH_EVENT, {"ok": True, "summary": summary})
    except Exception as exc:
        win.QueueEvent(win, FINISH_EVENT, {
            "ok": False,
            "error": f"{exc}\n\n{traceback.format_exc()}",
        })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(*, resolve: Any, project: Any, fusion: Any, bmd: Any) -> None:
    state = _WindowState()
    state.resolve = resolve
    state.project = project
    state.fusion = fusion
    state.bmd = bmd

    ui = fusion.UIManager
    dispatcher = bmd.UIDispatcher(ui)
    state.dispatcher = dispatcher

    # If a previous instance is open, just bring it forward instead of
    # opening a duplicate.
    existing = ui.FindWindow(WIN_ID)
    if existing:
        existing.Show()
        existing.Raise()
        return

    win = dispatcher.AddWindow({
        "ID": WIN_ID,
        "WindowTitle": "autoneat",
        "Geometry": [120, 120, 1180, 760],
        "MinimumSize": [960, 600],
        "StyleSheet": STYLESHEET,
        "Events": {APPEND_LOG_EVENT: True, PROGRESS_EVENT: True, FINISH_EVENT: True},
    }, _build_layout(ui))
    state.win = win

    # ─── Event handlers ──────────────────────────────────────────────

    def on_close(_ev: Dict[str, Any]) -> None:
        state.cancel_event.set()
        dispatcher.ExitLoop()

    def on_refresh(_ev: Dict[str, Any]) -> None:
        _refresh_clips(state)

    def on_start(_ev: Dict[str, Any]) -> None:
        if state.worker_thread and state.worker_thread.is_alive():
            return
        state.cancel_event = threading.Event()
        win.Find(LOG_TEXT).Clear()
        win.Find(START_BTN).Enabled = False
        win.Find(CANCEL_BTN).Enabled = True
        win.Find(REFRESH_BTN).Enabled = False
        # Re-probe so we send the worker a freshly-resolved project /
        # timeline pair (the user may have switched timelines mid-session).
        _refresh_clips(state)
        if state.timeline is None:
            win.Find(START_BTN).Enabled = True
            win.Find(CANCEL_BTN).Enabled = False
            win.Find(REFRESH_BTN).Enabled = True
            return
        settings = _settings_from_window(state)
        thread = threading.Thread(
            target=_run_worker,
            args=(state, settings),
            name="autoneat-batch",
            daemon=True,
        )
        state.worker_thread = thread
        thread.start()

    def on_cancel(_ev: Dict[str, Any]) -> None:
        state.cancel_event.set()
        win.Find(CANCEL_BTN).Enabled = False
        _append_log_line(state, "cancel requested — will stop after current clip")

    def on_append_log(ev: Dict[str, Any]) -> None:
        line = str(ev.get("line") or "")
        if line:
            _append_log_line(state, line)

    def on_finish(ev: Dict[str, Any]) -> None:
        win.Find(START_BTN).Enabled = True
        win.Find(CANCEL_BTN).Enabled = False
        win.Find(REFRESH_BTN).Enabled = True
        if ev.get("ok"):
            summary = ev.get("summary") or {}
            ok = int(summary.get("processed") or 0)
            failed = int(summary.get("failed") or 0)
            sidecar_path = summary.get("sidecar_path")
            line = f"Done. {ok} OK, {failed} failed. Sidecar: {sidecar_path}"
            _append_log_line(state, line)
        else:
            _append_log_line(state, f"FATAL: {ev.get('error')}")

    win.On[WIN_ID].Close = on_close
    win.On[WIN_ID][APPEND_LOG_EVENT] = on_append_log
    win.On[WIN_ID][FINISH_EVENT] = on_finish
    win.On[REFRESH_BTN].Clicked = on_refresh
    win.On[START_BTN].Clicked = on_start
    win.On[CANCEL_BTN].Clicked = on_cancel

    # ─── Show + initial probe + run loop ─────────────────────────────

    win.Show()
    _refresh_clips(state)
    dispatcher.RunLoop()


__all__ = ["run"]
