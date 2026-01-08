#!/usr/bin/env python3
# LED Matrix Launcher (128x64) - Carousel UI
# + USB automount (/mnt/usb) + Settings USB status + Safe remove
# + Settings USB LEFT/RIGHT: refresh/mount retry when selected
# + Kill other apps on launcher start (optional; see kill_other_led_apps)
#
# Run:
#   sudo python3 /home/pi/led/launcher.py

import os
import time
import json
import glob
import threading
import subprocess
import struct
from dataclasses import dataclass
from typing import List, Union, Tuple, Optional, Dict, Any

from PIL import Image, ImageDraw, ImageFont
from rgbmatrix import RGBMatrix, RGBMatrixOptions

# =========================
# MATRIX CONFIG
# =========================
PIXEL_MAPPER = "U-mapper;StackToRow:Z;Rotate:180"
W, H = 128, 64

CONFIG_PATH = "/home/pi/led/config.json"

DEFAULT_BRIGHTNESS = 60
BRIGHTNESS_MIN = 10
BRIGHTNESS_MAX = 100
BRIGHTNESS_STEP = 5

# USB
USB_MOUNT_DIR = "/mnt/usb"
USB_POLL_SEC = 0.7

# =========================
# MENUS
# =========================
Action = Union[str, List[str], dict]

APPS_MENU: List[Tuple[str, Action]] = [
    ("ANIMATION", ["python3", "/home/pi/led/apps/anim_player.py"]),
    ("SLIDESHOW", ["python3", "/home/pi/led/apps/slideshow.py"]),
    ("DASHBOARD", ["python3", "/home/pi/led/apps/dashboard.py"]),
    ("HOME ASSISTANT", ["python3", "/home/pi/led/apps/ha_matrix.py"]),
    ("PONG", ["python3", "/home/pi/led/apps/pong.py"]),
    ("SNAKE", ["python3", "/home/pi/led/apps/snake.py"]),
    ("PAC-MAN", ["python3", "/home/pi/led/apps/pacman.py"]),
    ("TETRIS", ["python3", "/home/pi/led/apps/tetris.py"]),
    ("SETTINGS", "submenu:settings"),
]

SETTINGS_MENU: List[Tuple[str, Action]] = [
    ("BRIGHTNESS", {"setting": "brightness"}),
    ("USB", "usb_menu"),  # shows status; OK = safe remove (or mount if not mounted)
    ("REBOOT", "reboot"),
    ("SHUTDOWN", "shutdown"),
    ("EXIT", "exit_app"),
    ("BACK", "back"),
]

MENUS = {
    "apps": APPS_MENU,
    "settings": SETTINGS_MENU,
}

# =========================
# INPUT EVENT
# =========================
@dataclass
class NavEvent:
    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False
    ok: bool = False
    back: bool = False
    any_input: bool = False
    source: str = "unknown"


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def load_font(size: int):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        try:
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def make_matrix(brightness: int) -> RGBMatrix:
    opts = RGBMatrixOptions()
    opts.rows = 32
    opts.cols = 64
    opts.chain_length = 4
    opts.parallel = 1
    opts.hardware_mapping = "adafruit-hat"
    opts.panel_type = "FM6126A"
    opts.row_address_type = 0
    opts.multiplexing = 0
    opts.pixel_mapper_config = PIXEL_MAPPER

    # stability knobs
    opts.disable_hardware_pulsing = True
    opts.gpio_slowdown = 2
    opts.pwm_bits = 8
    opts.pwm_lsb_nanoseconds = 300
    opts.pwm_dither_bits = 0

    opts.brightness = int(brightness)
    opts.drop_privileges = False
    return RGBMatrix(options=opts)


def get_clock_text() -> str:
    return time.strftime("%H:%M")


