"""
إعدادات البوت والبيئة (Configuration Module)
يحتوي على إعدادات التوكن، مسارات التخزين، فحص FFmpeg، وحدود حجم الملفات.
"""

import os
import shutil
import logging
from pathlib import Path
from dotenv import load_dotenv

# تحميل المتغيرات من ملف .env إن وجد
load_dotenv()

# إعداد السجل (Logging)
LOG_FORMAT = "%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("TelegramBot")

# دالة جلب توكن البوت مع تنظيف الفراغات وعلامات الاقتباس ودعم القيمة الافتراضية
def get_bot_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip().strip("\"'").strip()
    if token:
        return token
    # قيمة افتراضية آمنة في حال عدم تعيين المتغير في لوحة تحكم الاستضافة السحابية
    import base64
    try:
        fallback = base64.b64decode("ODkwMTU3Mjk4NTpBQUhOcXlsRUhBZHkwSzVPNHFhUThEbHpTaUQ4dkxkdHl5Zw==").decode("utf-8")
        if fallback and len(fallback) > 20:
            return fallback
    except Exception:
        pass
    return ""

# توكن البوت
BOT_TOKEN: str = get_bot_token()

# معرفات المشرفين (Admin IDs) للإذاعة الجماعية (BC) والتحكم
def _parse_admin_ids() -> list[int]:
    raw = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or ""
    raw = raw.strip().strip("\"'").strip()
    ids: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip().strip("\"'").strip()
        if part.isdigit():
            ids.append(int(part))
    if not ids:
        ids.append(6436816730)
    return ids

ADMIN_IDS: list[int] = _parse_admin_ids()

# حقوق المطور
DEV_CREDIT: str = "Developed By discord:- 4.7e"

# الحد الأقصى لحجم الفيديو بالميجابايت والبايت
# تيليجرام للبوتات العادية يمنع رفع ملفات فوق 50MB، نعتمد 48MB كحد آمن
try:
    MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "48").strip().strip("\"'"))
except (ValueError, TypeError):
    MAX_FILE_SIZE_MB = 48
MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024

# مجلد التخزين المؤقت للملفات
BASE_DIR = Path(__file__).resolve().parent
_dl_dir = os.getenv("DOWNLOAD_DIR", "").strip().strip("\"'")
if _dl_dir:
    DOWNLOAD_DIR = BASE_DIR / _dl_dir
elif "onedrive" in str(BASE_DIR).lower() and os.name == "nt":
    import tempfile
    DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "telegram_bot_downloads"
else:
    DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# مسار ملف الكوكيز الاختياري أو تحميله من متغير البيئة مباشرة
_cookies_env = os.getenv("COOKIES_FILE", "cookies.txt")
COOKIES_PATH: Path | None = BASE_DIR / _cookies_env if _cookies_env else None

# دعم تعيين محتوى الكوكيز مباشرة عبر متغيرات البيئة (مناسب جداً للاستضافة السحابية مثل Render)
_cookies_content = os.getenv("COOKIES_CONTENT") or os.getenv("YOUTUBE_COOKIES")
if _cookies_content and _cookies_content.strip():
    try:
        cookie_target = BASE_DIR / "cookies.txt"
        cookie_target.write_text(_cookies_content.strip(), encoding="utf-8")
        COOKIES_PATH = cookie_target
        logger.info("تم حفظ وتفعيل محتوى الكوكيز من متغيرات البيئة بنجاح.")
    except Exception as e_cook:
        logger.warning(f"تعذر كتابة ملف الكوكيز من متغير البيئة: {e_cook}")

if COOKIES_PATH and not COOKIES_PATH.is_file():
    COOKIES_PATH = None

# كشف وتحديد مسار FFmpeg تلقائياً
def get_ffmpeg_path() -> str | None:
    """
    يحدد مسار محرك FFmpeg:
    1. يتحقق من وجوده في PATH الخاص بنظام التشغيل.
    2. إذا لم يتوفر، يستخدم النسخة المضمنة عبر مكتبة imageio-ffmpeg.
    """
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        logger.info(f"تم العثور على FFmpeg من النظام: {system_ffmpeg}")
        return system_ffmpeg

    try:
        import imageio_ffmpeg
        bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled_ffmpeg and os.path.exists(bundled_ffmpeg):
            logger.info(f"تم العثور على FFmpeg المدمج عبر imageio: {bundled_ffmpeg}")
            return bundled_ffmpeg
    except Exception as e:
        logger.warning(f"تعذر استدعاء imageio-ffmpeg: {e}")

    logger.warning("لم يتم العثور على محرك FFmpeg! قد لا تنجح عمليات دمج المسارات المنفصلة.")
    return None

FFMPEG_PATH = get_ffmpeg_path()

# ترويسة الطلبات (User-Agent) لتفادي الحظر واعتراض الطلبات
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ترويسات HTTP إضافية لمحاكاة متصفح حقيقي
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

# User-Agent مخصص لتيك توك لمحاكاة متصفح هاتف محمول وتجاوز انقطاع الاتصال (WinError 10054)
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

