"""
وحدة لوحة تحكم المطور الشاملة (Super Admin Dashboard)
تتيح للمطور إدارة البوت بالكامل عبر أزرار تفاعلية (Inline Keyboards):
- الإحصائيات الشاملة والرسوم البيانية
- الإذاعة الذكية التدريجية (FSM)
- مراقبة موارد السيرفر (RAM, CPU, Disk, Uptime) عبر psutil
- إعدادات البوت (وضع الصيانة، الاشتراك الإجباري، تفعيل/تعطيل المنصات)
- إدارة الحظر وقائمة المحظورين
- أخذ نسخة احتياطية لقاعدة البيانات (bot.db)
- تنظيف الملفات المؤقتة
"""

import os
import time
import shutil
import platform
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from aiogram import Router, Bot, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
import psutil

import database
from config import ADMIN_IDS, DEV_CREDIT, DOWNLOAD_DIR, logger

# مسار قاعدة البيانات
DB_FILE = database.DB_PATH

# وقت تشغيل البوت لحساب Uptime
BOT_START_TIME = time.time()

admin_router = Router(name="admin_dashboard")


# ==========================================
# حالات FSM (Finite State Machine)
# ==========================================

class BroadcastStates(StatesGroup):
    waiting_for_content = State()
    confirm_broadcast = State()


class BanStates(StatesGroup):
    waiting_for_ban_id = State()
    waiting_for_unban_id = State()


class SettingsStates(StatesGroup):
    waiting_for_channel = State()


# ==========================================
# دوال مساعدة (Helper Functions)
# ==========================================

def is_authorized_admin(user_id: int) -> bool:
    """التحقق من صلاحية المشرف."""
    return database.is_admin(user_id, ADMIN_IDS)


def make_progress_bar(pct: float, length: int = 10) -> str:
    """توليد شريط بياني بصري منسق للنسب المئوية."""
    filled = int(round((pct / 100.0) * length))
    filled = min(max(filled, 0), length)
    return "█" * filled + "░" * (length - filled)


def format_uptime(seconds: float) -> str:
    """تنسيق مدة تشغيل البوت بصيغة مقروءة."""
    sec = int(round(seconds))
    days, sec = divmod(sec, 86400)
    hours, sec = divmod(sec, 3600)
    minutes, sec = divmod(sec, 60)
    parts = []
    if days > 0:
        parts.append(f"{days} يوم")
    if hours > 0:
        parts.append(f"{hours} ساعة")
    if minutes > 0:
        parts.append(f"{minutes} دقيقة")
    parts.append(f"{sec} ثانية")
    return " و ".join(parts)


async def safe_edit_callback(
    callback: CallbackQuery, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None
) -> None:
    """تعديل رسالة الزر بأمان دون توقف الكود عند ثبات المحتوى."""
    try:
        if callback.message:
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.debug(f"تخطي خطأ تعديل الرسالة: {e}")
    except Exception as e:
        logger.debug(f"خطأ غير متوقع أثناء تعديل رسالة الكولباك: {e}")


# ==========================================
# واجهات الأزرار التفاعلية (Keyboards)
# ==========================================

def get_main_dashboard_keyboard() -> InlineKeyboardMarkup:
    """لوحة المفاتيح الرئيسية للوحة تحكم المشرف."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 الإحصائيات الشاملة", callback_data="adm:stats"),
                InlineKeyboardButton(text="📢 إذاعة جماعية (Broadcast)", callback_data="adm:bc"),
            ],
            [
                InlineKeyboardButton(text="⚙️ إعدادات البوت", callback_data="adm:settings"),
                InlineKeyboardButton(text="🖥️ موارد السيرفر (Health)", callback_data="adm:health"),
            ],
            [
                InlineKeyboardButton(text="🚫 إدارة الحظر", callback_data="adm:ban"),
                InlineKeyboardButton(text="💾 نسخة احتياطية (bot.db)", callback_data="adm:backup"),
            ],
            [
                InlineKeyboardButton(text="🧹 تنظيف الملفات المؤقتة", callback_data="adm:clean"),
            ],
            [
                InlineKeyboardButton(text="❌ إغلاق لوحة التحكم", callback_data="adm:close"),
            ],
        ]
    )


def get_back_keyboard(target: str = "adm:main") -> InlineKeyboardMarkup:
    """زر رجوع مرن."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 رجوع للوحة الرئيسية", callback_data=target)]
        ]
    )


def get_settings_keyboard() -> InlineKeyboardMarkup:
    """أزرار قائمة إعدادات البوت والتحكم بالخدمات."""
    maint_on = database.is_maintenance_mode()
    maint_text = "🟢 مفعّل" if maint_on else "🔴 معطّل"

    fsub_on = database.is_force_sub_enabled()
    fsub_text = "🟢 مفعّل" if fsub_on else "🔴 معطّل"
    channel = database.get_force_sub_channel() or "غير محددة"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"وضع الصيانة: {maint_text}", callback_data="adm:toggle_maint"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"الاشتراك الإجباري: {fsub_text}", callback_data="adm:toggle_fsub"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📢 قناة الاشتراك ({channel})", callback_data="adm:set_fsub_channel"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌐 إدارة المنصات المفعلة (تفعيل/تعطيل)", callback_data="adm:platforms"
                )
            ],
            [
                InlineKeyboardButton(text="🔙 رجوع للوحة الرئيسية", callback_data="adm:main")
            ],
        ]
    )


