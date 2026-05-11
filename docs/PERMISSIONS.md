# macOS permissions

autoneat drives a GUI you can see, so macOS treats the subprocesses
it spawns the same way it treats any automation tool. The good news:
because autoneat runs *inside* DaVinci Resolve Studio, every
permission it needs is one that Resolve itself needs — there's no
second app for the user to grant access to.

## What Resolve needs

| Permission | Why | System Settings → Privacy & Security → … |
|---|---|---|
| **Accessibility** | So `cliclick` (a child process of Resolve) can move the cursor and click buttons inside the Neat Video window, and so JXA scripts run via `osascript` can enumerate Resolve's windows. | **Accessibility** — enable `DaVinci Resolve` |
| **Screen Recording** | So `screencapture` can read pixels from the Neat plugin window for OCR. | **Screen Recording** — enable `DaVinci Resolve` |
| **Automation: System Events** | JXA scripts that read window names and positions go through `System Events`. macOS prompts for this automatically the first time. | **Automation** — under `DaVinci Resolve`, allow `System Events` |

After granting any of these, **quit and relaunch Resolve**. macOS does
not pick up newly-granted permissions for an already-running process.

## Verifying the permissions

From a terminal (with Resolve running):

```bash
osascript -l JavaScript -e \
  'JSON.stringify({ok: Application("System Events").processes.byName("Resolve").exists()})'
```

If you see `{"ok":true}` — System Events automation is working.

If `screencapture` returns a black image of the Resolve UI (or the
desktop instead of Resolve's window), Screen Recording is denied and
the OCR step will reliably fail.

## Troubleshooting

- **The integration shows "Could not OCR-locate `apply` button"**:
  almost always a Screen Recording permission issue. Re-check the
  **Privacy & Security → Screen Recording** panel and make sure
  `DaVinci Resolve` is in the list and toggled on, then quit + relaunch
  Resolve.
- **`cliclick` clicks the wrong place / doesn't click at all**: the
  Accessibility toggle for `DaVinci Resolve` is off, or you've
  installed the plugin into a Resolve build that hasn't been granted
  Accessibility yet. macOS treats `Resolve.app` and `DaVinci
  Resolve.app` as different apps if you've ever moved Resolve out of
  `/Applications/` — easy to miss.
- **Multiple displays**: cliclick clicks in screen-absolute
  coordinates. Move Neat's window onto the same display where Resolve
  is running before starting a batch.
- **`osascript` complains about "not authorized"**: the System Events
  automation prompt was dismissed at some point. Reset it and try
  again:

  ```bash
  tccutil reset AppleEvents com.blackmagicdesign.resolve
  ```

  Then relaunch Resolve and re-trigger the integration so macOS shows
  the prompt again.
