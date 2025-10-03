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
from PIL import Image
import cloudscraper
from hanime_tv_plugin import HanimeTVExtractor  # Make sure this is properly installed
from playwright.async_api import async_playwright

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

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def extract_video_url_with_playwright(url: str) -> dict:
    """
    Use Playwright to properly extract video URLs from hanime.tv
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={'width': 1920, 'height': 1080}
        )
        page = await context.new_page()
        
        try:
            # Navigate to the page
            await page.goto(url, wait_until='networkidle', timeout=30000)
            
            # Wait for video player to load
            await page.wait_for_selector('video', timeout=15000)
            
            # Extract video information
            video_data = {}
            
            # Get page title
            video_data['title'] = await page.title()
            
            # Try to extract m3u8 URLs from network requests
            m3u8_urls = []
            
            def handle_request(request):
                if '.m3u8' in request.url:
                    m3u8_urls.append(request.url)
            
            page.on('request', handle_request)
            
            # Wait a bit for additional requests
            await asyncio.sleep(3)
            
            if m3u8_urls:
                video_data['video_url'] = m3u8_urls[-1]  # Use the last m3u8 URL
            
            # If no m3u8 found via requests, try to extract from video element
            if not video_data.get('video_url'):
                video_url = await page.evaluate('''() => {
                    const video = document.querySelector('video');
                    if (video && video.src) {
                        return video.src;
                    }
                    return null;
                }''')
                
                if video_url and '.m3u8' in video_url:
                    video_data['video_url'] = video_url
            
            # Extract thumbnail if available
            thumbnail = await page.evaluate('''() => {
                const meta = document.querySelector('meta[property="og:image"]');
                return meta ? meta.content : null;
            }''')
            
            if thumbnail:
                video_data['thumbnail'] = thumbnail
            
            return video_data
            
        finally:
            await browser.close()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_with_hanime_plugin(url: str) -> dict:
    """
    Use the hanime-tv-plugin to extract video information
    """
    try:
        extractor = HanimeTVExtractor()
        result = extractor.extract(url)
        return result
    except Exception as e:
        logger.error(f"Hanime plugin failed: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def extract_with_cloudscraper(url: str) -> str:
    """
    Use cloudscraper to bypass Cloudflare protection
    """
    scraper = cloudscraper.create_scraper()
    response = scraper.get(url, timeout=30)
    response.raise_for_status()
    return response.text

def extract_video_url_from_html(html_content: str, page_url: str) -> str:
    """
    Enhanced HTML parsing for video URLs
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Method 1: Look for video player iframe/src
    iframe = soup.find('iframe', {'src': True})
    if iframe and 'player.hanime.tv' in iframe['src']:
        return iframe['src']
    
    # Method 2: Look for m3u8 URLs in script tags with more patterns
    scripts = soup.find_all('script')
    for script in scripts:
        if script.string:
            content = script.string
            
            # Look for various m3u8 URL patterns
            patterns = [
                r'https?://[^\s"\']+\.m3u8[^\s"\']*',
                r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
                r'source\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
                r'url\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
                r'video_url\s*:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches:
                    if '.m3u8' in match:
                        return match
    
    # Method 3: Look for video data in JSON structures
    for script in scripts:
        if script.string:
            content = script.string
            # Look for JSON objects containing video data
            json_patterns = [
                r'\{[^{}]*"url"[^{}]*\.m3u8[^{}]*\}',
                r'\{[^{}]*"video_url"[^{}]*\.m3u8[^{}]*\}',
                r'\{[^{}]*"src"[^{}]*\.m3u8[^{}]*\}',
                r'\{[^{}]*"source"[^{}]*\.m3u8[^{}]*\}',
            ]
            
            for pattern in json_patterns:
                matches = re.findall(pattern, content)
                for match in matches:
                    try:
                        data = json.loads(match)
                        for key in ['url', 'video_url', 'src', 'source']:
                            if key in data and '.m3u8' in data[key]:
                                return data[key]
                    except:
                        continue
    
    return page_url

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
            "hanime": {
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
            # Try direct download as fallback
            try:
                ydl.download([url])
                # Try to find the downloaded file
                for file in outdir.iterdir():
                    if file.is_file() and file.stat().st_size > 0:
                        return file
            except Exception as e2:
                logger.error(f"Direct download also failed: {e2}")
                raise e

async def send_large_file(context, chat_id, file_path, status_msg, max_size=MAX_SEND_BYTES):
    """
    Handle large file sending with progress
    """
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
                read_timeout=60,
                write_timeout=60,
                connect_timeout=60
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
        # Step 1: Get random video page using cloudscraper
        await status_msg.edit_text("🛡️ Bypassing protection...")
        scraper = cloudscraper.create_scraper()
        response = scraper.get("https://hanime.tv/browse/random", allow_redirects=True, timeout=30)
        response.raise_for_status()
        video_page_url = response.url
        
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.find("title").text.strip() if soup.find("title") else "Random Video"
        
        await status_msg.edit_text(f"🎲 Selected: {title}\n🔍 Extracting video URL...")

        # Step 2: Try multiple methods to extract video URL
        video_url = None
        video_info = {}
        
        # Method 1: Try hanime plugin
        try:
            await status_msg.edit_text("🔧 Trying hanime plugin...")
            video_info = extract_with_hanime_plugin(video_page_url)
            if video_info.get('url'):
                video_url = video_info['url']
        except Exception as e:
            logger.warning(f"Hanime plugin failed: {e}")
        
        # Method 2: Try playwright
        if not video_url:
            try:
                await status_msg.edit_text("🌐 Using browser automation...")
                video_info = await extract_video_url_with_playwright(video_page_url)
                if video_info.get('video_url'):
                    video_url = video_info['video_url']
            except Exception as e:
                logger.warning(f"Playwright failed: {e}")
        
        # Method 3: Try HTML parsing
        if not video_url:
            try:
                await status_msg.edit_text("📄 Parsing HTML...")
                video_url = extract_video_url_from_html(response.text, video_page_url)
            except Exception as e:
                logger.warning(f"HTML parsing failed: {e}")
        
        if not video_url:
            video_url = video_page_url
        
        await status_msg.edit_text(f"🎯 Video URL found!\n📹 Starting download...")

        # Step 3: Download with yt-dlp
        progress_state = {"percent": 0, "downloaded_mb": 0, "total_mb": 0, "speed": 0}
        
        def run_download():
            return yt_dlp_download_blocking(video_url, get_download_dir(), progress_state)

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

        # Step 4: Get the downloaded file
        path = await future
        file_size = path.stat().st_size
        
        await status_msg.edit_text(f"✅ Download complete!\n📦 File: {path.name}\n💾 Size: {file_size/1024/1024:.2f}MB\n📤 Uploading...")

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
    # Check if hanime plugin is available
    try:
        from hanime_tv_plugin import HanimeTVExtractor
        logger.info("HanimeTV plugin loaded successfully")
    except ImportError:
        logger.warning("HanimeTV plugin not available, falling back to other methods")
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_hanime))
    
    logger.info("Bot started with enhanced hanime.tv support!")
    app.run_polling()

if __name__ == "__main__":
    main()