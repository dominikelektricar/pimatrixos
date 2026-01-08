#!/usr/bin/env python3
# image_player.py — player za SLIKE (bez teksta tijekom prikaza) za RGBMatrix 128x64
#
# USB / lokalno:
# - Ako postoji /mnt/usb/(photos|slike|pics) -> koristi USB
# - Inače koristi lokalni folder: ./photos
#
# Kontrole:
# - START ili SELECT: otvori meni
# - U meniju: START/SELECT = RESUME
# - A / X = OK
# - B / Y = BACK
#
# Meni:
# - RESUME
# - CHOOSE FILE
# - MODE (LEFT/RIGHT)
# - TIME (LEFT/RIGHT, OFF..999s, držanje >2s brzo mijenja)
# - EXIT
#
# Tijekom prikaza slika nema teksta.

import os
import sys
import time
import glob
import struct
import threading
from dataclasses import dataclass
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont
from rgbmatrix import RGBMatrix, RGBMatrixOptions

# ===== DISPLAY CONFIG =====
PIXEL_MAPPER = "U-mapper;StackToRow:Z;Rotate:180"
W, H = 128, 64

# ===== INPUT =====
JS_PATH = "/dev/input/js0"
DEADZONE = 12000
AXIS_X = 0
AXIS_Y = 1

# Button mapping (match your games)
BTN_X = 0
BTN_A = 1
BTN_B = 2
BTN_Y = 3
BTN_SELECT = 8
BTN_START  = 9

# ===== FOLDERS =====
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_PHOTO_DIR = os.path.join(SCRIPT_DIR, "photos")
USB_PHOTO_FOLDER_NAMES = ("photos", "slike", "pics")

# ===== UI COLORS =====
BG        = (0, 0, 0)
MENU_BG   = (0, 0, 0)
MENU_DIM  = (140, 140, 140)
MENU_FG   = (240, 240, 240)

# ===== MENU =====
MENU_ITEMS = ["RESUME", "CHOOSE FILE", "MODE", "TIME", "EXIT"]

# ===== IMAGE SETTINGS =====
IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp"}

# ===== MODES / TRANSITIONS =====
MODES = [
    "FADE",
    "FLASH",
    "CUT",
    "SLIDE_L",
    "SLIDE_R",
    "WIPE_L",
    "WIPE_R",
    "ROTATE",
]

# default time
DEFAULT_TIME_SEC = 5
MIN_TIME_SEC = 1
MAX_TIME_SEC = 999
OFF_TIME = 0  # OFF


# =========================
# MATRIX
# =========================
def make_matrix() -> RGBMatrix:
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

    # stability knobs (same as games)
    opts.disable_hardware_pulsing = True
    opts.gpio_slowdown = 2
    opts.pwm_bits = 8
    opts.pwm_lsb_nanoseconds = 300
    opts.pwm_dither_bits = 0

    # brightness from launcher via env
    try:
        opts.brightness = int(os.getenv("MATRIX_BRIGHTNESS", "60"))
    except Exception:
        opts.brightness = 60

    opts.drop_privileges = False
    return RGBMatrix(options=opts)


# =========================
# FONT (crisp) — samo za meni
# =========================
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

def text_width(d: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        return int(d.textlength(text, font=font))
    except Exception:
        bbox = d.textbbox((0, 0), text, font=font)
        return int(bbox[2] - bbox[0])

def draw_text_crisp(img_rgb: Image.Image, pos, text: str, font, fill=(255, 255, 255), threshold: int = 80):
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

def dim_overlay(img_rgb: Image.Image, alpha: int = 155):
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, alpha))
    base = img_rgb.convert("RGBA")
    base.alpha_composite(overlay)
    img_rgb.paste(base.convert("RGB"))


