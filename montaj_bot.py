# -*- coding: utf-8 -*-
"""
Telegram MONTAJ BOT (Pyrogram) — KATTA videolar (2GB gacha).
  • O'zbekcha aniq subtitr (Groq Whisper large-v3)
  • Aqlli kesish (gap o'rtasidan kesmaydi)
  • So'z-ba-so'z animatsiyali subtitr (viral uslub)
  • 9:16 + silliq zoom + ovoz tozalash

Muhit o'zgaruvchilari (Railway "Variables"):
  API_ID           (my.telegram.org)
  API_HASH         (my.telegram.org)
  TELEGRAM_TOKEN   (@BotFather)
  GROQ_API_KEY     (console.groq.com)
"""
import os, re, json, time, asyncio, tempfile, subprocess, shutil, requests
from pyrogram import Client, filters

HERE = os.path.dirname(os.path.abspath(__file__))
_envf = os.path.join(HERE, ".env")
if os.path.exists(_envf):
    for _l in open(_envf, encoding="utf-8"):
        _l=_l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            k,v=_l.split("=",1); os.environ.setdefault(k.strip(), v.strip())

API_ID   = int(os.environ.get("API_ID","0") or 0)
API_HASH = os.environ.get("API_HASH","").strip()
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN","").strip()
GROQ_KEY = os.environ.get("GROQ_API_KEY","").strip()
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GEMINI_KEY = os.environ.get("GEMINI_API_KEY","").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL","gemini-flash-latest").strip()
# --- Vertex AI (Google Cloud, $300 kredit) ---
GCP_PROJECT = os.environ.get("GCP_PROJECT_ID","").strip()
GCP_LOCATION = (os.environ.get("GCP_LOCATION","us-central1") or "us-central1").strip()
GCP_SA_JSON = os.environ.get("GCP_SA_JSON","").strip()          # service account JSON (butun matn)
VERTEX_TOKEN = os.environ.get("VERTEX_ACCESS_TOKEN","").strip() # yoki tayyor token (AQ...)
VERTEX_MODEL = os.environ.get("VERTEX_MODEL","gemini-2.5-flash").strip()
PEXELS_KEY = os.environ.get("PEXELS_API_KEY","").strip()   # b-roll (bepul stok rasm) uchun
FONTS_DIR = os.path.join(HERE,"fonts") if os.path.isdir(os.path.join(HERE,"fonts")) else HERE

CFG = {
    "til":"uz", "groq_model":"whisper-large-v3",
    "jimlik_dB":-30, "jimlik_min_soniya":0.9, "kesish_pad":0.12,
    "sozlar_soni":3, "shrift":"Anton", "shrift_olcham":90,
    "asosiy_rang":"#FFFFFF", "faol_rang":"#FFEA00", "chegara_rang":"#000000",
    "chegara":4, "bosh_harf":True, "past_chetdan":660,
    "broll_y":0.72,                # b-roll vertikal joyi (0=tepa,1=past) — subtitr ostida
    "zoom":True, "ovoz_tozalash":True,
    "takror_olib_tashlash":True,   # takror aytilgan gaplarni olib tashlash
    "subtitr_kechikish":0.20,      # subtitrni shuncha soniya kechiktirish (ovozga mos kelsin)
    "broll":True,                  # gapga mos rasm (b-roll) qo'shish (PEXELS_API_KEY kerak)
    "broll_soni":4,                # nechta b-roll
    "broll_davomiylik":2.5,        # har biri necha soniya ko'rinadi
}

# ---------- yordamchi ----------
def run(cmd, capture=False):
    if capture:
        p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                         universal_newlines=True,encoding="utf-8",errors="replace")
        return p.returncode,p.stdout
    return subprocess.run(cmd).returncode,""

def ffdur(p):
    c,o=run(["ffprobe","-v","error","-show_entries","format=duration",
             "-of","default=noprint_wrappers=1:nokey=1",p],capture=True)
    try:return float(o.strip())
    except:return 0.0

