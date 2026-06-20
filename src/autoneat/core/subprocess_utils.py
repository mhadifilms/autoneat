"""Shared subprocess helpers — UTF-8 safe, locale-independent."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence


def resource_dir() -> Path:
    """Path to the bundled ``resources/`` directory.

    Resolves relative to the package itself so it works whether the
    plugin was copied into Resolve's WFI dir or symlinked from a repo
    checkout.
    """
    return Path(__file__).resolve().parent.parent / "resources"


def run_proc(
    args: Sequence[str],
    *,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Wrapper around ``subprocess.run`` that forces UTF-8 decoding so OCR
    helper output with unicode characters never trips an ASCII codec error
    on machines with a C/POSIX locale (a common headless-server gotcha)."""
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def require_tool(name: str) -> str:
    """Locate a CLI tool, preferring the bundled copy under ``resources/``.

    Bundled binaries are made executable on first call (in case the
    install step copied them from a tarball that lost the +x bit).
    """
    bundled = resource_dir() / name
    if bundled.is_file():
        if not os.access(bundled, os.X_OK):
            try:
                bundled.chmod(0o755)
            except OSError:
                pass
        return str(bundled)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(
        f"Required tool not found: {name}. Install it (e.g. `brew install {name}`) "
        f"or bundle a copy under {resource_dir()}."
    )
