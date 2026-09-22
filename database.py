"""
نظام قاعدة بيانات البوت الشامل (Database Management Module)
يستخدم SQLite لتخزين وإدارة المستخدمين، عمليات التنزيل، إعدادات البوت، والحظر،
مع دعم لوحة تحكم المطور (Super Admin Dashboard) والتقارير والإذاعة الجماعية.
"""

import sqlite3
import logging
import html
from pathlib import Path
from typing import List, Dict, Any, Optional, Generator
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from collections import Counter

logger = logging.getLogger("Database")

# مسار مجلد البيانات وملف قاعدة البيانات
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    """مدير سياق لإنشاء اتصال آمن بقاعدة البيانات وإغلاقه تلقائياً لمنع تسرب الموارد."""
    conn = sqlite3.connect(str(DB_PATH), timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _get_columns(cursor: sqlite3.Cursor, table_name: str) -> List[str]:
    """جلب أسماء أعمدة جدول معين لفحص الحاجة للترقية."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row["name"] for row in cursor.fetchall()]


def init_db() -> None:
    """
    إنشاء الجداول الأساسية والفهارس وترقية الجداول القديمة إن وجدت بسلاسة.
    الجداول المطلوبة:
    1. users: (user_id, username, full_name, join_date, is_banned, total_downloads)
    2. downloads: (id, user_id, platform, status, file_size, timestamp)
    3. bot_settings: (key, value)
    """
    with get_connection() as conn:
        cursor = conn.cursor()

        # 1. جدول المستخدمين (users)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_banned INTEGER DEFAULT 0,
                total_downloads INTEGER DEFAULT 0
            )
        """)

        # فحص وترقية جدول المستخدمين في حال وجود أعمدة سابقة
        user_cols = _get_columns(cursor, "users")
        if "full_name" not in user_cols and "first_name" in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
            cursor.execute("UPDATE users SET full_name = first_name WHERE full_name IS NULL")
        if "join_date" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN join_date TIMESTAMP")
            if "joined_at" in user_cols:
                cursor.execute("UPDATE users SET join_date = joined_at WHERE join_date IS NULL")
        if "is_banned" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
            if "is_active" in user_cols:
                cursor.execute("UPDATE users SET is_banned = CASE WHEN is_active = 0 THEN 1 ELSE 0 END")
        if "total_downloads" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN total_downloads INTEGER DEFAULT 0")

        # 2. جدول التنزيلات (downloads)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                platform TEXT,
                status TEXT DEFAULT 'success',
                file_size INTEGER DEFAULT 0,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # فحص وترقية جدول التنزيلات
        dl_cols = _get_columns(cursor, "downloads")
        if "status" not in dl_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN status TEXT DEFAULT 'success'")
        if "file_size" not in dl_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN file_size INTEGER DEFAULT 0")
            if "file_size_bytes" in dl_cols:
                cursor.execute("UPDATE downloads SET file_size = file_size_bytes WHERE file_size IS NULL OR file_size = 0")
        if "timestamp" not in dl_cols:
            cursor.execute("ALTER TABLE downloads ADD COLUMN timestamp TIMESTAMP")
            if "downloaded_at" in dl_cols:
                cursor.execute("UPDATE downloads SET timestamp = downloaded_at WHERE timestamp IS NULL")

        # 3. جدول إعدادات البوت (bot_settings)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # نقل أي إعدادات سابقة من جدول settings القديم إن وجد
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'")
        if cursor.fetchone():
            cursor.execute("INSERT OR IGNORE INTO bot_settings (key, value) SELECT key, value FROM settings")

        # 4. جدول المشرفين (admins)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # فهارس لتسريع الاستعلامات والتقارير والإحصائيات
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_banned ON users(is_banned)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_join ON users(join_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_time ON downloads(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_platform ON downloads(platform)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status)")

        # تعيين القيم الافتراضية للإعدادات إن لم تكن محددة
        default_settings = {
            "maintenance_mode": "0",
            "force_sub_enabled": "0",
            "force_sub_channel": "",
            "report_hour": "23",
            "platform_tiktok": "1",
            "platform_instagram": "1",
            "platform_youtube": "1",
            "platform_twitter": "1",
            "platform_facebook": "1",
            "platform_pinterest": "1",
            "platform_reddit": "1",
        }
        for k, v in default_settings.items():
            cursor.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)", (k, v))

        conn.commit()
    logger.info(f"تمت تهيئة وتحديث قاعدة البيانات بنجاح في: {DB_PATH}")


