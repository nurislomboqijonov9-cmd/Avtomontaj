# -*- coding: utf-8 -*-
"""
MONTAJ STUDIO — veb backend (FastAPI).
Bosqichlar (job asosida, progress bilan):
  /api/upload -> /api/analyze -> /api/cut -> /api/broll_* -> /api/render
Tarix: tayyor videolar serverda saqlanadi.
"""
import os, json, uuid, time, shutil, threading, traceback, hashlib, secrets, hmac
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import montaj

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
PROJ = os.path.join(DATA, "projects")
os.makedirs(PROJ, exist_ok=True)

app = FastAPI(title="VIZEN")

# ================= AKKAUNT TIZIMI =================
USERS_F = os.path.join(DATA, "users.json")
SESS_F  = os.path.join(DATA, "sessions.json")
_lock = threading.Lock()
def _load(p, d):
    try: return json.load(open(p, encoding="utf-8"))
    except: return d
def _save(p, o):
    tmp=p+".tmp"; json.dump(o, open(tmp,"w",encoding="utf-8"), ensure_ascii=False); os.replace(tmp,p)
def users(): return _load(USERS_F, {})
def sessions(): return _load(SESS_F, {})
def _hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 120000).hex()
def user_public(u): return {"id":u["id"],"email":u["email"],"name":u.get("name",""),"plan":u.get("plan","free")}

def user_from_token(token):
    if not token: return None
    s=sessions().get(token)
    if not s: return None
    u=users().get(s.get("email",""))
    return u
def check_auth(token):
    """Endi: haqiqiy foydalanuvchi tokeni. Yo'q bo'lsa 401. Foydalanuvchini qaytaradi."""
    u=user_from_token(token)
    if not u: raise HTTPException(401, "Kirish kerak")
    return u

app.mount_data = DATA  # eslatma

# ---------- job menejeri ----------
JOBS = {}   # job_id -> {status, progress, step, result, error}
async def _peek(req):
    """So'rov tanasidan loyiha ID sini o'qiydi (egalikni tekshirish uchun)."""
    try: return str((await req.json()).get("project") or "")
    except Exception: return ""
def new_job():
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"status": "running", "progress": 0, "step": "", "result": None, "error": None}
    return jid
def run_job(jid, fn):
    def worker():
        try:
            JOBS[jid]["result"] = fn(lambda p, s="": JOBS[jid].update(progress=p, step=s))
            JOBS[jid]["status"] = "done"; JOBS[jid]["progress"] = 100
        except Exception as e:
            JOBS[jid]["status"] = "error"
            JOBS[jid]["error"] = str(e)[:400]
            traceback.print_exc()
    threading.Thread(target=worker, daemon=True).start()

# ---------- yordamchi ----------
def pdir(pid):
    if not _re.match(r"^[a-f0-9]{12}$", str(pid or "")): raise HTTPException(404, "loyiha topilmadi")   # "../" kabi yo'llarga yo'l yo'q
    d = os.path.join(PROJ, pid)
    if not os.path.isdir(d): raise HTTPException(404, "loyiha topilmadi")
    return d
def meta_path(pid): return os.path.join(pdir(pid), "meta.json")
def load_meta(pid):
    p = os.path.join(PROJ, pid, "meta.json")
    if not os.path.exists(p): return {}
    return json.load(open(p, encoding="utf-8"))
