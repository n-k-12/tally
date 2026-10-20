#!/usr/bin/env python3
"""Can this Mac control a Philips Hue Bluetooth lamp? A five-minute check before Tally is built for macOS.

    pip3 install bleak
    python3 mac_lamp_test.py

Switch the lamp on, keep it close, and close the Hue app on your phone first. The script scans
for Hue lamps, connects, reads the lamp, blinks it, shows a warm then a cool white, and puts it
back how it was. If macOS asks to let Terminal use Bluetooth, or to pair with the lamp, say yes.
At the end it prints a result to copy and send back.
"""
import asyncio
import platform
import sys
import traceback

try:
    import bleak
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("This needs the bleak library first:  pip3 install bleak")

HUE = "0000fe0f-0000-1000-8000-00805f9b34fb"           # what Hue Bluetooth lamps advertise
POWER = "932c32bd-0002-47a2-835a-a8d455b859dd"         # 1 byte: 0 off, 1 on
BRIGHTNESS = "932c32bd-0003-47a2-835a-a8d455b859dd"    # 1 byte: 1-254
TEMPERATURE = "932c32bd-0004-47a2-835a-a8d455b859dd"   # 2 bytes, little-endian mireds
# Only this service is discovered. Newer lamps also offer Matter's setup service (fff6), which
# macOS reserves for itself; asking about every service makes the whole connection fail there.
CONTROL_SERVICE = "932c32bd-0000-47a2-835a-a8d455b859dd"

try:
    from importlib.metadata import version
    BLEAK_VERSION = version("bleak")
except Exception:
    BLEAK_VERSION = "?"

report = {"result": "did not finish", "system": platform.platform(), "python": platform.python_version(),
          "bleak": BLEAK_VERSION}


def step(text):
    print(f"\n{text}", flush=True)


async def write(client, uuid, data):
    await client.write_gatt_char(uuid, data, response=True)


async def main():
    step("1/5  Looking for Hue lamps for 10 seconds...")
    found = {}

    def seen(device, adv):
        if HUE in adv.service_uuids or device.address in found:
            name = adv.local_name or device.name or (found.get(device.address) or (None, None))[1]
            found[device.address] = (device, name)

    async with BleakScanner(seen):
        await asyncio.sleep(10)
    report["lamps seen"] = len(found)
    if not found:
        report["result"] = "NOT FOUND: no Hue lamp seen (is it on, close by, and not connected to a phone?)"
        return

    lamps = list(found.values())
    for i, (device, name) in enumerate(lamps, 1):
        print(f"     {i}. {name or 'Hue lamp'}  ({device.address})")
    pick = 0
    if len(lamps) > 1:
        answer = input("     Which lamp? Type its number and press Enter: ").strip()
        pick = int(answer) - 1 if answer.isdigit() and 0 < int(answer) <= len(lamps) else 0
    device, name = lamps[pick]
    report["lamp"] = name or "Hue lamp"

    step(f"2/5  Connecting to {name or 'the lamp'}...")
    client = BleakClient(device, timeout=30, services=[CONTROL_SERVICE])
    try:
        await client.connect()
    except Exception as err:
        report["connect error"] = repr(err)
        report["result"] = "CONNECT FAILED: the lamp was seen but the connection didn't complete"
        return
    async with client:
        report["connected"] = True
        report["controls visible"] = client.services.get_characteristic(POWER) is not None
        report["services"] = len(list(client.services))

        step("3/5  Reading the lamp. If macOS asks to pair or connect, click Pair/Connect...")
        try:
            power = (await client.read_gatt_char(POWER))[0]
            brightness = (await client.read_gatt_char(BRIGHTNESS))[0]
            temperature = int.from_bytes(await client.read_gatt_char(TEMPERATURE), "little")
        except Exception as err:
            report["read error"] = repr(err)
            report["result"] = "CONNECTS BUT LOCKED: the lamp was reached but its controls could not be read (pairing)"
            return
        report["lamp was"] = f"power {power}, brightness {brightness}, temperature {temperature} mireds"

        step("4/5  Blinking the lamp, then warm white, then cool white...")
        try:
            await write(client, POWER, b"\x01")
            await write(client, BRIGHTNESS, bytes([254]))
            for on in (0, 1, 0, 1):
                await write(client, POWER, bytes([on]))
                await asyncio.sleep(0.8)
            for mireds in (454, 153):
                await write(client, TEMPERATURE, mireds.to_bytes(2, "little"))
                await asyncio.sleep(1.5)
        except Exception as err:
            report["write error"] = repr(err)
            report["result"] = "READS BUT CAN'T CONTROL: the lamp could be read but not changed"
            return
        finally:
            step("     Putting the lamp back how it was...")
            try:
                await write(client, TEMPERATURE, temperature.to_bytes(2, "little"))
                await write(client, BRIGHTNESS, bytes([max(1, brightness)]))
                await write(client, POWER, bytes([power]))
            except Exception as err:
                report["restore error"] = repr(err)

    step("5/5  Did you see the lamp blink, then turn warm white, then cool white?")
    answer = input("     Type y or n and press Enter: ").strip().lower()
    report["seen by eye"] = answer or "(no answer)"
    if answer.startswith("y"):
        report["result"] = "WORKS: this Mac can control the lamp"
    elif answer.startswith("n"):
        report["result"] = "NO VISIBLE CHANGE: commands were accepted but the lamp didn't respond"
    else:
        report["result"] = "UNSURE: every command was accepted; whether the lamp changed wasn't confirmed"


try:
    asyncio.run(main())
except KeyboardInterrupt:
    report["result"] = "stopped by hand"
except Exception as err:
    report["error"] = repr(err)
    report["traceback"] = traceback.format_exc(limit=3)
    if report["result"] == "did not finish":
        report["result"] = "ERROR: see below"

lines = ["----- copy everything from here and send it back -----",
         *(f"{key}: {value}" for key, value in report.items()),
         "----- to here -----"]
print("\n" + "\n".join(lines))
try:
    from datetime import datetime
    from pathlib import Path
    saved = Path(__file__).resolve().with_name(f"results-{datetime.now():%Y%m%d-%H%M%S}.txt")
    saved.write_text("\n".join(lines) + "\n")
    print(f"\nResults saved to: {saved}\nSend back the block above or this file.")
except OSError:
    pass
