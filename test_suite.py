"""
اختبارات برمجية شاملة للتحقق من سلامة كافة مكونات البوت، محرك التنزيل، تنظيف الروابط،
فك تحويل الروابط المختصرة (vt/vm.tiktok)، تنظيف رسائل الخطأ من ANSI، وتسريع الأداء.
"""

import unittest
import asyncio
import time
import subprocess
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
        """فحص إعدادات التكوين والمسارات وترويسات تيك توك وحقوق المطور."""
        self.assertEqual(MAX_FILE_SIZE_MB, 48)
        self.assertEqual(MAX_FILE_SIZE_BYTES, 48 * 1024 * 1024)
        self.assertTrue(DOWNLOAD_DIR.exists())
        self.assertIsNotNone(FFMPEG_PATH)
        self.assertTrue(Path(FFMPEG_PATH).exists())
        self.assertEqual(DEV_CREDIT, "Developed By discord:- 4.7e")
        self.assertIn("iPhone", TIKTOK_MOBILE_USER_AGENT)
        self.assertEqual(TIKTOK_HTTP_HEADERS["User-Agent"], TIKTOK_MOBILE_USER_AGENT)

    def test_clean_error_message(self):
        """فحص تنظيف رسائل الأخطاء من أكواد ANSI وكلمات ERROR المكررة."""
        raw_ansi = "\x1b[0;31mERROR: [0m31mERROR: [0m[tiktok] 10054: Remote closed"
        cleaned = clean_error_message(raw_ansi)
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("31m", cleaned)
        self.assertEqual(cleaned, "[tiktok] 10054: Remote closed")

    def test_sanitize_url_twitter(self):
        """فحص تنظيف روابط إكس / تويتر وحذف اللواحق والبارامترات."""
        test_cases = [
            (
                "https://x.com/getouuxx/status/1899999999999999999/video/1?s=20&t=abcdef",
                "https://x.com/getouuxx/status/1899999999999999999"
            ),
            (
                "https://twitter.com/nasa/status/123456789/photo/1",
                "https://x.com/nasa/status/123456789"
            ),
            (
                "https://vxtwitter.com/user/status/999888777?s=19",
                "https://x.com/user/status/999888777"
            ),
            (
                "https://x.com/i/status/1122334455/video/1",
                "https://x.com/i/status/1122334455"
            ),
            (
                "https://www.tiktok.com/@creator/video/123456789?is_from_webapp=1&sender_device=pc",
                "https://www.tiktok.com/@creator/video/123456789"
            ),
            (
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            ),
            (
                "https://youtu.be/dQw4w9WgXcQ?si=abcdef",
                "https://youtu.be/dQw4w9WgXcQ"
            ),
        ]
        for raw, expected in test_cases:
            cleaned = sanitize_url(raw)
            self.assertEqual(cleaned, expected)

    def test_resolve_redirect_url_non_short(self):
        """فحص تجاوز فك التحويل للروابط غير المختصرة لضمان السرعة."""
        normal_url = "https://www.instagram.com/reel/DczczxJPRHc/"
        result = asyncio.run(resolve_redirect_url(normal_url))
        self.assertEqual(result, normal_url)

    def test_format_duration(self):
        """فحص تنسيق المدة الزمنية للأعداد الصحيحة والعشرية (float)."""
        self.assertEqual(format_duration(None), "غير محدد")
        self.assertEqual(format_duration(0), "غير محدد")
        self.assertEqual(format_duration(45), "00:45")
        self.assertEqual(format_duration(14.52), "00:15")
        self.assertEqual(format_duration(65.4), "01:05")
        self.assertEqual(format_duration(3665.8), "01:01:06")
        self.assertEqual(format_duration("invalid"), "غير محدد")

    def test_url_regex(self):
        """فحص استخراج الروابط من الرسائل النصية."""
        test_cases = [
            ("https://x.com/getouuxx/status/1899999999999999999/video/1?s=20", "https://x.com/getouuxx/status/1899999999999999999/video/1?s=20"),
            ("https://vm.tiktok.com/ZMhABC123/", "https://vm.tiktok.com/ZMhABC123/"),
            ("شاهد هذا المقطع https://www.instagram.com/reel/C3_abc123/ رائع جداً", "https://www.instagram.com/reel/C3_abc123/"),
            ("رسالة عادية بدون رابط", None),
        ]
        for text, expected in test_cases:
            match = URL_REGEX.search(text)
            if expected:
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1), expected)
            else:
                self.assertIsNone(match)

    def test_platform_detection(self):
        """فحص التعرف على المنصات."""
        self.assertIn("TikTok", MediaDownloader._detect_platform("tiktok"))
        self.assertIn("Instagram", MediaDownloader._detect_platform("instagram"))
        self.assertIn("YouTube", MediaDownloader._detect_platform("youtube"))
        self.assertIn("Twitter", MediaDownloader._detect_platform("twitter"))
        self.assertIn("Twitter", MediaDownloader._detect_platform("x"))
        self.assertIn("Reddit", MediaDownloader._detect_platform("reddit"))
        self.assertIn("Pinterest", MediaDownloader._detect_platform("pinterest"))

    def test_sanitize_title(self):
        """فحص تنظيف عناوين الفيديوهات."""
        self.assertEqual(MediaDownloader._sanitize_title(""), "فيديو بدون عنوان")
        long_title = "A" * 150
        sanitized = MediaDownloader._sanitize_title(long_title)
        self.assertLessEqual(len(sanitized), 80)
        self.assertTrue(sanitized.endswith("..."))

    def test_ydl_options_builder_speed_optimizations(self):
        """فحص خيارات تسريع yt-dlp ومسار FFmpeg و preset ultrafast."""
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
        """فحص بنية الاستثناءات المخصصة."""
        large_err = VideoTooLargeError(55.4)
        self.assertIsInstance(large_err, DownloaderError)
        self.assertAlmostEqual(large_err.size_mb, 55.4)
        self.assertIn("55.4", str(large_err))

        self.assertTrue(issubclass(ContentUnavailableError, DownloaderError))
        self.assertTrue(issubclass(InvalidURLError, DownloaderError))

    def test_is_tiktok_url(self):
        """فحص التعرف على كافة صيغ روابط تيك توك."""
        self.assertTrue(is_tiktok_url("https://www.tiktok.com/@user/video/123456789"))
        self.assertTrue(is_tiktok_url("https://www.tiktok.com/@user/photo/123456789"))
        self.assertTrue(is_tiktok_url("https://vm.tiktok.com/ZMhABC123/"))
        self.assertTrue(is_tiktok_url("https://vt.tiktok.com/ZS-abc123/"))
        self.assertFalse(is_tiktok_url("https://www.instagram.com/reel/123/"))
        self.assertFalse(is_tiktok_url("https://x.com/user/status/123"))
        self.assertFalse(is_tiktok_url(""))

    def test_chunk_media_list(self):
        """فحص تقسيم ألبومات الصور بما يتوافق مع حدود تيليجرام (بين 2 و 10 عناصر عند التعدد)."""
        # قائمة فارغة
        self.assertEqual(chunk_media_list([]), [])

        # صورة واحدة
        single = [1]
        self.assertEqual(chunk_media_list(single), [[1]])

        # 5 صور
        five = list(range(5))
        chunks_5 = chunk_media_list(five)
        self.assertEqual(chunks_5, [five])

        # 10 صور
        ten = list(range(10))
        chunks_10 = chunk_media_list(ten)
        self.assertEqual(chunks_10, [ten])

        # 11 صورة (يجب أن تقسم 9 و 2 حتى لا يتبقى عنصر وحيد)
        eleven = list(range(11))
        chunks_11 = chunk_media_list(eleven)
        self.assertEqual(len(chunks_11), 2)
        self.assertEqual(len(chunks_11[0]), 9)
        self.assertEqual(len(chunks_11[1]), 2)

        # 15 صورة (10 ثم 5)
        fifteen = list(range(15))
        chunks_15 = chunk_media_list(fifteen)
        self.assertEqual(len(chunks_15), 2)
        self.assertEqual(len(chunks_15[0]), 10)
        self.assertEqual(len(chunks_15[1]), 5)

        # فحص شامل لكافة الأعداد حتى 35
        for count in range(2, 36):
            c_list = chunk_media_list(list(range(count)))
            for c in c_list:
                self.assertGreaterEqual(len(c), 2, f"Chunk size too small for count {count}")
                self.assertLessEqual(len(c), 10, f"Chunk size exceeds 10 for count {count}")

    def test_tiktok_media_info_dataclass(self):
        """فحص كائن TikTokMediaInfo والقيم الافتراضية."""
        info = TikTokMediaInfo(media_type="photos", title="اختبار الصور")
        self.assertEqual(info.media_type, "photos")
        self.assertEqual(info.title, "اختبار الصور")
        self.assertEqual(info.photo_urls, [])
        self.assertIsNone(info.video_url)
        self.assertIsNone(info.music_url)

    def test_pending_tiktok_choice_cleanup(self):
        """فحص حفظ وتنظيف طلبات الخيارات التفاعلية المنتهية الصلاحية."""
        now = time.time()
        # إضافة طلب منتهي الصلاحية (أقدم من ساعة)
        pending_tiktok_requests["expired_test"] = PendingTikTokChoice(
            user_id=123,
            title="قديم",
            photo_url="https://example.com/1.jpg",
            music_url=None,
            reply_to_message_id=999,
            created_at=now - 4000,
        )
        # إضافة طلب ساري الصلاحية
        pending_tiktok_requests["valid_test"] = PendingTikTokChoice(
            user_id=456,
            title="جديد",
            photo_url="https://example.com/2.jpg",
            music_url=None,
            reply_to_message_id=1000,
            created_at=now,
        )

        cleanup_expired_requests()
        self.assertNotIn("expired_test", pending_tiktok_requests)
        self.assertIn("valid_test", pending_tiktok_requests)

        # تنظيف بعد الاختبار
        pending_tiktok_requests.pop("valid_test", None)

    def test_render_photo_video_ffmpeg(self):
        """فحص إنشاء فيديو MP4 محلياً عبر FFmpeg من صورة وصوت مولد."""
        import tempfile
        downloader = MediaDownloader()
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            img_path = tmp_dir / "test.jpg"
            # إنشاء صورة اختبارية عبر FFmpeg
            create_img_cmd = [
                FFMPEG_PATH, "-y",
                "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=1",
                "-vframes", "1",
                str(img_path)
            ]
            subprocess.run(create_img_cmd, capture_output=True)
            self.assertTrue(img_path.exists())

            # دمج الصورة في فيديو
            res = downloader._render_photo_video_sync(img_path, None, tmp_dir, "فيديو تجريبي")
            self.assertTrue(res.video_path.exists())
            self.assertGreater(res.file_size_bytes, 1000)
            self.assertEqual(res.title, "فيديو تجريبي")
            self.assertEqual(res.platform, "تيك توك (TikTok)")
            self.assertIsNotNone(res.duration)

    def test_database_operations(self):
        """فحص عمليات قاعدة البيانات (تسجيل، إحصائيات، مشرفين، حظر، تقرير يومي)."""
        import database
        database.init_db()
        test_uid = 999111222
        try:
            database.add_or_update_user(test_uid, "tester", "Test First")
            stats = database.get_user_stats()
            self.assertGreaterEqual(stats["total"], 1)
            self.assertGreaterEqual(stats["active"], 1)

            active_users = database.get_active_user_ids()
            self.assertIn(test_uid, active_users)

            # اختبار فحص المشرف
            self.assertFalse(database.is_admin(test_uid))
            database.add_admin(test_uid)
            self.assertTrue(database.is_admin(test_uid))
            database.remove_admin(test_uid)
            self.assertFalse(database.is_admin(test_uid))

            # اختبار حظر المستخدم
            database.mark_user_blocked(test_uid)
            active_after = database.get_active_user_ids()
            self.assertNotIn(test_uid, active_after)

            # اختبار تسجيل التنزيل والتقرير اليومي
            database.record_download(test_uid, "تيك توك (TikTok)", "https://vm.tiktok.com/test", "عنوان اختباري", 1024)
            report_data = database.get_daily_report_data(hours=24)
            self.assertGreaterEqual(report_data["total_downloads_period"], 1)
            formatted = database.format_daily_report(report_data)
            self.assertIn("التقرير اليومي لنشاط البوت", formatted)
            self.assertIn("تيك توك (TikTok)", formatted)
        finally:
            with database.get_connection() as conn:
                conn.cursor().execute("DELETE FROM users WHERE user_id = ?", (test_uid,))
                conn.cursor().execute("DELETE FROM admins WHERE user_id = ?", (test_uid,))
                conn.cursor().execute("DELETE FROM downloads WHERE user_id = ?", (test_uid,))
                conn.commit()

    def test_audio_mp3_extraction(self):
        """فحص استخراج الصوت MP3 من ملف فيديو عبر FFmpeg."""
        import tempfile
        from downloader import extract_audio_mp3_sync
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            vid_path = tmp_dir / "sample.mp4"
            mp3_path = tmp_dir / "sample.mp3"

            # إنشاء فيديو اختباري مع صوت عبر FFmpeg
            create_vid_cmd = [
                FFMPEG_PATH, "-y",
                "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
                "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
                "-c:v", "libx264", "-c:a", "aac",
                str(vid_path)
            ]
            subprocess.run(create_vid_cmd, capture_output=True)
            self.assertTrue(vid_path.exists())

            # استخراج الصوت MP3
            res_audio = extract_audio_mp3_sync(vid_path, mp3_path)
            self.assertIsNotNone(res_audio)
            self.assertTrue(res_audio.exists())
            self.assertGreater(res_audio.stat().st_size, 500)

    def test_settings_and_platforms(self):
        """فحص إعدادات البوت والمنصات ووضع الصيانة والاشتراك الإجباري."""
        import database
        database.init_db()

        # فحص وضع الصيانة
        orig_maint = database.is_maintenance_mode()
        database.set_maintenance_mode(True)
        self.assertTrue(database.is_maintenance_mode())
        database.set_maintenance_mode(False)
        self.assertFalse(database.is_maintenance_mode())
        database.set_maintenance_mode(orig_maint)

        # فحص الاشتراك الإجباري
        orig_fsub = database.is_force_sub_enabled()
        orig_ch = database.get_force_sub_channel()
        database.set_force_sub_channel("@TestChannel")
        self.assertEqual(database.get_force_sub_channel(), "@TestChannel")
        database.set_force_sub_enabled(True)
        self.assertTrue(database.is_force_sub_enabled())
        database.set_force_sub_enabled(False)
        self.assertFalse(database.is_force_sub_enabled())
        database.set_force_sub_channel(orig_ch)
        database.set_force_sub_enabled(orig_fsub)

        # فحص تفعيل وتعطيل المنصات
        orig_tt = database.is_platform_enabled("tiktok")
        database.set_platform_enabled("tiktok", False)
        self.assertFalse(database.is_platform_enabled("tiktok"))
        database.set_platform_enabled("tiktok", True)
        self.assertTrue(database.is_platform_enabled("tiktok"))
        database.set_platform_enabled("tiktok", orig_tt)

    def test_admin_dashboard_components(self):
        """فحص لوحات المفاتيح والأشرطة البيانية ومراقبة psutil."""
        import admin
        import psutil

        kb_main = admin.get_main_dashboard_keyboard()
        self.assertIsNotNone(kb_main)
        self.assertTrue(len(kb_main.inline_keyboard) >= 4)

        kb_settings = admin.get_settings_keyboard()
        self.assertIsNotNone(kb_settings)

        kb_plats = admin.get_platforms_keyboard()
        self.assertIsNotNone(kb_plats)

        bar = admin.make_progress_bar(50, length=10)
        self.assertEqual(len(bar), 10)
        self.assertEqual(bar, "█████░░░░░")

        uptime = admin.format_uptime(3665)
        self.assertIn("1 ساعة", uptime)

        # فحص psutil
        vm = psutil.virtual_memory()
        self.assertGreater(vm.total, 0)
        self.assertGreaterEqual(vm.percent, 0.0)

    def test_youtube_options_and_cookies(self):
        """فحص إعدادات يوتيوب وتمرير ملف cookies.txt وقائمة player_client بالترتيب المطلوب."""
        downloader = MediaDownloader()
        opts = downloader._build_ydl_options(
            DOWNLOAD_DIR,
            "best",
            download_thumbnail=True,
            use_syndication=False,
            is_tiktok=False,
        )
        self.assertIn("extractor_args", opts)
        self.assertIn("youtube", opts["extractor_args"])
        self.assertEqual(
            opts["extractor_args"]["youtube"]["player_client"],
            ["ios", "android", "web"]
        )
        self.assertIn("remote_components", opts)
        self.assertIn("ejs:github", opts["remote_components"])

        # فحص تمرير الكوكيز مباشرة إلى خيارات YoutubeDL عند وجود الملف
        cookies_file = Path(__file__).resolve().parent / "cookies.txt"
        if cookies_file.is_file() and cookies_file.stat().st_size > 0:
            self.assertIn("cookiefile", opts)
            self.assertEqual(Path(opts["cookiefile"]).resolve(), cookies_file.resolve())


if __name__ == "__main__":
    unittest.main()