def hexass(h):
    h=h.lstrip("#")
    if len(h)==3:h="".join(c*2 for c in h)
    return "&H00%s%s%s"%(h[4:6].upper(),h[2:4].upper(),h[0:2].upper())

def ts(t):
    if t<0:t=0
    return "%d:%02d:%05.2f"%(int(t//3600),int((t%3600)//60),t%60)

# ---------- transkripsiya ----------
def transcribe_gemini(wav):
    """Gemini (oddiy REST, SDK'siz) -> (segments, matn, model@ver). Import muammosi yo'q."""
    import base64, json as _json
    mp3=wav+".mp3"
    run(["ffmpeg","-y","-loglevel","error","-i",wav,"-b:a","64k",mp3])
    src=mp3 if os.path.exists(mp3) else wav
    mime="audio/mp3" if src.endswith(".mp3") else "audio/wav"
    b64=base64.b64encode(open(src,"rb").read()).decode()
    prompt=("Quyidagi O'ZBEK tilidagi audioni juda ANIQ transkripsiya qil. "
            "HAR BIR SO'Z uchun audiodagi aniq boshlanish (s) va tugash (e) vaqtini soniyada ber. "
            "Vaqtlar ovozga aniq mos kelsin (sinxron muhim). "
            "Matn to'g'ri o'zbek lotin yozuvida bo'lsin (turkcha emas). "
            "JSON massiv qaytar: [{\"w\":\"so'z\",\"s\":0.0,\"e\":0.0}].")
    body={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":mime,"data":b64}}]}],
          "generationConfig":{"temperature":0,"response_mime_type":"application/json"}}
    models=[]
    for m in [GEMINI_MODEL,"gemini-flash-latest","gemini-2.5-flash","gemini-2.0-flash","gemini-1.5-flash"]:
        if m and m not in models: models.append(m)
    def post_retry(url):
        last=None
        for k in range(4):
            rr=requests.post(url,json=body,timeout=300)
            if rr.status_code<400: return rr
            last=rr
            if rr.status_code in (429,500,502,503,504):   # vaqtinchalik xato -> qayta urin
                time.sleep(2*(k+1)); continue
            return rr
        return last
    errors=[]
    for ver in ["v1beta","v1"]:
        for model in models:
            url=f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={GEMINI_KEY}"
            try:
                r=post_retry(url)
                if r.status_code>=400:
                    errors.append(f"{ver}/{model}:{r.status_code}"); continue
                j=r.json()
                txt=j["candidates"][0]["content"]["parts"][0]["text"]
                arr=_json.loads(txt)
                if isinstance(arr,dict): arr=arr.get("segments") or arr.get("data") or []
                segs=[]
                for s in arr:
                    t=str(s.get("w") or s.get("text") or "").strip()
                    if t: segs.append({"text":t,"start":float(s.get("s", s.get("start",0)) or 0),"end":float(s.get("e", s.get("end",0)) or 0)})
                if segs and all(s["end"]<=s["start"] for s in segs):
                    for i,s in enumerate(segs): s["start"]=i*2.0; s["end"]=i*2.0+2.0
                if segs:
                    return segs, " ".join(s["text"] for s in segs), f"GEMINI:{model}@{ver}"
                errors.append(f"{ver}/{model}:bo'sh")
            except Exception as e:
                errors.append(f"{ver}/{model}:{str(e)[:40]}")
    raise RuntimeError(" | ".join(errors[:3]))

def transcribe_groq(wav):
    """Groq Whisper (zaxira) -> (segments, matn, til)."""
    with open(wav,"rb") as f:
        r=requests.post(GROQ_URL,
            headers={"Authorization":f"Bearer {GROQ_KEY}"},
            files={"file":(os.path.basename(wav),f,"audio/wav")},
            data={"model":CFG["groq_model"],"language":"uz","temperature":"0",
                  "response_format":"verbose_json"}, timeout=300)
    r.raise_for_status(); j=r.json(); segs=[]
    for s in j.get("segments",[]):
        t=(s.get("text") or "").strip()
        if t: segs.append({"text":t,"start":float(s["start"]),"end":float(s["end"])})
    return segs, (j.get("text") or "").strip(), (j.get("language") or "?")

def _vertex_token():
    if VERTEX_TOKEN: return VERTEX_TOKEN
    import json as _json
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GRequest
    info=_json.loads(GCP_SA_JSON)
    creds=service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(GRequest())
    return creds.token

def transcribe_vertex(wav):
    """Vertex AI Gemini -> (segments, matn, model)."""
    import base64, json as _json
    token=_vertex_token()
    mp3=wav+".mp3"; run(["ffmpeg","-y","-loglevel","error","-i",wav,"-b:a","64k",mp3])
    src=mp3 if os.path.exists(mp3) else wav
    mime="audio/mp3" if src.endswith(".mp3") else "audio/wav"
    b64=base64.b64encode(open(src,"rb").read()).decode()
    prompt=("Quyidagi O'ZBEK tilidagi audioni juda ANIQ transkripsiya qil. "
            "HAR BIR SO'Z uchun audiodagi aniq boshlanish (s) va tugash (e) vaqtini soniyada ber. "
            "Vaqtlar ovozga aniq mos kelsin (sinxron muhim). "
            "Matn to'g'ri o'zbek lotin yozuvida bo'lsin (turkcha emas). "
            "JSON massiv qaytar: [{\"w\":\"so'z\",\"s\":0.0,\"e\":0.0}].")
    body={"contents":[{"role":"user","parts":[{"text":prompt},{"inlineData":{"mimeType":mime,"data":b64}}]}],
          "generationConfig":{"temperature":0,"responseMimeType":"application/json"}}
    if GCP_LOCATION=="global":
        host="aiplatform.googleapis.com"; loc="global"
    else:
        host=f"{GCP_LOCATION}-aiplatform.googleapis.com"; loc=GCP_LOCATION
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    models=[]
    for m in [VERTEX_MODEL,"gemini-2.5-flash","gemini-2.0-flash","gemini-1.5-flash-002","gemini-1.5-flash"]:
        if m and m not in models: models.append(m)
    def post_retry(url):
        last=None
        for k in range(4):
            rr=requests.post(url,headers=headers,json=body,timeout=300)
            if rr.status_code<400: return rr
            last=rr
            if rr.status_code in (429,500,502,503,504):   # vaqtinchalik -> qayta urin
                time.sleep(2*(k+1)); continue
            return rr
        return last
    errors=[]
    for model in models:
        url=f"https://{host}/v1/projects/{GCP_PROJECT}/locations/{loc}/publishers/google/models/{model}:generateContent"
        try:
            r=post_retry(url)
            if r.status_code>=400:
                errors.append(f"{model}:{r.status_code} {r.text[:70]}"); continue
            j=r.json()
            txt=j["candidates"][0]["content"]["parts"][0]["text"]
            arr=_json.loads(txt)
            if isinstance(arr,dict): arr=arr.get("segments") or arr.get("data") or []
            segs=[]
            for s in arr:
                t=str(s.get("w") or s.get("text") or "").strip()
                if t: segs.append({"text":t,"start":float(s.get("s", s.get("start",0)) or 0),"end":float(s.get("e", s.get("end",0)) or 0)})
            if segs and all(s["end"]<=s["start"] for s in segs):
                for i,s in enumerate(segs): s["start"]=i*2.0; s["end"]=i*2.0+2.0
            if segs:
                return segs, " ".join(s["text"] for s in segs), f"VERTEX:{model}"
            errors.append(f"{model}:bo'sh")
        except Exception as e:
            errors.append(f"{model}:{str(e)[:50]}")
    raise RuntimeError(" | ".join(errors[:2]))

def transcribe(wav):
    """Vertex (kredit) -> Gemini(AIza) -> Groq."""
    if GCP_PROJECT and (GCP_SA_JSON or VERTEX_TOKEN):
        try:
            segs,full,eng=transcribe_vertex(wav)
            if segs: return segs,full,eng+" ✅"
            verr="bo'sh natija"
        except Exception as e:
            verr=str(e)[:300]
        # Vertex majburiy rejim: xatoni YASHIRMAYMIZ (Gemini'ga o'tmaymiz), sababni ko'rsatamiz
        segs,full,_=transcribe_groq(wav)
        return segs,full,f"GROQ (VERTEX xato: {verr})"
    if GEMINI_KEY:
        try:
            segs,full,eng=transcribe_gemini(wav)
            if segs: return segs,full,eng+" ✅"
            gerr="bo'sh natija"
        except Exception as e:
            gerr=str(e)[:250]
        segs,full,_=transcribe_groq(wav)
        return segs,full,f"GROQ (gemini xato: {gerr})"
    segs,full,_=transcribe_groq(wav)
    return segs,full,"GROQ (kalit yo'q)"

def words_from_segments(segments):
    """Segment matnini so'zlarga bo'lib, vaqtni harf-uzunligiga qarab taqsimlaydi (uzluksiz subtitr)."""
    out=[]
    for s in segments:
        toks=s["text"].split()
        if not toks: continue
        dur=max(0.3, s["end"]-s["start"]); wsum=sum(max(1,len(t)) for t in toks)
        t=s["start"]
        for tok in toks:
            d=dur*max(1,len(tok))/wsum
            out.append({"w":tok,"s":round(t,3),"e":round(t+d,3)}); t+=d
        out[-1]["e"]=s["end"]
    return out

# ---------- kesish ----------
def detect_silences(p):
    c,o=run(["ffmpeg","-hide_banner","-i",p,"-af",
        "silencedetect=noise=%ddB:d=%s"%(CFG["jimlik_dB"],CFG["jimlik_min_soniya"]),
        "-f","null","-"],capture=True)
    st=[float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)",o)]
    en=[float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)",o)]
    return list(zip(st,en+[None]*(len(st)-len(en))))

def keep_segments(dur,sils,pad):
    keep=[];cur=0.0
    for s,e in sils:
        se=max(cur,s+pad)
        if se-cur>=0.3: keep.append((cur,se))
        cur=max(0.0,(e-pad) if e else dur)
    if dur-cur>=0.3: keep.append((cur,dur))
    m=[]
    for a,b in keep:
        a,b=max(0,a),min(dur,b)
        if b-a<0.1: continue
        if m and a-m[-1][1]<0.08: m[-1]=(m[-1][0],b)
        else: m.append((a,b))
    return m

def cut_video(inp,outp,segs):
    if len(segs)<=1:
        shutil.copy(inp,outp); return [(0,ffdur(inp))]
    sel="+".join("between(t,%.3f,%.3f)"%(a,b) for a,b in segs)
    fc=("[0:v]select='%s',setpts=N/FRAME_RATE/TB[v];"
        "[0:a]aselect='%s',asetpts=N/SR/TB[a]"%(sel,sel))
    c,_=run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",inp,
        "-filter_complex",fc,"-map","[v]","-map","[a]",
        "-c:v","libx264","-preset","veryfast","-crf","20","-c:a","aac","-b:a","160k",outp])
    if c!=0: shutil.copy(inp,outp)
    return segs

