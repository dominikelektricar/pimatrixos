#!/usr/bin/env python3
# Tetris for 128x64 RGBMatrix (rpi-rgb-led-matrix)
#
# Kontrole (kao u tvom Snake mappingu):
#   - DPAD LEFT/RIGHT: jedan pritisak = jedan pomak (precizno)
#   - DPAD DOWN (drži): soft drop (brže spuštanje)
#   - A ili X: rotacija
#   - Y ili B: HARD DROP (odmah na dno)
#   - START ili SELECT: pauza + meni (RESUME / RESTART / EXIT)
#   - GAME OVER: A/X = nova igra
#
# EXIT vraća na launcher.py.

import os
import sys
import time
import random
import struct
import threading
import glob
from dataclasses import dataclass
from typing import List, Tuple, Optional

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

# Button mapping (match your Snake mapping)
BTN_X = 0
BTN_A = 1
BTN_B = 2
BTN_Y = 3
BTN_SELECT = 8
BTN_START  = 9

# ===== GAME CONFIG =====
BOARD_W = 10
BOARD_H = 20

# kvadratne ćelije (da ne bude razvučeno)
CELL = 3
CELL_W = CELL
CELL_H = CELL

BOARD_PX_W = BOARD_W * CELL_W   # 30
BOARD_PX_H = BOARD_H * CELL_H   # 60

BOARD_X = 10
BOARD_Y = 2
UI_X = BOARD_X + BOARD_PX_W + 10  # ~50

# brzine
SOFT_DROP_INTERVAL = 0.035  # dok držiš DOWN
ROTATE_COOLDOWN = 0.14

# levele
LINES_PER_LEVEL = 10
BASE_DROP = 0.55
MIN_DROP  = 0.08

def drop_interval_for_level(level: int) -> float:
    return max(MIN_DROP, BASE_DROP * (0.88 ** max(0, level - 1)))

# ===== COLORS / UI =====
BG        = (0, 0, 0)
BORDER    = (70, 70, 70)

HUD_FG    = (240, 240, 240)
HUD_DIM   = (150, 150, 150)
HUD_ACC   = (220, 220, 220)

MENU_BG   = (0, 0, 0)
MENU_DIM  = (140, 140, 140)
MENU_FG   = (240, 240, 240)

COLORS = {
    "I": (0, 255, 255),
    "O": (255, 255, 0),
    "T": (190, 0, 255),
    "S": (0, 255, 0),
    "Z": (255, 0, 0),
    "J": (0, 140, 255),
    "L": (255, 160, 0),
}

SHAPES = {
    "I": [(0, 1), (1, 1), (2, 1), (3, 1)],
    "O": [(1, 1), (2, 1), (1, 2), (2, 2)],
    "T": [(1, 1), (0, 2), (1, 2), (2, 2)],
    "S": [(1, 1), (2, 1), (0, 2), (1, 2)],
    "Z": [(0, 1), (1, 1), (1, 2), (2, 2)],
    "J": [(0, 1), (0, 2), (1, 2), (2, 2)],
    "L": [(2, 1), (0, 2), (1, 2), (2, 2)],
}

# ===== HISCORE FILE =====
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISCORE_PATH = os.path.join(SCRIPT_DIR, "tetris_highscore.txt")

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

# ===== MATRIX (match Snake style) =====
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

    opts.disable_hardware_pulsing = True
    opts.gpio_slowdown = 2
    opts.pwm_bits = 8
    opts.pwm_lsb_nanoseconds = 300
    opts.pwm_dither_bits = 0

    try:
        opts.brightness = int(os.getenv("MATRIX_BRIGHTNESS", "60"))
    except Exception:
        opts.brightness = 60

    opts.drop_privileges = False
    return RGBMatrix(options=opts)

# ===== FONT (crisp) =====
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

    # okvir malo niži i sve pomaknuto gore, bez "PAUSED"
    x0, y0, x1, y1 = 18, 10, 110, 50
    d.rectangle((x0, y0, x1, y1), fill=MENU_BG, outline=MENU_DIM)

    base_y = y0 + 4
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
    Drži stanje osi (held smjerovi) -> pop() vraća held smjerove svaki frame.
    U mainu mi sami radimo "edge" (rising edge) za preciznost left/right/up.
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
    """Opcionalno: tipkovnica"""
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
        KEY_SPACE = getattr(ecodes, "KEY_SPACE", 57)
        KEY_ESC = getattr(ecodes, "KEY_ESC", 1)
        KEY_Z = getattr(ecodes, "KEY_Z", 44)  # rotate
        KEY_X = getattr(ecodes, "KEY_X", 45)  # hard drop

        while not self._stop:
            try:
                for ev in dev.read():
                    if ev.type == ecodes.EV_KEY and ev.value == 1:
                        c = ev.code
                        if c == KEY_UP: self._push(up=True)
                        elif c == KEY_DOWN: self._push(down=True)
                        elif c == KEY_LEFT: self._push(left=True)
                        elif c == KEY_RIGHT: self._push(right=True)
                        elif c in (KEY_ENTER, KEY_SPACE, KEY_Z): self._push(a=True)   # rotate
                        elif c == KEY_X: self._push(y=True)  # hard drop
                        elif c == KEY_ESC: self._push(b=True)
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

