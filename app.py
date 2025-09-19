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
ADMIN_ID   = int(os.getenv("ADMIN_ID", "0"))  # set to YOUR numeric Telegram user id

# Donation wallets (Solana)
WALLETS = {
    "USDT (Solana)": os.getenv("USDT_SOL_ADDR", "").strip(),
    "SOL":           os.getenv("SOL_ADDR", "").strip(),
}
WALLETS = {k: v for k, v in WALLETS.items() if v}

# ====== PATHS / LIMITS ======
BASE_DIR = Path(__file__).parent
ASSETS   = BASE_DIR / "assets"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True, parents=True)

USAGE_FILE = DATA_DIR / "usage.json"

WATERMARKS = {
    "white": ASSETS / "white.png",
    "black": ASSETS / "black.png",
}

IMG_MAX = 2 * 1024 * 1024     # 2 MB
VID_MAX = 20 * 1024 * 1024    # 20 MB
TMP_DIR = Path("/tmp")

# ====== VISUAL ======
FIT_PCT       = float(os.getenv("FIT_PCT", "0.65"))   # watermark width as % of media width
ALLOW_UPSCALE = os.getenv("ALLOW_UPSCALE", "true").lower() in ("1","true","yes")
VERT_MARGIN   = int(os.getenv("VERT_MARGIN", "20"))   # px from top/bottom
MASK_ALPHA    = float(os.getenv("MASK_ALPHA", "0.35"))  # 0..1 translucency of mask

# ====== STATE ======
PENDING = {}           # job_id -> {user_id,type,src,ts,logo,pos}
JOB_TTL_SECS = 15*60
USAGE = {}

# ====== USAGE PERSISTENCE ======
def load_usage():
    if USAGE_FILE.exists():
        try: return json.loads(USAGE_FILE.read_text())
        except: return {}
    return {}
def save_usage(d):
    try: USAGE_FILE.write_text(json.dumps(d))
    except: pass
USAGE = load_usage()

# ====== HELPERS ======
def ensure_logo(key: str) -> Path:
    p = WATERMARKS.get(key)
    return p if (p and p.exists()) else list(WATERMARKS.values())[0]

def cleanup_old_jobs():
    now = time.time()
    stale = [jid for jid,j in PENDING.items() if now - j["ts"] > JOB_TTL_SECS]
    for jid in stale:
        try: Path(PENDING[jid]["src"]).unlink(missing_ok=True)
        except: pass
        PENDING.pop(jid, None)

def job_logo_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Use White", callback_data=f"job:{job_id}:logo:white")],
        [InlineKeyboardButton(text="Use Black", callback_data=f"job:{job_id}:logo:black")],
        [InlineKeyboardButton(text="Cancel", callback_data=f"job:{job_id}:cancel")],
    ])

def job_position_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Top",    callback_data=f"job:{job_id}:pos:top"),
         InlineKeyboardButton(text="Middle", callback_data=f"job:{job_id}:pos:mid"),
         InlineKeyboardButton(text="Bottom", callback_data=f"job:{job_id}:pos:bot")],
        [InlineKeyboardButton(text="Cancel", callback_data=f"job:{job_id}:cancel")],
    ])