def remap(words,segs):
    out=[];cut=0.0
    for a,b in segs:
        for w in words:
            if w["s"]>=a-0.05 and w["s"]<b+0.05:
                ns=cut+(max(a,w["s"])-a); ne=cut+(min(b,w["e"])-a)
                if ne>ns: out.append({"w":w["w"],"s":round(ns,3),"e":round(ne,3)})
        cut+=(b-a)
    out.sort(key=lambda x:x["s"]); return out

# ---------- subtitr ----------
def group_cues(words,n):
    cues=[];cur=[]
    for w in words:
        if cur and (len(cur)>=n or w["s"]-cur[-1]["e"]>0.7): cues.append(cur);cur=[]
        cur.append(w)
    if cur:cues.append(cur)
    return cues

def build_ass(words,W,H):
    base=hexass(CFG["asosiy_rang"]);acc=hexass(CFG["faol_rang"]);outl=hexass(CFG["chegara_rang"])
    upper=CFG["bosh_harf"]
    head=f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{CFG['shrift']},{CFG['shrift_olcham']},{base},{base},{outl},&H64000000,-1,0,0,0,100,100,0,0,1,{CFG['chegara']},2,2,100,100,{CFG['past_chetdan']},1

[Events]
Format: Layer, Start, End, Style, MarginL, MarginR, MarginV, Effect, Text
"""
    lines=[]
    for cue in group_cues(words,CFG["sozlar_soni"]):
        disp=[(w["w"].upper() if upper else w["w"]).replace("{","(").replace("}",")") for w in cue]
        for i,w in enumerate(cue):
            parts=["{\\c%s}%s{\\c%s}"%(acc,d,base) if j==i else d for j,d in enumerate(disp)]
            ov="{\\fad(50,0)\\fscx92\\fscy92\\t(0,110,\\fscx100\\fscy100)}" if i==0 else ""
            off=CFG.get("subtitr_kechikish",0.0)
            lines.append("Dialogue: 0,%s,%s,Main,0,0,0,,%s%s"%(ts(w["s"]+off),ts(w["e"]+off),ov," ".join(parts)))
    return head+"\n".join(lines)+"\n"

# ---------- render ----------
def render(cut,ass,outp,brolls=None):
    brolls=brolls or []
    W,H=1080,1920
    ae=ass.replace("\\","/").replace(":","\\:"); fd=FONTS_DIR.replace("\\","/").replace(":","\\:")
    sub=f"ass='{ae}':fontsdir='{fd}'"
    if CFG["zoom"]:
        basev=(f"[0:v]scale={int(W*1.12)}:{int(H*1.12)}:force_original_aspect_ratio=increase,crop={int(W*1.12)}:{int(H*1.12)},"
               f"zoompan=z='min(1.0+0.0004*in,1.05)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps=30,"
               f"eq=contrast=1.05:saturation=1.08,setsar=1,{sub}[base]")
    else:
        basev=(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,{sub}[base]")
    af=("highpass=f=85,afftdn=nr=12,equalizer=f=3000:t=q:w=1.5:g=3,acompressor=threshold=-18dB:ratio=3,"
        "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000") if CFG["ovoz_tozalash"] else "aresample=48000"
    parts=[basev]; cur="[base]"; inputs=["-i",cut]; bw=int(W*0.78)
    for k,(p,s,d) in enumerate(brolls):
        inputs+=["-loop","1","-t",f"{d}","-i",p]
        idx=k+1
        parts.append(f"[{idx}:v]scale={bw}:-1,format=yuva420p,"
                     f"fade=t=in:st=0:d=0.3:alpha=1,fade=t=out:st={max(0.01,d-0.3):.2f}:d=0.3:alpha=1,"
                     f"setpts=PTS+{s:.2f}/TB[ov{k}]")
        nxt=f"[vb{k}]"
        parts.append(f"{cur}[ov{k}]overlay=(W-w)/2:{int(H*CFG.get('broll_y',0.66))}:enable='between(t,{s:.2f},{s+d:.2f})':eof_action=pass:repeatlast=0{nxt}")
        cur=nxt
    parts.append(f"[0:a]{af}[aout]")
    fc=";".join(parts)
    cmd=["ffmpeg","-y","-hide_banner","-loglevel","error"]+inputs+[
        "-filter_complex",fc,"-map",cur,"-map","[aout]",
        "-r","30","-c:v","libx264","-preset","medium","-crf","20","-pix_fmt","yuv420p",
        "-c:a","aac","-b:a","160k",outp]
    c,_=run(cmd)
    return c==0 and os.path.exists(outp)

# ---------- B-ROLL (gapga mos rasm) ----------
def _ai_json(prompt):
    """Vertex (bo'lsa) yoki Gemini API orqali JSON javob oladi."""
    if GCP_PROJECT and (GCP_SA_JSON or VERTEX_TOKEN):
        token=_vertex_token()
        if GCP_LOCATION=="global": host="aiplatform.googleapis.com"; loc="global"
        else: host=f"{GCP_LOCATION}-aiplatform.googleapis.com"; loc=GCP_LOCATION
        url=f"https://{host}/v1/projects/{GCP_PROJECT}/locations/{loc}/publishers/google/models/{VERTEX_MODEL}:generateContent"
        body={"contents":[{"role":"user","parts":[{"text":prompt}]}],
              "generationConfig":{"temperature":0.4,"responseMimeType":"application/json"}}
        r=requests.post(url,headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"},json=body,timeout=60)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    body={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.4,"response_mime_type":"application/json"}}
    r=requests.post(url,json=body,timeout=60); r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

def broll_plan(full_text, dur, n=4):
    prompt=(f"Video transkripti (o'zbekcha): \"{full_text[:1500]}\"\n"
            f"Video uzunligi: {int(dur)} soniya.\n"
            f"Shu gap mazmuniga mos {n} ta B-ROLL rasm g'oyasini ber. "
            f"Har biri uchun: 'prompt' = INGLIZCHA, aniq, tasviriy rasm generatsiya prompti "
            f"(fotorealistik, kinematik, ichida MATN/YOZUV bo'lmasin), "
            f"va 'at' = videodagi joyi 0.0 dan 1.0 gacha. Butun video bo'ylab tarqat. "
            f"JSON qaytar: [{{\"prompt\":\"...\",\"at\":0.0}}].")
    txt=_ai_json(prompt)
    import json as _json
    arr=_json.loads(txt)
    if isinstance(arr,dict): arr=arr.get("brolls") or arr.get("data") or arr.get("items") or []
    out=[]
    for it in arr:
        q=str(it.get("prompt") or it.get("keyword") or "").strip()
        at=float(it.get("at",0) or 0)
        if q: out.append((q, max(0.0,min(1.0,at))*dur))
    return out

def imagen_vertex(prompt, dest):
    """Vertex Imagen bilan rasm chizadi (matn-to-rasm). $300 kreditdan."""
    import base64, json as _json
    token=_vertex_token()
    if GCP_LOCATION=="global": host="us-central1-aiplatform.googleapis.com"; loc="us-central1"
    else: host=f"{GCP_LOCATION}-aiplatform.googleapis.com"; loc=GCP_LOCATION
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    body={"instances":[{"prompt":prompt}],
          "parameters":{"sampleCount":1,"aspectRatio":"16:9"}}
    for model in ["imagen-4.0-fast-generate-001","imagen-4.0-generate-001",
                  "imagen-3.0-generate-002","imagen-3.0-generate-001","imagegeneration@006"]:
        url=f"https://{host}/v1/projects/{GCP_PROJECT}/locations/{loc}/publishers/google/models/{model}:predict"
        try:
            r=requests.post(url,headers=headers,json=body,timeout=120)
            if r.status_code>=400: continue
            preds=r.json().get("predictions",[])
            if not preds: continue
            b64=preds[0].get("bytesBase64Encoded") or preds[0].get("image",{}).get("bytesBase64Encoded")
            if not b64: continue
            with open(dest,"wb") as f: f.write(base64.b64decode(b64))
            return dest
        except Exception:
            continue
    return None

def pexels_image(query, dest):
    r=requests.get("https://api.pexels.com/v1/search",
        headers={"Authorization":PEXELS_KEY},
        params={"query":query,"per_page":3,"orientation":"landscape"},timeout=30)
    if r.status_code>=400: return None
    photos=r.json().get("photos",[])
    if not photos: return None
    src=photos[0]["src"].get("large2x") or photos[0]["src"].get("large") or photos[0]["src"].get("original")
    im=requests.get(src,timeout=30)
    if im.status_code>=400: return None
    with open(dest,"wb") as f: f.write(im.content)
    return dest

def build_brolls(full_text, dur, work):
    """Fail-safe: xato bo'lsa bo'sh ro'yxat + sabab (video baribir chiqadi).
    Rasm manbasi: Vertex Imagen (bo'lsa) -> Pexels (bo'lsa). Qaytaradi: (ro'yxat, izoh)."""
    use_imagen = bool(GCP_PROJECT and (GCP_SA_JSON or VERTEX_TOKEN))
    if not CFG.get("broll"): return [], "o'chirilgan"
    if not (use_imagen or PEXELS_KEY): return [], "manba yo'q"
    if not full_text: return [], "matn yo'q"
    try:
        plan=broll_plan(full_text, dur, CFG.get("broll_soni",4))
    except Exception as e:
        return [], f"plan xato: {str(e)[:120]}"
    d=CFG.get("broll_davomiylik",2.5); out=[]; err=""
    for i,(q,t) in enumerate(plan):
        try:
            p=os.path.join(work,f"broll_{i}.png")
            got = imagen_vertex(q,p) if use_imagen else pexels_image(q,p)
            if got:
                st=max(0.0, min(dur-d, t-d/2))
                out.append((p, round(st,2), d))
            else:
                err="rasm olinmadi (Imagen/Pexels bo'sh)"
        except Exception as e:
            err=str(e)[:100]
    note=f"{len(out)} ta" if out else (err or "0")
    return out, note

# ---------- takror gaplarni topish ----------
def _norm(w): return re.sub(r"[^\w']","",w.lower())

def duplicate_ranges(segments):
    """Takror aytilgan yoki qayta boshlangan (restart) gaplarning birinchisini olib tashlaydi."""
    rem=[]
    for i in range(len(segments)-1):
        a=[_norm(x) for x in segments[i]["text"].split() if _norm(x)]
        b=[_norm(x) for x in segments[i+1]["text"].split() if _norm(x)]
        if len(a)<2 or len(b)<2: continue
        sa,sb=set(a),set(b)
        jac=len(sa&sb)/max(1,len(sa|sb))       # umumiy o'xshashlik
        cont=len(sa&sb)/len(sa)                 # a ning qancha qismi keyingisida bor (restart)
        # to'liq takror (jac yuqori) YOKI qayta boshlash (a deyarli b ichida)
        if jac>=0.6 or cont>=0.75:
            rem.append((segments[i]["start"]-0.05, segments[i]["end"]+0.05))
    return rem

def subtract_ranges(segs, removes):
    """keep segmentlaridan remove oraliqlarini ayiradi."""
    if not removes: return segs
    out=[]
    for a,b in segs:
        cuts=sorted([r for r in removes if r[1]>a and r[0]<b])
        cur=a
        for ra,rb in cuts:
            ra=max(a,ra); rb=min(b,rb)
            if ra-cur>0.15: out.append((cur,ra))
            cur=max(cur,rb)
        if b-cur>0.15: out.append((cur,b))
    return out

def normalize_segments(segs, dur):
    """Vaqtlarni tartiblaydi: 0..dur oralig'iga, o'sish tartibida, end>start."""
    segs=[s for s in segs if s.get("text","").strip()]
    segs.sort(key=lambda s:s["start"])
    out=[]; prev_end=0.0
    for s in segs:
        st=max(0.0,min(float(s["start"]), dur))
        en=float(s["end"])
        if en<=st: en=st+1.2
        st=max(st, prev_end-0.05)          # ustma-ust tushmasin
        en=min(en, dur)
        if en-st<0.2: en=min(dur, st+0.6)
        out.append({"text":s["text"].strip(),"start":round(st,3),"end":round(en,3)})
        prev_end=en
    return out

def process_video(inp,work):
    base=os.path.join(work,"job"); wav=base+"_16k.wav"
    run(["ffmpeg","-y","-loglevel","error","-i",inp,"-ar","16000","-ac","1",wav])
    dur=ffdur(inp)
    segments, full_text, lang = transcribe(wav)      # segments = SO'Z-darajali (har biri 1 so'z, aniq vaqt)
    segments = normalize_segments(segments, dur)
    words = words_from_segments(segments)            # aniq so'z vaqtlari (sinxron)
    # takror-topish uchun so'zlardan gaplar tuzamiz (pauzaga qarab)
    sents=[]; cur=[]
    for sg in segments:
        if cur and sg["start"]-cur[-1]["end"]>0.5: sents.append(cur); cur=[]
        cur.append(sg)
    if cur: sents.append(cur)
    sent_segs=[{"text":" ".join(x["text"] for x in s),"start":s[0]["start"],"end":s[-1]["end"]} for s in sents]
    keep=keep_segments(dur,detect_silences(inp),CFG["kesish_pad"])
    dups=duplicate_ranges(sent_segs) if CFG.get("takror_olib_tashlash",True) else []
    segs=subtract_ranges(keep,dups) if dups else keep
    cut=base+"_cut.mp4"; segs=cut_video(inp,cut,segs)
    words=remap(words,segs) if len(segs)>1 else words
    ass=base+".ass"; open(ass,"w",encoding="utf-8").write(build_ass(words,1080,1920))
    brolls,bnote=build_brolls(full_text, ffdur(cut), work)   # gapga mos rasm (fail-safe)
    out=base+"_final.mp4"; ok=render(cut,ass,out,brolls)
    return (out if ok else None), len(words), dur, ffdur(cut), full_text, lang+f" | broll:{bnote}"

# ---------- Telegram (Pyrogram) ----------
app = Client("montaj", api_id=API_ID, api_hash=API_HASH, bot_token=TG_TOKEN,
             workdir=HERE)

@app.on_message(filters.command("start"))
async def start(_, m):
    await m.reply_text("Salom! Menga video tashlang — kesib, o'zbekcha subtitr qo'yib, tayyor Reels qaytaraman. (2GB gacha)")

@app.on_message(filters.video | filters.document)
async def on_video(client, m):
    if m.document and not (m.document.mime_type or "").startswith("video"):
        return await m.reply_text("Iltimos, video yuboring.")
    status=await m.reply_text("⏳ Qabul qildim, videoni yuklab olyapman...")
    work=tempfile.mkdtemp()
    try:
        inp=os.path.join(work,"in.mp4")
        await m.download(file_name=inp)
        await status.edit_text("🎬 Montaj qilinyapti... (1-3 daqiqa)")
        out,nw,d0,d1,text,lang=await asyncio.to_thread(process_video, inp, work)
        if not out:
            return await status.edit_text("Xatolik yuz berdi.")
        await status.edit_text("📤 Tayyor, yuborilyapti...")
        await client.send_video(m.chat.id, out,
            caption=f"✅ Tayyor! {d0:.0f}s→{d1:.0f}s | til: {lang} | {nw} so'z")
        if text:
            await m.reply_text("📝 Tanilgan matn (tekshirish uchun):\n\n"+text[:3500])
        await status.delete()
    except Exception as e:
        try: await status.edit_text(f"Xatolik: {str(e)[:250]}")
        except: pass
    finally:
        shutil.rmtree(work, ignore_errors=True)

if __name__=="__main__":
    miss=[k for k,v in {"API_ID":API_ID,"API_HASH":API_HASH,"TELEGRAM_TOKEN":TG_TOKEN,"GROQ_API_KEY":GROQ_KEY}.items() if not v]
    if miss:
        print("! Muhit o'zgaruvchilari yetishmaydi:", ", ".join(miss))
    else:
        print("Bot ishga tushdi (Pyrogram, 2GB gacha).")
        app.run()
