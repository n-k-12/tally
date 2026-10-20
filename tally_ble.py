#!/usr/bin/env python3
"""Tally, Bluetooth test version.

Lights a Philips Hue Bluetooth lamp (White Ambiance or colour) while
Claude Code is working, pulses it when Claude needs your approval,
and flashes it when the last session finishes.

  python3 tally_ble.py scan                  find Hue lamps nearby
  python3 tally_ble.py demo ADDRESS          play every state once on the lamp
  python3 tally_ble.py serve ADDRESS         keep the lamp connected and listen for Claude Code
  python3 tally_ble.py serve --fake          same, but print to the terminal instead of using a lamp
  python3 tally_ble.py signal STATE          send a state by hand: working, needs-you, done, end
  python3 tally_ble.py status                show what the server is tracking

While 'serve' runs, open http://127.0.0.1:4567/ for the control station: live
sessions and how the lamp shows each state. Settings are kept in
tally_settings.json and the page in tally_dashboard.html, both next to this file.

Needs Python 3.11+ and:  python3 -m pip install HueBLE==2.2.3
"""
import argparse
import asyncio
import json
import math
import re
import sys
import time
import urllib.request
from pathlib import Path

PORT = 4567
HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "tally_settings.json"
DASHBOARD_FILE = HERE / "tally_dashboard.html"

STATES = ("working", "needs-you", "done", "end")
BEHAVIOURS = ("steady", "breathe", "pulse", "flash")

# Colour temperature is in kelvin and brightness in percent here; the lamp
# itself takes mireds (1,000,000 / kelvin) and brightness 1-254.
DEFAULTS = {
    "states": {
        "working": {"behaviour": "steady", "kelvin": 2700, "brightness": 60, "period": 4.0},
        "needs": {"behaviour": "pulse", "kelvin": 2200, "brightness": 100, "period": 2.2},
        "done": {"behaviour": "flash", "kelvin": 4000, "brightness": 100, "period": 0.6, "seconds": 2.0},
    },
    "several": "urgent",          # "urgent": needs you beats working; "latest": the most recent change wins
    "quiet": {"enabled": False, "from": "22:00", "until": "08:00"},
    "paused": False,
    "forget_after": 45,           # minutes before a session that has sent nothing is dropped
    # The lamp's real ends, found by eye on the dashboard; 0 means not set yet. Lamps
    # report back whatever they were sent, so the range can't be read from them.
    "lamp": {"min_mireds": 0, "max_mireds": 0},
}
LIMITS = {"kelvin": (1000, 20000), "brightness": (1, 100), "period": (0.4, 10),
          "seconds": (0.5, 10), "forget_after": (5, 480), "min_mireds": (0, 1000), "max_mireds": (0, 1000)}
HOLD_SECONDS = 120                # while finding the ends, give the lamp back after this long untouched
CHOICES = {"behaviour": BEHAVIOURS, "several": ("urgent", "latest")}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------- settings

def clean(value, current, key=None):
    """Merge a settings change into the current settings, keeping only known keys and sane values."""
    if isinstance(current, dict):
        value = value if isinstance(value, dict) else {}
        return {k: clean(value[k], v, k) if k in value else v for k, v in current.items()}
    if isinstance(current, bool):
        return value if isinstance(value, bool) else current
    if key in LIMITS:
        try:
            lo, hi = LIMITS[key]
            v = min(hi, max(lo, float(value)))
        except (TypeError, ValueError):
            return current
        return round(v) if isinstance(current, int) else round(v, 2)
    if key in CHOICES:
        return value if value in CHOICES[key] else current
    if key in ("from", "until"):
        ok = isinstance(value, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value)
        return value if ok else current
    return current


def load_settings():
    try:
        return clean(json.loads(SETTINGS_FILE.read_text()), DEFAULTS)
    except FileNotFoundError:
        return clean({}, DEFAULTS)
    except (OSError, ValueError) as err:
        log(f"could not read {SETTINGS_FILE.name} ({err}); using defaults")
        return clean({}, DEFAULTS)


