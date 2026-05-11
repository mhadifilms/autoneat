# autoneat

A **DaVinci Resolve Workflow Integration** that batch-runs Neat Video Pro 6's
*Auto Profile* on every clip in a timeline — without sitting at the
keyboard clicking *Auto Profile → Apply* a thousand times.

Neat Video has no public scripting API. autoneat fills the gap by
combining DaVinci Resolve's Fusion API (to add the OFX node and fire
Neat's `Prepare Profile` `ButtonControl`) with Apple Vision OCR +
`cliclick` (for the few clicks Neat's Qt UI still requires) into a
single batch runner that lives **inside Resolve** under
**Workspace → Workflow Integrations → autoneat**.

> macOS only · DaVinci Resolve **Studio** required (Workflow Integrations
> are a Studio-only feature) · Tested with Resolve 20 + Neat Video 6.

## What it does

For every clip on the active timeline (optionally filtered by shot id):

1. Move Resolve's playhead to the clip on the Edit page.
2. Add (or find) a **Reduce Noise v6** OFX node on the clip's Fusion
   comp. For ACES + HDR projects, automatically wrap it in a
   `ColorSpaceTransform` pair so Neat sees display-referred Rec.2020/PQ
   pixels and can actually build a useful noise profile (instead of the
   near-black AP0 linear it would otherwise see).
3. Open Neat's main window via the OFX `Prepare Profile___`
   `ButtonControl` — no Inspector OCR involved.
4. Drive Neat past the demo splash, the "selection too small"
   information dialog, and the *preparing input frames* phase.
5. Click **Auto Profile**, wait for the profile to be ready, click
   **Apply**, wait for the Neat window to close, advance to the next
   clip.
6. Write a sidecar JSON after every clip so you can resume from where
   you left off if something interrupts the run.

OCR runs once per button per run, and the resulting window-relative
offset is cached for the rest of the batch — subsequent clips on the
same machine click instantly without another OCR pass.

## Why a workflow integration (not a standalone app)?

DaVinci Resolve Studio loads anything dropped in
`/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow
Integration Plugins/` and exposes it as a menu entry under
**Workspace → Workflow Integrations**. Two flavors are supported:

- **Electron plugin** — a folder containing `manifest.xml` + `main.js` +
  `WorkflowIntegration.node`, with the UI built in HTML/CSS/JS.
- **Python script** — a `.py` at the WFI root that's exec'd inside
  Resolve, with `resolve` / `project` / `fusion` / `bmd` pre-injected as
  module-level globals and a UI built with Resolve's bundled UIManager.

autoneat uses the Python flavor. Why:

- The driver logic (Fusion comp wiring, Auto Profile state machine,
  cliclick + Vision OCR) is already Python. No JS rewrite, no
  `node_modules` to ship.
- UIManager is Qt under the hood — bundled with Resolve, no `pip
  install` step. The plugin runs on every machine that has Resolve
  Studio with **zero Python dependencies**.
- Subprocesses inherited from Resolve (cliclick, screencapture, swift)
  ride on Resolve's existing macOS Accessibility / Screen Recording
  permissions. No second app for the user to grant access to.

## Install

```bash
git clone https://github.com/mhadifilms/autoneat.git
cd autoneat
scripts/fetch_cliclick.sh   # bundle cliclick into the plugin (optional but recommended)
scripts/install.sh          # symlink the plugin into Resolve's WFI folder
```

Then:

1. Quit and relaunch DaVinci Resolve.
2. Open a project / timeline you want to process.
3. **Workspace → Workflow Integrations → autoneat**.

End users who don't want a repo checkout sitting around can use
`scripts/install.sh --copy` to copy the files instead of symlinking, or
download a tagged release zip and drop the contents into the WFI folder
manually.

Full guide: [`docs/INSTALL.md`](docs/INSTALL.md). Permissions
walkthrough: [`docs/PERMISSIONS.md`](docs/PERMISSIONS.md).

## The window

```
autoneat  —  Batch Auto-Profile for Neat Video
┌──────────────────────────────┬────────────────────────────────────────┐
│ Resolve                      │ Clips on the timeline                  │
│ ● Connected                  │ ┌────────────────────────────────────┐ │
│   Project: My Show           │ │ #  Track  Clip            Frames   │ │
│   Timeline: Reel 1           │ │ 1  V1     ep01_001        420      │ │
│   Clips: 35 / 91             │ │ 2  V1     ep01_005        612      │ │
│   [ Refresh ]                │ │ ...                                  │ │
├──────────────────────────────┤ └────────────────────────────────────┘ │
│ Filter                       │ Live log                               │
│   Shot ids: [          ]     │ ┌────────────────────────────────────┐ │
│   Track: 1   ☐ All tracks    │ │ Connected to Resolve…              │ │
│   Start from: 1   Limit: All │ │ [1/35] ep01_001 …                  │ │
├──────────────────────────────┤ │   [  0.5s] playhead:tc=…           │ │
│ Options                      │ │   [  1.8s] attach-neat:OK          │ │
│   ☐ Continue from last run   │ │   [  3.2s] color-wrap:applied      │ │
│   ☐ Retry previously-failed  │ │   …                                 │ │
│   ☑ Reuse existing Neat node │ │   → OK (44.3s, 12 steps)           │ │
│   ☐ Skip ACES color wrap     │ │ [2/35] ep01_005 …                  │ │
│   Apply delay:    5 s        │ │ …                                   │ │
│   Prepare timeout: 1800 s    │ └────────────────────────────────────┘ │
│   [ Start batch ] [ Cancel ] │                                        │
└──────────────────────────────┴────────────────────────────────────────┘
```

The Tree shows every clip on the active timeline that survives the
**Filter** rules. The log scrolls live as the batch runs, color-coded
by severity. **Cancel** is a soft stop — it lets the current clip
finish (so we don't leave Resolve hanging with a Neat window open) and
stops before the next one.

## Why no full headless mode?

Neat Video's OFX surface exposes a `ClipParams` parameter that holds
the full noise profile + plugin state as a base64-encoded blob, and
that parameter does round-trip cleanly through `tool.SetInput()` —
which means a profile *transplant* (analyze once, paste everywhere)
is fully scriptable. But Neat's *Auto Profile* algorithm itself is
only triggered through the plugin window: every other parameter
exposed via OFX is read-only or simply mirrors UI state. Setting the
`Prepare Profile___` `ButtonControl` to `1.0` opens the window — it
doesn't run profiling in the background.

So the runner is a hybrid: it scripts everything that's scriptable
(node placement, color wrap, window opening, Apply detection via
window-close, sidecar bookkeeping) and only OCR-clicks the two buttons
Neat genuinely won't expose (*Auto Profile* and *Apply*).

## Layout

```
autoneat/
├── plugin/                          ← what gets installed into the WFI folder
│   ├── autoneat.py              ← entry point Resolve registers
│   └── autoneat_lib/             ← Python package, sibling to entry
│       ├── core/                    ← engine — pure Python, no UI imports
│       │   ├── batch.py
│       │   ├── neat_ofx.py
│       │   ├── ocr.py               ← stdlib + sips + screencapture (no Pillow)
│       │   ├── recorder.py
│       │   ├── resolve_client.py
│       │   ├── shotid.py
│       │   ├── subprocess_utils.py
│       │   ├── ui_driver.py
│       │   └── windows.py
│       ├── ui/
│       │   └── window.py            ← UIManager UI + worker thread
│       └── resources/
│           ├── vision_ocr.swift
│           └── cliclick             ← bundled by scripts/fetch_cliclick.sh
├── scripts/
│   ├── install.sh                   ← symlink/copy plugin/* into WFI folder
│   └── fetch_cliclick.sh            ← bundle cliclick into resources/
└── docs/
    ├── INSTALL.md
    └── PERMISSIONS.md
```

## License

[MIT](LICENSE). The bundled `cliclick` binary is BSD-3 licensed by
Carsten Blüm (https://github.com/BlueM/cliclick).