# ===== TETRIS LOGIC =====
def rotate_points(points: List[Tuple[int, int]], rot: int) -> List[Tuple[int, int]]:
    out = []
    for x, y in points:
        rx, ry = x, y
        for _ in range(rot % 4):
            rx, ry = 3 - ry, rx
        out.append((rx, ry))
    return out

def new_bag() -> List[str]:
    bag = list(SHAPES.keys())
    random.shuffle(bag)
    return bag

class Tetris:
    def __init__(self):
        self.board: List[List[Optional[str]]] = [[None for _ in range(BOARD_W)] for _ in range(BOARD_H)]
        self.bag = new_bag()
        self.next_bag = new_bag()

        self.score = 0
        self.lines = 0
        self.level = 1
        self.game_over = False

        self.cur_kind: Optional[str] = None
        self.cur_rot = 0
        self.cur_x = 3
        self.cur_y = 0

        self.spawn()

    def update_level(self):
        self.level = 1 + (self.lines // LINES_PER_LEVEL)

    def next_kind(self) -> str:
        if self.bag:
            return self.bag[0]
        return self.next_bag[0]

    def cells(self, kind: str, rot: int) -> List[Tuple[int, int]]:
        return rotate_points(SHAPES[kind], rot)

    def can_place(self, x: int, y: int, kind: str, rot: int) -> bool:
        for cx, cy in self.cells(kind, rot):
            bx = x + cx
            by = y + cy
            if bx < 0 or bx >= BOARD_W or by >= BOARD_H:
                return False
            if by >= 0 and self.board[by][bx] is not None:
                return False
        return True

    def spawn(self):
        if not self.bag:
            self.bag = self.next_bag
            self.next_bag = new_bag()

        self.cur_kind = self.bag.pop(0)
        self.cur_rot = 0
        self.cur_x = 3
        self.cur_y = -1

        if not self.can_place(self.cur_x, self.cur_y, self.cur_kind, self.cur_rot):
            self.game_over = True

    def clear_lines(self) -> int:
        new_rows = []
        cleared = 0
        for row in self.board:
            if all(cell is not None for cell in row):
                cleared += 1
            else:
                new_rows.append(row)
        while len(new_rows) < BOARD_H:
            new_rows.insert(0, [None for _ in range(BOARD_W)])
        self.board = new_rows
        return cleared

    def add_score_for_clear(self, cleared: int):
        base = {1: 100, 2: 300, 3: 500, 4: 800}.get(cleared, 0)
        self.score += base * self.level

    def lock(self):
        if not self.cur_kind:
            return
        for cx, cy in self.cells(self.cur_kind, self.cur_rot):
            bx = self.cur_x + cx
            by = self.cur_y + cy
            if 0 <= bx < BOARD_W and 0 <= by < BOARD_H:
                self.board[by][bx] = self.cur_kind

        cleared = self.clear_lines()
        if cleared:
            self.lines += cleared
            self.update_level()
            self.add_score_for_clear(cleared)

        self.spawn()

    def move(self, dx: int, dy: int) -> bool:
        if self.game_over or not self.cur_kind:
            return False
        nx, ny = self.cur_x + dx, self.cur_y + dy
        if self.can_place(nx, ny, self.cur_kind, self.cur_rot):
            self.cur_x, self.cur_y = nx, ny
            return True
        return False

    def rotate(self) -> bool:
        if self.game_over or not self.cur_kind:
            return False
        nr = (self.cur_rot + 1) % 4
        for ox in (0, -1, 1, -2, 2):
            if self.can_place(self.cur_x + ox, self.cur_y, self.cur_kind, nr):
                self.cur_x += ox
                self.cur_rot = nr
                return True
        return False

    def tick_drop(self):
        if self.game_over:
            return
        if not self.move(0, 1):
            self.lock()

    def hard_drop(self):
        if self.game_over:
            return
        moved = 0
        while self.move(0, 1):
            moved += 1
        if moved > 0:
            self.score += moved * 2
        self.lock()

# ===== RENDER =====
def clamp_u8(v: int) -> int:
    return 0 if v < 0 else (255 if v > 255 else v)

def shade(col, delta):
    r, g, b = col
    return (clamp_u8(r + delta), clamp_u8(g + delta), clamp_u8(b + delta))

def draw_cell(d: ImageDraw.ImageDraw, bx: int, by: int, color):
    x1 = BOARD_X + bx * CELL_W
    y1 = BOARD_Y + by * CELL_H
    x2 = x1 + CELL_W - 1
    y2 = y1 + CELL_H - 1

    d.rectangle((x1, y1, x2, y2), fill=color)

    light = shade(color, +50)
    dark  = shade(color, -60)
    d.line((x1, y1, x2, y1), fill=light)
    d.line((x1, y1, x1, y2), fill=light)
    d.line((x1, y2, x2, y2), fill=dark)
    d.line((x2, y1, x2, y2), fill=dark)

def fmt_time(sec: int) -> str:
    m = sec // 60
    s = sec % 60
    return f"{m:02d}:{s:02d}"

def render(game: Tetris, img: Image.Image, font_mid, font_small, hiscore: int, play_seconds: int):
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W - 1, H - 1), fill=BG)

    # board frame (bez mreže)
    bx0 = BOARD_X - 1
    by0 = BOARD_Y - 1
    bx1 = BOARD_X + BOARD_PX_W
    by1 = BOARD_Y + BOARD_PX_H
    d.rectangle((bx0, by0, bx1, by1), outline=BORDER, fill=BG)

    # settled
    for y in range(BOARD_H):
        for x in range(BOARD_W):
            k = game.board[y][x]
            if k:
                draw_cell(d, x, y, COLORS[k])

    # current
    if not game.game_over and game.cur_kind:
        for cx, cy in rotate_points(SHAPES[game.cur_kind], game.cur_rot):
            bx = game.cur_x + cx
            by = game.cur_y + cy
            if 0 <= bx < BOARD_W and 0 <= by < BOARD_H:
                draw_cell(d, bx, by, COLORS[game.cur_kind])

    # UI title
    draw_text_crisp(img, (UI_X, 2), "TETRIS", font_mid, fill=HUD_FG, threshold=75)

    # Next preview (mali box ispod naslova)
    nk = game.next_kind()
    p0x = UI_X
    p0y = 14
    d.rectangle((p0x - 1, p0y - 1, p0x + 12 + 1, p0y + 12 + 1), outline=(40, 40, 40), fill=BG)
    for cx, cy in SHAPES[nk]:
        x1 = p0x + cx * 3
        y1 = p0y + cy * 3
        d.rectangle((x1, y1, x1 + 2, y1 + 2), fill=COLORS[nk])

    # Info panel (uredno, bez preklapanja)
    y0 = 28
    step = 9
    draw_text_crisp(img, (UI_X, y0 + 0*step), f"SCORE {game.score}", font_small, fill=HUD_ACC, threshold=75)
    draw_text_crisp(img, (UI_X, y0 + 1*step), f"HI    {hiscore}", font_small, fill=HUD_DIM, threshold=75)
    draw_text_crisp(img, (UI_X, y0 + 2*step), f"LVL   {game.level}", font_small, fill=HUD_FG, threshold=75)
    draw_text_crisp(img, (UI_X, y0 + 3*step), f"LINES {game.lines}", font_small, fill=HUD_FG, threshold=75)
    draw_text_crisp(img, (UI_X, y0 + 4*step), f"TIME  {fmt_time(play_seconds)}", font_small, fill=HUD_DIM, threshold=75)

    if game.game_over:
        dim_overlay(img, alpha=140)
        d = ImageDraw.Draw(img)
        d.rectangle((16, 18, W - 16, 46), outline=MENU_DIM, fill=MENU_BG)
        draw_centered_crisp(img, 22, "GAME OVER", font_mid, fill=MENU_FG)
        draw_centered_crisp(img, 34, "A/X = NEW GAME", font_small, fill=MENU_DIM)

