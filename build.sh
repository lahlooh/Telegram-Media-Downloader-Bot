#!/usr/bin/env bash
# Render build script for Telegram Downloader Bot
set -o errexit

echo "🚀 [1/3] تحديث pip وتثبيت مكتبات بايثون..."
pip install --upgrade pip
pip install -r requirements.txt

echo "🎬 [2/3] فحص محرك FFmpeg..."
python -c "
import config
print('FFmpeg path:', config.FFMPEG_PATH)
"

echo "📂 [3/3] تجهيز مجلدات التخزين المؤقت والبيانات..."
mkdir -p data downloads

echo "✅ تم تجهيز بيئة التشغيل على Render بنجاح!"
