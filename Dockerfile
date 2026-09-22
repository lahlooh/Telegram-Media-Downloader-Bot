FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=10000

# تثبيت FFmpeg و Node.js ومكتبات النظام الأساسية
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# تثبيت مكتبات بايثون أولاً للاستفادة من الكاش
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ بقية ملفات المشروع
COPY . .

# إنشاء المجلدات المطلوبة
RUN mkdir -p data downloads

EXPOSE 10000

CMD ["python", "bot.py"]
