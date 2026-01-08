#!/usr/bin/env python3
import os
import time
import struct
import subprocess
import errno
import fcntl

JS_DEV = "/dev/input/js0"
START_BTN = 9
SELECT_BTN = 8

HOLD_TIME = 5.0
COOLDOWN = 3.0
DROP_TOLERANCE = 0.35  # tolerate brief glitches

FMT = "IhBB"
SZ = struct.calcsize(FMT)

def log(msg: str):
    print(f"[launcher-rescue] {msg}", flush=True)

def kill_apps():
    subprocess.run(
        ["pkill", "-f", "/home/pi/led/apps/"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def start_launcher():
    subprocess.run(
        ["systemctl", "reset-failed", "led-launcher"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    r = subprocess.run(
        ["systemctl", "start", "led-launcher"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log(f"systemctl start led-launcher -> rc={r.returncode}")

def open_js_nonblocking():
    if not os.path.exists(JS_DEV):
        return None
    try:
        f = open(JS_DEV, "rb", buffering=0)
        fd = f.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return f
    except Exception as e:
        log(f"Cannot open {JS_DEV}: {e}")
        return None

def main():
    log("started")
    btn = {}  # num -> pressed bool

    hold_started_at = None
    last_seen_both = 0.0
    last_trigger = 0.0

    while True:
        f = open_js_nonblocking()
        if not f:
            time.sleep(0.5)
            continue

        log(f"opened {JS_DEV} (non-blocking)")
        try:
            while True:
                # Read all available events (non-blocking)
                while True:
                    try:
                        data = f.read(SZ)
                        if not data:
                            break
                        if len(data) != SZ:
                            break

                        _t, value, etype, num = struct.unpack(FMT, data)

                        # ignore init events
                        if (etype & 0x80) != 0:
                            continue

                        et = etype & 0x7F
                        if et == 0x01:  # button
                            if value == 1:
                                btn[num] = True
                            elif value == 0:
                                btn[num] = False

                    except BlockingIOError:
                        break
                    except OSError as e:
                        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                            break
                        # device disappeared -> reopen
                        log(f"read error -> reopen: {e}")
                        raise

                # Timer logic runs even if NO new events arrived
                now = time.time()
                start_pressed = btn.get(START_BTN, False)
                select_pressed = btn.get(SELECT_BTN, False)
                both = start_pressed and select_pressed

                if both:
                    last_seen_both = now
                    if hold_started_at is None:
                        hold_started_at = now
                        log(f"START+SELECT hold begin (start={START_BTN}, select={SELECT_BTN})")
                else:
                    if hold_started_at is not None and (now - last_seen_both) > DROP_TOLERANCE:
                        hold_started_at = None

                if hold_started_at is not None:
                    held = now - hold_started_at
                    if held >= HOLD_TIME:
                        if (now - last_trigger) >= COOLDOWN:
                            last_trigger = now
                            hold_started_at = None
                            log("TRIGGER -> kill apps + start launcher")
                            kill_apps()
                            time.sleep(0.2)
                            start_launcher()

                time.sleep(0.01)

        except Exception:
            # reopen device
            pass
        finally:
            try:
                f.close()
            except Exception:
                pass
            time.sleep(0.2)

if __name__ == "__main__":
    main()
