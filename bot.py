"""
الملف الرئيسي لتشغيل بوت تيليجرام (Telegram Bot Application)
مبني بالكامل على aiogram 3.x بنظام غير متزامن (Async/Await)
يتعرف تلقائياً على الروابط ويُحدّث الحالة لحظياً مع تنظيف فوري للملفات.
"""

import os
import re
import html
import asyncio
import time
import uuid
import shutil
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command, StateFilter
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
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext

import database
from admin import admin_router, handle_admin_command, BOT_START_TIME
from config import BOT_TOKEN, DEV_CREDIT, DOWNLOAD_DIR, ADMIN_IDS, logger, get_bot_token
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

# تعبير نمطي قوي ودقيق لالتقاط الروابط بالكامل دون بتر أي مسار أو بارامتر
URL_REGEX = re.compile(r'(https?://[^\s<>"]+)')

# إنشاء كائن المحرك
downloader = MediaDownloader()


@dataclass
class PendingTikTokChoice:
    """كائن حفظ خيارات تيك توك للصورة الواحدة أثناء انتظار رد المستخدم."""
    user_id: int
    title: str
    photo_url: str
    music_url: Optional[str]
    reply_to_message_id: int
    created_at: float


# جدول الطلبات المؤقتة في الذاكرة
pending_tiktok_requests: Dict[str, PendingTikTokChoice] = {}


def cleanup_expired_requests() -> None:
    """حذف الطلبات المنتهية الصلاحية (أكثر من 60 دقيقة)."""
    now = time.time()
    expired = [k for k, v in pending_tiktok_requests.items() if now - v.created_at > 3600]
    for k in expired:
        pending_tiktok_requests.pop(k, None)


def format_duration(seconds: Optional[Any]) -> str:
    """تنسيق المدة الزمنية بالثواني إلى صيغة دقائق وثواني مقروءة مع تحويل آمن للأعداد الصحيحة."""
    if seconds is None:
        return "غير محدد"
    try:
        total_seconds = int(round(float(seconds)))
        if total_seconds <= 0:
            return "غير محدد"
        minutes, secs = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"
    except (ValueError, TypeError):
        return "غير محدد"


async def safe_edit_message(
    message: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None
) -> None:
    """تحديث رسالة الحالة مع تجاهل أخطاء التعديل المتطابق أو القيود المؤقتة."""
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.debug(f"تخطي خطأ تعديل الرسالة: {e}")
    except Exception as e:
        logger.debug(f"خطأ أثناء تعديل الرسالة: {e}")


async def safe_delete_message(message: Message) -> None:
    """حذف رسالة بأمان دون توقف الكود عند الفشل."""
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"تعذر حذف الرسالة: {e}")


# ==========================================
# معالجات الأوامر (Command Handlers)
# ==========================================

main_router = Router(name="main_router")
dp = Dispatcher()
dp.include_router(admin_router)
dp.include_router(main_router)


def detect_url_platform(url: str) -> str:
    """التعرف السريع على المنصة من الرابط للتحقق من حالتها في الإعدادات."""
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    elif "instagram.com" in u:
        return "instagram"
    elif "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    elif "twitter.com" in u or "x.com" in u or "vxtwitter.com" in u:
        return "twitter"
    elif "facebook.com" in u or "fb.watch" in u:
        return "facebook"
    elif "pinterest.com" in u or "pin.it" in u:
        return "pinterest"
    elif "reddit.com" in u:
        return "reddit"
    return "other"


async def check_user_access(message: Message, bot: Bot) -> bool:
    """
    التحقق من أهلية المستخدم لاستخدام البوت:
    1. تسجيل/تحديث المستخدم.
    2. فحص الحظر (is_user_banned).
    3. فحص وضع الصيانة (is_maintenance_mode).
    4. فحص الاشتراك الإجباري (is_force_sub_enabled).
    """
    if not message.from_user:
        return False

    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name or ""

    # تسجيل وتحديث المستخدم في قاعدة البيانات
    database.add_or_update_user(user_id, username, full_name)

    # 1. فحص الحظر
    if database.is_user_banned(user_id):
        await message.answer(
            "🚫 <b>عذراً، تم حظر حسابك من استخدام البوت لمخالفة شروط الاستخدام.</b>\n\n"
            f"───────────────\n<b>{DEV_CREDIT}</b>",
            parse_mode=ParseMode.HTML
        )
        return False

    # المشرفون مستثنون من وضع الصيانة والاشتراك الإجباري
    if database.is_admin(user_id, ADMIN_IDS):
        return True

    # 2. فحص وضع الصيانة
    if database.is_maintenance_mode():
        await message.answer(
            "🛠 <b>عذراً، البوت في وضع الصيانة حالياً!</b>\n\n"
            "نقوم حالياً بترقية الخوادم وتحسين سرعة واستقرار التحميل.\n"
            "سنعود للعمل قريباً جداً، شكراً لتفهمك وصبرك! 🙏\n\n"
            f"───────────────\n<b>{DEV_CREDIT}</b>",
            parse_mode=ParseMode.HTML
        )
        return False

    # 3. فحص الاشتراك الإجباري
    if database.is_force_sub_enabled():
        channel = database.get_force_sub_channel()
        if channel:
            try:
                chat_member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
                if chat_member.status not in ["member", "administrator", "creator"]:
                    raise ValueError("Not a member")
            except Exception:
                clean_ch = channel.lstrip("@")
                channel_link = f"https://t.me/{clean_ch}"
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📢 اضغط هنا للاشتراك في القناة", url=channel_link)],
                        [InlineKeyboardButton(text="✅ تحقّق من الاشتراك", callback_data="check_sub")],
                    ]
                )
                await message.answer(
                    "📢 <b>عذراً، يجب عليك الاشتراك في قناة البوت الرسمية أولاً لاستخدام البوت:</b>\n\n"
                    f"👉 <b>{channel}</b>\n\n"
                    "بعد الاشتراك، اضغط على زر <b>«تحقّق من الاشتراك ✅»</b> للمتابعة والتحميل فوراً.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb
                )
                return False

    return True


