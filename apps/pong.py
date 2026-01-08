#!/usr/bin/env python3
# PONG 128x64 for rpi-rgb-led-matrix
# INPUT: /dev/input/js0 (same mapping style as your launcher/snake)
# - START toggles menu
# - SELECT or B or Y exits menu
# - A or X confirms selection
# - Menu single-step navigation (no skipping when holding dpad)
# - Start game with UP/DOWN (not START) + release-to-start after score/win
# - Difficulty (EASY/MEDIUM/HARD) changes with LEFT/RIGHT on DIFFICULTY row
# - Crisp text, nicer font

import os
import sys
import time
import math
import random
import struct
import threading
from dataclasses import dataclass
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont
from rgbmatrix import RGBMatrix, RGBMatrixOptions

# ===== DISPLAY CONFIG =====
PIXEL_MAPPER = "U-mapper;StackToRow:Z;Rotate:180"
W, H = 128, 64

# ===== GAME SETTINGS =====
PADDLE_W = 3
PADDLE_H = 14
BALL_R = 2

P1_X = 6
P2_X = W - 6 - PADDLE_W

SCORE_TO_WIN = 7

# ===== PHYSICS =====
PADDLE_SPEED = 120.0  # px/s
BALL_SPEED_START = 120.0
BALL_SPEED_MAX = 150.0
BALL_VY_MAX = 220.0
SPIN_P1 = 110.0

# ===== DIFFICULTY =====
DIFFS = ["EASY", "MEDIUM", "HARD"]
DIFF_PARAMS = {
    "EASY":   dict(AI_STRENGTH=0.55, AI_REACTION_S=0.16, AI_DEADZONE=2.4, AI_MAX_SPEED_MULT=0.75, SPIN_P2=70.0),
    "MEDIUM": dict(AI_STRENGTH=0.75, AI_REACTION_S=0.10, AI_DEADZONE=1.4, AI_MAX_SPEED_MULT=0.85, SPIN_P2=90.0),
    "HARD":   dict(AI_STRENGTH=0.95, AI_REACTION_S=0.06, AI_DEADZONE=0.8, AI_MAX_SPEED_MULT=0.95, SPIN_P2=110.0),
}

# ===== JS0 INPUT =====
JS_PATH = "/dev/input/js0"
DEADZONE = 12000
AXIS_X = 0
AXIS_Y = 1

# Your mapping (same as pacman/snake you posted):
BTN_X = 0
BTN_A = 1
BTN_B = 2
BTN_Y = 3
BTN_SELECT = 8
BTN_START = 9

# ===== COLORS =====
BG = (0, 0, 0)
FG = (240, 240, 240)
DIM = (120, 120, 120)
ACC = (80, 180, 255)
BOX = (140, 140, 140)

def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

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

    opts.disable_hardware_pulsing = True
    opts.gpio_slowdown = 4
    opts.pwm_bits = 11
    opts.pwm_lsb_nanoseconds = 300
    opts.pwm_dither_bits = 0

    try:
        opts.brightness = int(os.getenv("MATRIX_BRIGHTNESS", "60"))
    except Exception:
        opts.brightness = 60

    opts.drop_privileges = False
    return RGBMatrix(options=opts)

# ===== FONT + CRISP TEXT =====
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

def text_w(d: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        return int(d.textlength(text, font=font))
    except Exception:
        bb = d.textbbox((0, 0), text, font=font)
        return int(bb[2] - bb[0])

def text_h(d: ImageDraw.ImageDraw, text: str, font) -> int:
    bb = d.textbbox((0, 0), text, font=font)
    return int(bb[3] - bb[1])

def truncate_to_fit(d: ImageDraw.ImageDraw, s: str, font, max_w: int) -> str:
    if text_w(d, s, font) <= max_w:
        return s
    ell = "…"
    if text_w(d, ell, font) > max_w:
        return ""
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi) // 2
        t = s[:mid].rstrip() + ell
        if text_w(d, t, font) <= max_w:
            lo = mid + 1
        else:
            hi = mid
    return s[:max(0, lo - 1)].rstrip() + ell

def draw_text_crisp(img_rgb: Image.Image, pos, text: str, font, fill=FG, threshold: int = 80):
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

def draw_centered_crisp(img_rgb: Image.Image, y: int, text: str, font, fill=FG):
    d = ImageDraw.Draw(img_rgb)
    tw = text_w(d, text, font)
    x = (W - tw) // 2
    draw_text_crisp(img_rgb, (x, y), text, font, fill=fill)

