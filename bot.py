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

class HanimeExtractor:
    def __init__(self):
        self.session = cloudscraper.create_scraper()
        self.session.headers.update({
            'User-Agent': USER_AGENT,
            'Referer': REFERER,
            'Origin': ORIGIN,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
        })

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def get_random_video_url(self):
        """Get a random video URL from hanime.tv"""
        try:
            response = self.session.get("https://hanime.tv/browse/random", timeout=30)
            response.raise_for_status()
            return response.url
        except Exception as e:
            logger.error(f"Failed to get random video: {e}")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def extract_video_data(self, video_url):
        """Extract video data from hanime.tv page"""
        try:
            response = self.session.get(video_url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Extract title
            title = soup.find('title')
            title_text = title.text.strip() if title else "Unknown Title"
            
            # Look for video data in various places
            video_data = {
                'title': title_text,
                'url': video_url,
                'video_urls': []
            }
            
            # Method 1: Look for iframe embeds
            iframes = soup.find_all('iframe', {'src': True})
            for iframe in iframes:
                if 'hanime.tv' in iframe['src'] or 'player' in iframe['src']:
                    video_data['video_urls'].append(iframe['src'])
            
            # Method 2: Look for video tags
            video_tags = soup.find_all('video', {'src': True})
            for video in video_tags:
                video_data['video_urls'].append(video['src'])
            
            # Method 3: Look for source tags inside video
            sources = soup.find_all('source', {'src': True})
            for source in sources:
                video_data['video_urls'].append(source['src'])
            
            # Method 4: Look for m3u8 in script tags
            scripts = soup.find_all('script')
            for script in scripts:
                if script.string:
                    # Look for m3u8 URLs
                    m3u8_patterns = [
                        r'https?://[^\s"\']+\.m3u8[^\s"\']*',
                        r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
                        r'source\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
                    ]
                    
                    for pattern in m3u8_patterns:
                        matches = re.findall(pattern, script.string, re.IGNORECASE)
                        for match in matches:
                            if '.m3u8' in match:
                                video_data['video_urls'].append(match)
            
            # Method 5: Look for JSON data with video info
            for script in scripts:
                if script.string and 'video' in script.string.lower():
                    try:
                        # Look for JSON objects
                        json_patterns = [
                            r'\{[^{}]*"url"[^{}]*\.m3u8[^{}]*\}',
                            r'\{[^{}]*"sources"[^{}]*\}',
                            r'\{[^{}]*"video"[^{}]*\}',
                        ]
                        
                        for pattern in json_patterns:
                            matches = re.findall(pattern, script.string)
                            for match in matches:
                                try:
                                    data = json.loads(match)
                                    self._extract_from_json(data, video_data)
                                except:
                                    continue
                    except:
                        continue
            
            # Method 6: Look for API endpoints
            api_patterns = [
                r'https?://[^\s"\']+\.json[^\s"\']*',
                r'https?://[^\s"\']+/api/[^\s"\']*',
            ]
            
            for script in scripts:
                if script.string:
                    for pattern in api_patterns:
                        matches = re.findall(pattern, script.string)
                        for match in matches:
                            if 'hanime' in match or 'video' in match:
                                video_data['video_urls'].append(match)
            
            return video_data
            
        except Exception as e:
            logger.error(f"Failed to extract video data: {e}")
            raise

    def _extract_from_json(self, data, video_data):
        """Recursively extract video URLs from JSON data"""
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and ('.m3u8' in value or '.mp4' in value):
                    video_data['video_urls'].append(value)
                elif isinstance(value, (dict, list)):
                    self._extract_from_json(value, video_data)
        elif isinstance(data, list):
            for item in data:
                self._extract_from_json(item, video_data)

    def get_best_video_url(self, video_data):
        """Get the best video URL from extracted data"""
        if not video_data['video_urls']:
            return video_data['url']  # Fallback to original URL
        
        # Prioritize m3u8 URLs
        m3u8_urls = [url for url in video_data['video_urls'] if '.m3u8' in url]
        if m3u8_urls:
            return m3u8_urls[0]
        
        # Then mp4 URLs
        mp4_urls = [url for url in video_data['video_urls'] if '.mp4' in url]
        if mp4_urls:
            return mp4_urls[0]
        
        # Return the first URL found
        return video_data['video_urls'][0]

def yt_dlp_download_blocking(url: str, outdir: Path, progress_state: dict) -> Path:
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
        "extractor_args": {
            "generic": {
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
        elif d.get("status") == "finished":
            progress_state["percent"] = 100
            progress_state["downloaded_mb"] = progress_state.get("total_mb", 0)

    ydl_opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # First try to get info without downloading
            info = ydl.extract_info(url, download=False)
            logger.info(f"Video info: {info.get('title', 'Unknown')}")
            
            # Now download
            ydl.download([url])
            path = Path(ydl.prepare_filename(info))
            
            if not path.exists():
                # Try different extensions
                for ext in ['.mp4', '.mkv', '.webm', '.m4a']:
                    test_path = path.with_suffix(ext)
                    if test_path.exists():
                        path = test_path
                        break
            
            return path
            
        except Exception as e:
            logger.error(f"yt-dlp extraction failed: {e}")
            raise

async def send_large_file(context, chat_id, file_path, status_msg, max_size=MAX_SEND_BYTES):
    """Handle large file sending with progress"""
    file_size = file_path.stat().st_size
    
    if file_size > max_size:
        await status_msg.edit_text(f"❌ File too large ({file_size/1024/1024:.2f}MB > {max_size/1024/1024:.2f}MB).")
        return False
    
    try:
        async with aiofiles.open(file_path, 'rb') as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=file_path.name,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=120
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
        f"📁 Temp: {get_download_dir()}"
    )
    
    await update.message.reply_text(message)

async def random_hanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        await update.message.reply_text("This bot is private.")
        return

    status_msg = await update.message.reply_text("🔎 Fetching random video...")

    try:
        # Initialize extractor
        extractor = HanimeExtractor()
        
        # Step 1: Get random video URL
        await status_msg.edit_text("🎲 Getting random video...")
        video_url = extractor.get_random_video_url()
        
        # Step 2: Extract video data
        await status_msg.edit_text("🔍 Extracting video information...")
        video_data = extractor.extract_video_data(video_url)
        
        # Step 3: Get the best video URL
        best_url = extractor.get_best_video_url(video_data)
        
        await status_msg.edit_text(
            f"📹 Found: {video_data['title']}\n"
            f"🔗 URLs found: {len(video_data['video_urls'])}\n"
            f"⬇️ Starting download..."
        )

        # Step 4: Download with yt-dlp
        progress_state = {"percent": 0, "downloaded_mb": 0, "total_mb": 0, "speed": 0}
        
        def run_download():
            return yt_dlp_download_blocking(best_url, get_download_dir(), progress_state)

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, run_download)

        # Progress monitoring
        last_update = time.time()
        last_percent = -1
        
        while not future.done():
            current_time = time.time()
            pct = progress_state.get("percent", 0)
            downloaded_mb = progress_state.get("downloaded_mb", 0)
            total_mb = progress_state.get("total_mb", 0)
            speed = progress_state.get("speed", 0)
            
            # Update every 3 seconds or when percentage changes significantly
            if current_time - last_update >= 3 or abs(pct - last_percent) >= 5:
                speed_text = f" | {speed/1024/1024:.1f} MB/s" if speed else ""
                progress_text = (
                    f"⬇️ Downloading: {pct}%\n"
                    f"📊 {downloaded_mb:.1f}MB / {total_mb:.1f}MB{speed_text}"
                )
                
                try:
                    await status_msg.edit_text(progress_text)
                    last_update = current_time
                    last_percent = pct
                except Exception as e:
                    logger.warning(f"Failed to update progress: {e}")
            
            await asyncio.sleep(1)

        # Step 5: Get the downloaded file
        path = await future
        file_size = path.stat().st_size
        
        await status_msg.edit_text(f"✅ Download complete!\n📦 File: {path.name}\n💾 Size: {file_size/1024/1024:.2f}MB\n📤 Uploading...")

        # Step 6: Send the file
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
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_hanime))
    
    logger.info("Bot started with enhanced hanime.tv support!")
    app.run_polling()

if __name__ == "__main__":
    main()