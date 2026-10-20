r"""Build Tally for the computer this runs on: TallySetup-<version>.exe on Windows, or
Tally-<version>.dmg on an Apple Silicon Mac. Run with Python 3.11 or newer:

    python build/build.py

It works in a Tally-build folder (a virtual environment with the pinned packages, plus
PyInstaller's output) in %LOCALAPPDATA% or the temp folder, and writes the result to dist/.
The Windows installer needs Inno Setup 6:  winget install JRSoftware.InnoSetup
"""
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WINDOWS, MAC = sys.platform == "win32", sys.platform == "darwin"
WORK = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "Tally-build"
PY = WORK / "venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def run(*args, **kw):
    print(">", *args, flush=True)
    subprocess.run([str(a) for a in args], check=True, **kw)


def find_iscc():
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        for sub in (r"Programs\Inno Setup 6", "Inno Setup 6"):
            path = Path(base or "") / sub / "ISCC.exe"
            if path.exists():
                return path
    return shutil.which("iscc")


def windows_installer(version, src, dist):
    iscc = find_iscc()
    if not iscc:
        print(f"Built {WORK / 'dist' / 'Tally'}. Install Inno Setup 6 to make the installer.")
        return
    out = WORK / "out"
    run(iscc, "/Q", f"/DAppVersion={version}", f"/DSourceDir={WORK / 'dist' / 'Tally'}",
        f"/DIconFile={src / 'tally.ico'}", f"/O{out}", REPO / "build" / "tally.iss")
    setup = out / f"TallySetup-{version}.exe"
    shutil.copy2(setup, dist / setup.name)
    print(f"Done: {dist / setup.name}")


def mac_disk_image(version, dist):
    app = WORK / "dist" / "Tally.app"
    info = app / "Contents" / "Info.plist"
    plist = plistlib.loads(info.read_bytes())
    plist.update({
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": True,   # a menu bar app: no Dock icon
        "NSBluetoothAlwaysUsageDescription": "Tally talks to your Hue lamp over Bluetooth to show what Claude Code is doing.",
    })
    info.write_bytes(plistlib.dumps(plist))
    # Editing Info.plist breaks PyInstaller's signature, and Apple Silicon won't run unsigned code,
    # so sign it again (ad hoc: no Apple Developer account involved).
    run("codesign", "--force", "--deep", "--sign", "-", app)

    stage = WORK / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    run("ditto", app, stage / "Tally.app")
    (stage / "Applications").symlink_to("/Applications")   # drag Tally onto this to install
    image = dist / f"Tally-{version}.dmg"
    image.unlink(missing_ok=True)
    run("hdiutil", "create", "-volname", "Tally", "-srcfolder", stage, "-format", "UDZO", image)
    print(f"Done: {image}")


def main():
    version = re.search(r'__version__ = "(.+?)"', (REPO / "tally" / "__init__.py").read_text()).group(1)
    WORK.mkdir(exist_ok=True)
    if not PY.exists():
        run(sys.executable, "-m", "venv", WORK / "venv")
    run(PY, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", REPO / "build" / "requirements.txt")

    # PyInstaller is happier with a local copy than with a \\wsl$ path.
    src = WORK / "src"
    shutil.rmtree(src, ignore_errors=True)
    shutil.copytree(REPO / "tally", src / "tally", ignore=shutil.ignore_patterns("__pycache__"))
    (src / "Tally.py").write_text("from tally.app import main\n\nmain()\n")
    icon = "tally.ico" if WINDOWS else "tally.png"
    run(PY, "-c", "from tally.icon import save_ico, lamp, COLOURS; "
        "save_ico('tally.ico'); lamp(COLOURS['working'], 1024, backdrop=True).save('tally.png')", cwd=src)

    args = ["--noconfirm", "--clean", "--log-level", "WARN", "--windowed", "--name", "Tally", "--icon", icon,
            "--add-data", f"tally{os.sep}dashboard.html{os.pathsep}tally",
            "--distpath", WORK / "dist", "--workpath", WORK / "pyi"]
    if WINDOWS:
        args += ["--hidden-import", "bleak.backends.winrt.scanner", "--hidden-import", "bleak.backends.winrt.client",
                 "--collect-submodules", "winrt"]
    elif MAC:
        args += ["--osx-bundle-identifier", "com.n-k-12.tally",
                 "--hidden-import", "bleak.backends.corebluetooth.scanner",
                 "--hidden-import", "bleak.backends.corebluetooth.client",
                 "--hidden-import", "pystray._darwin", "--hidden-import", "PyObjCTools.AppHelper"]
    run(PY, "-m", "PyInstaller", *args, "Tally.py", cwd=src)

    dist = REPO / "dist"
    dist.mkdir(exist_ok=True)
    if WINDOWS:
        windows_installer(version, src, dist)
    elif MAC:
        mac_disk_image(version, dist)


if __name__ == "__main__":
    main()
