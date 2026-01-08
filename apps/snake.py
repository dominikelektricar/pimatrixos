#!/usr/bin/env python3
import os
import sys
import time
import random
import struct
import threading
import glob
from dataclasses import dataclass
from typing import Tuple, Set

from PIL import Image, ImageDraw, ImageFont
from rgbmatrix import RGBMatrix, RGBMatrixOptions

# ===== DISPLAY CONFIG =====
PIXEL_MAPPER = "U-mapper;StackToRow:Z;Rotate:180"
W, H = 128, 64

# ===== SNAKE GRID =====
CELL = 4
GW, GH = W // CELL, H // CELL  # 32 x 16

# ===== SPEED (SLOWER RAMP) =====
START_SPEED = 6.2
MAX_SPEED   = 10.0
SPEED_GAIN  = 0.16  # +gain * sqrt(foods)

# ===== INPUT =====
JS_PATH = "/dev/input/js0"
DEADZONE = 12000
AXIS_X = 0
AXIS_Y = 1

# Button mapping (match your Pac-Man mapping)
BTN_X = 0
BTN_A = 1
BTN_B = 2
BTN_Y = 3
BTN_SELECT = 8
BTN_START  = 9

# ===== COLORS =====
BG        = (0, 0, 0)
SNAKE_COL = (0, 220, 80)
HEAD_COL  = (0, 255, 120)
FOOD_COL  = (255, 60, 60)

HUD_FG    = (240, 240, 240)
HUD_DIM   = (160, 160, 160)

MENU_BG   = (0, 0, 0)
MENU_DIM  = (140, 140, 140)
MENU_FG   = (240, 240, 240)

# ===== HISCORE FILE =====
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISCORE_PATH = os.path.join(SCRIPT_DIR, "snake_highscore.txt")

def load_hiscore() -> int:
    try:
        with open(HISCORE_PATH, "r", encoding="utf-8") as f:
            return int((f.read().strip() or "0"))
    except Exception:
        return 0

def save_hiscore(v: int) -> None:
    try:
        with open(HISCORE_PATH, "w", encoding="utf-8") as f:
            f.write(str(int(v)))
    except Exception:
        pass

# ===== MATRIX =====
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

    # stability knobs
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

# ===== FONT (nicer + crisp) =====
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

def draw_centered_crisp(img_rgb: Image.Image, y: int, text: str, font, fill=(240, 240, 240)):
    d = ImageDraw.Draw(img_rgb)
    tw = text_width(d, text, font)
    x = (W - tw) // 2
    draw_text_crisp(img_rgb, (x, y), text, font, fill=fill)

def dim_overlay(img_rgb: Image.Image, alpha: int = 155):
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, alpha))
    base = img_rgb.convert("RGBA")
    base.alpha_composite(overlay)
    img_rgb.paste(base.convert("RGB"))

# ===== MENU =====
MENU_ITEMS = ["RESUME", "RESTART", "EXIT"]

def draw_menu_overlay(img_rgb: Image.Image, idx: int, font_mid, font_small):
    d = ImageDraw.Draw(img_rgb)
    x0, y0, x1, y1 = 18, 8, 110, 50
    d.rectangle((x0, y0, x1, y1), fill=MENU_BG, outline=MENU_DIM)

    base_y = y0 + 6
    for i, item in enumerate(MENU_ITEMS):
        yy = base_y + i * 12
        if i == idx:
            d.rectangle((x0 + 6, yy - 2, x1 - 6, yy + 10), outline=MENU_FG, fill=MENU_BG)
            draw_centered_crisp(img_rgb, yy, item, font_mid, fill=MENU_FG)
        else:
            draw_centered_crisp(img_rgb, yy, item, font_mid, fill=MENU_DIM)

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

