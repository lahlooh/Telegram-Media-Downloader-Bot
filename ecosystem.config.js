/**
 * إعدادات تشغيل البوت عبر مدير العمليات PM2
 * متوافق تلقائياً مع Windows و Linux (VPS / خوادم السحاب)
 * يضمن تشغيل البوت 24/7 وإعادة التشغيل التلقائي عند التوقف أو إعادة تشغيل الخادم
 */

const { execSync } = require("child_process");
const fs = require("fs");

function resolvePythonInterpreter() {
  // 1. إذا تم تحديد مسار يدوي مسبقاً عبر متغير البيئة PYTHON_PATH
  if (process.env.PYTHON_PATH && fs.existsSync(process.env.PYTHON_PATH)) {
    return process.env.PYTHON_PATH;
  }

  // 2. التحقق التلقائي من وجود بيئة افتراضية محلياً (venv / .venv)
  const localVenvs = [
    "./.venv/Scripts/python.exe",
    "./venv/Scripts/python.exe",
    "./.venv/bin/python",
    "./venv/bin/python",
  ];
  for (const vPath of localVenvs) {
    if (fs.existsSync(vPath)) {
      return vPath;
    }
  }

  // 3. استخراج المسار المطلق لمفسر بايثون الحقيقي في النظام لتفادي مشاكل WindowsApps
  const candidates = process.platform === "win32" ? ["python", "py"] : ["python3", "python"];
  for (const cmd of candidates) {
    try {
      const output = execSync(cmd + ' -c "import sys; sys.stdout.write(sys.executable)"', {
        encoding: "utf8",
        stdio: ["pipe", "pipe", "ignore"],
      }).trim();
      if (output && fs.existsSync(output)) {
        return output;
      }
    } catch (e) {
      // متابعة البحث في المرشح التالي
    }
  }

  // 4. مسارات شائعة في ويندوز
  if (process.platform === "win32") {
    const knownPaths = [
      "C:\\Users\\LENOVO\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe",
    ];
    for (const kp of knownPaths) {
      if (fs.existsSync(kp)) return kp;
    }
    return "python";
  }

  return "python3";
}

const pythonInterpreter = resolvePythonInterpreter();

module.exports = {
  apps: [
    {
      name: "telegram-downloader-bot",
      script: "bot.py",
      
      // مفسر بايثون المستكشف تلقائياً بالمسار المطلق
      interpreter: pythonInterpreter,
      
      // وسيط -u لضمان تدفق السجلات لحظياً وبدون تأخير
      interpreter_args: "-u",
      
      // إعادة التشغيل التلقائي
      autorestart: true,
      restart_delay: 2000,
      exp_backoff_restart_delay: 100,
      max_restarts: 50,
      
      // تجنب مراقبة الملفات في بيئة الإنتاج لمنع إعادة التشغيل العشوائية
      watch: false,
      
      // حد استهلاك الذاكرة لإعادة التشغيل الآمن
      max_memory_restart: "1G",
      
      // مسار ملفات السجلات (سجل الأخطاء والمخرجات العادية)
      error_file: "./logs/pm2-error.log",
      out_file: "./logs/pm2-out.log",
      merge_logs: true,
      time: true,
      
      // المتغيرات البيئية وترميز UTF-8 لدعم النصوص العربية والرموز التعبيرية
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
        NODE_ENV: "production",
      },
    },
  ],
};

