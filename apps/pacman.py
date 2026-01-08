#!/usr/bin/env python3
import os, sys, time, random, struct, threading, signal
from dataclasses import dataclass
from typing import Tuple, Set, List, Dict
from collections import deque

from PIL import Image, ImageDraw
from rgbmatrix import RGBMatrix, RGBMatrixOptions

# ======================
# MATRIX CONFIG (KEEP YOUR SETTINGS)
# ======================
PIXEL_MAPPER = "U-mapper;StackToRow:Z;Rotate:180"
W, H = 128, 64

def make_matrix():
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

# ======================
# COLORS
# ======================
BLACK  = (0, 0, 0)
WALL   = (0, 0, 170)
PEL    = (220, 220, 220)
PWR    = (255, 255, 255)
PAC    = (255, 220, 0)
TXT    = (240, 240, 240)
DIM    = (150, 150, 150)
FRIGHT = (40, 70, 255)

# HUD palette (high contrast)
HUD_BG   = (0, 0, 0)
HUD_LINE = (60, 60, 60)
HUD_FG   = (255, 255, 255)
HUD_DIM  = (200, 200, 200)
HUD_LAB  = (150, 150, 150)

GHOSTS = [
    ("blinky", (255, 0, 0)),
    ("pinky",  (255, 120, 180)),
    ("inky",   (0, 200, 255)),
    ("clyde",  (255, 140, 0)),
]

# ======================
# FILES
# ======================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISCORE_PATH = os.path.join(SCRIPT_DIR, "pacman_highscore.txt")

def load_hiscore() -> int:
    try:
        with open(HISCORE_PATH, "r", encoding="utf-8") as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0

def save_hiscore(v: int) -> None:
    try:
        with open(HISCORE_PATH, "w", encoding="utf-8") as f:
            f.write(str(int(v)))
    except Exception:
        pass

# ======================
# MAP (28x31) - classic-like layout
# ======================
WALLMAP = [
"XXXXXXXXXXXXXXXXXXXXXXXXXXXX",
"X            XX            X",
"X XXXX XXXXX XX XXXXX XXXX X",
"X XXXX XXXXX XX XXXXX XXXX X",
"X XXXX XXXXX XX XXXXX XXXX X",
"X                          X",
"X XXXX XX XXXXXXXX XX XXXX X",
"X XXXX XX XXXXXXXX XX XXXX X",
"X      XX    XX    XX      X",
"XXXXXX XXXXX XX XXXXX XXXXXX",
"     X XXXXX XX XXXXX X     ",
"     X XX          XX X     ",
"     X XX XXXXXXXX XX X     ",
"XXXXXX XX X      X XX XXXXXX",
"          X      X          ",
"XXXXXX XX X      X XX XXXXXX",
"     X XX XXXXXXXX XX X     ",
"     X XX          XX X     ",
"     X XX XXXXXXXX XX X     ",
"XXXXXX XX XXXXXXXX XX XXXXXX",
"X            XX            X",
"X XXXX XXXXX XX XXXXX XXXX X",
"X XXXX XXXXX XX XXXXX XXXX X",
"X   XX                XX   X",
"XXX XX XX XXXXXXXX XX XX XXX",
"XXX XX XX XXXXXXXX XX XX XXX",
"X      XX    XX    XX      X",
"X XXXXXXXXXX XX XXXXXXXXXX X",
"X XXXXXXXXXX XX XXXXXXXXXX X",
"X                          X",
"XXXXXXXXXXXXXXXXXXXXXXXXXXXX",
]
ROWS = len(WALLMAP)
COLS = len(WALLMAP[0])

# ghost house + gate
GATE_CY = 12
GATE_XS = list(range(11, 17))
HOUSE_X0, HOUSE_X1 = 10, 17
HOUSE_Y0, HOUSE_Y1 = 11, 18

GHOST_HOME_TILES = {
    "blinky": (13, 14),
    "pinky":  (14, 14),
    "inky":   (13, 15),
    "clyde":  (14, 15),
}

def compute_house_exit_tile() -> Tuple[int, int]:
    candidates = [(14,10),(13,10),(15,10),(14,9),(14,11)]
    for cx, cy in candidates:
        if 0 <= cx < COLS and 0 <= cy < ROWS and WALLMAP[cy][cx] == " ":
            return (cx, cy)
    return (14, 10)

HOUSE_EXIT_TILE = compute_house_exit_tile()