def save_settings(cfg):
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(SETTINGS_FILE)


def quiet_now(q):
    if not q["enabled"]:
        return False
    now, start, end = time.strftime("%H:%M"), q["from"], q["until"]
    return start <= now < end if start <= end else now >= start or now < end


def wave(behaviour, top):
    """Brightness steps for one cycle as (level, share of the cycle). Level 0 means off.

    The lamp fades between levels on its own, so a few steps look smooth.
    """
    low = max(1, round(top * 0.2))
    if behaviour == "breathe":
        return [(round(low + (top - low) * (1 + math.cos(math.pi * i / 4)) / 2), 1 / 8) for i in range(8)]
    if behaviour == "pulse":
        return [(top, 0.5), (low, 0.5)]
    if behaviour == "flash":
        return [(top, 0.5), (0, 0.5)]
    return []


# --------------------------------------------------------------------------- lamps

class FakeLamp:
    """Prints what a real lamp would do. Use it to test the hooks without Bluetooth."""

    mired_range = (153, 500)

    def __init__(self):
        self.state = {"power": True, "mireds": 300, "brightness": 200}

    async def connect(self):
        log("fake lamp ready")

    def info(self):
        return {"kind": "fake", "name": "Fake lamp", "connected": True, "colour": False}

    async def read(self):
        return dict(self.state)

    async def set(self, power=None, mireds=None, brightness=None):
        if mireds is not None:
            lo, hi = self.mired_range     # like a real lamp, it stays within what it can do
            mireds = min(hi, max(lo, mireds))
        for key, value in (("power", power), ("mireds", mireds), ("brightness", brightness)):
            if value is not None:
                self.state[key] = value
        if power is None and mireds is None:
            return  # brightness steps of an animation; printing them would drown the log
        parts = [f"{k}={v}" for k, v in (("power", power), ("mireds", mireds), ("brightness", brightness)) if v is not None]
        log("  lamp  " + "  ".join(parts))


class HueLamp:
    def __init__(self, address):
        self.address = address
        self.name = address
        self.light = None
        self.ok = False
        self.mired_range = (153, 500)

    async def connect(self):
        from bleak import BleakScanner
        import HueBLE

        log(f"looking for {self.address} (up to 20 s) ...")
        device = await BleakScanner.find_device_by_address(self.address, timeout=20)
        if device is None:
            sys.exit("Lamp not found. Check it is powered on and close by, then run 'scan' to confirm the address.")
        self.light = HueBLE.HueBleLight(device)
        await self.light.connect()
        self.ok = True
        self.name = device.name or self.address
        self.mired_range = (getattr(self.light, "minimum_mireds", None) or 153,
                            getattr(self.light, "maximum_mireds", None) or 500)
        log(f"connected to {self.name}")

    def info(self):
        return {"kind": "hue", "name": self.name, "address": self.address, "connected": self.ok,
                "colour": bool(getattr(self.light, "supports_colour_xy", False))}

    async def _retry(self, action):
        try:
            result = await action()
            self.ok = True
            return result
        except Exception as err:  # Bluetooth drops happen; try once more after reconnecting
            log(f"Bluetooth error ({err}); reconnecting")
            self.ok = False
            try:
                await self.light.connect()
                result = await action()
                self.ok = True
                return result
            except Exception as err2:
                log(f"still failing ({err2}); skipping this change")
                return None

    async def read(self):
        async def do():
            return {
                "power": await self.light.poll_power_state(),
                "mireds": await self.light.poll_colour_temp(),
                "brightness": await self.light.poll_brightness(),
            }
        return await self._retry(do)

    async def set(self, power=None, mireds=None, brightness=None):
        import HueBLE

        async def do():
            if power is True:
                await self.light.set_power(True)
            if mireds is not None:
                # HueBLE's set_colour_temp clamps to 153-500; write directly so a lamp with a wider range can use it.
                await self.light._write_gatt(HueBLE.UUID_TEMPERATURE, int(mireds).to_bytes(2, "little"))
            if brightness is not None:
                await self.light.set_brightness(brightness)
            if power is False:
                await self.light.set_power(False)
        await self._retry(do)


