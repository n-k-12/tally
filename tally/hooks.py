"""Connect Claude Code to Tally by adding hooks to its settings.json, on Windows and in WSL.

Claude Code on Windows posts straight to Tally. Inside WSL, 127.0.0.1 may be
Linux rather than Windows, so the hooks run Windows' own curl.exe instead,
which reaches Tally whatever the WSL networking mode.
"""
import json
import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath

from . import PORT

EVENTS = [  # (Claude Code event, matcher, Tally state)
    ("UserPromptSubmit", None, "working"),
    ("PostToolUse", "*", "working"),
    ("Notification", "permission_prompt", "needs-you"),
    ("Stop", None, "done"),
    ("SessionEnd", None, "end"),
]
MARK = f"127.0.0.1:{PORT}/"       # how Tally recognises its own hooks
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def wanted(curl=None):
    """Tally's hooks: HTTP ones, or with `curl` (a path to curl.exe), command ones."""
    hooks = {}
    for event, matcher, state in EVENTS:
        url = f"http://{MARK}{state}"
        if curl:
            hook = {"type": "command", "timeout": 5,
                    "command": f"'{curl}' -s -m 3 -X POST -H 'Content-Type: application/json' "
                               f"--data-binary @- {url} >/dev/null 2>&1 || true"}
        else:
            hook = {"type": "http", "url": url, "timeout": 3}
        entry = {"hooks": [hook]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        hooks[event] = [entry]
    return hooks


def ours(hook):
    return MARK in str(hook.get("url", "")) + str(hook.get("command", ""))


def without_ours(settings):
    """A copy of settings.json content with Tally's hooks taken out, leaving everything else."""
    settings = dict(settings)
    kept = {}
    for event, entries in (settings.get("hooks") or {}).items():
        if not isinstance(entries, list):
            kept[event] = entries
            continue
        new = []
        for entry in entries:
            hooks = entry.get("hooks") if isinstance(entry, dict) else None
            if isinstance(hooks, list):
                left = [h for h in hooks if not (isinstance(h, dict) and ours(h))]
                if not left:
                    continue
                entry = {**entry, "hooks": left}
            new.append(entry)
        if new:
            kept[event] = new
    if kept:
        settings["hooks"] = kept
    else:
        settings.pop("hooks", None)
    return settings


def with_ours(settings, curl=None):
    settings = without_ours(settings)
    hooks = settings.setdefault("hooks", {})
    for event, entries in wanted(curl).items():
        hooks.setdefault(event, []).extend(entries)
    return settings


def read_settings(path):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("settings.json does not hold a JSON object")
    return data


def write_settings(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".before-tally")
    if path.exists() and not backup.exists():
        backup.write_bytes(path.read_bytes())
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def state_of(path, curl=None):
    """'connected', 'outdated' (Tally hooks that differ from this version's) or 'not connected'."""
    settings = read_settings(path)
    found = [h for entries in (settings.get("hooks") or {}).values() if isinstance(entries, list)
             for e in entries if isinstance(e, dict) for h in (e.get("hooks") or []) if isinstance(h, dict) and ours(h)]
    if not found:
        return "not connected"
    return "connected" if with_ours(settings, curl) == settings else "outdated"


# --------------------------------------------------------------------------- where Claude Code lives

def _run(args, timeout=20):
    out = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    return out.stdout


def local_target():
    """Claude Code on this computer itself."""
    home = Path.home()
    label = {"win32": "Windows", "darwin": "This Mac"}.get(sys.platform, "This computer")
    return {"id": "local", "label": label, "path": str(home / ".claude" / "settings.json"),
            "claude": (home / ".claude").exists(), "curl": None}


def targets():
    """Every Claude Code install Tally can connect: this computer, then on Windows each WSL distro (which starts them)."""
    found = [local_target()]
    if sys.platform != "win32":
        return found
    try:
        raw = _run(["wsl.exe", "--list", "--quiet"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return found
    names = raw.decode("utf-16-le", "ignore").replace("\ufeff", "").split()
    curl_win = str(PureWindowsPath(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe")
    for name in names:
        name = name.strip("\x00").strip()
        if not name or name.lower().startswith(("docker-desktop", "rancher-desktop")):
            continue
        try:
            lines = _run(["wsl.exe", "-d", name, "--", "sh", "-c", f'echo "$HOME"; wslpath -u \'{curl_win}\''])
            linux_home, curl = lines.decode("utf-8", "ignore").split()[:2]
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        unc = Path(rf"\\wsl$\{name}" + linux_home.replace("/", "\\"))
        found.append({"id": f"wsl:{name}", "label": f"{name} (WSL)", "path": str(unc / ".claude" / "settings.json"),
                      "claude": (unc / ".claude").exists(), "curl": curl})
    return found


def status(found=None):
    result = []
    for t in found if found is not None else targets():
        try:
            state = state_of(t["path"], t["curl"])
        except (OSError, ValueError) as err:
            state = f"unreadable: {err}"
        result.append({"id": t["id"], "label": t["label"], "claude": t["claude"], "state": state, "path": t["path"]})
    return result


def connect(target):
    write_settings(target["path"], with_ours(read_settings(target["path"]), target["curl"]))


def disconnect(target):
    path = Path(target["path"])
    if path.exists():
        settings = read_settings(path)
        if settings != without_ours(settings):
            write_settings(path, without_ours(settings))


def disconnect_all():
    for t in targets():
        try:
            disconnect(t)
        except (OSError, ValueError):
            pass