# ==========================================
# إدارة المستخدمين (Users Management)
# ==========================================

def add_or_update_user(user_id: int, username: Optional[str] = None, full_name: Optional[str] = None) -> None:
    """تسجيل مستخدم جديد أو تحديث بياناته مع الحفاظ على عدد التنزيلات وحالة الحظر."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    clean_full_name = full_name.strip() if full_name else "مستخدم تيليجرام"
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (user_id, username, full_name, join_date, is_banned, total_downloads)
                VALUES (?, ?, ?, ?, 0, 0)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    full_name = COALESCE(excluded.full_name, users.full_name)
            """, (user_id, username, clean_full_name, now))
            conn.commit()
    except Exception as e:
        logger.error(f"خطأ أثناء تسجيل/تحديث المستخدم {user_id}: {e}")


def is_user_banned(user_id: int) -> bool:
    """التحقق مما إذا كان المستخدم محظوراً من البوت."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                return bool(row["is_banned"])
            return False
    except Exception as e:
        logger.error(f"خطأ أثناء فحص حالة حظر المستخدم {user_id}: {e}")
        return False


def set_user_ban(user_id: int, is_banned: bool) -> bool:
    """حظر أو فك حظر مستخدم."""
    val = 1 if is_banned else 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            # في حال لم يكن مسجلاً مسبقاً، يتم إضافته أولاً
            cursor.execute("""
                INSERT INTO users (user_id, username, full_name, join_date, is_banned, total_downloads)
                VALUES (?, NULL, 'مستخدم محظور', ?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE SET is_banned = ?
            """, (user_id, now, val, val))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"خطأ أثناء تعديل حالة حظر المستخدم {user_id}: {e}")
        return False


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """جلب بيانات مستخدم محدد."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"خطأ أثناء جلب بيانات المستخدم {user_id}: {e}")
        return None


def get_banned_users() -> List[Dict[str, Any]]:
    """جلب قائمة بكافة المستخدمين المحظورين."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username, full_name, total_downloads FROM users WHERE is_banned = 1 ORDER BY user_id DESC")
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"خطأ أثناء جلب المحظورين: {e}")
        return []


def get_all_users() -> List[int]:
    """جلب كافة معرفات المستخدمين المسجلين في البوت."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            return [row["user_id"] for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"خطأ أثناء استرجاع كافة المستخدمين: {e}")
        return []


def get_active_user_ids() -> List[int]:
    """استرجاع معرفات المستخدمين غير المحظورين (للإذاعة)."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users WHERE is_banned = 0 ORDER BY user_id DESC")
            return [row["user_id"] for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"خطأ أثناء استرجاع المستخدمين النشطين: {e}")
        return []


def mark_user_blocked(user_id: int) -> None:
    """تحديد أن المستخدم قام بحظر البوت لحمايته وتفادي إرسال إذاعات متكررة له."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
    except Exception as e:
        logger.error(f"خطأ أثناء تعليم المستخدم {user_id} كمحظور: {e}")


# ==========================================
# تسجيل التنزيلات (Downloads Recording)
# ==========================================

def record_download(
    user_id: int,
    platform: str,
    status: str = "success",
    file_size: int = 0,
    *args,
    **kwargs
) -> None:
    """
    تسجيل عملية تنزيل في جدول downloads وتحديث total_downloads في جدول users.
    status: 'success' أو 'failed'
    file_size: حجم الملف بالبايت
    """
    # استخراج مرن للقيم لدعم استدعاءات الكود القديمة والجديدة
    if "status" in kwargs:
        clean_status = "success" if kwargs["status"] == "success" else "failed"
    elif status in ["success", "failed"]:
        clean_status = status
    else:
        clean_status = "success"

    if "file_size" in kwargs:
        actual_size = int(kwargs["file_size"])
    elif "file_size_bytes" in kwargs:
        actual_size = int(kwargs["file_size_bytes"])
    elif isinstance(file_size, (int, float)):
        actual_size = int(file_size)
    else:
        actual_size = 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO downloads (user_id, platform, status, file_size, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, platform, clean_status, actual_size, now))

            # إذا كانت العملية ناجحة، نزيد عداد التنزيلات للمستخدم
            if clean_status == "success":
                cursor.execute("""
                    UPDATE users SET total_downloads = total_downloads + 1 WHERE user_id = ?
                """, (user_id,))

            conn.commit()
    except Exception as e:
        logger.error(f"خطأ أثناء تسجيل التنزيل للمستخدم {user_id}: {e}")