# --------------------------------------------------------------------------- state

class Controller:
    """Tracks every Claude Code session and decides what the lamp shows.

    By default Needs you beats Working; the dashboard can switch to "latest
    change wins". The lamp goes back to how you left it once no session is busy.
    """

    def __init__(self, lamp):
        self.lamp = lamp
        self.cfg = load_settings()
        self.sessions = {}            # session id -> {"state", "since", "seen", "started", "project", "cwd", "message"}
        self.shown = "idle"
        self.saved = None             # the lamp as it was before Tally took over
        self.lock = asyncio.Lock()    # one change of look at a time
        self.io = asyncio.Lock()      # one Bluetooth write at a time
        self.anim = None
        self.preview = None           # a state being tested from the dashboard
        self.test_task = None
        self.dirty = False            # settings changed, so redraw even if the state has not
        self.holding = False          # the dashboard is showing raw colour temperatures to find the lamp's ends
        self.held = False             # the lamp has been changed by holding and needs giving back
        self.release_timer = None

    def muted(self):
        return self.cfg["paused"] or quiet_now(self.cfg["quiet"])

    def mired_range(self):
        measured = self.cfg["lamp"]
        if measured["min_mireds"] and measured["max_mireds"]:
            return measured["min_mireds"], measured["max_mireds"]
        return self.lamp.mired_range

    def hold(self, mireds):
        """Show a raw colour temperature, past the usual range, while you look for the lamp's ends."""
        self.holding = True
        if self.release_timer:
            self.release_timer.cancel()
        self.release_timer = asyncio.create_task(self._release_later())
        asyncio.create_task(self._hold(mireds))

    async def _hold(self, mireds):
        async with self.lock:
            if not self.holding:
                return
            await self._stop()
            if self.shown == "idle" and not self.held:
                self.saved = await self.lamp.read()
            self.held = True
            await self._write(power=True, mireds=mireds, brightness=254)

    async def release(self):
        """Give the lamp back after holding: to how you left it, or to what the sessions need."""
        if self.release_timer and self.release_timer is not asyncio.current_task():
            self.release_timer.cancel()
        self.release_timer = None
        async with self.lock:
            if not self.holding:
                return
            self.holding = False
            if self.held and self.shown == "idle":
                await self._restore()
            self.held = False
            self.dirty = True
        await self.update()

    async def _release_later(self):
        await asyncio.sleep(HOLD_SECONDS)
        log("finding the lamp's ends was left open; giving the lamp back")
        await self.release()

    def signal(self, state, sid, info=None):
        info = info or {}
        now = time.time()
        s = self.sessions.get(sid)
        if state == "needs-you" and (s is None or s["state"] == "idle"):
            log(f"ignored needs-you from an idle session {sid[:8]}")
            return
        finished = False
        if state == "end":
            self.sessions.pop(sid, None)
        else:
            new = {"working": "working", "needs-you": "needs", "done": "idle"}[state]
            if s is None:
                s = self.sessions[sid] = {"state": new, "since": now, "started": now,
                                          "project": "", "cwd": "", "message": ""}
            elif s["state"] != new:
                finished = new == "idle"
                s["state"], s["since"] = new, now
            s["seen"] = now
            cwd = info.get("cwd")
            if isinstance(cwd, str) and cwd:
                s["cwd"] = cwd
                s["project"] = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            message = info.get("message")
            s["message"] = message if new == "needs" and isinstance(message, str) else ""
        busy = sum(v["state"] != "idle" for v in self.sessions.values())
        if self.muted() or (self.cfg["several"] == "urgent" and busy):
            finished = False
        log(f"{state:<9} session {sid[:8]}  {s['project'] if s else ''}  ({busy} busy)")
        asyncio.create_task(self.update(finished))

    def forget(self, sid):
        if self.sessions.pop(sid, None):
            log(f"forgot session {sid[:8]} from the dashboard")
        asyncio.create_task(self.update())

    def apply(self, change):
        self.cfg = clean(change, self.cfg)
        save_settings(self.cfg)
        self.dirty = True
        asyncio.create_task(self.update())

    def prune(self):
        now, limit = time.time(), self.cfg["forget_after"] * 60
        for sid in [s for s, v in self.sessions.items() if now - v["seen"] > limit]:
            log(f"forgot quiet session {sid[:8]}")
            del self.sessions[sid]

    def target(self):
        self.prune()
        if self.preview:
            return self.preview
        busy = [v for v in self.sessions.values() if v["state"] != "idle"]
        if self.muted() or not busy:
            return "idle"
        if self.cfg["several"] == "latest":
            return max(busy, key=lambda v: v["since"])["state"]
        return "needs" if any(v["state"] == "needs" for v in busy) else "working"

    async def update(self, finished=False):
        """Bring the lamp to the target state, playing the Finished look first if asked."""
        async with self.lock:
            if self.holding:
                return
            target = self.target()
            if target == self.shown and not finished and not self.dirty:
                return
            self.dirty = False
            await self._stop()
            if self.shown == "idle" and (target != "idle" or finished):
                self.saved = await self.lamp.read()
            if finished:
                log("lamp -> finished")
                await self._play("done", self.cfg["states"]["done"]["seconds"])
            if target != "idle":
                log(f"lamp -> {target}")
                self.anim = asyncio.create_task(self._play(target))
            elif self.shown != "idle" or finished:
                log("lamp -> back to how you left it")
                await self._restore()
            self.shown = target

    async def _stop(self):
        if self.anim:
            self.anim.cancel()
            await asyncio.wait([self.anim])
            self.anim = None

    async def _write(self, **change):
        async with self.io:
            await self.lamp.set(**change)

    async def _play(self, name, seconds=None):
        """Show one state's look, forever or for a number of seconds."""
        look = self.cfg["states"][name]
        lo, hi = self.mired_range()
        mireds = min(hi, max(lo, round(1_000_000 / look["kelvin"])))
        top = max(1, round(look["brightness"] * 254 / 100))
        await self._write(power=True, mireds=mireds, brightness=top)
        steps = wave(look["behaviour"], top)
        end = time.monotonic() + seconds if seconds else math.inf
        i, off = 0, False
        while True:
            if steps and i:
                level, _ = steps[i % len(steps)]
                if level == 0:
                    await self._write(power=False)
                else:
                    await self._write(power=True if off else None, brightness=level)
                off = level == 0
            share = steps[i % len(steps)][1] if steps else 1
            wait = min(look["period"] * share, end - time.monotonic())
            if wait <= 0:
                return
            await asyncio.sleep(wait)
            i += 1

    async def _restore(self):
        s = self.saved
        if s and s.get("power"):
            await self._write(power=True, mireds=s.get("mireds"), brightness=s.get("brightness"))
        else:
            await self._write(power=False)

    def test(self, which):
        if self.test_task:
            self.test_task.cancel()
        self.test_task = asyncio.create_task(self.run_test(which))

    async def run_test(self, which):
        """Play one state, or "all" of them, regardless of sessions, pause or quiet hours."""
        for name in ("working", "needs", "done") if which == "all" else (which,):
            if name == "done":
                self.preview = None
                await self.update(finished=True)
            else:
                self.preview = name
                await self.update()
                await asyncio.sleep(4)
        self.preview = None
        await self.update()

    def snapshot(self):
        self.prune()
        now = time.time()
        lo, hi = self.mired_range()
        kmin, kmax = LIMITS["kelvin"]
        sessions = sorted(self.sessions.items(), key=lambda kv: kv[1]["started"])
        return {
            "lamp": {**self.lamp.info(), "showing": self.shown, "testing": self.preview is not None,
                     "kelvin": [max(kmin, math.ceil(1_000_000 / hi)), min(kmax, math.floor(1_000_000 / lo))],
                     "measured": bool(self.cfg["lamp"]["min_mireds"] and self.cfg["lamp"]["max_mireds"])},
            "paused": self.cfg["paused"],
            "quiet_now": quiet_now(self.cfg["quiet"]),
            "sessions": [{"id": sid, "state": v["state"], "project": v["project"], "cwd": v["cwd"],
                          "message": v["message"], "for": round(now - v["since"]), "seen": round(now - v["seen"])}
                         for sid, v in sessions],
            "settings": self.cfg,
        }

    async def sweep(self):
        while True:
            await asyncio.sleep(20)
            await self.update()