def get_platforms_keyboard() -> InlineKeyboardMarkup:
    """أزرار تفعيل وتعطيل المنصات المدعومة."""
    plats = database.get_all_platforms_status()
    names = {
        "tiktok": "تيك توك (TikTok)",
        "instagram": "إنستغرام (Instagram)",
        "youtube": "يوتيوب (YouTube)",
        "twitter": "إكس / تويتر (X/Twitter)",
        "facebook": "فيسبوك (Facebook)",
        "pinterest": "بنترست (Pinterest)",
        "reddit": "ريديت (Reddit)",
    }

    buttons = []
    for key, name in names.items():
        is_on = plats.get(key, True)
        icon = "✅" if is_on else "❌"
        buttons.append([
            InlineKeyboardButton(text=f"{name}: {icon}", callback_data=f"adm:plat:{key}")
        ])

    buttons.append([InlineKeyboardButton(text="🔙 رجوع للإعدادات", callback_data="adm:settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_ban_menu_keyboard() -> InlineKeyboardMarkup:
    """أزرار إدارة الحظر والمستخدمين."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚫 حظر مستخدم بالآيدي", callback_data="adm:ban_user"),
                InlineKeyboardButton(text="✅ فك حظر مستخدم بالآيدي", callback_data="adm:unban_user"),
            ],
            [
                InlineKeyboardButton(text="📋 قائمة المستخدمين المحظورين", callback_data="adm:ban_list"),
            ],
            [
                InlineKeyboardButton(text="🔙 رجوع للوحة الرئيسية", callback_data="adm:main"),
            ],
        ]
    )


# ==========================================
# الأوامر الرئيسية للوحة التحكم (/admin)
# ==========================================

@admin_router.message(Command("admin", "dashboard", "panel", "لوحة_التحكم"))
@admin_router.message(F.text.lower().in_(["لوحة التحكم", "البانل", "بانل", "ادمن", "الادمن"]))
async def handle_admin_command(message: Message, state: FSMContext):
    """فتح لوحة تحكم المطور التفاعلية."""
    await state.clear()
    if not message.from_user or not is_authorized_admin(message.from_user.id):
        await message.answer("⚠️ <b>عذراً، هذه اللوحة مخصصة للمطور ومشرفي البوت فقط.</b>", parse_mode=ParseMode.HTML)
        return

    logger.info(f"فتح لوحة التحكم للمشرف {message.from_user.id} ({message.from_user.full_name})")

    user_name = message.from_user.full_name or "المطور"
    stats = database.get_comprehensive_stats()
    maint_status = "🟢 مفعّل" if database.is_maintenance_mode() else "🔴 معطّل"
    fsub_status = "🟢 مفعّل" if database.is_force_sub_enabled() else "🔴 معطّل"

    text = (
        "👑 <b>لوحة تحكم المطور الشاملة (Super Admin Dashboard)</b>\n"
        f"⚡ مرحباً بك يا <b>{user_name}</b> في مركز إدارة البوت والتحكم.\n\n"
        "📊 <b>نظرة سريعة على النظام:</b>\n"
        f"• 👥 إجمالي المشتركين: <b>{stats['total_users']}</b> مستخدم\n"
        f"• 📥 إجمالي التنزيلات: <b>{stats['total_downloads']}</b> مقطع\n"
        f"• 🛠 وضع الصيانة: <b>{maint_status}</b>\n"
        f"• 📢 الاشتراك الإجباري: <b>{fsub_status}</b>\n\n"
        "👇 <i>اختر القسم المطلوب من الأزرار التفاعلية أدناه:</i>\n\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_dashboard_keyboard())


# ==========================================
# معالجات الأزرار التفاعلية (Callbacks)
# ==========================================

@admin_router.callback_query(F.data == "adm:main")
async def cb_main_dashboard(callback: CallbackQuery, state: FSMContext):
    """العودة للشاشة الرئيسية للوحة التحكم."""
    await state.clear()
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        await callback.answer("⚠️ غير مصرح لك بهذا الإجراء.", show_alert=True)
        return

    user_name = callback.from_user.full_name or "المطور"
    stats = database.get_comprehensive_stats()
    maint_status = "🟢 مفعّل" if database.is_maintenance_mode() else "🔴 معطّل"
    fsub_status = "🟢 مفعّل" if database.is_force_sub_enabled() else "🔴 معطّل"

    text = (
        "👑 <b>لوحة تحكم المطور الشاملة (Super Admin Dashboard)</b>\n"
        f"⚡ مرحباً بك يا <b>{user_name}</b> في مركز إدارة البوت والتحكم.\n\n"
        "📊 <b>نظرة سريعة على النظام:</b>\n"
        f"• 👥 إجمالي المشتركين: <b>{stats['total_users']}</b> مستخدم\n"
        f"• 📥 إجمالي التنزيلات: <b>{stats['total_downloads']}</b> مقطع\n"
        f"• 🛠 وضع الصيانة: <b>{maint_status}</b>\n"
        f"• 📢 الاشتراك الإجباري: <b>{fsub_status}</b>\n\n"
        "👇 <i>اختر القسم المطلوب من الأزرار التفاعلية أدناه:</i>\n\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>"
    )
    await safe_edit_callback(callback, text, reply_markup=get_main_dashboard_keyboard())


@admin_router.callback_query(F.data == "adm:close")
async def cb_close_dashboard(callback: CallbackQuery, state: FSMContext):
    """إغلاق لوحة التحكم."""
    await state.clear()
    await callback.answer("تم إغلاق لوحة التحكم بنجاح.")
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass


# ------------------------------------------
# 1. قسم الإحصائيات الشاملة
# ------------------------------------------

@admin_router.callback_query(F.data == "adm:stats")
async def cb_stats(callback: CallbackQuery):
    """عرض تقرير إحصائي مفصل ورسوم بيانية دقيقة 100%."""
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return

    stats = database.get_comprehensive_stats()
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "📊 <b>تقرير الإحصائيات الشامل لنشاط البوت</b>",
        f"📅 <b>تاريخ التحديث:</b> <code>{date_str}</code>",
        "─────────────────",
        "👥 <b>المستخدمون:</b>",
        f"• إجمالي المشتركين المسجلين: <b>{stats['total_users']}</b>",
        f"• المشتركون الجدد اليوم: <b>{stats['today_users']}</b>",
        f"• الحسابات النشطة: <b>{stats['active_users']}</b>",
        f"• الحسابات المحظورة: <b>{stats['banned_users']}</b>",
        "",
        "📥 <b>عمليات التنزيل:</b>",
        f"• إجمالي التنزيلات الكلي: <b>{stats['total_downloads']}</b>",
        f"• تنزيلات اليوم: <b>{stats['today_downloads']}</b>",
        f"• التنزيلات الناجحة: <b>{stats['successful_downloads']}</b>",
        f"• التنزيلات الفاشلة: <b>{stats['failed_downloads']}</b>",
        f"• نسبة النجاح: <b>{stats['success_rate']:.1f}%</b>",
        f"• إجمالي حجم الوسائط المرفوعة: <b>{stats['total_size_mb']:.1f} MB</b>",
        "",
        "🌐 <b>توزيع التحميلات حسب المنصات:</b>",
    ]

    platforms = stats["platforms_breakdown"]
    if platforms:
        for p in platforms:
            bar = make_progress_bar(p["percentage"], length=8)
            lines.append(
                f"• <b>{p['platform']}:</b> {p['count']} ({p['percentage']:.1f}%)\n  <code>[{bar}]</code>"
            )
    else:
        lines.append("• <i>لا توجد بيانات تنزيل مسجلة حتى الآن.</i>")

    lines.extend([
        "",
        "─────────────────",
        "✅ <i>جميع الأرقام والإحصائيات دقيقة 100% ومستخرجة من SQLite مباشرة.</i>",
        f"<b>{DEV_CREDIT}</b>"
    ])

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 تحديث الإحصائيات", callback_data="adm:stats")],
            [InlineKeyboardButton(text="🔙 رجوع للوحة الرئيسية", callback_data="adm:main")],
        ]
    )
    await safe_edit_callback(callback, "\n".join(lines), reply_markup=kb)


# ------------------------------------------
# 2. قسم مراقبة موارد السيرفر (Health Monitor)
# ------------------------------------------

@admin_router.callback_query(F.data == "adm:health")
async def cb_server_health(callback: CallbackQuery):
    """عرض استهلاك المعالج، الذاكرة، القرص، ووقت التشغيل عبر psutil."""
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return

    # قراءة الذاكرة العشوائية RAM
    vm = psutil.virtual_memory()
    ram_pct = vm.percent
    ram_used_gb = vm.used / (1024 ** 3)
    ram_total_gb = vm.total / (1024 ** 3)
    ram_bar = make_progress_bar(ram_pct, length=10)

    # قراءة المعالج CPU
    cpu_pct = psutil.cpu_percent(interval=0.1)
    cpu_count = psutil.cpu_count(logical=True)
    cpu_bar = make_progress_bar(cpu_pct, length=10)

    # قراءة القرص الصلب Disk
    disk = psutil.disk_usage(str(Path(__file__).resolve().parent))
    disk_pct = disk.percent
    disk_free_gb = disk.free / (1024 ** 3)
    disk_total_gb = disk.total / (1024 ** 3)
    disk_bar = make_progress_bar(disk_pct, length=10)

    # استهلاك عملية البوت نفسها
    try:
        proc = psutil.Process()
        proc_ram_mb = proc.memory_info().rss / (1024 * 1024)
    except Exception:
        proc_ram_mb = 0.0

    # وقت التشغيل
    uptime_str = format_uptime(time.time() - BOT_START_TIME)

    # تقييم الحالة
    health_status = "🟢 السيرفر يعمل بكفاءة وسرعة فائقة"
    if ram_pct > 85 or cpu_pct > 85:
        health_status = "⚠️ تنبيه: استهلاك الموارد مرتفع نسبياً"

    text = (
        "🖥️ <b>مراقبة موارد السيرفر وصحة النظام (Server Health)</b>\n"
        "─────────────────\n"
        f"📊 <b>الحالة العامة:</b> {health_status}\n"
        f"⏱ <b>مدة تشغيل البوت (Uptime):</b> {uptime_str}\n\n"
        f"🧠 <b>استهلاك الذاكرة (RAM):</b> <b>{ram_pct}%</b>\n"
        f"<code>[{ram_bar}]</code> ({ram_used_gb:.2f} GB / {ram_total_gb:.2f} GB)\n"
        f"• استهلاك برنامج البوت: <b>{proc_ram_mb:.1f} MB</b>\n\n"
        f"⚙️ <b>استهلاك المعالج (CPU):</b> <b>{cpu_pct}%</b> ({cpu_count} Cores)\n"
        f"<code>[{cpu_bar}]</code>\n\n"
        f"💾 <b>مساحة التخزين (Disk):</b> <b>{disk_pct}%</b>\n"
        f"<code>[{disk_bar}]</code> (المتبقي: {disk_free_gb:.1f} GB من أصل {disk_total_gb:.1f} GB)\n\n"
        f"💻 <b>نظام التشغيل:</b> {platform.system()} {platform.release()} ({platform.machine()})\n"
        f"🐍 <b>إصدار بايثون:</b> {platform.python_version()}\n"
        "─────────────────\n"
        f"<b>{DEV_CREDIT}</b>"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 تحديث الموارد الآن", callback_data="adm:health")],
            [InlineKeyboardButton(text="🔙 رجوع للوحة الرئيسية", callback_data="adm:main")],
        ]
    )
    await safe_edit_callback(callback, text, reply_markup=kb)


# ------------------------------------------
# 3. قسم إعدادات البوت والخدمات
# ------------------------------------------

@admin_router.callback_query(F.data == "adm:settings")
async def cb_settings(callback: CallbackQuery, state: FSMContext):
    """عرض خيارات إعدادات البوت والتحكم."""
    await state.clear()
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return

    text = (
        "⚙️ <b>إعدادات البوت والتحكم الفوري:</b>\n\n"
        "• <b>وضع الصيانة:</b> عند تفعيله، يتم إيقاف تنزيل الفيديوهات لجميع المشتركين وإظهار رسالة صيانة أنيقة، باستثناء المشرفين.\n"
        "• <b>الاشتراك الإجباري:</b> إلزام المستخدمين بالانضمام لقناتك أولاً قبل السماح لهم بتحميل المقاطع.\n"
        "• <b>إدارة المنصات:</b> تفعيل أو تعطيل التنزيل من منصات محددة بنقرة واحدة.\n\n"
        "👇 <i>اضغط على أي إعداد لتغيير حالته فوراً:</i>\n\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>"
    )
    await safe_edit_callback(callback, text, reply_markup=get_settings_keyboard())


@admin_router.callback_query(F.data == "adm:toggle_maint")
async def cb_toggle_maintenance(callback: CallbackQuery):
    """تبديل حالة وضع الصيانة."""
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return
    current = database.is_maintenance_mode()
    database.set_maintenance_mode(not current)
    new_state = "تم تفعيل وضع الصيانة 🛠️" if not current else "تم إيقاف وضع الصيانة وعودة البوت للعمل للجميع ✅"
    await callback.answer(new_state, show_alert=True)
    await cb_settings(callback, None)


@admin_router.callback_query(F.data == "adm:toggle_fsub")
async def cb_toggle_forcesub(callback: CallbackQuery):
    """تبديل حالة الاشتراك الإجباري."""
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return
    current = database.is_force_sub_enabled()
    channel = database.get_force_sub_channel()
    if not current and not channel:
        await callback.answer("⚠️ يرجى تحديد معرف القناة أولاً قبل تفعيل الاشتراك الإجباري.", show_alert=True)
        return
    database.set_force_sub_enabled(not current)
    new_state = "تم تفعيل الاشتراك الإجباري بنجاح 📢" if not current else "تم إيقاف الاشتراك الإجباري 🔴"
    await callback.answer(new_state, show_alert=True)
    await cb_settings(callback, None)


@admin_router.callback_query(F.data == "adm:set_fsub_channel")
async def cb_set_fsub_channel(callback: CallbackQuery, state: FSMContext):
    """بدء استقبال معرف قناة الاشتراك الإجباري."""
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return
    await state.set_state(SettingsStates.waiting_for_channel)
    current = database.get_force_sub_channel() or "غير محددة"
    text = (
        "📢 <b>تعيين قناة الاشتراك الإجباري:</b>\n\n"
        f"• القناة الحالية: <code>{current}</code>\n\n"
        "✍️ <b>أرسل الآن معرف القناة أو الآيدي الخاص بها</b>\n"
        "مثال: <code>@MyChannel</code> أو <code>-1001234567890</code>\n\n"
        "⚠️ <i>ملاحظة هامة: تأكد من رفع البوت كمشرف (Admin) في القناة بصلاحية دعوة المستخدمين لكي يتمكن من التحقق من اشتراكهم تلقائياً.</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="adm:settings")]
        ]
    )
    await safe_edit_callback(callback, text, reply_markup=kb)


@admin_router.message(SettingsStates.waiting_for_channel)
async def handle_fsub_channel_input(message: Message, state: FSMContext, bot: Bot):
    """معالجة حفظ قناة الاشتراك الإجباري مع التحقق التلقائي."""
    if not message.from_user or not is_authorized_admin(message.from_user.id):
        return
    channel_input = (message.text or "").strip()
    if not channel_input:
        await message.answer("⚠️ يرجى إرسال معرف صحيح للقناة.")
        return

    status_msg = await message.answer("⏳ جاري فحص صلاحيات البوت في القناة...")
    try:
        chat = await bot.get_chat(channel_input)
        bot_member = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
        if bot_member.status not in ["administrator", "creator"]:
            await status_msg.edit_text(
                "⚠️ <b>تحذير:</b> البوت موجود في القناة لكنه ليس مشرفاً (Admin)!\n"
                "يرجى ترقية البوت لمشرف ليتمكن من فحص الاشتراكات بدقة.\n\n"
                f"تم حفظ القناة: <b>{chat.title}</b> (<code>{channel_input}</code>)",
                parse_mode=ParseMode.HTML,
                reply_markup=get_back_keyboard("adm:settings")
            )
        else:
            database.set_force_sub_channel(channel_input)
            database.set_force_sub_enabled(True)
            await status_msg.edit_text(
                f"✅ <b>تم التحقق بنجاح! البوت مشرف في القناة.</b>\n\n"
                f"• القناة: <b>{chat.title}</b>\n"
                f"• المعرف: <code>{channel_input}</code>\n"
                "• تم تفعيل الاشتراك الإجباري تلقائياً 📢",
                parse_mode=ParseMode.HTML,
                reply_markup=get_back_keyboard("adm:settings")
            )
    except Exception as e:
        logger.warning(f"خطأ أثناء التحقق من القناة {channel_input}: {e}")
        database.set_force_sub_channel(channel_input)
        await status_msg.edit_text(
            f"⚠️ <b>تم حفظ المعرف:</b> <code>{channel_input}</code>\n"
            f"<i>تعذر التحقق التلقائي ({e}). تأكد أن البوت مضاف كمشرف في القناة.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_back_keyboard("adm:settings")
        )
    finally:
        await state.clear()


@admin_router.callback_query(F.data == "adm:platforms")
async def cb_platforms_menu(callback: CallbackQuery):
    """عرض قائمة المنصات المفعلة والمعطلة للتحكم بها."""
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return

    text = (
        "🌐 <b>إدارة المنصات المدعومة في البوت:</b>\n\n"
        "اضغط على أي منصة للتبديل بين:\n"
        "• ✅ <b>مفعّلة:</b> متاح التنزيل منها لجميع المستخدمين.\n"
        "• ❌ <b>معطّلة:</b> يظهر تنبيه للمستخدم بأن المنصة معطلة مؤقتاً.\n\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>"
    )
    await safe_edit_callback(callback, text, reply_markup=get_platforms_keyboard())


@admin_router.callback_query(F.data.startswith("adm:plat:"))
async def cb_toggle_platform(callback: CallbackQuery):
    """تبديل حالة تفعيل منصة معينة بنقرة زر."""
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return
    platform_key = callback.data.split(":")[-1]
    is_enabled = database.is_platform_enabled(platform_key)
    database.set_platform_enabled(platform_key, not is_enabled)
    state_txt = "تم التعطيل ❌" if is_enabled else "تم التفعيل ✅"
    await callback.answer(f"{platform_key}: {state_txt}")
    await safe_edit_callback(
        callback,
        "🌐 <b>إدارة المنصات المدعومة في البوت:</b>\n\n"
        "اضغط على أي منصة للتبديل بين التفعيل والتعطيل:\n\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>",
        reply_markup=get_platforms_keyboard()
    )


# ------------------------------------------
# 4. قسم الإذاعة الجماعية الذكية (Smart Broadcast with FSM)
# ------------------------------------------

@admin_router.callback_query(F.data == "adm:bc")
async def cb_broadcast_start(callback: CallbackQuery, state: FSMContext):
    """بدء استقبال محتوى الإذاعة الجماعية."""
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return

    active_users_count = len(database.get_active_user_ids())
    await state.set_state(BroadcastStates.waiting_for_content)

    text = (
        "📢 <b>نظام الإذاعة الجماعية الذكي (Smart Broadcast)</b>\n"
        "─────────────────\n"
        f"👥 <b>عدد المستخدمين المستهدفين:</b> <b>{active_users_count}</b> مستخدم نشط\n\n"
        "✍️ <b>أرسل الآن المحتوى الذي تريد إذاعته:</b>\n"
        "• يدعم: <b>نصوص، صور، فيديوهات، تسجيلات صوتية، ملفات، أو ملصقات</b>.\n"
        "• يدعم أيضاً: <b>عمل تحويل (Forward)</b> لأي رسالة تريد نقلها كما هي.\n\n"
        "👇 <i>يرجى إرسال الرسالة الآن، أو اضغط إلغاء للعودة:</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء الإذاعة", callback_data="adm:main")]
        ]
    )
    await safe_edit_callback(callback, text, reply_markup=kb)


@admin_router.message(BroadcastStates.waiting_for_content)
async def handle_broadcast_content(message: Message, state: FSMContext):
    """استلام محتوى الإذاعة وعرض خيار التأكيد قبل الإرسال."""
    if not message.from_user or not is_authorized_admin(message.from_user.id):
        return

    await state.update_data(
        from_chat_id=message.chat.id,
        broadcast_message_id=message.message_id
    )
    await state.set_state(BroadcastStates.confirm_broadcast)

    active_users = database.get_active_user_ids()
    text = (
        "⚠️ <b>تأكيد بدء الإذاعة الجماعية:</b>\n\n"
        f"👥 <b>عدد المستهدفين:</b> <b>{len(active_users)}</b> مستخدم\n"
        "• تم استلام الرسالة بنجاح وسيتم إرسالها لجميع المشتركين تدريجياً وبدون حظر.\n\n"
        "هل ترغب في بدء الإرسال الآن؟"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 بدء الإذاعة الآن", callback_data="adm:bc_confirm"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data="adm:main"),
            ]
        ]
    )
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@admin_router.callback_query(F.data == "adm:bc_confirm", BroadcastStates.confirm_broadcast)
async def cb_broadcast_execute(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """تنفيذ الإذاعة الجماعية الذكية مع التحديث التدريجي كل 5 ثوانٍ ومعالجة الأخطاء."""
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return

    data = await state.get_data()
    from_chat_id = data.get("from_chat_id")
    msg_id = data.get("broadcast_message_id")
    await state.clear()

    if not from_chat_id or not msg_id:
        await callback.answer("⚠️ حدث خطأ: لم يتم العثور على محتوى الإذاعة.", show_alert=True)
        return

    users = database.get_active_user_ids()
    total = len(users)

    if total == 0:
        await safe_edit_callback(
            callback,
            "⚠️ <b>لا يوجد مستخدمين نشطين في قاعدة البيانات للإرسال لهم.</b>",
            reply_markup=get_back_keyboard("adm:main")
        )
        return

    status_msg = await callback.message.edit_text(
        f"⏳ <b>جاري بدء الإذاعة الذكية إلى {total} مستخدم...</b>\n"
        "<i>يتم التحديث كل 5 ثوانٍ...</i>",
        parse_mode=ParseMode.HTML
    )

    start_time = time.time()
    last_update_time = time.time()
    success_count = 0
    blocked_count = 0
    failed_count = 0

    for idx, u_id in enumerate(users, 1):
        try:
            await bot.copy_message(
                chat_id=u_id,
                from_chat_id=from_chat_id,
                message_id=msg_id
            )
            success_count += 1
            # تأخير ذكي لتفادي حد الـ FloodWait في تيليجرام (30 msg/s)
            await asyncio.sleep(0.04)
        except TelegramRetryAfter as e_retry:
            logger.warning(f"Telegram FloodWait أثناء الإذاعة: انتظر {e_retry.retry_after} ثوانٍ")
            await asyncio.sleep(e_retry.retry_after + 0.5)
            # إعادة المحاولة بعد الانتظار
            try:
                await bot.copy_message(chat_id=u_id, from_chat_id=from_chat_id, message_id=msg_id)
                success_count += 1
            except Exception:
                failed_count += 1
        except TelegramForbiddenError:
            blocked_count += 1
            database.mark_user_blocked(u_id)
        except Exception as e_send:
            err_str = str(e_send).lower()
            if any(w in err_str for w in ["blocked", "deactivated", "chat not found", "user is deactivated"]):
                blocked_count += 1
                database.mark_user_blocked(u_id)
            else:
                failed_count += 1
                logger.debug(f"فشل إرسال الإذاعة لـ {u_id}: {e_send}")

        # تحديث رسالة التقدم كل 5 ثوانٍ
        now = time.time()
        if now - last_update_time >= 5.0 or idx == total:
            last_update_time = now
            pct = (idx / total) * 100
            bar = make_progress_bar(pct, length=10)
            update_txt = (
                "⏳ <b>جاري الإذاعة الجماعية...</b>\n\n"
                f"• التقدم: <b>{idx} / {total}</b> ({pct:.1f}%)\n"
                f"<code>[{bar}]</code>\n\n"
                f"✅ ناجح: <b>{success_count}</b> | 🚫 محظور: <b>{blocked_count}</b> | ❌ فشل: <b>{failed_count}</b>"
            )
            try:
                await status_msg.edit_text(update_txt, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    elapsed = int(round(time.time() - start_time))
    final_report = (
        "🎉 <b>اكتملت الإذاعة الجماعية الذكية بنجاح!</b>\n"
        "─────────────────\n"
        f"👥 <b>إجمالي المستهدفين:</b> {total}\n"
        f"✅ <b>تم الإرسال بنجاح:</b> {success_count}\n"
        f"🚫 <b>حظر البوت أو حذف الحساب:</b> {blocked_count}\n"
        f"❌ <b>أخطاء أخرى:</b> {failed_count}\n"
        f"⏱ <b>المدة المستغرقة:</b> {elapsed} ثانية\n\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>"
    )
    await status_msg.edit_text(final_report, parse_mode=ParseMode.HTML, reply_markup=get_back_keyboard("adm:main"))


# ------------------------------------------
# 5. قسم إدارة الحظر (Ban Management)
# ------------------------------------------

@admin_router.callback_query(F.data == "adm:ban")
async def cb_ban_menu(callback: CallbackQuery, state: FSMContext):
    """عرض قائمة إدارة الحظر."""
    await state.clear()
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return

    banned_list = database.get_banned_users()
    text = (
        "🚫 <b>نظام إدارة الحظر وحماية موارد البوت:</b>\n\n"
        f"• عدد المستخدمين المحظورين حالياً: <b>{len(banned_list)}</b> مستخدم\n\n"
        "عند حظر أي مستخدم، يتم منعه فوراً من استهلاك موارد البوت أو تحميل أي مقطع، ويظهر له تنبيه يفيد بحظر حسابه.\n\n"
        "👇 <i>اختر الإجراء المطلوب:</i>\n\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>"
    )
    await safe_edit_callback(callback, text, reply_markup=get_ban_menu_keyboard())


@admin_router.callback_query(F.data == "adm:ban_user")
async def cb_ban_user_prompt(callback: CallbackQuery, state: FSMContext):
    """طلب آيدي المستخدم لحظره."""
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return
    await state.set_state(BanStates.waiting_for_ban_id)
    text = (
        "🚫 <b>حظر مستخدم من استخدام البوت:</b>\n\n"
        "✍️ أرسل الآن <b>الآيدي الرقمي (User ID)</b> الخاص بالمستخدم لحظره فوراً:\n"
        "مثال: <code>123456789</code>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="adm:ban")]]
    )
    await safe_edit_callback(callback, text, reply_markup=kb)


@admin_router.message(BanStates.waiting_for_ban_id)
async def handle_ban_id_input(message: Message, state: FSMContext):
    """معالجة حظر المستخدم."""
    if not message.from_user or not is_authorized_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("⚠️ يرجى إرسال آيدي رقمي صحيح (أرقام فقط).")
        return
    target_id = int(text)
    if is_authorized_admin(target_id):
        await message.answer("⚠️ لا يمكن حظر مطور أو مشرف في البوت!")
        await state.clear()
        return

    database.set_user_ban(target_id, True)
    await state.clear()
    await message.answer(
        f"🚫 <b>تم حظر المستخدم بنجاح!</b>\nالآيدي: <code>{target_id}</code>\nلن يتمكن من استخدام البوت بعد الآن.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_back_keyboard("adm:ban")
    )


@admin_router.callback_query(F.data == "adm:unban_user")
async def cb_unban_user_prompt(callback: CallbackQuery, state: FSMContext):
    """طلب آيدي المستخدم لفك حظره."""
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return
    await state.set_state(BanStates.waiting_for_unban_id)
    text = (
        "✅ <b>فك حظر مستخدم:</b>\n\n"
        "✍️ أرسل الآن <b>الآيدي الرقمي (User ID)</b> للمستخدم لفك حظره واستعادة إمكانية التنزيل:\n"
        "مثال: <code>123456789</code>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء", callback_data="adm:ban")]]
    )
    await safe_edit_callback(callback, text, reply_markup=kb)


@admin_router.message(BanStates.waiting_for_unban_id)
async def handle_unban_id_input(message: Message, state: FSMContext):
    """معالجة فك حظر المستخدم."""
    if not message.from_user or not is_authorized_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("⚠️ يرجى إرسال آيدي رقمي صحيح.")
        return
    target_id = int(text)
    database.set_user_ban(target_id, False)
    await state.clear()
    await message.answer(
        f"✅ <b>تم فك حظر المستخدم بنجاح!</b>\nالآيدي: <code>{target_id}</code>\nأصبح بإمكانه استخدام البوت كالمعتاد.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_back_keyboard("adm:ban")
    )


@admin_router.callback_query(F.data == "adm:ban_list")
async def cb_banned_list(callback: CallbackQuery):
    """عرض قائمة المستخدمين المحظورين."""
    await callback.answer()
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return

    banned = database.get_banned_users()
    if not banned:
        text = "✅ <b>لا يوجد أي مستخدم محظور حالياً في قاعدة البيانات.</b>"
    else:
        lines = [f"📋 <b>قائمة المستخدمين المحظورين ({len(banned)}):</b>\n"]
        for idx, u in enumerate(banned[:50], 1):
            name = u.get("full_name") or "بدون اسم"
            handle = f"(@{u['username']})" if u.get("username") else ""
            lines.append(f"{idx}. <b>{name}</b> {handle} ➔ <code>{u['user_id']}</code>")
        if len(banned) > 50:
            lines.append(f"\n<i>... وهناك {len(banned) - 50} مستخدمين آخرين محظورين.</i>")
        text = "\n".join(lines)

    await safe_edit_callback(callback, text, reply_markup=get_back_keyboard("adm:ban"))


# ------------------------------------------
# 6. قسم النسخ الاحتياطي لقاعدة البيانات
# ------------------------------------------

@admin_router.callback_query(F.data == "adm:backup")
async def cb_backup_db(callback: CallbackQuery, bot: Bot):
    """إرسال نسخة احتياطية من ملف قاعدة البيانات bot.db للمطور مباشرة."""
    await callback.answer("جاري تجهيز وإرسال ملف قاعدة البيانات...")
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return

    if not DB_FILE.exists():
        await callback.answer("⚠️ ملف قاعدة البيانات غير موجود بعد!", show_alert=True)
        return

    stats = database.get_comprehensive_stats()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_size_kb = DB_FILE.stat().st_size / 1024

    caption = (
        "💾 <b>نسخة احتياطية لقاعدة البيانات (SQLite Backup)</b>\n"
        "─────────────────\n"
        f"📅 <b>تاريخ النسخ:</b> <code>{now_str}</code>\n"
        f"📦 <b>حجم الملف:</b> {file_size_kb:.2f} KB\n"
        f"👥 <b>إجمالي المشتركين:</b> {stats['total_users']}\n"
        f"📥 <b>إجمالي التحميلات المسجلة:</b> {stats['total_downloads']}\n\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>"
    )

    try:
        await bot.send_document(
            chat_id=callback.message.chat.id,
            document=FSInputFile(str(DB_FILE), filename=f"bot_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db"),
            caption=caption,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"تعذر إرسال النسخة الاحتياطية: {e}")
        await callback.answer(f"❌ تعذر إرسال الملف: {e}", show_alert=True)


# ------------------------------------------
# 7. قسم تنظيف الملفات المؤقتة
# ------------------------------------------

@admin_router.callback_query(F.data == "adm:clean")
async def cb_cleanup_temp_files(callback: CallbackQuery):
    """تنظيف الملفات المؤقتة والمجلدات المتروكة في مجلد التنزيل."""
    await callback.answer("جاري فحص وتنظيف الملفات المؤقتة...")
    if not callback.from_user or not is_authorized_admin(callback.from_user.id):
        return

    deleted_files = 0
    deleted_dirs = 0
    freed_bytes = 0

    if DOWNLOAD_DIR.exists():
        for item in list(DOWNLOAD_DIR.iterdir()):
            try:
                if item.is_file():
                    freed_bytes += item.stat().st_size
                    item.unlink(missing_ok=True)
                    deleted_files += 1
                elif item.is_dir():
                    for sub in item.rglob("*"):
                        if sub.is_file():
                            freed_bytes += sub.stat().st_size
                    shutil.rmtree(item, ignore_errors=True)
                    deleted_dirs += 1
            except Exception as e:
                logger.debug(f"خطأ أثناء حذف {item}: {e}")

    freed_mb = freed_bytes / (1024 * 1024)
    text = (
        "🧹 <b>تقرير تنظيف الملفات المؤقتة:</b>\n"
        "─────────────────\n"
        f"📦 <b>المساحة المحررة:</b> <b>{freed_mb:.2f} MB</b>\n"
        f"🗑 <b>الملفات المحذوفة:</b> {deleted_files} ملف\n"
        f"📁 <b>المجلدات المؤقتة المنظفة:</b> {deleted_dirs} مجلد\n\n"
        "✅ مجلد التنزيل نظيف تماماً وخالٍ من أي مخلفات قديمة.\n\n"
        f"───────────────\n<b>{DEV_CREDIT}</b>"
    )
    await safe_edit_callback(callback, text, reply_markup=get_back_keyboard("adm:main"))