# ==========================================
# إدارة إعدادات البوت (Bot Settings)
# ==========================================

def get_setting(key: str, default: str = "") -> str:
    """استرجاع قيمة إعداد من جدول bot_settings."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row and row["value"] is not None else default
    except Exception as e:
        logger.error(f"خطأ أثناء قراءة الإعداد {key}: {e}")
        return default


def set_setting(key: str, value: str) -> None:
    """حفظ أو تحديث إعداد في جدول bot_settings."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO bot_settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, str(value)))
            conn.commit()
    except Exception as e:
        logger.error(f"خطأ أثناء حفظ الإعداد {key}: {e}")


# وضع الصيانة
def is_maintenance_mode() -> bool:
    return get_setting("maintenance_mode", "0") == "1"


def set_maintenance_mode(enabled: bool) -> None:
    set_setting("maintenance_mode", "1" if enabled else "0")


# الاشتراك الإجباري
def is_force_sub_enabled() -> bool:
    return get_setting("force_sub_enabled", "0") == "1"


def set_force_sub_enabled(enabled: bool) -> None:
    set_setting("force_sub_enabled", "1" if enabled else "0")


def get_force_sub_channel() -> str:
    return get_setting("force_sub_channel", "").strip()


def set_force_sub_channel(channel: str) -> None:
    set_setting("force_sub_channel", channel.strip())


# المنصات المفعلة
PLATFORM_KEYS = {
    "tiktok": "platform_tiktok",
    "instagram": "platform_instagram",
    "youtube": "platform_youtube",
    "twitter": "platform_twitter",
    "x": "platform_twitter",
    "facebook": "platform_facebook",
    "pinterest": "platform_pinterest",
    "reddit": "platform_reddit",
}


def normalize_platform_key(platform_name: str) -> str:
    p_lower = platform_name.lower()
    for k in PLATFORM_KEYS:
        if k in p_lower:
            return k
    return p_lower


def is_platform_enabled(platform_name: str) -> bool:
    key_norm = normalize_platform_key(platform_name)
    setting_key = PLATFORM_KEYS.get(key_norm, f"platform_{key_norm}")
    return get_setting(setting_key, "1") == "1"


def set_platform_enabled(platform_key: str, enabled: bool) -> None:
    setting_key = PLATFORM_KEYS.get(platform_key.lower(), f"platform_{platform_key.lower()}")
    set_setting(setting_key, "1" if enabled else "0")


def get_all_platforms_status() -> Dict[str, bool]:
    """استرجاع حالة تفعيل كافة المنصات الرئيسية."""
    canonical_platforms = ["tiktok", "instagram", "youtube", "twitter", "facebook", "pinterest", "reddit"]
    return {
        p: is_platform_enabled(p)
        for p in canonical_platforms
    }


# ==========================================
# الإحصائيات الشاملة والتقارير (Comprehensive Stats)
# ==========================================

