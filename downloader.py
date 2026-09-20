import os
import re
import html
import uuid
import base64
import shutil
import asyncio
import logging
import subprocess
import urllib.request
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

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

logger = logging.getLogger("bot.downloader")

SHORT_DOMAINS = ("vt.tiktok.com", "vm.tiktok.com", "tiktok.com/t/", "t.co", "bit.ly", "tinyurl.com")


def _get_http_session() -> cffi_requests.Session:
    return cffi_requests.Session(impersonate="chrome")


async def resolve_redirect_url(url: str) -> str:
    if not any(domain in url.lower() for domain in SHORT_DOMAINS):
        return url

    def _unshorten():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.geturl()
        except Exception:
            return url

    return await asyncio.to_thread(_unshorten)


def sanitize_url(raw_url: str) -> str:
    if not raw_url:
        return ""

    url = raw_url.strip()

    twitter_pattern = (
        r'https?://(?:www\.|mobile\.|m\.)?'
        r'(?:twitter\.com|x\.com|vxtwitter\.com|fxtwitter\.com|fixupx\.com)/'
        r'(?:([a-zA-Z0-9_]+)|i(?:/web)?)/status(?:es)?/(\d+)'
    )
    if match := re.search(twitter_pattern, url, re.IGNORECASE):
        user = match.group(1) or "i"
        status_id = match.group(2)
        return f"https://x.com/{user}/status/{status_id}"

    if "?" in url:
        base, query = url.split("?", 1)
        if "youtube.com/watch" in base:
            if v_match := re.search(r'(?:^|&)v=([a-zA-Z0-9_-]+)', query):
                return f"{base}?v={v_match.group(1)}"
        url = base

    return url.split("#")[0].rstrip("/")


def is_tiktok_url(raw_url: str) -> bool:
    if not raw_url:
        return False
    u = raw_url.lower()
    return any(d in u for d in ("tiktok.com", "douyin.com", "tiktokv.com", "vm.tiktok.com", "vt.tiktok.com"))


def chunk_media_list(items: list, max_chunk: int = 10) -> list[list]:
    if not items:
        return []
    if len(items) <= max_chunk:
        return [items]

    chunks = []
    i, n = 0, len(items)
    while i < n:
        rem = n - i
        take = max_chunk - 1 if rem == max_chunk + 1 else min(rem, max_chunk)
        chunks.append(items[i:i + take])
        i += take
    return chunks


def clean_error_message(msg: str) -> str:
    if not msg:
        return ""
    clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', msg)
    clean = re.sub(r'(?:\[[0-9;]*m|[0-9]{1,2}m)', '', clean)
    clean = re.sub(r'^(?:error:\s*)+', '', clean, flags=re.IGNORECASE)
    return clean.strip()


def extract_video_metadata_with_ffmpeg(
    video_path: Path, temp_dir: Path
) -> tuple[int | None, int | None, int | None, Path | None]:
    if not FFMPEG_PATH or not video_path.exists():
        return None, None, None, None

    duration: int | None = None
    width: int | None = None
    height: int | None = None
    thumbnail_path: Path | None = None

    try:
        res = subprocess.run([FFMPEG_PATH, "-i", str(video_path)], capture_output=True, text=True, errors="ignore")
        stderr = res.stderr or ""

        if dur_match := re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', stderr):
            h, m, s = dur_match.groups()
            duration = int(h) * 3600 + int(m) * 60 + int(float(s))

        if dim_match := re.search(r'Stream.*Video:.*,\s*(\d{2,5})x(\d{2,5})', stderr):
            width, height = int(dim_match.group(1)), int(dim_match.group(2))

        thumb_file = temp_dir / "thumbnail.jpg"
        subprocess.run(
            [FFMPEG_PATH, "-y", "-ss", "00:00:01", "-i", str(video_path), "-vframes", "1", "-q:v", "2", str(thumb_file)],
            capture_output=True,
            errors="ignore",
        )
        if thumb_file.exists() and thumb_file.stat().st_size > 0:
            thumbnail_path = thumb_file

    except Exception as e:
        logger.debug(f"FFmpeg probe error: {e}")

    return duration, width, height, thumbnail_path