@main_router.callback_query(F.data == "check_sub")
async def handle_check_sub(callback: CallbackQuery, bot: Bot):
    """التحقق من اشتراك المستخدم في القناة الإجبارية عند النقر على الزر."""
    if not callback.from_user:
        return
    channel = database.get_force_sub_channel()
    if not channel:
        await callback.answer("✅ يمكنك استخدام البوت الآن!", show_alert=True)
        if callback.message:
            await safe_delete_message(callback.message)
        return

    try:
        chat_member = await bot.get_chat_member(chat_id=channel, user_id=callback.from_user.id)
        if chat_member.status in ["member", "administrator", "creator"]:
            await callback.answer("✅ تم التحقق من اشتراكك بنجاح! يمكنك الآن إرسال الروابط للتحميل.", show_alert=True)
            if callback.message:
                await safe_delete_message(callback.message)
        else:
            await callback.answer("⚠️ لم تقم بالاشتراك في القناة بعد! يرجى الاشتراك أولاً ثم الضغط هنا.", show_alert=True)
    except Exception as e:
        logger.warning(f"تعذر التحقق من عضوية {callback.from_user.id}: {e}")
        await callback.answer("✅ تم قبول طلبك! يمكنك استخدام البوت الآن.", show_alert=True)
        if callback.message:
            await safe_delete_message(callback.message)


@main_router.message(CommandStart())
async def handle_start(message: Message, bot: Bot):
    """الترحيب بالمستخدم وشرح طريقة الاستخدام والمنصات المدعومة مع حقوق المطور."""
    if not await check_user_access(message, bot):
        return
    user_name = html.escape(message.from_user.full_name if message.from_user else "صديقي")
    welcome_text = (
        f"مرحباً بك يا <b>{user_name}</b> في <b>بوت تحميل الفيديوهات الذكي</b> 🎬⚡\n\n"
        "🚀 <b>مميزات البوت:</b>\n"
        "• تحميل بأعلى دقة صوت وصورة أصلية (Full Quality).\n"
        "• استخراج وإرسال صوت المقطع MP3 تلقائياً مع الفيديو.\n"
        "• دمج الصوت والصورة تلقائياً بدون فقدان في الجودة.\n"
        "• دعم منصات X/Twitter، تيك توك، إنستغرام، يوتيوب وغيرها.\n"
        "• سرعة فائقة ودون الحاجة لأي أوامر معقدة.\n\n"
        "🌐 <b>المنصات المدعومة:</b>\n"
        "• إكس / تويتر (X / Twitter)\n"
        "• تيك توك (TikTok)\n"
        "• إنستغرام (Instagram Reels, Stories, Posts)\n"
        "• يوتيوب (YouTube Videos & Shorts)\n"
        "• فيسبوك (Facebook Reels & Videos)\n"
        "• ريديت (Reddit)\n"
        "• بنترست (Pinterest)\n"
        "• ثريدز، فيميو، سناب شات وغيرها الكثير...\n\n"
        "📥 <b>طريقة الاستخدام:</b>\n"
        "فقط أرسل رابط المقطع مباشرة هنا وسيتكفل البوت بالباقي!\n\n"
        f"───────────────\n"
        f"<b>{DEV_CREDIT}</b>"
    )
    await message.answer(welcome_text, parse_mode=ParseMode.HTML)


@main_router.message(Command("help"))
async def handle_help(message: Message, bot: Bot):
    """تعليمات المساعدة وحل المشكلات مع حقوق المطور."""
    if not await check_user_access(message, bot):
        return
    help_text = (
        "💡 <b>دليل المساعدة والاستخدام:</b>\n\n"
        "1. انسخ رابط أي مقطع من منصتك المفضلة.\n"
        "2. الصق الرابط وأرسله في هذه المحادثة مباشرة.\n"
        "3. سيقوم البوت بسحب أعلى دقة وتوفيرها بصيغة MP4 مع ملف الصوت MP3 تلقائياً.\n\n"
        "⚠️ <b>ملاحظات هامة:</b>\n"
        "• الحد الأقصى لحجم الملف عبر تيليجرام للبوتات هو <b>50 ميجابايت</b>.\n"
        "• في حال تجاوز المقطع 48MB بالجودة الفائقة، يحاول البوت تلقائياً اختيار أفضل دقة تقع تحت الحد.\n"
        "• المقاطع الخاصة (Private) أو التي تتطلب اشتراكاً لا يمكن تنزيلها.\n"
        "• روابط التغريدات الخالية من الفيديو (صور أو نصوص) لا يمكن تحميلها كفيديو.\n\n"
        f"───────────────\n"
        f"<b>{DEV_CREDIT}</b>"
    )
    await message.answer(help_text, parse_mode=ParseMode.HTML)


@main_router.message(Command("id", "myid"))
async def handle_id(message: Message):
    """عرض معرف الحساب والمحادثة ورتبة المستخدم في البوت."""
    if message.from_user:
        database.add_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        user_id = message.from_user.id
        is_adm = database.is_admin(user_id, ADMIN_IDS)
        rank = "مشرف (Admin) 👑" if is_adm else "مستخدم (User) 👤"
        resp = (
            "🆔 <b>معلومات الحساب:</b>\n\n"
            f"• <b>معرف المستخدم (User ID):</b> <code>{user_id}</code>\n"
            f"• <b>معرف المحادثة (Chat ID):</b> <code>{message.chat.id}</code>\n"
            f"• <b>الرتبة:</b> {rank}\n\n"
            "💡 <i>يمكنك وضع هذا الآيدي في ADMIN_ID داخل ملف .env لتصبح مشرفاً.</i>\n\n"
            f"───────────────\n<b>{DEV_CREDIT}</b>"
        )
        await message.answer(resp, parse_mode=ParseMode.HTML)


