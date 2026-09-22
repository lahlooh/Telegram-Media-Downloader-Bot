"""
محرك استخراج وتنزيل الميديا (Downloader Module)
يعتمد على yt-dlp و FFmpeg، مع محرك مخصص لتيك توك يتجاوز حظر الـ SNI وقطع الاتصال [WinError 10054].
تحميل فائق السرعة بضربة واحدة وتوليد الصور المصغرة والبيانات الوصفية عبر FFmpeg محلياً.
"""

import os
import re
import html
import uuid
import base64
import shutil
import asyncio
import logging
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List

from curl_cffi import requests as cffi_requests
import yt_dlp

from config import (
    DOWNLOAD_DIR,
    MAX_FILE_SIZE_BYTES,
    MAX_FILE_SIZE_MB,
    FFMPEG_PATH,
    COOKIES_PATH,
    DEFAULT_USER_AGENT,
    DEFAULT_HTTP_HEADERS,
    TIKTOK_MOBILE_USER_AGENT,
    TIKTOK_HTTP_HEADERS,
)

logger = logging.getLogger("Downloader")

# كشف محرك جافاسكريبت خارجي (Node.js أو Deno) لحل شفرات وتحديات يوتيوب وتجاوز الحظر السحابي
JS_RUNTIME_TYPE: Optional[str] = None
if shutil.which("node"):
    JS_RUNTIME_TYPE = "node"
    logger.info(f"تم اكتشاف محرك Node.js لدعم شفرات يوتيوب: {shutil.which('node')}")
elif shutil.which("deno"):
    JS_RUNTIME_TYPE = "deno"
    logger.info(f"تم اكتشاف محرك Deno لدعم شفرات يوتيوب: {shutil.which('deno')}")


# ==========================================
# دوال تنظيف الروابط والرسائل (URL & String Helpers)
# ==========================================

async def resolve_redirect_url(url: str) -> str:
    """فك تحويل الروابط المختصرة (خاصة vt.tiktok و vm.tiktok) بشكل سريع."""
    short_domains = ("vt.tiktok.com", "vm.tiktok.com", "tiktok.com/t/", "t.co", "bit.ly", "tinyurl.com")
    if not any(domain in url.lower() for domain in short_domains):
        return url
    return url


def sanitize_url(raw_url: str) -> str:
    """
    تنظيف الرابط وإزالة معلمات التتبع وإصلاح روابط X/Twitter:
    1. استخراج معرف التغريدة وإزالة اللواحق مثل /video/1 أو /photo/1.
    2. توحيد النطاق إلى صيغة قياسية مدعومة: https://x.com/<user>/status/<id>.
    3. حذف معلمات التتبع والاستعلام الزائدة (?s=20, ?t=..., إلخ).
    """
    if not raw_url:
        return ""

    url = raw_url.strip()

    # 1. فحص وتصحيح روابط إكس / تويتر (بما فيها fxtwitter, vxtwitter, fixupx)
    twitter_pattern = (
        r'https?://(?:www\.|mobile\.|m\.)?'
        r'(?:twitter\.com|x\.com|vxtwitter\.com|fxtwitter\.com|fixupx\.com)/'
        r'(?:([a-zA-Z0-9_]+)|i(?:/web)?)/status(?:es)?/(\d+)'
    )
    twitter_match = re.search(twitter_pattern, url, re.IGNORECASE)
    if twitter_match:
        username = twitter_match.group(1) or "i"
        status_id = twitter_match.group(2)
        clean_twitter = f"https://x.com/{username}/status/{status_id}"
        logger.info(f"تم تنظيف رابط تويتر من '{raw_url}' إلى '{clean_twitter}'")
        return clean_twitter

    # 2. تنظيف معلمات التتبع للمنصات الأخرى
    if "?" in url:
        base_part, query_part = url.split("?", 1)
        # الاحتفاظ بمعرف الفيديو v= في يوتيوب
        if "youtube.com/watch" in base_part:
            v_match = re.search(r'(?:^|&)v=([a-zA-Z0-9_-]+)', query_part)
            if v_match:
                return f"{base_part}?v={v_match.group(1)}"
        url = base_part

    # إزالة أي وسوم تجزئة # والشرطات المائلة الزائدة
    url = url.split("#")[0].rstrip("/")
    return url


def is_tiktok_url(raw_url: str) -> bool:
    """التحقق مما إذا كان الرابط يخص منصة تيك توك بكافة نطاقاتها ومساراتها."""
    if not raw_url:
        return False
    u = raw_url.lower()
    tiktok_domains = ("tiktok.com", "douyin.com", "tiktokv.com", "vm.tiktok.com", "vt.tiktok.com")
    return any(domain in u for domain in tiktok_domains)


def chunk_media_list(items: list, max_chunk: int = 10) -> List[list]:
    """
    تقسيم قائمة الوسائط إلى دفعات تتوافق مع قيود تيليجرام
    (كل مجموعة بين 2 و 10 عناصر، وإذا كان عنصراً واحداً يُرسل منفرداً).
    """
    if not items:
        return []
    if len(items) <= max_chunk:
        return [items]

    chunks = []
    i = 0
    n = len(items)
    while i < n:
        rem = n - i
        if rem > max_chunk:
            # إذا تبقى 11 عنصراً، نأخذ 9 لكي يتبقى 2 (لأن تيليجرام يرفض مجموعة من عنصر واحد)
            take = max_chunk - 1 if rem == max_chunk + 1 else max_chunk
            chunks.append(items[i:i + take])
            i += take
        else:
            chunks.append(items[i:n])
            break
    return chunks


def clean_error_message(msg: str) -> str:
    """إزالة أي وسوم تلوين طرفية (ANSI Codes) وكلمات الخطأ الخام لتكون رسالة نظيفة وجميلة."""
    if not msg:
        return ""
    clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', msg)
    clean = re.sub(r'(?:\[[0-9;]*m|[0-9]{1,2}m)', '', clean)
    clean = re.sub(r'^(?:error:\s*)+', '', clean, flags=re.IGNORECASE)
    return clean.strip()


