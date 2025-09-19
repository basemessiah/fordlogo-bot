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

# ====== VISUAL: "fit like the example" ======
# Target width of the watermark relative to media width (NOT full bleed)
FIT_PCT = float(os.getenv("FIT_PCT", "0.65"))   # 65% by default
ALLOW_UPSCALE = os.getenv("ALLOW_UPSCALE", "true").lower() in ("1","true","yes")
BOTTOM_MARGIN = int(os.getenv("BOTTOM_MARGIN", "20"))  # px padding from bottom/top

# ====== STATE ======
PENDING = {}           # job_id -> {user_id,type,src,ts,logo,pos,opacity}
WAITING = {}           # user_id -> job_id (for custom opacity prompt)
JOB_TTL_SECS = 15*60

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

def job_opacity_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="40%", callback_data=f"job:{job_id}:op:40"),
         InlineKeyboardButton(text="60%", callback_data=f"job:{job_id}:op:60"),
         InlineKeyboardButton(text="80%", callback_data=f"job:{job_id}:op:80")],
        [InlineKeyboardButton(text="Custom…", callback_data=f"job:{job_id}:op:custom")],
        [InlineKeyboardButton(text="Cancel", callback_data=f"job:{job_id}:cancel")],
    ])

def percent_to_alpha255(pct: float) -> int:
    return int(round(255 * (max(0.0, min(100.0, pct)) / 100.0)))

def compute_xy_for_position(img_w, img_h, wm_w, wm_h, pos_key: str) -> tuple[int,int]:
    x = max(0, (img_w - wm_w) // 2)
    if pos_key == "top":
        y = BOTTOM_MARGIN
    elif pos_key == "mid":
        y = max(0, (img_h - wm_h)//2)
    else:
        y = max(0, img_h - wm_h - BOTTOM_MARGIN)
    return x, y

# ====== WATERMARKING (scaled to a nice "fit") ======
def build_logo_scaled(logo_path: Path, target_w: int, opacity_pct: float) -> Path:
    """Resize logo to target_w (optionally upscaling), apply opacity, return temp PNG."""
    logo = Image.open(logo_path).convert("RGBA")
    if not ALLOW_UPSCALE:
        target_w = min(target_w, logo.width)
    ratio = target_w / max(1, logo.width)
    new_w = max(1, int(logo.width * ratio))
    new_h = max(1, int(logo.height * ratio))
    resized = logo.resize((new_w, new_h), resample=Image.LANCZOS)

    alpha_val = percent_to_alpha255(opacity_pct)
    r,g,b,a = resized.split()
    a = a.point(lambda i: alpha_val if i > 0 else 0)
    wm = Image.merge("RGBA", (r,g,b,a))

    out = Path("/tmp") / f"wm_{uuid.uuid4().hex}.png"
    wm.save(out, "PNG")
    return out

def paste_watermark_pillow(src_path: Path, dst_path: Path, wm_key: str, opacity_pct: float, pos_key: str):
    im = Image.open(src_path).convert("RGBA")
    # target width = FIT_PCT * image width
    target_w = max(1, int(im.width * FIT_PCT))
    wm_path = build_logo_scaled(ensure_logo(wm_key), target_w, opacity_pct)
    try:
        wm = Image.open(wm_path).convert("RGBA")
        x, y = compute_xy_for_position(im.width, im.height, wm.width, wm.height, pos_key)
        base = Image.new("RGBA", im.size)
        base = Image.alpha_composite(base, im)
        base.alpha_composite(wm, (x, y))
        base.convert("RGB").save(dst_path, "JPEG", quality=92)
    finally:
        Path(wm_path).unlink(missing_ok=True)

def ffmpeg_overlay_video(src_path: Path, dst_path: Path, wm_key: str, opacity_pct: float, pos_key: str):
    # Get video width with ffprobe
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
    wm_path = build_logo_scaled(ensure_logo(wm_key), target_w, opacity_pct)

    # vertical position
    if pos_key == "top":
        overlay_y = str(BOTTOM_MARGIN)
    elif pos_key == "mid":
        overlay_y = "(main_h-overlay_h)/2"
    else:
        overlay_y = f"main_h-overlay_h-{BOTTOM_MARGIN}"

    vf = f"[0][1]overlay=(main_w-overlay_w)/2:{overlay_y}"
    cmd = [
        "ffmpeg","-y",
        "-i", str(src_path),
        "-i", str(wm_path),
        "-filter_complex", vf,
        "-c:v","libx264","-preset","veryfast","-crf","23",
        "-c:a","copy",
        "-pix_fmt","yuv420p",
        "-movflags","+faststart",
        str(dst_path)
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finally:
        Path(wm_path).unlink(missing_ok=True)

# ====== DONATIONS ======
def qr_image_bytes(payload: str) -> BytesIO:
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(payload); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO(); img.save(buf, format="PNG"); buf.seek(0); return buf

async def maybe_thank_and_prompt_donate(chat_id: int, user_id: int):
    uid = str(user_id)
    USAGE[uid] = USAGE.get(uid, 0) + 1
    save_usage(USAGE)
    n = USAGE[uid]
    if n == 3 or (n > 3 and (n % 5 == 0)):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=coin, callback_data=f"donate:{coin}")]
            for coin in WALLETS.keys()
        ]) if WALLETS else None
        text = ("🙏 Looks like you’re enjoying the bot! If it helps, consider a small donation."
                " Tap a coin below to get the address/QR.") if WALLETS else "🙏 Enjoying the bot? A small donation helps."
        await bot.send_message(chat_id, text, reply_markup=kb)

