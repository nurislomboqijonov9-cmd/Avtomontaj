# 🎬 Montaj Studio — veb video-tahrirlagich

Videongizni yuklaysiz → bot o'zbekcha subtitr qo'yadi, keraksiz joylarni (jimlik, takror gaplar) kesadi, gapga mos animatsiya/rasm qo'shadi, 9:16 ga o'giradi, ovozini tozalaydi.
**Farqi:** endi hammasini o'zingiz timeline'da tuzatasiz — qayerni kesish, subtitr qayerda turishi, qaysi joyga animatsiya qo'yish, tempini namuna videoga moslash. Tayyor videolar **Tarix** bo'limida saqlanadi.

## Bosqichlar
1. **Kesish** — avto takliflar (jimlik + takror gaplar) ko'rinadi; yoqasiz/o'chirasiz yoki o'zingiz qo'lda kesasiz (pleyerni joyga qo'yib "Boshi/Oxiri").
2. **Subtitr** — sinxron siljishi, balandligi (soqol ostida), o'lchami, rang, so'z soni. Pleyerda **jonli ko'rinadi** — sozni to'g'ri qo'yguncha.
3. **Animatsiya** — "Avto g'oyalar" yoki qo'lda; har biriga Gemini rasm chizadi, joyini/vaqtini o'zingiz tanlaysiz.
4. **Namuna uslub** — yoqtirgan videongizni yuklang, bot uning subtitr tempi/kesish uslubini o'lchab sizga moslaydi.

## Studio (asosiy interfeys)
**Bo'limlar:** yon menyuda *Bosh sahifa* (statistika, yangi loyiha, so'nggi loyihalar), *Loyihalar* (qidirish, filtr, nom o'zgartirish, nusxa olish, o'chirish), *Tayyor videolar* (ko'rish va yuklab olish), *Uslublar* (namuna videodan olingan montaj uslublari), *Sozlamalar* (akkaunt, yangi loyiha uchun standart til/format/sifat, klaviatura yorliqlari). Loyiha ochilganda muharrir chiqadi.

Sayt ochilganda (`/`) yangi Studio chiqadi. Eski interfeys `/classic` manzilida o'zgarishsiz turadi.
- **Avto montaj:** video tashlanadi -> transkripsiya, pauza va takrorlarni kesish, subtitr, stiker, hook, b-roll avtomatik qo'yiladi. Keyin hammasini taymlaynda qo'lda o'zgartirish mumkin.
- **Namuna videodan uslub:** bosh sahifada yoki «AI uslub» bo'limida namuna video yuklanadi. Gemini (Vertex) uning subtitr turi, tempi, rangi, zoomi va b-roll zichligini o'rganadi, profil saqlanadi va yangi videolarga qo'llanadi. Vertex ulanmagan bo'lsa faqat temp (pauza, kadr almashish) o'lchanadi.
- **Tuzilishi:** chapda kutubxona (Media, Audio, Matn, Stiker, Effekt, Filtr, AI uslub), o'rtada pleyer, o'ngda tanlangan element sozlamalari va agentlar paneli, pastda ko'p trekli taymlayn.
- **Taymlayn:** Bo'lish (S yoki Ctrl+B), chapini/o'ngini o'chirish (Q / W), Delete, oraliq kesish (B), Ctrl+Z. Kesilgan joylar qizil bo'lib ko'rinadi va istalgan payt tiklanadi.
- **Uch pog'onali subtitr:** so'zlar 3 qatorga bo'linadi, har qator o'z o'lchami va rangida, aytilayotgan so'z ajralib turadi. Preview eksport bilan bir xil ko'rinadi.
- **Stiker va matn animatsiyalari:** har bir qatlamga kirish (13 tur), chiqish (10 tur) va takroriy (10 tur) animatsiya birga qo'yiladi; pleyerda sichqoncha bilan suriladi. Emoji kutubxonasi (6 bo'lim) va o'z rasm/logongizni stiker qilib yuklash.
- **Erkin matn qatlamlari:** 6 ta tayyor ko'rinish, shrift, rang, kontur, fon, burilish, shaffoflik.
- **O'tishlar:** kesilgan joylarga zoom zarba, oq chaqnash, qoraga botish, xiralashish, glitch, silkinish.
- **Zoom zarbalari:** qo'lda yoki «Avto zoom» (har ikkinchi bo'lak yaqinlashadi).
- **Ovoz effektlari:** o'tishda whoosh, stiker/matnda pop (serverda sintez qilinadi), musiqaga fade.
- **Tezlik:** har bir video bo'lagiga alohida 0.25×–4× tezlik (ovoz balandligi buzilmaydi).
- **Surilish va aylanish o'tishlari:** chapga/o'ngga/tepaga/pastga surish, aylanish; har bir kesimga alohida o'tish tanlash mumkin.
- **Video va to'liq ekranli b-roll:** rasm yoki video qatlam, «To'liq ekran» rejimida asosiy video o'rnini egallaydi, subtitr ustida qoladi.
- **Keyframe:** stiker va matn vaqt davomida harakatlanadi (joy, masshtab, burilish, shaffoflik). Harakatli GIF stikerlar.
- **Ovoz effektlari:** qo'lda whoosh/pop/ding/boom/click qo'yish, pleyerda ham eshitiladi.
- **Bir nechta klip:** «Klip qo'shish» yana bir videoni loyiha oxiriga ulaydi.
- **Formatlar:** 9:16, 1:1, 4:5, 16:9. **Kadr:** kattalashtirish, joyini surish, oynadek aks, 180° ag'darish.
- **Teskari o'ynatish:** tanlangan bo'lak uchun (eksportda).
- **Rang:** soyalar, yorug' joylar, vinyetka, aniqlik; 16 ta tayyor filtr.
- **Matn effektlari:** soya va porlash.
- **Audio kliplar:** qo'shimcha audio fayl yuklash va mikrofondan ovoz yozish, alohida trekda.
- Barcha o'zgarishlar avtomatik saqlanadi (`studio.json`).

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
