#!/usr/bin/env python3
# anim_player.py — player za VIDEO + GIF (bez slika) za RGBMatrix 128x64
#
# - Automatski koristi USB ako postoji /media/.../(media ili meda) folder
# - Inače koristi lokalni folder: ./media
#
# Kontrole:
#   START ili SELECT: meni (RESUME / CHOOSE FILE / PLAY MODE / EXIT)
#   CHOOSE FILE: DPAD up/down odabir, A/X OK, B/Y back
#   PLAY MODE: PLAY ALL (redoslijed svih fajlova) ili LOOP ONE (vrti odabrani)
#
# Playback: nema teksta na ekranu (čist prikaz)

import os
import sys
import time
import glob
import struct
import threading
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageSequence
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

# ===== MEDIA FOLDER =====
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_MEDIA_DIR = os.path.join(SCRIPT_DIR, "media")

# traži i "meda" jer si spomenuo taj naziv
USB_MEDIA_FOLDER_NAMES = ("media", "meda")

# ===== UI COLORS =====
BG        = (0, 0, 0)
MENU_BG   = (0, 0, 0)
MENU_DIM  = (140, 140, 140)
MENU_FG   = (240, 240, 240)

# ===== MENU =====
MENU_ITEMS = ["RESUME", "CHOOSE FILE", "PLAY MODE", "EXIT"]

# ===== PLAY SETTINGS =====
DEFAULT_GIF_FALLBACK_MS = 80

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm"}
GIF_EXT   = {".gif"}

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

    # stability knobs (same as your games)
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
    Held axis states; we use rising-edge in UI.
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
        self.AXIS_TIMEOUT = 0.45

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

                    if et == 0x02:
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

                    elif et == 0x01 and value == 1:
                        if num == BTN_START: self._push_btn(start=True)
                        elif num == BTN_SELECT: self._push_btn(select=True)
                        elif num == BTN_A: self._push_btn(a=True)
                        elif num == BTN_B: self._push_btn(b=True)
                        elif num == BTN_X: self._push_btn(x=True)
                        elif num == BTN_Y: self._push_btn(y=True)
        except Exception:
            return

# =========================
# USB MEDIA DETECTION
# =========================
def find_usb_media_dir() -> Optional[str]:
    """
    Traži /media/pi/*/(media|meda) i /media/*/(media|meda).
    Vraća prvi pronađeni folder.
    """
    # manual mount fallback
    for name in ("media", "meda"):
        p = os.path.join("/mnt/usb", name)
        if os.path.isdir(p):
            return p

    candidates: List[str] = []

    # najčešće na RPi OS
    candidates += glob.glob("/media/pi/*")
    candidates += glob.glob("/media/*")

    seen = set()
    for root in candidates:
        if not os.path.isdir(root):
            continue
        if root in seen:
            continue
        seen.add(root)

        for name in USB_MEDIA_FOLDER_NAMES:
            p = os.path.join(root, name)
            if os.path.isdir(p):
                return p
    return None

def choose_active_media_dir() -> str:
    usb = find_usb_media_dir()
    if usb:
        return usb
    return LOCAL_MEDIA_DIR

# =========================
# MEDIA LISTING (VIDEO + GIF ONLY)
# =========================
def list_media_files(folder: str) -> List[str]:
    if not os.path.isdir(folder):
        return []
    files = []
    for p in sorted(glob.glob(os.path.join(folder, "*"))):
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(p)[1].lower()
        if ext in GIF_EXT or ext in VIDEO_EXT:
            files.append(p)
    return files

# =========================
# VIDEO (ffmpeg)
# =========================
class FFMpegVideo:
    def __init__(self, path: str, loop_forever: bool):
        self.path = path
        self.loop_forever = loop_forever
        self.proc: Optional[subprocess.Popen] = None
        self.bufsize = W * H * 3

    def open(self) -> bool:
        # PLAY ALL -> no looping (EOF -> next file)
        # LOOP ONE -> loop forever
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if self.loop_forever:
            cmd += ["-stream_loop", "-1"]
        cmd += [
            "-i", self.path,
            "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                   f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "pipe:1",
        ]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return True
        except Exception:
            self.proc = None
            return False

    def read_frame(self) -> Optional[Image.Image]:
        if not self.proc or not self.proc.stdout:
            return None
        data = self.proc.stdout.read(self.bufsize)
        if not data or len(data) != self.bufsize:
            return None
        return Image.frombytes("RGB", (W, H), data)

    def close(self):
        try:
            if self.proc:
                self.proc.kill()
        except Exception:
            pass
        self.proc = None

