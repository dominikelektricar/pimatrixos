#!/usr/bin/env python3
# Dashboard (128x64) - 2 pages + overlay menu (START/SELECT)
# Page 1: Time/Date + Weather (animated icon) + Chance of rain
# Page 2: System info (CPU/RAM/NET/Storage/Uptime)
#
# Controls:
# - DPAD LEFT/RIGHT: change page
# - START or SELECT: open menu
# - Menu: UP/DOWN select, OK confirm/toggle/edit, BACK cancel/close
#
# Config: /home/pi/led/config.json
#   postcode: "10000"
#   dash_autoscroll: true/false
#   dash_autodim: true/false
#
# Weather source: wttr.in JSON (no API key)

import os
import time
import json
import math
import struct
import threading
import subprocess
import errno
import fcntl
from typing import Optional, Dict, Any, Tuple, List

import urllib.request
from PIL import Image, ImageDraw, ImageFont
from rgbmatrix import RGBMatrix, RGBMatrixOptions

# =========================
# MATRIX CONFIG (match your launcher)
# =========================
PIXEL_MAPPER = "U-mapper;StackToRow:Z;Rotate:180"
W, H = 128, 64

CONFIG_PATH = "/home/pi/led/config.json"
LAUNCHER_PATH = "/home/pi/led/launcher.py"

# Weather
WEATHER_REFRESH_SEC = 15 * 60  # 15 min cache
WEATHER_TIMEOUT_SEC = 4

# Menu timings
NAV_REPEAT = 0.14

# Auto scroll
AUTO_SCROLL_SEC = 7.0

# Auto dim schedule (local time)
DIM_NIGHT_START = 22  # 22:00
DIM_NIGHT_END = 7     # 07:00
DIM_FACTOR_NIGHT = 0.35  # multiply brightness

# Gamepad mapping (per your setup)
BTN_OK = {0, 1}       # A/X
BTN_BACK = {2, 3}     # B/Y
BTN_SELECT = 8
BTN_START = 9

# Joystick device
JS_DEV = "/dev/input/js0"
FMT = "IhBB"
SZ = struct.calcsize(FMT)


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


def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: Dict[str, Any]):
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

    # stability knobs (match launcher)
    opts.disable_hardware_pulsing = True
    opts.gpio_slowdown = 2
    opts.pwm_bits = 8
    opts.pwm_lsb_nanoseconds = 300
    opts.pwm_dither_bits = 0

    opts.brightness = int(brightness)
    opts.drop_privileges = False
    return RGBMatrix(options=opts)