def compute_xy_for_position(img_w, img_h, wm_w, wm_h, pos_key: str) -> tuple[int,int]:
    x = max(0, (img_w - wm_w) // 2)
    if pos_key == "top":
        y = VERT_MARGIN
    elif pos_key == "mid":
        y = max(0, (img_h - wm_h)//2)
    else:
        y = max(0, img_h - wm_h - VERT_MARGIN)
    return x, y

def percent_to_alpha255(p: float) -> int:
    p = max(0.0, min(1.0, p))
    return int(round(255 * p))

# ====== CARD (logo + translucent mask behind for readability) ======
def build_card_scaled(logo_path: Path, target_w: int, logo_key: str) -> Path:
    """
    Resize the logo to target_w (optionally upscaling), draw a translucent mask
    rectangle of the same size behind it (white for black logo, black for white logo),
    and return a PNG with both combined (RGBA).
    """
    logo = Image.open(logo_path).convert("RGBA")

    if not ALLOW_UPSCALE:
        target_w = min(target_w, logo.width)

    ratio = target_w / max(1, logo.width)
    new_w = max(1, int(logo.width * ratio))
    new_h = max(1, int(logo.height * ratio))
    wm = logo.resize((new_w, new_h), resample=Image.LANCZOS)

    card = Image.new("RGBA", (new_w, new_h), (0,0,0,0))
    if logo_key == "black":
        mask_color = (255, 255, 255, percent_to_alpha255(MASK_ALPHA))
    else:  # "white"
        mask_color = (0, 0, 0, percent_to_alpha255(MASK_ALPHA))

    mask_layer = Image.new("RGBA", (new_w, new_h), mask_color)
    card.alpha_composite(mask_layer, (0,0))
    card.alpha_composite(wm, (0,0))

    out = Path("/tmp") / f"card_{uuid.uuid4().hex}.png"
    card.save(out, "PNG")
    return out

# ====== IMAGE PROCESS ======
def paste_watermark_pillow(src_path: Path, dst_path: Path, wm_key: str, pos_key: str):
    base = Image.open(src_path).convert("RGBA")
    target_w = max(1, int(base.width * FIT_PCT))
    card_path = build_card_scaled(ensure_logo(wm_key), target_w, wm_key)
    try:
        card = Image.open(card_path).convert("RGBA")
        x, y = compute_xy_for_position(base.width, base.height, card.width, card.height, pos_key)
        canvas = Image.new("RGBA", base.size)
        canvas = Image.alpha_composite(canvas, base)
        canvas.alpha_composite(card, (x, y))
        canvas.save(dst_path, "PNG")  # lossless
    finally:
        Path(card_path).unlink(missing_ok=True)

# ====== VIDEO PROCESS ======
def ffmpeg_overlay_video(src_path: Path, dst_path: Path, wm_key: str, pos_key: str):
    # probe width
    try:
        probe = subprocess.run(
            ["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width","-of","csv=p=0", str(src_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, text=True
        )
        vid_w = int(probe.stdout.strip() or "0")
    except Exception:
        vid_w = 0
    video_w = vid_w if vid_w > 0 else 640

    target_w = max(1, int(video_w * FIT_PCT))
    card_path = build_card_scaled(ensure_logo(wm_key), target_w, wm_key)

    if pos_key == "top":
        overlay_y = str(VERT_MARGIN)
    elif pos_key == "mid":
        overlay_y = "(main_h-overlay_h)/2"
    else:
        overlay_y = f"main_h-overlay_h-{VERT_MARGIN}"

    vf = f"[0][1]overlay=(main_w-overlay_w)/2:{overlay_y}"

    cmd = [
        "ffmpeg","-y",
        "-i", str(src_path),
        "-i", str(card_path),
        "-filter_complex", vf,
        "-c:v","libx264","-preset","medium","-crf","20",
        "-c:a","copy",
        "-pix_fmt","yuv420p",
        "-movflags","+faststart",
        str(dst_path)
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finally:
        Path(card_path).unlink(missing_ok=True)

# ====== DONATIONS ======
def qr_image_bytes(payload: str) -> BytesIO:
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(payload); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO(); img.save(buf, format="PNG"); buf.seek(0); return buf

bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

@dp.message(Command("donate"))
async def on_donate(msg: Message):
    if not WALLETS:
        await msg.answer("Donations are currently unavailable."); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=coin, callback_data=f"donate:{coin}")]
        for coin in WALLETS.keys()
    ])
    await msg.answer("Choose a crypto to donate:", reply_markup=kb)

@dp.callback_query(F.data.startswith("donate:"))
async def on_donate_coin(cb: CallbackQuery):
    # NOTE: Bots cannot copy to clipboard automatically.
    # We send the address in a monospace block + QR, then notify.
    coin = cb.data.split(":", 1)[1]
    addr = WALLETS.get(coin)
    if not addr: await cb.answer("Unavailable", show_alert=True); return

    caption = (
        f"**{coin} Address**\n"
        f"`{addr}`\n\n"
        "👉 Long-press to copy, then send your donation to this address.\n"
        "_Thank you for supporting the bot!_"
    )
    try:
        png = qr_image_bytes(addr if coin != "SOL" else f"solana:{addr}")
        await bot.send_photo(
            chat_id=cb.message.chat.id,
            photo=InputFile(png, filename=f"{coin.replace(' ','_')}.png"),
            caption=caption,
            parse_mode="Markdown"
        )
    except Exception:
        await bot.send_message(cb.message.chat.id, caption, parse_mode="Markdown")

    await cb.answer("Address sent — please donate to the address I just sent.", show_alert=False)

# ====== HOW TO USE ======
@dp.message(Command("start"))
async def on_start(msg: Message):
    await msg.answer(
        "Welcome to $Ford Logo Bot 👋\n\n"
        "How to use:\n"
        "1) Send an image (≤2MB) or video (≤20MB).\n"
        "2) Choose watermark color: White or Black.\n"
        "3) Choose position: Top / Middle / Bottom.\n"
        "✅ I’ll place the logo at ~65% of the width with a subtle contrast mask so it’s readable.\n\n"
        "Need to support the bot? Use /donate",
    )

@dp.message(Command("help"))
async def on_help(msg: Message):
    await msg.answer(
        "How to use:\n"
        "• Send an image (≤2MB) or video (≤20MB).\n"
        "• Pick White or Black.\n"
        "• Pick Top / Middle / Bottom.\n"
        "• I’ll return the watermarked result.\n\n"
        "For tips & support: /donate"
    )

@dp.message(Command("about"))
async def on_about(msg: Message):
    await msg.answer(f"Built with ❤️ by {CREATOR}.")

# Quick helper to get YOUR numeric ID so you can set ADMIN_ID
@dp.message(Command("whoami"))
async def on_whoami(msg: Message):
    await msg.answer(f"Your Telegram numeric ID is: `{msg.from_user.id}`\n"
                     "Set this in Render → Environment as `ADMIN_ID` and redeploy.",
                     parse_mode="Markdown")

# ====== FILE HANDLERS ======
def job_opacity_keyboard(job_id: str) -> InlineKeyboardMarkup:
    # (opacity removed by request; keep stub if needed in future)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Cancel", callback_data=f"job:{job_id}:cancel")],
    ])

