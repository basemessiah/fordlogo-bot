
# $Ford Logo Bot

Telegram bot that adds a down-center watermark (choose logo per job, adjustable opacity) and supports crypto donations (USDT on Solana, SOL).

## Deploy on Render (free)

1) Create a new GitHub repo and upload these files.
2) On https://render.com → New → Web Service → connect repo (Dockerfile is auto-detected).
3) Environment Variables:
   - BOT_TOKEN = (from BotFather)
   - USDT_SOL_ADDR = your USDT (Solana) address
   - SOL_ADDR = your SOL address
4) Click Create Web Service and wait for deploy.
5) Open Telegram and /start the bot.

## Commands
- /start, /help, /donate
- Send image (≤2MB) or video (≤20MB), choose logo + opacity → receive result.

## Notes
- For videos, bot uses FFmpeg (libx264, faststart).
- Per-job selection: each upload is independent.