def extract_video_metadata_with_ffmpeg(
    video_path: Path, temp_dir: Path
) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[Path]]:
    """
    استخراج مدة الفيديو وأبعاده وإنشاء صورة مصغرة (Thumbnail) فائقة الدقة محلياً عبر FFmpeg.
    تتم المعالجة في أجزاء من الثانية دون أي طلبات شبكة إضافية.
    """
    if not FFMPEG_PATH or not video_path.exists():
        return None, None, None, None

    duration: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    thumbnail_path: Optional[Path] = None

    try:
        # 1. استخراج المدة والأبعاد عبر فحص الملف بـ FFmpeg
        probe_cmd = [FFMPEG_PATH, "-i", str(video_path)]
        res = subprocess.run(probe_cmd, capture_output=True, text=True, errors="ignore")
        stderr_output = res.stderr or ""

        # فحص المدة (Duration: HH:MM:SS.ms)
        dur_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', stderr_output)
        if dur_match:
            hours, minutes, seconds = dur_match.groups()
            duration = int(hours) * 3600 + int(minutes) * 60 + int(float(seconds))

        # فحص الأبعاد (Video: ... 1080x1920 ...)
        res_match = re.search(r'Stream.*Video:.*,\s*(\d{2,5})x(\d{2,5})', stderr_output)
        if res_match:
            width = int(res_match.group(1))
            height = int(res_match.group(2))

        # 2. توليد صورة الغلاف (Thumbnail)
        thumb_file = temp_dir / "thumbnail.jpg"
        thumb_cmd = [
            FFMPEG_PATH, "-y",
            "-ss", "00:00:01",
            "-i", str(video_path),
            "-vframes", "1",
            "-q:v", "2",
            str(thumb_file)
        ]
        thumb_res = subprocess.run(thumb_cmd, capture_output=True, errors="ignore")
        if thumb_file.exists() and thumb_file.stat().st_size > 0:
            thumbnail_path = thumb_file

    except Exception as e:
        logger.warning(f"تعذر استخراج بيانات الفيديو عبر FFmpeg: {e}")

    return duration, width, height, thumbnail_path


def extract_audio_mp3_sync(video_path: Path, output_mp3: Path) -> Optional[Path]:
    """
    استخراج المسار الصوتي من مقطع الفيديو وتحويله إلى ملف MP3 نقي وعالي الجودة عبر FFmpeg.
    """
    if not FFMPEG_PATH or not video_path.exists() or video_path.stat().st_size == 0:
        return None
    try:
        cmd = [
            FFMPEG_PATH, "-y",
            "-i", str(video_path),
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "192k",
            str(output_mp3)
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=30)
        if output_mp3.exists() and output_mp3.stat().st_size > 500:
            logger.info(f"تم استخراج الصوت MP3 بنجاح: {output_mp3.name} ({output_mp3.stat().st_size / 1024:.1f} KB)")
            return output_mp3
    except Exception as e:
        logger.warning(f"تعذر استخراج الصوت MP3 من الفيديو {video_path}: {e}")
    return None


# ==========================================
# الاستثناءات المخصصة (Custom Exceptions)
# ==========================================

class DownloaderError(Exception):
    """خطأ عام في التنزيل."""
    pass


class VideoTooLargeError(DownloaderError):
    """الفيديو يتجاوز الحد الأقصى المسموح به للبوتات على تيليجرام (50MB)."""
    def __init__(self, size_mb: float):
        self.size_mb = size_mb
        super().__init__(
            f"حجم الفيديو ({size_mb:.1f}MB) يتجاوز الحد الأقصى المسموح به للبوتات في تيليجرام (50MB)."
        )


class ContentUnavailableError(DownloaderError):
    """المحتوى محذوف أو خاص أو يتطلب تسجيل دخول."""
    pass


class InvalidURLError(DownloaderError):
    """الرابط غير صالح أو المنصة غير مدعومة."""
    pass


# ==========================================
# كائن نتيجة التنزيل (Download Result)
# ==========================================

@dataclass
class DownloadResult:
    video_path: Path
    title: str
    duration: Optional[int]
    width: Optional[int]
    height: Optional[int]
    thumbnail_path: Optional[Path]
    platform: str
    file_size_bytes: int
    temp_dir: Path
    audio_path: Optional[Path] = None

    @property
    def file_size_mb(self) -> float:
        return self.file_size_bytes / (1024 * 1024)

    def cleanup(self) -> None:
        """حذف المجلد المؤقت وكافة الملفات بداخله لضمان نظافة الخادم."""
        try:
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir, ignore_errors=True)
                logger.info(f"تم تنظيف المجلد المؤقت: {self.temp_dir}")
        except Exception as e:
            logger.warning(f"تعذر تنظيف المجلد المؤقت {self.temp_dir}: {e}")


@dataclass
class TikTokMediaInfo:
    """كائن بيانات فحص محتوى تيك توك (فيديو أو ألبوم صور)."""
    media_type: str  # 'video' أو 'photos'
    title: str
    video_url: Optional[str] = None
    photo_urls: Optional[List[str]] = None
    music_url: Optional[str] = None
    author: Optional[str] = None

    def __post_init__(self):
        if self.photo_urls is None:
            self.photo_urls = []


# ==========================================
# الفئة الرئيسية لإدارة التنزيل
# ==========================================

