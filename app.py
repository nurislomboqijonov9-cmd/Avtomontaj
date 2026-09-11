# -*- coding: utf-8 -*-
"""
MONTAJ STUDIO — veb backend (FastAPI).
Bosqichlar (job asosida, progress bilan):
  /api/upload -> /api/analyze -> /api/cut -> /api/broll_* -> /api/render
Tarix: tayyor videolar serverda saqlanadi.
"""
import os, json, uuid, time, shutil, threading, traceback
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import montaj

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
PROJ = os.path.join(DATA, "projects")
os.makedirs(PROJ, exist_ok=True)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()   # bo'sh bo'lsa -> ochiq

app = FastAPI(title="Montaj Studio")

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

def check_auth(token):
    if APP_PASSWORD and token != APP_PASSWORD:
        raise HTTPException(401, "Parol noto'g'ri")

# ---------- statik ----------
app.mount("/media", StaticFiles(directory=PROJ), name="media")
STATIC = os.path.join(HERE, "static")

@app.get("/", response_class=HTMLResponse)
def index():
    # index.html qayerda bo'lsa ham topamiz (static/ ichida yoki asosiy papkada)
    for p in [os.path.join(STATIC, "index.html"),
              os.path.join(HERE, "index.html"),
              os.path.join(HERE, "static", "index.html")]:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
    return HTMLResponse("<h2>index.html topilmadi.</h2>"
        "<p>index.html faylini repo'ga (static/ papkasiga yoki asosiy papkaga) yuklang.</p>", status_code=500)

@app.get("/api/config")
def config():
    return {"auth": bool(APP_PASSWORD), "vertex": montaj.have_vertex()}

@app.post("/api/login")
async def login(req: Request):
    body = await req.json()
    if APP_PASSWORD and body.get("password", "") != APP_PASSWORD:
        raise HTTPException(401, "Parol noto'g'ri")
    return {"ok": True, "token": APP_PASSWORD}

# ---------- 1) YUKLASH ----------
@app.post("/api/upload")
async def upload(file: UploadFile = File(...), x_auth: str = Header("")):
    check_auth(x_auth)
    pid = uuid.uuid4().hex[:12]
    d = os.path.join(PROJ, pid); os.makedirs(d, exist_ok=True)
    ext = os.path.splitext(file.filename or "in.mp4")[1].lower() or ".mp4"
    inp = os.path.join(d, "input" + ext)
    with open(inp, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            f.write(chunk)
    dur = montaj.ffdur(inp)
    meta = {"id": pid, "input": os.path.basename(inp), "name": file.filename or "video",
            "created": int(time.time()), "duration": round(dur, 2), "stage": "uploaded"}
    save_meta(pid, meta)
    return {"project": pid, "video_url": f"/media/{pid}/{os.path.basename(inp)}",
            "duration": meta["duration"], "name": meta["name"]}

# ---------- 2) TAHLIL ----------
@app.post("/api/analyze")
async def analyze(req: Request, x_auth: str = Header("")):
    check_auth(x_auth)
    body = await req.json(); pid = body["project"]
    d = pdir(pid); meta = load_meta(pid)
    inp = os.path.join(d, meta["input"])
    jid = new_job()
    def fn(prog):
        prog(10, "Transkripsiya (o'zbekcha)...")
        res = montaj.analyze(inp, d)
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
    brolls_in = body.get("brolls", [])
    jid = new_job()
    def fn(prog):
        prog(15, "Subtitr tayyorlanyapti...")
        ass = os.path.join(d, "subs.ass")
        if subtitle_on and words:
            open(ass, "w", encoding="utf-8").write(montaj.build_ass(words, sub))
        else:
            open(ass, "w", encoding="utf-8").write(montaj.build_ass([], sub))
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
        ok = montaj.render_final(cut, ass, out, brolls, zoom=zoom, audio_clean=audio)
        if not ok: raise RuntimeError("render xatosi")
        meta.update(final="final.mp4", stage="done", finished=int(time.time()),
                    render_settings={"subtitle": sub, "zoom": zoom, "audio_clean": audio,
                                     "subtitle_on": subtitle_on, "brolls": brolls_in})
        save_meta(pid, meta)
        return {"final_url": f"/media/{pid}/final.mp4"}
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
    check_auth(x_auth)
    items = []
    for pid in os.listdir(PROJ):
        m = load_meta(pid)
        if m.get("final"):
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

@app.delete("/api/history/{pid}")
def delete_project(pid: str, x_auth: str = Header("")):
    check_auth(x_auth)
    shutil.rmtree(os.path.join(PROJ, pid), ignore_errors=True)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