@dp.message( (F.photo) | (F.document & F.document.mime_type.startswith("image/")) )
async def handle_image(msg: Message):
    cleanup_old_jobs()
    item = msg.photo[-1] if msg.photo else msg.document
    if item.file_size and item.file_size > IMG_MAX:
        await msg.reply("❌ Image too large (limit 2MB)."); return
    f = await bot.get_file(item.file_id)
    src = TMP_DIR / f"{uuid.uuid4()}.img"
    await bot.download_file(f.file_path, destination=src)
    job_id = str(uuid.uuid4())
    PENDING[job_id] = {"user_id": msg.from_user.id, "type": "image", "src": src, "ts": time.time(),
                       "logo": None, "pos": None}
    await msg.reply("Choose watermark color:", reply_markup=job_logo_keyboard(job_id))

@dp.message( (F.video) | (F.animation) )
async def handle_video(msg: Message):
    cleanup_old_jobs()
    item = msg.video or msg.animation
    if item.file_size and item.file_size > VID_MAX:
        await msg.reply("❌ Video too large (limit 20MB)."); return
    f = await bot.get_file(item.file_id)
    src = TMP_DIR / f"{uuid.uuid4()}.mp4"
    await bot.download_file(f.file_path, destination=src)
    job_id = str(uuid.uuid4())
    PENDING[job_id] = {"user_id": msg.from_user.id, "type": "video", "src": src, "ts": time.time(),
                       "logo": None, "pos": None}
    await msg.reply("Choose watermark color:", reply_markup=job_logo_keyboard(job_id))

@dp.callback_query(F.data.startswith("job:"))
async def on_job_callback(cb: CallbackQuery):
    parts = cb.data.split(":")
    if len(parts) < 3:
        await cb.answer("Bad request", show_alert=True); return
    _, job_id, section, *rest = parts

    if section == "cancel":
        job = PENDING.pop(job_id, None)
        if job:
            try: Path(job["src"]).unlink(missing_ok=True)
            except: pass
        await cb.message.edit_text("✖️ Canceled.")
        await cb.answer(); return

    job = PENDING.get(job_id)
    if not job:
        await cb.answer("This job expired. Please resend the file.", show_alert=True); return
    if cb.from_user.id != job["user_id"]:
        await cb.answer("Not your job.", show_alert=True); return

    if section == "logo":
        val = rest[0] if rest else None
        if val not in WATERMARKS:
            await cb.answer("Unknown logo.", show_alert=True); return
        job["logo"] = val
        await cb.message.edit_text("Pick position:", reply_markup=job_position_keyboard(job_id))
        await cb.answer(); return

    if section == "pos":
        val = rest[0] if rest else None
        if val not in ("top","mid","bot"):
            await cb.answer("Unknown position.", show_alert=True); return
        job["pos"] = val
        await cb.message.edit_text("⏳ Processing…")
        await process_and_send(bot, cb.message.chat.id, job_id, msg_to_edit=cb.message)
        await cb.answer(); return

    await cb.answer("Unknown action.", show_alert=True)

