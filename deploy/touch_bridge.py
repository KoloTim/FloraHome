#!/usr/bin/env python3
"""FloraHome touch bridge.

The 5" panel's controller (USB 8888:6666) is an **absolute mouse**: it reports
ABS_X/ABS_Y + BTN_LEFT but no BTN_TOUCH, so the kernel/libinput (and every
browser) treats it as a mouse. Taps click, but drags select text and never
scroll — there is no touch gesture at all.

This daemon reads that device straight from /dev/input/eventX and re-emits the
events through /dev/uinput as a **real touchscreen** (INPUT_PROP_DIRECT +
BTN_TOUCH + ABS_MT_*). libinput then hands Chromium proper touch events, giving
native kinetic scrolling and pinch.

It does *not* consume the original device, so the old mouse behaviour remains as
a fallback. Only touches on the panel are converted; the original device keeps
working.

Run as root (needs /dev/uinput). Installed by deploy/install-touch-bridge.sh.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import struct
import sys
import time

# ---- input event ------------------------------------------------------------------
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0x00
BTN_LEFT, BTN_TOUCH = 0x110, 0x14A
ABS_X, ABS_Y, ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x00, 0x01, 0x2F, 0x35, 0x36
ABS_MT_TRACKING_ID = 0x39
INPUT_PROP_DIRECT = 0x01
BUS_USB = 0x03
# struct input_event: timeval (long seconds, long usec) + u16 type + u16 code + s32 value
EVENT_FMT = "llHHi"          # 64-bit: 8 + 8 + 2 + 2 + 4 = 24 bytes
DEVICE = "FloraHome touchscreen"
VENDOR, PRODUCT = 0x1D6B, 0x0F10

# ---- uinput ioctls ----------------------------------------------------------------
UINPUT_MAX_NAME_SIZE = 80
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E


class InputId(ctypes.Structure):
    _fields_ = [("bustype", ctypes.c_uint16), ("vendor", ctypes.c_uint16),
                ("product", ctypes.c_uint16), ("version", ctypes.c_uint16)]


class InputAbsInfo(ctypes.Structure):
    _fields_ = [("value", ctypes.c_int32), ("minimum", ctypes.c_int32),
                ("maximum", ctypes.c_int32), ("fuzz", ctypes.c_int32),
                ("flat", ctypes.c_int32), ("resolution", ctypes.c_int32)]


class UinputAbsSetup(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("absinfo", InputAbsInfo)]


class UinputSetup(ctypes.Structure):
    """struct uinput_setup: input_id + name[80] (+ 4 bytes padding) + u32."""
    _fields_ = [("id", InputId),
                ("name", ctypes.c_char * UINPUT_MAX_NAME_SIZE),
                ("ff_effects_max", ctypes.c_uint32)]


def ioctl(fd: int, req: int, arg: int) -> None:
    fcntl.ioctl(fd, req, arg)


def make_uinput(max_x: int, max_y: int) -> int:
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    ioctl(fd, UI_SET_EVBIT, EV_KEY)
    ioctl(fd, UI_SET_EVBIT, EV_ABS)
    ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH)
    for ab in (ABS_X, ABS_Y, ABS_MT_SLOT, ABS_MT_TRACKING_ID,
               ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
        ioctl(fd, UI_SET_ABSBIT, ab)
    ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)

    dev = UinputSetup()
    dev.name = DEVICE.encode()
    dev.id = InputId(BUS_USB, VENDOR, PRODUCT, 1)
    # UI_DEV_SETUP = _IOW('U', 3, struct uinput_setup)
    UI_DEV_SETUP = 0x40000000 | (ctypes.sizeof(UinputSetup) << 16) | (ord("U") << 8) | 3
    fcntl.ioctl(fd, UI_DEV_SETUP, bytes(dev))
    for args in ((ABS_X, max_x), (ABS_Y, max_y), (ABS_MT_SLOT, 9),
                 (ABS_MT_TRACKING_ID, 65535),
                 (ABS_MT_POSITION_X, max_x), (ABS_MT_POSITION_Y, max_y)):
        code, mx = args
        s = UinputAbsSetup(code, InputAbsInfo(0, 0, mx, 0, 0, 0))
        # UI_ABS_SETUP = _IOW('U', 4, struct uinput_abs_setup)
        UI_ABS_SETUP = 0x40000000 | (ctypes.sizeof(UinputAbsSetup) << 16) | (ord("U") << 8) | 4
        fcntl.ioctl(fd, UI_ABS_SETUP, bytes(s))
    UI_DEV_CREATE = 0x5501
    fcntl.ioctl(fd, UI_DEV_CREATE)
    time.sleep(0.3)
    return fd


def emit(fd: int, etype: int, code: int, value: int) -> None:
    os.write(fd, struct.pack(EVENT_FMT, 0, 0, etype, code, value))


def syn(fd: int) -> None:
    emit(fd, EV_SYN, SYN_REPORT, 0)


def find_event_nodes(vendor: int, product: int) -> list[str]:
    """Return /dev/input/eventN for the device with the given USB id."""
    nodes = []
    base = "/sys/class/input"
    for name in os.listdir(base):
        if not name.startswith("event"):
            continue
        dev = os.path.realpath(os.path.join(base, name, "device"))
        try:
            with open(os.path.join(dev, "id", "vendor")) as fh:
                v = int(fh.read().strip(), 16)
            with open(os.path.join(dev, "id", "product")) as fh:
                p = int(fh.read().strip(), 16)
        except OSError:
            continue
        if v == vendor and p == product:
            nodes.append("/dev/input/" + name)
    return nodes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-x", type=int, default=800)
    ap.add_argument("--max-y", type=int, default=480)
    ap.add_argument("--vendor", type=lambda s: int(s, 0), default=0x8888)
    ap.add_argument("--product", type=lambda s: int(s, 0), default=0x6666)
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("must run as root (needs /dev/uinput)", file=sys.stderr)
        return 1

    out = make_uinput(args.max_x, args.max_y)
    print(f"[touch-bridge] virtual '{DEVICE}' created ({args.max_x}x{args.max_y})", flush=True)

    # read one device at a time; re-open if it disappears
    while True:
        nodes = find_event_nodes(args.vendor, args.product)
        if not nodes:
            time.sleep(2)
            continue
        path = nodes[0]
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            time.sleep(2)
            continue
        print(f"[touch-bridge] bridging {path}", flush=True)
        x = y = 0
        touching = False
        try:
            while True:
                data = os.read(fd, 24 * 64)
                for off in range(0, len(data), 24):
                    _, _, etype, code, value = struct.unpack(EVENT_FMT, data[off:off + 24])
                    if etype == EV_ABS and code == ABS_X:
                        x = value
                    elif etype == EV_ABS and code == ABS_Y:
                        y = value
                    elif etype == EV_KEY and code == BTN_LEFT:
                        if value == 1 and not touching:
                            touching = True
                            emit(out, EV_KEY, BTN_TOUCH, 1)
                            emit(out, EV_ABS, ABS_MT_SLOT, 0)
                            emit(out, EV_ABS, ABS_MT_TRACKING_ID, 1)
                            emit(out, EV_ABS, ABS_MT_POSITION_X, x)
                            emit(out, EV_ABS, ABS_MT_POSITION_Y, y)
                            emit(out, EV_ABS, ABS_X, x)
                            emit(out, EV_ABS, ABS_Y, y)
                            syn(out)
                        elif value == 0 and touching:
                            touching = False
                            emit(out, EV_ABS, ABS_MT_TRACKING_ID, -1)
                            emit(out, EV_KEY, BTN_TOUCH, 0)
                            syn(out)
                    elif etype == EV_SYN and code == SYN_REPORT and touching:
                        emit(out, EV_ABS, ABS_MT_POSITION_X, x)
                        emit(out, EV_ABS, ABS_MT_POSITION_Y, y)
                        emit(out, EV_ABS, ABS_X, x)
                        emit(out, EV_ABS, ABS_Y, y)
                        syn(out)
        except OSError:
            print(f"[touch-bridge] {path} went away, rescanning", flush=True)
            time.sleep(2)
        finally:
            os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
