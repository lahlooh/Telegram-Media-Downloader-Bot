import os
import re
import html
import asyncio
import time
import uuid
import shutil
import logging
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    InputMediaPhoto,
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

from config import BOT_TOKEN, DEV_CREDIT, DOWNLOAD_DIR

logger = logging.getLogger("bot.main")
from downloader import (
    MediaDownloader,
    DownloadResult,
    TikTokMediaInfo,
    is_tiktok_url,
    chunk_media_list,
    VideoTooLargeError,
    ContentUnavailableError,
    InvalidURLError,
    DownloaderError,
    sanitize_url,
    clean_error_message,
)

URL_REGEX = re.compile(r'(https?://[^\s<>"]+)')
downloader = MediaDownloader()

_bot_username: str | None = None


async def get_bot_username(bot: Bot) -> str:
    global _bot_username
    if not _bot_username:
        me = await bot.get_me()
        _bot_username = me.username or "VideoDownloaderBot"
    return _bot_username


async def send_chat_action_safe(bot: Bot, chat_id: int, action: str) -> None:
    try:
        await bot.send_chat_action(chat_id=chat_id, action=action)
    except Exception:
        pass


@dataclass(slots=True)
class PendingTikTokChoice:
    user_id: int
    title: str
    photo_url: str
    music_url: str | None
    reply_to_message_id: int
    created_at: float


pending_tiktok_requests: dict[str, PendingTikTokChoice] = {}


def cleanup_expired_requests() -> None:
    now = time.time()
    expired = [k for k, v in pending_tiktok_requests.items() if now - v.created_at > 3600]
    for k in expired:
        pending_tiktok_requests.pop(k, None)


def format_duration(seconds: Any) -> str:
    try:
        total_seconds = int(round(float(seconds)))
        if total_seconds <= 0:
            return "غير محدد"
        minutes, secs = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours > 0 else f"{minutes:02d}:{secs:02d}"
    except (ValueError, TypeError):
        return "غير محدد"


async def safe_edit_message(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.debug(f"edit message skipped: {e}")
    except Exception as e:
        logger.debug(f"edit message error: {e}")


async def safe_delete_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"delete message error: {e}")


dp = Dispatcher()


@dp.message(CommandStart())
async def handle_start(message: Message):
    user_name = html.escape(message.from_user.full_name if message.from_user else "صديقي")
    welcome_text = (
        f"أهلاً <b>{user_name}</b> 👋\n\n"
        "أرسل رابط أي فيديو (تيك توك، انستا، يوتيوب، تويتر، فيسبوك...) وبنزله لك مباشرة بأعلى جودة.\n\n"
        f"<b>{DEV_CREDIT}</b>"
    )
    await message.answer(welcome_text, parse_mode=ParseMode.HTML)


@dp.message(Command("help"))
async def handle_help(message: Message):
    help_text = (
        "📌 <b>طريقة الاستخدام:</b>\n"
        "أرسل رابط الفيديو مباشرة في المحادثة.\n\n"
        "⚠️ <b>ملاحظات:</b>\n"
        "- الحد الأقصى لحجم الفيديو في تيليجرام 50MB.\n"
        "- الحسابات الخاصة والمقاطع المحمية لا يمكن تحميلها.\n\n"
        f"<b>{DEV_CREDIT}</b>"
    )
    await message.answer(help_text, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data.startswith("tt:"))
