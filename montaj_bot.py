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
import os, re, json, asyncio, tempfile, subprocess, shutil, requests
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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL","gemini-2.0-flash").strip()
# --- Vertex AI (Google Cloud, $300 kredit) ---
GCP_PROJECT = os.environ.get("GCP_PROJECT_ID","").strip()
GCP_LOCATION = (os.environ.get("GCP_LOCATION","us-central1") or "us-central1").strip()
GCP_SA_JSON = os.environ.get("GCP_SA_JSON","").strip()          # service account JSON (butun matn)
VERTEX_TOKEN = os.environ.get("VERTEX_ACCESS_TOKEN","").strip() # yoki tayyor token (AQ...)
VERTEX_MODEL = os.environ.get("VERTEX_MODEL","gemini-2.5-flash").strip()
FONTS_DIR = os.path.join(HERE,"fonts") if os.path.isdir(os.path.join(HERE,"fonts")) else HERE

CFG = {
    "til":"uz", "groq_model":"whisper-large-v3",
    "jimlik_dB":-30, "jimlik_min_soniya":0.9, "kesish_pad":0.12,
    "sozlar_soni":3, "shrift":"Anton", "shrift_olcham":90,
    "asosiy_rang":"#FFFFFF", "faol_rang":"#FFEA00", "chegara_rang":"#000000",
    "chegara":4, "bosh_harf":True, "past_chetdan":340,
    "zoom":True, "ovoz_tozalash":True,
    "takror_olib_tashlash":True,   # takror aytilgan gaplarni olib tashlash
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
            "Har bir gap/ibora uchun boshlanish va tugash vaqti (soniyada) bilan segment ber. "
            "Matn to'g'ri o'zbek lotin yozuvida bo'lsin (turkcha emas). "
            "JSON massiv qaytar: [{\"start\":son,\"end\":son,\"text\":\"...\"}].")
    body={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":mime,"data":b64}}]}],
          "generationConfig":{"temperature":0,"response_mime_type":"application/json"}}
    models=[]
    for m in [GEMINI_MODEL,"gemini-2.5-flash","gemini-flash-latest","gemini-2.0-flash","gemini-1.5-flash"]:
        if m and m not in models: models.append(m)
    errors=[]
    for ver in ["v1beta","v1"]:
        for model in models:
            url=f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={GEMINI_KEY}"
            try:
                r=requests.post(url,json=body,timeout=300)
                if r.status_code>=400:
                    errors.append(f"{ver}/{model}:{r.status_code}"); continue
                j=r.json()
                txt=j["candidates"][0]["content"]["parts"][0]["text"]
                arr=_json.loads(txt)
                if isinstance(arr,dict): arr=arr.get("segments") or arr.get("data") or []
                segs=[]
                for s in arr:
                    t=str(s.get("text","")).strip()
                    if t: segs.append({"text":t,"start":float(s.get("start",0) or 0),"end":float(s.get("end",0) or 0)})
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
            "Har bir gap/ibora uchun boshlanish va tugash vaqti (soniyada) bilan segment ber. "
            "Matn to'g'ri o'zbek lotin yozuvida bo'lsin (turkcha emas). "
            "JSON massiv qaytar: [{\"start\":son,\"end\":son,\"text\":\"...\"}].")
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
    errors=[]
    for model in models:
        url=f"https://{host}/v1/projects/{GCP_PROJECT}/locations/{loc}/publishers/google/models/{model}:generateContent"
        try:
            r=requests.post(url,headers=headers,json=body,timeout=300)
            if r.status_code>=400:
                errors.append(f"{model}:{r.status_code} {r.text[:70]}"); continue
            j=r.json()
            txt=j["candidates"][0]["content"]["parts"][0]["text"]
            arr=_json.loads(txt)
            if isinstance(arr,dict): arr=arr.get("segments") or arr.get("data") or []
            segs=[]
            for s in arr:
                t=str(s.get("text","")).strip()
                if t: segs.append({"text":t,"start":float(s.get("start",0) or 0),"end":float(s.get("end",0) or 0)})
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
            verr=str(e)[:250]
        if GEMINI_KEY:
            try:
                segs,full,eng=transcribe_gemini(wav)
                if segs: return segs,full,eng+" ✅"
            except Exception: pass
        segs,full,_=transcribe_groq(wav)
        return segs,full,f"GROQ (vertex xato: {verr})"
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
            lines.append("Dialogue: 0,%s,%s,Main,0,0,0,,%s%s"%(ts(w["s"]),ts(w["e"]),ov," ".join(parts)))
    return head+"\n".join(lines)+"\n"

# ---------- render ----------
def render(cut,ass,outp):
    W,H=1080,1920
    ae=ass.replace("\\","/").replace(":","\\:"); fd=FONTS_DIR.replace("\\","/").replace(":","\\:")
    sub=f"ass='{ae}':fontsdir='{fd}'"
    if CFG["zoom"]:
        vf=(f"scale={int(W*1.12)}:{int(H*1.12)}:force_original_aspect_ratio=increase,crop={int(W*1.12)}:{int(H*1.12)},"
            f"zoompan=z='min(1.0+0.0004*in,1.05)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps=30,"
            f"eq=contrast=1.05:saturation=1.08,setsar=1,{sub}")
    else:
        vf=f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,{sub}"
    af=("highpass=f=85,afftdn=nr=12,equalizer=f=3000:t=q:w=1.5:g=3,acompressor=threshold=-18dB:ratio=3,"
        "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000") if CFG["ovoz_tozalash"] else "aresample=48000"
    c,_=run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",cut,"-vf",vf,"-af",af,
        "-r","30","-c:v","libx264","-preset","medium","-crf","20","-pix_fmt","yuv420p",
        "-c:a","aac","-b:a","160k",outp])
    return c==0 and os.path.exists(outp)

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
    segments, full_text, lang = transcribe(wav)
    segments = normalize_segments(segments, dur)
    words = words_from_segments(segments)          # uzluksiz subtitr
    keep=keep_segments(dur,detect_silences(inp),CFG["kesish_pad"])
    dups=duplicate_ranges(segments) if CFG.get("takror_olib_tashlash",True) else []
    segs=subtract_ranges(keep,dups) if dups else keep
    cut=base+"_cut.mp4"; segs=cut_video(inp,cut,segs)
    words=remap(words,segs) if len(segs)>1 else words
    ass=base+".ass"; open(ass,"w",encoding="utf-8").write(build_ass(words,1080,1920))
    out=base+"_final.mp4"; ok=render(cut,ass,out)
    return (out if ok else None), len(words), dur, ffdur(cut), full_text, lang

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
