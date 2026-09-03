FROM python:3.11-slim

# ffmpeg (video/audio uchun) o'rnatiladi
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# TELEGRAM_TOKEN va GROQ_API_KEY muhit o'zgaruvchilaridan olinadi
CMD ["python", "montaj_bot.py"]