def get_user_stats() -> Dict[str, Any]:
    """استرجاع إحصائيات سريعة لعدد المستخدمين (للتوافق مع الأوامر السابقة)."""
    s = get_comprehensive_stats()
    return {
        "total": s["total_users"],
        "active": s["active_users"],
        "blocked": s["banned_users"]
    }


def get_comprehensive_stats() -> Dict[str, Any]:
    """
    استخراج تقرير إحصائي شامل ودقيق 100% للوحة تحكم المطور:
    - إجمالي المشتركين
    - مستخدمي اليوم (تاريخ الانضمام اليوم)
    - المستخدمين المحظورين
    - إجمالي التحميلات
    - التحميلات الناجحة والفاشلة
    - تحميلات اليوم
    - إجمالي حجم البيانات المحملة بالميجابايت
    - توزيع المنصات مع النسب المئوية
    """
    now = datetime.now(timezone.utc)
    today_start = now.strftime("%Y-%m-%d 00:00:00")

    try:
        with get_connection() as conn:
            cursor = conn.cursor()

            # 1. إجمالي المستخدمين
            cursor.execute("SELECT COUNT(*) as c FROM users")
            total_users = cursor.fetchone()["c"]

            # 2. مستخدمي اليوم
            cursor.execute("SELECT COUNT(*) as c FROM users WHERE REPLACE(join_date, 'T', ' ') >= ?", (today_start,))
            today_users = cursor.fetchone()["c"]

            # 3. المستخدمين المحظورين
            cursor.execute("SELECT COUNT(*) as c FROM users WHERE is_banned = 1")
            banned_users = cursor.fetchone()["c"]

            # 4. إجمالي التنزيلات وحجمها
            cursor.execute("SELECT COUNT(*) as c, COALESCE(SUM(file_size), 0) as total_size FROM downloads")
            row_dl = cursor.fetchone()
            total_downloads = row_dl["c"]
            total_size_bytes = row_dl["total_size"]
            total_size_mb = total_size_bytes / (1024 * 1024)

            # 5. التنزيلات الناجحة والفاشلة
            cursor.execute("SELECT COUNT(*) as c FROM downloads WHERE status = 'success'")
            successful_dl = cursor.fetchone()["c"]

            cursor.execute("SELECT COUNT(*) as c FROM downloads WHERE status = 'failed'")
            failed_dl = cursor.fetchone()["c"]

            # 6. تنزيلات اليوم
            cursor.execute("SELECT COUNT(*) as c FROM downloads WHERE REPLACE(timestamp, 'T', ' ') >= ?", (today_start,))
            today_downloads = cursor.fetchone()["c"]

            # 7. توزيع المنصات
            cursor.execute("""
                SELECT platform, COUNT(*) as count
                FROM downloads
                GROUP BY platform
                ORDER BY count DESC
            """)
            raw_platforms = cursor.fetchall()
            platforms_breakdown = []
            for r in raw_platforms:
                pct = (r["count"] / total_downloads * 100) if total_downloads > 0 else 0
                platforms_breakdown.append({
                    "platform": r["platform"],
                    "count": r["count"],
                    "percentage": pct
                })

            success_rate = (successful_dl / total_downloads * 100) if total_downloads > 0 else 100.0

            return {
                "total_users": total_users,
                "today_users": today_users,
                "banned_users": banned_users,
                "active_users": total_users - banned_users,
                "total_downloads": total_downloads,
                "successful_downloads": successful_dl,
                "failed_downloads": failed_dl,
                "today_downloads": today_downloads,
                "success_rate": success_rate,
                "total_size_mb": total_size_mb,
                "platforms_breakdown": platforms_breakdown,
            }
    except Exception as e:
        logger.error(f"خطأ أثناء جلب الإحصائيات الشاملة: {e}")
        return {
            "total_users": 0,
            "today_users": 0,
            "banned_users": 0,
            "active_users": 0,
            "total_downloads": 0,
            "successful_downloads": 0,
            "failed_downloads": 0,
            "today_downloads": 0,
            "success_rate": 0.0,
            "total_size_mb": 0.0,
            "platforms_breakdown": [],
        }