@main_router.message(Command("stats", "احصائيات", "users"))
async def handle_stats(message: Message):
    """عرض إحصائيات المشتركين في البوت للمشرفين."""
    if not message.from_user:
        return
    database.add_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not database.is_admin(message.from_user.id, ADMIN_IDS):
        await message.answer("⚠️ <b>عذراً، هذا الأمر مخصص لمشرفي البوت فقط.</b>", parse_mode=ParseMode.HTML)
        return

    stats = database.get_user_stats()
    text = (
        "📊 <b>إحصائيات مستخدمي البوت:</b>\n\n"
        f"👥 <b>إجمالي المستخدمين المسجلين:</b> {stats['total']}\n"
        f"✅ <b>المستخدمين النشطين:</b> {stats['active']}\n"
        f"🚫 <b>المستخدمين المحظورين أو المعطلين:</b> {stats['blocked']}\n\n"
        "📢 يمكنك استخدام أمر <code>/bc</code> لإرسال إذاعة لجميع المشتركين.\n\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@main_router.message(Command("report", "daily_report", "تقرير"))
async def handle_report(message: Message):
    """عرض التقرير اليومي الحقيقي لنشاط البوت للمشرفين."""
    if not message.from_user:
        return
    database.add_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not database.is_admin(message.from_user.id, ADMIN_IDS):
        await message.answer("⚠️ <b>عذراً، هذا التقرير مخصص لمشرفي البوت فقط.</b>", parse_mode=ParseMode.HTML)
        return

    data = database.get_daily_report_data(hours=24)
    report_text = database.format_daily_report(data)
    report_hour = database.get_setting("report_hour", "23")
    try:
        report_h_int = int(report_hour)
    except (ValueError, TypeError):
        report_h_int = 23

    footer = (
        f"\n\n⚙️ <i>يتم إرسال هذا التقرير تلقائياً يومياً عند الساعة {report_h_int:02d}:00.</i>\n"
        "<i>يمكنك تغيير موعد الإرسال التلقائي عبر: <code>/set_report_hour 22</code></i>\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>"
    )
    await message.answer(report_text + footer, parse_mode=ParseMode.HTML)


@main_router.message(Command("set_report_hour", "وقت_التقرير"))
async def handle_set_report_hour(message: Message):
    """تحديد ساعة إرسال التقرير اليومي التلقائي للمشرفين (من 0 إلى 23)."""
    if not message.from_user or not database.is_admin(message.from_user.id, ADMIN_IDS):
        await message.answer("⚠️ <b>هذا الأمر مخصص للمشرفين فقط.</b>", parse_mode=ParseMode.HTML)
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit() or not (0 <= int(parts[1]) <= 23):
        await message.answer(
            "⚠️ <b>يرجى كتابة الساعة بنظام 24 (من 0 إلى 23).</b>\n\n"
            "• مثال لإرسال التقرير الساعة 11 مساءً:\n<code>/set_report_hour 23</code>\n"
            "• مثال لإرسال التقرير الساعة 8 مساءً:\n<code>/set_report_hour 20</code>",
            parse_mode=ParseMode.HTML
        )
        return
    new_hour = int(parts[1])
    database.set_setting("report_hour", str(new_hour))
    await message.answer(
        f"✅ <b>تم ضبط موعد إرسال التقرير اليومي التلقائي على الساعة {new_hour:02d}:00 بنجاح!</b>\n"
        "سيرسل لك البوت يومياً تقريراً حقيقياً بأعداد المستخدمين والفيديوهات والمنصات وأسمائهم.",
        parse_mode=ParseMode.HTML
    )



@main_router.message(Command("addadmin"))
async def handle_addadmin(message: Message):
    """إضافة مشرف جديد للبوت."""
    if not message.from_user or not database.is_admin(message.from_user.id, ADMIN_IDS):
        await message.answer("⚠️ <b>هذا الأمر مخصص للمشرفين فقط.</b>", parse_mode=ParseMode.HTML)
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("⚠️ <b>يرجى إرسال الآيدي بعد الأمر، مثال:</b>\n<code>/addadmin 123456789</code>", parse_mode=ParseMode.HTML)
        return
    target_id = int(parts[1])
    database.add_admin(target_id)
    await message.answer(f"✅ <b>تمت إضافة المشرف بنجاح!</b>\nالآيدي: <code>{target_id}</code>", parse_mode=ParseMode.HTML)


@main_router.message(Command("bc", "broadcast", "اذاعة", "نشر"))
async def handle_broadcast(message: Message, bot: Bot):
    """
    نظام الإذاعة الجماعية (Broadcast / BC):
    يتيح للمشرف إرسال رسائل أو مقاطع فيديو أو صور أو تسجيلات صوتية لجميع مستخدمي البوت.
    """
    if not message.from_user:
        return
    database.add_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not database.is_admin(message.from_user.id, ADMIN_IDS):
        await message.answer("⚠️ <b>عذراً، هذا الأمر مخصص لمشرفي البوت فقط للإذاعة الجماعية.</b>", parse_mode=ParseMode.HTML)
        return

    reply_msg = message.reply_to_message
    broadcast_text = ""
    cmd_parts = (message.text or "").split(maxsplit=1)
    if len(cmd_parts) > 1:
        broadcast_text = cmd_parts[1].strip()

    if not reply_msg and not broadcast_text:
        help_bc = (
            "📢 <b>طريقة استخدام الإذاعة الجماعية (Broadcast / BC):</b>\n\n"
            "1️⃣ <b>بالرد (Reply) على أي رسالة:</b>\n"
            "• أرسل في المحادثة أي مقطع فيديو، صورة، تسجيل صوتي، أو نص.\n"
            "• قم بالرد (Reply) على تلك الرسالة واكتب: <code>/bc</code>\n"
            "• سيقوم البوت بنسخها وإرسالها لجميع مستخدمي البوت فوراً بنفس التنسيق.\n\n"
            "2️⃣ <b>إذاعة نصية مباشرة:</b>\n"
            "• اكتب: <code>/bc نص الرسالة التي تريد إرسالها</code>\n\n"
            f"───────────────\n<b>{DEV_CREDIT}</b>"
        )
        await message.answer(help_bc, parse_mode=ParseMode.HTML)
        return

    users = database.get_active_user_ids()
    total_users = len(users)

    if total_users == 0:
        await message.answer("⚠️ <b>لا يوجد مستخدمين مسجلين في قاعدة البيانات حتى الآن!</b>", parse_mode=ParseMode.HTML)
        return

    status_msg = await message.answer(
        f"⏳ <b>جاري بدء الإذاعة الجماعية إلى {total_users} مستخدم...</b>\n"
        "<i>يرجى الانتظار حتى اكتمال الإرسال...</i>",
        parse_mode=ParseMode.HTML
    )

    start_time = time.time()
    success_count = 0
    failed_count = 0

    for u_id in users:
        try:
            if reply_msg:
                # نسخ الرسالة بالكامل (فيديو، صورة، ملف، صوت، نص، ملصق)
                await bot.copy_message(
                    chat_id=u_id,
                    from_chat_id=reply_msg.chat.id,
                    message_id=reply_msg.message_id
                )
            else:
                await bot.send_message(
                    chat_id=u_id,
                    text=broadcast_text,
                    parse_mode=ParseMode.HTML
                )
            success_count += 1
            # تأخير محسوب للالتزام بحدود تيليجرام وتفادي حظر FloodWait
            await asyncio.sleep(0.04)
        except TelegramForbiddenError:
            failed_count += 1
            database.mark_user_blocked(u_id)
        except Exception as e_send:
            failed_count += 1
            err_str = str(e_send).lower()
            if any(w in err_str for w in ["blocked", "deactivated", "chat not found", "user is deactivated"]):
                database.mark_user_blocked(u_id)
            logger.debug(f"فشل إرسال الإذاعة للمستخدم {u_id}: {e_send}")

    duration_sec = int(round(time.time() - start_time))
    report_text = (
        "📢 <b>اكتملت عملية الإذاعة الجماعية (BC) بنجاح!</b> ✅\n\n"
        f"👥 <b>إجمالي المستهدفين:</b> {total_users}\n"
        f"✅ <b>تم الإرسال بنجاح:</b> {success_count}\n"
        f"❌ <b>تعذر الإرسال (حظر أو حذف الحساب):</b> {failed_count}\n"
        f"⏱ <b>المدة المستغرقة:</b> {duration_sec} ثانية\n\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>"
    )
    await safe_edit_message(status_msg, report_text)


# ==========================================
# معالج أزرار خيارات تيك توك (TikTok Inline Choice Handler)
# ==========================================

@main_router.callback_query(F.data.startswith("tt:"))
async def handle_tiktok_choice(callback: CallbackQuery, bot: Bot):
    """معالجة اختيار المستخدم بين الفيديو أو الصورة لمنشور تيك توك ذي الصورة الواحدة."""
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
                "⚠️ <b>انتهت صلاحية هذا الطلب!</b>\nيرجى إعادة إرسال الرابط مرة أخرى.\n\n"
                f"───────────────\n<b>{DEV_CREDIT}</b>"
            )
        return

    # التحقق من أن المستخدم هو صاحب الطلب الأصلي
    if callback.from_user and callback.from_user.id != req.user_id:
        await callback.answer("⚠️ هذا الخيار مخصص لصاحب الرسالة فقط.", show_alert=True)
        return

    # إزالة الطلب من الذاكرة لمنع الضغط المتكرر
    pending_tiktok_requests.pop(session_id, None)

    temp_dir = DOWNLOAD_DIR / str(uuid.uuid4())
    temp_dir.mkdir(parents=True, exist_ok=True)
    clean_title = html.escape(req.title)
    bot_info = await bot.get_me()
    bot_username = bot_info.username or "VideoDownloaderBot"

    try:
        if action == "vid":
            if callback.message:
                await safe_edit_message(
                    callback.message,
                    "🎬 <b>جاري دمج الصورة والصوت وتجهيز الفيديو بأعلى دقة...</b>\n"
                    "<i>اللهم صلِّ وسلّم على نبينا محمد ﷺ</i>"
                )

            # تنزيل الصورة والصوت ودمجهما محلياً عبر FFmpeg
            photos = await downloader.download_tiktok_photos([req.photo_url], temp_dir)
            if not photos:
                raise ContentUnavailableError("تعذر تنزيل الصورة لإنشاء الفيديو.")
            audio = await downloader.download_tiktok_audio(req.music_url, temp_dir)

            res = await downloader.render_tiktok_photo_video(photos[0], audio, temp_dir, req.title)

            display_title = clean_title.strip() if clean_title and clean_title.strip() and clean_title != "فيديو بدون عنوان" else "محتوى تيك توك"
            caption = (
                f"🎬 <b>{display_title}</b>\n\n"
                f"🌐 <b>المنصة:</b> تيك توك (TikTok)\n"
                f"⏱ <b>المدة:</b> {format_duration(res.duration)}\n"
                f"📦 <b>الحجم:</b> {res.file_size_mb:.1f} MB\n"
                f"✨ <b>الجودة:</b> الدقة الأصلية (Original HD)\n\n"
                f"🤖 بواسطة: @{bot_username}\n"
                f"{DEV_CREDIT}"
            )

            try:
                await bot.send_chat_action(chat_id=callback.message.chat.id, action="upload_video")
            except Exception:
                pass

            await callback.message.answer_video(
                video=FSInputFile(str(res.video_path)),
                caption=caption,
                parse_mode=ParseMode.HTML,
                duration=res.duration,
                width=res.width,
                height=res.height,
                thumbnail=FSInputFile(str(res.thumbnail_path)) if res.thumbnail_path and res.thumbnail_path.exists() else None,
                supports_streaming=True,
                reply_to_message_id=req.reply_to_message_id,
            )

            # إرسال صوت المقطع MP3 تحت الفيديو مباشرة
            if res.audio_path and res.audio_path.exists() and res.audio_path.stat().st_size > 500:
                try:
                    await bot.send_chat_action(chat_id=callback.message.chat.id, action="upload_voice")
                    audio_caption = f"🎵 <b>صوت المقطع (MP3):</b> {display_title}\n\n<b>{DEV_CREDIT}</b>"
                    await callback.message.answer_audio(
                        audio=FSInputFile(str(res.audio_path)),
                        title=display_title,
                        performer="تيك توك (TikTok)",
                        caption=audio_caption,
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=req.reply_to_message_id,
                    )
                except Exception as e_audio:
                    logger.warning(f"تعذر إرسال الصوت MP3 في خيار تيك توك: {e_audio}")

            # تسجيل العملية بنجاح في قاعدة البيانات لإحصائيات دقيقة 100%
            if req.user_id:
                database.record_download(
                    user_id=req.user_id,
                    platform="تيك توك (TikTok)",
                    url=req.photo_url,
                    title=req.title,
                    file_size_bytes=res.file_size_bytes,
                )

            if callback.message:
                await safe_delete_message(callback.message)

        elif action == "img":
            if callback.message:
                await safe_edit_message(
                    callback.message,
                    "🖼️ <b>جاري إرسال الصورة بأعلى جودة متوفرة...</b>\n"
                    "<i>اللهم صلِّ وسلّم على نبينا محمد ﷺ</i>"
                )

            photos = await downloader.download_tiktok_photos([req.photo_url], temp_dir)
            if not photos:
                raise ContentUnavailableError("تعذر تنزيل الصورة.")
            audio = await downloader.download_tiktok_audio(req.music_url, temp_dir)

            caption = (
                f"🖼️ <b>{clean_title}</b>\n\n"
                f"🌐 <b>المنصة:</b> تيك توك (TikTok)\n"
                f"✨ <b>الجودة:</b> الدقة الأصلية (Original)\n\n"
                f"🤖 بواسطة: @{bot_username}\n"
                f"{DEV_CREDIT}"
            )

            try:
                await bot.send_chat_action(chat_id=callback.message.chat.id, action="upload_photo")
            except Exception:
                pass

            await callback.message.answer_photo(
                photo=FSInputFile(str(photos[0])),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=req.reply_to_message_id,
            )

            if audio and audio.exists():
                try:
                    await bot.send_chat_action(chat_id=callback.message.chat.id, action="upload_voice")
                except Exception:
                    pass
                audio_caption = f"🎵 <b>الصوت المرفق:</b> {clean_title}\n{DEV_CREDIT}"
                await callback.message.answer_audio(
                    audio=FSInputFile(str(audio)),
                    caption=audio_caption,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=req.reply_to_message_id,
                )

            # تسجيل العملية بنجاح في قاعدة البيانات
            if req.user_id:
                img_size = photos[0].stat().st_size if photos and photos[0].exists() else 0
                database.record_download(
                    user_id=req.user_id,
                    platform="تيك توك (TikTok)",
                    url=req.photo_url,
                    title=req.title,
                    file_size_bytes=img_size,
                )

            if callback.message:
                await safe_delete_message(callback.message)

    except Exception as e:
        logger.exception(f"خطأ أثناء معالجة خيار تيك توك ({action}): {e}")
        if callback.message:
            await safe_edit_message(
                callback.message,
                f"❌ <b>حدث خطأ أثناء المعالجة:</b> {html.escape(clean_error_message(str(e)))}\n\n"
                f"───────────────\n<b>{DEV_CREDIT}</b>"
            )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ==========================================
