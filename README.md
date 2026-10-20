# Tally

Tally turns a Philips Hue Bluetooth lamp into a status light for [Claude Code](https://claude.com/claude-code),
like the on-air tally light on a studio camera. You can see from across the room whether Claude is working,
waiting for your approval, or done.

| Claude Code is… | The lamp… (you choose the looks) |
|---|---|
| working | glows a steady warm white |
| waiting for your approval | pulses |
| finished | flashes, then goes back to how you left it |

It follows every Claude Code session at once, on Windows (including WSL) and on Macs, and talks to the
lamp over Bluetooth. No Hue Bridge is needed.

![Tally's dashboard: the live lamp, open Claude Code sessions, and how the lamp shows each state](docs/screenshot.png)

## Install

You need a Philips Hue lamp with Bluetooth, Claude Code, and either Windows 10 or 11 with Bluetooth,
or a Mac with Apple Silicon (M1 or newer) running macOS 12 or later.

### Windows

1. Download **TallySetup** from the [latest release](https://github.com/n-k-12/tally/releases/latest) and run it.
   Windows may say *"Windows protected your PC"* because the installer isn't code-signed yet:
   choose **More info → Run anyway**.
2. Tally opens a short setup in your browser (see below).

Tally then lives in the system tray.

### Mac

1. Download **Tally-….dmg** from the [latest release](https://github.com/n-k-12/tally/releases/latest),
   open it, and drag Tally into Applications.
2. Open Tally. The first time, macOS says it can't check Tally for malicious software, because the app
   isn't notarised by Apple yet. Click **Done**, then open *System Settings → Privacy & Security*,
   scroll down to the message about Tally, and click **Open Anyway**.
3. When macOS asks whether Tally may use Bluetooth, click **Allow**. Setup opens in your browser.

Tally then lives in the menu bar, and starts when you log in (you can turn that off in its menu).

### Setup

1. **Find your lamp**: scan, then pick it.
2. **Connect Claude Code**: one click for this computer, and on Windows for each WSL distro where
   Claude Code is installed.
3. **Choose the looks**: colour, brightness and behaviour for each state, with a Test button.

Click Tally's lamp icon any time to open the dashboard: live sessions, the looks, an idle light,
quiet hours, pausing, and your lamp's colour range.

### Lamp not found?

- Make sure Bluetooth is on. On a Mac, also check *System Settings → Privacy & Security → Bluetooth*
  allows Tally.
- Make the lamp visible: in the Hue app, look for the option to make it visible to other devices
  (often under *Settings → Voice assistants*), or switch the lamp off and on at the wall. Close the Hue app
  afterwards, as the lamp may only talk to one device at a time.
- On Windows, pair it: *Settings → Bluetooth & devices → Add device → Bluetooth*, then pick the lamp.
  On a Mac, accept the pairing request that appears the first time Tally talks to the lamp.
- Scan again in Tally.

## How it works

Tally runs a small web server on your own computer at `http://127.0.0.1:4567`. Claude Code
[hooks](https://docs.claude.com/en/docs/claude-code/hooks) tell it when each session starts working,
needs permission, finishes or closes. In WSL the hooks use Windows' `curl.exe`, so they reach Tally
whatever your WSL networking mode.

Connecting Claude Code adds five hooks to its `settings.json` and keeps everything else in that file.
The first time, the original is saved next to it as `settings.json.before-tally`.

**Privacy:** nothing leaves your computer except a once-a-day check of this repository's releases for a
newer version. You can turn that off from Tally's menu.

## Uninstall

- **Windows:** *Settings → Apps → Tally → Uninstall*. This also takes Tally's hooks out of Claude Code.
  Your settings stay in `%APPDATA%\Tally`; delete that folder to remove them too.
- **Mac:** choose **Uninstall Tally…** in its menu, which takes the hooks out of Claude Code and stops it
  starting at login, then drag Tally from Applications to the Bin. Your settings stay in
  `~/Library/Application Support/Tally`; delete that folder to remove them too.

## Development

```sh
python -m tally --fake --no-tray --port 4999   # a pretend lamp that prints to the terminal
```

Then open http://127.0.0.1:4999/. `TALLY_DATA=some/folder` keeps test settings apart from your real ones.

To build with Python 3.11+, run this on the system you're building for:

```sh
python build/build.py        # Windows: TallySetup-<version>.exe (needs Inno Setup 6); Mac: Tally-<version>.dmg
python build/smoke_test.py   # checks the built app starts, serves the dashboard and accepts hooks
```

Results land in `dist/`. Windows needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer.

To release, bump `__version__` in `tally/__init__.py`, commit, and push a matching tag such as `v0.2.1`.
GitHub Actions builds and checks both versions and attaches them to a draft release; publish the draft
when it looks right. To build and check without releasing, run the Release workflow by hand from the
Actions tab.

## Licence

[MIT](LICENSE). Tally is an independent project, not affiliated with or endorsed by Anthropic or Signify (Philips Hue).
