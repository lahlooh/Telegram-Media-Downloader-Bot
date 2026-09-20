# Telegram Media Downloader Bot

بوت تيليجرام غير متزامن (Async/Await) مبني بلغة Python وإطار العمل `aiogram 3.x` ومحرك `yt-dlp`، مخصص لتحميل مقاطع الفيديو والوسائط من شبكات التواصل الاجتماعي المختلفة (TikTok, Instagram, YouTube, X/Twitter, Facebook, Reddit, Pinterest) بجودة عالية ومعالجة قيود حجم الملفات في تيليجرام.

---

## المزايا الأساسية

- **استخراج ودمج الوسائط**: سحب مسارات الصوت والفيديو بأعلى دقة متوفرة مع دمجها محلياً عبر `FFmpeg` في حاوية MP4 متوافقة مع البث المباشر على تيليجرام.
- **معالجة قيود الأحجام**: الالتزام التلقائي بحد الرفع للبوتات في تيليجرام (50MB) واختيار أفضل دقة ممكنة تقع تحت الحد قبل الرفع.
- **دعم متقدم لتيك توك**: التعامل مع منشورات الصور المتعددة وتنزيلها كألبوم صور (Media Group)، وإمكانية تحويل المنشور ذي الصورة الواحدة إلى فيديو مدمج بالصوت أو إرساله كصورة.
- **معالجة غير متزامنة وغير حاجبة**: تشغيل كافة عمليات التنزيل والدمج الثقيلة داخل مسارات منفصلة (`asyncio.to_thread`) لضمان استجابة البوت لبقية المستخدمين دون تجميد.
- **توليد البيانات محلياً**: إنشاء الصور المصغرة (Thumbnails) واستخراج الأبعاد والمدة بدقة عبر `FFmpeg`.
- **تنظيف دوري للموارد**: حذف فوري للملفات والمجلدات المؤقتة فور اكتمال الإرسال أو عند حدوث أي خطأ.

---

## المتطلبات الأساسية

- **Python 3.10+**
- **FFmpeg**: يتضمن المشروع حزمة `imageio-ffmpeg` التي توفر مفسر FFmpeg تلقائياً، أو يمكن استخدام نسخة النظام إذا كانت متوفرة في `PATH`.
- **Node.js & PM2** *(اختياري)*: لإدارة تشغيل البوت في الخلفية 24/7 على خوادم الإنتاج أو VPS.

---

## خطوات التثبيت والإعداد

### 1. استنساخ المستودع وإنشاء البيئة الافتراضية

```bash
git clone <repository-url>
cd "Telegram Video Downloader Bot"

# إنشاء البيئة الافتراضية
python -m venv .venv

# تفعيل البيئة الافتراضية:
# على أنظمة Windows (PowerShell):
.venv\Scripts\Activate.ps1
# أو على Command Prompt:
.venv\Scripts\activate.bat
# على أنظمة Linux / macOS:
source .venv/bin/activate
```

### 2. تثبيت الاعتماديات

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. ضبط المتغيرات البيئية

قم بنسخ ملف الإعداد النموذجي إلى `.env`:

```bash
cp .env.example .env
```

ثم حدد قيمة `BOT_TOKEN` الخاص بك المستخرج من [@BotFather](https://t.me/BotFather):

```env
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
MAX_FILE_SIZE_MB=48
DOWNLOAD_DIR=downloads
COOKIES_FILE=cookies.txt
```

---

## أوامر التشغيل

### تشغيل مباشر (بيئة التطوير)

```bash
python bot.py
```

### تشغيل دائم في الإنتاج عبر PM2

يتضمن المشروع ملف `ecosystem.config.js` مضبوطاً لاكتشاف مفسر بايثون تلقائياً وإعادة تشغيل البوت عند أي توقف غير متوقع:

```bash
# تشغيل البوت
pm2 start ecosystem.config.js

# متابعة السجلات المباشرة
pm2 logs telegram-downloader-bot

# فحص حالة العملية
pm2 status

# إيقاف أو إعادة التشغيل
pm2 restart telegram-downloader-bot
pm2 stop telegram-downloader-bot
```

---

## هيكل المشروع

```text
├── bot.py                # نقطة الدخول ومعالجة رسائل وأوامر تيليجرام
├── config.py             # إدارة الإعدادات واكتشاف مسار FFmpeg والترويسات
├── downloader.py         # محرك الاستخراج والتنزيل والمعالجة عبر yt-dlp و FFmpeg
├── ecosystem.config.js   # تكوين مدير العمليات PM2 للإنتاج
├── test_suite.py         # اختبارات الوحدة للمشروع
├── requirements.txt      # الاعتماديات والحزم المطلوبة
├── .env.example          # نموذج إعدادات المتغيرات البيئية
├── .gitignore            # قواعد تجاهل الملفات المؤقتة والسرية
└── README.md             # دليل التثبيت والتوثيق التقني
```

---

## تشغيل الاختبارات

للتحقق من سلامة المكونات وتنظيف الروابط ومنطق تجزئة الألبومات والأحجام:

```bash
python test_suite.py
```
