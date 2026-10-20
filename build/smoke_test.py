"""Check the app build.py made actually starts: its libraries are all inside, it serves the
dashboard, accepts a Claude Code hook, and quits when asked. Uses the pretend lamp, so no
Bluetooth is needed. Run after build.py:  python build/smoke_test.py
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import WORK, MAC  # noqa: E402

APP = WORK / "dist" / ("Tally.app/Contents/MacOS/Tally" if MAC else "Tally/Tally.exe")
URL = "http://127.0.0.1:4999"


def call(path, body=None):
    req = urllib.request.Request(URL + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.read()


def main():
    data = Path(tempfile.mkdtemp())
    env = {**__import__("os").environ, "TALLY_DATA": str(data)}

    subprocess.run([APP, "--self-check"], env=env, timeout=120, check=True)
    assert (data / "self-check-ok").exists(), "the Bluetooth and tray libraries didn't load"
    print("libraries load")

    app = subprocess.Popen([APP, "--no-tray", "--fake", "--port", "4999"], env=env)
    try:
        for _ in range(60):
            try:
                state = json.loads(call("/api/state"))
                break
            except OSError:
                time.sleep(1)
        else:
            raise SystemExit("the app never started serving")
        print("serving, version", state["app"]["version"])
        assert b"<title>Tally</title>" in call("/"), "the dashboard is missing"
        call("/working", {"session_id": "smoke-test", "cwd": "/tmp/project"})
        time.sleep(0.5)
        sessions = json.loads(call("/api/state"))["sessions"]
        assert [s["project"] for s in sessions] == ["project"], f"the hook wasn't recorded: {sessions}"
        print("dashboard and hooks work")
        call("/api/quit", {})
        app.wait(timeout=20)
        print("quits cleanly")
    finally:
        if app.poll() is None:
            app.kill()
            log = data / "tally.log"
            print(log.read_text() if log.exists() else "(no log)")


if __name__ == "__main__":
    main()
