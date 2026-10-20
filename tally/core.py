"""Settings, lamps, and the controller that decides what the lamp shows."""
import asyncio
import json
import math
import re
import time

from . import data_dir

BEHAVIOURS = ("steady", "breathe", "pulse", "flash")

# Colour temperature is in kelvin and brightness in percent here; the lamp
# itself takes mireds (1,000,000 / kelvin) and brightness 1-254.
DEFAULTS = {
    "version": 1,
    "setup_done": False,
    "states": {
        "working": {"behaviour": "steady", "kelvin": 2700, "brightness": 60, "period": 4.0},
        "needs": {"behaviour": "pulse", "kelvin": 2200, "brightness": 100, "period": 2.2},
        "done": {"behaviour": "flash", "kelvin": 4000, "brightness": 100, "period": 0.6, "seconds": 2.0},
    },
    "several": "urgent",          # "urgent": needs you beats working; "latest": the most recent change wins
    "quiet": {"enabled": False, "from": "22:00", "until": "08:00"},
    "paused": False,
    "forget_after": 45,           # minutes before a session that has sent nothing is dropped
    # The chosen lamp. Its ends are found by eye on the dashboard (0 means not yet):
    # lamps report back whatever they were sent, so the range can't be read from them.
    "lamp": {"address": "", "name": "", "min_mireds": 0, "max_mireds": 0},
    "check_updates": True,
}
LIMITS = {"kelvin": (1000, 20000), "brightness": (1, 100), "period": (0.4, 10),
          "seconds": (0.5, 10), "forget_after": (5, 480), "min_mireds": (0, 1000), "max_mireds": (0, 1000)}
CHOICES = {"behaviour": BEHAVIOURS, "several": ("urgent", "latest")}
TEXT = {"address": 64, "name": 80}
HOLD_SECONDS = 120                # while finding the ends, give the lamp back after this long untouched
DEFAULT_MIREDS = (50, 1000)       # 20,000 K to 1,000 K, until the ends are found on the lamp itself
RECONNECT_SECONDS = 20


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
    if key in TEXT:
        return value[:TEXT[key]] if isinstance(value, str) else current
    if key in ("from", "until"):
        ok = isinstance(value, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value)
        return value if ok else current
    return current


def settings_file():
    return data_dir() / "settings.json"


def load_settings():
    path = settings_file()
    try:
        return clean(json.loads(path.read_text(encoding="utf-8")), DEFAULTS)
    except FileNotFoundError:
        return clean({}, DEFAULTS)
    except (OSError, ValueError) as err:
        log(f"could not read {path} ({err}); using defaults")
        return clean({}, DEFAULTS)


def save_settings(cfg):
    path = settings_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(path)


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

class NoLamp:
    """Stands in until a lamp has been chosen."""

    ok = False

    async def connect(self):
        return False

    async def close(self):
        pass

    def info(self):
        return {"kind": "none", "name": "", "address": "", "status": "none", "connected": False, "colour": False}

    async def read(self):
        return None

    async def set(self, power=None, mireds=None, brightness=None):
        pass


class FakeLamp:
    """Prints what a real lamp would do. Use it to try Tally without Bluetooth."""

    mired_range = (153, 500)
    address = "FA:KE:00:00:00:01"

    def __init__(self):
        self.state = {"power": True, "mireds": 300, "brightness": 200}
        self.ok = False

    async def connect(self):
        self.ok = True
        log("fake lamp ready")
        return True

    async def close(self):
        self.ok = False

    def info(self):
        return {"kind": "fake", "name": "Fake lamp", "address": self.address,
                "status": "connected" if self.ok else "none", "connected": self.ok, "colour": False}

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
    def __init__(self, address, name=""):
        self.address = address
        self.name = name or address
        self.light = None
        self.ok = False
        self.status = "connecting"    # connecting, connected, not found, unreachable

    async def connect(self):
        from bleak import BleakScanner
        import HueBLE

        self.status = "connecting"
        try:
            if self.light is None:
                log(f"looking for {self.address} ...")
                device = await BleakScanner.find_device_by_address(self.address, timeout=15)
                if device is None:
                    self.status = "not found"
                    log("lamp not found; is it powered on and close by?")
                    return False
                self.light = HueBLE.HueBleLight(device)
            await self.light.connect()
        except Exception as err:
            self.ok, self.light, self.status = False, None, "unreachable"
            log(f"could not connect to the lamp ({err})")
            return False
        self.ok, self.status = True, "connected"
        log(f"connected to {self.name}")
        return True

    async def close(self):
        self.ok = False
        if self.light:
            try:
                await self.light.disconnect()
            except Exception:
                pass

    def info(self):
        return {"kind": "hue", "name": self.name, "address": self.address, "status": self.status,
                "connected": self.ok, "colour": bool(getattr(self.light, "supports_colour_xy", False))}

    async def _retry(self, action):
        if not self.ok:
            return None   # the controller reconnects in the background
        try:
            return await action()
        except Exception as err:  # Bluetooth drops happen; try once more after reconnecting
            log(f"Bluetooth error ({err}); reconnecting")
            try:
                await self.light.connect()
                return await action()
            except Exception as err2:
                log(f"still failing ({err2}); will keep trying in the background")
                self.ok, self.status = False, "unreachable"
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


async def scan_lamps(timeout=10):
    """Hue lamps nearby as [{"address", "name"}]. A lamp's name often arrives in a later
    packet than the one that says it is a Hue lamp, so names keep being filled in."""
    from bleak import BleakScanner
    import HueBLE

    found = {}

    def seen(device, adv):
        if HueBLE.UUID_HUE_IDENTIFIER in adv.service_uuids or device.address in found:
            found[device.address] = adv.local_name or device.name or found.get(device.address)

    log(f"scanning for Hue lamps for {timeout} s ...")
    async with BleakScanner(seen):
        await asyncio.sleep(timeout)
    return [{"address": address, "name": name or "Hue lamp"} for address, name in found.items()]


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
        self.connecting = False

    def muted(self):
        return self.cfg["paused"] or quiet_now(self.cfg["quiet"])

    def mired_range(self):
        measured = self.cfg["lamp"]
        if measured["min_mireds"] and measured["max_mireds"]:
            return measured["min_mireds"], measured["max_mireds"]
        return DEFAULT_MIREDS

    # ----- the lamp itself

    async def connect_lamp(self):
        if self.connecting or self.lamp.ok:
            return
        self.connecting = True
        try:
            ok = await self.lamp.connect()
        finally:
            self.connecting = False
        if ok:
            self.dirty = True
            await self.update()

    async def use_lamp(self, lamp):
        """Switch to another lamp, giving the old one back first."""
        async with self.lock:
            await self._stop()
            if self.shown != "idle":
                await self._restore()
            old, self.lamp = self.lamp, lamp
            self.shown, self.saved = "idle", None
        await old.close()
        await self.connect_lamp()

    async def shutdown(self):
        """Give the lamp back and let go of it, for quitting."""
        async with self.lock:
            await self._stop()
            if self.shown != "idle" or self.held:
                await self._restore()
            self.holding = True       # nothing more changes the lamp from here
        await self.lamp.close()

    # ----- sessions

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

    # ----- showing states

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

    # ----- testing and finding the lamp's ends

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

    # ----- reporting

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
        """Every so often: drop silent sessions, follow quiet hours, and reconnect a lost lamp."""
        while True:
            await asyncio.sleep(RECONNECT_SECONDS)
            await self.update()
            if not self.lamp.ok and not isinstance(self.lamp, NoLamp):
                await self.connect_lamp()
