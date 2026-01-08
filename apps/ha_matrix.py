#!/usr/bin/env python3
import time, json, os, urllib.request
from PIL import Image, ImageDraw, ImageFont
from rgbmatrix import RGBMatrix, RGBMatrixOptions

PIXEL_MAPPER = "U-mapper;StackToRow:Z;Rotate:180"
W, H = 128, 64

CONFIG_PATH = "/home/pi/led/ha_config.json"

DEFAULT = {
    "ha_url": "http://homeassistant.local:8123",
    "token": "PASTE_YOUR_LONG_LIVED_TOKEN_HERE",
    "entities": [
        "sensor.living_room_temperature",
        "sensor.living_room_humidity",
    ],
    "refresh_s": 10
}

def load_cfg():
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT, f, indent=2)
        return DEFAULT
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def make_matrix(brightness=60):
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
    opts.brightness = brightness
    opts.drop_privileges = False
    return RGBMatrix(options=opts)

def font(sz):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", sz)
    except Exception:
        return ImageFont.load_default()

def ha_get_state(base_url, token, entity_id):
    url = f"{base_url}/api/states/{entity_id}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as r:
        data = json.loads(r.read().decode("utf-8"))
        return data.get("state", "?"), data.get("attributes", {})

def main():
    cfg = load_cfg()
    matrix = make_matrix(60)
    f12 = font(12)
    f10 = font(10)

    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)

    base = cfg["ha_url"].rstrip("/")
    token = cfg["token"]
    ent = cfg["entities"]
    refresh = int(cfg.get("refresh_s", 10))

    while True:
        d.rectangle((0, 0, W, H), fill=(0,0,0))
        now = time.strftime("%H:%M:%S")
        d.text((2, 1), "HOME ASSISTANT", font=f10, fill=(180,180,180))
        d.text((2, 13), now, font=f12, fill=(255,255,255))

        y = 30
        for e in ent[:3]:
            try:
                st, _attrs = ha_get_state(base, token, e)
                name = e.split(".", 1)[1].replace("_", " ")[:16]
                d.text((2, y), f"{name}:", font=f10, fill=(140,140,140))
                d.text((70, y), str(st)[:10], font=f10, fill=(255,255,255))
            except Exception:
                d.text((2, y), f"{e.split('.',1)[1][:16]}:", font=f10, fill=(140,140,140))
                d.text((70, y), "ERR", font=f10, fill=(255,80,80))
            y += 11

        d.text((2, H-10), "SELECT=EXIT", font=f10, fill=(90,90,90))
        matrix.SetImage(img, 0, 0)

        # exit on SELECT (simple: kill by systemd when launcher restarts)
        time.sleep(refresh)

if __name__ == "__main__":
    main()