# ======================
# SCALE / LAYOUT
# ======================
CELL_X = 4
CELL_Y = 2
BAND_R = 1
OX = 0
OY = 1

MAZE_W = OX + COLS * CELL_X
HUD_X0 = MAZE_W + 1  # right side HUD separator (do not change)

# ======================
# INPUT
# ======================
@dataclass
class NavEvent:
    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False
    a: bool = False
    b: bool = False
    x: bool = False
    y: bool = False
    start: bool = False
    select: bool = False

# Your controller mapping:
BTN_X = 0
BTN_A = 1
BTN_B = 2
BTN_Y = 3
BTN_SELECT = 8
BTN_START  = 9

MENU_TOGGLE_FALLBACK_Y = False
MENU_DEBOUNCE_SEC = 0.35

class Joystick(threading.Thread):
    def __init__(self, path="/dev/input/js0"):
        super().__init__(daemon=True)
        self.path = path
        self.lock = threading.Lock()
        self.ev = NavEvent()
        self.DEAD = 12000
        self._stop = False

    def stop(self):
        self._stop = True

    def pop(self) -> NavEvent:
        with self.lock:
            e = self.ev
            self.ev = NavEvent()
            return e

    def run(self):
        if not os.path.exists(self.path):
            return
        fmt = "IhBB"
        sz = struct.calcsize(fmt)
        try:
            with open(self.path, "rb", buffering=0) as f:
                while not self._stop:
                    data = f.read(sz)
                    if not data or len(data) != sz:
                        time.sleep(0.01)
                        continue
                    _, value, etype, num = struct.unpack(fmt, data)
                    if etype & 0x80:
                        continue
                    etype &= 0x7F

                    with self.lock:
                        if etype == 0x02:  # axis
                            if num == 0:
                                if value < -self.DEAD:
                                    self.ev.left = True
                                elif value > self.DEAD:
                                    self.ev.right = True
                            elif num == 1:
                                if value < -self.DEAD:
                                    self.ev.up = True
                                elif value > self.DEAD:
                                    self.ev.down = True

                        elif etype == 0x01 and value == 1:  # button press
                            if num == BTN_A: self.ev.a = True
                            elif num == BTN_B: self.ev.b = True
                            elif num == BTN_X: self.ev.x = True
                            elif num == BTN_Y: self.ev.y = True
                            elif num == BTN_START: self.ev.start = True
                            elif num == BTN_SELECT: self.ev.select = True
        except Exception:
            return

# ======================
# MAZE HELPERS
# ======================
DIRS = {"L": (-1, 0), "R": (1, 0), "U": (0, -1), "D": (0, 1)}
DIR_LIST = list(DIRS.values())

def in_bounds(cx: int, cy: int) -> bool:
    return 0 <= cx < COLS and 0 <= cy < ROWS

def is_wall(cx: int, cy: int) -> bool:
    if not in_bounds(cx, cy):
        return True
    return WALLMAP[cy][cx] == "X"

def is_gate_tile(cx: int, cy: int) -> bool:
    return (cy == GATE_CY) and (cx in GATE_XS)

def in_house(cx: int, cy: int) -> bool:
    return HOUSE_X0 <= cx <= HOUSE_X1 and HOUSE_Y0 <= cy <= HOUSE_Y1

def is_open_for_pac(cx: int, cy: int) -> bool:
    if is_gate_tile(cx, cy):
        return False
    return not is_wall(cx, cy)

def is_open_for_ghost(cx: int, cy: int) -> bool:
    return (not is_wall(cx, cy)) or is_gate_tile(cx, cy)

