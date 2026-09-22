# 🚀 دليل رفع وتشغيل البوت على منصة Render مجاناً (24/7)

تم إعداد وتجهيز كافة ملفات المشروع لتعمل مباشرة على منصة **Render** كخدمة ويب مجانية (**Free Web Service**) مع خادم مدمج لفحص الصحة (**Health Check Server**) لتلبية متطلبات المنصة بالكامل.

---

## 📁 1. الملفات التي تم تجهيزها لك في المشروع:

1. **`render.yaml`**: ملف إعداد آلي (Blueprint) لإنشاء الخدمة وضبط متغيرات البيئة بضغطة زر واحدة.
2. **`build.sh`**: سكريبت بناء وتجهيز البيئة وتثبيت الاعتماديات تلقائياً أثناء النشر.
3. **`Dockerfile`**: خيار تشغيل بديل عبر Docker مجهز ومثبت به محرك FFmpeg ومكتبات الوسائط كاملة.
4. **`bot.py`**: يحتوي على خادم ويب غير متزامن خفيف يستمع تلقائياً للمنفذ (`$PORT`) مع مسارات `/health` و `/status` للرد على منصة Render بـ `HTTP 200 OK`.

---

## 🛠️ 2. خطوات الرفع السريعة (3 دقائق):

### الخطوة الأولى: رفع الكود إلى GitHub
1. قم بإنشاء مستودع جديد (Repository) على [GitHub](https://github.com) باسم تختاره (مثلاً `telegram-downloader-bot`).
2. في مجلد المشروع على جهازك، قم برفع الملفات عبر سطر الأوامر (Terminal / PowerShell):
   ```bash
   git init
   git add .
   git commit -m "Initial commit for Render deployment"
   git branch -M main
   git remote add origin https://github.com/<YOUR_USERNAME>/<REPO_NAME>.git
   git push -u origin main
   ```

---

### الخطوة الثانية: تشغيل البوت على Render

1. افتح موقع [Render Dashboard](https://dashboard.render.com) وسجّل الدخول بحساب GitHub الخاص بك.
2. اضغط على زر **New +** في أعلى الصفحة، واختر:
   - **Blueprint**: إذا أردت أن يقوم Render بقراءة ملف `render.yaml` وضبط كل شيء تلقائياً.
   - **أو Web Service**: للإعداد اليدوي (موضحة بالأسفل).

#### إعدادات الـ Web Service اليدوية:
- **Connect a repository**: اختر مستودع البوت الذي رفعته للتو.
- **Name**: `telegram-downloader-bot` (أو أي اسم تفضله).
- **Runtime**: `Python 3` (أو اختر `Docker` إذا كنت تفضل بيئة الحاويات).
- **Build Command**: `bash build.sh` (أو `pip install -r requirements.txt`).
- **Start Command**: `python bot.py`.
- **Instance Type**: اختر **Free**.

---

### الخطوة الثالثة: إضافة المتغيرات السرية (Environment Variables)

في تبويب **Environment** داخل لوحة تحكم الخدمة على Render، أضف المتغيرات التالية (مهم جداً لأن ملف `.env` لا يتم رفعه إلى GitHub):

| اسم المتغير (Key) | القيمة (Value) | الشرح |
| :--- | :--- | :--- |
| `BOT_TOKEN` | `8901572985:AAHNqylEHAdy0K5O4qaQ8DlzSiD8vLdtyyg` | توكن البوت الخاص بك |
| `ADMIN_ID` | `6436816730` | الآيدي الخاص بك كمشرف ولوحة التحكم |
| `MAX_FILE_SIZE_MB` | `48` | أقصى حجم للملف (48 ميجابايت) |
| `PORT` | `10000` | المنفذ لخادم فحص الحالة (يُعيّنه Render تلقائياً) |

---

### الخطوة الرابعة: فحص الصحة (Health Check)
- في قسم **Advanced**، يمكنك تعيين:
  - **Health Check Path**: `/health`

اضغط الآن على **Create Web Service** أو **Manual Deploy**!
سيقوم Render ببناء التطبيق وتشغيل البوت والتحقق من حالته في خلال دقيقة واحدة.

---

## ⚡ 3. خدعة تشغيل البوت 24 ساعة دون أن ينام (Free 24/7 Keep-Alive):

في الخطة المجانية على Render، يتوقف السيرفر مؤقتاً (Sleep mode) إذا لم يستقبل زيارة ويب لمدة 15 دقيقة. 
لإبقاء البوت متصلاً وشغالاً 24 ساعة يومياً بدون انقطاع:

1. انسخ الرابط المجاني للخدمة الذي يعطيه لك Render (مثلاً: `https://telegram-downloader-bot.onrender.com`).
2. ادخل إلى موقع مجاني لجدولة الزيارات مثل:
   - [cron-job.org](https://cron-job.org) (مجاني 100%)
   - أو [UptimeRobot](https://uptimerobot.com) (مجاني 100%)
3. أنشئ مهمة جديدة (New Cron / Monitor):
   - **URL**: `https://YOUR-APP-NAME.onrender.com/health`
   - **Interval / Schedule**: كل **10 دقائق** (Every 10 minutes).
4. اضغط حفظ!

بهذه الطريقة البسيطة، سيتلقى الخادم طلباً خفيفاً كل 10 دقائق، وسيرد عليه فوراً بـ `HTTP 200 OK`، ولن يدخل في وضع السكون أبداً، وسيظل بوت التيليجرام نشطاً ومستعداً للتحميل في أي لحظة! 🎬✨