def save_meta(pid, m):
    json.dump(m, open(os.path.join(PROJ, pid, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False)

# ---------- statik ----------
STATIC = os.path.join(HERE, "static") if os.path.isdir(os.path.join(HERE, "static")) else HERE
# check_dir=False -> papka bo'lmasa ham server YIQILMAYDI (faqat 404 beradi)
app.mount("/media", StaticFiles(directory=PROJ, check_dir=False), name="media")
app.mount("/fonts", StaticFiles(directory=montaj.FONTS_DIR, check_dir=False), name="fonts")
app.mount("/assets", StaticFiles(directory=STATIC, check_dir=False), name="assets")

# ---------- USLUB VIDEO-MISOLLARI (serverda generatsiya, keshlab) ----------
import re as _re
PREVIEW_VER = "v3"   # effekt/fon o'zgarsa bump qiling -> keshdan qayta chiziladi
PREV_DIR = os.path.join(DATA, "previews", PREVIEW_VER); os.makedirs(PREV_DIR, exist_ok=True)
_prev_locks = {}
def _prev_lock(name):
    with _lock:
        lk = _prev_locks.get(name)
        if lk is None:
            lk = threading.Lock(); _prev_locks[name] = lk
        return lk

@app.get("/preview/{fname}")
def preview_clip(fname: str):
    if not _re.match(r"^[ms]_[a-z0-9_]+\.mp4$", fname):
        raise HTTPException(404, "topilmadi")
    path = os.path.join(PREV_DIR, fname)
    if not (os.path.exists(path) and os.path.getsize(path) > 1000):
        import previews
        with _prev_lock(fname):
            if not (os.path.exists(path) and os.path.getsize(path) > 1000):
                out = previews.build_preview(fname, PREV_DIR)
                if not out or not os.path.exists(out):
                    raise HTTPException(404, "misol yo'q")
    return FileResponse(path, media_type="video/mp4")

@app.get("/", response_class=HTMLResponse)
def home():
    """Asosiy sahifa — yangi Studio. Eski interfeys /classic da."""
    p = os.path.join(HERE, "studio.html")
    if os.path.exists(p): return open(p, encoding="utf-8").read()
    return index()

@app.get("/classic", response_class=HTMLResponse)
def index():
    # index.html qayerda bo'lsa ham topamiz (static/ ichida yoki asosiy papkada)
    for p in [os.path.join(STATIC, "index.html"),
              os.path.join(HERE, "index.html"),
              os.path.join(HERE, "static", "index.html")]:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
    return HTMLResponse("<h2 style='font-family:sans-serif'>index.html topilmadi.</h2>"
        "<p style='font-family:sans-serif'>index.html faylini GitHub repo'ga yuklang "
        "(static/ papkasiga yoki asosiy papkaga). Server ishlayapti, faqat interfeys fayli yetishmayapti.</p>")

@app.get("/api/config")
def config():
    return {"vertex": montaj.have_vertex()}

# ---------- AUTH ----------
def _valid_email(e): return "@" in e and "." in e.split("@")[-1] and len(e) <= 120

@app.post("/api/auth/signup")
async def signup(req: Request):
    b = await req.json()
    email = (b.get("email") or "").strip().lower()
    pw = b.get("password") or ""
    name = (b.get("name") or "").strip()[:60]
    if not _valid_email(email): raise HTTPException(400, "Email noto'g'ri")
    if len(pw) < 6: raise HTTPException(400, "Parol kamida 6 belgi")
    with _lock:
        us = users()
        if email in us: raise HTTPException(409, "Bu email allaqachon ro'yxatdan o'tgan")
        salt = secrets.token_hex(16)
        u = {"id": uuid.uuid4().hex[:12], "email": email, "name": name,
             "salt": salt, "hash": _hash_pw(pw, salt), "created": int(time.time()), "plan": "free"}
        us[email] = u; _save(USERS_F, us)
        token = secrets.token_urlsafe(24)
        ss = sessions(); ss[token] = {"email": email, "created": int(time.time())}; _save(SESS_F, ss)
    return {"token": token, "user": user_public(u)}

@app.post("/api/auth/login")
async def auth_login(req: Request):
    b = await req.json()
    email = (b.get("email") or "").strip().lower()
    pw = b.get("password") or ""
    u = users().get(email)
    if not u or not hmac.compare_digest(u["hash"], _hash_pw(pw, u["salt"])):
        raise HTTPException(401, "Email yoki parol noto'g'ri")
    with _lock:
        token = secrets.token_urlsafe(24)
        ss = sessions(); ss[token] = {"email": email, "created": int(time.time())}; _save(SESS_F, ss)
    return {"token": token, "user": user_public(u)}

@app.get("/api/auth/me")
def auth_me(x_auth: str = Header("")):
    u = check_auth(x_auth)
    return {"user": user_public(u)}

@app.post("/api/auth/logout")
def auth_logout(x_auth: str = Header("")):
    with _lock:
        ss = sessions()
        if x_auth in ss: del ss[x_auth]; _save(SESS_F, ss)
    return {"ok": True}

# ---------- 1) YUKLASH ----------
@app.post("/api/upload")
async def upload(file: UploadFile = File(...), x_auth: str = Header("")):
    usr = check_auth(x_auth)
    pid = uuid.uuid4().hex[:12]
    d = os.path.join(PROJ, pid); os.makedirs(d, exist_ok=True)
    ext = os.path.splitext(file.filename or "in.mp4")[1].lower() or ".mp4"
    inp = os.path.join(d, "input" + ext)
    with open(inp, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            f.write(chunk)
    # BARQAROR kadr tezligiga keltiramiz (subtitr drift'ini yo'q qiladi)
    norm = os.path.join(d, "input_cfr.mp4")
    work_input = montaj.prepare_cfr(inp, norm)
    use = os.path.basename(work_input)
    dur = montaj.ffdur(work_input)
    meta = {"id": pid, "input": use, "orig": os.path.basename(inp), "name": file.filename or "video",
            "created": int(time.time()), "duration": round(dur, 2), "stage": "uploaded",
            "user_id": usr["id"]}
    save_meta(pid, meta)
    return {"project": pid, "video_url": f"/media/{pid}/{use}",
            "duration": meta["duration"], "name": meta["name"]}

# ---------- MUSIQA yuklash ----------
@app.post("/api/music")
async def music_upload(file: UploadFile = File(...), project: str = Form(...), x_auth: str = Header("")):
    _own(project, check_auth(x_auth))
    d = pdir(project); meta = load_meta(project)
    ext = os.path.splitext(file.filename or "m.mp3")[1].lower() or ".mp3"
    if ext not in (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"): ext = ".mp3"
    mp = os.path.join(d, "music" + ext)
    with open(mp, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            f.write(chunk)
    meta["music"] = os.path.basename(mp); save_meta(project, meta)
    return {"ok": True, "name": file.filename or "music", "url": f"/media/{project}/{os.path.basename(mp)}"}

# ---------- 2) TAHLIL ----------
@app.post("/api/analyze")
async def analyze(req: Request, x_auth: str = Header("")):
    _own((await _peek(req)), check_auth(x_auth))
    body = await req.json(); pid = body["project"]
    d = pdir(pid); meta = load_meta(pid)
    inp = os.path.join(d, meta["input"])
    lang = (body.get("lang") or meta.get("lang") or "uz").lower()
    meta["lang"] = lang; save_meta(pid, meta)
    jid = new_job()
    def fn(prog):
        prog(10, "Transkripsiya...")
        res = montaj.analyze(inp, d, lang)
        prog(90, "Kesish takliflari...")
        meta.update(words=res["words"], full_text=res["full_text"],
                    engine=res["engine"], duration=res["duration"],
                    silence_removes=res["silence_removes"], dup_removes=res["dup_removes"],
                    stage="analyzed")
        save_meta(pid, meta)
        return res
    run_job(jid, fn)
    return {"job": jid}

# ---------- 3) KESISH ----------
@app.post("/api/cut")
async def cut(req: Request, x_auth: str = Header("")):
    _own((await _peek(req)), check_auth(x_auth))
    body = await req.json(); pid = body["project"]
    d = pdir(pid); meta = load_meta(pid)
    inp = os.path.join(d, meta["input"])
    removes = body.get("removes", [])
    words = body.get("words") or meta.get("words", [])
    dur = meta.get("duration", montaj.ffdur(inp))
    jid = new_job()
    def fn(prog):
        prog(20, "Videoni kesyapman...")
        cutp, w2, cdur = montaj.apply_cut(inp, words, removes, d, dur)
        meta.update(cut=os.path.basename(cutp), cut_words=w2, cut_duration=cdur,
                    removes=removes, stage="cut")
        save_meta(pid, meta)
        return {"cut_url": f"/media/{pid}/{os.path.basename(cutp)}", "words": w2, "duration": cdur}
    run_job(jid, fn)
    return {"job": jid}

# ---------- 4a) B-ROLL TAKLIF ----------
@app.post("/api/broll_suggest")
async def broll_suggest(req: Request, x_auth: str = Header("")):
    _own((await _peek(req)), check_auth(x_auth))
    body = await req.json(); pid = body["project"]; meta = load_meta(pid)
    n = int(body.get("n", 4))
    dur = meta.get("cut_duration") or meta.get("duration", 0)
    full = meta.get("full_text", "")
    jid = new_job()
    def fn(prog):
        prog(30, "G'oyalar tuzilyapti...")
        return {"items": montaj.broll_suggest(full, dur, n)}
    run_job(jid, fn)
    return {"job": jid}

# ---------- 4b) B-ROLL RASM (bitta) ----------
@app.post("/api/broll_image")
async def broll_image(req: Request, x_auth: str = Header("")):
    _own((await _peek(req)), check_auth(x_auth))
    body = await req.json(); pid = body["project"]; d = pdir(pid)
    prompt = (body.get("prompt") or "").strip()
    if not prompt: raise HTTPException(400, "prompt bo'sh")
    bid = uuid.uuid4().hex[:8]; fn_name = f"broll_{bid}.png"
    dest = os.path.join(d, fn_name)
    jid = new_job()
    def fn(prog):
        prog(20, "Rasm chizilyapti (Gemini)...")
        montaj.gen_image(prompt, dest)
        return {"image": fn_name, "image_url": f"/media/{pid}/{fn_name}", "prompt": prompt}
    run_job(jid, fn)
    return {"job": jid}

# ---------- 5) RENDER ----------
@app.post("/api/render")
async def render(req: Request, x_auth: str = Header("")):
    _own((await _peek(req)), check_auth(x_auth))
    body = await req.json(); pid = body["project"]; d = pdir(pid); meta = load_meta(pid)
    cutname = meta.get("cut") or meta.get("input")
    cut = os.path.join(d, cutname)
    words = body.get("words") or meta.get("cut_words") or meta.get("words", [])
    sub = body.get("subtitle", {})
    zoom = bool(body.get("zoom", True)); audio = bool(body.get("audio_clean", True))
    subtitle_on = bool(body.get("subtitle_on", True))
    grade = body.get("grade", "vivid"); zoom_level = int(body.get("zoom_level", 1))
    quality = str(body.get("quality", "1080")); fx = str(body.get("fx", "none"))
    lang = (body.get("lang") or meta.get("lang") or "uz").lower()
    brolls_in = body.get("brolls", [])
    removes = [list(r) for r in (body.get("removes") or []) if r and len(r) == 2]
    segments = body.get("segments")                 # Studio: [{a,b,v}] — tezlik o'zgargan bo'lsa (so'zlar allaqachon chiqish vaqtida)
    do_stickers = bool(body.get("stickers", True)); do_hook = bool(body.get("hook", True))
    sticker_items = body.get("sticker_items")      # brauzer rejasi (emoji belgisi bilan) yoki None
    hook_override = body.get("hook_text")           # brauzerdan tahrirlangan hook matni
    use_music = bool(body.get("music", True)); music_vol = float(body.get("music_vol", 0.28))
    orig_vol = float(body.get("orig_vol", 1.0))
    RAT = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080), "4:5": (1080, 1350)}
    res_w, res_h = RAT.get(str(body.get("ratio") or "9:16"), (1080, 1920))
    qs_ = {"480": 480, "540": 540, "720": 720}.get(quality.lower(), 1080) / 1080.0
    out_w, out_h = int(res_w * qs_) // 2 * 2, int(res_h * qs_) // 2 * 2
    frame_in = body.get("frame") if isinstance(body.get("frame"), dict) else {}
    audios_in = body.get("audios") or []
    layers_in = body.get("layers")                  # Studio: animatsiyali stiker/rasm/matn qatlamlari
    transitions = body.get("transitions") or []; zooms_in = body.get("zooms") or []
    sfx_in = body.get("sfx") or []; music_fade = bool(body.get("music_fade", False))
    music_path = None
    if use_music and meta.get("music"):
        mp = os.path.join(d, meta["music"])
        if os.path.exists(mp): music_path = mp
    jid = new_job()
    def fn(prog):
        nonlocal cut, words
        if isinstance(segments, list) and segments:
            prog(10, "Bo'laklar kesilib, tezligi o'zgartirilyapti...")
            segs = [(float(x["a"]), float(x["b"]), float(x.get("v", 1)), bool(x.get("rev"))) for x in segments if float(x["b"]) - float(x["a"]) > 0.05][:200]
            tmp = os.path.join(d, "render_src.mp4")
            if not montaj.cut_video_speed(cut, tmp, segs, d): raise RuntimeError("bo'laklarni kesishda xato")
            cut = tmp
        # qo'lda belgilangan kesishlarni SHU render ichida qo'llaymiz (alohida kutish yo'q)
        elif removes:
            prog(10, "Kesilgan joylar olib tashlanyapti...")
            cdur = meta.get("cut_duration") or montaj.ffdur(cut)
            keep = montaj.subtract_ranges([(0.0, float(cdur))], [tuple(r) for r in removes])
            keep = [(a, b) for a, b in keep if b - a > 0.1] or [(0.0, float(cdur))]
            tmp = os.path.join(d, "render_src.mp4")
            montaj.cut_video(cut, tmp, keep)
            if os.path.exists(tmp):
                words = montaj.remap_words(words, keep)   # bitta bo'lak qolsa ham vaqt siljiydi
                cut = tmp
        prog(15, "Subtitr tayyorlanyapti...")
        ass = os.path.join(d, "subs.ass")
        if hook_override is not None:
            hooktext = (hook_override or "").strip() or None
        else:
            hooktext = montaj.hook_text(meta.get("full_text", ""), lang) if do_hook else None
        if subtitle_on and words:
            open(ass, "w", encoding="utf-8").write(montaj.build_ass(words, sub, lang, hook=hooktext, res=(res_w, res_h)))
        else:
            open(ass, "w", encoding="utf-8").write(montaj.build_ass([], sub, lang, hook=hooktext, res=(res_w, res_h)))
        anim_layers = []
        if isinstance(layers_in, list):
            # STUDIO QATLAMLARI: har biri In/Out/Loop animatsiyali PNG kadrlarga aylanadi
            from PIL import Image
            tw_, th_ = out_w, out_h
            ldir = os.path.join(d, "layers"); shutil.rmtree(ldir, ignore_errors=True); os.makedirs(ldir, exist_ok=True)
            for i, L in enumerate(layers_in[:40]):
                try:
                    kind = L.get("kind"); ldur = max(0.2, min(30.0, float(L.get("dur", 1.4)))); img = None
                    if kind == "emoji":
                        emo = (L.get("emoji") or "").strip()
                        if not emo: continue
                        pth = os.path.join(ldir, f"src{i}.png"); px = max(24, int(tw_ * float(L.get("size", 0.16))))
                        if montaj._emoji_png(emo, pth, px=px): img = Image.open(pth).convert("RGBA")
                    elif kind == "image":
                        pth = os.path.join(d, os.path.basename(str(L.get("image") or "")))
                        if os.path.isfile(pth):
                            im0 = Image.open(pth); wpx = max(24, int(tw_ * float(L.get("size", 0.3))))
                            hpx = max(2, int(im0.size[1] * wpx / im0.size[0]))
                            if getattr(im0, "is_animated", False) and getattr(im0, "n_frames", 1) > 1:   # harakatli GIF
                                img = []; tacc = 0.0
                                for fi in range(min(im0.n_frames, 120)):
                                    im0.seek(fi); tacc += max(0.02, (im0.info.get("duration") or 80) / 1000.0)
                                    img.append((im0.convert("RGBA").resize((wpx, hpx), Image.BILINEAR), tacc))
                            else:
                                img = im0.convert("RGBA").resize((wpx, hpx), Image.LANCZOS)
                    elif kind == "text":
                        img = montaj.text_png(L, min(tw_, th_) / 1080.0, lang)
                    if img is None: continue
                    fdir = os.path.join(ldir, f"L{i}")
                    haskf = bool(L.get("keys"))
                    n, cw, ch = montaj.layer_frames(img, fdir, ldur, L, frame=(tw_, th_))
                    anim_layers.append({"dir": fdir, "time": max(0.0, float(L.get("time", 0))), "dur": n / 30.0,
                                        "cx": tw_ / 2 if haskf else tw_ * float(L.get("x", 0.5)),
                                        "cy": th_ / 2 if haskf else th_ * float(L.get("y", 0.4)), "cw": cw, "ch": ch})
                except Exception:
                    continue
            stickers = []
        elif isinstance(sticker_items, list):
            stickers = []
            for it in sticker_items:
                emo = (it.get("emoji") or "").strip()
                if not emo: continue
                code = "_".join(f"{ord(c):x}" for c in emo)
                pth = os.path.join(d, f"emo_{code}.png")
                if not os.path.exists(pth): montaj._emoji_png(emo, pth)
                if os.path.exists(pth):
                    stickers.append({"path": pth, "time": float(it.get("time", 0)), "dur": float(it.get("dur", 1.4)),
                                     "x": float(it.get("x", 0.5)), "y": float(it.get("y", 0.4)),
                                     "size": float(it.get("size", 0.15)), "anim": it.get("anim", "pop")})
        elif do_stickers:
            stickers = montaj.emoji_plan(words, d, n=8)
        else:
            stickers = []
        brolls = []
        for b in brolls_in:
            if not b.get("on", True): continue
            img = os.path.basename(str(b.get("image") or ""))   # faqat shu loyiha papkasidagi fayl
            if not img: continue
            p = os.path.join(d, img)
            if os.path.exists(p):
                brolls.append({"path": p, "time": float(b.get("time", 0)),
                               "dur": float(b.get("dur", 2.5)), "y": float(b.get("y", 0.72)),
                               "w": float(b.get("w", 0.78)), "full": bool(b.get("full"))})
        prog(45, "Video render qilinyapti (1-3 daqiqa)...")
        out = os.path.join(d, "final.mp4")
        sfx = []
        for x in sfx_in[:40]:
            sp = montaj.sfx_file(str(x.get("type")), os.path.join(DATA, "sfx"))
            if sp: sfx.append({"path": sp, "t": float(x.get("t", 0)), "vol": float(x.get("vol", 0.5))})
        for x in audios_in[:20]:      # qo'shimcha audio kliplar (ovoz yozuvi, yuklangan fayl)
            ap = os.path.join(d, os.path.basename(str(x.get("file") or "")))
            if os.path.isfile(ap): sfx.append({"path": ap, "t": float(x.get("t", 0)), "vol": float(x.get("vol", 1)), "dur": float(x.get("dur") or 0) or None})
        zooms = [{"a": float(z.get("a", 0)), "b": float(z.get("b", 0)), "scale": float(z.get("scale", 1.15)), "ramp": float(z.get("ramp", 0.12))} for z in zooms_in if isinstance(z, dict)]
        ok = montaj.render_final(cut, ass, out, brolls, zoom=zoom, audio_clean=audio, grade=grade, zoom_level=zoom_level, quality=quality, fx=fx, stickers=stickers, music=music_path, music_vol=music_vol, orig_vol=orig_vol,
                                 anim_layers=anim_layers, transitions=[t for t in transitions if isinstance(t, dict)], zooms=zooms, sfx=sfx, music_fade=music_fade,
                                 size=(out_w, out_h), frame=frame_in)
        shutil.rmtree(os.path.join(d, "layers"), ignore_errors=True)
        if not ok: raise RuntimeError("render xatosi")
        meta.update(final="final.mp4", stage="done", finished=int(time.time()),
                    cut_words=words, brolls=brolls_in,
                    render_settings={"subtitle": sub, "zoom": zoom, "audio_clean": audio,
                                     "subtitle_on": subtitle_on, "broll_y": (brolls_in[0]["y"] if brolls_in else 0.72),
                                     "grade": grade, "zoom_level": zoom_level, "quality": quality, "fx": fx,
                                     "stickers": do_stickers, "hook": do_hook})
        save_meta(pid, meta)
        return {"final_url": f"/media/{pid}/final.mp4"}
    run_job(jid, fn)
    return {"job": jid}

# ---------- USLUB NAMUNASI (foydalanuvchi videosida qisqa klip) ----------
@app.post("/api/preview")
async def preview(req: Request, x_auth: str = Header("")):
    check_auth(x_auth)
    import hashlib, json as _json
    body = await req.json(); pid = body["project"]; d = pdir(pid); meta = load_meta(pid)
    srcname = meta.get("cut") or meta.get("input")
    if not srcname:
        raise HTTPException(400, "video yo'q")
    src = os.path.join(d, srcname)
    words = meta.get("cut_words") or meta.get("words", [])
    sub = body.get("subtitle", {}) or {}
    grade = body.get("grade", "vivid"); zoom_level = int(body.get("zoom_level", 1))
    fx = str(body.get("fx", "none"))
    lang = (body.get("lang") or meta.get("lang") or "uz").lower()
    key = hashlib.md5(_json.dumps({"s": sub, "g": grade, "z": zoom_level, "fx": fx, "src": srcname},
                                  sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]
    outname = f"prev_{key}.mp4"; out = os.path.join(d, outname)
    jid = new_job()
    def fn(prog):
        prog(25, "Namuna tayyorlanyapti...")
        if not os.path.exists(out):
            ok = montaj.render_preview(src, words, sub, out, grade=grade,
                                       zoom_level=zoom_level, lang=lang, workdir=d, fx=fx)
            if not ok:
                raise RuntimeError("preview xato")
        return {"url": f"/media/{pid}/{outname}"}
    run_job(jid, fn)
    return {"job": jid}

# ---------- FILMSTRIP (timeline uchun haqiqiy kadrlar) ----------
@app.post("/api/filmstrip")
async def filmstrip_ep(req: Request, x_auth: str = Header("")):
    check_auth(x_auth)
    import hashlib
    body = await req.json(); pid = body["project"]; d = pdir(pid); meta = load_meta(pid)
    srcname = meta.get("cut") or meta.get("input")
    if not srcname:
        raise HTTPException(400, "video yo'q")
    src = os.path.join(d, srcname)
    key = hashlib.md5(srcname.encode("utf-8")).hexdigest()[:8]
    outname = f"strip_{key}.jpg"; out = os.path.join(d, outname)
    n = meta.get("strip_n_" + key, 0)
    if not (os.path.exists(out) and os.path.getsize(out) > 500 and n):
        n = montaj.filmstrip(src, out)
        if not n:
            raise HTTPException(500, "strip xato")
        meta["strip_n_" + key] = n; save_meta(pid, meta)
    return {"url": f"/media/{pid}/{outname}", "n": n}

# ---------- AVTOMATIK (hammasi bir tugmada) ----------
@app.post("/api/auto")
async def auto(req: Request, x_auth: str = Header("")):
    _own((await _peek(req)), check_auth(x_auth))
    body = await req.json(); pid = body["project"]
    d = pdir(pid); meta = load_meta(pid)
    inp = os.path.join(d, meta["input"])
    n = int(body.get("broll_n", 4))
    zoom = bool(body.get("zoom", True)); audio = bool(body.get("audio_clean", True))
    broll_y = float(body.get("broll_y", 0.72)); do_broll = bool(body.get("broll", True))
    do_cut = bool(body.get("cut", True))               # kesish ixtiyoriy
    subtitle_on = bool(body.get("subtitle_on", True))  # subtitr ixtiyoriy
    grade = body.get("grade", "vivid"); zoom_level = int(body.get("zoom_level", 1))
    quality = str(body.get("quality", "1080")); fx = str(body.get("fx", "none"))
    do_stickers = bool(body.get("stickers", True)); do_hook = bool(body.get("hook", True))
    use_music = bool(body.get("music", True)); music_vol = float(body.get("music_vol", 0.28)); orig_vol = float(body.get("orig_vol", 1.0))
    music_path = None
    if use_music and meta.get("music"):
        _mp = os.path.join(d, meta["music"])
        if os.path.exists(_mp): music_path = _mp
    lang = (body.get("lang") or meta.get("lang") or "uz").lower()
    meta["lang"] = lang
    sub = body.get("subtitle") or {"delay": 0.0, "margin_v": 660, "size": 90, "words": 3,
        "active": "#ffea00", "base": "#ffffff", "upper": True, "font": "Anton",
        "outline": "#000000", "border": 4}
    jid = new_job()
    def fn(prog):
        prog(8, "Transkripsiya...")
        res = montaj.analyze(inp, d, lang)
        words = res["words"]; full = res["full_text"]; dur = res["duration"]
        removes = [list(r) for r in res["silence_removes"]] + [list(r) for r in res["dup_removes"]]
        meta.update(words=words, full_text=full, engine=res["engine"], duration=dur,
                    silence_removes=res["silence_removes"], dup_removes=res["dup_removes"])
        if do_cut:
            prog(35, "Keraksiz joylar kesilyapti...")
            cut, w2, cdur = montaj.apply_cut(inp, words, removes, d, dur)
        else:
            prog(35, "Kesishsiz...")
            cut, w2, cdur = inp, words, dur
            removes = []
        meta.update(cut=os.path.basename(cut), cut_words=w2, cut_duration=cdur, removes=removes)
        save_meta(pid, meta)
        outb = []; bnote = ""
        if do_broll:
            prog(55, "Animatsiya rasmlarini chizyapti...")
            try:
                plan = montaj.broll_suggest(full, cdur, n)
            except Exception as e:
                plan = []; bnote = f"g'oya xato: {str(e)[:90]}"
            for i, it in enumerate(plan):
                try:
                    nm = f"broll_a{i}.png"; dest = os.path.join(d, nm)
                    montaj.gen_image(it["prompt"], dest)
                    dd = 2.5; t = float(it["at"])
                    outb.append({"image": nm, "image_url": f"/media/{pid}/{nm}", "prompt": it["prompt"],
                                 "time": round(max(0, min(cdur - dd, t - dd / 2)), 2), "dur": dd, "on": True})
                except Exception as e:
                    bnote = str(e)[:130]; break
            if not bnote: bnote = f"{len(outb)} ta"
        prog(70, "Video render qilinyapti (1-3 daqiqa)...")
        ass = os.path.join(d, "subs.ass")
        hooktext = montaj.hook_text(full, lang) if do_hook else None
        open(ass, "w", encoding="utf-8").write(montaj.build_ass(w2 if subtitle_on else [], sub, lang, hook=hooktext))
        stickers = montaj.emoji_plan(w2, d, n=8) if do_stickers else []
        rbrolls = [{"path": os.path.join(d, b["image"]), "time": b["time"], "dur": b["dur"],
                    "y": broll_y, "w": 0.92} for b in outb]
        out = os.path.join(d, "final.mp4")
        if not montaj.render_final(cut, ass, out, rbrolls, zoom=zoom, audio_clean=audio, grade=grade, zoom_level=zoom_level, quality=quality, fx=fx, stickers=stickers, music=music_path, music_vol=music_vol, orig_vol=orig_vol):
            raise RuntimeError("render xato")
        meta.update(final="final.mp4", stage="done", finished=int(time.time()), brolls=outb,
                    render_settings={"subtitle": sub, "zoom": zoom, "audio_clean": audio,
                                     "subtitle_on": subtitle_on, "broll_y": broll_y,
                                     "grade": grade, "zoom_level": zoom_level, "quality": quality, "fx": fx,
                                     "stickers": do_stickers, "hook": do_hook})
        save_meta(pid, meta)
        return {"final_url": f"/media/{pid}/final.mp4", "cut_url": f"/media/{pid}/{os.path.basename(cut)}",
                "words": w2, "duration": cdur, "brolls": outb, "removes": removes,
                "engine": res["engine"], "broll_note": bnote}
    run_job(jid, fn)
    return {"job": jid}

# ---------- NAMUNA USLUB ----------
@app.post("/api/reference")
async def reference(file: UploadFile = File(...), x_auth: str = Header("")):
    check_auth(x_auth)
    import tempfile
    tmp = tempfile.mkdtemp()
    ext = os.path.splitext(file.filename or "ref.mp4")[1].lower() or ".mp4"
    inp = os.path.join(tmp, "ref" + ext)
    with open(inp, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            f.write(chunk)
    jid = new_job()
    def fn(prog):
        prog(30, "Namuna tempi o'lchanyapti...")
        try:
            return montaj.analyze_reference(inp, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    run_job(jid, fn)
    return {"job": jid}

# ---------- JOB holati ----------
@app.get("/api/job/{jid}")
def job(jid: str):
    j = JOBS.get(jid)
    if not j: raise HTTPException(404, "job topilmadi")
    return j

# ---------- TARIX ----------
@app.get("/api/history")
def history(x_auth: str = Header("")):
    usr = check_auth(x_auth)
    items = []
    for pid in os.listdir(PROJ):
        m = load_meta(pid)
        if m.get("final") and m.get("user_id") == usr["id"]:
            items.append({"project": pid, "name": m.get("name", "video"),
                          "finished": m.get("finished", m.get("created", 0)),
                          "final_url": f"/media/{pid}/final.mp4",
                          "duration": m.get("cut_duration") or m.get("duration", 0)})
    items.sort(key=lambda x: x["finished"], reverse=True)
    return {"items": items}

@app.get("/api/project/{pid}")
def project(pid: str, x_auth: str = Header("")):
    check_auth(x_auth)
    return load_meta(pid)

@app.get("/api/resume/{pid}")
def resume(pid: str, x_auth: str = Header("")):
    """Tarixdagi loyihani tahrirda davom ettirish uchun to'liq holat."""
    usr = check_auth(x_auth)
    m = load_meta(pid)
    if not m: raise HTTPException(404, "loyiha topilmadi")
    if m.get("user_id") != usr["id"]: raise HTTPException(403, "ruxsat yo'q")
    rs = m.get("render_settings", {})
    cutname = m.get("cut") or m.get("input")
    words = m.get("cut_words") or m.get("words", [])
    dur = m.get("cut_duration") or m.get("duration", 0)
    brolls = []
    for b in m.get("brolls", []):
        img = b.get("image")
        if img and os.path.exists(os.path.join(PROJ, pid, img)):
            brolls.append({"image": img, "image_url": f"/media/{pid}/{img}",
                           "prompt": b.get("prompt", ""), "time": b.get("time", 0),
                           "dur": b.get("dur", 2.5), "on": b.get("on", True)})
    return {"project": pid, "name": m.get("name", "video"), "lang": m.get("lang", "uz"),
            "cut_url": f"/media/{pid}/{cutname}", "final_url": (f"/media/{pid}/final.mp4" if m.get("final") else None),
            "words": words, "duration": dur, "removes": m.get("removes", []),
            "brolls": brolls, "engine": m.get("engine", ""),
            "subtitle": rs.get("subtitle", {}), "subtitle_on": rs.get("subtitle_on", True),
            "zoom": rs.get("zoom", True), "audio_clean": rs.get("audio_clean", True),
            "broll_y": rs.get("broll_y", 0.72), "grade": rs.get("grade","vivid"), "zoom_level": rs.get("zoom_level",1),
            "fx": rs.get("fx","none"), "quality": rs.get("quality","1080")}

# ================= STUDIO (yangi professional interfeys) =================
_PID_RE = _re.compile(r"^[a-f0-9]{12}$")
def _own(pid, usr):
    """Loyiha shu foydalanuvchiniki ekanini tekshiradi, meta qaytaradi."""
    if not _PID_RE.match(pid or ""): raise HTTPException(404, "loyiha topilmadi")
    m = load_meta(pid)
    if not m: raise HTTPException(404, "loyiha topilmadi")
    if m.get("user_id") and m.get("user_id") != usr["id"]: raise HTTPException(403, "ruxsat yo'q")
    return m

@app.get("/studio", response_class=HTMLResponse)
def studio_page():
    p = os.path.join(HERE, "studio.html")
    if os.path.exists(p): return open(p, encoding="utf-8").read()
    return HTMLResponse("<h2 style='font-family:sans-serif'>studio.html topilmadi.</h2>", status_code=404)

_FONT_CACHE = {"t": 0, "items": []}
@app.get("/api/fonts")
def fonts_list():
    """Serverda bor shriftlar (subtitr renderi aynan shularni ishlatadi)."""
    if _FONT_CACHE["items"] and time.time() - _FONT_CACHE["t"] < 300: return {"items": _FONT_CACHE["items"]}
    items = []
    try:
        from fontTools.ttLib import TTFont
        for fn in sorted(os.listdir(montaj.FONTS_DIR)):
            if not fn.lower().endswith((".ttf", ".otf")): continue
            try:
                f = TTFont(os.path.join(montaj.FONTS_DIR, fn), lazy=True, fontNumber=0)
                fam = f["name"].getBestFamilyName()
                # libass shrift o'lchamini (ascent+descent) bo'yicha hisoblaydi -> preview shu nisbatda kichraytiradi
                try:
                    os2 = f["OS/2"]; cell = (os2.usWinAscent + os2.usWinDescent) or (f["hhea"].ascent - f["hhea"].descent)
                    scale = round(f["head"].unitsPerEm / float(cell), 4)
                except Exception:
                    scale = 0.8
                f.close()
                if fam and not any(x["family"] == fam for x in items):
                    items.append({"family": fam, "file": fn, "cyr": fam in montaj.FONT_CYR, "scale": scale})
            except Exception:
                continue
    except Exception:
        pass
    _FONT_CACHE.update(t=time.time(), items=items)
    return {"items": items}

@app.get("/api/studio/projects")
def studio_projects(x_auth: str = Header("")):
    usr = check_auth(x_auth); items = []; made = 0
    for pid in os.listdir(PROJ):
        if not _PID_RE.match(pid): continue
        m = load_meta(pid)
        if m.get("user_id") != usr["id"]: continue
        tp = os.path.join(PROJ, pid, "thumb.jpg")
        if not os.path.exists(tp) and made < 8 and (m.get("cut") or m.get("input")):   # muqova kadri (birinchi so'rovda yasaladi)
            src = os.path.join(PROJ, pid, m.get("cut") or m.get("input")); made += 1
            if os.path.exists(src):
                montaj.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "0.8", "-i", src, "-frames:v", "1",
                            "-vf", "scale=-2:360", "-q:v", "5", tp])
        fin = os.path.join(PROJ, pid, "final.mp4")
        if m.get("final") and os.path.exists(fin) and m.get("final_ts") != int(os.path.getmtime(fin)):   # tayyor video uzunligi (kesh)
            m["final_duration"] = round(montaj.ffdur(fin), 2); m["final_ts"] = int(os.path.getmtime(fin)); save_meta(pid, m)
        sj = os.path.join(PROJ, pid, "studio.json")
        items.append({"project": pid, "name": m.get("name", "video"),
                      "source_duration": m.get("duration", 0), "final_duration": m.get("final_duration"),
                      "analyzed": bool(m.get("words")), "edited": os.path.exists(sj), "folder": m.get("folder", ""),
                      "updated": int(os.path.getmtime(sj)) if os.path.exists(sj) else (m.get("finished") or m.get("created", 0)),
                      "made": m.get("created", 0),
                      "thumb_url": (f"/media/{pid}/thumb.jpg" if os.path.exists(tp) else None),
                      "created": m.get("finished") or m.get("created", 0),
                      "duration": m.get("cut_duration") or m.get("duration", 0),
                      "final_url": (f"/media/{pid}/final.mp4" if m.get("final") else None)})
    items.sort(key=lambda x: x["updated"], reverse=True)
    return {"items": items[:200]}

@app.post("/api/studio/rename")
async def studio_rename(req: Request, x_auth: str = Header("")):
    usr = check_auth(x_auth); b = await req.json(); pid = b.get("project", ""); m = _own(pid, usr)
    name = str(b.get("name") or "").strip()[:80]
    if not name: raise HTTPException(400, "nom bo'sh")
    m["name"] = name; save_meta(pid, m)
    return {"ok": True, "name": name}

@app.post("/api/studio/move")
async def studio_move(req: Request, x_auth: str = Header("")):
    """Loyihani papkaga ko'chirish (bo'sh nom = papkasiz)."""
    usr = check_auth(x_auth); b = await req.json(); pid = b.get("project", ""); m = _own(pid, usr)
    m["folder"] = str(b.get("folder") or "").strip()[:40]; save_meta(pid, m)
    return {"ok": True, "folder": m["folder"]}

@app.post("/api/studio/plan")
async def studio_plan(req: Request, x_auth: str = Header("")):
    """Erkin matnli buyruqni montaj sozlamalariga aylantiradi (AI yoki kalit so'zlar)."""
    check_auth(x_auth); b = await req.json()
    plan, src = montaj.plan_from_prompt(str(b.get("prompt") or ""))
    return {"plan": plan, "source": src}

@app.post("/api/studio/styles/save")
async def style_save(req: Request, x_auth: str = Header("")):
    """Joriy loyihadagi ko'rinishni qayta ishlatiladigan uslub sifatida saqlash."""
    usr = check_auth(x_auth); b = await req.json(); pr = b.get("profile")
    name = str(b.get("name") or "").strip()[:40]
    if not name or not isinstance(pr, dict) or len(json.dumps(pr)) > 20000: raise HTTPException(400, "uslub noto'g'ri")
    pr = dict(pr); pr.update(id=uuid.uuid4().hex[:10], name=name, created=int(time.time()), source="saved")
    with _lock:
        allst = _load(STYLES_F, {}); lst = allst.get(usr["id"], [])
        lst.insert(0, pr); allst[usr["id"]] = lst[:30]; _save(STYLES_F, allst)
    return pr

@app.post("/api/studio/reorder")
async def studio_reorder(req: Request, x_auth: str = Header("")):
    """Video bo'laklarini yangi tartibda qayta yig'adi (kliplar o'rnini almashtirish)."""
    usr = check_auth(x_auth); b = await req.json(); pid = b.get("project", ""); meta = _own(pid, usr); d = os.path.join(PROJ, pid)
    pieces = [(float(x[0]), float(x[1]), 1.0) for x in (b.get("pieces") or []) if float(x[1]) - float(x[0]) > 0.03][:200]
    if len(pieces) < 2: raise HTTPException(400, "bo'laklar yetarli emas")
    jid = new_job()
    def fn(prog):
        prog(20, "Kliplar qayta tartiblanmoqda...")
        cur = os.path.join(d, meta.get("input")); outn = f"input_r{uuid.uuid4().hex[:6]}.mp4"; outp = os.path.join(d, outn)
        if not montaj.cut_video_speed(cur, outp, pieces, d): raise RuntimeError("qayta tartiblab bo'lmadi")
        m = load_meta(pid); m.update(input=outn, duration=round(montaj.ffdur(outp), 2)); m.pop("cut", None); m.pop("cut_duration", None); save_meta(pid, m)
        try: os.remove(os.path.join(d, "thumb.jpg"))
        except Exception: pass
        return {"video_url": f"/media/{pid}/{outn}", "duration": m["duration"]}
    run_job(jid, fn)
    return {"job": jid}

@app.post("/api/studio/beats")
async def studio_beats(req: Request, x_auth: str = Header("")):
    usr = check_auth(x_auth); b = await req.json(); pid = b.get("project", ""); meta = _own(pid, usr)
    if not meta.get("music"): raise HTTPException(400, "avval musiqa yuklang")
    mp = os.path.join(PROJ, pid, meta["music"])
    key = "beats_" + meta["music"] + str(int(os.path.getmtime(mp)))
    if meta.get("beats_key") != key:
        meta["beats"] = montaj.detect_beats(mp); meta["beats_key"] = key; save_meta(pid, meta)
    return meta["beats"]

@app.post("/api/studio/duplicate")
async def studio_duplicate(req: Request, x_auth: str = Header("")):
    """Loyihaning to'liq nusxasi (video, sozlamalar, qatlamlar)."""
    usr = check_auth(x_auth); b = await req.json(); pid = b.get("project", ""); m = _own(pid, usr)
    new = uuid.uuid4().hex[:12]
    shutil.copytree(os.path.join(PROJ, pid), os.path.join(PROJ, new))
    m2 = load_meta(new); m2.update(id=new, name=(m.get("name", "video") + " (nusxa)")[:80], created=int(time.time()))
    save_meta(new, m2)
    sj = os.path.join(PROJ, new, "studio.json")      # qatlam manzillaridagi eski loyiha ID sini almashtiramiz
    if os.path.exists(sj):
        try:
            t = open(sj, encoding="utf-8").read().replace(f"/media/{pid}/", f"/media/{new}/")
            open(sj, "w", encoding="utf-8").write(t)
        except Exception: pass
    return {"project": new}

@app.get("/api/studio/load/{pid}")
def studio_load(pid: str, x_auth: str = Header("")):
    usr = check_auth(x_auth); m = _own(pid, usr)
    src = m.get("cut") or m.get("input")
    was_cut = bool(m.get("cut")) and m.get("cut") != m.get("input")
    words = (m.get("cut_words") if was_cut else None) or m.get("words", [])
    removes = []
    if not was_cut:   # avto takliflar hali videoga qo'llanmagan -> timeline'da ko'rsatamiz
        removes = [{"s": r[0], "e": r[1], "type": "pauza"} for r in m.get("silence_removes", [])] + \
                  [{"s": r[0], "e": r[1], "type": "takror"} for r in m.get("dup_removes", [])]
    brolls = []
    for b in m.get("brolls", []):
        img = b.get("image")
        if img and os.path.exists(os.path.join(PROJ, pid, img)):
            brolls.append({"image": img, "image_url": f"/media/{pid}/{img}", "prompt": b.get("prompt", ""),
                           "time": b.get("time", 0), "dur": b.get("dur", 2.5)})
    state = _load(os.path.join(PROJ, pid, "studio.json"), None)
    return {"project": pid, "name": m.get("name", "video"), "lang": m.get("lang", "uz"),
            "video_url": f"/media/{pid}/{src}", "duration": (m.get("cut_duration") if was_cut else None) or m.get("duration", 0),
            "words": words, "removes": removes, "brolls": brolls, "engine": m.get("engine", ""),
            "analyzed": bool(m.get("words")), "music": ({"name": m["music"], "url": f"/media/{pid}/{m['music']}"} if m.get("music") else None),
            "final_url": (f"/media/{pid}/final.mp4" if m.get("final") else None), "state": state}

# ---------- USLUB PROFILLARI (namuna videodan) ----------
STYLES_F = os.path.join(DATA, "styles.json")
@app.get("/api/studio/styles")
def styles_list(x_auth: str = Header("")):
    usr = check_auth(x_auth)
    return {"items": _load(STYLES_F, {}).get(usr["id"], [])}

@app.post("/api/studio/style")
async def style_create(file: UploadFile = File(...), name: str = Form(""), x_auth: str = Header("")):
    """Namuna video yuklanadi -> uslub profili (subtitr, temp, rang, zoom...) chiqariladi va saqlanadi."""
    usr = check_auth(x_auth)
    import tempfile
    tmp = tempfile.mkdtemp()
    ext = os.path.splitext(file.filename or "ref.mp4")[1].lower() or ".mp4"
    inp = os.path.join(tmp, "ref" + ext)
    with open(inp, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            f.write(chunk)
    nm = (name.strip() or os.path.splitext(file.filename or "Namuna")[0])[:40]
    jid = new_job()
    def fn(prog):
        try:
            prog(25, "Namuna video ko'rilyapti...")
            prof = montaj.analyze_style(inp, tmp)
            prof.update(id=uuid.uuid4().hex[:10], name=nm, created=int(time.time()))
            with _lock:
                allst = _load(STYLES_F, {}); lst = allst.get(usr["id"], [])
                lst.insert(0, prof); allst[usr["id"]] = lst[:20]; _save(STYLES_F, allst)
            return prof
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    run_job(jid, fn)
    return {"job": jid}

@app.delete("/api/studio/styles/{sid}")
def style_delete(sid: str, x_auth: str = Header("")):
    usr = check_auth(x_auth)
    with _lock:
        allst = _load(STYLES_F, {})
        allst[usr["id"]] = [x for x in allst.get(usr["id"], []) if x.get("id") != sid]
        _save(STYLES_F, allst)
    return {"ok": True}

@app.post("/api/studio/image")
async def studio_image(file: UploadFile = File(...), project: str = Form(...), x_auth: str = Header("")):
    """O'z rasm / GIF / video faylingizni qatlam (b-roll yoki stiker) sifatida yuklash."""
    usr = check_auth(x_auth); _own(project, usr)
    ext = os.path.splitext(file.filename or "")[1].lower()
    isvid = ext in (".mp4", ".mov", ".webm", ".m4v")
    if not isvid and ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"): raise HTTPException(400, "faqat rasm (PNG/JPG/WEBP/GIF) yoki video (MP4/MOV/WEBM)")
    limit = (300 if isvid else 20) * 1024 * 1024
    name = f"broll_{'v' if isvid else 'u'}{uuid.uuid4().hex[:8]}{ext}"; dest = os.path.join(PROJ, project, name); size = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            size += len(chunk)
            if size > limit:
                f.close(); os.remove(dest); raise HTTPException(400, "fayl juda katta")
            f.write(chunk)
    return {"image": name, "image_url": f"/media/{project}/{name}", "prompt": (file.filename or "fayl")[:80], "video": isvid}

@app.post("/api/studio/audio")
async def studio_audio(file: UploadFile = File(...), project: str = Form(...), x_auth: str = Header("")):
    """Qo'shimcha audio klip: yuklangan fayl yoki mikrofondan yozilgan ovoz."""
    usr = check_auth(x_auth); _own(project, usr)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".webm", ".mp4"): raise HTTPException(400, "audio fayl emas")
    name = f"aud_{uuid.uuid4().hex[:8]}{ext}"; dest = os.path.join(PROJ, project, name); size = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            size += len(chunk)
            if size > 60 * 1024 * 1024:
                f.close(); os.remove(dest); raise HTTPException(400, "fayl juda katta")
            f.write(chunk)
    dur = montaj.ffdur(dest)
    if not dur or dur <= 0:
        os.remove(dest); raise HTTPException(400, "audio o'qilmadi")
    return {"file": name, "url": f"/media/{project}/{name}", "name": (file.filename or "audio")[:60], "duration": round(dur, 2)}

@app.post("/api/studio/append")
async def studio_append(file: UploadFile = File(...), project: str = Form(...), x_auth: str = Header("")):
    """Yana bir video klipni loyiha oxiriga qo'shadi."""
    usr = check_auth(x_auth); meta = _own(project, usr); d = os.path.join(PROJ, project)
    ext = os.path.splitext(file.filename or "c.mp4")[1].lower() or ".mp4"
    newp = os.path.join(d, f"append_{uuid.uuid4().hex[:6]}{ext}")
    with open(newp, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            f.write(chunk)
    jid = new_job()
    def fn(prog):
        prog(20, "Klip qo'shilyapti...")
        cur = os.path.join(d, meta.get("input")); old = float(meta.get("duration") or montaj.ffdur(cur))
        outn = f"input_m{uuid.uuid4().hex[:6]}.mp4"; outp = os.path.join(d, outn)
        ok = montaj.append_clip(cur, newp, outp)
        try: os.remove(newp)
        except Exception: pass
        if not ok: raise RuntimeError("klipni qo'shib bo'lmadi (videoda ovoz yo'lakchasi bormi?)")
        m = load_meta(project); m.update(input=outn, duration=round(montaj.ffdur(outp), 2)); m.pop("cut", None); m.pop("cut_duration", None)
        save_meta(project, m)
        return {"video_url": f"/media/{project}/{outn}", "duration": m["duration"], "join": round(old, 3)}
    run_job(jid, fn)
    return {"job": jid}

@app.post("/api/studio/save")
async def studio_save(req: Request, x_auth: str = Header("")):
    usr = check_auth(x_auth); b = await req.json(); pid = b.get("project", "")
    _own(pid, usr)
    st = b.get("state")
    if not isinstance(st, dict) or len(json.dumps(st)) > 2_000_000: raise HTTPException(400, "holat noto'g'ri")
    with _lock: _save(os.path.join(PROJ, pid, "studio.json"), st)
    return {"ok": True}

@app.delete("/api/history/{pid}")
def delete_project(pid: str, x_auth: str = Header("")):
    usr = check_auth(x_auth)
    if not _PID_RE.match(pid or ""): raise HTTPException(404, "loyiha topilmadi")   # ".." kabi yo'llar butun papkani o'chirib yubormasin
    m = load_meta(pid)
    if not m or m.get("user_id") != usr["id"]: raise HTTPException(403, "ruxsat yo'q")
    shutil.rmtree(os.path.join(PROJ, pid), ignore_errors=True)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