# ====== BOT ======
bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

@dp.message(Command("start"))
async def on_start(msg: Message):
    pct = int(FIT_PCT * 100)
    await msg.answer(
        "Welcome to $Ford Logo Bot 👋\n\n"
        "📌 *How to use*\n"
        "1) Send an *image (≤2MB)* or *video (≤20MB)*.\n"
        "2) Choose watermark color: **White** or **Black**.\n"
        "3) Choose position: **Top / Middle / Bottom**.\n"
        "4) Choose opacity (40/60/80% or Custom number).\n"
        f"→ I’ll place the logo at ~{pct}% of the width, centered at your chosen position.\n\n"
        "💸 /donate  ℹ️ /help  👤 /about  📊 /stats",
        parse_mode="Markdown"
    )

@dp.message(Command("help"))
async def on_help(msg: Message):
    pct = int(FIT_PCT * 100)
    await msg.answer(
        f"Watermark width ≈ {pct}% of media. Change with env `FIT_PCT` (e.g., 0.7).\n"
        "Upscaling is on so it’s always visible; set `ALLOW_UPSCALE=false` to disable.\n"
        "If video fails, try a smaller file or MP4.\n"
        "Commands: /start /help /donate /about"
    )

@dp.message(Command("about"))
async def on_about(msg: Message):
    await msg.answer(f"Built with ❤️ by **{CREATOR}**.", parse_mode="Markdown")

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
    coin = cb.data.split(":", 1)[1]
    addr = WALLETS.get(coin)
    if not addr: await cb.answer("Unavailable", show_alert=True); return
    uri = f"solana:{addr}" if coin == "SOL" else addr
    png = qr_image_bytes(uri)
    await bot.send_photo(
        chat_id=cb.message.chat.id,
        photo=InputFile(png, filename=f"{coin.replace(' ','_')}.png"),
        caption=f"**{coin} Donation**\n`{addr}`\n\n• Network: Solana (SPL for USDT)",
        parse_mode="Markdown"
    )
    await cb.answer()

# ====== FILE HANDLERS ======
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
                       "logo": None, "pos": None, "opacity": None}
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
                       "logo": None, "pos": None, "opacity": None}
    await msg.reply("Choose watermark color:", reply_markup=job_logo_keyboard(job_id))

# position + opacity flow
def job_position_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Top", callback_data=f"job:{job_id}:pos:top"),
         InlineKeyboardButton(text="Middle", callback_data=f"job:{job_id}:pos:mid"),
         InlineKeyboardButton(text="Bottom", callback_data=f"job:{job_id}:pos:bot")],
        [InlineKeyboardButton(text="Cancel", callback_data=f"job:{job_id}:cancel")],
    ])

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
        await cb.message.edit_text("Choose opacity:", reply_markup=job_opacity_keyboard(job_id))
        await cb.answer(); return

    if section == "op":
        val = rest[0] if rest else None
        if val in ("40","60","80"):
            job["opacity"] = float(val)
            await cb.answer("Opacity set.")
            await process_and_send(bot, cb.message.chat.id, job_id, msg_to_edit=cb.message)
            return
        elif val == "custom":
            WAITING[cb.from_user.id] = job_id
            await cb.message.edit_text("Send a number between **10** and **100** (e.g., 65).", parse_mode="Markdown")
            await cb.answer(); return

    await cb.answer("Unknown action.", show_alert=True)

@dp.message(F.text)
async def on_text(msg: Message):
    job_id = WAITING.get(msg.from_user.id)
    if not job_id: return
    job = PENDING.get(job_id)
    if not job:
        WAITING.pop(msg.from_user.id, None); return

    m = re.search(r"(\d{1,3})", msg.text.strip())
    if not m: await msg.reply("Send a number like 65 (10–100)."); return
    val = int(m.group(1))
    if not 10 <= val <= 100:
        await msg.reply("Value must be 10–100."); return

    job["opacity"] = float(val)
    WAITING.pop(msg.from_user.id, None)
    await process_and_send(bot, msg.chat.id, job_id)

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
    if job.get("opacity") is None:
        if msg_to_edit: await msg_to_edit.edit_text("Pick an opacity.")
        return

    src = job["src"]
    dst = TMP_DIR / (f"{uuid.uuid4()}.mp4" if job["type"] == "video" else f"{uuid.uuid4()}.jpg")
    if msg_to_edit:
        try: await msg_to_edit.edit_text("⏳ Processing…")
        except: pass

    try:
        if job["type"] == "image":
            paste_watermark_pillow(src, dst, job["logo"], job["opacity"], job["pos"])
            await bot.send_photo(chat_id, FSInputFile(dst), caption="✅ Watermarked")
        else:
            ffmpeg_overlay_video(src, dst, job["logo"], job["opacity"], job["pos"])
            await bot.send_video(chat_id, FSInputFile(dst), caption="✅ Watermarked")
        await maybe_thank_and_prompt_donate(chat_id, job["user_id"])
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
