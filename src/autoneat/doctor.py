"""Environment checks for autoneat."""

from __future__ import annotations

import platform
import shutil
from typing import Iterable


REQUIRED_TOOLS = ("osascript", "screencapture", "sips", "swift")


def check_environment() -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []
    rows.append(("macOS", platform.system() == "Darwin", platform.platform()))
    for tool in REQUIRED_TOOLS:
        found = shutil.which(tool)
        rows.append((tool, bool(found), found or "not found on PATH"))
    try:
        import dvr  # noqa: F401

        rows.append(("dvr", True, "importable"))
    except ImportError as exc:
        rows.append(("dvr", False, str(exc)))
    try:
        import Quartz  # noqa: F401

        rows.append(("Quartz", True, "importable"))
    except ImportError as exc:
        rows.append(("Quartz", False, str(exc)))
    return rows


def print_report(rows: Iterable[tuple[str, bool, str]]) -> int:
    ok = True
    for name, passed, detail in rows:
        ok = ok and passed
        status = "OK" if passed else "MISSING"
        print(f"{status:<8} {name:<14} {detail}", flush=True)
    if not ok:
        print(
            "\nGrant Terminal/iTerm Accessibility and Screen Recording permissions, "
            "then reinstall autoneat if Python dependencies are missing.",
            flush=True,
        )
    return 0 if ok else 1