# ===== MAIN =====
def main():
    matrix = make_matrix()
    img = Image.new("RGB", (W, H), BG)

    font_mid = load_font(11)
    font_small = load_font(9)  # manji da sve stane čisto

    js = Js0Reader()
    js.start()
    kb = KeyboardReader()
    kb.start()

    hiscore = load_hiscore()
    hiscore_dirty = False

    def flush_hiscore():
        nonlocal hiscore_dirty
        if hiscore_dirty:
            save_hiscore(hiscore)
            hiscore_dirty = False

    game = Tetris()

    last = time.perf_counter()
    last_rotate = 0.0
    last_drop = time.perf_counter()

    # playtime (bez pauze)
    play_seconds = 0
    play_acc = 0.0

    # menu
    menu = False
    menu_idx = 0
    MENU_DEBOUNCE = 0.28
    last_menu_toggle = 0.0
    prev_menu_up = False
    prev_menu_down = False

    # precizni edge za DPAD (jedan pritisak = jedan pomak)
    prev_left = False
    prev_right = False
    prev_up = False
    prev_down = False

    try:
        while True:
            now = time.perf_counter()
            dt = now - last
            last = now
            if dt < 0: dt = 0
            if dt > 0.05: dt = 0.05

            inp = merge(js.pop(), kb.pop())

            # ----- PAUSE MENU TOGGLE -----
            if (inp.start or inp.select) and (time.time() - last_menu_toggle > MENU_DEBOUNCE):
                last_menu_toggle = time.time()
                menu = True
                menu_idx = 0
                prev_menu_up = True if inp.up else False
                prev_menu_down = True if inp.down else False

            # ----- MENU MODE -----
            if menu:
                up_edge = inp.up and not prev_menu_up
                down_edge = inp.down and not prev_menu_down
                prev_menu_up = inp.up
                prev_menu_down = inp.down

                if up_edge:
                    menu_idx = (menu_idx - 1) % len(MENU_ITEMS)
                elif down_edge:
                    menu_idx = (menu_idx + 1) % len(MENU_ITEMS)

                if (inp.start or inp.select or inp.y or inp.b) and (time.time() - last_menu_toggle > 0.08):
                    menu = False

                if inp.x or inp.a:
                    choice = MENU_ITEMS[menu_idx]
                    if choice == "RESUME":
                        menu = False
                    elif choice == "RESTART":
                        flush_hiscore()
                        game = Tetris()
                        last_drop = time.perf_counter()
                        play_seconds = 0
                        play_acc = 0.0
                        menu = False
                    elif choice == "EXIT":
                        flush_hiscore()
                        exec_launcher_or_exit(matrix)

                if game.score > hiscore:
                    hiscore = game.score
                    hiscore_dirty = True

                render(game, img, font_mid, font_small, hiscore, play_seconds)
                dim_overlay(img, alpha=155)
                draw_menu_overlay(img, menu_idx, font_mid, font_small)
                matrix.SetImage(img, 0, 0)
                time.sleep(0.016)
                continue

            # ----- GAME OVER: A/X = NEW GAME -----
            if game.game_over:
                if inp.a or inp.x:
                    flush_hiscore()
                    game = Tetris()
                    last_drop = time.perf_counter()
                    play_seconds = 0
                    play_acc = 0.0

                if game.score > hiscore:
                    hiscore = game.score
                    hiscore_dirty = True

                render(game, img, font_mid, font_small, hiscore, play_seconds)
                matrix.SetImage(img, 0, 0)
                time.sleep(0.016)
                continue

            # ----- PLAYTIME -----
            play_acc += dt
            if play_acc >= 1.0:
                add = int(play_acc)
                play_seconds += add
                play_acc -= add

            # ----- DPAD EDGES (precizno) -----
            left_edge  = inp.left  and not prev_left
            right_edge = inp.right and not prev_right
            up_edge    = inp.up    and not prev_up
            down_held  = inp.down  # soft drop mora biti held

            prev_left = inp.left
            prev_right = inp.right
            prev_up = inp.up
            prev_down = inp.down

            # ----- ACTIONS -----
            # rotacija: A ili X (i opcionalno DPAD UP edge)
            if (inp.a or inp.x or up_edge) and (time.perf_counter() - last_rotate) >= ROTATE_COOLDOWN:
                game.rotate()
                last_rotate = time.perf_counter()

            # hard drop: Y ili B
            if inp.y or inp.b:
                game.hard_drop()
                last_drop = time.perf_counter()

            # pomak: jedan pritisak = jedan pomak
            if left_edge:
                game.move(-1, 0)
            elif right_edge:
                game.move(+1, 0)

            # padanje: level ili soft drop dok držiš DOWN
            tnow = time.perf_counter()
            interval = SOFT_DROP_INTERVAL if down_held else drop_interval_for_level(game.level)
            if (tnow - last_drop) >= interval:
                game.tick_drop()
                last_drop = tnow

            # hi-score
            if game.score > hiscore:
                hiscore = game.score
                hiscore_dirty = True

            # render
            render(game, img, font_mid, font_small, hiscore, play_seconds)
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
