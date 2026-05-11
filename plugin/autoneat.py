"""DaVinci Resolve Workflow Integration entry point.

Resolve registers any ``.py`` at the root of its
``Workflow Integration Plugins`` folder and exposes it under
``Workspace → Workflow Integrations``. When the user picks the menu
entry, Resolve execs this file with ``resolve``, ``project``, ``fusion``,
and ``bmd`` already bound as module-level globals.

This file is intentionally tiny: it adds its own directory to
``sys.path`` so the sibling ``autoneat_lib/`` package can be imported,
then hands off to the UIManager window in ``autoneat_lib.ui.window``.
"""

import os
import sys
import traceback

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

try:
    from autoneat_lib.ui.window import run as _run_window
except Exception:
    sys.stderr.write(
        "autoneat: failed to import autoneat_lib.ui.window — is the\n"
        "sibling `autoneat_lib/` package installed next to this file?\n\n"
    )
    sys.stderr.write(traceback.format_exc())
    raise

# Resolve injects these as globals before exec'ing the script.
_run_window(
    resolve=resolve,  # noqa: F821 - injected by Resolve at script-load time
    project=project,  # noqa: F821 - injected by Resolve at script-load time
    fusion=fusion,    # noqa: F821 - injected by Resolve at script-load time
    bmd=bmd,          # noqa: F821 - injected by Resolve at script-load time
)
