import asyncio, os, subprocess, uuid, time, re, json
from pathlib import Path
from typing import Optional
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, InputFile
)
from aiogram.filters import Command
from PIL import Image
import qrcode
from io import BytesIO

# Webhook server (Render)
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ====== ENV ======
BOT_TOKEN  = os.getenv("BOT_TOKEN")
BASE_URL   = os.getenv("BASE_URL")
CREATOR    = os.getenv("CREATOR_NAME", "basemessiah @pndmedia")

ADMIN_ID   = int(os.getenv("ADMIN_ID", "0"))

WALLETS = {
    "USDT (Solana)": os.getenv("USDT_SOL_ADDR", "").strip(),
    "SOL":           os.getenv("SOL_ADDR", "").strip(),
}
WALLETS = {k: v for k, v in WALLETS.items() if v}

# ====== PATHS ======
BASE_DIR = Path(__file__).parent
ASSETS = BASE_DIR / "assets"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True, parents=True)

USAGE_FILE = DATA_DIR / "usage.json"

WATERMARKS = {
    "white": ASSETS / "white.png",
    "black": ASSETS / "black.png",
}

IMG_MAX = 2 * 1024 * 1024
VID_MAX = 20 * 1024 * 1024
TMP_DIR = Path("/tmp")

# ==== VISUAL ====
SCALE = float(os.getenv("SCALE", "0.98"))
ALLOW_UPSCALE = os.getenv("ALLOW_UPSCALE", "true").lower() in ("1","true","yes")

PENDING = {}
WAITING = {}
USAGE = {}

# ====== USAGE ======
def load_usage():
    if USAGE_FILE.exists():
        try:
            return json.loads(USAGE_FILE.read_text())
        except:
            return {}
    return {}
def save_usage(d):
    try: USAGE_FILE.write_text(json.dumps(d))
    except: pass
USAGE = load_usage()

def ensure_logo(key: str) -> Path:
    p = WATERMARKS.get(key)
    return p if (p and p.exists()) else list(WATERMARKS.values())[0]

def percent_to_alpha255(pct: float) -> int:
    return int(round(255 * (max(0,min(100,pct))/100.0)))

def compute_xy(img_w, img_h, wm_w, wm_h, pos: str, margin=20):
    x = (img_w - wm_w) // 2
    if pos=="top": y = margin
    elif pos=="mid": y = (img_h - wm_h)//2
    else: y = img_h - wm_h - margin
    return max(0,x), max(0,y)

# ====== WATERMARKING ======
def build_logo(logo_path: Path, target_w: int, opacity_pct: float) -> Path:
    logo = Image.open(logo_path).convert("RGBA")
    if not ALLOW_UPSCALE: target_w = min(target_w, logo.width)
    ratio = target_w/max(1,logo.width)
    resized = logo.resize((max(1,int(logo.width*ratio)), max(1,int(logo.height*ratio))), Image.LANCZOS)

    alpha_val = percent_to_alpha255(opacity_pct)
    r,g,b,a = resized.split()
    a = a.point(lambda i: alpha_val if i>0 else 0)
    wm = Image.merge("RGBA",(r,g,b,a))

    out = Path("/tmp")/f"wm_{uuid.uuid4().hex}.png"
    wm.save(out,"PNG")
    return out

def paste_image(src: Path, dst: Path, wm_key: str, opacity: float, pos: str):
    im = Image.open(src).convert("RGBA")
    target_w = max(1,int(im.width*SCALE))
    wm_path = build_logo(ensure_logo(wm_key), target_w, opacity)
    try:
        wm = Image.open(wm_path).convert("RGBA")
        x,y = compute_xy(im.width, im.height, wm.width, wm.height, pos)
        base = Image.new("RGBA", im.size)
        base = Image.alpha_composite(base, im)
        base.alpha_composite(wm,(x,y))
        base.convert("RGB").save(dst,"JPEG",quality=92)
    finally:
        Path(wm_path).unlink(missing_ok=True)

def overlay_video(src: Path, dst: Path, wm_key: str, opacity: float, pos: str):
    try:
        probe = subprocess.run(
            ["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width","-of","csv=p=0",str(src)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,text=True
        )
        vid_w = int(probe.stdout.strip() or "0")
    except: vid_w=0
    target_w = max(320,int(vid_w*SCALE)) if vid_w>0 else 640
    wm_path = build_logo(ensure_logo(wm_key), target_w, opacity)

    if pos=="top": y="20"
    elif pos=="mid": y="(main_h-overlay_h)/2"
    else: y="main_h-overlay_h-20"

    vf=f"[0][1]overlay=(main_w-overlay_w)/2:{y}"
    cmd=["ffmpeg","-y","-i",str(src),"-i",str(wm_path),"-filter_complex",vf,
         "-c:v","libx264","-preset","veryfast","-crf","23","-c:a","copy","-movflags","+faststart",str(dst)]
    try: subprocess.run(cmd,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    finally: Path(wm_path).unlink(missing_ok=True)

# ====== BOT CORE ======
bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer(
        "Welcome to $Ford Logo Bot 👋\n\n"
        "Send me an image (≤2MB) or video (≤20MB).\n"
        "Then pick a logo (white/black), position, and opacity.\n"
        "I'll return the watermarked result."
    )

# (keep the rest of the handlers exactly as in previous version: help, donate, callbacks, usage, webhook etc.)
# To save space here, only watermark-specific changes were made (background strip removed).
# The rest of your bot code remains the same from my last version.