# =========================
# GIF LOADING
# =========================
def fit_image_to_screen(im: Image.Image) -> Image.Image:
    # GIF frame fit (letterbox), ali bez teksta u playbacku
    im = im.convert("RGB")
    src_w, src_h = im.size
    if src_w <= 0 or src_h <= 0:
        return Image.new("RGB", (W, H), BG)

    scale = min(W / src_w, H / src_h)
    nw = max(1, int(src_w * scale))
    nh = max(1, int(src_h * scale))
    rim = im.resize((nw, nh), Image.BILINEAR)

    out = Image.new("RGB", (W, H), BG)
    ox = (W - nw) // 2
    oy = (H - nh) // 2
    out.paste(rim, (ox, oy))
    return out

# =========================
# UI
# =========================
def draw_menu_overlay(img_rgb: Image.Image, idx: int, font_mid, mode_play_all: bool):
    d = ImageDraw.Draw(img_rgb)
    # box dovoljno velik za 4 reda
    x0, y0, x1, y1 = 18, 6, 110, 58
    d.rectangle((x0, y0, x1, y1), fill=MENU_BG, outline=MENU_DIM)

    base_y = y0 + 6
    line_h = 12

    for i, item in enumerate(MENU_ITEMS):
        yy = base_y + i * line_h
        label = item
        if item == "PLAY MODE":
            label = "PLAY ALL" if mode_play_all else "LOOP ONE"

        if i == idx:
            d.rectangle((x0 + 6, yy - 2, x1 - 6, yy + 10), outline=MENU_FG, fill=MENU_BG)
            tw = text_width(d, label, font_mid)
            draw_text_crisp(img_rgb, ((W - tw)//2, yy), label, font_mid, fill=MENU_FG, threshold=75)
        else:
            tw = text_width(d, label, font_mid)
            draw_text_crisp(img_rgb, ((W - tw)//2, yy), label, font_mid, fill=MENU_DIM, threshold=75)

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
# MAIN
# =========================
def main():
    matrix = make_matrix()
    base_img = Image.new("RGB", (W, H), BG)

    font_mid = load_font(11)
    font_small = load_font(10)

    js = Js0Reader()
    js.start()

    # active media source (USB > local)
    active_media_dir = choose_active_media_dir()
    files = list_media_files(active_media_dir)
    selected_idx = 0

    mode_play_all = True  # True = play all, False = loop one

    # UI state
    menu = False
    menu_idx = 0
    picker = False
    picker_scroll = 0

    MENU_DEBOUNCE = 0.28
    last_menu_toggle = 0.0

    # edges
    prev_up = prev_down = False

    # rescan (USB hotplug)
    RESCAN_EVERY = 2.0
    last_rescan = time.time()

    # player state
    current_path: Optional[str] = files[selected_idx] if files else None
    current_type: Optional[str] = None  # "gif" | "vid"
    gif_frames: List[Tuple[Image.Image, float]] = []
    gif_i = 0
    gif_t = 0.0
    vid: Optional[FFMpegVideo] = None

    def detect_type(path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext in GIF_EXT: return "gif"
        return "vid"

    def close_media():
        nonlocal vid, gif_frames
        if vid:
            vid.close()
            vid = None
        gif_frames = []

    def open_media(path: str):
        nonlocal current_path, current_type, gif_frames, gif_i, gif_t, vid
        close_media()
        current_path = path
        current_type = detect_type(path)
        gif_i = 0
        gif_t = 0.0

        if current_type == "gif":
            try:
                im = Image.open(path)
                frames = []
                for fr in ImageSequence.Iterator(im):
                    delay_ms = fr.info.get("duration", DEFAULT_GIF_FALLBACK_MS)
                    delay_sec = max(0.02, float(delay_ms) / 1000.0)
                    frames.append((fit_image_to_screen(fr), delay_sec))
                gif_frames[:] = frames if frames else [(fit_image_to_screen(im), 0.08)]
            except Exception:
                gif_frames[:] = [(Image.new("RGB", (W, H), BG), 0.2)]

        else:
            loop_forever = (not mode_play_all)  # loop only in LOOP ONE
            vid = FFMpegVideo(path, loop_forever=loop_forever)
            if not vid.open():
                # ako nema ffmpeg ili greška, samo crno
                current_type = "gif"
                gif_frames[:] = [(Image.new("RGB", (W, H), BG), 0.2)]

    def advance_to_next_file():
        nonlocal selected_idx
        if not files:
            return
        selected_idx = (selected_idx + 1) % len(files)
        open_media(files[selected_idx])

    def rescan_media():
        nonlocal active_media_dir, files, selected_idx, current_path

        new_dir = choose_active_media_dir()
        if new_dir != active_media_dir:
            active_media_dir = new_dir
            files = list_media_files(active_media_dir)
            selected_idx = 0
            current_path = files[0] if files else None
            if current_path:
                open_media(current_path)
            else:
                close_media()
            return

        # isti dir, ali moguće da su se fajlovi promijenili
        new_files = list_media_files(active_media_dir)
        if new_files != files:
            files = new_files
            if not files:
                selected_idx = 0
                close_media()
                current_path = None
            else:
                # zadrži current ako postoji
                if current_path in files:
                    selected_idx = files.index(current_path)
                else:
                    selected_idx = 0
                    open_media(files[0])

    # init
    if current_path:
        open_media(current_path)

    try:
        last = time.perf_counter()

        while True:
            now = time.perf_counter()
            dt = now - last
            last = now
            if dt < 0: dt = 0
            if dt > 0.05: dt = 0.05

            inp = js.pop()

            # periodic rescan (USB hotplug + file changes)
            if (time.time() - last_rescan) >= RESCAN_EVERY:
                last_rescan = time.time()
                rescan_media()

            # menu toggle
            if (inp.start or inp.select) and (time.time() - last_menu_toggle > MENU_DEBOUNCE):
                last_menu_toggle = time.time()
                if picker:
                    picker = False
                else:
                    menu = True
                    menu_idx = 0
                prev_up = inp.up
                prev_down = inp.down

            up_edge = inp.up and not prev_up
            down_edge = inp.down and not prev_down
            prev_up, prev_down = inp.up, inp.down

            # ===== MENU =====
            if menu:
                if up_edge:
                    menu_idx = (menu_idx - 1) % len(MENU_ITEMS)
                elif down_edge:
                    menu_idx = (menu_idx + 1) % len(MENU_ITEMS)

                if inp.b or inp.y:
                    menu = False

                if inp.a or inp.x:
                    item = MENU_ITEMS[menu_idx]
                    if item == "RESUME":
                        menu = False
                    elif item == "CHOOSE FILE":
                        menu = False
                        picker = True
                        picker_scroll = max(0, min(selected_idx, max(0, len(files) - 4)))
                    elif item == "PLAY MODE":
                        mode_play_all = not mode_play_all
                        # re-open current video to apply loop mode
                        if current_path and detect_type(current_path) == "vid":
                            open_media(current_path)
                    elif item == "EXIT":
                        close_media()
                        exec_launcher_or_exit(matrix)

                frame = base_img.copy()
                dim_overlay(frame, alpha=155)
                draw_menu_overlay(frame, menu_idx, font_mid, mode_play_all)
                matrix.SetImage(frame, 0, 0)
                time.sleep(0.016)
                continue

            # ===== FILE PICKER =====
            if picker:
                if not files:
                    frame = base_img.copy()
                    dim_overlay(frame, alpha=155)
                    d = ImageDraw.Draw(frame)
                    d.rectangle((10, 14, 118, 50), fill=MENU_BG, outline=MENU_DIM)
                    msg = "NO VIDEO/GIF FILES"
                    tw = text_width(d, msg, font_small)
                    draw_text_crisp(frame, ((W - tw)//2, 28), msg, font_small, fill=MENU_FG, threshold=75)
                    matrix.SetImage(frame, 0, 0)
                    time.sleep(0.016)
                    continue

                if up_edge:
                    selected_idx = (selected_idx - 1) % len(files)
                elif down_edge:
                    selected_idx = (selected_idx + 1) % len(files)

                if selected_idx < picker_scroll:
                    picker_scroll = selected_idx
                if selected_idx >= picker_scroll + 4:
                    picker_scroll = selected_idx - 3

                if inp.b or inp.y:
                    picker = False

                if inp.a or inp.x:
                    open_media(files[selected_idx])
                    picker = False

                frame = base_img.copy()
                dim_overlay(frame, alpha=155)
                draw_file_picker(frame, files, selected_idx, picker_scroll, font_mid, font_small)
                matrix.SetImage(frame, 0, 0)
                time.sleep(0.016)
                continue

            # ===== PLAYBACK (NO TEXT) =====
            if not current_path:
                matrix.SetImage(base_img, 0, 0)
                time.sleep(0.05)
                continue

            if current_type == "vid" and vid:
                fr = vid.read_frame()
                if fr is None:
                    # EOF / error
                    if mode_play_all:
                        advance_to_next_file()
                    else:
                        open_media(current_path)
                    matrix.SetImage(base_img, 0, 0)
                    time.sleep(0.016)
                    continue

                matrix.SetImage(fr, 0, 0)

            else:
                # GIF
                if not gif_frames:
                    open_media(current_path)

                frame, delay = gif_frames[gif_i]
                gif_t += dt

                if gif_t >= delay:
                    gif_t = 0.0
                    gif_i += 1
                    if gif_i >= len(gif_frames):
                        gif_i = 0
                        if mode_play_all:
                            advance_to_next_file()

                matrix.SetImage(frame, 0, 0)

            time.sleep(0.016)

    finally:
        try:
            close_media()
        except Exception:
            pass
        js.stop()
        try:
            matrix.Clear()
        except Exception:
            pass

if __name__ == "__main__":
    main()
