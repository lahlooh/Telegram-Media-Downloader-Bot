import os
import shutil
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)
logger = logging.getLogger("bot.config")

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
DEV_CREDIT: str = "Developed By discord:- 4.7e"

MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "48"))
MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / os.getenv("DOWNLOAD_DIR", "downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

_cookies_env = os.getenv("COOKIES_FILE", "cookies.txt")
COOKIES_PATH: Path | None = BASE_DIR / _cookies_env if _cookies_env else None
if COOKIES_PATH and not COOKIES_PATH.is_file():
    COOKIES_PATH = None


def get_ffmpeg_path() -> str | None:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and os.path.exists(bundled):
            return bundled
    except Exception as e:
        logger.debug(f"imageio-ffmpeg error: {e}")

    logger.warning("FFmpeg not found in system or bundled imageio.")
    return None


FFMPEG_PATH = get_ffmpeg_path()

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HTTP_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

TIKTOK_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

TIKTOK_HTTP_HEADERS = {
    "User-Agent": TIKTOK_MOBILE_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Sec-Fetch-Mode": "navigate",
}
