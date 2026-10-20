"""Tally's entry point: the tray app, plus a few switches for the installer and for development.

  Tally.exe                  run in the tray and open the dashboard
  Tally.exe --background     run in the tray without opening anything (used when Windows starts)
  Tally.exe --quit           close a running Tally, giving the lamp back first
  Tally.exe --uninstall      disconnect Claude Code, stop starting with Windows, close Tally
  python -m tally --fake --no-tray --port 4999
                             try it in a terminal with a pretend lamp
"""
import argparse
import asyncio
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

from . import PORT, __version__, data_dir, hooks
from .core import log
from .server import Server

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
FROZEN = getattr(sys, "frozen", False)


def running(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def quit_running(port):
    if not running(port):
        return
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass
    for _ in range(75):           # it gives the lamp back before closing; wait up to 15 s
        if not running(port):
            return
        time.sleep(0.2)


def autostart(enable=None):
    """Whether Tally starts with Windows. Pass True or False to change it."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        if enable is True:
            winreg.SetValueEx(key, "Tally", 0, winreg.REG_SZ, f'"{sys.executable}" --background')
        elif enable is False:
            try:
                winreg.DeleteValue(key, "Tally")
            except FileNotFoundError:
                pass
        try:
            winreg.QueryValueEx(key, "Tally")
            return True
        except FileNotFoundError:
            return False


def message(text):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Tally", 0x40)
    else:
        print(text)


def log_to_file():
    r"""A windowed app has no console, so the log goes to %APPDATA%\Tally\tally.log."""
    path = data_dir() / "tally.log"
    if path.exists() and path.stat().st_size > 1_000_000:
        path.replace(path.with_suffix(".log.1"))
    sys.stdout = sys.stderr = open(path, "a", buffering=1, encoding="utf-8")


def describe(server):
    ctl = server.ctl
    if ctl.cfg["paused"]:
        return "Paused"
    busy = [s for s in ctl.sessions.values() if s["state"] != "idle"]
    waiting = sum(s["state"] == "needs" for s in busy)
    if waiting:
        return f"{waiting} waiting for you"
    if busy:
        return f"{len(busy)} working"
    return "Idle"


def tray(server, loop, url):
    """Show the tray icon until Quit is chosen. Its lamp glows with the current state."""
    import pystray
    from .icon import COLOURS, lamp

    ctl = server.ctl
    images = {name: lamp(colour) for name, colour in COLOURS.items()}
    stopping = threading.Event()

    def later(fn, *args):
        loop.call_soon_threadsafe(fn, *args)

    menu = pystray.Menu(
        pystray.MenuItem("Open Tally", lambda icon, item: webbrowser.open(url), default=True),
        pystray.MenuItem(lambda item: f"Get Tally {server.update['version']}" if server.update else "Update",
                         lambda icon, item: webbrowser.open(server.update["url"]),
                         visible=lambda item: bool(server.update)),
        pystray.MenuItem(lambda item: "Resume lamp" if ctl.cfg["paused"] else "Pause lamp",
                         lambda icon, item: later(ctl.apply, {"paused": not ctl.cfg["paused"]})),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start with Windows", lambda icon, item: autostart(not autostart()),
                         checked=lambda item: FROZEN and autostart(), enabled=FROZEN),
        pystray.MenuItem("Check for updates", lambda icon, item: later(ctl.apply, {"check_updates": not ctl.cfg["check_updates"]}),
                         checked=lambda item: ctl.cfg["check_updates"]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Tally", lambda icon, item: icon.stop()),
    )
    icon = pystray.Icon("Tally", images["idle"], "Tally", menu)
    server.on_quit = icon.stop

    def follow(icon):
        icon.visible = True
        blink, told = False, None
        while not stopping.is_set():
            state = "paused" if ctl.cfg["paused"] else ctl.shown
            blink = not blink
            image = images["idle"] if state == "needs" and blink else images.get(state, images["idle"])
            if icon.icon is not image:
                icon.icon = image
            title = f"Tally · {describe(server)}"
            if icon.title != title:
                icon.title = title
            if server.update and told != server.update["version"]:
                told = server.update["version"]
                icon.update_menu()
                icon.notify(f"Tally {told} is available. Choose 'Get Tally {told}' in the tray menu.", "Tally")
            stopping.wait(0.7)

    icon.run(setup=follow)
    stopping.set()


def main(argv=None):
    p = argparse.ArgumentParser(prog="Tally", description="Light a Hue Bluetooth lamp while Claude Code works.")
    p.add_argument("--background", action="store_true", help="start without opening the dashboard")
    p.add_argument("--quit", action="store_true", help="close a running Tally")
    p.add_argument("--uninstall", action="store_true", help="disconnect Claude Code and stop starting with Windows")
    p.add_argument("--fake", action="store_true", help="use a pretend lamp that prints instead")
    p.add_argument("--no-tray", action="store_true", help="run in this terminal instead of the tray")
    p.add_argument("--port", type=int, default=PORT, help="for development only; Claude Code hooks use 4567")
    p.add_argument("--version", action="version", version=f"Tally {__version__}")
    args = p.parse_args(argv)

    if sys.stdout is None or (FROZEN and not args.no_tray):
        log_to_file()
    url = f"http://127.0.0.1:{args.port}/"

    if args.quit:
        return quit_running(args.port)
    if args.uninstall:
        log("uninstalling: disconnecting Claude Code and closing Tally")
        hooks.disconnect_all()
        if sys.platform == "win32":
            autostart(False)
        return quit_running(args.port)
    if running(args.port):        # already running: just show it
        if not args.background:
            webbrowser.open(url)
        return

    server = Server(fake=args.fake, port=args.port)
    if args.no_tray:
        try:
            asyncio.run(server.run())
        except KeyboardInterrupt:
            pass
        return

    loop = asyncio.new_event_loop()
    ready, failed = threading.Event(), threading.Event()

    def serve():
        try:
            loop.run_until_complete(server.run(ready.set))
        except Exception:
            traceback.print_exc()
            failed.set()
            ready.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready.wait(15)
    if failed.is_set() or not ready.is_set():
        message(f"Tally couldn't start: another program is probably using port {args.port}.")
        return
    if not args.background:
        webbrowser.open(url)
    tray(server, loop, url)
    server.on_quit = None
    loop.call_soon_threadsafe(server.quit)
    thread.join(15)