def text_width(d: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        return int(d.textlength(text, font=font))
    except Exception:
        bbox = d.textbbox((0, 0), text, font=font)
        return int(bbox[2] - bbox[0])


def draw_text_crisp(img_rgb: Image.Image, pos, text: str, font, fill=(255, 255, 255), threshold: int = 60):
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


# =========================
# INPUT (non-blocking js0)
# =========================
class Joy(threading.Thread):
    def __init__(self, path=JS_DEV):
        super().__init__(daemon=True)
        self.path = path
        self._stop = False
        self._lock = threading.Lock()
        self._btn = {}  # num -> pressed
        self._axis = {0: 0, 1: 0}  # x,y state -1/0/+1 edge detected
        self._events = {
            "up": False, "down": False, "left": False, "right": False,
            "ok": False, "back": False, "start": False, "select": False
        }
        self.DEADZONE = 12000

    def stop(self):
        self._stop = True

    def pop_events(self) -> Dict[str, bool]:
        with self._lock:
            ev = dict(self._events)
            for k in self._events:
                self._events[k] = False
            return ev

    def _push(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                self._events[k] = self._events[k] or v

    def _open_nonblock(self):
        if not os.path.exists(self.path):
            return None
        f = open(self.path, "rb", buffering=0)
        fd = f.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return f

    def run(self):
        while not self._stop:
            try:
                f = self._open_nonblock()
                if not f:
                    time.sleep(0.3)
                    continue

                while not self._stop:
                    try:
                        data = f.read(SZ)
                        if not data:
                            time.sleep(0.005)
                            continue
                        if len(data) != SZ:
                            time.sleep(0.005)
                            continue

                        _t, value, etype, num = struct.unpack(FMT, data)
                        if (etype & 0x80) != 0:
                            continue
                        et = etype & 0x7F

                        # Axis
                        if et == 0x02:
                            v = int(value)
                            if num == 0:  # X
                                new_state = 0
                                if v < -self.DEADZONE:
                                    new_state = -1
                                elif v > self.DEADZONE:
                                    new_state = +1
                                with self._lock:
                                    old = self._axis[0]
                                    if new_state != old:
                                        self._axis[0] = new_state
                                        if new_state == -1:
                                            self._events["left"] = True
                                        elif new_state == +1:
                                            self._events["right"] = True

                            elif num == 1:  # Y
                                new_state = 0
                                if v < -self.DEADZONE:
                                    new_state = -1
                                elif v > self.DEADZONE:
                                    new_state = +1
                                with self._lock:
                                    old = self._axis[1]
                                    if new_state != old:
                                        self._axis[1] = new_state
                                        if new_state == -1:
                                            self._events["up"] = True
                                        elif new_state == +1:
                                            self._events["down"] = True

                        # Buttons
                        elif et == 0x01:
                            with self._lock:
                                if value == 1:
                                    self._btn[num] = True
                                elif value == 0:
                                    self._btn[num] = False

                            if value == 1:
                                if num in BTN_OK:
                                    self._push(ok=True)
                                elif num in BTN_BACK:
                                    self._push(back=True)
                                elif num == BTN_START:
                                    self._push(start=True)
                                elif num == BTN_SELECT:
                                    self._push(select=True)

                    except BlockingIOError:
                        time.sleep(0.005)
                    except OSError as e:
                        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                            time.sleep(0.005)
                            continue
                        break

                try:
                    f.close()
                except Exception:
                    pass
            except Exception:
                pass
            time.sleep(0.2)


# =========================
# WEATHER (wttr.in)
# =========================
def _fetch_wttr(postcode: str) -> Optional[Dict[str, Any]]:
    url = f"https://wttr.in/{postcode}?format=j1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "led-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=WEATHER_TIMEOUT_SEC) as r:
            data = r.read().decode("utf-8", errors="ignore")
        return json.loads(data)
    except Exception:
        return None


def _parse_weather(j: Dict[str, Any]) -> Tuple[Optional[int], str, Optional[int]]:
    """
    Returns (temp_c, kind, rain_pct)
    rain_pct = max chance of rain in next ~12h (0..100), or None
    kind in {"sun","cloud","rain","snow","fog","unknown"}
    """
    temp_c: Optional[int] = None
    kind: str = "unknown"
    rain_pct: Optional[int] = None

    # current condition -> temp + kind
    try:
        cur = j["current_condition"][0]
        temp_c = int(float(cur.get("temp_C", "0")))
        desc = ""
        if cur.get("weatherDesc"):
            desc = (cur["weatherDesc"][0].get("value") or "").lower()
        code = (cur.get("weatherCode") or "").strip()

        text = f"{desc} {code}".strip()
        if "snow" in text:
            kind = "snow"
        elif "rain" in text or "drizzle" in text or "shower" in text:
            kind = "rain"
        elif "fog" in text or "mist" in text or "haze" in text:
            kind = "fog"
        elif "cloud" in text or "overcast" in text:
            kind = "cloud"
        elif "sun" in text or "clear" in text:
            kind = "sun"
        else:
            kind = "unknown"
    except Exception:
        pass

    # rain chance: max of next ~12h (first 8 slots of 3h)
    try:
        today = j.get("weather", [])[0]
        hourly = today.get("hourly", []) or []
        vals: List[int] = []
        for h in hourly[:8]:
            for k in ("chanceofrain", "chanceOfRain"):
                if k in h:
                    try:
                        vals.append(int(h[k]))
                    except Exception:
                        pass
                    break
        if vals:
            rain_pct = max(vals)
    except Exception:
        pass

    return temp_c, kind, rain_pct


class WeatherCache:
    def __init__(self):
        self.last = 0.0
        self.temp_c: Optional[int] = None
        self.kind: str = "unknown"
        self.rain_pct: Optional[int] = None
        self.ok: bool = False

    def update(self, postcode: str):
        now = time.time()
        if postcode and self.ok and (now - self.last) < WEATHER_REFRESH_SEC:
            return

        if not postcode:
            self.ok = False
            self.temp_c = None
            self.kind = "unknown"
            self.rain_pct = None
            return

        j = _fetch_wttr(postcode)
        if not j:
            self.ok = False
            self.rain_pct = None
            return

        t, k, rp = _parse_weather(j)
        self.temp_c = t
        self.kind = k
        self.rain_pct = rp
        self.ok = t is not None
        self.last = now


# =========================
# SYSTEM INFO
# =========================
def cpu_temp_c() -> Optional[float]:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r", encoding="utf-8") as f:
            v = int(f.read().strip())
        return v / 1000.0
    except Exception:
        return None


def load_1m() -> Optional[float]:
    try:
        return float(os.getloadavg()[0])
    except Exception:
        return None


def ram_used_pct() -> Optional[int]:
    try:
        mem_total = None
        mem_avail = None
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1])
        if not mem_total or mem_avail is None:
            return None
        used = mem_total - mem_avail
        return int(round(100.0 * used / mem_total))
    except Exception:
        return None


