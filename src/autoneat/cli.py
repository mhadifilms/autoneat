"""Command-line interface for autoneat."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autoneat.api import ProfileOptions, run_profile
from autoneat.doctor import check_environment, print_report


def _parse_shot_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.replace(",", " ").split() if part.strip()]


def _profile(args: argparse.Namespace) -> int:
    options = ProfileOptions(
        project_name=args.project,
        timeline_name=args.timeline,
        track=args.track,
        all_video_tracks=args.all_tracks,
        shot_ids=_parse_shot_ids(args.shot_ids),
        start_from=args.start_from,
        limit=args.limit,
        continue_run=args.continue_run,
        retry_failed=args.retry_failed,
        reuse_existing_neat=not args.no_reuse_existing,
        no_color_wrap=args.no_color_wrap,
        open_timeout=args.open_timeout,
        editor_timeout=args.editor_timeout,
        prepare_timeout=args.prepare_timeout,
        profile_wait=args.profile_wait,
        ready_timeout=args.ready_timeout,
        apply_delay=args.apply_delay,
        close_timeout=args.close_timeout,
        step_delay=args.step_delay,
        sidecar_path=Path(args.state).expanduser() if args.state else None,
    )
    result = run_profile(options, sink=lambda line: print(line, flush=True))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True), flush=True)
    return 0 if result.get("ok") else 1


def _doctor(_args: argparse.Namespace) -> int:
    return print_report(check_environment())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoneat")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check macOS/Resolve automation prerequisites")
    doctor.set_defaults(func=_doctor)

    profile = sub.add_parser("profile", help="Run Neat Video Auto Profile over timeline clips")
    profile.add_argument("--project", help="Resolve project name (default: current project)")
    profile.add_argument("--timeline", help="Resolve timeline name (default: current timeline)")
    profile.add_argument("--track", type=int, default=1, help="Video track to process")
    profile.add_argument("--all-tracks", action="store_true", help="Process all video tracks")
    profile.add_argument("--shot-ids", help="Comma/space-separated shot ids to include")
    profile.add_argument("--start-from", type=int, default=1, help="1-based clip offset after filters")
    profile.add_argument("--limit", type=int, default=0, help="Maximum clips to process")
    profile.add_argument("--continue", dest="continue_run", action="store_true", help="Resume from state")
    profile.add_argument("--retry-failed", action="store_true", help="Retry failed clips on resume")
    profile.add_argument("--no-reuse-existing", action="store_true", help="Add a fresh Neat node")
    profile.add_argument("--no-color-wrap", action="store_true", help="Skip ACES/HDR CST wrapping")
    profile.add_argument("--state", help="Path to run state JSON")
    profile.add_argument("--open-timeout", type=float, default=18.0)
    profile.add_argument("--editor-timeout", type=float, default=60.0)
    profile.add_argument("--prepare-timeout", type=float, default=1800.0)
    profile.add_argument("--profile-wait", type=float, default=3.0)
    profile.add_argument("--ready-timeout", type=float, default=90.0)
    profile.add_argument("--apply-delay", type=float, default=5.0)
    profile.add_argument("--close-timeout", type=float, default=20.0)
    profile.add_argument("--step-delay", type=float, default=1.0)
    profile.add_argument("--json", action="store_true", help="Print final summary JSON")
    profile.set_defaults(func=_profile)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