class DownloaderError(Exception):
    pass


class VideoTooLargeError(DownloaderError):
    def __init__(self, size_mb: float):
        self.size_mb = size_mb
        super().__init__(
            f"حجم الفيديو ({size_mb:.1f}MB) يتجاوز الحد الأقصى المسموح به للبوتات في تيليجرام (50MB)."
        )


class ContentUnavailableError(DownloaderError):
    pass


class InvalidURLError(DownloaderError):
    pass


@dataclass(slots=True)
class DownloadResult:
    video_path: Path
    title: str
    duration: int | None
    width: int | None
    height: int | None
    thumbnail_path: Path | None
    platform: str
    file_size_bytes: int
    temp_dir: Path

    @property
    def file_size_mb(self) -> float:
        return self.file_size_bytes / (1024 * 1024)

    def cleanup(self) -> None:
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)


@dataclass(slots=True)
class TikTokMediaInfo:
    media_type: str
    title: str
    video_url: str | None = None
    photo_urls: list[str] = field(default_factory=list)
    music_url: str | None = None
    author: str | None = None


class MediaDownloader:

    PLATFORM_NAMES: dict[str, str] = {
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
    def _sanitize_title(title: str | None) -> str:
        if not title:
            return "فيديو بدون عنوان"
        clean = " ".join(title.split())
        return (clean[:77] + "...") if len(clean) > 80 else clean

    @classmethod
    def _detect_platform(cls, extractor_key: str | None) -> str:
        if not extractor_key:
            return "منصة وسائط"
        key = extractor_key.lower()
        return next((name for k, name in cls.PLATFORM_NAMES.items() if k in key), extractor_key.capitalize())

    @staticmethod
    def _get_extractor_args(use_syndication: bool = True) -> dict[str, Any]:
        args: dict[str, Any] = {
            "youtube": {
                "player_client": ["android", "web", "ios"],
                "skip": ["dash", "hls"],
            },
            "tiktok": {
                "app_version": "34.1.2",
            }
        }
        if use_syndication:
            args["twitter"] = {"api": ["syndication"]}
        return args

    @classmethod
    def _build_ydl_options(
        cls,
        temp_dir: Path,
        format_selector: str,
        download_thumbnail: bool = True,
        use_syndication: bool = True,
        is_tiktok: bool = False,
    ) -> dict[str, Any]:
        postprocessors = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
        if download_thumbnail:
            postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})

        opts: dict[str, Any] = {
            "format": format_selector,
            "outtmpl": str(temp_dir / "%(id)s.%(ext)s"),
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
            "user_agent": TIKTOK_MOBILE_USER_AGENT if is_tiktok else DEFAULT_USER_AGENT,
            "http_headers": TIKTOK_HTTP_HEADERS if is_tiktok else DEFAULT_HTTP_HEADERS,
            "concurrent_fragment_downloads": 4,
            "socket_timeout": 20,
            "retries": 5,
            "fragment_retries": 5,
            "extractor_args": cls._get_extractor_args(use_syndication=use_syndication),
        }

        if FFMPEG_PATH:
            opts["ffmpeg_location"] = FFMPEG_PATH

        if COOKIES_PATH and COOKIES_PATH.is_file():
            opts["cookiefile"] = str(COOKIES_PATH)

        return opts

    @staticmethod
    def _find_media_files(temp_dir: Path) -> tuple[Path | None, Path | None]:
        video_exts = {".mp4", ".mkv", ".webm", ".mov"}
        image_exts = {".jpg", ".jpeg", ".png", ".webp"}

        video_path = next((f for f in temp_dir.iterdir() if f.is_file() and f.suffix.lower() in video_exts), None)
        thumb_path = next((f for f in temp_dir.iterdir() if f.is_file() and f.suffix.lower() in image_exts), None)
        return video_path, thumb_path

    @classmethod
    def _handle_download_error(cls, e: Exception) -> None:
        error_msg = str(e).lower()
        cleaned = clean_error_message(str(e))

        if "no video could be found" in error_msg:
            raise ContentUnavailableError("لا يوجد مقطع فيديو داخل هذا الرابط.") from e
        if any(term in error_msg for term in ["private", "login", "requires account", "members-only"]):
            raise ContentUnavailableError("هذا المقطع خاص أو يتطلب تسجيل دخول.") from e
        if any(term in error_msg for term in ["not found", "deleted", "unavailable", "does not exist"]):
            raise ContentUnavailableError("تم حذف هذا المقطع أو أنه غير متاح.") from e
        if "unsupported url" in error_msg:
            raise InvalidURLError("الرابط المرسل غير مدعوم أو غير صحيح.") from e
        if any(code in error_msg for code in ["10054", "connection reset", "forcibly closed"]):
            raise DownloaderError("انقطع الاتصال بخادم المنصة، يرجى المحاولة مرة أخرى.") from e
        if any(term in error_msg for term in ["country", "region", "geo"]):
            raise ContentUnavailableError("هذا المقطع غير متوفر في منطقتك الجغرافية.") from e

        raise DownloaderError(f"حدث خطأ أثناء تحميل المقطع: {cleaned}") from e

    @staticmethod
    def _download_media_stream_sync(
        session: cffi_requests.Session,
        media_url: str | None,
        target_file: Path,
        default_referer: str = "https://ssstik.io/",
    ) -> bool:
        if not media_url:
            return False

        try:
            resp = session.get(media_url, headers={"Referer": default_referer}, timeout=4)
            if resp.status_code == 200 and len(resp.content) > 500:
                target_file.write_bytes(resp.content)
                return True
        except Exception:
            pass

        try:
            b64 = base64.b64encode(media_url.encode()).decode()
            proxy_url = f"https://tikcdn.io/ssstik/{b64}"
            resp = session.get(proxy_url, headers={"Referer": "https://ssstik.io/"}, timeout=25)
            if resp.status_code == 200 and len(resp.content) > 500:
                target_file.write_bytes(resp.content)
                return True
        except Exception as e_proxy:
            logger.debug(f"tikcdn proxy error: {e_proxy}")

        return False

    def _extract_tiktok_info_sync(self, tiktok_url: str) -> TikTokMediaInfo:
        session = _get_http_session()

        # 1. tikwm API
        try:
            r = session.get(f"https://www.tikwm.com/api/?url={tiktok_url}&hd=1", timeout=12)
            data = r.json()
            if data.get("code") == 0:
                d = data.get("data", {})
                title = self._sanitize_title(d.get("title"))
                music_url = d.get("music")
                images = d.get("images") or []
                author = d.get("author", {}).get("nickname")

                if images:
                    return TikTokMediaInfo(
                        media_type="photos",
                        title=title,
                        photo_urls=images,
                        music_url=music_url,
                        author=author,
                    )
                if vid_url := (d.get("hdplay") or d.get("play")):
                    return TikTokMediaInfo(
                        media_type="video",
                        title=title,
                        video_url=vid_url,
                        music_url=music_url,
                        author=author,
                    )
        except Exception as e_tikwm:
            logger.debug(f"tikwm error: {e_tikwm}")

        # 2. ssstik.io fallback
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
            if title_match := re.search(r'<p[^>]*class="[^"]*maintext[^"]*"[^>]*>(.*?)</p>', resp_text, re.DOTALL):
                title = self._sanitize_title(html.unescape(title_match.group(1).strip()))

            author_match = re.search(r'<h2[^>]*>(.*?)</h2>', resp_text, re.DOTALL)
            author = html.unescape(author_match.group(1).strip()) if author_match else None

            slide_links = (
                re.findall(r'href="(https://tikcdn\.io/ssstik/[^"]+)"\s+class="[^"]*download_link\s+slide', resp_text) or
                re.findall(r'href="([^"]+)"\s+class="[^"]*download_link\s+slide', resp_text)
            )

            music_match = re.search(r'href="(https://tikcdn\.io/ssstik/m/[^"]+)"', resp_text)
            music_url = music_match.group(1) if music_match else None

            if slide_links:
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
            logger.debug(f"ssstik error: {e_ssstik}")

        raise ContentUnavailableError("تعذر العثور على رابط التنزيل لمحتوى تيك توك.")

    def _download_and_convert_photos_sync(self, photo_urls: list[str], temp_dir: Path) -> list[Path]:
        session = _get_http_session()
        results: list[Path] = []

        for idx, url in enumerate(photo_urls, 1):
            target_jpg = temp_dir / f"photo_{idx:02d}.jpg"
            raw_path = temp_dir / f"raw_{idx}"

            ok = self._download_media_stream_sync(session, url, raw_path, default_referer="https://ssstik.io/")
            if not (ok and raw_path.exists() and raw_path.stat().st_size > 100):
                continue

            if FFMPEG_PATH:
                cmd = [FFMPEG_PATH, "-y", "-i", str(raw_path), "-q:v", "2", str(target_jpg)]
                subprocess.run(cmd, capture_output=True)
                if target_jpg.exists() and target_jpg.stat().st_size > 0:
                    results.append(target_jpg)
                    raw_path.unlink(missing_ok=True)
                    continue

            raw_path.rename(target_jpg)
            results.append(target_jpg)

        return results

    def _download_audio_sync(self, music_url: str | None, temp_dir: Path) -> Path | None:
        if not music_url:
            return None
        session = _get_http_session()
        target_audio = temp_dir / "music.mp3"
        ok = self._download_media_stream_sync(session, music_url, target_audio, default_referer="https://ssstik.io/")
        return target_audio if (ok and target_audio.exists() and target_audio.stat().st_size > 500) else None

    def _render_photo_video_sync(
        self, image_path: Path, audio_path: Path | None, temp_dir: Path, title: str
    ) -> DownloadResult:
        if not FFMPEG_PATH:
            raise DownloaderError("محرك FFmpeg غير متوفر لدمج الفيديو والصوت.")

        if not image_path or not image_path.exists() or image_path.stat().st_size < 100:
            raise DownloaderError("الصورة المطلوبة لإنشاء الفيديو غير صالحة.")

        output_video = temp_dir / "video.mp4"
        scale_filter = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

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
                "-vf", scale_filter,
                "-shortest",
                "-movflags", "+faststart",
                str(output_video),
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
                "-vf", scale_filter,
                "-movflags", "+faststart",
                str(output_video),
            ]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if not output_video.exists() or output_video.stat().st_size < 1000:
            logger.error(f"FFmpeg render error: {res.stderr}")
            raise DownloaderError("فشل إنشاء الفيديو من الصورة والصوت.")

        duration, width, height, thumb = extract_video_metadata_with_ffmpeg(output_video, temp_dir)

        return DownloadResult(
            video_path=output_video,
            title=title or "فيديو تيك توك",
            duration=duration or 5,
            width=width,
            height=height,
            thumbnail_path=thumb or image_path,
            platform="تيك توك (TikTok)",
            file_size_bytes=output_video.stat().st_size,
            temp_dir=temp_dir,
        )

    def _download_tiktok_video_from_url_sync(
        self, video_url: str, title: str, temp_dir: Path
    ) -> DownloadResult:
        session = _get_http_session()
        video_file = temp_dir / "video.mp4"

        ok = self._download_media_stream_sync(session, video_url, video_file, default_referer="https://ssstik.io/")
        if not ok or not video_file.exists() or video_file.stat().st_size < 1000:
            raise DownloaderError("فشل تنزيل فيديو تيك توك.")

        duration, width, height, thumb = extract_video_metadata_with_ffmpeg(video_file, temp_dir)

        return DownloadResult(
            video_path=video_file,
            title=title,
            duration=duration,
            width=width,
            height=height,
            thumbnail_path=thumb,
            platform="تيك توك (TikTok)",
            file_size_bytes=video_file.stat().st_size,
            temp_dir=temp_dir,
        )

    def _download_tiktok_direct(self, tiktok_url: str, temp_dir: Path) -> DownloadResult:
        info = self._extract_tiktok_info_sync(tiktok_url)
        if info.media_type == "photos":
            if not info.photo_urls:
                raise ContentUnavailableError("لا توجد صور في هذا المنشور.")
            photos = self._download_and_convert_photos_sync([info.photo_urls[0]], temp_dir)
            if not photos:
                raise ContentUnavailableError("تعذر تنزيل صورة تيك توك.")
            audio = self._download_audio_sync(info.music_url, temp_dir)
            return self._render_photo_video_sync(photos[0], audio, temp_dir, info.title)

        if not info.video_url:
            raise ContentUnavailableError("تعذر العثور على رابط التنزيل لمقطع تيك توك.")
        return self._download_tiktok_video_from_url_sync(info.video_url, info.title, temp_dir)

    def _sync_download(self, url: str, temp_dir: Path) -> DownloadResult:
        is_tiktok = any(dom in url.lower() for dom in ["tiktok.com", "douyin.com", "tiktokv.com"])

        if is_tiktok:
            try:
                return self._download_tiktok_direct(url, temp_dir)
            except Exception as e_tt:
                logger.debug(f"Direct TikTok extractor failed, falling back to yt-dlp: {e_tt}")

        is_twitter = any(dom in url.lower() for dom in ["x.com", "twitter.com"])
        ydl_opts = self._build_ydl_options(
            temp_dir=temp_dir,
            format_selector="bv*+ba/b",
            download_thumbnail=True,
            use_syndication=True,
            is_tiktok=is_tiktok,
        )

        info: dict[str, Any] | None = None

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            if is_twitter:
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
                    self._handle_download_error(fb_err)
            elif is_tiktok:
                try:
                    return self._download_tiktok_direct(url, temp_dir)
                except Exception as final_tt_err:
                    self._handle_download_error(final_tt_err)
            else:
                self._handle_download_error(e)

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

        if duration is None or width is None or height is None or thumbnail_path is None:
            ff_dur, ff_w, ff_h, ff_thumb = extract_video_metadata_with_ffmpeg(video_path, temp_dir)
            duration = duration or ff_dur
            width = width or ff_w
            height = height or ff_h
            thumbnail_path = thumbnail_path or ff_thumb

        file_size = video_path.stat().st_size
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
            )

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
            raise VideoTooLargeError(file_size / (1024 * 1024)) from e

        video_path, thumbnail_path = self._find_media_files(temp_dir)
        if not video_path or not video_path.exists():
            raise VideoTooLargeError(file_size / (1024 * 1024))

        new_file_size = video_path.stat().st_size
        if new_file_size > MAX_FILE_SIZE_BYTES:
            video_path.unlink(missing_ok=True)
            raise VideoTooLargeError(new_file_size / (1024 * 1024))

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
        )

    async def download_video(self, raw_url: str) -> DownloadResult:
        clean_url = sanitize_url(raw_url)
        temp_dir = DOWNLOAD_DIR / uuid.uuid4().hex
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            return await asyncio.to_thread(self._sync_download, clean_url, temp_dir)
        except Exception as e:
            logger.error(f"Download failed for {clean_url}: {e}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    async def get_tiktok_info(self, raw_url: str) -> TikTokMediaInfo:
        clean_url = sanitize_url(raw_url)
        return await asyncio.to_thread(self._extract_tiktok_info_sync, clean_url)

    async def download_tiktok_photos(self, photo_urls: list[str], temp_dir: Path) -> list[Path]:
        return await asyncio.to_thread(self._download_and_convert_photos_sync, photo_urls, temp_dir)

    async def download_tiktok_audio(self, music_url: str | None, temp_dir: Path) -> Path | None:
        return await asyncio.to_thread(self._download_audio_sync, music_url, temp_dir)

    async def render_tiktok_photo_video(
        self, image_path: Path, audio_path: Path | None, temp_dir: Path, title: str
    ) -> DownloadResult:
        return await asyncio.to_thread(self._render_photo_video_sync, image_path, audio_path, temp_dir, title)

    async def download_tiktok_direct_video(
        self, video_url: str, title: str, temp_dir: Path
    ) -> DownloadResult:
        return await asyncio.to_thread(self._download_tiktok_video_from_url_sync, video_url, title, temp_dir)