# معالج الروابط التلقائي (Automatic URL Handler)
# ==========================================

@main_router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def handle_url_message(message: Message, bot: Bot):
    """التقاط الروابط تلقائياً والتحميل الفوري وتحديث الحالات."""
    text = message.text or ""
    match = URL_REGEX.search(text)

    # إذا لم تحتوي الرسالة على أي رابط، نتجاهلها
    if not match:
        return

    # 1. التحقق من أهلية المستخدم (حظر، صيانة، اشتراك إجباري)
    if not await check_user_access(message, bot):
        return

    raw_url = match.group(1).strip()
    clean_url = sanitize_url(raw_url)
    user_id = message.from_user.id if message.from_user else 0
    logger.info(f"طلب تنزيل جديد من {user_id}: {clean_url} (الأصل: {raw_url})")

    # 2. فحص ما إذا كانت المنصة معطلة في لوحة تحكم الإدارة
    plat_key = detect_url_platform(clean_url)
    if plat_key != "other" and not database.is_platform_enabled(plat_key):
        plat_arabic = {
            "tiktok": "تيك توك (TikTok)",
            "instagram": "إنستغرام (Instagram)",
            "youtube": "يوتيوب (YouTube)",
            "twitter": "إكس / تويتر (Twitter/X)",
            "facebook": "فيسبوك (Facebook)",
            "pinterest": "بنترست (Pinterest)",
            "reddit": "ريديت (Reddit)",
        }.get(plat_key, plat_key)
        await message.reply(
            f"⚠️ <b>عذراً، التنزيل من منصة {plat_arabic} معطّل مؤقتاً بأمر الإدارة.</b>\n\n"
            "يرجى تجربة رابط من منصة أخرى أو المحاولة لاحقاً.\n\n"
            f"───────────────\n<b>{DEV_CREDIT}</b>",
            parse_mode=ParseMode.HTML
        )
        return

    # 3. إرسال رسالة الحالة الأولى: فحص الرابط
    status_msg = await message.reply(
        "⚡ <b>جاري فحص الرابط ومعالجة الطلب...</b>\n<i>اللهم صلِّ وسلّم على نبينا محمد ﷺ</i>",
        parse_mode=ParseMode.HTML
    )

    # 2. فحص مخصص لروابط تيك توك لدعم منشورات الصور والخيارات التفاعلية
    if is_tiktok_url(clean_url):
        try:
            tt_info = await downloader.get_tiktok_info(clean_url)
            if tt_info.media_type == "photos":
                # حالة: صورة واحدة فقط مع صوت (طلب المستخدم: يطلبني ودك الفيديو ولا الصورة)
                if len(tt_info.photo_urls) == 1:
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
                                InlineKeyboardButton(
                                    text="🎬 فيديو (مع الصوت)", callback_data=f"tt:vid:{session_id}"
                                ),
                                InlineKeyboardButton(
                                    text="🖼️ صورة", callback_data=f"tt:img:{session_id}"
                                ),
                            ]
                        ]
                    )

                    clean_title = html.escape(tt_info.title)
                    prompt_text = (
                        f"🎬 <b>محتوى تيك توك: صورة واحدة مع صوت</b> 🎵\n\n"
                        f"📝 <b>العنوان:</b> {clean_title}\n\n"
                        f"🤔 <b>ودك الفيديو ولا الصورة؟</b>\n\n"
                        f"───────────────\n"
                        f"<b>{DEV_CREDIT}</b>"
                    )
                    await safe_edit_message(status_msg, prompt_text, reply_markup=keyboard)
                    return

                # حالة: أكثر من صورة (طلب المستخدم: ينزل كل الصور بنفس الجودة)
                elif len(tt_info.photo_urls) > 1:
                    await safe_edit_message(
                        status_msg,
                        f"📥 <b>جاري تحميل الصور بأعلى جودة ({len(tt_info.photo_urls)} صور)...</b>\n"
                        "<i>صلِّ على النبي محمد ﷺ، ثوانٍ وتصلك الصور...</i>"
                    )

                    temp_dir = DOWNLOAD_DIR / str(uuid.uuid4())
                    temp_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        photos = await downloader.download_tiktok_photos(tt_info.photo_urls, temp_dir)
                        if not photos:
                            raise ContentUnavailableError("تعذر تنزيل صور المنشور.")

                        audio = await downloader.download_tiktok_audio(tt_info.music_url, temp_dir)

                        bot_info = await bot.get_me()
                        bot_username = bot_info.username or "VideoDownloaderBot"
                        clean_title = html.escape(tt_info.title)

                        chunks = chunk_media_list(photos, 10)
                        for idx, chunk in enumerate(chunks):
                            media_group = []
                            for i, p in enumerate(chunk):
                                if idx == 0 and i == 0:
                                    caption = (
                                        f"📸 <b>{clean_title}</b>\n\n"
                                        f"🌐 <b>المنصة:</b> تيك توك (TikTok)\n"
                                        f"🖼 <b>عدد الصور:</b> {len(photos)} صور\n"
                                        f"✨ <b>الجودة:</b> الدقة الأصلية (Original HD)\n\n"
                                        f"🤖 بواسطة: @{bot_username}\n"
                                        f"{DEV_CREDIT}"
                                    )
                                else:
                                    caption = None
                                media_group.append(
                                    InputMediaPhoto(
                                        media=FSInputFile(str(p)),
                                        caption=caption,
                                        parse_mode=ParseMode.HTML,
                                    )
                                )

                            try:
                                await bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
                            except Exception:
                                pass

                            await message.answer_media_group(
                                media=media_group,
                                reply_to_message_id=message.message_id if idx == 0 else None,
                            )

                        if audio and audio.exists():
                            try:
                                await bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
                            except Exception:
                                pass
                            audio_caption = f"🎵 <b>الصوت المرفق:</b> {clean_title}\n{DEV_CREDIT}"
                            await message.answer_audio(
                                audio=FSInputFile(str(audio)),
                                caption=audio_caption,
                                parse_mode=ParseMode.HTML,
                                reply_to_message_id=message.message_id,
                            )

                        # تسجيل العملية بنجاح في قاعدة البيانات لإحصائيات دقيقة 100%
                        if message.from_user:
                            total_size = sum(p.stat().st_size for p in photos if p.exists())
                            database.record_download(
                                user_id=message.from_user.id,
                                platform="تيك توك (TikTok)",
                                url=clean_url,
                                title=tt_info.title,
                                file_size_bytes=total_size,
                            )

                        await safe_delete_message(status_msg)
                        return

                    finally:
                        shutil.rmtree(temp_dir, ignore_errors=True)

        except Exception as e_info:
            logger.warning(f"تعذر استخراج بيانات تيك توك المتقدمة ({e_info})، جاري المتابعة بالمسار العام...")

    download_result: Optional[DownloadResult] = None

    try:
        # 3. تحديث الحالة: جاري التنزيل بأعلى جودة مع الصلاة على النبي
        await safe_edit_message(
            status_msg,
            "📥 <b>جاري تحميل الفيديو بأعلى جودة متوفرة...</b>\n<i>صلِّ على النبي محمد ﷺ، ثوانٍ ويكون المقطع جاهزاً...</i>"
        )

        # استدعاء محرك التنزيل غير المتزامن
        download_result = await downloader.download_video(clean_url)

        # 3. تحديث الحالة: جاري الرفع إلى تيليجرام
        await safe_edit_message(
            status_msg,
            "🚀 <b>اكتمل التحميل! جاري الرفع إلى تيليجرام...</b>\n<i>اللهم صلِّ وسلّم على نبينا محمد ﷺ</i>"
        )

        # تجهيز تفاصيل الفيديو والعنوان التوضيحي
        clean_title = html.escape(download_result.title)
        bot_info = await bot.get_me()
        bot_username = bot_info.username or "VideoDownloaderBot"

        display_title = clean_title.strip() if clean_title and clean_title.strip() and clean_title != "فيديو بدون عنوان" else "محتوى الوسائط"
        caption = (
            f"🎬 <b>{display_title}</b>\n\n"
            f"🌐 <b>المنصة:</b> {download_result.platform}\n"
            f"⏱ <b>المدة:</b> {format_duration(download_result.duration)}\n"
            f"📦 <b>الحجم:</b> {download_result.file_size_mb:.1f} MB\n"
            f"✨ <b>الجودة:</b> الدقة الأصلية (Original HD)\n\n"
            f"🤖 بواسطة: @{bot_username}\n"
            f"{DEV_CREDIT}"
        )

        video_file = FSInputFile(str(download_result.video_path))
        thumb_file = (
            FSInputFile(str(download_result.thumbnail_path))
            if download_result.thumbnail_path and download_result.thumbnail_path.exists()
            else None
        )

        # إرسال إشعار الرفع للمستخدم لضمان تجربة حية وتفاعلية
        try:
            await bot.send_chat_action(chat_id=message.chat.id, action="upload_video")
        except Exception:
            pass

        safe_duration = int(round(float(download_result.duration))) if download_result.duration is not None else None
        safe_width = int(round(float(download_result.width))) if download_result.width is not None else None
        safe_height = int(round(float(download_result.height))) if download_result.height is not None else None

        # إرسال الفيديو للمستخدم
        await message.answer_video(
            video=video_file,
            caption=caption,
            parse_mode=ParseMode.HTML,
            duration=safe_duration,
            width=safe_width,
            height=safe_height,
            thumbnail=thumb_file,
            supports_streaming=True,
            reply_to_message_id=message.message_id
        )

        # إرسال صوت الفيديو MP3 تحت الفيديو الأصلي مباشرة لجميع المنصات
        if download_result.audio_path and download_result.audio_path.exists() and download_result.audio_path.stat().st_size > 500:
            try:
                await bot.send_chat_action(chat_id=message.chat.id, action="upload_voice")
                audio_file = FSInputFile(str(download_result.audio_path))
                display_audio_title = clean_title.strip() if clean_title and clean_title.strip() and clean_title != "فيديو بدون عنوان" else "صوت المقطع"
                audio_caption = (
                    f"🎵 <b>صوت المقطع (MP3):</b> {display_audio_title}\n\n"
                    f"🌐 <b>المنصة:</b> {download_result.platform}\n"
                    f"🤖 بواسطة: @{bot_username}\n"
                    f"{DEV_CREDIT}"
                )
                await message.answer_audio(
                    audio=audio_file,
                    title=display_audio_title,
                    performer=download_result.platform,
                    caption=audio_caption,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=message.message_id
                )
            except Exception as e_audio:
                logger.warning(f"تعذر إرسال الصوت MP3 للمقطع: {e_audio}")

        # تسجيل عملية التنزيل بنجاح في قاعدة البيانات لإحصائيات حقيقية 100%
        if message.from_user:
            database.record_download(
                user_id=message.from_user.id,
                platform=download_result.platform,
                url=clean_url,
                title=download_result.title,
                file_size_bytes=download_result.file_size_bytes
            )

        # حذف رسالة الحالة فور نجاح الإرسال لتبقى المحادثة نظيفة
        await safe_delete_message(status_msg)

    except VideoTooLargeError as e:
        logger.exception(f"خطأ حجم الفيديو ({clean_url}) (Full Traceback): {e}")
        error_text = (
            "⚠️ <b>عذراً، حجم الفيديو كبير جداً!</b>\n\n"
            f"حجم المقطع يقارب <b>{e.size_mb:.1f} ميجابايت</b>، "
            "وهو يتجاوز الحد الأقصى المسموح به للبوتات في تيليجرام (50MB).\n\n"
            "💡 <i>نصيحة: يمكنك تجربة مقطع أقصر أو تحميله عبر متصفحك مباشرة.</i>\n\n"
            f"───────────────\n"
            f"<b>{DEV_CREDIT}</b>"
        )
        await safe_edit_message(status_msg, error_text)

    except ContentUnavailableError as e:
        logger.exception(f"المحتوى غير متاح للرابط ({clean_url}) (Full Traceback): {e}")
        cleaned_msg = html.escape(clean_error_message(str(e)))
        error_text = (
            f"🔒 <b>المحتوى غير متاح:</b>\n\n"
            f"{cleaned_msg}\n\n"
            "💡 <i>تأكد من أن المقطع عام ويحتوي على فيديو صالح وليس مجرد نص أو صورة.</i>\n\n"
            f"───────────────\n"
            f"<b>{DEV_CREDIT}</b>"
        )
        await safe_edit_message(status_msg, error_text)

    except InvalidURLError as e:
        logger.exception(f"رابط غير صالح ({clean_url}) (Full Traceback): {e}")
        cleaned_msg = html.escape(clean_error_message(str(e)))
        error_text = (
            f"⚠️ <b>رابط غير صالح:</b>\n\n"
            f"{cleaned_msg}\n\n"
            "يرجى التأكد من إرسال رابط فيديو صحيح.\n\n"
            f"───────────────\n"
            f"<b>{DEV_CREDIT}</b>"
        )
        await safe_edit_message(status_msg, error_text)

    except DownloaderError as e:
        logger.exception(f"خطأ تنزيل للمقطع ({clean_url}) (Full Traceback): {e}")
        cleaned_msg = html.escape(clean_error_message(str(e)))
        error_text = (
            "❌ <b>تعذر تنزيل هذا المقطع:</b>\n\n"
            f"{cleaned_msg}\n\n"
            "يرجى التحقق من الرابط والمحاولة لاحقاً.\n\n"
            f"───────────────\n"
            f"<b>{DEV_CREDIT}</b>"
        )
        await safe_edit_message(status_msg, error_text)

    except Exception as e:
        logger.exception(f"خطأ غير متوقع أثناء معالجة الرابط ({clean_url}) (Full Traceback): {e}")
        error_text = (
            "❌ <b>حدث خطأ غير متوقع أثناء المعالجة:</b>\n\n"
            "قد يكون الخادم الخاص بالموقع يواجه ضغطاً أو حدث انقطاع مؤقت.\n"
            "يرجى المحاولة مرة أخرى بعد قليل.\n\n"
            f"───────────────\n"
            f"<b>{DEV_CREDIT}</b>"
        )
        await safe_edit_message(status_msg, error_text)

    finally:
        # 4. تنظيف وحذف الملفات والمجلد المؤقت فوراً لمنع امتلاء مساحة التخزين
        if download_result:
            download_result.cleanup()