# --------------------------------------------------------------------------- server

async def serve(lamp):
    await lamp.connect()
    ctl = Controller(lamp)

    async def api(name, data):
        extra = {}
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
        else:
            return "404 Not Found", extra
        return "200 OK", extra

    async def handle(reader, writer):
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
                ctl.signal(path, info.get("session_id") or "manual", info)
            elif path == "" and method == "GET":
                try:
                    reply, kind = DASHBOARD_FILE.read_bytes(), "text/html; charset=utf-8"
                except OSError:
                    code, reply = "404 Not Found", f"{DASHBOARD_FILE.name} is missing; keep it next to tally_ble.py".encode()
            elif path in ("status", "api/state"):
                reply, kind = json.dumps(ctl.snapshot(), indent=2).encode(), "application/json"
            elif path.startswith("api/") and method == "POST":
                # Insisting on JSON stops other websites changing settings with a plain form post.
                if not headers.get("content-type", "").startswith("application/json"):
                    code = "415 Unsupported Media Type"
                else:
                    data = json.loads(body or b"{}")
                    code, extra = await api(path[4:], data if isinstance(data, dict) else {})
                    reply, kind = json.dumps({**ctl.snapshot(), **extra}).encode(), "application/json"
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

    server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    log(f"control station on http://127.0.0.1:{PORT}/  (Ctrl+C to stop)")
    async with server:
        await asyncio.gather(server.serve_forever(), ctl.sweep())