# ===== INPUT EVENTS =====
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
    Robust joystick reader:
    - keeps current axis states (held directions)
    - pop() returns held direction too (not only edges)
    - axis timeout -> auto neutral (prevents "stuck" direction)
    """
    def __init__(self, js_path=JS_PATH):
        super().__init__(daemon=True)
        self.js_path = js_path
        self._lock = threading.Lock()
        self._ev = Controls()
        self._stop = False

        self._x_state = 0  # -1 left, 0 neutral, +1 right
        self._y_state = 0  # -1 up,   0 neutral, +1 down
        self._last_axis_ts = time.time()
        self.AXIS_TIMEOUT = 0.45  # seconds

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

            # held dirs
            if self._x_state == -1:
                e.left = True
                e.any = True
            elif self._x_state == +1:
                e.right = True
                e.any = True

            if self._y_state == -1:
                e.up = True
                e.any = True
            elif self._y_state == +1:
                e.down = True
                e.any = True

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
                            if v < -DEADZONE:
                                self._x_state = -1
                            elif v > DEADZONE:
                                self._x_state = +1
                            else:
                                self._x_state = 0

                        elif num == AXIS_Y:
                            if v < -DEADZONE:
                                self._y_state = -1
                            elif v > DEADZONE:
                                self._y_state = +1
                            else:
                                self._y_state = 0

                    elif et == 0x01 and value == 1:  # button press
                        if num == BTN_START:
                            self._push_btn(start=True)
                        elif num == BTN_SELECT:
                            self._push_btn(select=True)
                        elif num == BTN_A:
                            self._push_btn(a=True)
                        elif num == BTN_B:
                            self._push_btn(b=True)
                        elif num == BTN_X:
                            self._push_btn(x=True)
                        elif num == BTN_Y:
                            self._push_btn(y=True)

        except Exception:
            return

class KeyboardReader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._ev = Controls()
        self._stop = False

    def stop(self):
        self._stop = True

    def pop(self) -> Controls:
        with self._lock:
            e = self._ev
            self._ev = Controls()
            return e

    def _push(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self._ev, k, getattr(self._ev, k) or v)
            self._ev.any = True

    def run(self):
        try:
            from evdev import InputDevice, ecodes
        except Exception:
            return

        paths = sorted(glob.glob("/dev/input/by-path/*kbd*event*")) or sorted(glob.glob("/dev/input/event*"))
        if not paths:
            return

        dev = None
        for p in paths:
            try:
                dev = InputDevice(p)
                break
            except Exception:
                pass
        if dev is None:
            return

        KEY_UP = getattr(ecodes, "KEY_UP", 103)
        KEY_DOWN = getattr(ecodes, "KEY_DOWN", 108)
        KEY_LEFT = getattr(ecodes, "KEY_LEFT", 105)
        KEY_RIGHT = getattr(ecodes, "KEY_RIGHT", 106)
        KEY_ENTER = getattr(ecodes, "KEY_ENTER", 28)
        KEY_KPENTER = getattr(ecodes, "KEY_KPENTER", 96)
        KEY_SPACE = getattr(ecodes, "KEY_SPACE", 57)
        KEY_ESC = getattr(ecodes, "KEY_ESC", 1)
        KEY_BACKSPACE = getattr(ecodes, "KEY_BACKSPACE", 14)

        while not self._stop:
            try:
                for ev in dev.read():
                    if ev.type == ecodes.EV_KEY and ev.value == 1:
                        c = ev.code
                        if c == KEY_UP: self._push(up=True)
                        elif c == KEY_DOWN: self._push(down=True)
                        elif c == KEY_LEFT: self._push(left=True)
                        elif c == KEY_RIGHT: self._push(right=True)
                        elif c in (KEY_ENTER, KEY_KPENTER, KEY_SPACE): self._push(a=True)
                        elif c in (KEY_ESC, KEY_BACKSPACE): self._push(b=True)
            except Exception:
                pass
            time.sleep(0.01)

def merge(a: Controls, b: Controls) -> Controls:
    return Controls(
        up=a.up or b.up,
        down=a.down or b.down,
        left=a.left or b.left,
        right=a.right or b.right,
        start=a.start or b.start,
        select=a.select or b.select,
        a=a.a or b.a,
        b=a.b or b.b,
        x=a.x or b.x,
        y=a.y or b.y,
        any=a.any or b.any,
    )

# ===== GAME HELPERS =====
def new_food(snake_set: Set[Tuple[int, int]]) -> Tuple[int, int]:
    while True:
        fx = random.randrange(0, GW)
        fy = random.randrange(0, GH)
        if (fx, fy) not in snake_set:
            return fx, fy

def restart_game():
    snake = [(GW // 2, GH // 2), (GW // 2 - 1, GH // 2), (GW // 2 - 2, GH // 2)]
    snake_set = set(snake)
    direction = (1, 0)
    pending_dir = direction
    food = new_food(snake_set)
    score = 0
    foods = 0
    step_acc = 0.0
    return snake, snake_set, direction, pending_dir, food, score, foods, step_acc

def main():
    matrix = make_matrix()
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    font_mid = load_font(11)
    font_small = load_font(10)

    js = Js0Reader()
    js.start()
    kb = KeyboardReader()
    kb.start()

    hiscore = load_hiscore()
    hiscore_dirty = False

    snake, snake_set, direction, pending_dir, food, score, foods, step_acc = restart_game()

    menu = False
    menu_idx = 0
    waiting = True

    MENU_DEBOUNCE = 0.28
    last_menu_toggle = 0.0

    # --- single-step-only state for menu navigation ---
    prev_menu_up = False
    prev_menu_down = False

    last = time.perf_counter()

    def flush_hiscore():
        nonlocal hiscore_dirty
        if hiscore_dirty:
            save_hiscore(hiscore)
            hiscore_dirty = False

    try:
        while True:
            now = time.perf_counter()
            dt = now - last
            last = now
            if dt < 0: dt = 0
            if dt > 0.05: dt = 0.05

            inp = merge(js.pop(), kb.pop())

            # --- MENU ENTRY ---
            if (inp.start or inp.select) and (time.time() - last_menu_toggle > MENU_DEBOUNCE):
                last_menu_toggle = time.time()
                menu = True
                menu_idx = 0
                # reset menu edge states so it doesn't instantly scroll
                prev_menu_up = True if inp.up else False
                prev_menu_down = True if inp.down else False

            # --- MENU MODE ---
            if menu:
                # single-step only: act only on rising edge (False -> True)
                up_edge = inp.up and not prev_menu_up
                down_edge = inp.down and not prev_menu_down
                prev_menu_up = inp.up
                prev_menu_down = inp.down

                if up_edge:
                    menu_idx = (menu_idx - 1) % len(MENU_ITEMS)
                elif down_edge:
                    menu_idx = (menu_idx + 1) % len(MENU_ITEMS)

                # exit menu
                if (inp.start or inp.select or inp.y or inp.b) and (time.time() - last_menu_toggle > 0.08):
                    menu = False

                # confirm
                if inp.x or inp.a:
                    choice = MENU_ITEMS[menu_idx]
                    if choice == "RESUME":
                        menu = False
                    elif choice == "RESTART":
                        flush_hiscore()
                        snake, snake_set, direction, pending_dir, food, score, foods, step_acc = restart_game()
                        waiting = True
                        menu = False
                    elif choice == "EXIT":
                        flush_hiscore()
                        exec_launcher_or_exit(matrix)

            # --- GAME INPUT ---
            if not menu:
                if waiting:
                    if inp.up:
                        pending_dir = (0, -1); waiting = False
                    elif inp.down:
                        pending_dir = (0, 1); waiting = False
                    elif inp.left:
                        pending_dir = (-1, 0); waiting = False
                    elif inp.right:
                        pending_dir = (1, 0); waiting = False
                else:
                    if inp.up:
                        pending_dir = (0, -1)
                    elif inp.down:
                        pending_dir = (0, 1)
                    elif inp.left:
                        pending_dir = (-1, 0)
                    elif inp.right:
                        pending_dir = (1, 0)

                if not waiting:
                    if pending_dir[0] == -direction[0] and pending_dir[1] == -direction[1]:
                        pending_dir = direction

            speed = min(MAX_SPEED, START_SPEED + SPEED_GAIN * (foods ** 0.5))

            # --- STEP UPDATES ---
            if (not menu) and (not waiting):
                step_acc += dt * speed
                while step_acc >= 1.0:
                    step_acc -= 1.0
                    direction = pending_dir

                    hx, hy = snake[0]
                    nx = (hx + direction[0]) % GW
                    ny = (hy + direction[1]) % GH
                    new_head = (nx, ny)

                    tail = snake[-1]
                    eating = (new_head == food)

                    # self bite => auto reset to waiting
                    if new_head in snake_set and not (not eating and new_head == tail):
                        flush_hiscore()
                        snake, snake_set, direction, pending_dir, food, score, foods, step_acc = restart_game()
                        waiting = True
                        break

                    snake.insert(0, new_head)
                    snake_set.add(new_head)

                    if eating:
                        score += 1
                        foods += 1
                        food = new_food(snake_set)
                    else:
                        tx, ty = snake.pop()
                        snake_set.remove((tx, ty))

            # hi live update + mark dirty
            if score > hiscore:
                hiscore = score
                hiscore_dirty = True

            # --- RENDER ---
            d.rectangle((0, 0, W, H), fill=BG)

            fx, fy = food
            d.rectangle((fx*CELL, fy*CELL, fx*CELL + CELL - 1, fy*CELL + CELL - 1), fill=FOOD_COL)

            for i, (sx, sy) in enumerate(snake):
                col = HEAD_COL if i == 0 else SNAKE_COL
                d.rectangle((sx*CELL, sy*CELL, sx*CELL + CELL - 1, sy*CELL + CELL - 1), fill=col)

            draw_text_crisp(img, (2, 1), f"SCORE {score}", font_small, fill=HUD_FG, threshold=75)
            hi_text = f"HI {hiscore}"
            tw = text_width(d, hi_text, font_small)
            draw_text_crisp(img, (W - 2 - tw, 1), hi_text, font_small, fill=HUD_DIM, threshold=75)

            if waiting and not menu:
                draw_centered_crisp(img, 52, "PRESS D-PAD", font_small, fill=HUD_DIM)

            if menu:
                dim_overlay(img, alpha=155)
                draw_menu_overlay(img, menu_idx, font_mid, font_small)

            matrix.SetImage(img, 0, 0)
            time.sleep(0.016)

    finally:
        try:
            flush_hiscore()
        except Exception:
            pass
        js.stop()
        kb.stop()
        try:
            matrix.Clear()
        except Exception:
            pass

if __name__ == "__main__":
    main()
