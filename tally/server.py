"""The local web server: Claude Code hooks post to it, and it serves the dashboard and its API."""
import asyncio
import json
import urllib.request
from pathlib import Path

from . import PORT, UPDATE_REPO, __version__, hooks
from .core import Controller, FakeLamp, HueLamp, NoLamp, log, scan_lamps

DASHBOARD = Path(__file__).with_name("dashboard.html")
STATES = ("working", "needs-you", "done", "end")
UPDATE_EVERY = 24 * 3600


def version_tuple(text):
    return tuple(int("".join(c for c in part if c.isdigit()) or 0) for part in text.lstrip("vV").split("."))


def latest_release():
    """The newest release on GitHub if it is newer than this one, else None."""
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": f"Tally/{__version__}"})
    with urllib.request.urlopen(req, timeout=10) as res:
        data = json.load(res)
    tag = data.get("tag_name") or ""
    if version_tuple(tag) > version_tuple(__version__):
        return {"version": tag.lstrip("vV"), "url": data.get("html_url")}
    return None


class Server:
    def __init__(self, fake=False, port=PORT):
        self.fake = fake
        self.port = port
        self.ctl = Controller(NoLamp())
        lamp = self.ctl.cfg["lamp"]
        self.ctl.lamp = self.make_lamp(lamp["address"], lamp["name"])
        self.update = None            # {"version", "url"} once a newer release is out
        self.claude = None            # Claude Code installs found by hooks.targets(), looked up when first asked
        self.on_quit = None           # set by the tray app so a quit from the dashboard closes it too
        self.stopped = None

    def make_lamp(self, address, name=""):
        if not address:
            return NoLamp()
        return FakeLamp() if self.fake else HueLamp(address, name)

    def snapshot(self):
        snap = self.ctl.snapshot()
        snap["app"] = {"version": __version__, "setup_done": self.ctl.cfg["setup_done"],
                       "update": self.update, "fake": self.fake}
        return snap

    # ----- API

    async def claude_status(self, refresh=False):
        loop = asyncio.get_running_loop()
        if self.claude is None or refresh:
            self.claude = await loop.run_in_executor(None, hooks.targets)
        return await loop.run_in_executor(None, hooks.status, self.claude)

    async def api(self, name, data):
        """Handle one POST /api/<name>. Returns (HTTP status, extra fields for the reply)."""
        ctl, extra = self.ctl, {}
        if name == "settings":
            ctl.apply(data)
        elif name == "test" and data.get("state") in ("all", "working", "needs", "done"):
            ctl.test(data["state"])
        elif name == "forget":
            ctl.forget(str(data.get("id")))
        elif name == "hold" and isinstance(data.get("mireds"), (int, float)):
            ctl.hold(min(1000, max(50, round(data["mireds"]))))
        elif name == "release":
            await ctl.release()
        elif name == "scan":
            try:
                if self.fake:
                    await asyncio.sleep(1)
                    extra["lamps"] = [{"address": FakeLamp.address, "name": "Fake lamp"}]
                else:
                    extra["lamps"] = await scan_lamps()
            except Exception as err:
                log(f"scan failed ({err})")
                extra["lamps"], extra["scan_error"] = [], "Bluetooth seems to be off or unavailable on this PC."
        elif name == "lamp" and isinstance(data.get("address"), str) and data["address"]:
            address, name_ = data["address"], str(data.get("name") or "")
            change = {"address": address, "name": name_}
            if address != ctl.cfg["lamp"]["address"]:
                change.update(min_mireds=0, max_mireds=0)   # a different lamp has its own ends
            ctl.apply({"lamp": change})
            asyncio.create_task(ctl.use_lamp(self.make_lamp(address, name_)))
        elif name == "claude":
            action, target = data.get("action"), data.get("id")
            if action in ("connect", "disconnect"):
                found = [t for t in (self.claude or []) if t["id"] == target]
                if not found:
                    return "404 Not Found", extra
                job = hooks.connect if action == "connect" else hooks.disconnect
                try:
                    await asyncio.get_running_loop().run_in_executor(None, job, found[0])
                    log(f"Claude Code {action}ed: {found[0]['label']}")
                except (OSError, ValueError) as err:
                    extra["claude_error"] = f"Couldn't change {found[0]['path']}: {err}"
            extra["claude"] = await self.claude_status(refresh=action == "refresh")
        elif name == "quit":
            asyncio.get_running_loop().call_later(0.2, self.quit)
        else:
            return "404 Not Found", extra
        return "200 OK", extra

    async def handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)
            body = await asyncio.wait_for(reader.readexactly(length), 5) if length else b""
            path = path.split("?")[0].strip("/")
            code, reply, kind = "200 OK", b"", "text/plain"
            if path in STATES:
                try:
                    info = json.loads(body or b"{}")
                except ValueError:
                    info = {}
                info = info if isinstance(info, dict) else {}
                self.ctl.signal(path, info.get("session_id") or "manual", info)
            elif path == "" and method == "GET":
                reply, kind = DASHBOARD.read_bytes(), "text/html; charset=utf-8"
            elif path in ("status", "api/state"):
                reply, kind = json.dumps(self.snapshot(), indent=2).encode(), "application/json"
            elif path.startswith("api/") and method == "POST":
                # Insisting on JSON stops other websites changing settings with a plain form post.
                if not headers.get("content-type", "").startswith("application/json"):
                    code = "415 Unsupported Media Type"
                else:
                    data = json.loads(body or b"{}")
                    code, extra = await self.api(path[4:], data if isinstance(data, dict) else {})
                    reply, kind = json.dumps({**self.snapshot(), **extra}).encode(), "application/json"
            else:
                code = "404 Not Found"
            # Claude Code treats an empty 2xx body as plain success.
            writer.write(f"HTTP/1.1 {code}\r\nContent-Type: {kind}\r\nContent-Length: {len(reply)}\r\n"
                         f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + reply)
            await writer.drain()
        except Exception as err:
            log(f"bad request ignored ({err})")
        finally:
            writer.close()

    # ----- running

    async def check_updates(self):
        loop = asyncio.get_running_loop()
        while True:
            if self.ctl.cfg["check_updates"]:
                try:
                    self.update = await loop.run_in_executor(None, latest_release)
                    if self.update:
                        log(f"Tally {self.update['version']} is available")
                except Exception as err:
                    log(f"update check failed ({err})")
            await asyncio.sleep(UPDATE_EVERY)

    async def refresh_hooks(self):
        """Bring Windows' Claude Code hooks up to this version if an older Tally wrote them."""
        def job():
            windows = hooks.windows_target()
            if hooks.state_of(windows["path"]) == "outdated":
                hooks.connect(windows)
                log("updated the Claude Code hooks to this version")
        try:
            await asyncio.get_running_loop().run_in_executor(None, job)
        except (OSError, ValueError) as err:
            log(f"could not check the Claude Code hooks ({err})")

    def quit(self):
        if self.on_quit:
            self.on_quit()
        if self.stopped:
            self.stopped.set()

    async def run(self, ready=None):
        self.stopped = asyncio.Event()
        server = await asyncio.start_server(self.handle, "127.0.0.1", self.port)
        log(f"Tally {__version__} on http://127.0.0.1:{self.port}/")
        tasks = [asyncio.create_task(self.ctl.sweep()), asyncio.create_task(self.ctl.connect_lamp())]
        if not self.fake:
            tasks += [asyncio.create_task(self.check_updates()), asyncio.create_task(self.refresh_hooks())]
        if ready:
            ready()
        async with server:
            await self.stopped.wait()
        for task in tasks:
            task.cancel()
        await self.ctl.shutdown()
        log("stopped")
