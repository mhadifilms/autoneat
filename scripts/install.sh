#!/usr/bin/env bash
# Install autoneat as a DaVinci Resolve Workflow Integration.
#
# By default this *symlinks* the plugin into Resolve's Workflow Integration
# Plugins folder, so edits to the repo are picked up the next time Resolve
# launches the integration. Pass `--copy` to do a full copy instead (useful
# for end users who want the plugin to keep working after they delete the
# repo checkout).
#
# Usage:
#   scripts/install.sh           # symlink into Resolve's WFI folder
#   scripts/install.sh --copy    # copy files instead of symlinking
#   scripts/install.sh --uninstall   # remove the integration
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_SRC="${REPO_ROOT}/plugin"
WFI_ROOT="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins"
ENTRY_NAME="autoneat.py"
PKG_NAME="autoneat_lib"

remove_installed_path() {
    local path="$1"
    case "$path" in
        "${WFI_ROOT}/${ENTRY_NAME}"|"${WFI_ROOT}/${PKG_NAME}") ;;
        *)
            echo "refusing to remove unexpected path: ${path}" >&2
            exit 2
            ;;
    esac

    if [[ -L "$path" || -f "$path" ]]; then
        sudo rm -f "$path"
    elif [[ -d "$path" ]]; then
        sudo rm -R "$path"
    fi
}

mode="symlink"
for arg in "$@"; do
    case "$arg" in
        --copy)      mode="copy" ;;
        --symlink)   mode="symlink" ;;
        --uninstall) mode="uninstall" ;;
        -h|--help)
            sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "unknown argument: $arg" >&2
            exit 2 ;;
    esac
done

if ! sudo -n true 2>/dev/null; then
    echo "Resolve's Workflow Integration Plugins folder lives under /Library —"
    echo "this script needs sudo to write to it. You may see a sudo prompt."
fi

sudo mkdir -p "${WFI_ROOT}"

if [[ "$mode" == "uninstall" ]]; then
    remove_installed_path "${WFI_ROOT}/${ENTRY_NAME}"
    remove_installed_path "${WFI_ROOT}/${PKG_NAME}"
    echo "Uninstalled autoneat from ${WFI_ROOT}"
    exit 0
fi

# Always start clean so we don't end up with a stale symlink shadowing a
# fresh copy or vice versa.
remove_installed_path "${WFI_ROOT}/${ENTRY_NAME}"
remove_installed_path "${WFI_ROOT}/${PKG_NAME}"

if [[ "$mode" == "symlink" ]]; then
    sudo ln -s "${PLUGIN_SRC}/${ENTRY_NAME}" "${WFI_ROOT}/${ENTRY_NAME}"
    sudo ln -s "${PLUGIN_SRC}/${PKG_NAME}"   "${WFI_ROOT}/${PKG_NAME}"
    echo "Symlinked:"
else
    sudo cp "${PLUGIN_SRC}/${ENTRY_NAME}" "${WFI_ROOT}/${ENTRY_NAME}"
    sudo cp -R "${PLUGIN_SRC}/${PKG_NAME}" "${WFI_ROOT}/${PKG_NAME}"
    echo "Copied:"
fi
echo "  ${WFI_ROOT}/${ENTRY_NAME}"
echo "  ${WFI_ROOT}/${PKG_NAME}/"
echo
echo "Quit and relaunch DaVinci Resolve, then open:"
echo "  Workspace → Workflow Integrations → autoneat"