def disk_used_pct(path="/") -> Optional[int]:
    try:
        st = os.statvfs(path)
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        used = total - free
        if total <= 0:
            return None
        return int(round(100.0 * used / total))
    except Exception:
        return None


def get_ip() -> Optional[str]:
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True).strip()
        parts = [p for p in out.split() if p and not p.startswith("127.")]
        return parts[0] if parts else None
    except Exception:
        return None


# =========================
# ANIMATED ICONS (simple pixel art)
# =========================
def draw_icon(img: Image.Image, kind: str, x: int, y: int, frame: int):
    d = ImageDraw.Draw(img)
    FG = (220, 220, 220)
    MID = (160, 160, 160)
    DIM = (90, 90, 90)

    if kind == "sun":
        d.ellipse((x + 10, y + 10, x + 18, y + 18), outline=FG, fill=None)
        on = (frame % 8) < 4
        if on:
            d.line((x + 14, y + 3, x + 14, y + 8), fill=FG)
            d.line((x + 14, y + 20, x + 14, y + 25), fill=FG)
            d.line((x + 3, y + 14, x + 8, y + 14), fill=FG)
            d.line((x + 20, y + 14, x + 25, y + 14), fill=FG)
            d.line((x + 6, y + 6, x + 9, y + 9), fill=FG)
            d.line((x + 19, y + 19, x + 22, y + 22), fill=FG)
            d.line((x + 19, y + 9, x + 22, y + 6), fill=FG)
            d.line((x + 6, y + 22, x + 9, y + 19), fill=FG)

    elif kind == "cloud":
        dx = int(2 * math.sin(frame / 6.0))
        d.ellipse((x + 6 + dx, y + 12, x + 14 + dx, y + 20), outline=MID, fill=None)
        d.ellipse((x + 12 + dx, y + 9, x + 22 + dx, y + 19), outline=FG, fill=None)
        d.ellipse((x + 18 + dx, y + 12, x + 26 + dx, y + 20), outline=MID, fill=None)
        d.rectangle((x + 8 + dx, y + 16, x + 24 + dx, y + 22), outline=FG, fill=None)

    elif kind == "rain":
        d.ellipse((x + 7, y + 10, x + 15, y + 18), outline=MID)
        d.ellipse((x + 13, y + 7, x + 23, y + 17), outline=FG)
        d.ellipse((x + 19, y + 10, x + 27, y + 18), outline=MID)
        d.rectangle((x + 9, y + 14, x + 25, y + 20), outline=FG)
        offs = frame % 6
        for i, xx in enumerate([x + 11, x + 16, x + 21]):
            yy = y + 22 + ((offs + i * 2) % 6)
            d.line((xx, yy, xx, yy + 2), fill=FG)

    elif kind == "snow":
        d.ellipse((x + 7, y + 10, x + 15, y + 18), outline=MID)
        d.ellipse((x + 13, y + 7, x + 23, y + 17), outline=FG)
        d.ellipse((x + 19, y + 10, x + 27, y + 18), outline=MID)
        d.rectangle((x + 9, y + 14, x + 25, y + 20), outline=FG)
        on = (frame % 8) < 4
        if on:
            for xx in [x + 12, x + 18, x + 23]:
                d.point((xx, y + 24), fill=FG)
                d.point((xx - 1, y + 25), fill=FG)
                d.point((xx + 1, y + 25), fill=FG)
                d.point((xx, y + 26), fill=FG)

    elif kind == "fog":
        for i in range(3):
            yy = y + 12 + i * 5 + int(1 * math.sin((frame + i * 2) / 4.0))
            d.line((x + 6, yy, x + 26, yy), fill=DIM)

    else:
        d.rectangle((x + 8, y + 8, x + 24, y + 24), outline=MID)