async def demo(lamp):
    await lamp.connect()
    await Controller(lamp).run_test("all")
    log("demo finished; the lamp should be back how you left it")


async def scan():
    import HueBLE
    from bleak import BleakScanner

    log("scanning for Hue lamps for 10 s ...")
    lights = await HueBLE.discover_lights(timeout=10)
    if lights:
        for d in lights:
            print(f"  {d.address}   {d.name or '(no name)'}")
        return
    print("No Hue lamps recognised. Named Bluetooth devices nearby:")
    for d in await BleakScanner.discover(timeout=8):
        if d.name:
            print(f"  {d.address}   {d.name}")


def send(state):
    body = b"{}"
    if not sys.stdin.isatty():
        body = sys.stdin.read().encode() or b"{}"
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/{state}", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=1.5).read()
    except Exception as err:
        print(f"Tally server not reachable ({err}). Is 'serve' running?", file=sys.stderr)
    # always exit 0 so a hook never gets in Claude's way


def status():
    try:
        print(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status", timeout=1.5).read().decode())
    except Exception as err:
        print(f"Tally server not reachable ({err}). Is 'serve' running?")

def main():
    p = argparse.ArgumentParser(description="Tally: light a Hue Bluetooth lamp while Claude Code works.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="find Hue lamps nearby")
    for name in ("demo", "serve"):
        sp = sub.add_parser(name)
        sp.add_argument("address", nargs="?", help="lamp address from 'scan'")
        sp.add_argument("--fake", action="store_true", help="print instead of using a real lamp")
    sg = sub.add_parser("signal", help="send a state to the running server")
    sg.add_argument("state", choices=STATES)
    sub.add_parser("status", help="show what the server is tracking")
    args = p.parse_args()

    if args.cmd == "signal":
        return send(args.state)
    if args.cmd == "status":
        return status()
    try:
        if args.cmd == "scan":
            asyncio.run(scan())
        else:
            if not args.fake and not args.address:
                sys.exit(f"Give the lamp address from 'scan', or use --fake. Example: python3 tally_ble.py {args.cmd} --fake")
            lamp = FakeLamp() if args.fake else HueLamp(args.address)
            asyncio.run(serve(lamp) if args.cmd == "serve" else demo(lamp))
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
