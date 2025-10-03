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
import re
import json

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

def extract_video_url_from_page(html_content: str, page_url: str) -> str:
    """
    Extract the actual video URL from hanime.tv page
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Method 1: Look for video player iframe/src
    iframe = soup.find('iframe', {'src': True})
    if iframe and 'player.hanime.tv' in iframe['src']:
        return iframe['src']
    
    # Method 2: Look for m3u8 URLs in script tags
    scripts = soup.find_all('script')
    for script in scripts:
        if script.string:
            # Look for m3u8 URLs
            m3u8_matches = re.findall(r'https?://[^\s"\']+\.m3u8[^\s"\']*', script.string)
            if m3u8_matches:
                return m3u8_matches[0]
            
            # Look for video data in JSON
            if 'video' in script.string.lower() or 'm3u8' in script.string.lower():
                try:
                    # Try to extract JSON data
                    json_matches = re.findall(r'\{[^{}]*"url"[^{}]*\}', script.string)
                    for json_str in json_matches:
                        data = json.loads(json_str)
                        if 'url' in data and '.m3u8' in data['url']:
                            return data['url']
                except:
                    continue
    
    # Method 3: Look for video sources
    video_tags = soup.find_all('video')
    for video in video_tags:
        source_tags = video.find_all('source', {'src': True})
        for source in source_tags:
            if '.m3u8' in source['src']:
                return source['src']
    
    # If no m3u8 found, return the actual video page URL for yt-dlp to handle
    return page_url

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
        try:
            info = ydl.extract_info(url, download=False)
            # If it's a direct m3u8 URL, yt-dlp should handle it now
            if 'url' in info and '.m3u8' in info['url']:
                logger.info(f"Found m3u8 stream: {info['url']}")
            
            ydl.download([url])
            path = Path(ydl.prepare_filename(info))
            if not path.exists():
                path = path.with_suffix(".mp4")
            return path
        except Exception as e:
            logger.error(f"yt-dlp failed: {e}")
            raise

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
        video_page_url = r.url

        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.find("title").text.strip() if soup.find("title") else "Random Video"

        await status_msg.edit_text(f"🎲 Selected: {title}\n🔍 Extracting video URL...")

        # Extract the actual video URL from the page
        video_url = extract_video_url_from_page(r.text, video_page_url)
        
        if video_url == video_page_url:
            await status_msg.edit_text(f"⚠️ Using page URL (couldn't extract m3u8): {video_url}")
        else:
            await status_msg.edit_text(f"🎯 Found video URL: {video_url[:100]}...")

        progress_state = {"percent": 0}

        def run_download():
            return yt_dlp_download_blocking(video_url, get_download_dir(), progress_state)

        future = loop.run_in_executor(None, run_download)

        last_percent = -1
        while not future.done():
            pct = progress_state.get("percent", 0)
            if pct != last_percent:
                try:
                    await status_msg.edit_text(f"⬇️ Downloading: {pct}%")
                    last_percent = pct
                except:
                    pass
            await asyncio.sleep(2)

        path = await future
        size = path.stat().st_size
        if size > MAX_SEND_BYTES:
            await status_msg.edit_text(f"❌ File too large ({size/1024/1024:.2f}MB).")
            path.unlink()  # Clean up
            return

        await status_msg.edit_text(f"📤 Uploading {path.name}...")
        with path.open("rb") as f:
            await context.bot.send_document(chat_id=CHAT_ID, document=f, filename=path.name)

        await status_msg.edit_text(f"✅ Sent: {path.name}")
        
        # Clean up
        try:
            path.unlink()
        except:
            pass

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