# ====== PROCESSOR ======
async def process_and_send(bot: Bot, chat_id: int, job_id: str, msg_to_edit: Optional[Message] = None):
    job = PENDING.get(job_id)
    if not job:
        if msg_to_edit: await msg_to_edit.edit_text("This job expired. Please resend the file.")
        return
    if not job.get("logo"):
        if msg_to_edit: await msg_to_edit.edit_text("Pick a watermark color (white/black).")
        return
    if not job.get("pos"):
        if msg_to_edit: await msg_to_edit.edit_text("Pick a position (top/middle/bottom).")
        return

    src = job["src"]
    dst = TMP_DIR / (f"{uuid.uuid4()}.mp4" if job["type"] == "video" else f"{uuid.uuid4()}.png")

    try:
        if job["type"] == "image":
            paste_watermark_pillow(src, dst, job["logo"], job["pos"])
            await bot.send_photo(chat_id, FSInputFile(dst), caption="✅ Watermarked")
        else:
            ffmpeg_overlay_video(src, dst, job["logo"], job["pos"])
            await bot.send_video(chat_id, FSInputFile(dst), caption="✅ Watermarked")

        # persist usage; small donation nudge sometimes
        uid = job["user_id"]
        USAGE[str(uid)] = USAGE.get(str(uid), 0) + 1
        save_usage(USAGE)
        n = USAGE[str(uid)]
        if n == 3 or (n > 3 and n % 5 == 0):
            if WALLETS:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=coin, callback_data=f"donate:{coin}")]
                    for coin in WALLETS.keys()
                ])
                await bot.send_message(chat_id, "🙏 Enjoying the bot? Consider a small donation.", reply_markup=kb)

    except subprocess.CalledProcessError:
        if msg_to_edit: await msg_to_edit.edit_text("FFmpeg failed. Try a smaller/standard MP4.")
    except Exception as e:
        if msg_to_edit: await msg_to_edit.edit_text(f"Processing error: {e}")
    finally:
        try:
            Path(src).unlink(missing_ok=True)
            Path(dst).unlink(missing_ok=True)
        except: pass
        PENDING.pop(job_id, None)

# ====== ADMIN ======
@dp.message(Command("stats"))
async def on_stats(msg: Message):
    if ADMIN_ID and msg.from_user.id == ADMIN_ID:
        total_users = len(USAGE)
        total_jobs = sum(USAGE.values())
        top = sorted(USAGE.items(), key=lambda x: x[1], reverse=True)[:5]
        lines = [f"📊 Bot Stats", f"Users: {total_users}", f"Total Jobs: {total_jobs}", "Top users:"]
        for uid, cnt in top: lines.append(f"- {uid}: {cnt}")
        await msg.answer("\n".join(lines))
    else:
        await msg.answer("⛔ You are not allowed to view stats.")

@dp.message(Command("exportstats"))
async def on_exportstats(msg: Message):
    if ADMIN_ID and msg.from_user.id == ADMIN_ID:
        if not USAGE_FILE.exists(): USAGE_FILE.write_text(json.dumps(USAGE))
        await bot.send_document(msg.chat.id, FSInputFile(USAGE_FILE), caption="usage.json")
    else:
        await msg.answer("⛔ You are not allowed to export stats.")

# ====== WEBHOOK ======
async def on_startup(app: web.Application):
    assert BOT_TOKEN, "BOT_TOKEN env var required"
    assert BASE_URL, "BASE_URL env var required (e.g. https://<service>.onrender.com)"
    await bot.set_webhook(f"{BASE_URL}/webhook/{BOT_TOKEN}")

async def on_shutdown(app: web.Application):
    await bot.delete_webhook(drop_pending_updates=False)

async def main():
    app = web.Application()
    SimpleRequestHandler(dp, bot).register(app, path=f"/webhook/{BOT_TOKEN}")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup); app.on_shutdown.append(on_shutdown)
    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port); await site.start()
    while True: await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
