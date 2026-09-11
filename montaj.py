# -*- coding: utf-8 -*-
"""
MONTAJ pipeline (veb uchun kutubxona).
Bosqichlar:
  analyze(video)  -> so'z-darajali transkript + kesish takliflari + matn/uzunlik
  apply_cut(...)  -> tanlangan qismlarni kesadi, so'z vaqtlarini qayta moslaydi
  broll_suggest(..) -> gapga mos rasm g'oyalari (prompt+at)
  gen_image(prompt) -> bitta rasm (Gemini-image -> Imagen -> Pexels)
  render_final(...) -> subtitr + animatsiyani kesilgan videoga chizadi
Barcha AI: Vertex AI (Google $300 kredit). Zaxira: Gemini API, Groq.
"""
import os, re, json, time, subprocess, shutil, base64, requests

HERE = os.path.dirname(os.path.abspath(__file__))

# --- muhit ---
GROQ_KEY   = os.environ.get("GROQ_API_KEY","").strip()
GROQ_URL   = "https://api.groq.com/openai/v1/audio/transcriptions"
GEMINI_KEY = os.environ.get("GEMINI_API_KEY","").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL","gemini-flash-latest").strip()
GCP_PROJECT  = os.environ.get("GCP_PROJECT_ID","").strip()
GCP_LOCATION = (os.environ.get("GCP_LOCATION","us-central1") or "us-central1").strip()
GCP_SA_JSON  = os.environ.get("GCP_SA_JSON","").strip()
VERTEX_TOKEN = os.environ.get("VERTEX_ACCESS_TOKEN","").strip()
VERTEX_MODEL = os.environ.get("VERTEX_MODEL","gemini-2.5-flash").strip()
PEXELS_KEY   = os.environ.get("PEXELS_API_KEY","").strip()
FONTS_DIR = os.path.join(HERE,"fonts") if os.path.isdir(os.path.join(HERE,"fonts")) else HERE

W, H = 1080, 1920
GROQ_MODEL = "whisper-large-v3"

def have_vertex():
    return bool(GCP_PROJECT and (GCP_SA_JSON or VERTEX_TOKEN))

# ---------- yordamchi ----------
def run(cmd, capture=False):
    if capture:
        p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                         universal_newlines=True,encoding="utf-8",errors="replace")
        return p.returncode,p.stdout
    return subprocess.run(cmd).returncode,""

def prepare_cfr(src, dst, fps=30):
    """Videoni BARQAROR kadr tezligiga (CFR) va 48k audioga keltiradi.
    VFR (o'zgaruvchan kadr) sabab bo'ladigan subtitr 'drift'ini yo'q qiladi."""
    c,_=run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",src,
        "-r",str(fps),"-vsync","cfr","-c:v","libx264","-preset","veryfast","-crf","18",
        "-c:a","aac","-ar","48000","-ac","2","-movflags","+faststart",dst])
    if c==0 and os.path.exists(dst) and ffdur(dst)>0: return dst
    return src

def ffdur(p):
    c,o=run(["ffprobe","-v","error","-show_entries","format=duration",
             "-of","default=noprint_wrappers=1:nokey=1",p],capture=True)
    try: return float(o.strip())
    except: return 0.0

def hexass(h):
    h=h.lstrip("#")
    if len(h)==3: h="".join(c*2 for c in h)
    return "&H00%s%s%s"%(h[4:6].upper(),h[2:4].upper(),h[0:2].upper())

