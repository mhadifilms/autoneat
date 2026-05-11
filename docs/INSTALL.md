# Install

autoneat is a **DaVinci Resolve Workflow Integration**, not a
standalone app. It runs *inside* DaVinci Resolve Studio.

Workflow Integrations are a Studio-only feature. The free build of
DaVinci Resolve does not load anything from the
`Workflow Integration Plugins` folder. Make sure you're on **Resolve
Studio 18 or newer** before continuing.

## Requirements

- macOS 12 or newer.
- DaVinci Resolve **Studio** 18+.
- Neat Video **6** OFX (registered ID `ofx.com.absoft.NeatVideo6.rs`),
  installed and licensed (or in demo mode — works fine, just adds an
  extra "Continue" splash per clip).
- The `swift` runner from Xcode Command Line Tools (almost always
  installed on a developer machine; install with `xcode-select --install`
  if missing).

There is **no Python pip dependency**. The plugin uses macOS's built-in
tools (`screencapture`, `sips`, `swift`, `osascript`) for everything
preprocessing-related.

## Install (developer / repo checkout)

```bash
git clone https://github.com/mhadifilms/autoneat.git
cd autoneat
scripts/fetch_cliclick.sh   # bundles cliclick into plugin/autoneat_lib/resources/
scripts/install.sh          # symlinks plugin/* into Resolve's WFI folder
```

`install.sh` writes to `/Library/Application Support/Blackmagic
Design/DaVinci Resolve/Workflow Integration Plugins/`, which is owned by
`root`. The script will prompt for `sudo`.

After install, **quit and relaunch Resolve**. The plugin appears under
**Workspace → Workflow Integrations → autoneat**.

## Install (end user / no repo)

1. Download a tagged release zip from GitHub Releases.
2. Unzip it.
3. Copy the contents of the unzipped `plugin/` folder into:

   ```
   /Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/
   ```

   You should end up with these two items at the top level of that
   folder:

   ```
   Workflow Integration Plugins/
   ├── autoneat.py        ← the entry point Resolve registers
   └── autoneat_lib/       ← support package (next to the entry)
   ```

4. Quit and relaunch Resolve.

## Bundling cliclick

`cliclick` is the lightweight (≈160 KB) BSD-3 utility we use to perform
the *Auto Profile* and *Apply* clicks. Two ways to satisfy the
dependency:

- **Bundled** (recommended for distribution): run
  `scripts/fetch_cliclick.sh` once before installing. It copies the
  Homebrew copy of `cliclick` into `plugin/autoneat_lib/resources/` so
  the integration is fully self-contained.
- **System install**: if no bundled copy is present, the integration
  falls back to whatever `cliclick` it finds on `PATH`. If you've
  installed Resolve from a fresh image, a `brew install cliclick`
  is all you need.

## Verify the install

After relaunching Resolve, open the **Workspace** menu — there should
be a **Workflow Integrations** sub-menu, and **autoneat** should be
the top entry under it. Selecting it opens the integration window. The
top-left card should show **● Connected** with the current project /
timeline name as soon as the window finishes opening.

If the menu entry is missing, the most common causes are:

- You're on the free version of Resolve, not Studio.
- The `.py` file is inside a sub-folder of `Workflow Integration
  Plugins/` (it must be at the *root* of that folder).
- Resolve was already running when you installed — quit and relaunch.

## Updating

If you installed via `scripts/install.sh` (symlink mode, the default),
you can `git pull` and the changes are picked up the next time the
integration window opens. UIManager re-reads the layout each time the
script is exec'd.

If you installed via `scripts/install.sh --copy` or the manual zip
route, repeat the install step against the new release.

## Uninstall

```bash
scripts/install.sh --uninstall
```

Or manually delete `autoneat.py` and `autoneat_lib/` from
`/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow
Integration Plugins/`.
