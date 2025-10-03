#!/usr/bin/env python3
import asyncio
import logging
import os
import time
from pathlib import Path
import tempfile
import requests
import cloudscraper
from bs4 import BeautifulSoup
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import re
import json
from tenacity import retry, stop_after_attempt, wait_exponential
import aiofiles
import psutil
import sys

# Try to import hanime plugin
try:
    # Add the plugin path if it exists
    plugin_path = Path("/tmp/hanime-tv-plugin")
    if plugin_path.exists() and str(plugin_path) not in sys.path:
        sys.path.insert(0, str(plugin_path))
    
    from yt_dlp_plugins import hanime_tv
    HANIME_PLUGIN_AVAILABLE = True
    print("✅ Hanime TV plugin loaded successfully")
except ImportError as e:
    print(f"❌ Hanime TV plugin not available: {e}")
    HANIME_PLUGIN_AVAILABLE = False

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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REFERER = "https://hanime.tv/"
ORIGIN = "https://hanime.tv"

def get_download_dir() -> Path:
    tmp = Path(tempfile.gettempdir()) / "hanime_bot_downloads"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp

class HanimeDownloader:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper()
        self.scraper.headers.update({
            'User-Agent': USER_AGENT,
            'Referer': REFERER,
            'Origin': ORIGIN,
        })

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def get_random_video_page(self):
        """Get a random video page URL"""
        try:
            response = self.scraper.get(
                "https://hanime.tv/browse/random", 
                allow_redirects=True, 
                timeout=30
            )
            response.raise_for_status()
            return response.url
        except Exception as e:
            logger.error(f"Failed to get random video: {e}")
            raise

    def get_video_info(self, url):
        """Get video information using yt-dlp with hanime plugin"""
        ydl_opts = {
            'quiet': True,
            'no_warnings': False,
            'extract_flat': False,
            'force_json': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                return info
            except Exception as e:
                logger.error(f"yt-dlp info extraction failed: {e}")
                raise

    def download_video(self, url, outdir: Path, progress_state: dict) -> Path:
        """Download video using yt-dlp with hanime plugin support"""
        ydl_opts = {
            "format": "best[ext=mp4]/best",
            "outtmpl": str(outdir / "%(title).200s.%(ext)s"),
            "http_chunk_size": 10 * 1024 * 1024,
            "noplaylist": True,
            "quiet": False,
            "no_warnings": False,
            "hls_use_mpegts": True,
            "merge_output_format": "mp4",
            "http_headers": {
                "User-Agent": USER_AGENT,
                "Referer": REFERER,
                "Origin": ORIGIN,
            },
            # Hanime.tv specific options
            "extractor_args": {
                "hanimetv": {
                    "skip_download": False
                }
            }
        }

        def progress_hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                if total and total > 0:
                    progress_state["percent"] = min(100, int(downloaded * 100 / total))
                    progress_state["downloaded_mb"] = downloaded / 1024 / 1024
                    progress_state["total_mb"] = total / 1024 / 1024
                    progress_state["speed"] = d.get('speed', 0)
                    progress_state["eta"] = d.get('eta', 0)
            elif d.get("status") == "finished":
                progress_state["percent"] = 100
                progress_state["downloaded_mb"] = progress_state.get("total_mb", 0)

        ydl_opts["progress_hooks"] = [progress_hook]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                # Extract info first
                info = ydl.extract_info(url, download=False)
                logger.info(f"🎯 Video found: {info.get('title', 'Unknown')}")
                logger.info(f"📺 Duration: {info.get('duration', 'Unknown')}s")
                logger.info(f"🎞️ Formats: {len(info.get('formats', []))}")
                
                # Download the video
                ydl.download([url])
                
                # Get the downloaded file path
                path = Path(ydl.prepare_filename(info))
                
                # Handle different file extensions
                if not path.exists():
                    for ext in ['.mp4', '.mkv', '.webm', '.m4a', '.ts']:
                        test_path = path.with_suffix(ext)
                        if test_path.exists():
                            path = test_path
                            break
                
                if not path.exists():
                    # Try to find any recently created video files
                    download_files = list(outdir.glob("*"))
                    if download_files:
                        # Get the most recently modified file
                        download_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                        path = download_files[0]
                
                return path
                
            except Exception as e:
                logger.error(f"Download failed: {e}")
                raise

async def send_large_file(context, chat_id, file_path, status_msg, max_size=MAX_SEND_BYTES):
    """Handle large file sending with progress"""
    if not file_path.exists():
        await status_msg.edit_text("❌ Downloaded file not found")
        return False
        
    file_size = file_path.stat().st_size
    
    if file_size > max_size:
        await status_msg.edit_text(f"❌ File too large ({file_size/1024/1024:.2f}MB > {max_size/1024/1024:.2f}MB).")
        return False
    
    try:
        await status_msg.edit_text(f"📤 Uploading {file_path.name}...")
        
        async with aiofiles.open(file_path, 'rb') as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=file_path.name,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=120,
                caption=f"🎬 {file_path.stem}"
            )
        return True
    except Exception as e:
        logger.error(f"Failed to send file: {e}")
        await status_msg.edit_text(f"❌ Failed to send file: {e}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        await update.message.reply_text("This bot is private.")
        return
    
    # System info
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    message = (
        "🎉 Hanime Bot Ready!\n"
        "Use /random to fetch a random video\n\n"
        f"💾 Memory: {memory.percent}% used\n"
        f"💿 Disk: {disk.percent}% used\n"
        f"📁 Temp: {get_download_dir()}\n"
        f"🔌 Hanime Plugin: {'✅ Available' if HANIME_PLUGIN_AVAILABLE else '❌ Not Available'}"
    )
    
    await update.message.reply_text(message)

async def random_hanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        await update.message.reply_text("This bot is private.")
        return

    status_msg = await update.message.reply_text("🔎 Fetching random video...")

    try:
        downloader = HanimeDownloader()
        
        # Step 1: Get random video URL
        await status_msg.edit_text("🎲 Getting random video page...")
        video_url = downloader.get_random_video_page()
        
        logger.info(f"Random video URL: {video_url}")
        await status_msg.edit_text(f"🔗 Found: {video_url}")

        # Step 2: Get video info
        await status_msg.edit_text("📋 Getting video information...")
        try:
            video_info = downloader.get_video_info(video_url)
            title = video_info.get('title', 'Unknown Title')
            duration = video_info.get('duration', 0)
            
            duration_text = f"{duration//60}:{duration%60:02d}" if duration else "Unknown"
            await status_msg.edit_text(f"🎬 {title}\n⏱️ Duration: {duration_text}\n⬇️ Starting download...")
        except Exception as e:
            logger.warning(f"Couldn't get video info: {e}")
            await status_msg.edit_text(f"🎬 Starting download...\n⚠️ Couldn't get video info: {e}")

        # Step 3: Download video
        progress_state = {
            "percent": 0, 
            "downloaded_mb": 0, 
            "total_mb": 0, 
            "speed": 0,
            "eta": 0
        }
        
        def run_download():
            return downloader.download_video(video_url, get_download_dir(), progress_state)

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, run_download)

        # Progress monitoring
        last_update = time.time()
        last_percent = -1
        
        while not future.done():
            await asyncio.sleep(2)
            current_time = time.time()
            pct = progress_state.get("percent", 0)
            downloaded_mb = progress_state.get("downloaded_mb", 0)
            total_mb = progress_state.get("total_mb", 0)
            speed = progress_state.get("speed", 0)
            eta = progress_state.get("eta", 0)
            
            # Update every 5 seconds or when percentage changes significantly
            if current_time - last_update >= 5 or abs(pct - last_percent) >= 10:
                speed_text = f" | 🚀 {speed/1024/1024:.1f}MB/s" if speed else ""
                eta_text = f" | ⏳ {eta}s" if eta else ""
                progress_text = (
                    f"⬇️ Downloading: {pct}%\n"
                    f"📊 {downloaded_mb:.1f}MB / {total_mb:.1f}MB"
                    f"{speed_text}{eta_text}"
                )
                
                try:
                    await status_msg.edit_text(progress_text)
                    last_update = current_time
                    last_percent = pct
                except Exception as e:
                    logger.warning(f"Failed to update progress: {e}")

        # Step 4: Get the downloaded file
        path = await future
        if not path or not path.exists():
            await status_msg.edit_text("❌ Download failed - no file found")
            return

        file_size = path.stat().st_size
        
        await status_msg.edit_text(
            f"✅ Download complete!\n"
            f"📦 File: {path.name}\n"
            f"💾 Size: {file_size/1024/1024:.2f}MB\n"
            f"📤 Uploading..."
        )

        # Step 5: Send the file
        success = await send_large_file(context, CHAT_ID, path, status_msg)
        
        if success:
            await status_msg.edit_text(f"🎉 Successfully sent: {path.name}")
        else:
            await status_msg.edit_text("❌ Failed to send file")

        # Cleanup
        try:
            if path.exists():
                path.unlink()
                logger.info(f"Cleaned up: {path}")
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")

    except Exception as e:
        logger.exception("Error in random_hanime")
        error_msg = f"❌ Error: {str(e)}"
        if len(error_msg) > 4000:
            error_msg = error_msg[:4000] + "..."
        await status_msg.edit_text(error_msg)

def main():
    # Check plugin status
    if not HANIME_PLUGIN_AVAILABLE:
        logger.warning("Hanime TV plugin not available - some features may not work")
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_hanime))
    
    logger.info("Bot started!")
    logger.info(f"Hanime plugin: {'Available' if HANIME_PLUGIN_AVAILABLE else 'Not available'}")
    
    app.run_polling()

if __name__ == "__main__":
    main()