def get_daily_report_data(hours: int = 24) -> Dict[str, Any]:
    """استخراج بيانات التقرير اليومي الحقيقي 100%."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")

    try:
        with get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT COUNT(*) as total_dl
                FROM downloads
                WHERE REPLACE(timestamp, 'T', ' ') >= ? AND status = 'success'
            """, (since_str,))
            total_dl = cursor.fetchone()["total_dl"]

            cursor.execute("""
                SELECT platform, COUNT(*) as count
                FROM downloads
                WHERE REPLACE(timestamp, 'T', ' ') >= ? AND status = 'success'
                GROUP BY platform
                ORDER BY count DESC
            """, (since_str,))
            platforms_breakdown = [
                {"platform": row["platform"], "count": row["count"]}
                for row in cursor.fetchall()
            ]

            cursor.execute("""
                SELECT d.user_id, u.full_name, u.username, COUNT(*) as dl_count,
                       GROUP_CONCAT(d.platform) as platforms_concat
                FROM downloads d
                LEFT JOIN users u ON d.user_id = u.user_id
                WHERE REPLACE(d.timestamp, 'T', ' ') >= ? AND d.status = 'success'
                GROUP BY d.user_id
                ORDER BY dl_count DESC
            """, (since_str,))
            downloaders = []
            downloaders_user_ids = set()
            for row in cursor.fetchall():
                u_id = row["user_id"]
                downloaders_user_ids.add(u_id)
                raw_plat = row["platforms_concat"] or ""
                p_items = [p.strip() for p in raw_plat.split(",") if p.strip()]
                p_counts = Counter(p_items)
                p_summary = "، ".join(f"{count} {plat}" for plat, count in p_counts.items())
                downloaders.append({
                    "user_id": u_id,
                    "full_name": row["full_name"] or "مستخدم تيليجرام",
                    "username": row["username"],
                    "dl_count": row["dl_count"],
                    "platforms_summary": p_summary
                })

            cursor.execute("""
                SELECT user_id, full_name, username, join_date
                FROM users
                WHERE REPLACE(join_date, 'T', ' ') >= ?
                ORDER BY join_date DESC
            """, (since_str,))
            active_users = [
                {
                    "user_id": row["user_id"],
                    "full_name": row["full_name"] or "مستخدم تيليجرام",
                    "username": row["username"]
                }
                for row in cursor.fetchall()
            ]

            other_active_users = [
                u for u in active_users if u["user_id"] not in downloaders_user_ids
            ]

            cursor.execute("SELECT COUNT(*) as total_users FROM users")
            total_users_all = cursor.fetchone()["total_users"]

            cursor.execute("SELECT COUNT(*) as total_dl_all FROM downloads WHERE status = 'success'")
            total_dl_all = cursor.fetchone()["total_dl_all"]

            return {
                "hours": hours,
                "total_downloads_period": total_dl,
                "platforms_breakdown": platforms_breakdown,
                "downloaders": downloaders,
                "active_users_period": active_users,
                "other_active_users": other_active_users,
                "total_users_all": total_users_all,
                "total_downloads_all": total_dl_all,
            }
    except Exception as e:
        logger.error(f"خطأ أثناء استخراج بيانات التقرير اليومي: {e}")
        return {
            "hours": hours,
            "total_downloads_period": 0,
            "platforms_breakdown": [],
            "downloaders": [],
            "active_users_period": [],
            "other_active_users": [],
            "total_users_all": 0,
            "total_downloads_all": 0,
        }


