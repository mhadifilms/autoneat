# autoneat

Standalone Neat Video Pro 6 Auto Profile automation for DaVinci Resolve.

Neat Video has no public scripting API for Auto Profile. `autoneat` scripts the
parts Resolve exposes, then uses macOS window automation plus Apple Vision OCR
for the two controls Neat does not expose: **Auto Profile** and **Apply**.

## Install

```bash
python3 -m pip install autoneat
```

DaVinci Resolve Studio, Neat Video Pro 6, and macOS Accessibility / Screen
Recording permissions are required. Run:

```bash
autoneat doctor
```

## CLI

Run against the current Resolve project/timeline:

```bash
autoneat profile --state artifacts/neat/state.json
```

Run against a specific project/timeline and shot filter:

```bash
autoneat profile \
  --project "My Show" \
  --timeline "My Show_Neat" \
  --shot-ids 001,002,003 \
  --state artifacts/neat/state.json
```

Important options:

- `--continue` resumes from the state JSON.
- `--retry-failed` retries previously failed clips when resuming.
- `--all-tracks` processes all video tracks instead of `--track 1`.
- `--no-color-wrap` skips the ACES/HDR ColorSpaceTransform wrapper.
- `--json` prints the final summary object after the live log.

## Python API

```python
from autoneat import ProfileOptions, run_profile

result = run_profile(
    ProfileOptions(
        project_name="My Show",
        timeline_name="My Show_Neat",
        shot_ids=["001", "002"],
    )
)
```

## How It Works

For each selected timeline clip, `autoneat`:

1. Moves the Resolve playhead to the clip.
2. Adds or reuses the Neat Video OFX node.
3. Wraps ACES/HDR clips with ColorSpaceTransform nodes so Neat sees
   display-referred pixels.
4. Opens Neat via the OFX `Prepare Profile___` button control.
5. OCR-clicks Auto Profile, waits for readiness, then OCR-clicks Apply.
6. Writes a state JSON after every clip for resumable batches.

## Development

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[test]"
./.venv/bin/python -m pytest
```

## License

[MIT](LICENSE).
