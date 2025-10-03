#!/usr/bin/env python3
import asyncio
import logging
import os
import time
from pathlib import Path
import tempfile
import requests
from bs4 import BeautifulSoup
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------- Config from Environment ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))  # numeric chat id
MAX_SEND_BYTES = 2 * 1024 * 1024 * 1024  # 2GB

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("❌ BOT_TOKEN and CHAT_ID environment variables must be set!")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; SM-G975F) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36"
)
REFERER = "https://hanime.tv/"
ORIGIN = "https://player.hanime.tv"

def get_download_dir() -> Path:
    tmp = Path(tempfile.gettempdir()) / "hanime_bot_downloads"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp

def yt_dlp_download_blocking(url: str, outdir: Path, progress_state: dict) -> Path:
    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": str(outdir / "%(title).200s.%(ext)s"),
        "http_chunk_size": 10 * 1024 * 1024,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "hls_use_mpegts": True,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
            "Origin": ORIGIN,
        },
    }

    def progress_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                progress_state["percent"] = int(downloaded * 100 / total)
        elif d.get("status") == "finished":
            progress_state["percent"] = 100

    ydl_opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        ydl.download([url])
        path = Path(ydl.prepare_filename(info))
        if not path.exists():
            path = path.with_suffix(".mp4")
        return path

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        await update.message.reply_text("This bot is private.")
        return
    await update.message.reply_text("🎉 Hanime Bot ready! Use /random to fetch a video.")

async def random_hanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        await update.message.reply_text("This bot is private.")
        return

    status_msg = await update.message.reply_text("🔎 Fetching random video...")

    loop = asyncio.get_running_loop()
    try:
        # Fetch random video URL
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        r = s.get("https://hanime.tv/browse/random", allow_redirects=True, timeout=20)
        r.raise_for_status()
        video_url = r.url

        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.find("title").text.strip() if soup.find("title") else "Random Video"

        await status_msg.edit_text(f"🎲 Selected: {title}\n⬇️ Starting download...")

        progress_state = {"percent": 0}

        def run_download():
            return yt_dlp_download_blocking(video_url, get_download_dir(), progress_state)

        future = loop.run_in_executor(None, run_download)

        while not future.done():
            pct = progress_state.get("percent", 0)
            try:
                await status_msg.edit_text(f"⬇️ Downloading: {pct}%")
            except:
                pass
            await asyncio.sleep(2)

        path = await future
        size = path.stat().st_size
        if size > MAX_SEND_BYTES:
            await status_msg.edit_text(f"❌ File too large ({size/1024/1024:.2f}MB).")
            return

        await status_msg.edit_text(f"📤 Uploading {path.name}...")
        with path.open("rb") as f:
            await context.bot.send_document(chat_id=CHAT_ID, document=f, filename=path.name)

        await status_msg.edit_text(f"✅ Sent: {path.name}")

    except Exception as e:
        logger.exception("Error")
        await status_msg.edit_text(f"❌ Error: {e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_hanime))
    logger.info("Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()