# =========================
# LAUNCHER EXIT
# =========================
def exec_launcher_or_exit(matrix: RGBMatrix):
    try:
        matrix.Clear()
    except Exception:
        pass

    parent = os.path.dirname(SCRIPT_DIR)
    candidates = [
        os.path.join(parent, "launcher.py"),
        os.path.join(SCRIPT_DIR, "launcher.py"),
    ]
    for launcher in candidates:
        if os.path.exists(launcher):
            os.execv(sys.executable, [sys.executable, launcher])

    raise SystemExit("launcher.py not found in parent or current folder")


# =========================
# INPUT
# =========================
@dataclass
class Controls:
    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False
    start: bool = False
    select: bool = False
    a: bool = False
    b: bool = False
    x: bool = False
    y: bool = False
    any: bool = False

class Js0Reader(threading.Thread):
    """
    Held axis states; buttons are edge events.
    """
    def __init__(self, js_path=JS_PATH):
        super().__init__(daemon=True)
        self.js_path = js_path
        self._lock = threading.Lock()
        self._ev = Controls()
        self._stop = False

        self._x_state = 0
        self._y_state = 0
        self._last_axis_ts = time.time()
        self.AXIS_TIMEOUT = 7.0

    def stop(self):
        self._stop = True

    def pop(self) -> Controls:
        with self._lock:
            e = self._ev
            self._ev = Controls()

            now = time.time()
            if (now - self._last_axis_ts) > self.AXIS_TIMEOUT:
                self._x_state = 0
                self._y_state = 0

            if self._x_state == -1:
                e.left = True; e.any = True
            elif self._x_state == +1:
                e.right = True; e.any = True

            if self._y_state == -1:
                e.up = True; e.any = True
            elif self._y_state == +1:
                e.down = True; e.any = True

            return e

    def _push_btn(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self._ev, k, getattr(self._ev, k) or v)
            self._ev.any = True

    def run(self):
        if not os.path.exists(self.js_path):
            return

        fmt = "IhBB"
        sz = struct.calcsize(fmt)

        try:
            with open(self.js_path, "rb", buffering=0) as f:
                while not self._stop:
                    data = f.read(sz)
                    if not data or len(data) != sz:
                        time.sleep(0.005)
                        continue

                    _t, value, etype, num = struct.unpack(fmt, data)
                    if (etype & 0x80) != 0:
                        continue
                    et = etype & 0x7F

                    if et == 0x02:  # axis
                        v = int(value)
                        self._last_axis_ts = time.time()

                        if num == AXIS_X:
                            if v < -DEADZONE: self._x_state = -1
                            elif v > DEADZONE: self._x_state = +1
                            else: self._x_state = 0

                        elif num == AXIS_Y:
                            if v < -DEADZONE: self._y_state = -1
                            elif v > DEADZONE: self._y_state = +1
                            else: self._y_state = 0

                    elif et == 0x01 and value == 1:  # button press
                        if num == BTN_START: self._push_btn(start=True)
                        elif num == BTN_SELECT: self._push_btn(select=True)
                        elif num == BTN_A: self._push_btn(a=True)
                        elif num == BTN_B: self._push_btn(b=True)
                        elif num == BTN_X: self._push_btn(x=True)
                        elif num == BTN_Y: self._push_btn(y=True)

        except Exception:
            return


# =========================
# USB / LOCAL PATH SELECTION
# =========================
def find_usb_photo_dir() -> Optional[str]:
    # manual mount fallback first
    for name in USB_PHOTO_FOLDER_NAMES:
        p = os.path.join("/mnt/usb", name)
        if os.path.isdir(p):
            return p

    # typical automount locations
    roots = []
    roots += glob.glob("/media/pi/*")
    roots += glob.glob("/media/*")
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in USB_PHOTO_FOLDER_NAMES:
            p = os.path.join(root, name)
            if os.path.isdir(p):
                return p
    return None

def choose_active_photo_dir() -> str:
    usb = find_usb_photo_dir()
    if usb:
        return usb
    return LOCAL_PHOTO_DIR


# =========================
# IMAGE LISTING + LOADING
# =========================
def list_image_files(folder: str) -> List[str]:
    if not os.path.isdir(folder):
        return []
    out = []
    for p in sorted(glob.glob(os.path.join(folder, "*"))):
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(p)[1].lower()
        if ext in IMG_EXT:
            out.append(p)
    return out