async def handle_tiktok_choice(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    cleanup_expired_requests()

    parts = callback.data.split(":")
    if len(parts) != 3:
        return
    _, action, session_id = parts

    req = pending_tiktok_requests.get(session_id)
    if not req:
        if callback.message:
            await safe_edit_message(
                callback.message,
                f"⚠️ <b>انتهت صلاحية هذا الطلب، أرسل الرابط مرة أخرى.</b>\n\n<b>{DEV_CREDIT}</b>"
            )
        return

    if callback.from_user and callback.from_user.id != req.user_id:
        await callback.answer("⚠️ هذا الخيار مخصص لصاحب الرسالة فقط.", show_alert=True)
        return

    pending_tiktok_requests.pop(session_id, None)

    temp_dir = DOWNLOAD_DIR / uuid.uuid4().hex
    temp_dir.mkdir(parents=True, exist_ok=True)
    clean_title = html.escape(req.title)
    bot_username = await get_bot_username(bot)

    try:
        if action == "vid":
            if callback.message:
                await safe_edit_message(
                    callback.message,
                    "🎬 <b>جاري تجهيز الفيديو...</b>\n<i>صلِّ على النبي ﷺ</i>"
                )

            photos = await downloader.download_tiktok_photos([req.photo_url], temp_dir)
            if not photos:
                raise ContentUnavailableError("تعذر تنزيل الصورة لإنشاء الفيديو.")
            audio = await downloader.download_tiktok_audio(req.music_url, temp_dir)

            res = await downloader.render_tiktok_photo_video(photos[0], audio, temp_dir, req.title)

            display_title = clean_title.strip() if clean_title and clean_title.strip() and clean_title != "فيديو بدون عنوان" else "محتوى تيك توك"
            caption = (
                f"🎬 <b>{display_title}</b>\n\n"
                f"⏱ {format_duration(res.duration)} | 📦 {res.file_size_mb:.1f} MB\n\n"
                f"@{bot_username}\n"
                f"{DEV_CREDIT}"
            )

            await send_chat_action_safe(bot, callback.message.chat.id, "upload_video")

            thumb_file = FSInputFile(str(res.thumbnail_path)) if res.thumbnail_path and res.thumbnail_path.exists() else None
            await callback.message.answer_video(
                video=FSInputFile(str(res.video_path)),
                caption=caption,
                parse_mode=ParseMode.HTML,
                duration=res.duration,
                width=res.width,
                height=res.height,
                thumbnail=thumb_file,
                supports_streaming=True,
                reply_to_message_id=req.reply_to_message_id,
            )
            if callback.message:
                await safe_delete_message(callback.message)

        elif action == "img":
            if callback.message:
                await safe_edit_message(callback.message, "🖼️ <b>جاري إرسال الصورة...</b>")

            photos = await downloader.download_tiktok_photos([req.photo_url], temp_dir)
            if not photos:
                raise ContentUnavailableError("تعذر تنزيل الصورة.")
            audio = await downloader.download_tiktok_audio(req.music_url, temp_dir)

            caption = (
                f"🖼️ <b>{clean_title}</b>\n\n"
                f"@{bot_username}\n"
                f"{DEV_CREDIT}"
            )

            await send_chat_action_safe(bot, callback.message.chat.id, "upload_photo")
            await callback.message.answer_photo(
                photo=FSInputFile(str(photos[0])),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=req.reply_to_message_id,
            )

            if audio and audio.exists():
                await send_chat_action_safe(bot, callback.message.chat.id, "upload_voice")
                await callback.message.answer_audio(
                    audio=FSInputFile(str(audio)),
                    caption=f"🎵 {clean_title}\n{DEV_CREDIT}",
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=req.reply_to_message_id,
                )

            if callback.message:
                await safe_delete_message(callback.message)

    except Exception as e:
        logger.exception(f"Error handling TikTok choice ({action}): {e}")
        if callback.message:
            await safe_edit_message(
                callback.message,
                f"❌ <b>حدث خطأ أثناء المعالجة:</b> {html.escape(clean_error_message(str(e)))}\n\n<b>{DEV_CREDIT}</b>"
            )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def _handle_tiktok_single_photo(
    message: Message, status_msg: Message, tt_info: TikTokMediaInfo
) -> None:
    cleanup_expired_requests()
    session_id = uuid.uuid4().hex[:10]
    pending_tiktok_requests[session_id] = PendingTikTokChoice(
        user_id=message.from_user.id if message.from_user else 0,
        title=tt_info.title,
        photo_url=tt_info.photo_urls[0],
        music_url=tt_info.music_url,
        reply_to_message_id=message.message_id,
        created_at=time.time(),
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 فيديو", callback_data=f"tt:vid:{session_id}"),
                InlineKeyboardButton(text="🖼️ صورة", callback_data=f"tt:img:{session_id}"),
            ]
        ]
    )

    clean_title = html.escape(tt_info.title)
    prompt_text = (
        f"<b>{clean_title}</b>\n\n"
        "تبيه فيديو بالصوت ولا صورة؟\n\n"
        f"<b>{DEV_CREDIT}</b>"
    )
    await safe_edit_message(status_msg, prompt_text, reply_markup=keyboard)


