"""Public screen-capture API and CLI for AutoNeat nodes."""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path


def capture_screen(output: str | Path | None = None) -> Path:
    """Capture the active macOS display, including from an SSH session."""
    from autoneat import _neat_ui

    path = (
        Path(output).expanduser()
        if output is not None
        else _neat_ui._cache_base()
        / "captures"
        / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _neat_ui._capture_screen(path)
    return path


def _build_parser(*, prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Capture this node's screen, including over SSH.",
    )
    parser.add_argument("-o", "--output", help="Output PNG path")
    parser.add_argument(
        "--b64",
        action="store_true",
        help="Also print the PNG as a BASE64-prefixed line",
    )
    parser.add_argument("--json", action="store_true", help="Print a JSON result line")
    return parser


def main(argv: list[str] | None = None, *, prog: str = "autoneat capture") -> int:
    args = _build_parser(prog=prog).parse_args(argv)
    try:
        path = capture_screen(args.output)
        size = path.stat().st_size
    except Exception as exc:
        message = str(exc).strip() or "screen capture failed"
        if args.json:
            print(json.dumps({"ok": False, "error": message}), flush=True)
        else:
            print(f"error: {message}", file=sys.stderr, flush=True)
        return 1

    if args.json:
        print(json.dumps({"ok": True, "path": str(path), "bytes": size}), flush=True)
    else:
        print(f"captured {path} ({size} bytes)", flush=True)
    if args.b64:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        print(f"BASE64:{data}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