def ts(t):
    if t<0: t=0
    return "%d:%02d:%05.2f"%(int(t//3600),int((t%3600)//60),t%60)

# ---------- Vertex token ----------
def _vertex_token():
    if VERTEX_TOKEN: return VERTEX_TOKEN
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GRequest
    info=json.loads(GCP_SA_JSON)
    creds=service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(GRequest())
    return creds.token

def _vertex_host():
    if GCP_LOCATION=="global": return "aiplatform.googleapis.com","global"
    return f"{GCP_LOCATION}-aiplatform.googleapis.com", GCP_LOCATION

# ---------- transkripsiya ----------
# IBORA (gap bo'lagi) darajasida — so'zma-so'z vaqtdan ko'ra BARQAROR va silliq.
_PROMPT_ASR=("Quyidagi O'ZBEK tilidagi audioni juda ANIQ transkripsiya qil. "
    "Matnni tabiiy IBORALARGA (2-6 so'zdan, pauzalarga qarab) bo'l. "
    "HAR BIR IBORA uchun audiodagi aniq boshlanish (s) va tugash (e) vaqtini SONIYADA ber. "
    "Vaqtlar ovozga juda aniq mos kelsin (sinxron eng muhim). "
    "Matn to'g'ri o'zbek lotin yozuvida bo'lsin (turkcha emas). "
    "JSON massiv qaytar: [{\"text\":\"ibora\",\"s\":0.0,\"e\":0.0}].")

def _parse_words(arr):
    if isinstance(arr,dict): arr=arr.get("segments") or arr.get("data") or arr.get("words") or []
    segs=[]
    for s in arr:
        t=str(s.get("w") or s.get("text") or "").strip()
        if t: segs.append({"text":t,
                           "start":float(s.get("s", s.get("start",0)) or 0),
                           "end":float(s.get("e", s.get("end",0)) or 0)})
    if segs and all(s["end"]<=s["start"] for s in segs):
        for i,s in enumerate(segs): s["start"]=i*2.0; s["end"]=i*2.0+2.0
    return segs

# har bir SO'Z uchun aniq vaqt (qisqa bo'lak ichida)
_PROMPT_WORDS=("Bu O'ZBEK tilidagi qisqa audio bo'lagi. Uni juda ANIQ transkripsiya qil. "
    "HAR BIR SO'Z uchun shu bo'lak ichidagi aniq boshlanish (s) va tugash (e) vaqtini SONIYADA ber "
    "(bo'lak boshi = 0.0). Vaqtlar ovozga juda aniq mos kelsin. "
    "So'zlar tartibi audiodagidek bo'lsin, o'zbek lotin yozuvida (turkcha emas). "
    "JSON massiv: [{\"w\":\"so'z\",\"s\":0.0,\"e\":0.0}].")

def _vertex_audio_call(b64, mime, prompt):
    """Vertex generateContentга audio + prompt yuboradi, matn qaytaradi (model fallback + retry)."""
    token=_vertex_token(); host,loc=_vertex_host()
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    body={"contents":[{"role":"user","parts":[{"text":prompt},{"inlineData":{"mimeType":mime,"data":b64}}]}],
          "generationConfig":{"temperature":0,"responseMimeType":"application/json"}}
    models=[]
    for m in [VERTEX_MODEL,"gemini-2.5-flash","gemini-2.0-flash","gemini-1.5-flash-002","gemini-1.5-flash"]:
        if m and m not in models: models.append(m)
    errs=[]
    for model in models:
        url=f"https://{host}/v1/projects/{GCP_PROJECT}/locations/{loc}/publishers/google/models/{model}:generateContent"
        try:
            last=None
            for k in range(4):
                rr=requests.post(url,headers=headers,json=body,timeout=300)
                if rr.status_code<400: last=rr; break
                last=rr
                if rr.status_code in (429,500,502,503,504): time.sleep(2*(k+1)); continue
                break
            if last.status_code>=400: errs.append(f"{model}:{last.status_code} {last.text[:50]}"); continue
            return last.json()["candidates"][0]["content"]["parts"][0]["text"], model
        except Exception as e:
            errs.append(f"{model}:{str(e)[:50]}")
    raise RuntimeError(" | ".join(errs[:2]))

STT_TEXT_FROM_GEMINI = (os.environ.get("STT_TEXT_FROM_GEMINI","1").strip() not in ("0","false","no",""))

def _parse_offset(v):
    """Google STT offset -> soniya. '1.200s' yoki {'seconds':1,'nanos':...}."""
    if v is None: return 0.0
    if isinstance(v,(int,float)): return float(v)
    if isinstance(v,dict):
        return float(v.get("seconds",0) or 0)+float(v.get("nanos",0) or 0)/1e9
    s=str(v).strip()
    if s.endswith("s"): s=s[:-1]
    try: return float(s)
    except: return 0.0

def stt_words_gcp(clip_path):
    """Google Cloud Speech-to-Text v2 (Chirp) — HAQIQIY akustik so'z vaqtlari. uz-UZ.
    Qaytaradi: [{w,s,e}] (bo'lakka nisbatan)."""
    token=_vertex_token()
    b64=base64.b64encode(open(clip_path,"rb").read()).decode()
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    errs=[]
    # (region, model) — chirp_2 regional; chirp/long global; oxirida v1 zaxira
    for loc,model in [("us-central1","chirp_2"),("us-central1","chirp"),
                      ("global","chirp_2"),("global","long")]:
        host = "speech.googleapis.com" if loc=="global" else f"{loc}-speech.googleapis.com"
        url=f"https://{host}/v2/projects/{GCP_PROJECT}/locations/{loc}/recognizers/_:recognize"
        body={"config":{"autoDecodingConfig":{},"languageCodes":["uz-UZ"],"model":model,
                        "features":{"enableWordTimeOffsets":True}},"content":b64}
        try:
            r=requests.post(url,headers=headers,json=body,timeout=120)
            if r.status_code>=400: errs.append(f"v2/{loc}/{model}:{r.status_code} {r.text[:50]}"); continue
            out=[]
            for res in r.json().get("results",[]):
                alts=res.get("alternatives",[])
                if not alts: continue
                for w in alts[0].get("words",[]):
                    t=str(w.get("word","")).strip()
                    if not t: continue
                    s=_parse_offset(w.get("startOffset")); e=_parse_offset(w.get("endOffset"))
                    out.append({"w":t,"s":s,"e":e if e>s else s+0.2})
            if out: return out
            errs.append(f"v2/{loc}/{model}:bo'sh")
        except Exception as ex:
            errs.append(f"v2/{loc}/{model}:{str(ex)[:40]}")
    # v1 zaxira (sync, <60s)
    try:
        url="https://speech.googleapis.com/v1/speech:recognize"
        body={"config":{"languageCode":"uz-UZ","enableWordTimeOffsets":True,
                        "enableAutomaticPunctuation":True,"model":"latest_long"},
              "audio":{"content":b64}}
        r=requests.post(url,headers=headers,json=body,timeout=120)
        if r.status_code<400:
            out=[]
            for res in r.json().get("results",[]):
                alts=res.get("alternatives",[])
                if not alts: continue
                for w in alts[0].get("words",[]):
                    t=str(w.get("word","")).strip()
                    if t: out.append({"w":t,"s":_parse_offset(w.get("startTime")),"e":_parse_offset(w.get("endTime"))})
            if out: return out
            errs.append("v1:bo'sh")
        else: errs.append(f"v1:{r.status_code} {r.text[:50]}")
    except Exception as ex:
        errs.append(f"v1:{str(ex)[:40]}")
    raise RuntimeError(" | ".join(errs[:2]) or "STT ishlamadi")

def gemini_words_clip(clip_path):
    """Gemini'дан bo'lak matni (o'zbekcha sifatli) — [{w,s,e}] (vaqt taxminiy)."""
    b64=base64.b64encode(open(clip_path,"rb").read()).decode()
    mime="audio/wav" if clip_path.endswith(".wav") else "audio/mp3"
    txt,model=_vertex_audio_call(b64,mime,_PROMPT_WORDS)
    arr=json.loads(txt)
    if isinstance(arr,dict): arr=arr.get("words") or arr.get("segments") or arr.get("data") or []
    out=[]
    for w in arr:
        t=str(w.get("w") or w.get("text") or "").strip()
        if t: out.append({"w":t,"s":float(w.get("s",w.get("start",0)) or 0),"e":float(w.get("e",w.get("end",0)) or 0)})
    return out

def _align_text_timing(gem_words, stt_words):
    """Gemini MATNini STT VAQTIga moslaydi (so'z ketma-ketligi bo'yicha)."""
    if not stt_words: return gem_words
    if not gem_words: return stt_words
    import difflib
    a=[_norm(w["w"]) for w in gem_words]; b=[_norm(w["w"]) for w in stt_words]
    sm=difflib.SequenceMatcher(a=a,b=b,autojunk=False); out=[]
    for tag,i1,i2,j1,j2 in sm.get_opcodes():
        if tag=="equal":
            for k in range(i2-i1):
                g=gem_words[i1+k]; s=stt_words[j1+k]
                out.append({"w":g["w"],"s":s["s"],"e":s["e"]})
        else:
            gseg=gem_words[i1:i2]; sseg=stt_words[j1:j2]
            if gseg and sseg:
                t0=sseg[0]["s"]; t1=sseg[-1]["e"]; span=max(0.2,t1-t0); n=len(gseg)
                for k,g in enumerate(gseg):
                    out.append({"w":g["w"],"s":round(t0+span*k/n,3),"e":round(t0+span*(k+1)/n,3)})
            elif sseg:
                out.extend([{"w":s["w"],"s":s["s"],"e":s["e"]} for s in sseg])
            elif gseg:
                out.extend(gseg)
    out.sort(key=lambda x:x["s"]); return out

def _clip_words_real(clip):
    """Bir bo'lak uchun: STT vaqti (aniq) + Gemini matni (sifatli), moslashtirilgan."""
    stt=[]
    try: stt=stt_words_gcp(clip)
    except Exception: stt=[]
    gem=[]
    if STT_TEXT_FROM_GEMINI or not stt:
        try: gem=gemini_words_clip(clip)
        except Exception: gem=[]
    if stt and gem: return _align_text_timing(gem,stt)
    return stt or gem

def merge_chunk_words(raw):
    """raw = [(offset, [{w,s,e}...])...] -> bitta tartiblangan, dublikatsiz so'z ro'yxati."""
    allw=[]
    for off,ws in raw:
        for w in ws:
            t=str(w.get("w") or w.get("text") or "").strip()
            if not t: continue
            st=off+float(w.get("s", w.get("start",0)) or 0)
            en=off+float(w.get("e", w.get("end",st)) or st)
            allw.append({"w":t,"s":st,"e":en})
    allw.sort(key=lambda x:x["s"])
    out=[]
    for w in allw:
        if out:
            last=out[-1]
            # overlap zonasidagi takror so'zni tashlab yuboramiz
            if _norm(w["w"])==_norm(last["w"]) and abs(w["s"]-last["s"])<0.7: continue
            if w["s"]<last["e"]-0.02: w["s"]=last["e"]      # ustma-ust bo'lmasin
        if w["e"]<=w["s"]: w["e"]=w["s"]+0.20
        w["s"]=round(w["s"],3); w["e"]=round(w["e"],3)
        out.append(w)
    return out

def transcribe_words_vertex(wav, chunk=18.0, overlap=1.5, _clip_fn=None, _dur=None):
    """Audioni qisqa bo'laklarga bo'lib, HAR BIR SO'Z uchun aniq vaqt oladi (drift kam)."""
    dur=_dur if _dur is not None else ffdur(wav)
    if dur<=0: raise RuntimeError("audio uzunligi 0")
    raw=[]; used_model=""
    s=0.0
    while s < dur-0.05:
        e=min(dur, s+chunk)
        if _clip_fn is not None:
            ws=_clip_fn(s,e)
        else:
            clip=wav+f".{int(s*100)}.wav"    # WAV — kodek kechikishi yo'q, sample-aniq
            run(["ffmpeg","-y","-loglevel","error","-ss",f"{s:.2f}","-to",f"{e:.2f}","-i",wav,
                 "-ar","16000","-ac","1","-c:a","pcm_s16le",clip])
            ws=_clip_words_real(clip)         # STT vaqti + Gemini matni
            used_model="STT+Gemini"
            try: os.remove(clip)
            except: pass
        raw.append((s, ws))
        if e>=dur: break
        s += (chunk-overlap)
    words=merge_chunk_words(raw)
    if not words: raise RuntimeError("so'z topilmadi")
    full=" ".join(w["w"] for w in words)
    return words, full, f"VERTEX-word:{used_model or 'ok'}"

def transcribe_vertex(wav):
    """IBORA darajasidagi zaxira yo'l (bir martalik)."""
    mp3=wav+".mp3"; run(["ffmpeg","-y","-loglevel","error","-i",wav,"-b:a","64k",mp3])
    src=mp3 if os.path.exists(mp3) else wav
    mime="audio/mp3" if src.endswith(".mp3") else "audio/wav"
    b64=base64.b64encode(open(src,"rb").read()).decode()
    txt,model=_vertex_audio_call(b64,mime,_PROMPT_ASR)
    segs=_parse_words(json.loads(txt))
    if segs: return segs," ".join(s["text"] for s in segs),f"VERTEX:{model}"
    raise RuntimeError("bo'sh natija")

def transcribe_gemini(wav):
    mp3=wav+".mp3"; run(["ffmpeg","-y","-loglevel","error","-i",wav,"-b:a","64k",mp3])
    src=mp3 if os.path.exists(mp3) else wav
    mime="audio/mp3" if src.endswith(".mp3") else "audio/wav"
    b64=base64.b64encode(open(src,"rb").read()).decode()
    body={"contents":[{"parts":[{"text":_PROMPT_ASR},{"inline_data":{"mime_type":mime,"data":b64}}]}],
          "generationConfig":{"temperature":0,"response_mime_type":"application/json"}}
    models=[]
    for m in [GEMINI_MODEL,"gemini-flash-latest","gemini-2.5-flash","gemini-2.0-flash","gemini-1.5-flash"]:
        if m and m not in models: models.append(m)
    errs=[]
    for ver in ["v1beta","v1"]:
        for model in models:
            url=f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={GEMINI_KEY}"
            try:
                r=requests.post(url,json=body,timeout=300)
                if r.status_code>=400: errs.append(f"{ver}/{model}:{r.status_code}"); continue
                txt=r.json()["candidates"][0]["content"]["parts"][0]["text"]
                segs=_parse_words(json.loads(txt))
                if segs: return segs," ".join(s["text"] for s in segs),f"GEMINI:{model}@{ver}"
            except Exception as e:
                errs.append(f"{ver}/{model}:{str(e)[:40]}")
    raise RuntimeError(" | ".join(errs[:3]))

def transcribe_groq(wav):
    with open(wav,"rb") as f:
        r=requests.post(GROQ_URL,headers={"Authorization":f"Bearer {GROQ_KEY}"},
            files={"file":(os.path.basename(wav),f,"audio/wav")},
            data={"model":GROQ_MODEL,"language":"uz","temperature":"0","response_format":"verbose_json"},timeout=300)
    r.raise_for_status(); j=r.json(); segs=[]
    for s in j.get("segments",[]):
        t=(s.get("text") or "").strip()
        if t: segs.append({"text":t,"start":float(s["start"]),"end":float(s["end"])})
    return segs,(j.get("text") or "").strip(),"GROQ"

def transcribe(wav):
    if have_vertex():
        try:
            segs,full,eng=transcribe_vertex(wav)
            if segs: return segs,full,eng
            verr="bo'sh"
        except Exception as e: verr=str(e)[:250]
        segs,full,_=transcribe_groq(wav)
        return segs,full,f"GROQ (VERTEX xato: {verr})"
    if GEMINI_KEY:
        try:
            segs,full,eng=transcribe_gemini(wav)
            if segs: return segs,full,eng
        except Exception as e:
            segs,full,_=transcribe_groq(wav); return segs,full,f"GROQ (gemini xato:{str(e)[:120]})"
    segs,full,_=transcribe_groq(wav)
    return segs,full,"GROQ"

# ---------- so'z / vaqt ----------
def normalize_words(segs, dur):
    """So'zlarni tartiblaydi. STT'ning ANIQ boshlanish vaqtini SAQLAYDI —
    ustma-ustlikда oldingi so'z oxirini qisqartiradi (so'z boshini surmaydi)."""
    segs=[s for s in segs if str(s.get("text","")).strip()]
    segs.sort(key=lambda s:float(s["start"]))
    out=[]
    for s in segs:
        st=max(0.0,min(float(s["start"]),dur))       # aniq boshlanish — SAQLANADI
        en=float(s["end"])
        if en<=st: en=st+0.25
        en=min(en,dur)
        if out and out[-1]["e"]>st: out[-1]["e"]=round(st,3)   # oldingi so'z oxirini qisqartiramiz
        out.append({"w":s["text"].strip(),"s":round(st,3),"e":round(en,3)})
    return out

def normalize_segments(segs, dur):
    """IBORA segmentlarini tartiblaydi (o'sish, ustma-ust yo'q, 0..dur)."""
    segs=[s for s in segs if str(s.get("text","")).strip()]
    segs.sort(key=lambda s:float(s.get("start",0)))
    out=[]; prev=0.0
    for s in segs:
        st=max(0.0,min(float(s.get("start",0)),dur))
        en=float(s.get("end",0))
        if en<=st: en=st+1.0
        st=max(st,prev-0.02); en=min(en,dur)
        if en-st<0.25: en=min(dur,st+0.6)
        out.append({"text":str(s["text"]).strip(),"start":round(st,3),"end":round(en,3)})
        prev=en
    return out

def words_from_segments(segments):
    """Ibora segmentini so'zlarga bo'lib, vaqtni harf-uzunligiga qarab TEKIS taqsimlaydi.
    Vertex'ning so'zma-so'z vaqtidan ko'ra barqarorroq va silliqroq (sinxron uchun)."""
    out=[]
    for s in segments:
        toks=str(s["text"]).split()
        if not toks: continue
        dur=max(0.3, float(s["end"])-float(s["start"])); wsum=sum(max(1,len(t)) for t in toks)
        t=float(s["start"])
        for tok in toks:
            d=dur*max(1,len(tok))/wsum
            out.append({"w":tok,"s":round(t,3),"e":round(t+d,3)}); t+=d
        out[-1]["e"]=round(float(s["end"]),3)
    return out

# ---------- kesish ----------
def detect_silences(p, dB=-30, mind=0.9):
    c,o=run(["ffmpeg","-hide_banner","-i",p,"-af",
        "silencedetect=noise=%ddB:d=%s"%(dB,mind),"-f","null","-"],capture=True)
    st=[float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)",o)]
    en=[float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)",o)]
    return list(zip(st,en+[None]*(len(st)-len(en))))

def keep_from_silence(dur,sils,pad=0.12):
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

def _norm(w): return re.sub(r"[^\w']","",w.lower())

def duplicate_ranges(sent_segs):
    """Takror/qayta boshlangan gaplarning birinchisini olib tashlash oralig'i.
    EHTIYOTKOR: faqat aniq takror bo'lganда (yaxshi gapni kesib yubormaslik uchun)."""
    rem=[]
    for i in range(len(sent_segs)-1):
        a=[_norm(x) for x in sent_segs[i]["text"].split() if _norm(x)]
        b=[_norm(x) for x in sent_segs[i+1]["text"].split() if _norm(x)]
        if len(a)<3 or len(b)<3: continue          # juda qisqa iboralarga tegmaymiz
        sa,sb=set(a),set(b)
        jac=len(sa&sb)/max(1,len(sa|sb)); cont=len(sa&sb)/len(sa)
        # ketma-ket va vaqtда yaqin bo'lsa (qayta boshlash) hamda juda o'xshash bo'lsa
        near=(sent_segs[i+1]["start"]-sent_segs[i]["end"])<1.2
        if near and (jac>=0.72 or cont>=0.85):
            rem.append((sent_segs[i]["start"]-0.05, sent_segs[i]["end"]+0.05))
    return rem

def safe_silence_removes(sil_removes, words, margin=0.12):
    """Faqat SO'Z ustidan o'tmaydigan jimlik-kesishlarni qoldiradi (gapni kesmaslik uchun).
    So'zga tegib turgan qismini qirqib, faqat toza jimlik bo'lagini o'chiradi."""
    if not words: return sil_removes
    out=[]
    for a,b in sil_removes:
        # shu oraliqda so'z bormi?
        seg_a,seg_b=a,b
        # so'z bilan kesishsa, so'zdan chetlatamiz
        for w in words:
            ws,we=w["s"]-margin, w["e"]+margin
            if we<=seg_a or ws>=seg_b: continue
            # so'z oraliqni to'liq qoplasa -> bu kesishni tashlab yuboramiz
            if ws<=seg_a and we>=seg_b: seg_a=seg_b=0; break
            if ws<=seg_a<we: seg_a=we        # boshini so'zdan keyinga
            if ws<we<=seg_b: pass
            if ws<seg_b<=we: seg_b=ws        # oxirini so'zdan oldinga
        if seg_b-seg_a>=0.35:                 # faqat sezilarli jimlik
            out.append([round(seg_a,2),round(seg_b,2)])
    return out

def sentences_from_words(words):
    sents=[]; cur=[]
    for w in words:
        if cur and w["s"]-cur[-1]["e"]>0.5: sents.append(cur); cur=[]
        cur.append(w)
    if cur: sents.append(cur)
    return [{"text":" ".join(x["w"] for x in s),"start":s[0]["s"],"end":s[-1]["e"]} for s in sents]

def subtract_ranges(segs, removes):
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

def remap_words(words,segs):
    out=[];cut=0.0
    for a,b in segs:
        for w in words:
            if w["s"]>=a-0.05 and w["s"]<b+0.05:
                ns=cut+(max(a,w["s"])-a); ne=cut+(min(b,w["e"])-a)
                if ne>ns: out.append({"w":w["w"],"s":round(ns,3),"e":round(ne,3)})
        cut+=(b-a)
    out.sort(key=lambda x:x["s"]); return out

# ---------- subtitr (ASS) ----------
def group_cues(words,n):
    cues=[];cur=[]
    for w in words:
        if cur and (len(cur)>=n or w["s"]-cur[-1]["e"]>0.7): cues.append(cur);cur=[]
        cur.append(w)
    if cur:cues.append(cur)
    return cues

def build_ass(words, sub):
    """sub = dict: font, size, base, active, outline, border, upper, words, delay, margin_v"""
    base=hexass(sub.get("base","#FFFFFF")); acc=hexass(sub.get("active","#FFEA00"))
    outl=hexass(sub.get("outline","#000000")); upper=sub.get("upper",True)
    font=sub.get("font","Anton"); size=int(sub.get("size",90)); border=sub.get("border",4)
    mv=int(sub.get("margin_v",660)); off=float(sub.get("delay",0.20)); n=int(sub.get("words",3))
    head=f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{size},{base},{base},{outl},&H64000000,-1,0,0,0,100,100,0,0,1,{border},2,2,100,100,{mv},1

[Events]
Format: Layer, Start, End, Style, MarginL, MarginR, MarginV, Effect, Text
"""
    cues=group_cues(words,n)
    flat=[]
    for cue in cues:
        for wi,w in enumerate(cue): flat.append((cue,wi,w))
    # UZLUKSIZ va USTMA-UST TUSHMAYDIGAN qilib qat'iy tartiblash
    lines=[]; prev_end=-1.0
    for idx,(cue,wi,w) in enumerate(flat):
        start=w["s"]
        end=flat[idx+1][2]["s"] if idx+1<len(flat) else w["e"]+0.4
        start=max(start, prev_end)          # oldingi tugagach boshlanadi -> stacking yo'q
        if end<=start: end=start+0.20
        prev_end=end
        disp=[(x["w"].upper() if upper else x["w"]).replace("{","(").replace("}",")") for x in cue]
        parts=["{\\c%s}%s{\\c%s}"%(acc,d,base) if j==wi else d for j,d in enumerate(disp)]
        ov="{\\fad(50,0)\\fscx94\\fscy94\\t(0,110,\\fscx100\\fscy100)}" if wi==0 else ""
        lines.append("Dialogue: 0,%s,%s,Main,0,0,0,,%s%s"%(ts(start+off),ts(end+off),ov," ".join(parts)))
    return head+"\n".join(lines)+"\n"

# ---------- B-ROLL rasm ----------
def _ai_json(prompt):
    if have_vertex():
        token=_vertex_token(); host,loc=_vertex_host()
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

def broll_suggest(full_text, dur, n=4):
    prompt=(f"Video transkripti (o'zbekcha): \"{full_text[:1500]}\"\n"
            f"Video uzunligi: {int(dur)} soniya.\n"
            f"Shu gap mazmuniga mos {n} ta B-ROLL rasm g'oyasini ber. "
            f"Har biri uchun: 'prompt' = INGLIZCHA, aniq, tasviriy rasm generatsiya prompti "
            f"(fotorealistik, kinematik, ichida MATN/YOZUV bo'lmasin), "
            f"va 'at' = videodagi joyi 0.0 dan 1.0 gacha. Butun video bo'ylab tarqat. "
            f"JSON qaytar: [{{\"prompt\":\"...\",\"at\":0.0}}].")
    txt=_ai_json(prompt); arr=json.loads(txt)
    if isinstance(arr,dict): arr=arr.get("brolls") or arr.get("data") or arr.get("items") or []
    out=[]
    for it in arr:
        q=str(it.get("prompt") or it.get("keyword") or "").strip()
        at=float(it.get("at",0) or 0)
        if q: out.append({"prompt":q,"at":max(0.0,min(1.0,at))*dur})
    return out

def gemini_image_vertex(prompt, dest):
    token=_vertex_token()
    # rasm modellari REGIONAL — us-central1 da bo'ladi (global emas)
    host="us-central1-aiplatform.googleapis.com"; loc="us-central1"
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    full=("Generate a single photorealistic, cinematic 16:9 image. "
          "NO text, NO letters, NO captions, NO watermark. Subject: "+prompt)
    body={"contents":[{"role":"user","parts":[{"text":full}]}],
          "generationConfig":{"responseModalities":["TEXT","IMAGE"]}}
    errs=[]
    for model in ["gemini-2.5-flash-image","gemini-2.0-flash-preview-image-generation",
                  "gemini-2.5-flash-image-preview","gemini-2.0-flash-exp"]:
        url=f"https://{host}/v1/projects/{GCP_PROJECT}/locations/{loc}/publishers/google/models/{model}:generateContent"
        try:
            r=requests.post(url,headers=headers,json=body,timeout=120)
            if r.status_code>=400: errs.append(f"{model}:{r.status_code} {r.text[:50]}"); continue
            j=r.json(); b64=None
            for cand in j.get("candidates",[]):
                for part in cand.get("content",{}).get("parts",[]):
                    inl=part.get("inlineData") or part.get("inline_data")
                    if inl and inl.get("data"): b64=inl["data"]; break
                if b64: break
            if not b64: errs.append(f"{model}:rasm yo'q"); continue
            open(dest,"wb").write(base64.b64decode(b64)); return dest
        except Exception as e:
            errs.append(f"{model}:{str(e)[:40]}")
    raise RuntimeError(" || ".join(errs[:2]) or "gemini-image ishlamadi")

def imagen_vertex(prompt, dest):
    token=_vertex_token()
    if GCP_LOCATION=="global": host="us-central1-aiplatform.googleapis.com"; loc="us-central1"
    else: host=f"{GCP_LOCATION}-aiplatform.googleapis.com"; loc=GCP_LOCATION
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    body={"instances":[{"prompt":prompt}],"parameters":{"sampleCount":1,"aspectRatio":"16:9"}}
    errs=[]
    for model in ["imagen-4.0-fast-generate-001","imagen-4.0-generate-001","imagen-3.0-generate-002","imagen-3.0-generate-001","imagegeneration@006"]:
        url=f"https://{host}/v1/projects/{GCP_PROJECT}/locations/{loc}/publishers/google/models/{model}:predict"
        try:
            r=requests.post(url,headers=headers,json=body,timeout=120)
            if r.status_code>=400: errs.append(f"{model}:{r.status_code}"); continue
            preds=r.json().get("predictions",[])
            if not preds: errs.append(f"{model}:bo'sh"); continue
            b64=preds[0].get("bytesBase64Encoded") or preds[0].get("image",{}).get("bytesBase64Encoded")
            if not b64: errs.append(f"{model}:b64 yo'q"); continue
            open(dest,"wb").write(base64.b64decode(b64)); return dest
        except Exception as e:
            errs.append(f"{model}:{str(e)[:40]}")
    raise RuntimeError(" || ".join(errs[:2]) or "imagen ishlamadi")

def pexels_image(query, dest):
    if not PEXELS_KEY: return None
    r=requests.get("https://api.pexels.com/v1/search",headers={"Authorization":PEXELS_KEY},
        params={"query":query,"per_page":3,"orientation":"landscape"},timeout=30)
    if r.status_code>=400: return None
    photos=r.json().get("photos",[])
    if not photos: return None
    src=photos[0]["src"].get("large2x") or photos[0]["src"].get("large") or photos[0]["src"].get("original")
    im=requests.get(src,timeout=30)
    if im.status_code>=400: return None
    open(dest,"wb").write(im.content); return dest

def gen_image(prompt, dest):
    """Vertex bo'lsa: gemini-image -> imagen. Aks holda Pexels. Xatoni ko'taradi."""
    if have_vertex():
        e1=""
        try: return gemini_image_vertex(prompt,dest)
        except Exception as ex: e1=str(ex)[:80]
        try: return imagen_vertex(prompt,dest)
        except Exception as ex: raise RuntimeError(f"gem-img[{e1}] / imagen[{str(ex)[:80]}]")
    got=pexels_image(prompt,dest)
    if not got: raise RuntimeError("Pexels bo'sh (yoki kalit yo'q)")
    return got

# ---------- final render ----------
def render_final(cut, ass_path, outp, brolls, zoom=True, audio_clean=True):
    """brolls = [{'path':..,'time':s,'dur':d,'y':0.72,'w':0.78}]"""
    ae=ass_path.replace("\\","/").replace(":","\\:"); fd=FONTS_DIR.replace("\\","/").replace(":","\\:")
    sub=f"ass='{ae}':fontsdir='{fd}'"
    if zoom:
        basev=(f"[0:v]scale={int(W*1.12)}:{int(H*1.12)}:force_original_aspect_ratio=increase,crop={int(W*1.12)}:{int(H*1.12)},"
               f"zoompan=z='min(1.0+0.0004*in,1.05)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps=30,"
               f"eq=contrast=1.05:saturation=1.08,setsar=1,{sub}[base]")
    else:
        basev=f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,{sub}[base]"
    af=("highpass=f=85,afftdn=nr=12,equalizer=f=3000:t=q:w=1.5:g=3,acompressor=threshold=-18dB:ratio=3,"
        "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000") if audio_clean else "aresample=48000"
    parts=[basev]; cur="[base]"; inputs=["-i",cut]
    for k,b in enumerate(brolls):
        p=b["path"]; s=float(b["time"]); d=float(b.get("dur",2.5))
        y=float(b.get("y",0.72)); bw=int(W*float(b.get("w",0.78)))
        inputs+=["-loop","1","-t",f"{d}","-i",p]
        idx=k+1
        parts.append(f"[{idx}:v]scale={bw}:-1,format=yuva420p,"
                     f"fade=t=in:st=0:d=0.25:alpha=1,fade=t=out:st={max(0.01,d-0.25):.2f}:d=0.25:alpha=1,"
                     f"setpts=PTS+{s:.2f}/TB[ov{k}]")
        nxt=f"[vb{k}]"
        parts.append(f"{cur}[ov{k}]overlay=(W-w)/2:{int(H*y)}:enable='between(t,{s:.2f},{s+d:.2f})':eof_action=pass:repeatlast=0{nxt}")
        cur=nxt
    parts.append(f"[0:a]{af}[aout]")
    fc=";".join(parts)
    cmd=["ffmpeg","-y","-hide_banner","-loglevel","error"]+inputs+[
        "-filter_complex",fc,"-map",cur,"-map","[aout]",
        "-r","30","-c:v","libx264","-preset","medium","-crf","20","-pix_fmt","yuv420p",
        "-c:a","aac","-b:a","160k",outp]
    c,_=run(cmd)
    return c==0 and os.path.exists(outp)

# ================= YUQORI DARAJADAGI BOSQICHLAR =================
def analyze(video_path, work):
    """Transkripsiya + kesish takliflari. Qaytaradi: dict."""
    wav=os.path.join(work,"a16k.wav")
    run(["ffmpeg","-y","-loglevel","error","-i",video_path,"-ar","16000","-ac","1",wav])
    dur=ffdur(video_path)
    eng=""
    words=None
    if have_vertex():
        try:
            words, full, eng = transcribe_words_vertex(wav)   # QISQA BO'LAKLI, har so'z aniq
            words = normalize_words([{"text":w["w"],"start":w["s"],"end":w["e"]} for w in words], dur)
        except Exception as e:
            eng=f"(word-level xato: {str(e)[:80]})"; words=None
    if not words:                                  # zaxira: ibora darajasi + tekis taqsim
        segs,full,eng2=transcribe(wav)
        segs=normalize_segments(segs,dur)
        words=words_from_segments(segs)
        eng=(eng+" "+eng2).strip()
    sents=sentences_from_words(words)              # takror-aniqlash gaplar bo'yicha
    # jimlik kesish — biroz konservativ (gapni kesmasin)
    sils=detect_silences(video_path, dB=-32, mind=0.8)
    keep=keep_from_silence(dur,sils,pad=0.10)
    raw_sil=[]
    prev_e=0.0
    for a,b in keep:
        if a-prev_e>0.25: raw_sil.append([round(prev_e,2),round(a,2)])
        prev_e=b
    if dur-prev_e>0.25: raw_sil.append([round(prev_e,2),round(dur,2)])
    # SO'Z ustidan kesmaslik uchun filtr (gapни kesmaydi)
    silence_removes=safe_silence_removes(raw_sil, words, margin=0.12)
    dup_removes=[[round(a,2),round(b,2)] for a,b in duplicate_ranges(sents)]
    return {"duration":round(dur,2),"words":words,"full_text":full,"engine":eng,
            "silence_removes":silence_removes,"dup_removes":dup_removes,
            "sentences":[{"text":s["text"],"start":s["start"],"end":s["end"]} for s in sents]}

def analyze_reference(video_path, work):
    """Namuna videoning subtitr TEMPI va kesish uslubini o'lchaydi (rasm-uslub emas — temp/ritm).
    Faqat dastlabki ~60s tahlil qilinadi (tejamkorlik)."""
    dur=ffdur(video_path)
    clip=os.path.join(work,"ref60.wav")
    run(["ffmpeg","-y","-loglevel","error","-t","60","-i",video_path,"-ar","16000","-ac","1",clip])
    segs,full,eng=transcribe(clip)
    words=normalize_words(segs, min(dur,60))
    # so'z/soniya tempi
    span=max(1.0,(words[-1]["e"]-words[0]["s"])) if len(words)>1 else 1.0
    wps=len(words)/span
    # tabiiy guruhlar (pauzaga qarab) o'rtacha necha so'z
    cues=group_cues(words, 6)
    avg=sum(len(c) for c in cues)/max(1,len(cues))
    words_per_cue=max(1,min(5,round(avg))) if cues else 3
    # kesish zichligi (jimliklar soni / daqiqa)
    sils=detect_silences(video_path)
    per_min=len(sils)/max(1.0,(min(dur,60)/60.0))
    fast=per_min>=12 or wps>=3.2
    pace="tez" if wps>=3.0 else ("o'rtacha" if wps>=2.0 else "sekin")
    return {"words_per_cue":words_per_cue,"wps":round(wps,2),"pace":pace,
            "fast_cut":bool(fast),"engine":eng}

def apply_cut(video_path, words, removes, work, dur):
    """removes = [[s,e],...] (o'chiriladigan). Kesadi, so'zlarni qayta moslaydi."""
    keep=subtract_ranges([(0.0,dur)], [tuple(r) for r in removes]) if removes else [(0.0,dur)]
    keep=[(a,b) for a,b in keep if b-a>0.1] or [(0.0,dur)]
    cut=os.path.join(work,"cut.mp4"); cut_video(video_path,cut,keep)
    w2=remap_words(words,keep) if len(keep)>1 else words
    return cut, w2, round(ffdur(cut),2)