def dim_overlay(img_rgb: Image.Image, alpha: int = 155):
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, alpha))
    base = img_rgb.convert("RGBA")
    base.alpha_composite(overlay)
    img_rgb.paste(base.convert("RGB"))

# ===== RETURN TO LAUNCHER =====
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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
    raise SystemExit("launcher.py not found")

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

class Js0Reader(threading.Thread):
    """
    /dev/input/js0 reader:
    - keeps held axis state (so paddle movement works)
    - provides edge flags for directions (for menu single-step + release-to-start)
    """
    def __init__(self, js_path=JS_PATH):
        super().__init__(daemon=True)
        self.js_path = js_path
        self._lock = threading.Lock()
        self._stop = False

        self._held_x = 0  # -1 left, 0 neutral, +1 right
        self._held_y = 0  # -1 up,   0 neutral, +1 down

        self._edge_up = False
        self._edge_down = False
        self._edge_left = False
        self._edge_right = False

        self._btn = Controls()

    def stop(self):
        self._stop = True

    def pop(self):
        """
        Returns:
          held Controls (up/down/left/right as held),
          edge tuple (up_edge, down_edge, left_edge, right_edge)
        Buttons are one-shot.
        """
        with self._lock:
            c = self._btn
            self._btn = Controls()

            # held directions
            held = Controls(
                up=(self._held_y == -1),
                down=(self._held_y == +1),
                left=(self._held_x == -1),
                right=(self._held_x == +1),
                start=c.start,
                select=c.select,
                a=c.a, b=c.b, x=c.x, y=c.y,
            )

            edges = (self._edge_up, self._edge_down, self._edge_left, self._edge_right)
            self._edge_up = self._edge_down = self._edge_left = self._edge_right = False

            return held, edges

    def _push_btn(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self._btn, k, getattr(self._btn, k) or v)

    def run(self):
        if not os.path.exists(self.js_path):
            print("No joystick:", self.js_path)
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

                        if num == AXIS_X:
                            new = 0
                            if v < -DEADZONE:
                                new = -1
                            elif v > DEADZONE:
                                new = +1
                            with self._lock:
                                if new != self._held_x:
                                    self._held_x = new
                                    if new == -1:
                                        self._edge_left = True
                                    elif new == +1:
                                        self._edge_right = True

                        elif num == AXIS_Y:
                            new = 0
                            if v < -DEADZONE:
                                new = -1
                            elif v > DEADZONE:
                                new = +1
                            with self._lock:
                                if new != self._held_y:
                                    self._held_y = new
                                    if new == -1:
                                        self._edge_up = True
                                    elif new == +1:
                                        self._edge_down = True

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

        except Exception as e:
            print("JoystickReader error:", e)

