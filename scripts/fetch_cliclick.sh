#!/usr/bin/env bash
# Bundle a copy of cliclick into plugin/autoneat_lib/resources/ so the
# integration runs on machines that don't have Homebrew (or any system
# cliclick) installed.
#
# Upstream: https://github.com/BlueM/cliclick (BSD-3-Clause).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="${REPO_ROOT}/plugin/autoneat_lib/resources"
DEST="${DEST_DIR}/cliclick"
mkdir -p "${DEST_DIR}"

if [[ -f "${DEST}" ]]; then
    echo "cliclick already present at ${DEST}"
    exit 0
fi

if command -v brew >/dev/null 2>&1; then
    BREW_PREFIX="$(brew --prefix cliclick 2>/dev/null || true)"
    if [[ -n "${BREW_PREFIX}" && -x "${BREW_PREFIX}/bin/cliclick" ]]; then
        cp "${BREW_PREFIX}/bin/cliclick" "${DEST}"
        chmod +x "${DEST}"
        echo "Bundled cliclick from ${BREW_PREFIX}/bin/cliclick → ${DEST}"
        exit 0
    fi
fi

if command -v cliclick >/dev/null 2>&1; then
    SRC="$(command -v cliclick)"
    cp "${SRC}" "${DEST}"
    chmod +x "${DEST}"
    echo "Bundled cliclick from ${SRC} → ${DEST}"
    exit 0
fi

cat <<EOF >&2
ERROR: could not find a cliclick to bundle.

Install one of:
  brew install cliclick
  https://github.com/BlueM/cliclick/releases  (download a universal binary)

Then re-run this script.
EOF
exit 1
