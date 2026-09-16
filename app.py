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
PREVIEW_VER = "v2"   # effektlar o'zgarsa bump qiling -> keshdan qayta chiziladi
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

# ---------- 2) TAHLIL ----------
@app.post("/api/analyze")
async def analyze(req: Request, x_auth: str = Header("")):
    check_auth(x_auth)
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
    check_auth(x_auth)
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
    check_auth(x_auth)
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
    check_auth(x_auth)
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
    check_auth(x_auth)
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
    jid = new_job()
    def fn(prog):
        prog(15, "Subtitr tayyorlanyapti...")
        ass = os.path.join(d, "subs.ass")
        if subtitle_on and words:
            open(ass, "w", encoding="utf-8").write(montaj.build_ass(words, sub, lang))
        else:
            open(ass, "w", encoding="utf-8").write(montaj.build_ass([], sub, lang))
        brolls = []
        for b in brolls_in:
            if not b.get("on", True): continue
            img = b.get("image")
            if not img: continue
            p = os.path.join(d, img)
            if os.path.exists(p):
                brolls.append({"path": p, "time": float(b.get("time", 0)),
                               "dur": float(b.get("dur", 2.5)), "y": float(b.get("y", 0.72)),
                               "w": float(b.get("w", 0.78))})
        prog(45, "Video render qilinyapti (1-3 daqiqa)...")
        out = os.path.join(d, "final.mp4")
        ok = montaj.render_final(cut, ass, out, brolls, zoom=zoom, audio_clean=audio, grade=grade, zoom_level=zoom_level, quality=quality, fx=fx)
        if not ok: raise RuntimeError("render xatosi")
        meta.update(final="final.mp4", stage="done", finished=int(time.time()),
                    cut_words=words, brolls=brolls_in,
                    render_settings={"subtitle": sub, "zoom": zoom, "audio_clean": audio,
                                     "subtitle_on": subtitle_on, "broll_y": (brolls_in[0]["y"] if brolls_in else 0.72),
                                     "grade": grade, "zoom_level": zoom_level, "quality": quality, "fx": fx})
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

# ---------- AVTOMATIK (hammasi bir tugmada) ----------
@app.post("/api/auto")
async def auto(req: Request, x_auth: str = Header("")):
    check_auth(x_auth)
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
        open(ass, "w", encoding="utf-8").write(montaj.build_ass(w2 if subtitle_on else [], sub, lang))
        rbrolls = [{"path": os.path.join(d, b["image"]), "time": b["time"], "dur": b["dur"],
                    "y": broll_y, "w": 0.78} for b in outb]
        out = os.path.join(d, "final.mp4")
        if not montaj.render_final(cut, ass, out, rbrolls, zoom=zoom, audio_clean=audio, grade=grade, zoom_level=zoom_level, quality=quality, fx=fx):
            raise RuntimeError("render xato")
        meta.update(final="final.mp4", stage="done", finished=int(time.time()), brolls=outb,
                    render_settings={"subtitle": sub, "zoom": zoom, "audio_clean": audio,
                                     "subtitle_on": subtitle_on, "broll_y": broll_y,
                                     "grade": grade, "zoom_level": zoom_level, "quality": quality, "fx": fx})
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

@app.delete("/api/history/{pid}")
def delete_project(pid: str, x_auth: str = Header("")):
    usr = check_auth(x_auth)
    m = load_meta(pid)
    if m and m.get("user_id") != usr["id"]: raise HTTPException(403, "ruxsat yo'q")
    shutil.rmtree(os.path.join(PROJ, pid), ignore_errors=True)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