async def _handle_tiktok_album(
    message: Message, status_msg: Message, bot: Bot, tt_info: TikTokMediaInfo
) -> None:
    await safe_edit_message(
        status_msg,
        f"📥 <b>جاري تحميل الصور ({len(tt_info.photo_urls)})...</b>\n<i>صلِّ على النبي ﷺ</i>"
    )

    temp_dir = DOWNLOAD_DIR / uuid.uuid4().hex
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        photos = await downloader.download_tiktok_photos(tt_info.photo_urls, temp_dir)
        if not photos:
            raise ContentUnavailableError("تعذر تنزيل صور المنشور.")

        audio = await downloader.download_tiktok_audio(tt_info.music_url, temp_dir)
        bot_username = await get_bot_username(bot)
        clean_title = html.escape(tt_info.title)

        for idx, chunk in enumerate(chunk_media_list(photos, 10)):
            media_group = [
                InputMediaPhoto(
                    media=FSInputFile(str(p)),
                    caption=(
                        f"📸 <b>{clean_title}</b>\n\n🖼 {len(photos)} صور\n\n@{bot_username}\n{DEV_CREDIT}"
                        if idx == 0 and i == 0 else None
                    ),
                    parse_mode=ParseMode.HTML,
                )
                for i, p in enumerate(chunk)
            ]

            await send_chat_action_safe(bot, message.chat.id, "upload_photo")
            await message.answer_media_group(
                media=media_group,
                reply_to_message_id=message.message_id if idx == 0 else None,
            )

        if audio and audio.exists():
            await send_chat_action_safe(bot, message.chat.id, "upload_voice")
            await message.answer_audio(
                audio=FSInputFile(str(audio)),
                caption=f"🎵 {clean_title}\n{DEV_CREDIT}",
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message.message_id,
            )

        await safe_delete_message(status_msg)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def _handle_standard_download(
    clean_url: str, message: Message, status_msg: Message, bot: Bot
) -> None:
    download_result: DownloadResult | None = None

    try:
        await safe_edit_message(status_msg, "📥 <b>جاري التحميل...</b>\n<i>صلِّ على النبي ﷺ</i>")
        download_result = await downloader.download_video(clean_url)

        await safe_edit_message(status_msg, "📤 <b>جاري رفع الفيديو...</b>")
        bot_username = await get_bot_username(bot)
        clean_title = html.escape(download_result.title).strip()
        display_title = clean_title if clean_title and clean_title != "فيديو بدون عنوان" else "محتوى الوسائط"

        caption = (
            f"🎬 <b>{display_title}</b>\n\n"
            f"📌 {download_result.platform}\n"
            f"⏱ {format_duration(download_result.duration)} | 📦 {download_result.file_size_mb:.1f} MB\n\n"
            f"@{bot_username}\n"
            f"{DEV_CREDIT}"
        )

        thumb_file = (
            FSInputFile(str(download_result.thumbnail_path))
            if download_result.thumbnail_path and download_result.thumbnail_path.exists()
            else None
        )

        await send_chat_action_safe(bot, message.chat.id, "upload_video")
        await message.answer_video(
            video=FSInputFile(str(download_result.video_path)),
            caption=caption,
            parse_mode=ParseMode.HTML,
            duration=download_result.duration,
            width=download_result.width,
            height=download_result.height,
            thumbnail=thumb_file,
            supports_streaming=True,
            reply_to_message_id=message.message_id
        )
        await safe_delete_message(status_msg)

    except VideoTooLargeError as e:
        err_msg = f"⚠️ <b>حجم الفيديو كبير ({e.size_mb:.1f} MB)</b>\nالحد الأقصى في تيليجرام 50MB."
    except ContentUnavailableError:
        err_msg = "🔒 <b>المحتوى غير متاح أو الحساب خاص.</b>"
    except InvalidURLError:
        err_msg = "⚠️ <b>الرابط غير صالح أو غير مدعوم.</b>"
    except DownloaderError as e:
        err_msg = f"❌ <b>تعذر تنزيل هذا المقطع:</b>\n{html.escape(clean_error_message(str(e)))}"
    except Exception as e:
        logger.exception(f"Unexpected error for {clean_url}: {e}")
        err_msg = "❌ <b>حدث خطأ أثناء تحميل المقطع، حاول مرة أخرى لاحقاً.</b>"
    else:
        return
    finally:
        if download_result:
            download_result.cleanup()

    await safe_edit_message(status_msg, f"{err_msg}\n\n<b>{DEV_CREDIT}</b>")


@dp.message(F.text)
async def handle_url_message(message: Message, bot: Bot):
    text = message.text or ""
    match = URL_REGEX.search(text)
    if not match:
        return

    clean_url = sanitize_url(match.group(1).strip())
    user_id = message.from_user.id if message.from_user else "Unknown"
    logger.info(f"Request from {user_id}: {clean_url}")

    status_msg = await message.reply(
        "⚡ <b>جاري فحص الرابط...</b>\n<i>صلِّ على النبي ﷺ</i>",
        parse_mode=ParseMode.HTML
    )

    if is_tiktok_url(clean_url):
        try:
            tt_info = await downloader.get_tiktok_info(clean_url)
            if tt_info.media_type == "photos":
                if len(tt_info.photo_urls) == 1:
                    await _handle_tiktok_single_photo(message, status_msg, tt_info)
                    return
                elif len(tt_info.photo_urls) > 1:
                    await _handle_tiktok_album(message, status_msg, bot, tt_info)
                    return
        except Exception as e:
            logger.debug(f"TikTok photo bypass error: {e}")

    await _handle_standard_download(clean_url, message, status_msg, bot)


async def start_health_check_server():
    port_str = os.getenv("PORT")
    if not port_str:
        return None

    try:
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/", lambda _: web.Response(text="OK"))
        app.router.add_get("/health", lambda _: web.Response(text="OK"))

        runner = web.AppRunner(app)
        await runner.setup()
        port = int(port_str)
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"Health check server listening on port {port}")
        return runner
    except Exception as e:
        logger.warning(f"Health server failed on port {port_str}: {e}")
        return None


async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not configured in .env")
        return

    health_runner = await start_health_check_server()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    logger.info("Starting bot...")
    await bot.delete_webhook(drop_pending_updates=True)

    try:
        username = await get_bot_username(bot)
        logger.info(f"Connected as @{username}")
        await dp.start_polling(bot)
    finally:
        if health_runner:
            try:
                await health_runner.cleanup()
            except Exception:
                pass
        await bot.session.close()
        logger.info("Bot session closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
