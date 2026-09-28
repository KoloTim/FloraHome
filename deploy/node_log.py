#!/usr/bin/env python3
"""Stream an ESP32's ESPHome serial log from the Pi, optionally after a reset.

The board must be on the Pi's USB (CP2102/CH340). Reads at 115200 and toggles
DTR/RTS to reset the chip into run mode - so you get the boot banner plus the
live sensor/MQTT lines.

Usage:
  python3 node_log.py                 # reset, then stream until Ctrl-C
  python3 node_log.py 20              # reset, then stream for 20 s
  python3 node_log.py 20 --no-reset   # stream without resetting
  python3 node_log.py --port /dev/ttyUSB0 --baud 115200
"""
import argparse
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial missing: python3 -m pip install --user --break-system-packages pyserial")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("seconds", nargs="?", type=float, default=0, help="0 = until Ctrl-C")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--no-reset", action="store_true")
    a = p.parse_args()

    try:
        s = serial.Serial(a.port, a.baud, timeout=0.2)
    except Exception as exc:  # noqa: BLE001
        return f"cannot open {a.port}: {exc}"

    if not a.no_reset:
        # ESP32 auto-reset: IO0 high (DTR False), pulse EN low (RTS True->False).
        s.dtr = False
        s.rts = True
        time.sleep(0.15)
        s.rts = False

    end = time.time() + a.seconds if a.seconds else None
    try:
        while True:
            if end and time.time() > end:
                break
            data = s.read(4096)
            if data:
                sys.stdout.write(data.decode(errors="replace"))
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