# =========================
# MENU
# =========================
MENU_ITEMS = ["RETURN", "POSTCODE", "AUTO SCROLL", "AUTO DIM", "EXIT"]


def draw_menu(img: Image.Image, font: ImageFont.ImageFont, sel: int,
              postcode: str, autoscroll: bool, autodim: bool,
              edit_postcode: bool, cursor: int):
    d = ImageDraw.Draw(img)
    d.rectangle((10, 8, W - 10, H - 8), fill=(0, 0, 0), outline=(120, 120, 120))
    draw_centered_crisp(img, 10, "DASH MENU", font, fill=(200, 200, 200))

    y0 = 20
    for i, it in enumerate(MENU_ITEMS):
        yy = y0 + i * 8
        col = (245, 245, 245) if i == sel else (160, 160, 160)

        label = it
        if it == "POSTCODE":
            pc = postcode if postcode else "-----"
            if edit_postcode:
                pc_list = list(pc)
                while len(pc_list) < 5:
                    pc_list.append("0")
                pc = "".join(pc_list[:5])
            label = f"POST {pc}"
        elif it == "AUTO SCROLL":
            label = f"AUTO SCROLL {'ON' if autoscroll else 'OFF'}"
        elif it == "AUTO DIM":
            label = f"AUTO DIM {'ON' if autodim else 'OFF'}"

        draw_text_crisp(img, (14, yy), label, font, fill=col)

    if edit_postcode:
        pc = (postcode or "00000")
        pc_list = list(pc)
        while len(pc_list) < 5:
            pc_list.append("0")
        pc5 = "".join(pc_list[:5])

        # underline current digit under "POST {pc5}"
        pre = f"POST {pc5[:cursor]}"
        x = 14 + text_width(ImageDraw.Draw(img), pre, font)
        d.line((x, y0 + 1 * 8 + 7, x + 4, y0 + 1 * 8 + 7), fill=(245, 245, 245))


# =========================
# BRIGHTNESS (auto dim)
# =========================
def compute_brightness(base: int, autodim: bool) -> int:
    if not autodim:
        return int(base)
    hr = int(time.strftime("%H"))
    night = (hr >= DIM_NIGHT_START) or (hr < DIM_NIGHT_END)
    if night:
        return int(clamp(int(base * DIM_FACTOR_NIGHT), 5, base))
    return int(base)