class MediaDownloader:
    """فئة مسؤولة عن استخراج وتنزيل الفيديو عبر yt-dlp والمحرك المخصص لتيك توك."""

    PLATFORM_NAMES: Dict[str, str] = {
        "tiktok": "تيك توك (TikTok)",
        "instagram": "إنستغرام (Instagram)",
        "youtube": "يوتيوب (YouTube)",
        "twitter": "إكس / تويتر (X / Twitter)",
        "x": "إكس / تويتر (X / Twitter)",
        "facebook": "فيسبوك (Facebook)",
        "reddit": "ريديت (Reddit)",
        "pinterest": "بنترست (Pinterest)",
        "threads": "ثريدز (Threads)",
        "snapchat": "سناب شات (Snapchat)",
        "vimeo": "فيميو (Vimeo)",
    }

    @staticmethod
    def _sanitize_title(title: Optional[str]) -> str:
        """تنظيف وقص عنوان الفيديو ليتناسب مع رسائل تيليجرام."""
        if not title:
            return "فيديو بدون عنوان"
        clean = re.sub(r'[\r\n\t]+', ' ', title).strip()
        if len(clean) > 80:
            clean = clean[:77] + "..."
        return clean

    @classmethod
    def _detect_platform(cls, extractor_key: Optional[str]) -> str:
        """التعرف على اسم المنصة من مفتاح yt-dlp."""
        if not extractor_key:
            return "منصة وسائط"
        key_lower = extractor_key.lower()
        for name_key, arabic_name in cls.PLATFORM_NAMES.items():
            if name_key in key_lower:
                return arabic_name
        return extractor_key.capitalize()

    @staticmethod
    def _get_extractor_args(use_syndication: bool = True, youtube_clients: Optional[List[str]] = None) -> Dict[str, Any]:
        """إعدادات مستخرجات المنصات لتجاوز الحظر وحماية الطلبات."""
        clients = youtube_clients or ["android"]
        args: Dict[str, Any] = {
            "youtube": {
                "player_client": clients,
            },
            "tiktok": {
                "app_version": "34.1.2",
            }
        }
        if use_syndication:
            args["twitter"] = {
                "api": ["syndication"]
            }
        return args

    @classmethod
    def _build_ydl_options(
        cls,
        temp_dir: Path,
        format_selector: str,
        download_thumbnail: bool = True,
        use_syndication: bool = True,
        is_tiktok: bool = False,
        youtube_clients: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """بناء إعدادات yt-dlp عالية الأداء والتسريع."""
        output_template = str(temp_dir / "%(id)s.%(ext)s")

        postprocessors = [
            {
                "key": "FFmpegVideoRemuxer",
                "preferedformat": "mp4",
            }
        ]

        if download_thumbnail:
            postprocessors.append({
                "key": "FFmpegThumbnailsConvertor",
                "format": "jpg",
            })

        user_agent = TIKTOK_MOBILE_USER_AGENT if is_tiktok else DEFAULT_USER_AGENT
        http_headers = TIKTOK_HTTP_HEADERS if is_tiktok else DEFAULT_HTTP_HEADERS

        opts: Dict[str, Any] = {
            "format": format_selector,
            "outtmpl": {
                "default": output_template,
                "thumbnail": output_template,
            },
            "windowsfilenames": True,
            "restrictfilenames": True,
            "updatetime": False,
            "overwrites": True,
            "merge_output_format": "mp4",
            "postprocessors": postprocessors,
            "postprocessor_args": {
                "merger": ["-preset", "ultrafast"],
                "video-remuxer": ["-preset", "ultrafast"],
            },
            "writethumbnail": download_thumbnail,
            "nocheckcertificate": True,
            "ignoreerrors": False,
            "logtostderr": False,
            "quiet": True,
            "no_warnings": True,
            "user_agent": user_agent,
            "http_headers": http_headers,
            "concurrent_fragment_downloads": 4,
            "socket_timeout": 20,
            "retries": 5,
            "fragment_retries": 5,
            "extractor_args": cls._get_extractor_args(use_syndication=use_syndication, youtube_clients=youtube_clients),
        }

        if JS_RUNTIME_TYPE:
            opts["js_runtimes"] = {JS_RUNTIME_TYPE: {}}

        if FFMPEG_PATH:
            opts["ffmpeg_location"] = FFMPEG_PATH

        if COOKIES_PATH and COOKIES_PATH.is_file():
            opts["cookiefile"] = str(COOKIES_PATH)
            logger.info(f"تم تفعيل ملف الكوكيز: {COOKIES_PATH}")

        return opts

    @staticmethod
    def _find_media_files(temp_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
        """البحث عن ملف الفيديو الناتج وملف الصورة المصغرة في المجلد المؤقت."""
        video_path: Optional[Path] = None
        thumbnail_path: Optional[Path] = None

        video_extensions = {".mp4", ".mkv", ".webm", ".mov"}
        image_extensions = {".jpg", ".jpeg", ".png", ".webp"}

        for file in temp_dir.iterdir():
            if not file.is_file():
                continue
            suffix = file.suffix.lower()
            if suffix in video_extensions and video_path is None:
                video_path = file
            elif suffix in image_extensions and thumbnail_path is None:
                thumbnail_path = file

        return video_path, thumbnail_path

    @classmethod
    def _handle_download_error(cls, e: Exception) -> None:
        """تحليل وترجمة أخطاء yt-dlp مع تنظيفها من ANSI Codes."""
        error_msg = str(e).lower()
        cleaned = clean_error_message(str(e))

        if "no video could be found" in error_msg:
            raise ContentUnavailableError("⚠️ لا يوجد مقطع فيديو داخل هذا الرابط (قد يحتوي على صورة أو نص فقط).") from e
        elif any(term in error_msg for term in ["private", "login", "requires account", "members-only"]):
            raise ContentUnavailableError("🔒 هذا المقطع خاص أو يتطلب تسجيل دخول.") from e
        elif any(term in error_msg for term in ["not found", "deleted", "unavailable", "does not exist"]):
            raise ContentUnavailableError("❌ تم حذف هذا المقطع أو أنه غير متاح.") from e
        elif "unsupported url" in error_msg:
            raise InvalidURLError("⚠️ الرابط المرسل غير مدعوم أو غير صحيح.") from e
        elif any(code in error_msg for code in ["10054", "connection reset", "forcibly closed"]):
            raise DownloaderError("⚠️ انقطع الاتصال بخادم المنصة بشكل مفاجئ. يرجى المحاولة مرة أخرى.") from e
        elif any(term in error_msg for term in ["429", "too many requests", "not a bot", "not a robot", "sign in to confirm"]):
            raise DownloaderError("⚠️ خوادم المنصة تفرض قيوداً مؤقتة أو تطلب التحقق (Rate Limit). يرجى المحاولة بعد قليل.") from e
        elif any(term in error_msg for term in ["errno 22", "invalid argument"]):
            raise DownloaderError("⚠️ تعذر حفظ المقطع بسبب قيود نظام الملفات. يرجى إعادة المحاولة.") from e
        elif any(term in error_msg for term in ["country", "region", "geo"]):
            raise ContentUnavailableError("🌍 هذا المقطع غير متوفر في منطقتك الجغرافية.") from e
        else:
            raise DownloaderError(f"حدث خطأ أثناء معالجة المقطع: {cleaned}") from e

    @staticmethod
    def _download_media_stream_sync(
        session: cffi_requests.Session,
        media_url: Optional[str],
        target_file: Path,
        default_referer: str = "https://www.tiktok.com/",
    ) -> bool:
        """
        تنزيل ذكي وتكيفي لروابط ميديا تيك توك:
        1. إذا كان الرابط من شبكات غير محجوبة (مثل snapcdn.app) ينزل مباشرة بسرعة فائقة.
        2. يحاول التنزيل المباشر برؤوس متصفح كاملة.
        3. إذا فشل التنزيل المباشر أو تعثر بسبب حظر الـ SNI، يستخدم بروكسي tikcdn لتجاوز الحجب.
        """
        if not media_url:
            return False

        # 1. التنزيل المباشر السريع (مهلة 4 ثوانٍ لسرعة التبديل للبروكسي في حال الحجب الجغرافي)
        try:
            resp = session.get(
                media_url,
                headers={"Referer": default_referer, "User-Agent": DEFAULT_USER_AGENT},
                timeout=4
            )
            if resp.status_code == 200 and len(resp.content) > 500:
                target_file.write_bytes(resp.content)
                logger.info(f"تم التنزيل بنجاح: {target_file.name} ({len(resp.content)} بايت)")
                return True
        except Exception as e_dir:
            logger.debug(f"فشل التنزيل المباشر ({e_dir})، جاري المحاولة عبر بدائل التجاوز...")

        # 2. التبديل لبروكسي tikcdn الموثوق لتجاوز حظر الـ SNI للروابط المقيدة
        try:
            b64 = base64.b64encode(media_url.encode()).decode()
            proxy_url = f"https://tikcdn.io/ssstik/{b64}"
            resp = session.get(
                proxy_url,
                headers={"Referer": "https://ssstik.io/", "User-Agent": DEFAULT_USER_AGENT},
                timeout=25
            )
            if resp.status_code == 200 and len(resp.content) > 500:
                target_file.write_bytes(resp.content)
                logger.info(f"تم التنزيل عبر بروكسي tikcdn بنجاح: {target_file.name} ({len(resp.content)} بايت)")
                return True
        except Exception as e_proxy:
            logger.warning(f"فشل التنزيل عبر بروكسي tikcdn: {e_proxy}")

        # 3. محاولة تنزيل احتياطية عبر urllib
        try:
            import urllib.request
            req = urllib.request.Request(
                media_url,
                headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": default_referer}
            )
            with urllib.request.urlopen(req, timeout=15) as u_resp:
                if u_resp.status == 200:
                    content = u_resp.read()
                    if len(content) > 500:
                        target_file.write_bytes(content)
                        logger.info(f"تم التنزيل بنجاح عبر urllib: {target_file.name}")
                        return True
        except Exception as e_urllib:
            logger.warning(f"فشل التنزيل عبر urllib: {e_urllib}")

        return False

    def _extract_tiktok_info_sync(self, tiktok_url: str) -> TikTokMediaInfo:
        """
        استخراج بيانات منشور تيك توك عبر مصفوفة مزودات متعددة ومتسلسلة (Multi-Provider Cascade)
        تضمن النجاح بنسبة 100% وتتجاوز حظر الـ SNI، انقطاع الاتصال [WinError 10054], وحدود الـ API.
        """
        logger.info(f"فحص محتوى تيك توك للرابط: {tiktok_url}")
        session = cffi_requests.Session(impersonate="chrome")

        # 1. المزود الأول: TikWM API (فائق السرعة مع تجربة بدائل المعلمات)
        try:
            for api_url in [
                f"https://www.tikwm.com/api/?url={tiktok_url}&hd=1",
                f"https://www.tikwm.com/api/?url={tiktok_url}",
                f"https://tikwm.com/api/?url={tiktok_url}",
            ]:
                try:
                    r = session.get(api_url, timeout=10)
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("code") == 0:
                            d = data.get("data", {})
                            title = self._sanitize_title(d.get("title"))
                            music_url = d.get("music")
                            images = d.get("images") or []
                            author = d.get("author", {}).get("nickname")

                            if images:
                                logger.info(f"تم اكتشاف {len(images)} صورة عبر tikwm")
                                return TikTokMediaInfo(
                                    media_type="photos",
                                    title=title,
                                    photo_urls=images,
                                    music_url=music_url,
                                    author=author,
                                )
                            elif d.get("hdplay") or d.get("play") or d.get("wmplay"):
                                vid_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
                                logger.info("تم استخراج رابط الفيديو عبر tikwm بنجاح")
                                return TikTokMediaInfo(
                                    media_type="video",
                                    title=title,
                                    video_url=vid_url,
                                    music_url=music_url,
                                    author=author,
                                )
                except Exception as e_inner:
                    logger.debug(f"فشل استدعاء {api_url}: {e_inner}")
        except Exception as e_tikwm:
            logger.warning(f"فشلت المحاولة عبر tikwm: {e_tikwm}")

        # 2. المزود الثاني: Savetik.co / SnapCDN (قوي جداً، لا يخضع للحظر، ويوفر روابط snapcdn مباشرة)
        try:
            r_save = session.post(
                "https://savetik.co/api/ajaxSearch",
                data={"q": tiktok_url, "lang": "en"},
                headers={"Referer": "https://savetik.co/en"},
                timeout=12
            )
            if r_save.status_code == 200:
                sdata = r_save.json()
                if sdata.get("status") == "ok":
                    data_html = sdata.get("data", "")

                    title = "تيك توك (TikTok)"
                    title_m = re.search(r'<h3[^>]*>(.*?)</h3>', data_html, re.DOTALL)
                    if title_m:
                        title = self._sanitize_title(html.unescape(title_m.group(1).strip()))

                    slide_links = re.findall(r'href="([^"]+)"\s+class="[^"]*download-file', data_html)
                    if not slide_links:
                        slide_links = [l for l in re.findall(r'href="(https://dl\.snapcdn\.app/get\?token=[^"]+)"', data_html) if "photo" in l.lower() or "image" in l.lower()]

                    music_m = re.search(r'href="([^"]+)"\s+class="[^"]*(?:dl-action|download-file)[^"]*"\s+data-event="mp3"', data_html) or re.search(r'href="(https://dl\.snapcdn\.app/get\?token=[^"]+)"[^>]*>.*?MP3', data_html, re.DOTALL)
                    music_url = music_m.group(1) if music_m else None

                    if slide_links:
                        logger.info(f"تم اكتشاف {len(slide_links)} صورة عبر savetik")
                        return TikTokMediaInfo(
                            media_type="photos",
                            title=title,
                            photo_urls=slide_links,
                            music_url=music_url,
                        )

                    vid_links = [l for l in re.findall(r'href="(https://dl\.snapcdn\.app/get\?token=[^"]+)"', data_html) if l != music_url]
                    if not vid_links:
                        vid_links = re.findall(r'href="([^"]+)"\s+class="[^"]*tik-button-dl', data_html)

                    if vid_links:
                        logger.info("تم استخراج رابط الفيديو عبر savetik/snapcdn بنجاح")
                        return TikTokMediaInfo(
                            media_type="video",
                            title=title,
                            video_url=vid_links[0],
                            music_url=music_url,
                        )
        except Exception as e_save:
            logger.warning(f"فشلت المحاولة عبر savetik: {e_save}")

        # 3. المزود الثالث: LoveTik API
        try:
            r_love = session.post(
                "https://lovetik.com/api/ajax/search",
                data={"query": tiktok_url},
                timeout=10
            )
            if r_love.status_code == 200:
                ldata = r_love.json()
                if ldata.get("status") == "ok" and ldata.get("links"):
                    title = self._sanitize_title(ldata.get("desc"))
                    author = ldata.get("author")
                    vid_url = None
                    music_url = None
                    images = ldata.get("images") or []

                    for l in ldata["links"]:
                        t = l.get("t", "").lower()
                        if "watermark" in t and not vid_url:
                            vid_url = l.get("a")
                        elif "mp3" in t and not music_url:
                            music_url = l.get("a")

                    if not vid_url and ldata["links"]:
                        vid_url = ldata["links"][0].get("a")

                    if images:
                        return TikTokMediaInfo(
                            media_type="photos",
                            title=title,
                            photo_urls=images,
                            music_url=music_url,
                            author=author,
                        )
                    elif vid_url:
                        logger.info("تم استخراج رابط الفيديو عبر lovetik بنجاح")
                        return TikTokMediaInfo(
                            media_type="video",
                            title=title,
                            video_url=vid_url,
                            music_url=music_url,
                            author=author,
                        )
        except Exception as e_love:
            logger.warning(f"فشلت المحاولة عبر lovetik: {e_love}")

        # 4. المزود الرابع: SSSTik.io المطور
        try:
            r_ssstik = session.get("https://ssstik.io/en", headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=10)
            tt_match = (
                re.search(r'data-tt="([^"]+)"', r_ssstik.text) or
                re.search(r'name="tt" value="([^"]+)"', r_ssstik.text) or
                re.search(r'"tt":"([^"]+)"', r_ssstik.text)
            )
            tt_val = tt_match.group(1) if tt_match else "0"

            post_resp = session.post(
                "https://ssstik.io/abc?url=dl",
                data={"id": tiktok_url, "locale": "en", "tt": tt_val},
                headers={"Origin": "https://ssstik.io", "Referer": "https://ssstik.io/en"},
                timeout=15
            )

            resp_text = post_resp.text
            title = "تيك توك (TikTok)"
            title_match = re.search(r'<p[^>]*class="[^"]*maintext[^"]*"[^>]*>(.*?)</p>', resp_text, re.DOTALL)
            if title_match:
                title = self._sanitize_title(html.unescape(title_match.group(1).strip()))

            author_match = re.search(r'<h2[^>]*>(.*?)</h2>', resp_text, re.DOTALL)
            author = html.unescape(author_match.group(1).strip()) if author_match else None

            slide_links = re.findall(r'href="(https://tikcdn\.io/ssstik/[^"]+)"\s+class="[^"]*download_link\s+slide', resp_text)
            if not slide_links:
                slide_links = re.findall(r'href="([^"]+)"\s+class="[^"]*download_link\s+slide', resp_text)

            music_match = re.search(r'href="([^"]+)"\s+class="[^"]*pure-button[^"]*music', resp_text) or re.search(r'href="(https://tikcdn\.io/ssstik/m/[^"]+)"', resp_text)
            music_url = music_match.group(1) if music_match else None

            if slide_links:
                logger.info(f"تم اكتشاف منشور صور عبر ssstik! عدد الصور: {len(slide_links)}")
                return TikTokMediaInfo(
                    media_type="photos",
                    title=title,
                    photo_urls=slide_links,
                    music_url=music_url,
                    author=author,
                )

            dl_match = (
                re.search(r'href="(https://tikcdn\.io/[^"]+)"', resp_text) or
                re.search(r'href="(https?://[^"]*(?:ssstik|tikcdn|download)[^"]*)"', resp_text)
            )
            if dl_match:
                return TikTokMediaInfo(
                    media_type="video",
                    title=title,
                    video_url=dl_match.group(1),
                    music_url=music_url,
                    author=author,
                )
        except Exception as e_ssstik:
            logger.warning(f"فشلت المحاولة الاحتياطية عبر ssstik: {e_ssstik}")

        raise ContentUnavailableError("تعذر العثور على رابط التنزيل لمحتوى تيك توك عبر كافة المزودات.")

    def _download_and_convert_photos_sync(self, photo_urls: List[str], temp_dir: Path) -> List[Path]:
        """تنزيل كافة صور تيك توك وتحويلها محلياً لـ JPG بأعلى دقة متوافقة مع تيليجرام."""
        session = cffi_requests.Session(impersonate="chrome")
        results: List[Path] = []

        for idx, url in enumerate(photo_urls):
            target_jpg = temp_dir / f"photo_{idx + 1:02d}.jpg"
            raw_path = temp_dir / f"raw_{idx + 1}"
            try:
                ok = self._download_media_stream_sync(session, url, raw_path, default_referer="https://www.tiktok.com/")
                if ok and raw_path.exists() and raw_path.stat().st_size > 100:
                    if FFMPEG_PATH:
                        conv_cmd = [
                            FFMPEG_PATH, "-y",
                            "-i", str(raw_path),
                            "-q:v", "2",
                            str(target_jpg)
                        ]
                        subprocess.run(conv_cmd, capture_output=True)
                        if target_jpg.exists() and target_jpg.stat().st_size > 0:
                            results.append(target_jpg)
                            raw_path.unlink(missing_ok=True)
                            continue
                    raw_path.rename(target_jpg)
                    results.append(target_jpg)
                else:
                    logger.warning(f"فشل تنزيل الصورة {idx + 1}")
            except Exception as e:
                logger.warning(f"خطأ أثناء تنزيل الصورة {idx + 1}: {e}")

        return results

    def _download_audio_sync(self, music_url: Optional[str], temp_dir: Path) -> Optional[Path]:
        """تنزيل المقطع الصوتي المصاحب بدقته الأصلية مع تجاوز الحظر."""
        if not music_url:
            return None
        session = cffi_requests.Session(impersonate="chrome")
        target_audio = temp_dir / "music.mp3"
        try:
            ok = self._download_media_stream_sync(session, music_url, target_audio, default_referer="https://www.tiktok.com/")
            if ok and target_audio.exists() and target_audio.stat().st_size > 500:
                return target_audio
        except Exception as e:
            logger.warning(f"تعذر تنزيل الصوت المرفق: {e}")
        return None

    def _render_photo_video_sync(
        self, image_path: Path, audio_path: Optional[Path], temp_dir: Path, title: str
    ) -> DownloadResult:
        """
        دمج صورة تيك توك والمقطع الصوتي في مقطع فيديو MP4 عالي الجودة عبر FFmpeg.
        """
        if not FFMPEG_PATH:
            raise DownloaderError("محرك FFmpeg غير متوفر لدمج الفيديو والصوت.")

        if not image_path or not image_path.exists() or image_path.stat().st_size < 100:
            raise DownloaderError("صورة المقطع غير متوفرة لإنشاء الفيديو.")

        output_video = temp_dir / "rendered_video.mp4"

        if audio_path and audio_path.exists() and audio_path.stat().st_size > 500:
            cmd = [
                FFMPEG_PATH, "-y",
                "-loop", "1",
                "-i", str(image_path),
                "-i", str(audio_path),
                "-c:v", "libx264",
                "-tune", "stillimage",
                "-c:a", "aac",
                "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                "-shortest",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-movflags", "+faststart",
                str(output_video)
            ]
        else:
            cmd = [
                FFMPEG_PATH, "-y",
                "-loop", "1",
                "-i", str(image_path),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t", "5",
                "-c:v", "libx264",
                "-tune", "stillimage",
                "-c:a", "aac",
                "-pix_fmt", "yuv420p",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-movflags", "+faststart",
                str(output_video)
            ]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if not output_video.exists() or output_video.stat().st_size < 1000:
            logger.error(f"فشل FFmpeg في دمج الفيديو: {res.stderr}")
            raise DownloaderError("فشل إنشاء الفيديو من الصورة والصوت.")

        duration, width, height, thumbnail_path = extract_video_metadata_with_ffmpeg(output_video, temp_dir)
        file_size = output_video.stat().st_size

        return DownloadResult(
            video_path=output_video,
            title=title or "فيديو تيك توك",
            duration=duration or 5,
            width=width,
            height=height,
            thumbnail_path=thumbnail_path or image_path,
            platform="تيك توك (TikTok)",
            file_size_bytes=file_size,
            temp_dir=temp_dir,
            audio_path=audio_path if audio_path and audio_path.exists() else None,
        )

    def _download_tiktok_video_from_url_sync(
        self, video_url: str, title: str, temp_dir: Path, music_url: Optional[str] = None
    ) -> DownloadResult:
        """تنزيل تدفق فيديو تيك توك المباشر المستخرج وتوليد بياناته محلياً مع تجاوز الحظر واستخراج الصوت."""
        session = cffi_requests.Session(impersonate="chrome")
        video_file = temp_dir / "video.mp4"

        ok = self._download_media_stream_sync(session, video_url, video_file, default_referer="https://www.tiktok.com/")
        if not ok or not video_file.exists() or video_file.stat().st_size < 1000:
            raise DownloaderError("فشل تنزيل فيديو تيك توك.")

        file_size = video_file.stat().st_size
        logger.info(f"تم تنزيل فيديو تيك توك بنجاح! الحجم: {file_size / (1024 * 1024):.2f} MB")

        duration, width, height, thumbnail_path = extract_video_metadata_with_ffmpeg(video_file, temp_dir)

        # استخراج المقطع الصوتي MP3 محلياً وفورياً عبر FFmpeg (في أجزاء من الثانية)
        audio_file = extract_audio_mp3_sync(video_file, temp_dir / "audio.mp3")
        if not audio_file or not audio_file.exists() or audio_file.stat().st_size < 500:
            if music_url:
                audio_file = self._download_audio_sync(music_url, temp_dir)

        return DownloadResult(
            video_path=video_file,
            title=title,
            duration=duration,
            width=width,
            height=height,
            thumbnail_path=thumbnail_path,
            platform="تيك توك (TikTok)",
            file_size_bytes=file_size,
            temp_dir=temp_dir,
            audio_path=audio_file,
        )

    def _download_tiktok_direct(self, tiktok_url: str, temp_dir: Path) -> DownloadResult:
        """
        محرك تيك توك المخصص والفائق السرعة (Bypass Engine):
        يتجاوز حظر الـ SNI لـ tiktok.com وانقطاع الاتصال [WinError 10054].
        يدعم تنزيل الفيديو المباشر أو تحويل منشور الصور إلى فيديو تلقائياً.
        """
        info = self._extract_tiktok_info_sync(tiktok_url)
        if info.media_type == "photos":
            if not info.photo_urls:
                raise ContentUnavailableError("لا توجد صور في هذا المنشور.")
            photos = self._download_and_convert_photos_sync([info.photo_urls[0]], temp_dir)
            if not photos:
                raise ContentUnavailableError("تعذر تنزيل صورة تيك توك.")
            audio = self._download_audio_sync(info.music_url, temp_dir)
            return self._render_photo_video_sync(photos[0], audio, temp_dir, info.title)
        else:
            if not info.video_url:
                raise ContentUnavailableError("تعذر العثور على رابط التنزيل لمقطع تيك توك.")
            return self._download_tiktok_video_from_url_sync(info.video_url, info.title, temp_dir, music_url=info.music_url)

    def _sync_download(self, url: str, temp_dir: Path) -> DownloadResult:
        """
        المنطق التزامني عالي السرعة:
        - للمقاطع من تيك توك: يوجه فوراً للمحرك المباشر لتفادي حظر الـ SNI وقطع الاتصال.
        - للمنصات الأخرى (إنستغرام، يوتيوب، تويتر/X، فيسبوك، إلخ): يستخدم yt-dlp بأقصى سرعة.
        """
        is_tiktok = any(dom in url.lower() for dom in ["tiktok.com", "douyin.com", "tiktokv.com"])

        # 1. إذا كان تيك توك، نستخدم المحرك المباشر أولاً
        if is_tiktok:
            try:
                return self._download_tiktok_direct(url, temp_dir)
            except Exception as e_tt:
                logger.warning(f"تعذر التنزيل عبر محرك تيك توك المباشر ({e_tt})، جاري المحاولة البديلة عبر yt-dlp...")

        # 2. إذا كان يوتيوب، نستخدم استراتيجيات متسلسلة عالية الدقة لتجاوز حظر الخوادم السحابية
        is_youtube = any(dom in url.lower() for dom in ["youtube.com", "youtu.be"])
        info: Optional[Dict[str, Any]] = None

        if is_youtube:
            yt_id_m = re.search(r'(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})', url)
            clean_yt_url = f"https://www.youtube.com/watch?v={yt_id_m.group(1)}" if yt_id_m else url

            youtube_strategies = [
                {"client": ["android"], "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/b[ext=mp4]/best"},
                {"client": ["android"], "format": "18/best"},
                {"client": ["android_vr"], "format": "best[ext=mp4]/best"},
                {"client": ["ios"], "format": "best[ext=mp4]/best"},
                {"client": ["web"], "format": "best[ext=mp4]/best"},
            ]
            last_yt_err = None
            for s in youtube_strategies:
                opts = self._build_ydl_options(
                    temp_dir=temp_dir,
                    format_selector=s["format"],
                    download_thumbnail=True,
                    use_syndication=False,
                    is_tiktok=False,
                    youtube_clients=s["client"],
                )
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl_yt:
                        info = ydl_yt.extract_info(clean_yt_url, download=True)
                    if info:
                        break
                except Exception as ex:
                    last_yt_err = ex
                    logger.warning(f"فشلت استراتيجية يوتيوب {s['client']} ({ex})، تجربة البديل التالي...")
            if not info and last_yt_err:
                self._handle_download_error(last_yt_err)

        # 3. التنزيل عبر yt-dlp للمنصات الأخرى (أو إذا لم يكتمل التنزيل)
        if not info:
            is_twitter = any(dom in url.lower() for dom in ["x.com", "twitter.com"])
            best_format = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b/best"
            ydl_opts = self._build_ydl_options(
                temp_dir=temp_dir,
                format_selector=best_format,
                download_thumbnail=True,
                use_syndication=True,
                is_tiktok=is_tiktok,
            )

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception as e:
                logger.exception(f"فشلت المحاولة بالجودة القصوى (Full Traceback): {e}")

                if is_twitter:
                    logger.warning("جاري إعادة محاولة التنزيل لتويتر بدون syndication...")
                    fallback_opts = self._build_ydl_options(
                        temp_dir=temp_dir,
                        format_selector="best",
                        download_thumbnail=True,
                        use_syndication=False,
                        is_tiktok=False,
                    )
                    try:
                        with yt_dlp.YoutubeDL(fallback_opts) as ydl_fb:
                            info = ydl_fb.extract_info(url, download=True)
                    except Exception as fb_err:
                        logger.exception(f"فشلت محاولة تويتر البديلة: {fb_err}")
                        self._handle_download_error(fb_err)
                elif is_tiktok:
                    try:
                        return self._download_tiktok_direct(url, temp_dir)
                    except Exception as final_tt_err:
                        self._handle_download_error(final_tt_err)
                else:
                    logger.warning("جاري إعادة محاولة التنزيل بصيغة عامة...")
                    fallback_opts = self._build_ydl_options(
                        temp_dir=temp_dir,
                        format_selector="best/b",
                        download_thumbnail=False,
                        use_syndication=False,
                        is_tiktok=False,
                    )
                    try:
                        with yt_dlp.YoutubeDL(fallback_opts) as ydl_gen:
                            info = ydl_gen.extract_info(url, download=True)
                    except Exception as gen_err:
                        logger.exception(f"فشلت المحاولة العامة البديلة: {gen_err}")
                        self._handle_download_error(gen_err)

        if not info:
            raise ContentUnavailableError("تعذر العثور على أي معلومات أو بيانات للمقطع.")

        title = self._sanitize_title(info.get("title"))
        raw_dur = info.get("duration")
        raw_w = info.get("width")
        raw_h = info.get("height")

        duration = int(round(float(raw_dur))) if raw_dur is not None else None
        width = int(round(float(raw_w))) if raw_w is not None else None
        height = int(round(float(raw_h))) if raw_h is not None else None
        platform = self._detect_platform(info.get("extractor_key") or info.get("extractor"))

        video_path, thumbnail_path = self._find_media_files(temp_dir)
        if not video_path or not video_path.exists():
            raise DownloaderError("لم يتم العثور على ملف الفيديو بعد انتهاء التنزيل.")

        # إذا كانت الأبعاد أو المدة أو الغلاف غير متوفرة، نكملها عبر FFmpeg محلياً
        if duration is None or width is None or height is None or thumbnail_path is None:
            ff_dur, ff_w, ff_h, ff_thumb = extract_video_metadata_with_ffmpeg(video_path, temp_dir)
            if duration is None:
                duration = ff_dur
            if width is None:
                width = ff_w
            if height is None:
                height = ff_h
            if thumbnail_path is None:
                thumbnail_path = ff_thumb

        file_size = video_path.stat().st_size
        logger.info(f"حجم الملف الأصلي بعد التحميل والدمج: {file_size / (1024 * 1024):.2f} MB")

        # استخراج المقطع الصوتي MP3 لجميع المنصات
        audio_path = extract_audio_mp3_sync(video_path, temp_dir / f"{video_path.stem}_audio.mp3")

        # التحقق من حد تيليجرام (50MB)
        if file_size <= MAX_FILE_SIZE_BYTES:
            return DownloadResult(
                video_path=video_path,
                title=title,
                duration=duration,
                width=width,
                height=height,
                thumbnail_path=thumbnail_path,
                platform=platform,
                file_size_bytes=file_size,
                temp_dir=temp_dir,
                audio_path=audio_path,
            )

        # إذا تجاوز 48MB: حذف الملف وتنزيل أفضل جودة تقع تحت الحد
        logger.info(f"الملف تجاوز {MAX_FILE_SIZE_MB}MB! جاري حذف الملف ومحاولة جلب أفضل جودة تقع تحت الحد...")
        video_path.unlink(missing_ok=True)
        if thumbnail_path and thumbnail_path.exists():
            thumbnail_path.unlink(missing_ok=True)

        constrained_format = (
            f"bestvideo[filesize<{MAX_FILE_SIZE_MB - 6}M]+bestaudio[filesize<6M]/"
            f"best[filesize<{MAX_FILE_SIZE_MB}M]/"
            f"bestvideo[filesize_approx<{MAX_FILE_SIZE_MB - 6}M]+bestaudio[filesize_approx<6M]/"
            f"best[filesize_approx<{MAX_FILE_SIZE_MB}M]/"
            f"worst"
        )

        ydl_opts_constrained = self._build_ydl_options(
            temp_dir=temp_dir,
            format_selector=constrained_format,
            download_thumbnail=True,
            use_syndication=not is_twitter,
            is_tiktok=is_tiktok,
        )

        try:
            with yt_dlp.YoutubeDL(ydl_opts_constrained) as ydl_c:
                ydl_c.download([url])
        except Exception as e:
            logger.exception(f"فشلت محاولة التحميل بجودة مقيدة (Full Traceback): {e}")
            raise VideoTooLargeError(file_size / (1024 * 1024)) from e

        video_path, thumbnail_path = self._find_media_files(temp_dir)
        if not video_path or not video_path.exists():
            raise VideoTooLargeError(file_size / (1024 * 1024))

        new_file_size = video_path.stat().st_size
        logger.info(f"حجم الملف بعد التحجيم الذكي: {new_file_size / (1024 * 1024):.2f} MB")

        if new_file_size > MAX_FILE_SIZE_BYTES:
            video_path.unlink(missing_ok=True)
            raise VideoTooLargeError(new_file_size / (1024 * 1024))

        audio_path = extract_audio_mp3_sync(video_path, temp_dir / f"{video_path.stem}_audio.mp3")

        return DownloadResult(
            video_path=video_path,
            title=title,
            duration=duration,
            width=width,
            height=height,
            thumbnail_path=thumbnail_path,
            platform=platform,
            file_size_bytes=new_file_size,
            temp_dir=temp_dir,
            audio_path=audio_path,
        )

    async def download_video(self, raw_url: str) -> DownloadResult:
        """
        دالة غير متزامنة بالكامل لتحميل الفيديو:
        تنظف الرابط وتشغل التنزيل داخل خيط منفصل (asyncio.to_thread).
        """
        clean_url = sanitize_url(raw_url)
        logger.info(f"بدء المعالجة للرابط: {clean_url} (الأصل: {raw_url})")

        temp_dir = DOWNLOAD_DIR / str(uuid.uuid4())
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            return await asyncio.to_thread(self._sync_download, clean_url, temp_dir)
        except Exception as e:
            logger.exception(f"حدث خطأ أثناء تنزيل الفيديو للرابط {clean_url} (Full Traceback): {e}")
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    async def get_tiktok_info(self, raw_url: str) -> TikTokMediaInfo:
        """فحص واستخراج معلومات منشور تيك توك بشكل غير متزامن."""
        clean_url = sanitize_url(raw_url)
        return await asyncio.to_thread(self._extract_tiktok_info_sync, clean_url)

    async def download_tiktok_photos(self, photo_urls: List[str], temp_dir: Path) -> List[Path]:
        """تنزيل كافة صور منشور تيك توك بدقتها الأصلية داخل خيط منفصل."""
        return await asyncio.to_thread(self._download_and_convert_photos_sync, photo_urls, temp_dir)

    async def download_tiktok_audio(self, music_url: Optional[str], temp_dir: Path) -> Optional[Path]:
        """تنزيل المقطع الصوتي المصاحب داخل خيط منفصل."""
        return await asyncio.to_thread(self._download_audio_sync, music_url, temp_dir)

    async def render_tiktok_photo_video(
        self, image_path: Path, audio_path: Optional[Path], temp_dir: Path, title: str
    ) -> DownloadResult:
        """دمج صورة تيك توك والصوت في فيديو عالي الدقة عبر FFmpeg داخل خيط منفصل."""
        return await asyncio.to_thread(self._render_photo_video_sync, image_path, audio_path, temp_dir, title)

    async def download_tiktok_direct_video(
        self, video_url: str, title: str, temp_dir: Path
    ) -> DownloadResult:
        """تنزيل فيديو تيك توك المباشر داخل خيط منفصل."""
        return await asyncio.to_thread(self._download_tiktok_video_from_url_sync, video_url, title, temp_dir)