def cell_center(cx: int, cy: int) -> Tuple[int, int]:
    x = OX + cx * CELL_X + (CELL_X // 2)
    y = OY + cy * CELL_Y + (CELL_Y // 2)
    return x, y

def px_to_cell(px: int, py: int) -> Tuple[int, int]:
    cx = int(round((px - (OX + (CELL_X // 2))) / CELL_X))
    cy = int(round((py - (OY + (CELL_Y // 2))) / CELL_Y))
    cx = max(0, min(COLS - 1, cx))
    cy = max(0, min(ROWS - 1, cy))
    return cx, cy

def is_at_tile_center(px: int, py: int) -> bool:
    cx, cy = px_to_cell(px, py)
    tx, ty = cell_center(cx, cy)
    return px == tx and py == ty

def collide(px: int, py: int, gx: int, gy: int, r: int = 1) -> bool:
    return abs(px - gx) <= r and abs(py - gy) <= r

# ======================
# RENDER MAZE
# ======================
def draw_maze_visual(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, MAZE_W, H), fill=WALL)

    for cy in range(ROWS):
        for cx in range(COLS):
            if is_wall(cx, cy) and not is_gate_tile(cx, cy):
                continue
            x, y = cell_center(cx, cy)
            d.rectangle((x - BAND_R, y - BAND_R, x + BAND_R, y + BAND_R), fill=BLACK)

            if cx + 1 < COLS and is_open_for_ghost(cx + 1, cy):
                xr, _ = cell_center(cx + 1, cy)
                d.rectangle((min(x, xr), y - BAND_R, max(x, xr), y + BAND_R), fill=BLACK)

            if cy + 1 < ROWS and is_open_for_ghost(cx, cy + 1):
                _, yd = cell_center(cx, cy + 1)
                d.rectangle((x - BAND_R, min(y, yd), x + BAND_R, max(y, yd)), fill=BLACK)

    for cx in GATE_XS:
        gx, gy = cell_center(cx, GATE_CY)
        d.rectangle((gx - BAND_R, gy - BAND_R, gx + BAND_R, gy + BAND_R), fill=WALL)

# ======================
# CENTERLINE MASKS
# ======================
def build_centerline_masks():
    pac = [[False] * W for _ in range(H)]
    ghost = [[False] * W for _ in range(H)]

    def setpix(mask, x, y):
        if 0 <= x < W and 0 <= y < H and x < MAZE_W:
            mask[y][x] = True

    for cy in range(ROWS):
        for cx in range(COLS):
            if is_wall(cx, cy) and not is_gate_tile(cx, cy):
                continue
            x, y = cell_center(cx, cy)

            if is_open_for_pac(cx, cy):
                setpix(pac, x, y)
            if is_open_for_ghost(cx, cy):
                setpix(ghost, x, y)

            if cx + 1 < COLS and is_open_for_ghost(cx + 1, cy):
                x2, _ = cell_center(cx + 1, cy)
                for xx in range(min(x, x2), max(x, x2) + 1):
                    if is_open_for_pac(cx, cy) and is_open_for_pac(cx + 1, cy):
                        setpix(pac, xx, y)
                    setpix(ghost, xx, y)

            if cy + 1 < ROWS and is_open_for_ghost(cx, cy + 1):
                _, y2 = cell_center(cx, cy + 1)
                for yy in range(min(y, y2), max(y, y2) + 1):
                    if is_open_for_pac(cx, cy) and is_open_for_pac(cx, cy + 1):
                        setpix(pac, x, yy)
                    setpix(ghost, x, yy)

    for cx in GATE_XS:
        x, y = cell_center(cx, GATE_CY)
        setpix(ghost, x, y)

    return pac, ghost

def can_stand(mask, x, y) -> bool:
    return 0 <= x < W and 0 <= y < H and mask[y][x]

# ======================
# TUNNEL WRAP
# ======================
TUNNEL_ROWS: Set[int] = set()
for cy in range(ROWS):
    if is_open_for_ghost(0, cy) and is_open_for_ghost(COLS - 1, cy):
        TUNNEL_ROWS.add(cy)

def tunnel_wrap_on_centerline(x: int, y: int, dirx: int) -> Tuple[int, int]:
    cx, cy = px_to_cell(x, y)
    if cy not in TUNNEL_ROWS:
        return x, y
    lx, ly = cell_center(0, cy)
    rx, ry = cell_center(COLS - 1, cy)
    y = ly
    if dirx < 0 and x <= lx:
        return rx, ry
    if dirx > 0 and x >= rx:
        return lx, ly
    return x, y

# ======================
# PELLETS
# ======================
def flood_reachable(start: Tuple[int, int]) -> Set[Tuple[int, int]]:
    sx, sy = start
    if not is_open_for_pac(sx, sy):
        return set()
    q = deque([(sx, sy)])
    seen = {(sx, sy)}
    while q:
        x, y = q.popleft()
        for dx, dy in DIR_LIST:
            nx, ny = x + dx, y + dy
            if (nx, ny) in seen:
                continue
            if in_bounds(nx, ny) and is_open_for_pac(nx, ny):
                seen.add((nx, ny))
                q.append((nx, ny))
    return seen

def pick_start_tile_pac() -> Tuple[int, int]:
    cx = COLS // 2
    for cy in range(ROWS - 2, 0, -1):
        if is_open_for_pac(cx, cy):
            return cx, cy
    return (1, 1)

def build_pellets(reachable: Set[Tuple[int, int]]) -> Tuple[Set[Tuple[int, int]], Set[Tuple[int, int]]]:
    pellets: Set[Tuple[int, int]] = set()
    power: Set[Tuple[int, int]] = set()
    for (cx, cy) in reachable:
        if in_house(cx, cy):
            continue
        pellets.add((cx, cy))
    candidates = [(1, 3), (COLS - 2, 3), (1, 23), (COLS - 2, 23)]
    for c in candidates:
        if c in reachable:
            pellets.discard(c)
            power.add(c)
    return pellets, power

# ======================
# HUD / FONT (3x5 only, vertical + bold)
# ======================
DIG2x4 = {
    "0": ["##","# #","# #","##"],
    "1": [" #","##"," #","##"],
    "2": ["##"," #","##","# "],
    "3": ["##"," #","##"," #"],
    "4": ["# #","##","  #","  #"],
    "5": ["##","# ","##"," #"],
    "6": ["##","# ","##","# #"],
    "7": ["##"," #"," #"," #"],
    "8": ["##","# #","##","# #"],
    "9": ["##","# #","##"," #"],
}

FONT_3x5 = {
"A":["###","# #","###","# #","# #"],
"B":["## ","# #","## ","# #","## "],
"C":["###","#  ","#  ","#  ","###"],
"D":["## ","# #","# #","# #","## "],
"E":["###","#  ","###","#  ","###"],
"F":["###","#  ","###","#  ","#  "],
"G":["###","#  ","# ##","# #","###"],
"H":["# #","# #","###","# #","# #"],
"I":["###"," # "," # "," # ","###"],
"J":["  #","  #","  #","# #","###"],
"K":["# #","# #","## ","# #","# #"],
"L":["#  ","#  ","#  ","#  ","###"],
"M":["# #","###","###","# #","# #"],
"N":["# #","###","###","###","# #"],
"O":["###","# #","# #","# #","###"],
"P":["## ","# #","## ","#  ","#  "],
"Q":["###","# #","# #","###","  #"],
"R":["## ","# #","## ","# #","# #"],
"S":["###","#  ","###","  #","###"],
"T":["###"," # "," # "," # "," # "],
"U":["# #","# #","# #","# #","###"],
"V":["# #","# #","# #","# #"," # "],
"W":["# #","# #","###","###","# #"],
"X":["# #"," # "," # "," # ","# #"],
"Y":["# #"," # "," # "," # "," # "],
"Z":["###","  #"," # ","#  ","###"],
":":["   "," # ","   "," # ","   "],
" ":[
"   ","   ","   ","   ","   "
],
"0":["###","# #","# #","# #","###"],
"1":[" ##","  #","  #","  #"," ###"],
"2":["###","  #","###","#  ","###"],
"3":["###","  #","###","  #","###"],
"4":["# #","# #","###","  #","  #"],
"5":["###","#  ","###","  #","###"],
"6":["###","#  ","###","# #","###"],
"7":["###","  #","  #","  #","  #"],
"8":["###","# #","###","# #","###"],
"9":["###","# #","###","  #","###"],
"V":["# #","# #","# #","# #"," # "],
}

def draw_text_3x5(d: ImageDraw.ImageDraw, x: int, y: int, s: str, color=TXT, spacing: int=1):
    for ch in s:
        pat = FONT_3x5.get(ch, FONT_3x5[" "])
        for yy, row in enumerate(pat):
            for xx, c in enumerate(row):
                if c == "#":
                    d.point((x+xx, y+yy), fill=color)
        x += 3 + spacing

def draw_text_3x5_v(d: ImageDraw.ImageDraw, x: int, y: int, s: str, color=TXT, spacing: int=1, bold: bool=True):
    """
    Draw 3x5 font vertically (upright) by rotating each glyph 90deg clockwise.
    Each char becomes 5x3 pixels, stacked downward.
    bold=True draws a second pass 1px to the right for better LED readability.
    """
    for ch in s:
        pat = FONT_3x5.get(ch, FONT_3x5[" "])

        # rotate 90° clockwise: new is 3 rows x 5 cols
        rot = []
        for ry in range(3):
            row = []
            for rx in range(5):
                row.append(pat[4 - rx][ry])
            rot.append(row)

        for yy in range(3):
            for xx in range(5):
                if rot[yy][xx] == "#":
                    d.point((x + xx, y + yy), fill=color)
                    if bold:
                        d.point((x + xx + 1, y + yy), fill=color)

        y += 3 + spacing

def draw_hud(d: ImageDraw.ImageDraw, score: int, hiscore: int, lives: int, tsec: int, level: int):
    d.rectangle((HUD_X0, 0, W-1, H-1), fill=HUD_BG)
    d.line((HUD_X0, 0, HUD_X0, H-1), fill=HUD_LINE)

    hud_w = W - HUD_X0
    # rotated glyph width ~5, bold adds +1 -> 6
    text_w = 6
    x = HUD_X0 + max(0, (hud_w - text_w) // 2)

    # SCORE (big vertical digits)
    draw_text_3x5_v(d, x, 0, f"{score%100000:05d}", color=HUD_FG, spacing=1, bold=True)

    # HISCORE
    draw_text_3x5_v(d, x, 22, f"{hiscore%100000:05d}", color=HUD_DIM, spacing=1, bold=True)

    # LIVES + LEVEL
    draw_text_3x5_v(d, x, 44, f"LV{max(0,lives)%10}", color=HUD_FG, spacing=0, bold=True)
    draw_text_3x5_v(d, x, 54, f"L{min(9,max(1,level))}", color=HUD_DIM, spacing=0, bold=True)

    # TIME (seconds, 5 digits)
    draw_text_3x5_v(d, x, 60, f"{max(0,tsec)%100000:05d}", color=HUD_DIM, spacing=1, bold=True)

# ======================
# SPRITES
# ======================
def draw_pellets_img(d: ImageDraw.ImageDraw, pellets: Set[Tuple[int, int]]):
    for cx, cy in pellets:
        x, y = cell_center(cx, cy)
        d.point((x, y), fill=PEL)

def draw_power_img(d: ImageDraw.ImageDraw, power: Set[Tuple[int, int]], blink_on: bool):
    if not blink_on:
        return
    for cx, cy in power:
        x, y = cell_center(cx, cy)
        d.point((x, y), fill=PWR)
        d.point((x + 1, y), fill=PWR)
        d.point((x, y + 1), fill=PWR)
        d.point((x + 1, y + 1), fill=PWR)

def draw_pacman(d: ImageDraw.ImageDraw, x: int, y: int, mouth_open: bool, direction: Tuple[int, int]):
    r = 2
    d.ellipse((x - r, y - r, x + r, y + r), fill=PAC)
    if not mouth_open:
        return
    dx, dy = direction
    if dx == 1:
        for p in [(x+1,y),(x+2,y-1),(x+2,y),(x+2,y+1)]: d.point(p, fill=BLACK)
    elif dx == -1:
        for p in [(x-1,y),(x-2,y-1),(x-2,y),(x-2,y+1)]: d.point(p, fill=BLACK)
    elif dy == 1:
        for p in [(x,y+1),(x-1,y+2),(x,y+2),(x+1,y+2)]: d.point(p, fill=BLACK)
    elif dy == -1:
        for p in [(x,y-1),(x-1,y-2),(x,y-2),(x+1,y-2)]: d.point(p, fill=BLACK)

def draw_ghost(d: ImageDraw.ImageDraw, x: int, y: int, col, frightened: bool):
    body = FRIGHT if frightened else col
    pattern = ["01110","11111","11111","11111","10101"]
    for ry, row in enumerate(pattern):
        py = y - 2 + ry
        for rx, c in enumerate(row):
            if c == "1":
                d.point((x - 2 + rx, py), fill=body)
    d.point((x - 1, y - 1), fill=BLACK)
    d.point((x + 1, y - 1), fill=BLACK)

# ======================
# MENU
# ======================
MENU_ITEMS = ["RESUME", "RESTART", "EXIT"]

def draw_menu_overlay(d: ImageDraw.ImageDraw, idx: int):
    x0, y0, x1, y1 = 18, 12, 100, 46
    d.rectangle((x0, y0, x1, y1), outline=DIM, fill=BLACK)

    base_y = y0 + 6
    for i, item in enumerate(MENU_ITEMS):
        yy = base_y + i*10
        if i == idx:
            d.rectangle((x0+4, yy-2, x1-4, yy+7), outline=TXT, fill=BLACK)
        draw_text_3x5(d, x0+10, yy, item, TXT if i==idx else DIM)

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

# ======================
# GHOST AI (intersection-based)
# ======================
def ghost_possible_dirs(mask, x: int, y: int) -> List[Tuple[int,int]]:
    out = []
    for dx, dy in DIR_LIST:
        if can_stand(mask, x + dx, y + dy):
            out.append((dx, dy))
    return out

def ghost_choose_dir(gx: int, gy: int, gdir: Tuple[int,int], target_px: Tuple[int,int], frightened: bool, mask) -> Tuple[int,int]:
    dirs = ghost_possible_dirs(mask, gx, gy)
    if not dirs:
        return (0, 0)
    rev = (-gdir[0], -gdir[1])
    choices = [d for d in dirs if d != rev] or dirs
    tx, ty = target_px

    def score_dir(d):
        nx = gx + d[0]
        ny = gy + d[1]
        md = abs(tx - nx) + abs(ty - ny)
        return -md if frightened else md

    choices.sort(key=score_dir)
    if len(choices) > 1 and random.random() < (0.25 if not frightened else 0.10):
        return random.choice(choices[:2])
    return choices[0]

# ======================
# GAME STATE
# ======================
def reset_level(state: Dict) -> None:
    ps = pick_start_tile_pac()
    reachable = flood_reachable(ps)
    pellets, power = build_pellets(reachable)
    state["pellets"] = pellets
    state["power"] = power

    state["px"], state["py"] = cell_center(*ps)
    state["dir"] = DIRS["L"]
    state["want"] = DIRS["L"]
    state["p_acc"] = 0.0

    for name, _ in GHOSTS:
        hx, hy = GHOST_HOME_TILES[name]
        gx, gy = cell_center(hx, hy)
        g = state["ghosts"][name]
        g["x"], g["y"] = gx, gy
        g["dir"] = DIRS["U"]
        g["fright"] = 0.0
        g["home"] = (hx, hy)
        state["g_acc"][name] = 0.0

    state["menu"] = False
    state["menu_idx"] = 0
    state["fright_chain"] = 0
    state["waiting"] = True  # READY until L/R

def new_game(hiscore: int) -> Dict:
    ghosts: Dict[str, Dict] = {}
    for name, col in GHOSTS:
        hx, hy = GHOST_HOME_TILES[name]
        gx, gy = cell_center(hx, hy)
        ghosts[name] = {"x": gx, "y": gy, "dir": DIRS["U"], "col": col, "fright": 0.0, "home": (hx, hy)}

    state = {
        "score": 0,
        "hiscore": hiscore,
        "lives": 3,
        "level": 1,

        "pellets": set(),
        "power": set(),

        "px": 0, "py": 0,
        "dir": DIRS["L"],
        "want": DIRS["L"],

        "ghosts": ghosts,

        "dead": False,
        "dead_t": 0.0,

        "start_time": time.time(),
        "time_offset": 0.0,

        "mouth_open": True,
        "mouth_timer": 0.0,

        "power_on": True,
        "power_timer": 0.0,

        "p_acc": 0.0,
        "g_acc": {name: 0.0 for name, _ in GHOSTS},

        "menu": False,
        "menu_idx": 0,
        "fright_chain": 0,
        "waiting": True,
        "last_menu_toggle": 0.0,
    }
    reset_level(state)
    return state

# ======================
# MAIN
# ======================
def main():
    matrix = make_matrix()
    off = matrix.CreateFrameCanvas()
    js = Joystick()
    js.start()

    maze_img = Image.new("RGB", (W, H), BLACK)
    draw_maze_visual(maze_img)

    pac_mask, ghost_mask = build_centerline_masks()
    frame = Image.new("RGB", (W, H), BLACK)

    hiscore = load_hiscore()
    game = new_game(hiscore)

    PAC_SPEED_BASE = 24.0
    dt = 0.02
    last = time.time()

    MOUTH_PERIOD = 0.80
    POWER_BLINK_PERIOD = 1.30
    FRIGHT_DURATION = 10.5

    running = True
    def handle_exit(signum, frame_):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    try:
        while running:
            ev = js.pop()
            now = time.time()

            # MENU TOGGLE
            raw_toggle = ev.start or ev.select or (MENU_TOGGLE_FALLBACK_Y and ev.y)
            if raw_toggle and (now - game["last_menu_toggle"] > MENU_DEBOUNCE_SEC):
                game["last_menu_toggle"] = now
                game["menu"] = not game["menu"]
                if game["menu"]:
                    game["menu_idx"] = 0

            # WAITING TO START: only LEFT/RIGHT starts
            if game["waiting"] and not game["menu"] and not game["dead"]:
                if ev.left:
                    game["waiting"] = False
                    game["want"] = DIRS["L"]
                    game["dir"]  = DIRS["L"]
                elif ev.right:
                    game["waiting"] = False
                    game["want"] = DIRS["R"]
                    game["dir"]  = DIRS["R"]

            # MENU
            if game["menu"]:
                if ev.up:
                    game["menu_idx"] = (game["menu_idx"] - 1) % len(MENU_ITEMS)
                if ev.down:
                    game["menu_idx"] = (game["menu_idx"] + 1) % len(MENU_ITEMS)
                if ev.b or ev.y:
                    game["menu"] = False
                if ev.a or ev.x:
                    choice = MENU_ITEMS[game["menu_idx"]]
                    if choice == "RESUME":
                        game["menu"] = False
                    elif choice == "RESTART":
                        if game["score"] > game["hiscore"]:
                            game["hiscore"] = game["score"]
                            save_hiscore(game["hiscore"])
                        game = new_game(game["hiscore"])
                    elif choice == "EXIT":
                        if game["score"] > game["hiscore"]:
                            game["hiscore"] = game["score"]
                            save_hiscore(game["hiscore"])
                        exec_launcher_or_exit(matrix)

            # DPAD intent only if playing
            if (not game["waiting"]) and (not game["menu"]) and (not game["dead"]):
                if ev.left:  game["want"] = DIRS["L"]
                if ev.right: game["want"] = DIRS["R"]
                if ev.up:    game["want"] = DIRS["U"]
                if ev.down:  game["want"] = DIRS["D"]

            # timing
            now = time.time()
            if now - last < dt:
                time.sleep(0.001)
                continue
            elapsed = now - last
            last = now

            paused = game["waiting"] or game["menu"] or game["dead"]

            if paused:
                game["time_offset"] += elapsed

            # death handling
            if game["dead"]:
                game["dead_t"] += elapsed
                if game["dead_t"] >= 1.0:
                    game["dead_t"] = 0.0
                    game["dead"] = False
                    game["lives"] -= 1

                    if game["lives"] <= 0:
                        if game["score"] > game["hiscore"]:
                            game["hiscore"] = game["score"]
                            save_hiscore(game["hiscore"])
                        game = new_game(game["hiscore"])
                    else:
                        ps = pick_start_tile_pac()
                        game["px"], game["py"] = cell_center(*ps)
                        game["dir"] = DIRS["L"]
                        game["want"] = DIRS["L"]
                        game["p_acc"] = 0.0

                        for name, gg in game["ghosts"].items():
                            hx, hy = gg["home"]
                            gg["x"], gg["y"] = cell_center(hx, hy)
                            gg["dir"] = DIRS["U"]
                            gg["fright"] = 0.0
                            game["g_acc"][name] = 0.0

                        game["fright_chain"] = 0
                        game["waiting"] = True

            # animate only when not paused
            if not paused:
                game["mouth_timer"] += elapsed
                if game["mouth_timer"] >= MOUTH_PERIOD:
                    game["mouth_timer"] -= MOUTH_PERIOD
                    game["mouth_open"] = not game["mouth_open"]

                game["power_timer"] += elapsed
                if game["power_timer"] >= POWER_BLINK_PERIOD:
                    game["power_timer"] -= POWER_BLINK_PERIOD
                    game["power_on"] = not game["power_on"]

            # speed per level: ghosts +10% per level up to +100% (2x)
            lvl = game["level"]
            ghost_factor = min(2.0, 1.0 + 0.1 * (lvl - 1))
            PAC_SPEED = PAC_SPEED_BASE
            GHOST_SPEED = PAC_SPEED_BASE * ghost_factor

            # gameplay
            if not paused:
                wx, wy = game["want"]
                if can_stand(pac_mask, game["px"] + wx, game["py"] + wy):
                    game["dir"] = game["want"]

                game["p_acc"] += PAC_SPEED * elapsed
                while game["p_acc"] >= 1.0:
                    game["p_acc"] -= 1.0
                    dx, dy = game["dir"]
                    if dx != 0:
                        game["px"], game["py"] = tunnel_wrap_on_centerline(game["px"], game["py"], dx)
                    nx, ny = game["px"] + dx, game["py"] + dy
                    if can_stand(pac_mask, nx, ny):
                        game["px"], game["py"] = nx, ny

                cx, cy = px_to_cell(game["px"], game["py"])
                txc, tyc = cell_center(cx, cy)
                if game["px"] == txc and game["py"] == tyc:
                    if (cx, cy) in game["pellets"]:
                        game["pellets"].remove((cx, cy))
                        game["score"] += 10
                    if (cx, cy) in game["power"]:
                        game["power"].remove((cx, cy))
                        game["score"] += 50
                        for gg in game["ghosts"].values():
                            gg["fright"] = FRIGHT_DURATION
                        game["fright_chain"] = 0

                for gg in game["ghosts"].values():
                    if gg["fright"] > 0.0:
                        gg["fright"] = max(0.0, gg["fright"] - elapsed)

                pac_px = (game["px"], game["py"])

                for name, g in game["ghosts"].items():
                    frightened = g["fright"] > 0.0
                    gx, gy = g["x"], g["y"]

                    if is_at_tile_center(gx, gy):
                        gcx, gcy = px_to_cell(gx, gy)
                        if in_house(gcx, gcy) and not frightened:
                            if can_stand(ghost_mask, gx, gy - 1):
                                g["dir"] = DIRS["U"]
                            else:
                                ex, ey = cell_center(*HOUSE_EXIT_TILE)
                                g["dir"] = ghost_choose_dir(gx, gy, g["dir"], (ex, ey), False, ghost_mask)
                        else:
                            g["dir"] = ghost_choose_dir(gx, gy, g["dir"], pac_px, frightened, ghost_mask)

                    game["g_acc"][name] += GHOST_SPEED * elapsed
                    while game["g_acc"][name] >= 1.0:
                        game["g_acc"][name] -= 1.0
                        ddx, ddy = g["dir"]
                        if ddx != 0:
                            g["x"], g["y"] = tunnel_wrap_on_centerline(g["x"], g["y"], ddx)
                        nx, ny = g["x"] + ddx, g["y"] + ddy
                        if can_stand(ghost_mask, nx, ny):
                            g["x"], g["y"] = nx, ny
                        else:
                            break

                    if collide(game["px"], game["py"], g["x"], g["y"], r=1):
                        if frightened:
                            game["fright_chain"] += 1
                            game["score"] += 200 * (2 ** max(0, game["fright_chain"] - 1))
                            hx, hy = g["home"]
                            g["x"], g["y"] = cell_center(hx, hy)
                            g["dir"] = DIRS["U"]
                            g["fright"] = 0.0
                            game["g_acc"][name] = 0.0
                        else:
                            game["dead"] = True
                            game["dead_t"] = 0.0
                            break

                if game["score"] > game["hiscore"]:
                    game["hiscore"] = game["score"]

                # NEXT LEVEL (FIX): lives reset to 3, do NOT force level=3
                if len(game["pellets"]) == 0 and len(game["power"]) == 0:
                    game["level"] += 1
                    game["lives"] = 3
                    reset_level(game)

            tsec = int((time.time() - game["start_time"]) - game["time_offset"])

            # render
            d = ImageDraw.Draw(frame)
            frame.paste(maze_img, (0, 0))

            draw_pellets_img(d, game["pellets"])
            draw_power_img(d, game["power"], game["power_on"])

            draw_pacman(d, game["px"], game["py"], game["mouth_open"], game["dir"])
            for g in game["ghosts"].values():
                draw_ghost(d, g["x"], g["y"], g["col"], g["fright"] > 0.0)

            draw_hud(d, game["score"], game["hiscore"], game["lives"], tsec, game["level"])

            if game["waiting"] and not game["menu"]:
                d.rectangle((30, 26, 98, 38), fill=BLACK, outline=DIM)
                draw_text_3x5(d, 34, 29, "READY", TXT)

            if game["menu"]:
                draw_menu_overlay(d, game["menu_idx"])

            if game["dead"]:
                d.rectangle((28, 26, 100, 38), fill=BLACK, outline=DIM)
                draw_text_3x5(d, 38, 29, "OUCH", TXT)

            off.SetImage(frame, 0, 0)
            off = matrix.SwapOnVSync(off)

    finally:
        try:
            save_hiscore(game["hiscore"])
        except Exception:
            pass
        js.stop()
        try:
            matrix.Clear()
        except Exception:
            pass

if __name__ == "__main__":
    main()
