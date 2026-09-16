FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu-core fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Shriftlarni build vaqtida yuklab olamiz (repoda fonts/ bo'lmasa ham ishlaydi)
RUN python fetch_fonts.py || true

ENV PORT=8080
EXPOSE 8080
CMD ["python", "-c", "import os,uvicorn; uvicorn.run('app:app',host='0.0.0.0',port=int(os.environ.get('PORT','8080')))"]