async def start_health_check_server():
    """
    تشغيل خادم ويب خفيف (Health Check Server) لدعم الاستضافة على منصات السحاب مثل Render.
    يستمع للمنفذ المحدد في متغير البيئة PORT (الافتراضي 10000 على Render) لضمان نجاح فحص الحالة (Health Check).
    """
    port_str = os.getenv("PORT")
    if not port_str:
        return None

    try:
        from aiohttp import web

        app = web.Application()

        async def health_handler(request):
            uptime_sec = time.time() - BOT_START_TIME
            accept = request.headers.get("Accept", "")
            current_token = get_bot_token()
            bot_configured = bool(current_token)

            if "text/html" in accept and request.path == "/":
                if bot_configured:
                    html_content = (
                        "<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
                        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
                        "<title>🤖 بوت تحميل الفيديوهات الذكي - متصل</title>"
                        "<style>body{background:#0f172a;color:#f8fafc;font-family:system-ui,-apple-system,sans-serif;"
                        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:1rem;box-sizing:border-box;}"
                        ".card{background:#1e293b;padding:2.5rem 2rem;border-radius:1rem;text-align:center;"
                        "box-shadow:0 10px 25px rgba(0,0,0,0.5);border:1px solid #334155;max-width:520px;width:100%;}"
                        ".badge{background:#10b981;color:#fff;padding:0.4rem 1rem;border-radius:9999px;font-size:0.875rem;font-weight:bold;display:inline-block;margin:1rem 0;}"
                        ".info{background:#0f172a;padding:1rem;border-radius:0.5rem;font-size:0.9rem;color:#94a3b8;border:1px solid #334155;margin-top:1.5rem;}"
                        "</style></head><body><div class='card'>"
                        "<h1>🎬 بوت تحميل الفيديوهات الذكي</h1>"
                        "<div><span class='badge'>🟢 متصل ويعمل بنجاح (Online)</span></div>"
                        "<p style='color:#cbd5e1;line-height:1.6;'>البوت نشط ومتصل بخوادم تيليجرام بنجاح ومستعد لتحميل الوسائط 24/7.</p>"
                        "<div class='info'>"
                        f"🚀 الاستضافة: Render Web Service<br>"
                        f"⏱️ مدة التشغيل المستمرة: {int(uptime_sec)} ثانية<br>"
                        f"🔌 المنفذ النشط: {port_str}"
                        "</div>"
                        "</div></body></html>"
                    )
                else:
                    html_content = (
                        "<!DOCTYPE html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
                        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
                        "<title>⚠️ إعداد BOT_TOKEN مطلوب | Render</title>"
                        "<style>body{background:#0f172a;color:#f8fafc;font-family:system-ui,-apple-system,sans-serif;"
                        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:1rem;box-sizing:border-box;}"
                        ".card{background:#1e293b;padding:2.5rem 2rem;border-radius:1rem;text-align:center;"
                        "box-shadow:0 10px 25px rgba(0,0,0,0.5);border:1px solid #f59e0b;max-width:550px;width:100%;}"
                        ".badge{background:#f59e0b;color:#000;padding:0.4rem 1rem;border-radius:9999px;font-size:0.875rem;font-weight:bold;display:inline-block;margin:1rem 0;}"
                        ".steps{text-align:right;background:#0f172a;padding:1.2rem;border-radius:0.5rem;margin:1.5rem 0;font-size:0.92rem;line-height:1.8;border:1px solid #334155;}"
                        "code{background:#334155;padding:0.2rem 0.4rem;border-radius:4px;color:#38bdf8;direction:ltr;display:inline-block;}"
                        "</style></head><body><div class='card'>"
                        "<h1>🎬 خدمة الويب متصلة بنجاح!</h1>"
                        "<div><span class='badge'>⚠️ خطوة أخيرة: إضافة توكن البوت في Render</span></div>"
                        "<div class='steps'>"
                        "<b>الخادم يعمل بنجاح على المنفذ، ولكن يحتاج لتوكن البوت للاتصال بتيليجرام:</b><br>"
                        "1. اذهب إلى لوحة تحكم <b>Render Dashboard</b>.<br>"
                        "2. اختر خدمتك (Web Service) ثم اضغط على <b>Environment</b>.<br>"
                        "3. أضف المتغير: <code>BOT_TOKEN</code> مع التوكن الخاص بك.<br>"
                        "4. أضف المتغير: <code>ADMIN_ID</code> مع الآيدي الخاص بك.<br>"
                        "5. اضغط <b>Save Changes</b> وسيعاد تشغيل البوت تلقائياً فوراً!"
                        "</div>"
                        f"<p style='color:#94a3b8;font-size:0.85rem;'>Uptime: {int(uptime_sec)}s | Port: {port_str}</p>"
                        "</div></body></html>"
                    )
                return web.Response(text=html_content, content_type="text/html", status=200)

            data = {
                "status": "healthy" if bot_configured else "waiting_for_token",
                "bot_configured": bot_configured,
                "service": "telegram-video-downloader-bot",
                "uptime_seconds": int(uptime_sec),
                "timestamp": datetime.now().isoformat(),
                "message": "Bot is active and running" if bot_configured else "Please add BOT_TOKEN in Render Environment tab",
            }
            return web.json_response(data, status=200)

        for path in ["/", "/health", "/healthz", "/status", "/ping"]:
            app.router.add_get(path, health_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        port = int(port_str)
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"تم تشغيل خادم فحص الحالة (Health Check Server) على المنفذ {port} بنجاح!")
        return runner
    except Exception as e:
        logger.warning(f"تعذر تشغيل خادم التحقق من الصحة على المنفذ {port_str}: {e}")
        return None


