r"""Build Tally.exe and the TallySetup installer. Run with Windows Python 3.11 or newer:

    python build\build.py

It works in %LOCALAPPDATA%\Tally-build (a virtual environment with the pinned
packages, plus PyInstaller's output) and writes the installer to dist\ in this repo.
The installer needs Inno Setup 6:  winget install JRSoftware.InnoSetup
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORK = Path(os.environ["LOCALAPPDATA"]) / "Tally-build"
PY = WORK / "venv" / "Scripts" / "python.exe"


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
    run(PY, "-c", "from tally.icon import save_ico; save_ico('tally.ico')", cwd=src)
    run(PY, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN", "--windowed",
        "--name", "Tally", "--icon", "tally.ico", "--add-data", f"tally{os.sep}dashboard.html{os.pathsep}tally",
        "--hidden-import", "bleak.backends.winrt.scanner", "--hidden-import", "bleak.backends.winrt.client",
        "--collect-submodules", "winrt", "--distpath", WORK / "dist", "--workpath", WORK / "pyi", "Tally.py", cwd=src)

    iscc = find_iscc()
    if not iscc:
        print(f"Built {WORK / 'dist' / 'Tally'}. Install Inno Setup 6 to make the installer.")
        return
    out = WORK / "out"
    run(iscc, "/Q", f"/DAppVersion={version}", f"/DSourceDir={WORK / 'dist' / 'Tally'}",
        f"/DIconFile={src / 'tally.ico'}", f"/O{out}", REPO / "build" / "tally.iss")
    dist = REPO / "dist"
    dist.mkdir(exist_ok=True)
    setup = out / f"TallySetup-{version}.exe"
    shutil.copy2(setup, dist / setup.name)
    print(f"Done: {dist / setup.name}")


if __name__ == "__main__":
    main()
