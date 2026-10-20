"""Tally: a Philips Hue Bluetooth lamp that shows what Claude Code is doing."""
import os
import sys
from pathlib import Path

__version__ = "0.1.0"
PORT = 4567                       # Claude Code hooks point here; never change it once released
UPDATE_REPO = "n-k-12/tally"      # GitHub owner/repo whose releases are checked for updates


def data_dir():
    r"""Where settings and the log live: %APPDATA%\Tally on Windows. TALLY_DATA overrides it for testing."""
    if os.environ.get("TALLY_DATA"):
        path = Path(os.environ["TALLY_DATA"])
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA") or Path.home()) / "Tally"
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "tally"
    path.mkdir(parents=True, exist_ok=True)
    return path