# ==========================================
# مجدول التقارير اليومية التلقائية (Daily Report Scheduler)
# ==========================================

async def daily_report_scheduler(bot: Bot):
    """
    مهمة مجدولة في الخلفية ترسل التقرير اليومي الحقيقي تلقائياً للمشرفين
    مرة واحدة يومياً عند حلول الساعة المحددة (الافتراضي: 23:00 / 11:00 مساءً بالتوقيت المحلي).
    """
    logger.info("تم تفعيل مجدول التقارير اليومية التلقائية للمشرفين.")
    while True:
        try:
            await asyncio.sleep(60)
            target_hour_str = database.get_setting("report_hour", "23")
            try:
                target_hour = int(target_hour_str)
            except (ValueError, TypeError):
                target_hour = 23

            now_local = datetime.now()
            today_str = now_local.strftime("%Y-%m-%d")
            last_sent = database.get_setting("last_daily_report_date")

            if last_sent != today_str and now_local.hour >= target_hour:
                logger.info(f"بدء إرسال التقرير اليومي التلقائي لتاريخ: {today_str}...")
                data = database.get_daily_report_data(hours=24)
                report_text = database.format_daily_report(data)
                auto_footer = (
                    f"\n\n🔔 <i>التقرير اليومي المجدول تلقائياً (الساعة {target_hour:02d}:00)</i>\n"
                    f"───────────────\n<b>{DEV_CREDIT}</b>"
                )
                full_report = report_text + auto_footer

                all_recipients = set(ADMIN_IDS + database.get_all_admins())
                if not all_recipients:
                    logger.warning("لم يتم العثور على أي مشرف لإرسال التقرير اليومي له!")

                for admin_id in all_recipients:
                    try:
                        await bot.send_message(
                            chat_id=admin_id,
                            text=full_report,
                            parse_mode=ParseMode.HTML
                        )
                        logger.info(f"تم إرسال التقرير اليومي بنجاح إلى المشرف: {admin_id}")
                    except Exception as e_send:
                        logger.error(f"تعذر إرسال التقرير اليومي للمشرف {admin_id}: {e_send}")

                database.set_setting("last_daily_report_date", today_str)
        except asyncio.CancelledError:
            logger.info("تم إيقاف مجدول التقرير اليومي.")
            break
        except Exception as e:
            logger.error(f"خطأ غير متوقع في مجدول التقرير اليومي: {e}")
            await asyncio.sleep(60)