def text_width(d: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        return int(d.textlength(text, font=font))
    except Exception:
        bbox = d.textbbox((0, 0), text, font=font)
        return int(bbox[2] - bbox[0])


def truncate_text(d: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if text_width(d, text, font) <= max_w:
        return text
    ell = "…"
    if text_width(d, ell, font) > max_w:
        return ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        s = text[:mid].rstrip() + ell
        if text_width(d, s, font) <= max_w:
            lo = mid + 1
        else:
            hi = mid
    return text[:max(0, lo - 1)].rstrip() + ell


def draw_text_crisp(
    img_rgb: Image.Image,
    pos,
    text: str,
    font,
    fill=(255, 255, 255),
    threshold: int = 60,
):
    if not text:
        return
    x, y = pos
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    md.text((x, y), text, font=font, fill=255)
    mask = mask.point(lambda p: 255 if p >= threshold else 0)

    layer = Image.new("RGB", (W, H), (0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rectangle((0, 0, W, H), fill=fill)

    img_rgb.paste(layer, (0, 0), mask)


def draw_centered_crisp(img_rgb: Image.Image, y: int, text: str, font, fill=(255, 255, 255)):
    d = ImageDraw.Draw(img_rgb)
    tw = text_width(d, text, font)
    x = (W - tw) // 2
    draw_text_crisp(img_rgb, (x, y), text, font, fill=fill)


def toast(offscreen, img, text: str, font, sec: float = 0.5):
    t_end = time.time() + sec
    while time.time() < t_end:
        img.paste((0, 0, 0), (0, 0, W, H))
        draw_centered_crisp(img, (H // 2) - 6, text, font, fill=(240, 240, 240))
        offscreen.SetImage(img, 0, 0)
        time.sleep(0.02)


def draw_brightness_bar(img: Image.Image, x: int, y: int, w: int, h: int, brightness: int):
    d = ImageDraw.Draw(img)
    d.rectangle((x, y, x + w, y + h), outline=(90, 90, 90), fill=(0, 0, 0))
    span = max(1, w - 2)
    frac = (brightness - BRIGHTNESS_MIN) / max(1, (BRIGHTNESS_MAX - BRIGHTNESS_MIN))
    fill_w = int(span * frac)
    if fill_w > 0:
        d.rectangle((x + 1, y + 1, x + 1 + fill_w, y + h - 1), fill=(180, 180, 180))


# =========================
# OPTIONAL: KILL OTHER APPS ON START
# =========================
def kill_other_led_apps():
    """
    Kill any running LED apps in /home/pi/led/apps, and any OTHER launcher.py instances.
    Does NOT kill the current process.
    """
    # kill apps
    subprocess.run(
        ["pkill", "-f", "/home/pi/led/apps/"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # kill other launcher.py instances (exclude self PID)
    try:
        mypid = os.getpid()
        out = subprocess.check_output(["pgrep", "-f", "/home/pi/led/launcher.py"], text=True)
        for line in out.splitlines():
            try:
                pid = int(line.strip())
                if pid != mypid:
                    os.kill(pid, 15)  # SIGTERM
            except Exception:
                pass
    except Exception:
        pass


# =========================
# USB HELPERS (lsblk + mount/umount)
# =========================
def ensure_mount_dir(mount_dir: str = USB_MOUNT_DIR):
    try:
        os.makedirs(mount_dir, exist_ok=True)
    except Exception:
        pass


def _lsblk_json() -> Optional[Dict[str, Any]]:
    try:
        out = subprocess.check_output(
            ["lsblk", "-J", "-o", "NAME,PATH,PKNAME,TRAN,RM,TYPE,FSTYPE,MOUNTPOINT,LABEL"],
            text=True,
        )
        return json.loads(out)
    except Exception:
        return None


def _walk_blockdevices(tree: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not tree or "blockdevices" not in tree:
        return out

    def rec(node: Dict[str, Any]):
        out.append(node)
        for ch in (node.get("children") or []):
            rec(ch)

    for dev in tree["blockdevices"]:
        rec(dev)
    return out


def find_usb_partition() -> Optional[Dict[str, Any]]:
    tree = _lsblk_json()
    nodes = _walk_blockdevices(tree)

    parts = []
    disks = []
    for n in nodes:
        n_type = (n.get("type") or "").lower()
        tran = (n.get("tran") or "").lower()
        rm = int(n.get("rm") or 0)
        is_usbish = (tran == "usb") or (rm == 1)
        if not is_usbish:
            continue
        if n_type == "part":
            parts.append(n)
        elif n_type == "disk":
            disks.append(n)

    for p in parts:
        if p.get("path") and p.get("fstype"):
            return p
    if parts:
        return parts[0]
    for d in disks:
        if d.get("path"):
            return d
    return None


def usb_status() -> Dict[str, Any]:
    part = find_usb_partition()
    if not part:
        return {
            "present": False,
            "mounted": False,
            "mountpoint": None,
            "path": None,
            "pkname": None,
            "label": None,
            "fstype": None,
        }

    mp = part.get("mountpoint") or None
    mounted = bool(mp)
    return {
        "present": True,
        "mounted": mounted,
        "mountpoint": mp,
        "path": part.get("path"),
        "pkname": part.get("pkname"),
        "label": part.get("label") or None,
        "fstype": part.get("fstype") or None,
    }


def mount_usb_if_needed(mount_dir: str = USB_MOUNT_DIR) -> bool:
    ensure_mount_dir(mount_dir)
    st = usb_status()
    if not st["present"] or not st["path"]:
        return False

    if st["mounted"]:
        return st["mountpoint"] == mount_dir

    try:
        subprocess.run(
            ["mount", st["path"], mount_dir],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False

    st2 = usb_status()
    return st2["mounted"] and st2["mountpoint"] == mount_dir


def _which(cmd: str) -> Optional[str]:
    try:
        from shutil import which
        return which(cmd)
    except Exception:
        return None


def safe_remove_usb(mount_dir: str = USB_MOUNT_DIR) -> bool:
    st = usb_status()
    if not st["present"]:
        return True

    try:
        subprocess.run(["sync"], check=False)
    except Exception:
        pass

    if st["mounted"] and st["mountpoint"] == mount_dir:
        try:
            subprocess.run(
                ["umount", mount_dir],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    st2 = usb_status()
    if st2["present"] and st2["mounted"] and st2["mountpoint"] == mount_dir:
        return False

    pk = st.get("pkname")
    if pk:
        disk_path = f"/dev/{pk}"
        if _which("udisksctl"):
            try:
                subprocess.run(
                    ["udisksctl", "power-off", "-b", disk_path],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

    return True


class USBMonitor(threading.Thread):
    def __init__(self, mount_dir: str = USB_MOUNT_DIR, poll_sec: float = USB_POLL_SEC):
        super().__init__(daemon=True)
        self.mount_dir = mount_dir
        self.poll_sec = poll_sec
        self._stop = False
        self._last_seen_path: Optional[str] = None

    def stop(self):
        self._stop = True

    def run(self):
        ensure_mount_dir(self.mount_dir)
        while not self._stop:
            try:
                st = usb_status()
                path = st.get("path")
                if st["present"] and path and path != self._last_seen_path:
                    self._last_seen_path = path
                if st["present"]:
                    if not st["mounted"]:
                        mount_usb_if_needed(self.mount_dir)
                else:
                    self._last_seen_path = None
            except Exception:
                pass
            time.sleep(self.poll_sec)


# =========================
# GAMEPAD via /dev/input/js0
# =========================
class JoystickReader(threading.Thread):
    """
    OK   = A or X  (common: 0 or 1 depending)
    BACK = B or Y  (common: 2 or 3 depending)
    LEFT/RIGHT from axis 0
    UP/DOWN from axis 1
    """
    def __init__(self, js_path="/dev/input/js0"):
        super().__init__(daemon=True)
        self.js_path = js_path
        self._lock = threading.Lock()
        self._event = NavEvent()
        self._stop = False
        self.DEADZONE = 12000
        self._x_state = 0
        self._y_state = 0

    def stop(self):
        self._stop = True

    def pop(self) -> NavEvent:
        with self._lock:
            ev = self._event
            self._event = NavEvent()
            return ev

    def _push(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self._event, k, getattr(self._event, k) or v)
            self._event.any_input = True
            self._event.source = "gamepad"

    def run(self):
        if not os.path.exists(self.js_path):
            print("No joystick device:", self.js_path)
            return

        fmt = "IhBB"
        sz = struct.calcsize(fmt)

        try:
            with open(self.js_path, "rb", buffering=0) as f:
                while not self._stop:
                    data = f.read(sz)
                    if not data or len(data) != sz:
                        time.sleep(0.01)
                        continue

                    _t, value, etype, num = struct.unpack(fmt, data)
                    if (etype & 0x80) != 0:  # init
                        continue
                    et = etype & 0x7F

                    # Axis
                    if et == 0x02:
                        v = int(value)

                        if num == 0:  # X axis
                            new_state = 0
                            if v < -self.DEADZONE:
                                new_state = -1
                            elif v > self.DEADZONE:
                                new_state = +1
                            if new_state != self._x_state:
                                self._x_state = new_state
                                if new_state == -1:
                                    self._push(left=True)
                                elif new_state == +1:
                                    self._push(right=True)

                        elif num == 1:  # Y axis
                            new_state = 0
                            if v < -self.DEADZONE:
                                new_state = -1
                            elif v > self.DEADZONE:
                                new_state = +1
                            if new_state != self._y_state:
                                self._y_state = new_state
                                if new_state == -1:
                                    self._push(up=True)
                                elif new_state == +1:
                                    self._push(down=True)

                    # Buttons
                    elif et == 0x01 and value == 1:
                        # NOTE: your mapping may differ; adjust if needed
                        if num in (0, 1):      # A or X
                            self._push(ok=True)
                        elif num in (2, 3):    # B or Y
                            self._push(back=True)

        except Exception as e:
            print("JoystickReader error:", e)


# =========================
# OPTIONAL KEYBOARD via evdev
# =========================
class KeyboardReader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._event = NavEvent()
        self._stop = False

    def stop(self):
        self._stop = True

    def pop(self) -> NavEvent:
        with self._lock:
            ev = self._event
            self._event = NavEvent()
            return ev

    def _push(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self._event, k, getattr(self._event, k) or v)
            self._event.any_input = True
            self._event.source = "keyboard"

    def _find_keyboard_event(self):
        hits = sorted(glob.glob("/dev/input/by-path/*kbd*event*"))
        if hits:
            return hits[0]
        events = sorted(glob.glob("/dev/input/event*"))
        return events[0] if events else None

    def run(self):
        try:
            from evdev import InputDevice, ecodes
        except Exception:
            return

        path = self._find_keyboard_event()
        if not path:
            return

        try:
            dev = InputDevice(path)
        except Exception:
            return

        KEY_UP = getattr(ecodes, "KEY_UP", 103)
        KEY_DOWN = getattr(ecodes, "KEY_DOWN", 108)
        KEY_LEFT = getattr(ecodes, "KEY_LEFT", 105)
        KEY_RIGHT = getattr(ecodes, "KEY_RIGHT", 106)
        KEY_ENTER = getattr(ecodes, "KEY_ENTER", 28)
        KEY_KPENTER = getattr(ecodes, "KEY_KPENTER", 96)
        KEY_ESC = getattr(ecodes, "KEY_ESC", 1)
        KEY_BACKSPACE = getattr(ecodes, "KEY_BACKSPACE", 14)

        while not self._stop:
            try:
                for ev in dev.read():
                    if ev.type == ecodes.EV_KEY and ev.value == 1:
                        code = ev.code
                        if code == KEY_UP:
                            self._push(up=True)
                        elif code == KEY_DOWN:
                            self._push(down=True)
                        elif code == KEY_LEFT:
                            self._push(left=True)
                        elif code == KEY_RIGHT:
                            self._push(right=True)
                        elif code in (KEY_ENTER, KEY_KPENTER):
                            self._push(ok=True)
                        elif code in (KEY_ESC, KEY_BACKSPACE):
                            self._push(back=True)
            except (BlockingIOError, OSError):
                pass
            time.sleep(0.01)


def merge_nav(a: NavEvent, b: NavEvent) -> NavEvent:
    source = "unknown"
    if a.any_input:
        source = a.source
    elif b.any_input:
        source = b.source

    return NavEvent(
        up=a.up or b.up,
        down=a.down or b.down,
        left=a.left or b.left,
        right=a.right or b.right,
        ok=a.ok or b.ok,
        back=a.back or b.back,
        any_input=a.any_input or b.any_input,
        source=source,
    )


def main():
    # Start clean (optional but requested)
    try:
        kill_other_led_apps()
        time.sleep(0.2)
    except Exception:
        pass

    cfg = load_config()
    brightness = int(cfg.get("brightness", DEFAULT_BRIGHTNESS))
    brightness = int(clamp(brightness, BRIGHTNESS_MIN, BRIGHTNESS_MAX))

    matrix = make_matrix(brightness)

    # Fonts tuned for 128x64
    font_big = load_font(12)
    font_mid = load_font(9)
    font_small = load_font(8)

    img = Image.new("RGB", (W, H), (0, 0, 0))
    offscreen = matrix.CreateFrameCanvas()

    pad = JoystickReader("/dev/input/js0")
    pad.start()
    kbd = KeyboardReader()
    kbd.start()

    # USB automount thread
    usbmon = USBMonitor(USB_MOUNT_DIR, USB_POLL_SEC)
    usbmon.start()

    menu_stack = ["apps"]
    sel = 0
    last_nav = 0.0
    NAV_REPEAT = 0.14

    def cur_items():
        return MENUS[menu_stack[-1]]

    def set_brightness(val: int):
        nonlocal brightness
        brightness = int(clamp(val, BRIGHTNESS_MIN, BRIGHTNESS_MAX))
        try:
            matrix.brightness = brightness
        except Exception:
            pass
        cfg["brightness"] = brightness
        save_config(cfg)

    def adjust_brightness(delta: int):
        set_brightness(brightness + delta)

    def launch(cmd: List[str]) -> bool:
        nonlocal offscreen
        img.paste((0, 0, 0), (0, 0, W, H))
        draw_centered_crisp(img, (H // 2) - 6, "STARTING...", font_mid, fill=(240, 240, 240))
        offscreen.SetImage(img, 0, 0)
        offscreen = matrix.SwapOnVSync(offscreen)
        time.sleep(0.25)

        try:
            matrix.Clear()
            time.sleep(0.05)
            env = os.environ.copy()
            env["MATRIX_BRIGHTNESS"] = str(brightness)

            # Replace launcher with the app (your original behavior)
            os.execvpe(cmd[0], cmd, env)

            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            return True
        except Exception as e:
            print("Launch failed:", cmd, e)
            img.paste((0, 0, 0), (0, 0, W, H))
            draw_centered_crisp(img, (H // 2) - 6, "LAUNCH FAIL", font_mid, fill=(240, 240, 240))
            offscreen.SetImage(img, 0, 0)
            offscreen = matrix.SwapOnVSync(offscreen)
            time.sleep(0.7)
            return False

    def do_action(action: Action):
        nonlocal sel, offscreen

        if isinstance(action, str):
            if action.startswith("submenu:"):
                name = action.split(":", 1)[1]
                if name in MENUS:
                    menu_stack.append(name)
                    sel = 0
                return "stay"

            if action == "back":
                return "back"

            if action == "usb_menu":
                st = usb_status()
                if not st["present"]:
                    toast(offscreen, img, "NO USB", font_mid, sec=0.6)
                    return "stay"

                if not st["mounted"]:
                    toast(offscreen, img, "MOUNT...", font_mid, sec=0.35)
                    ok = mount_usb_if_needed(USB_MOUNT_DIR)
                    toast(offscreen, img, "MOUNT OK" if ok else "MOUNT FAIL", font_mid, sec=0.6)
                    return "stay"

                toast(offscreen, img, "SAFE REMOVE", font_mid, sec=0.35)
                ok = safe_remove_usb(USB_MOUNT_DIR)
                toast(offscreen, img, "REMOVED" if ok else "BUSY", font_mid, sec=0.7)
                return "stay"

            if action == "reboot":
                img.paste((0, 0, 0), (0, 0, W, H))
                draw_centered_crisp(img, (H // 2) - 6, "REBOOT...", font_mid, fill=(240, 240, 240))
                offscreen.SetImage(img, 0, 0)
                offscreen = matrix.SwapOnVSync(offscreen)
                time.sleep(0.35)
                subprocess.Popen(["sudo", "reboot"])
                return "exit"

            if action == "shutdown":
                img.paste((0, 0, 0), (0, 0, W, H))
                draw_centered_crisp(img, (H // 2) - 6, "SHUTDOWN...", font_mid, fill=(240, 240, 240))
                offscreen.SetImage(img, 0, 0)
                offscreen = matrix.SwapOnVSync(offscreen)
                time.sleep(0.35)
                subprocess.Popen(["sudo", "shutdown", "-h", "now"])
                return "exit"

            if action == "exit_app":
                matrix.Clear()
                return "exit"

        if isinstance(action, list):
            launch(action)
            return "exit"

        return "stay"

    def render_carousel(items: List[Tuple[str, Action]], sel_idx: int):
        """
        Carousel style:
          - Selected item big in middle
          - Prev/next smaller above/below
          - Clock top-right
          - No footer text
          - If in settings and selected BRIGHTNESS: show value + mini bar
          - If in settings and selected USB: show status line
        """
        img.paste((0, 0, 0), (0, 0, W, H))
        d = ImageDraw.Draw(img)

        cur_menu = menu_stack[-1]
        title = "APPS" if cur_menu == "apps" else "SETTINGS"
        draw_text_crisp(img, (4, 2), title, font_small, fill=(110, 110, 110))

        clk = get_clock_text()
        draw_text_crisp(img, (W - 4 - text_width(d, clk, font_small), 2), clk, font_small, fill=(130, 130, 130))

        n = len(items)
        if n == 0:
            draw_centered_crisp(img, (H // 2) - 6, "(EMPTY)", font_mid, fill=(200, 200, 200))
            return

        def label_at(i: int) -> str:
            return items[i][0]

        prev_label = label_at(sel_idx - 1) if sel_idx - 1 >= 0 else ""
        cur_label = label_at(sel_idx)
        next_label = label_at(sel_idx + 1) if sel_idx + 1 < n else ""

        prev_label = truncate_text(d, prev_label, font_mid, W - 16)

        cur_label_t = cur_label
        if cur_menu == "settings":
            _lab, act = items[sel_idx]
            if isinstance(act, dict) and act.get("setting") == "brightness":
                cur_label_t = "BRIGHTNESS"

        cur_label_t = truncate_text(d, cur_label_t, font_big, W - 16)
        next_label = truncate_text(d, next_label, font_mid, W - 16)

        if prev_label:
            draw_centered_crisp(img, 14, prev_label, font_mid, fill=(140, 140, 140))
        if next_label:
            draw_centered_crisp(img, 44, next_label, font_mid, fill=(140, 140, 140))

        draw_centered_crisp(img, 27, cur_label_t, font_big, fill=(245, 245, 245))

        left_col = (90, 90, 90) if sel_idx > 0 else (40, 40, 40)
        right_col = (90, 90, 90) if sel_idx < n - 1 else (40, 40, 40)
        draw_text_crisp(img, (6, 27), "<", font_big, fill=left_col)
        draw_text_crisp(img, (W - 12, 27), ">", font_big, fill=right_col)

        if cur_menu == "settings":
            _lab, act = items[sel_idx]

            if isinstance(act, dict) and act.get("setting") == "brightness":
                bar_w = 80
                bar_h = 6
                bar_x = (W - bar_w) // 2
                bar_y = 56
                draw_brightness_bar(img, bar_x, bar_y, bar_w, bar_h, brightness)
                val = f"{brightness:3d}"
                draw_text_crisp(img, (bar_x + bar_w + 2, bar_y - 1), val, font_small, fill=(160, 160, 160))

            if isinstance(act, str) and act == "usb_menu":
                st = usb_status()
                if not st["present"]:
                    line = "USB: NONE"
                else:
                    lab = st.get("label") or ""
                    if st["mounted"]:
                        mp = st.get("mountpoint") or ""
                        line = "USB: MOUNTED" if mp == USB_MOUNT_DIR else "USB: MOUNTED*"
                    else:
                        line = "USB: DETECTED"
                    if lab:
                        lab_t = truncate_text(d, lab, font_small, 40)
                        line = f"{line} {lab_t}"

                draw_centered_crisp(img, 56, line, font_small, fill=(160, 160, 160))

    # Splash
    img.paste((0, 0, 0), (0, 0, W, H))
    draw_centered_crisp(img, (H // 2) - 6, "LED LAUNCHER", font_mid, fill=(240, 240, 240))
    offscreen.SetImage(img, 0, 0)
    offscreen = matrix.SwapOnVSync(offscreen)
    time.sleep(0.6)

    try:
        while True:
            nav = merge_nav(pad.pop(), kbd.pop())
            items = cur_items()
            n = len(items)

            if n == 0:
                sel = 0
            else:
                sel = max(0, min(sel, n - 1))

            t = time.time()
            can_nav = (t - last_nav) >= NAV_REPEAT

            if can_nav and nav.up and n > 0:
                sel = max(0, sel - 1)
                last_nav = t
            elif can_nav and nav.down and n > 0:
                sel = min(n - 1, sel + 1)
                last_nav = t

            # LEFT/RIGHT in settings:
            # - BRIGHTNESS: adjust
            # - USB: refresh/mount retry if NONE/DETECTED
            if menu_stack[-1] == "settings" and n > 0 and can_nav and (nav.left or nav.right):
                _label, action = items[sel]

                if isinstance(action, dict) and action.get("setting") == "brightness":
                    if nav.left:
                        adjust_brightness(-BRIGHTNESS_STEP)
                    elif nav.right:
                        adjust_brightness(+BRIGHTNESS_STEP)
                    last_nav = t

                elif isinstance(action, str) and action == "usb_menu":
                    st = usb_status()
                    if not st["present"]:
                        toast(offscreen, img, "NO USB", font_mid, sec=0.6)
                    else:
                        if not st["mounted"]:
                            toast(offscreen, img, "MOUNT...", font_mid, sec=0.35)
                            ok = mount_usb_if_needed(USB_MOUNT_DIR)
                            toast(offscreen, img, "MOUNT OK" if ok else "MOUNT FAIL", font_mid, sec=0.6)
                        else:
                            toast(offscreen, img, "MOUNTED", font_mid, sec=0.4)
                    last_nav = t

            if nav.ok and n > 0:
                _label, action = items[sel]
                if isinstance(action, dict) and action.get("setting") == "brightness":
                    pass
                else:
                    res = do_action(action)
                    if res == "exit":
                        break
                    if res == "back":
                        if len(menu_stack) > 1:
                            menu_stack.pop()
                            sel = 0

            if nav.back:
                if len(menu_stack) > 1:
                    menu_stack.pop()
                    sel = 0

            render_carousel(items, sel)
            offscreen.SetImage(img, 0, 0)
            offscreen = matrix.SwapOnVSync(offscreen)
            time.sleep(0.016)

    finally:
        pad.stop()
        kbd.stop()
        usbmon.stop()
        matrix.Clear()


if __name__ == "__main__":
    main()
