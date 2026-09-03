# 🎬 Montaj Bot — Telegram uchun avtomatik video montaj

Telegram botiga video tashlaysiz — bot uni **kesib, o'zbekcha subtitr qo'yib, animatsiyali, 9:16 formatda, ovozi tozalangan** holda qaytaradi. Instagram Reels / TikTok / YouTube Shorts / VK uchun tayyor.

## Nima qiladi
- 🎙️ **O'zbekcha aniq subtitr** — Groq Whisper large-v3 (bepul API)
- ✂️ **Aqlli kesish** — uzun jimliklarni oladi, gap o'rtasidan kesmaydi
- 💬 **So'z-ba-so'z animatsiyali subtitr** (viral uslub)
- 📱 **9:16 format** + silliq zoom + rang jilosi
- 🔊 **Ovoz tozalash** — shovqin olib tashlash, tiniqlashtirish, balandlik tenglash

---

## 1-qadam: kerakli 2 ta bepul kalit

**Telegram bot tokeni:**
1. Telegram'da **@BotFather** ni oching
2. `/newbot` → botga nom va username bering
3. U bergan **tokenni** saqlang (masalan `12345:AAF...`)

**Groq API kaliti (bepul):**
1. **https://console.groq.com** ga kiring (Google bilan)
2. **API Keys** → **Create API Key**
3. Kalitni saqlang (`gsk_...`)

---

## 2-qadam: ishga tushirish

### A) Doimiy server — Railway (24/7, tavsiya) ⭐
1. Bu loyihani GitHub'ga yuklang (pastda qarang)
2. **https://railway.app** ga kiring → **New Project** → **Deploy from GitHub repo** → shu repo'ni tanlang
3. **Variables** bo'limiga ikkita o'zgaruvchi qo'shing:
   - `TELEGRAM_TOKEN` = BotFather tokeni
   - `GROQ_API_KEY` = Groq kaliti
4. Deploy tugaydi — bot ishlaydi! Telegram'da botga video tashlang.

(Render.com ham xuddi shunday ishlaydi — Dockerfile avtomatik topiladi.)

### B) O'z kompyuteringizda
1. **Python** va **ffmpeg** o'rnatilgan bo'lsin
2. `pip install -r requirements.txt`
3. `.env.example` faylidan nusxa olib, `.env` deb saqlang va kalitlarni qo'ying
4. `python montaj_bot.py`

---

## 3-qadam: GitHub'ga yuklash

**Eng oson (brauzerda):**
1. https://github.com/new → repo nomini yozing → **Create**
2. **uploading an existing file** havolasini bosing
3. Shu papkadagi barcha fayllarni sudrab tashlang → **Commit**

**Yoki terminalda:**
```
git init
git add .
git commit -m "montaj bot"
git branch -M main
git remote add origin https://github.com/FOYDALANUVCHI/REPO.git
git push -u origin main
```

> ⚠️ `.env` faylini GitHub'ga yuklamang (kalitlar maxfiy) — `.gitignore` buni avtomatik himoya qiladi.

---

## Sozlamalar
`montaj_bot.py` ichidagi `CFG` bo'limidan o'zgartirasiz: subtitr rangi, shrift, o'lcham, kesish sezgirligi, zoom, ovoz tozalash va h.k.

## Eslatma
- Telegram botlari **20MB gacha** videoni qabul qiladi (oddiy rejimda). Kattaroq video uchun self-hosted Telegram Bot API kerak.
- Groq bepul limiti kunlik ko'p videoga yetadi.