# ==========================================
# نقطة الدخول والتشغيل الرئيسية
# ==========================================

async def main():
    """تهيئة وتشغيل البوت."""
    # 1. تهيئة قاعدة البيانات المحلية أولاً
    database.init_db()

    # 2. تشغيل خادم فحص الصحة فوراً لفتح المنفذ وتفادي إغلاق Render للخدمة
    health_runner = await start_health_check_server()

    # 3. جلب وفحص توكن البوت
    token = get_bot_token()
    if not token:
        logger.error(
            "\n" + "=" * 65 + "\n"
            "❌ [ERROR] لم يتم العثور على متغير البيئة BOT_TOKEN!\n\n"
            "📌 حل المشكلة في منصة Render:\n"
            "1. افتح لوحة التحكم https://dashboard.render.com\n"
            "2. اختر الخدمة (Web Service) الخاصة بالبوت\n"
            "3. انتقل إلى تبويب 'Environment'\n"
            "4. أضف متغير البيئة التالي:\n"
            "   Key:   BOT_TOKEN\n"
            "   Value: توكن البوت الخاص بك من @BotFather\n"
            "5. (اختياري) أضف معرف المشرف:\n"
            "   Key:   ADMIN_ID\n"
            "   Value: الآيدي الخاص بك\n"
            "6. اضغط 'Save Changes' وسيعاد تشغيل البوت تلقائياً والاتصال بتيليجرام!\n"
            "=" * 65
        )
        if health_runner:
            logger.info("تم إبقاء خادم فحص الصحة نشطاً على المنفذ بانتظار إدخال BOT_TOKEN في Render...")
            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass
            finally:
                await health_runner.cleanup()
        return

    # 4. تهيئة البوت مع الإعدادات الافتراضية
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    logger.info("جاري بدء تشغيل البوت والاتصال بخوادم تيليجرام...")

    # حذف أي رسائل تراكمت أثناء توقف البوت (Drop pending updates)
    await bot.delete_webhook(drop_pending_updates=True)

    # تشغيل مجدول التقارير اليومية التلقائية في الخلفية
    scheduler_task = asyncio.create_task(daily_report_scheduler(bot))

    try:
        bot_info = await bot.get_me()
        logger.info(f"تم تسجيل الدخول بنجاح! اسم البوت: @{bot_info.username}")
        await dp.start_polling(bot)
    except Exception as e:
        logger.critical(f"حدث خطأ أثناء تشغيل البوت: {e}", exc_info=True)
        if health_runner:
            logger.info("إبقاء خادم فحص الصحة قيد التشغيل للسماح بفحص المشكلة في لوحة التحكم...")
            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        if health_runner:
            try:
                await health_runner.cleanup()
            except Exception:
                pass
        await bot.session.close()
        logger.info("تم إيقاف تشغيل البوت وإغلاق الجلسة بأمان.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("تم إيقاف البوت يدوياً.")
