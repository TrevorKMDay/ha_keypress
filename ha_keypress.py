#!/usr/bin/env python3
"""
Send keypresses from a keyboard/keypad attached to a Raspberry Pi Zero
to Home Assistant as events.

Each keypress fires an event named "pi_keypress" (configurable) with data:
    {"key": "KEY_A", "code": 30, "action": "down" | "up" | "hold", "device": "<name>"}

In Home Assistant, use an automation with an "Event" trigger:
    trigger:
      - platform: event
        event_type: pi_keypress
        event_data:
          key: KEY_1
          action: down

Setup on the Pi:
    sudo apt install python3-pip
    pip3 install evdev requests --break-system-packages
    export HA_URL="http://homeassistant.local:8123"
    export HA_TOKEN="your-long-lived-access-token"   # HA profile -> Security
    sudo -E python3 ha_keypress.py                   # root (or 'input' group) needed for /dev/input

Optional environment variables:
    HA_EVENT_TYPE   event name (default: pi_keypress)
    INPUT_DEVICE    path like /dev/input/event0 (default: auto-detect first keyboard)
    SEND_HOLD       "1" to also send auto-repeat "hold" events (default: off)
    GRAB_DEVICE     "1" to grab the device so keys don't also reach the Pi console
"""

import os
import queue
import sys
import threading
import time

import requests
from evdev import InputDevice, categorize, ecodes, list_devices

HA_URL = os.environ.get("HA_URL", "http://homeassistant:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN")
EVENT_TYPE = os.environ.get("HA_EVENT_TYPE", "pi_keypress")
INPUT_DEVICE = "/dev/input/event0"
SEND_HOLD = os.environ.get("SEND_HOLD") == "1"
GRAB_DEVICE = os.environ.get("GRAB_DEVICE") == "1"

ACTIONS = {0: "up", 1: "down", 2: "hold"}

events = queue.Queue(maxsize=1000)

def find_keyboard():
    """Return the first input device that looks like a keyboard/keypad."""
    for path in list_devices():
        dev = InputDevice(path)
        caps = dev.capabilities().get(ecodes.EV_KEY, [])
        # KEY_A (30) present => real keyboard; otherwise accept any key device
        if ecodes.KEY_A in caps or ecodes.KEY_1 in caps or ecodes.KEY_KP1 in caps:
            return dev
    return None


def sender():
    """Background thread: POST queued events to HA so a slow network never blocks input."""
    session = requests.Session()
    session.headers.update(
        {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    )
    url = f"{HA_URL}/api/events/{EVENT_TYPE}"
    while True:
        payload = events.get()
        for attempt in range(3):
            try:
                r = session.post(url, json=payload, timeout=5)
                if r.ok:
                    print(f"  -> HA accepted ({r.status_code}): {r.text.strip()}")
                    break
                print(f"HA returned {r.status_code}: {r.text}", file=sys.stderr)
                if r.status_code in (401, 403):
                    break  # bad token; retrying won't help
            except requests.RequestException as e:
                print(f"Send failed (attempt {attempt + 1}): {e}", file=sys.stderr)
            time.sleep(1 + attempt)


def main():
    if not HA_TOKEN:
        sys.exit("Set HA_TOKEN to a Home Assistant long-lived access token.")

    # Startup self-test: can we reach HA and is the token valid?
    try:
        r = requests.get(
            f"{HA_URL}/api/",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            timeout=5,
        )
        if r.status_code == 200:
            print(f"HA connection OK: {r.text.strip()}")
        elif r.status_code == 401:
            sys.exit("HA reachable but token rejected (401). Create a new long-lived token.")
        else:
            sys.exit(f"HA responded {r.status_code} at {HA_URL}/api/: {r.text[:200]}")
    except requests.RequestException as e:
        sys.exit(
            f"Cannot reach Home Assistant at {HA_URL}: {e}\n"
            "Try the IP address instead of homeassistant.local, e.g. http://192.168.1.50:8123"
        )

    dev = InputDevice(INPUT_DEVICE) if INPUT_DEVICE else find_keyboard()
    if dev is None:
        sys.exit("No keyboard found. Set INPUT_DEVICE=/dev/input/eventX (see `ls /dev/input`).")

    print(f"Listening on {dev.path} ({dev.name}); sending to {HA_URL} as '{EVENT_TYPE}'")

    if GRAB_DEVICE:
        dev.grab()

    threading.Thread(target=sender, daemon=True).start()

    try:
        for ev in dev.read_loop():
            if ev.type != ecodes.EV_KEY:
                continue
            action = ACTIONS.get(ev.value)
            if action is None or (action == "hold" and not SEND_HOLD):
                continue
            name = categorize(ev).keycode
            if isinstance(name, list):
                name = name[0]
            payload = {
                "key": name,
                "code": ev.code,
                "action": action,
                "device": dev.name,
            }
            print(payload)
            try:
                events.put_nowait(payload)
            except queue.Full:
                print("Queue full, dropping keypress", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        if GRAB_DEVICE:
            dev.ungrab()


if __name__ == "__main__":
    main()

