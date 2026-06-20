"""Command-line interface for autoneat."""

from __future__ import annotations

import argparse
import sys

from autoneat.doctor import check_environment, print_report
from autoneat import runner


def _doctor(_args: argparse.Namespace) -> int:
    return print_report(check_environment())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoneat")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check macOS/Resolve automation prerequisites")
    doctor.set_defaults(func=_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "profile":
        return runner.main(argv[1:])
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