# =========================
# MAIN
# =========================
def main():
    cfg = load_config()

    base_bright = int(cfg.get("brightness", 60))
    try:
        base_bright = int(os.environ.get("MATRIX_BRIGHTNESS", base_bright))
    except Exception:
        pass
    base_bright = int(clamp(base_bright, 5, 100))

    postcode = str(cfg.get("postcode", "10000")) if cfg.get("postcode") is not None else "10000"
    postcode = "".join([c for c in postcode if c.isdigit()])[:5]

    autoscroll = bool(cfg.get("dash_autoscroll", True))
    autodim = bool(cfg.get("dash_autodim", False))

    matrix = make_matrix(compute_brightness(base_bright, autodim))
    offscreen = matrix.CreateFrameCanvas()

    font_big = load_font(16)
    font_mid = load_font(9)
    font_small = load_font(8)

    img = Image.new("RGB", (W, H), (0, 0, 0))

    joy = Joy()
    joy.start()

    weather = WeatherCache()

    page = 0
    last_nav = 0.0
    last_page_switch = time.time()

    menu_open = False
    menu_sel = 0
    edit_postcode = False
    pc_cursor = 0

    frame = 0

    def save_dash_cfg():
        nonlocal cfg
        cfg = load_config()
        cfg["postcode"] = postcode
        cfg["dash_autoscroll"] = autoscroll
        cfg["dash_autodim"] = autodim
        save_config(cfg)

    def exit_to_launcher():
        matrix.Clear()
        time.sleep(0.05)
        os.execv("/usr/bin/python3", ["python3", LAUNCHER_PATH])

    def draw_page_1():
        img.paste((0, 0, 0), (0, 0, W, H))

        clk = time.strftime("%H:%M")
        draw_centered_crisp(img, 2, clk, font_big, fill=(245, 245, 245))

        date = time.strftime("%d.%m")
        dow = time.strftime("%a").upper()
        draw_centered_crisp(img, 20, f"{dow}  {date}", font_mid, fill=(170, 170, 170))

        weather.update(postcode)

        if not postcode:
            temp_str = "--°C"
            kind = "unknown"
            line = "POSTCODE?"
            rain_line = ""
        elif not weather.ok:
            temp_str = "--°C"
            kind = "unknown"
            line = "NO NET"
            rain_line = ""
        else:
            temp_str = f"{weather.temp_c:>2d}°C"
            kind = weather.kind
            # rain chance line
            if weather.rain_pct is None:
                rain_line = ""
            else:
                rain_line = f"RAIN {weather.rain_pct:>2d}%"
            line = "WEATHER"

        draw_icon(img, kind, 10, 30, frame)
        draw_text_crisp(img, (46, 36), temp_str, font_big, fill=(245, 245, 245))
        draw_text_crisp(img, (46, 52), line, font_mid, fill=(150, 150, 150))

        if rain_line:
            draw_text_crisp(img, (86, 54), rain_line, font_small, fill=(150, 150, 150))

        draw_text_crisp(img, (W - 16, H - 10), "1/2", font_small, fill=(90, 90, 90))

    def draw_page_2():
        img.paste((0, 0, 0), (0, 0, W, H))

        ip = get_ip() or "NO IP"
        t = cpu_temp_c()
        l = load_1m()
        ram = ram_used_pct()
        sd = disk_used_pct("/")

        draw_text_crisp(img, (4, 2), "SYSTEM", font_small, fill=(110, 110, 110))
        draw_text_crisp(img, (W - 16, 2), "2/2", font_small, fill=(90, 90, 90))

        y = 12
        cpu_s = f"CPU {t:4.1f}C" if t is not None else "CPU --.-C"
        load_s = f"L {l:.2f}" if l is not None else "L --"
        draw_text_crisp(img, (4, y), cpu_s, font_mid, fill=(235, 235, 235))
        draw_text_crisp(img, (72, y), load_s, font_mid, fill=(180, 180, 180))

        y += 10
        ram_s = f"RAM {ram:3d}%" if ram is not None else "RAM ---%"
        sd_s = f"SD {sd:3d}%" if sd is not None else "SD ---%"
        draw_text_crisp(img, (4, y), ram_s, font_mid, fill=(235, 235, 235))
        draw_text_crisp(img, (72, y), sd_s, font_mid, fill=(180, 180, 180))

        y += 10
        d = ImageDraw.Draw(img)
        ip_t = ip if text_width(d, ip, font_mid) <= (W - 8) else ip[:15] + "…"
        draw_text_crisp(img, (4, y), f"IP {ip_t}", font_mid, fill=(200, 200, 200))

        y += 12
        up = "UP ?"
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as f:
                sec = float(f.read().split()[0])
            days = int(sec // 86400)
            hrs = int((sec % 86400) // 3600)
            up = f"UP {days}d{hrs}h" if days > 0 else f"UP {hrs}h"
        except Exception:
            pass
        draw_text_crisp(img, (4, y), up, font_mid, fill=(150, 150, 150))

    try:
        while True:
            # apply auto dim live
            try:
                matrix.brightness = compute_brightness(base_bright, autodim)
            except Exception:
                pass

            ev = joy.pop_events()
            now = time.time()
            can_nav = (now - last_nav) >= NAV_REPEAT

            # open menu
            if (ev.get("start") or ev.get("select")) and not menu_open:
                menu_open = True
                edit_postcode = False
                menu_sel = 0
                last_nav = now

            if menu_open:
                if edit_postcode:
                    # edit postcode: LEFT/RIGHT cursor, UP/DOWN digit
                    pc = list((postcode or "00000"))
                    while len(pc) < 5:
                        pc.append("0")
                    pc = pc[:5]

                    if can_nav and ev.get("left"):
                        pc_cursor = max(0, pc_cursor - 1)
                        last_nav = now
                    elif can_nav and ev.get("right"):
                        pc_cursor = min(4, pc_cursor + 1)
                        last_nav = now
                    elif can_nav and ev.get("up"):
                        dgt = int(pc[pc_cursor])
                        pc[pc_cursor] = str((dgt + 1) % 10)
                        last_nav = now
                    elif can_nav and ev.get("down"):
                        dgt = int(pc[pc_cursor])
                        pc[pc_cursor] = str((dgt - 1) % 10)
                        last_nav = now

                    postcode = "".join(pc)

                    if ev.get("ok"):
                        edit_postcode = False
                        save_dash_cfg()

                    if ev.get("back"):
                        edit_postcode = False

                else:
                    # menu navigation
                    if can_nav and ev.get("up"):
                        menu_sel = max(0, menu_sel - 1)
                        last_nav = now
                    elif can_nav and ev.get("down"):
                        menu_sel = min(len(MENU_ITEMS) - 1, menu_sel + 1)
                        last_nav = now

                    if ev.get("back"):
                        menu_open = False

                    if ev.get("ok"):
                        item = MENU_ITEMS[menu_sel]
                        if item == "RETURN":
                            menu_open = False
                        elif item == "EXIT":
                            save_dash_cfg()
                            exit_to_launcher()
                        elif item == "AUTO SCROLL":
                            autoscroll = not autoscroll
                            save_dash_cfg()
                        elif item == "AUTO DIM":
                            autodim = not autodim
                            save_dash_cfg()
                        elif item == "POSTCODE":
                            edit_postcode = True
                            pc_cursor = 0

            else:
                # Page change with DPAD L/R
                if can_nav and ev.get("left"):
                    page = (page - 1) % 2
                    last_nav = now
                    last_page_switch = now
                elif can_nav and ev.get("right"):
                    page = (page + 1) % 2
                    last_nav = now
                    last_page_switch = now

                # Auto scroll
                if autoscroll and (now - last_page_switch) >= AUTO_SCROLL_SEC:
                    page = (page + 1) % 2
                    last_page_switch = now

            # Render
            if page == 0:
                draw_page_1()
            else:
                draw_page_2()

            if menu_open:
                draw_menu(img, font_small, menu_sel, postcode, autoscroll, autodim, edit_postcode, pc_cursor)

            offscreen.SetImage(img, 0, 0)
            offscreen = matrix.SwapOnVSync(offscreen)

            frame += 1
            time.sleep(0.04)

    finally:
        joy.stop()
        matrix.Clear()


if __name__ == "__main__":
    main()
