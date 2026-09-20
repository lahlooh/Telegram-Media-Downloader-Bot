import unittest
import asyncio
import time
import subprocess
import tempfile
from pathlib import Path
from config import (
    MAX_FILE_SIZE_BYTES,
    MAX_FILE_SIZE_MB,
    FFMPEG_PATH,
    DOWNLOAD_DIR,
    DEV_CREDIT,
    TIKTOK_MOBILE_USER_AGENT,
    TIKTOK_HTTP_HEADERS,
)
from downloader import (
    MediaDownloader,
    VideoTooLargeError,
    ContentUnavailableError,
    InvalidURLError,
    DownloaderError,
    TikTokMediaInfo,
    is_tiktok_url,
    chunk_media_list,
    sanitize_url,
    resolve_redirect_url,
    clean_error_message,
)
from bot import (
    URL_REGEX,
    format_duration,
    PendingTikTokChoice,
    pending_tiktok_requests,
    cleanup_expired_requests,
)


class TestBotComponents(unittest.TestCase):

    def test_config(self):
        self.assertEqual(MAX_FILE_SIZE_MB, 48)
        self.assertEqual(MAX_FILE_SIZE_BYTES, 48 * 1024 * 1024)
        self.assertTrue(DOWNLOAD_DIR.exists())
        self.assertIsNotNone(FFMPEG_PATH)
        self.assertTrue(Path(FFMPEG_PATH).exists())
        self.assertEqual(DEV_CREDIT, "Developed By discord:- 4.7e")
        self.assertIn("iPhone", TIKTOK_MOBILE_USER_AGENT)
        self.assertEqual(TIKTOK_HTTP_HEADERS["User-Agent"], TIKTOK_MOBILE_USER_AGENT)

    def test_clean_error_message(self):
        raw_ansi = "\x1b[0;31mERROR: [0m31mERROR: [0m[tiktok] 10054: Remote closed"
        cleaned = clean_error_message(raw_ansi)
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("31m", cleaned)
        self.assertEqual(cleaned, "[tiktok] 10054: Remote closed")

    def test_sanitize_url_twitter(self):
        test_cases = [
            (
                "https://x.com/theverge/status/1784920491829048192/video/1?s=20&t=9xK2mP",
                "https://x.com/theverge/status/1784920491829048192"
            ),
            (
                "https://twitter.com/reuters/status/1768402941859303425/photo/1",
                "https://x.com/reuters/status/1768402941859303425"
            ),
            (
                "https://vxtwitter.com/aljazeera/status/1753902184928190471?s=19",
                "https://x.com/aljazeera/status/1753902184928190471"
            ),
            (
                "https://x.com/i/status/1749102948192048571/video/1",
                "https://x.com/i/status/1749102948192048571"
            ),
            (
                "https://www.tiktok.com/@natgeo/video/7348910294819284710?is_from_webapp=1&sender_device=pc",
                "https://www.tiktok.com/@natgeo/video/7348910294819284710"
            ),
            (
                "https://www.youtube.com/watch?v=aqz-KE-bpKQ&feature=share",
                "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
            ),
            (
                "https://youtu.be/aqz-KE-bpKQ?si=9zK3jXq",
                "https://youtu.be/aqz-KE-bpKQ"
            ),
        ]
        for raw, expected in test_cases:
            cleaned = sanitize_url(raw)
            self.assertEqual(cleaned, expected)

    def test_resolve_redirect_url_non_short(self):
        normal_url = "https://www.instagram.com/reel/DczczxJPRHc/"
        result = asyncio.run(resolve_redirect_url(normal_url))
        self.assertEqual(result, normal_url)

    def test_format_duration(self):
        self.assertEqual(format_duration(None), "غير محدد")
        self.assertEqual(format_duration(0), "غير محدد")
        self.assertEqual(format_duration(45), "00:45")
        self.assertEqual(format_duration(14.52), "00:15")
        self.assertEqual(format_duration(65.4), "01:05")
        self.assertEqual(format_duration(3665.8), "01:01:06")
        self.assertEqual(format_duration("invalid"), "غير محدد")

    def test_url_regex(self):
        test_cases = [
            ("https://x.com/theverge/status/1784920491829048192/video/1?s=20", "https://x.com/theverge/status/1784920491829048192/video/1?s=20"),
            ("https://vm.tiktok.com/ZM6Xk9PqL/", "https://vm.tiktok.com/ZM6Xk9PqL/"),
            ("شاهد هذا المقطع https://www.instagram.com/reel/C4M8qZ1v_7A/ رائع جداً", "https://www.instagram.com/reel/C4M8qZ1v_7A/"),
            ("السلام عليكم كيف الحال جميعاً", None),
        ]
        for text, expected in test_cases:
            match = URL_REGEX.search(text)
            if expected:
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1), expected)
            else:
                self.assertIsNone(match)

    def test_platform_detection(self):
        self.assertIn("TikTok", MediaDownloader._detect_platform("tiktok"))
        self.assertIn("Instagram", MediaDownloader._detect_platform("instagram"))
        self.assertIn("YouTube", MediaDownloader._detect_platform("youtube"))
        self.assertIn("Twitter", MediaDownloader._detect_platform("twitter"))
        self.assertIn("Twitter", MediaDownloader._detect_platform("x"))
        self.assertIn("Reddit", MediaDownloader._detect_platform("reddit"))
        self.assertIn("Pinterest", MediaDownloader._detect_platform("pinterest"))

    def test_sanitize_title(self):
        self.assertEqual(MediaDownloader._sanitize_title(""), "فيديو بدون عنوان")
        long_title = "تقرير إخباري شامل ومفصل حول أبرز المستجدات والتطورات الاقتصادية والتقنية في الشرق الأوسط والعالم"
        sanitized = MediaDownloader._sanitize_title(long_title)
        self.assertLessEqual(len(sanitized), 80)
        self.assertTrue(sanitized.endswith("..."))

    def test_ydl_options_builder_speed_optimizations(self):
        downloader = MediaDownloader()
        opts = downloader._build_ydl_options(
            DOWNLOAD_DIR,
            "bv*+ba/b",
            download_thumbnail=True,
            use_syndication=True,
            is_tiktok=True,
        )
        self.assertEqual(opts["format"], "bv*+ba/b")
        self.assertEqual(opts["merge_output_format"], "mp4")
        self.assertEqual(opts["concurrent_fragment_downloads"], 4)
        self.assertEqual(opts["socket_timeout"], 20)
        self.assertEqual(opts["retries"], 5)
        self.assertEqual(opts["user_agent"], TIKTOK_MOBILE_USER_AGENT)
        self.assertIn("postprocessor_args", opts)
        self.assertEqual(opts["postprocessor_args"]["merger"], ["-preset", "ultrafast"])
        self.assertIn("ffmpeg_location", opts)
        self.assertEqual(opts["extractor_args"]["twitter"]["api"], ["syndication"])

    def test_exceptions_hierarchy(self):
        large_err = VideoTooLargeError(55.4)
        self.assertIsInstance(large_err, DownloaderError)
        self.assertAlmostEqual(large_err.size_mb, 55.4)
        self.assertIn("55.4", str(large_err))

        self.assertTrue(issubclass(ContentUnavailableError, DownloaderError))
        self.assertTrue(issubclass(InvalidURLError, DownloaderError))

    def test_is_tiktok_url(self):
        self.assertTrue(is_tiktok_url("https://www.tiktok.com/@aljazeera/video/7348910294819284710"))
        self.assertTrue(is_tiktok_url("https://www.tiktok.com/@bbcarabic/photo/7351029481928401923"))
        self.assertTrue(is_tiktok_url("https://vm.tiktok.com/ZM6Xk9PqL/"))
        self.assertTrue(is_tiktok_url("https://vt.tiktok.com/ZS2k9LmPq/"))
        self.assertFalse(is_tiktok_url("https://www.instagram.com/reel/C4M8qZ1v_7A/"))
        self.assertFalse(is_tiktok_url("https://x.com/reuters/status/1768402941859303425"))
        self.assertFalse(is_tiktok_url(""))

    def test_chunk_media_list(self):
        self.assertEqual(chunk_media_list([]), [])

        single = [1]
        self.assertEqual(chunk_media_list(single), [[1]])

        five = list(range(5))
        chunks_5 = chunk_media_list(five)
        self.assertEqual(chunks_5, [five])

        ten = list(range(10))
        chunks_10 = chunk_media_list(ten)
        self.assertEqual(chunks_10, [ten])

        eleven = list(range(11))
        chunks_11 = chunk_media_list(eleven)
        self.assertEqual(len(chunks_11), 2)
        self.assertEqual(len(chunks_11[0]), 9)
        self.assertEqual(len(chunks_11[1]), 2)

        fifteen = list(range(15))
        chunks_15 = chunk_media_list(fifteen)
        self.assertEqual(len(chunks_15), 2)
        self.assertEqual(len(chunks_15[0]), 10)
        self.assertEqual(len(chunks_15[1]), 5)

        for count in range(2, 36):
            c_list = chunk_media_list(list(range(count)))
            for c in c_list:
                self.assertGreaterEqual(len(c), 2, f"Chunk size too small for count {count}")
                self.assertLessEqual(len(c), 10, f"Chunk size exceeds 10 for count {count}")

    def test_tiktok_media_info_dataclass(self):
        info = TikTokMediaInfo(media_type="photos", title="صور سياحية")
        self.assertEqual(info.media_type, "photos")
        self.assertEqual(info.title, "صور سياحية")
        self.assertEqual(info.photo_urls, [])
        self.assertIsNone(info.video_url)
        self.assertIsNone(info.music_url)

    def test_pending_tiktok_choice_cleanup(self):
        now = time.time()
        pending_tiktok_requests["expired_test"] = PendingTikTokChoice(
            user_id=581940293,
            title="معالم أثرية قديمة",
            photo_url="https://p16-sign.tiktokcdn-us.com/tos-useast-p-0068/7348910294819284710.jpeg",
            music_url=None,
            reply_to_message_id=98214,
            created_at=now - 4000,
        )
        pending_tiktok_requests["valid_test"] = PendingTikTokChoice(
            user_id=892019481,
            title="ملخص جولة اليوم",
            photo_url="https://p16-sign.tiktokcdn-us.com/tos-useast-p-0068/7351029481928401923.jpeg",
            music_url=None,
            reply_to_message_id=98215,
            created_at=now,
        )

        cleanup_expired_requests()
        self.assertNotIn("expired_test", pending_tiktok_requests)
        self.assertIn("valid_test", pending_tiktok_requests)

        pending_tiktok_requests.pop("valid_test", None)

    def test_render_photo_video_ffmpeg(self):
        downloader = MediaDownloader()
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            img_path = tmp_dir / "frame.jpg"
            
            # Generate dummy image fixture via ffmpeg lavfi
            subprocess.run(
                [FFMPEG_PATH, "-y", "-f", "lavfi", "-i", "color=c=navy:s=720x1280:d=1", "-vframes", "1", str(img_path)],
                capture_output=True,
                check=True
            )
            self.assertTrue(img_path.exists())

            res = downloader._render_photo_video_sync(img_path, None, tmp_dir, "مقطع تجريبي")
            self.assertTrue(res.video_path.exists())
            self.assertGreater(res.file_size_bytes, 1000)
            self.assertEqual(res.title, "مقطع تجريبي")
            self.assertEqual(res.platform, "تيك توك (TikTok)")
            self.assertIsNotNone(res.duration)


if __name__ == "__main__":
    unittest.main()
