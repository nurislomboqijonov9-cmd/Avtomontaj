# 🎬 Montaj Studio — veb video-tahrirlagich

Videongizni yuklaysiz → bot o'zbekcha subtitr qo'yadi, keraksiz joylarni (jimlik, takror gaplar) kesadi, gapga mos animatsiya/rasm qo'shadi, 9:16 ga o'giradi, ovozini tozalaydi.
**Farqi:** endi hammasini o'zingiz timeline'da tuzatasiz — qayerni kesish, subtitr qayerda turishi, qaysi joyga animatsiya qo'yish, tempini namuna videoga moslash. Tayyor videolar **Tarix** bo'limida saqlanadi.

## Bosqichlar
1. **Kesish** — avto takliflar (jimlik + takror gaplar) ko'rinadi; yoqasiz/o'chirasiz yoki o'zingiz qo'lda kesasiz (pleyerni joyga qo'yib "Boshi/Oxiri").
2. **Subtitr** — sinxron siljishi, balandligi (soqol ostida), o'lchami, rang, so'z soni. Pleyerda **jonli ko'rinadi** — sozni to'g'ri qo'yguncha.
3. **Animatsiya** — "Avto g'oyalar" yoki qo'lda; har biriga Gemini rasm chizadi, joyini/vaqtini o'zingiz tanlaysiz.
4. **Namuna uslub** — yoqtirgan videongizni yuklang, bot uning subtitr tempi/kesish uslubini o'lchab sizga moslaydi.

---

## Railway'ga o'rnatish (24/7 onlayn)

1. Bu papkani GitHub'ga yuklang (yangi repo → **uploading an existing file** → hammasini tashlang → Commit).
2. **railway.app** → **New Project** → **Deploy from GitHub repo** → shu repo.
3. **Variables** bo'limiga qo'shing (eski bot bilan bir xil Vertex sozlamalari):

   | O'zgaruvchi | Qiymat |
   |---|---|
   | `GCP_PROJECT_ID` | Google Cloud project ID |
   | `GCP_LOCATION` | `us-central1` |
   | `GCP_SA_JSON` | Service-account JSON (butun matn bir qatorda) |
   | `VERTEX_MODEL` | `gemini-2.5-flash` (barqarorroq uchun `gemini-2.5-pro`) |
   | `APP_PASSWORD` | maxfiy parol (ixtiyoriy, lekin tavsiya — Vertex kredit himoyasi) |

   Ixtiyoriy zaxira: `GROQ_API_KEY`, `GEMINI_API_KEY`, `PEXELS_API_KEY`.
4. Railway **Networking → Generate Domain** bosing → link paydo bo'ladi. O'sha linkni telefon/kompyuterdan ochasiz.

> Dockerfile ffmpeg va shriftlarni avtomatik o'rnatadi. Deploy tugagach link ishlaydi.

## O'z kompyuteringizda ishlatish
```
pip install -r requirements.txt      # ffmpeg alohida o'rnatilgan bo'lsin
cp .env.example .env                 # kalitlarni qo'ying
python app.py                        # http://localhost:8080
```

## Eslatma
- **Rasm chizish** Vertex'da `gemini-2.5-flash-image` orqali. Ishlamasa avtomatik Imagen'ga, keyin Pexels'ga o'tadi. Xato bo'lsa animatsiya izohda sabab ko'rsatiladi (video baribir chiqadi).
- Tarix serverdagi `data/` papkasida saqlanadi. Railway'da doimiy saqlash uchun **Volume** ulab, `DATA_DIR` ni /shu volumega yo'naltiring.