# ===== DRAW HELPERS =====
def draw_center_line(d: ImageDraw.ImageDraw):
    for y in range(0, H, 6):
        d.rectangle((W // 2 - 1, y, W // 2, y + 2), fill=(40, 40, 40))

def reset_ball():
    x = W / 2.0
    y = H / 2.0
    angle = random.uniform(-0.65, 0.65)
    speed = BALL_SPEED_START
    vx = speed if random.random() < 0.5 else -speed
    vy = math.sin(angle) * (0.85 * speed)
    return x, y, vx, vy

class Overlay:
    def __init__(self, font):
        self.font = font
        self.msg = ""
        self.until = 0.0

    def show(self, msg: str, seconds: float):
        self.msg = msg
        self.until = time.time() + seconds

    def active(self):
        return time.time() < self.until and bool(self.msg)

    def draw(self, img: Image.Image):
        if not self.active():
            return
        d = ImageDraw.Draw(img)
        bb = d.textbbox((0, 0), self.msg, font=self.font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        draw_text_crisp(img, ((W - tw) // 2, (H - th) // 2), self.msg, self.font, fill=FG)

# ===== MENU =====
MENU_ITEMS = ["RESUME", "RESTART", "DIFFICULTY", "EXIT"]

def draw_menu(img: Image.Image, idx: int, diff_name: str, font):
    d = ImageDraw.Draw(img)

    row_h = max(12, text_h(d, "Ag", font) + 4)
    pad_x = 10
    pad_y = 8

    lines = []
    for it in MENU_ITEMS:
        if it == "DIFFICULTY":
            lines.append(f"DIFF: {diff_name}")
        else:
            lines.append(it)

    box_h = pad_y * 2 + row_h * len(lines)
    box_w = min(112, W - 16)

    x0 = (W - box_w) // 2
    y0 = (H - box_h) // 2
    x1 = x0 + box_w
    y1 = y0 + box_h

    d.rectangle((x0, y0, x1, y1), fill=(0, 0, 0), outline=BOX)

    for i, txt in enumerate(lines):
        yy = y0 + pad_y + i * row_h
        max_text_w = box_w - 2 * pad_x - 10
        txt = truncate_to_fit(d, txt, font, max_text_w)

        if i == idx:
            sel_y0 = yy - 1
            sel_y1 = yy + row_h - 3
            d.rectangle((x0 + 6, sel_y0, x1 - 6, sel_y1), outline=FG, fill=(0, 0, 0))

        draw_text_crisp(img, (x0 + pad_x, yy), txt, font, fill=(FG if i == idx else DIM), threshold=85)

# ===== MAIN =====
def main():
    matrix = make_matrix()

    font_score = load_font(12)
    font_menu = load_font(11)
    font_hint = load_font(10)

    js = Js0Reader(JS_PATH)
    js.start()

    # positions
    p1_y = H / 2 - PADDLE_H / 2
    p2_y = H / 2 - PADDLE_H / 2

    ball_x, ball_y, ball_vx, ball_vy = reset_ball()
    score1 = 0
    score2 = 0

    paused = True
    menu = False
    menu_idx = 0

    # release-to-start gate
    start_armed = False

    # difficulty index
    diff_i = 1  # MEDIUM

    # AI state
    ai_last_react = 0.0
    ai_target_y = p2_y

    # reusable image
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    overlay = Overlay(font_score)

    # timing
    target_fps = 60.0
    frame_dt = 1.0 / target_fps
    last = time.perf_counter()

    # start menu debounce
    MENU_DEBOUNCE = 0.28
    last_menu_toggle = 0.0

    def apply_diff():
        return DIFF_PARAMS[DIFFS[diff_i]]

    def begin_round():
        nonlocal paused, ball_x, ball_y, ball_vx, ball_vy
        paused = False
        ball_x, ball_y, ball_vx, ball_vy = reset_ball()

    def pause_round():
        nonlocal paused, start_armed
        paused = True
        start_armed = False

    try:
        while True:
            now = time.perf_counter()
            dt = clamp(now - last, 0.0, 0.05)
            last = now

            held, edges = js.pop()
            up_edge, down_edge, left_edge, right_edge = edges

            # START toggles menu
            if held.start:
                t = time.time()
                if (t - last_menu_toggle) > MENU_DEBOUNCE:
                    last_menu_toggle = t
                    menu = not menu
                    if menu:
                        menu_idx = 0

            # Exit menu with SELECT or B or Y
            if menu and (held.select or held.b or held.y):
                menu = False

            # MENU MODE
            if menu:
                if up_edge:
                    menu_idx = (menu_idx - 1) % len(MENU_ITEMS)
                elif down_edge:
                    menu_idx = (menu_idx + 1) % len(MENU_ITEMS)

                if MENU_ITEMS[menu_idx] == "DIFFICULTY":
                    if left_edge:
                        diff_i = (diff_i - 1) % len(DIFFS)
                    elif right_edge:
                        diff_i = (diff_i + 1) % len(DIFFS)

                if held.a or held.x:
                    choice = MENU_ITEMS[menu_idx]
                    if choice == "RESUME":
                        menu = False
                    elif choice == "RESTART":
                        score1 = score2 = 0
                        pause_round()
                        ball_x, ball_y, ball_vx, ball_vy = reset_ball()
                        menu = False
                    elif choice == "EXIT":
                        exec_launcher_or_exit(matrix)

            # START GAME WITH UP/DOWN (not START), release-to-start
            if (not menu) and paused and (not overlay.active()):
                if not held.up and not held.down:
                    start_armed = True
                if start_armed and (up_edge or down_edge):
                    begin_round()

            # difficulty params
            params = apply_diff()
            AI_STRENGTH = params["AI_STRENGTH"]
            AI_REACTION_S = params["AI_REACTION_S"]
            AI_DEADZONE = params["AI_DEADZONE"]
            AI_MAX_SPEED_MULT = params["AI_MAX_SPEED_MULT"]
            SPIN_P2 = params["SPIN_P2"]

            # P1 movement
            if (not paused) and (not menu):
                if held.up and not held.down:
                    p1_y -= PADDLE_SPEED * dt
                elif held.down and not held.up:
                    p1_y += PADDLE_SPEED * dt
            p1_y = clamp(p1_y, 0.0, H - PADDLE_H)

            # AI movement
            if (not paused) and (not menu):
                t = time.time()
                if t - ai_last_react >= AI_REACTION_S:
                    ai_last_react = t
                    ai_target_y = ball_y - PADDLE_H / 2

                diff = ai_target_y - p2_y
                if abs(diff) > AI_DEADZONE:
                    direction = 1.0 if diff > 0 else -1.0
                    p2_y += direction * (PADDLE_SPEED * AI_MAX_SPEED_MULT) * dt * AI_STRENGTH
            p2_y = clamp(p2_y, 0.0, H - PADDLE_H)

            # Ball
            if (not paused) and (not menu):
                ball_x += ball_vx * dt
                ball_y += ball_vy * dt

                # top/bottom
                if ball_y <= BALL_R:
                    ball_y = BALL_R
                    ball_vy *= -1.0
                elif ball_y >= (H - 1 - BALL_R):
                    ball_y = (H - 1 - BALL_R)
                    ball_vy *= -1.0

                # P1 collision
                if ball_vx < 0 and (ball_x - BALL_R) <= (P1_X + PADDLE_W):
                    if (p1_y - 1) <= ball_y <= (p1_y + PADDLE_H + 1):
                        ball_x = P1_X + PADDLE_W + BALL_R
                        ball_vx *= -1.0
                        rel = (ball_y - (p1_y + PADDLE_H / 2)) / (PADDLE_H / 2)
                        ball_vy += rel * SPIN_P1

                # P2 collision
                if ball_vx > 0 and (ball_x + BALL_R) >= P2_X:
                    if (p2_y - 1) <= ball_y <= (p2_y + PADDLE_H + 1):
                        ball_x = P2_X - BALL_R
                        ball_vx *= -1.0
                        rel = (ball_y - (p2_y + PADDLE_H / 2)) / (PADDLE_H / 2)
                        ball_vy += rel * SPIN_P2

                ball_vy = clamp(ball_vy, -BALL_VY_MAX, BALL_VY_MAX)

                sp = math.hypot(ball_vx, ball_vy)
                if sp > BALL_SPEED_MAX:
                    scale = BALL_SPEED_MAX / max(sp, 1e-6)
                    ball_vx *= scale
                    ball_vy *= scale

                # scoring -> pause + overlay + release-to-start again
                if ball_x < -10:
                    score2 += 1
                    pause_round()
                    overlay.show(f"{score1} : {score2}", 0.9)
                    ball_x, ball_y, ball_vx, ball_vy = reset_ball()
                elif ball_x > W + 10:
                    score1 += 1
                    pause_round()
                    overlay.show(f"{score1} : {score2}", 0.9)
                    ball_x, ball_y, ball_vx, ball_vy = reset_ball()

                if score1 >= SCORE_TO_WIN or score2 >= SCORE_TO_WIN:
                    winner = "YOU WIN" if score1 > score2 else "AI WINS"
                    overlay.show(winner, 1.3)
                    score1 = score2 = 0
                    pause_round()

            # RENDER
            d.rectangle((0, 0, W, H), fill=BG)
            draw_center_line(d)

            # paddles
            d.rectangle((P1_X, int(p1_y), P1_X + PADDLE_W, int(p1_y) + PADDLE_H), fill=(200, 200, 200))
            d.rectangle((P2_X, int(p2_y), P2_X + PADDLE_W, int(p2_y) + PADDLE_H), fill=(200, 200, 200))

            # ball
            d.ellipse((int(ball_x - BALL_R), int(ball_y - BALL_R), int(ball_x + BALL_R), int(ball_y + BALL_R)), fill=(255, 255, 255))

            # score
            draw_text_crisp(img, (W // 2 - 22, 2), str(score1), font_score, fill=DIM, threshold=85)
            draw_text_crisp(img, (W // 2 + 14, 2), str(score2), font_score, fill=DIM, threshold=85)

            if paused and (not overlay.active()) and (not menu):
                draw_centered_crisp(img, H - 14, "UP/DOWN TO PLAY", font_hint, fill=ACC)

            overlay.draw(img)

            if menu:
                dim_overlay(img, alpha=155)
                draw_menu(img, menu_idx, DIFFS[diff_i], font_menu)

            matrix.SetImage(img, 0, 0)

            # frame pacing
            work = time.perf_counter() - now
            sleep_for = frame_dt - work
            if sleep_for > 0:
                time.sleep(sleep_for)

    finally:
        js.stop()
        try:
            matrix.Clear()
        except Exception:
            pass

if __name__ == "__main__":
    main()