def fit_image_to_screen(im: Image.Image) -> Image.Image:
    im = im.convert("RGB")
    sw, sh = im.size
    if sw <= 0 or sh <= 0:
        return Image.new("RGB", (W, H), BG)

    scale = min(W / sw, H / sh)
    nw = max(1, int(sw * scale))
    nh = max(1, int(sh * scale))
    rim = im.resize((nw, nh), Image.BILINEAR)

    out = Image.new("RGB", (W, H), BG)
    ox = (W - nw) // 2
    oy = (H - nh) // 2
    out.paste(rim, (ox, oy))
    return out

def load_image_frame(path: Optional[str]) -> Image.Image:
    if not path:
        return Image.new("RGB", (W, H), BG)
    try:
        im = Image.open(path)
        return fit_image_to_screen(im)
    except Exception:
        return Image.new("RGB", (W, H), BG)


# =========================
# UI DRAW
# =========================
def draw_menu_overlay(img_rgb: Image.Image, idx: int, font_mid, mode: str, hold_sec: int):
    d = ImageDraw.Draw(img_rgb)

    # Fix: podignuto gore i više prostora dolje (EXIT više ne dira crtu)
    x0, y0, x1, y1 = 18, 2, 110, 61
    d.rectangle((x0, y0, x1, y1), fill=MENU_BG, outline=MENU_DIM)

    # Fix: start y pomaknut gore + bolji razmak
    base_y = y0 + 4
    line_h = 11  # 5 * 11 = 55; u boxu 59px visine -> stane lijepo

    for i, item in enumerate(MENU_ITEMS):
        yy = base_y + i * line_h
        label = item
        if item == "MODE":
            label = mode
        elif item == "TIME":
            label = "OFF" if hold_sec == OFF_TIME else f"{hold_sec}s"

        if i == idx:
            d.rectangle((x0 + 6, yy - 1, x1 - 6, yy + 9), outline=MENU_FG, fill=MENU_BG)
            tw = text_width(d, label, font_mid)
            draw_text_crisp(img_rgb, ((W - tw)//2, yy - 1), label, font_mid, fill=MENU_FG, threshold=75)
        else:
            tw = text_width(d, label, font_mid)
            draw_text_crisp(img_rgb, ((W - tw)//2, yy - 1), label, font_mid, fill=MENU_DIM, threshold=75)

def shorten_name(name: str, max_chars: int) -> str:
    if len(name) <= max_chars:
        return name
    base, ext = os.path.splitext(name)
    if not ext:
        return name[:max_chars-3] + "..."
    keep = max_chars - len(ext) - 3
    if keep <= 1:
        return name[:max_chars-3] + "..."
    return base[:keep] + "..." + ext

def draw_file_picker(img_rgb: Image.Image, files: List[str], sel: int, scroll: int, font_mid, font_small):
    d = ImageDraw.Draw(img_rgb)
    x0, y0, x1, y1 = 10, 6, 118, 58
    d.rectangle((x0, y0, x1, y1), fill=MENU_BG, outline=MENU_DIM)

    title = "CHOOSE FILE"
    tw = text_width(d, title, font_small)
    draw_text_crisp(img_rgb, ((W - tw)//2, y0 + 2), title, font_small, fill=MENU_DIM, threshold=75)

    list_y0 = y0 + 14
    visible = 4
    line_h = 11
    left_text = x0 + 10
    max_chars = 22

    for i in range(visible):
        idx = scroll + i
        yy = list_y0 + i * line_h
        if idx >= len(files):
            continue

        name = shorten_name(os.path.basename(files[idx]), max_chars)

        if idx == sel:
            d.rectangle((x0 + 6, yy - 2, x1 - 6, yy + 9), outline=MENU_FG, fill=MENU_BG)
            draw_text_crisp(img_rgb, (left_text, yy - 1), name, font_mid, fill=MENU_FG, threshold=75)
        else:
            draw_text_crisp(img_rgb, (left_text, yy - 1), name, font_mid, fill=MENU_DIM, threshold=75)


# =========================
# TRANSITIONS
# =========================
def _blend(a: Image.Image, b: Image.Image, t: float) -> Image.Image:
    return Image.blend(a, b, t)

def transition(matrix: RGBMatrix, mode: str, prev: Image.Image, nxt: Image.Image):
    if mode == "CUT":
        matrix.SetImage(nxt, 0, 0)
        return

    if mode == "FLASH":
        black = Image.new("RGB", (W, H), BG)
        matrix.SetImage(black, 0, 0)
        time.sleep(0.06)
        matrix.SetImage(nxt, 0, 0)
        return

    if mode == "FADE":
        steps = 10
        total = 0.35
        dt = total / steps
        for i in range(steps + 1):
            t = i / float(steps)
            frame = _blend(prev, nxt, t)
            matrix.SetImage(frame, 0, 0)
            time.sleep(dt)
        return

    if mode in ("SLIDE_L", "SLIDE_R"):
        steps = 12
        total = 0.30
        dt = total / steps
        for i in range(steps + 1):
            t = i / float(steps)
            dx = int(t * W)
            frame = Image.new("RGB", (W, H), BG)
            if mode == "SLIDE_L":
                frame.paste(prev, (-dx, 0))
                frame.paste(nxt, (W - dx, 0))
            else:
                frame.paste(prev, (dx, 0))
                frame.paste(nxt, (-W + dx, 0))
            matrix.SetImage(frame, 0, 0)
            time.sleep(dt)
        return

    if mode in ("WIPE_L", "WIPE_R"):
        steps = 12
        total = 0.25
        dt = total / steps
        for i in range(steps + 1):
            t = i / float(steps)
            cut = int(t * W)
            frame = prev.copy()
            if mode == "WIPE_L":
                region = nxt.crop((0, 0, cut, H))
                frame.paste(region, (0, 0))
            else:
                region = nxt.crop((W - cut, 0, W, H))
                frame.paste(region, (W - cut, 0))
            matrix.SetImage(frame, 0, 0)
            time.sleep(dt)
        return

    if mode == "ROTATE":
        steps = 10
        total = 0.30
        dt = total / steps
        for i in range(steps + 1):
            t = i / float(steps)
            s = abs(1.0 - 2.0 * t)
            w = max(1, int(W * (0.15 + 0.85 * s)))
            src = prev if t < 0.5 else nxt
            small = src.resize((w, H), Image.BILINEAR)
            frame = Image.new("RGB", (W, H), BG)
            frame.paste(small, ((W - w) // 2, 0))
            matrix.SetImage(frame, 0, 0)
            time.sleep(dt)
        matrix.SetImage(nxt, 0, 0)
        return

    matrix.SetImage(nxt, 0, 0)


# =========================
# TIME HOLD REPEATER (FIXED)
# =========================
class HoldRepeater:
    """
    1x klik odmah (edge), a auto-repeat tek nakon hold_delay sekundi.
    """
    def __init__(self, hold_delay=2.0, interval=0.06):
        self.hold_delay = hold_delay
        self.interval = interval
        self._held = False
        self._t0 = 0.0
        self._last = 0.0

    def start(self, now: float):
        self._held = True
        self._t0 = now
        self._last = now

    def stop(self):
        self._held = False

    def repeating(self, now: float) -> bool:
        if not self._held:
            return False
        if (now - self._t0) < self.hold_delay:
            return False
        if (now - self._last) >= self.interval:
            self._last = now
            return True
        return False


# =========================
# MAIN
# =========================
def main():
    matrix = make_matrix()
    base_img = Image.new("RGB", (W, H), BG)

    font_mid = load_font(11)
    font_small = load_font(10)

    js = Js0Reader()
    js.start()

    # settings
    mode_idx = 0
    hold_sec = DEFAULT_TIME_SEC  # OFF_TIME means OFF

    # active folder (USB > local)
    active_dir = choose_active_photo_dir()
    files = list_image_files(active_dir)
    selected_idx = 0

    # UI state
    in_menu = False
    menu_idx = 0
    in_picker = False
    picker_scroll = 0

    # start/select debounce
    MENU_DEBOUNCE = 0.28
    last_menu_toggle = 0.0

    # edges
    prev_up = prev_down = prev_left = prev_right = False

    # rescan (USB hotplug)
    RESCAN_EVERY = 2.0
    last_rescan = time.time()

    # playback
    current_path: Optional[str] = files[selected_idx] if files else None
    current_img: Image.Image = load_image_frame(current_path)
    last_switch = time.time()

    # repeaters for TIME (left/right)
    rep_left = HoldRepeater(hold_delay=2.0, interval=0.06)
    rep_right = HoldRepeater(hold_delay=2.0, interval=0.06)

    def show_current_immediately():
        matrix.SetImage(current_img, 0, 0)

    def open_menu():
        nonlocal in_menu, in_picker, menu_idx
        in_picker = False
        in_menu = True
        menu_idx = 0

    def resume_playback():
        nonlocal in_menu, in_picker, last_switch
        in_menu = False
        in_picker = False
        rep_left.stop()
        rep_right.stop()
        show_current_immediately()
        last_switch = time.time()

    def rescan():
        nonlocal active_dir, files, selected_idx, current_path, current_img, last_switch
        new_dir = choose_active_photo_dir()
        if new_dir != active_dir:
            active_dir = new_dir
            files = list_image_files(active_dir)
            selected_idx = 0
            current_path = files[0] if files else None
            current_img = load_image_frame(current_path)
            show_current_immediately()
            last_switch = time.time()
            return

        new_files = list_image_files(active_dir)
        if new_files != files:
            files = new_files
            if not files:
                selected_idx = 0
                current_path = None
                current_img = load_image_frame(None)
                show_current_immediately()
            else:
                if current_path in files:
                    selected_idx = files.index(current_path)
                else:
                    selected_idx = 0
                    current_path = files[0]
                    current_img = load_image_frame(current_path)
                    show_current_immediately()
            last_switch = time.time()

    def apply_select(idx: int):
        nonlocal selected_idx, current_path, current_img, last_switch
        if not files:
            return
        idx = idx % len(files)
        prev = current_img
        selected_idx = idx
        current_path = files[selected_idx]
        nxt = load_image_frame(current_path)
        transition(matrix, MODES[mode_idx], prev, nxt)
        current_img = nxt
        last_switch = time.time()

    def advance_next():
        if not files:
            return
        apply_select(selected_idx + 1)

    def adjust_time_one_step(delta: int):
        """
        OFF <-> 1..999
        delta is +/-1
        """
        nonlocal hold_sec
        v = hold_sec

        if v == OFF_TIME:
            if delta > 0:
                v = MIN_TIME_SEC
            else:
                v = OFF_TIME
        else:
            v += delta
            if v < MIN_TIME_SEC:
                v = OFF_TIME
            elif v > MAX_TIME_SEC:
                v = MAX_TIME_SEC

        hold_sec = v

    # show first
    show_current_immediately()

    try:
        while True:
            inp = js.pop()
            now = time.time()

            # periodic rescan
            if (now - last_rescan) >= RESCAN_EVERY:
                last_rescan = now
                rescan()

            # START/SELECT: open menu or resume if already in menu/picker
            if (inp.start or inp.select) and (now - last_menu_toggle > MENU_DEBOUNCE):
                last_menu_toggle = now
                if in_menu or in_picker:
                    resume_playback()
                else:
                    open_menu()

                # reset edges to avoid instant scroll
                prev_up = inp.up
                prev_down = inp.down
                prev_left = inp.left
                prev_right = inp.right

            # edges
            up_edge = inp.up and not prev_up
            down_edge = inp.down and not prev_down
            left_edge = inp.left and not prev_left
            right_edge = inp.right and not prev_right
            prev_up, prev_down, prev_left, prev_right = inp.up, inp.down, inp.left, inp.right

            ok = inp.a or inp.x
            back = inp.b or inp.y

            # ===== MENU =====
            if in_menu:
                # navigation
                if up_edge:
                    menu_idx = (menu_idx - 1) % len(MENU_ITEMS)
                elif down_edge:
                    menu_idx = (menu_idx + 1) % len(MENU_ITEMS)

                # BACK -> resume
                if back:
                    resume_playback()
                    continue

                current_item = MENU_ITEMS[menu_idx]

                # MODE with LEFT/RIGHT
                if current_item == "MODE":
                    rep_left.stop(); rep_right.stop()
                    if left_edge:
                        mode_idx = (mode_idx - 1) % len(MODES)
                    elif right_edge:
                        mode_idx = (mode_idx + 1) % len(MODES)

                # TIME with: 1x click +/-1; hold >2s repeats fast
                elif current_item == "TIME":
                    # single click
                    if left_edge:
                        adjust_time_one_step(-1)
                        rep_left.start(now)
                    if right_edge:
                        adjust_time_one_step(+1)
                        rep_right.start(now)

                    # held repeat after 2s
                    if inp.left:
                        if rep_left.repeating(now):
                            adjust_time_one_step(-1)
                    else:
                        rep_left.stop()

                    if inp.right:
                        if rep_right.repeating(now):
                            adjust_time_one_step(+1)
                    else:
                        rep_right.stop()

                else:
                    rep_left.stop(); rep_right.stop()

                # OK action
                if ok:
                    item = MENU_ITEMS[menu_idx]
                    if item == "RESUME":
                        resume_playback()
                        continue
                    elif item == "CHOOSE FILE":
                        in_menu = False
                        in_picker = True
                        picker_scroll = max(0, min(selected_idx, max(0, len(files) - 4)))
                    elif item == "EXIT":
                        exec_launcher_or_exit(matrix)

                # render menu overlay (over current image, dimmed)
                frame = current_img.copy()
                dim_overlay(frame, alpha=155)
                draw_menu_overlay(frame, menu_idx, font_mid, MODES[mode_idx], hold_sec)
                matrix.SetImage(frame, 0, 0)
                time.sleep(0.016)
                continue

            # ===== FILE PICKER =====
            if in_picker:
                if not files:
                    frame = base_img.copy()
                    dim_overlay(frame, alpha=155)
                    d = ImageDraw.Draw(frame)
                    d.rectangle((10, 14, 118, 50), fill=MENU_BG, outline=MENU_DIM)
                    msg = "NO IMAGES"
                    tw = text_width(d, msg, font_small)
                    draw_text_crisp(frame, ((W - tw)//2, 30), msg, font_small, fill=MENU_FG, threshold=75)
                    matrix.SetImage(frame, 0, 0)
                    time.sleep(0.05)
                    continue

                if up_edge:
                    selected_idx = (selected_idx - 1) % len(files)
                elif down_edge:
                    selected_idx = (selected_idx + 1) % len(files)

                if selected_idx < picker_scroll:
                    picker_scroll = selected_idx
                if selected_idx >= picker_scroll + 4:
                    picker_scroll = selected_idx - 3

                if back:
                    in_picker = False
                    in_menu = True
                    menu_idx = MENU_ITEMS.index("CHOOSE FILE")
                    continue

                if ok:
                    apply_select(selected_idx)  # shows immediately
                    in_picker = False
                    in_menu = False
                    continue

                frame = current_img.copy()
                dim_overlay(frame, alpha=155)
                draw_file_picker(frame, files, selected_idx, picker_scroll, font_mid, font_small)
                matrix.SetImage(frame, 0, 0)
                time.sleep(0.016)
                continue

            # ===== PLAYBACK (NO TEXT) =====
            if hold_sec != OFF_TIME:
                if (now - last_switch) >= float(hold_sec):
                    advance_next()

            time.sleep(0.016)

    finally:
        js.stop()
        try:
            matrix.Clear()
        except Exception:
            pass


if __name__ == "__main__":
    main()