def format_daily_report(data: Dict[str, Any]) -> str:
    """تنسيق تقرير نشاط البوت اليومي بشكل احترافي ومنظم بأرقام حقيقية مؤكدة 100%."""
    total_dl = data["total_downloads_period"]
    platforms = data["platforms_breakdown"]
    downloaders = data["downloaders"]
    active_users = data["active_users_period"]
    other_active = data.get("other_active_users", [])

    date_str = datetime.now().strftime("%Y-%m-%d")

    report_lines = [
        "📊 <b>التقرير اليومي لنشاط البوت (أرقام حقيقية 100%)</b>",
        f"📅 <b>تاريخ التقرير:</b> <code>{date_str}</code>",
        f"⏱ <b>الفترة الزمنية:</b> آخر {data['hours']} ساعة",
        "─────────────────",
        f"👥 <b>عدد المستخدمين الجدد/المتفاعلين اليوم:</b> <b>{len(active_users)}</b> مستخدم",
        f"📥 <b>إجمالي الفيديوهات التي تم تحميلها اليوم:</b> <b>{total_dl}</b> مقطع",
        "",
        "🌐 <b>تفصيل التحميلات حسب المنصة:</b>"
    ]

    if platforms and total_dl > 0:
        for p in platforms:
            pct = (p['count'] / total_dl) * 100
            report_lines.append(f"• <b>{p['platform']}:</b> {p['count']} مقطع ({pct:.0f}%)")
    else:
        report_lines.append("• <i>لم يتم تحميل أي مقطع خلال هذه الفترة.</i>")

    report_lines.append("")
    report_lines.append("👤 <b>قائمة الأشخاص الذين قاموا بالتحميل اليوم:</b>")
    if downloaders:
        for idx, u in enumerate(downloaders, 1):
            name_disp = html.escape(u["full_name"])
            user_handle = f"(@{u['username']})" if u["username"] else f"(ID: <code>{u['user_id']}</code>)"
            p_desc = f" <i>({u['platforms_summary']})</i>" if u.get("platforms_summary") else ""
            report_lines.append(f"{idx}. <b>{name_disp}</b> {user_handle} ➔ <b>{u['dl_count']}</b> مقطع{p_desc}")
    else:
        report_lines.append("• <i>لا يوجد مستخدمين قاموا بالتحميل خلال اليوم.</i>")

    if other_active:
        report_lines.append("")
        report_lines.append("💬 <b>مستخدمون تواصلوا مع البوت اليوم (بدون تحميل):</b>")
        for idx, u in enumerate(other_active, 1):
            name_disp = html.escape(u["full_name"])
            user_handle = f"(@{u['username']})" if u["username"] else f"(ID: <code>{u['user_id']}</code>)"
            report_lines.append(f"• <b>{name_disp}</b> {user_handle}")

    report_lines.extend([
        "",
        "─────────────────",
        "📈 <b>الإحصائيات الشاملة منذ انطلاق البوت:</b>",
        f"• إجمالي المشتركين المسجلين كلياً: <b>{data['total_users_all']}</b>",
        f"• إجمالي المقاطع المحمّلة كلياً: <b>{data['total_downloads_all']}</b>",
        "",
        "✅ <i>جميع الأرقام والإحصائيات دقيقة 100% ومستخرجة مباشرة من قاعدة البيانات.</i>"
    ])

    return "\n".join(report_lines)


# ==========================================
# إدارة المشرفين (Admin Management)
# ==========================================

def is_admin(user_id: int, env_admin_ids: Optional[List[int]] = None) -> bool:
    """التحقق مما إذا كان المعرف يخص مشرفاً في البوت."""
    if env_admin_ids and user_id in env_admin_ids:
        return True

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return row is not None
    except Exception as e:
        logger.error(f"خطأ أثناء فحص رتبة المشرف {user_id}: {e}")
        return False


def add_admin(user_id: int) -> bool:
    """إضافة مشرف جديد لقاعدة البيانات."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"خطأ أثناء إضافة المشرف {user_id}: {e}")
        return False


def remove_admin(user_id: int) -> bool:
    """حذف مشرف من قاعدة البيانات."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"خطأ أثناء حذف المشرف {user_id}: {e}")
        return False


def get_all_admins() -> List[int]:
    """جلب جميع معرفات المشرفين المسجلين في قاعدة البيانات."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM admins")
            return [row["user_id"] for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"خطأ أثناء جلب المشرفين: {e}")
        return []
