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

# ---------- TIL sozlamalari ----------
LANGS = {
    "uz": {"code": "uz-UZ", "name": "Uzbek", "note": "correct Uzbek Latin script (NOT Turkish)"},
    "ru": {"code": "ru-RU", "name": "Russian", "note": "correct Russian Cyrillic"},
    "en": {"code": "en-US", "name": "English", "note": "correct English"},
}
def _lang(lang): return LANGS.get((lang or "uz").lower(), LANGS["uz"])

# ---------- transkripsiya ----------
def _prompt_asr(lang="uz"):
    L=_lang(lang)
    return (f"Transcribe the following {L['name']} audio VERY accurately. "
        "Split the text into natural PHRASES (2-6 words, by pauses). "
        "For EACH PHRASE give the exact start (s) and end (e) time in SECONDS from the audio. "
        "Times must match the voice very precisely (sync is the priority). "
        f"Text must be in {L['note']}. "
        "Return a JSON array: [{\"text\":\"phrase\",\"s\":0.0,\"e\":0.0}].")
_PROMPT_ASR=_prompt_asr("uz")  # orqaga moslik

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
def _prompt_words(lang="uz"):
    L=_lang(lang)
    return (f"This is a short {L['name']} audio clip. Transcribe it VERY accurately. "
        "Return ONLY the words actually spoken in the audio — do NOT return this instruction, "
        "any comment, or extra text. If there is no speech, return an empty array []. "
        "For EACH WORD give the exact start (s) and end (e) time in SECONDS within this clip "
        "(clip start = 0.0). Times must match the voice very precisely. "
        f"Keep word order as in the audio, in {L['note']}. "
        "JSON array: [{\"w\":\"word\",\"s\":0.0,\"e\":0.0}].")
_PROMPT_WORDS=_prompt_words("uz")  # orqaga moslik

# ko'rsatma/echo bo'lib qolishi mumkin bo'lgan tokenlar (xavfsizlik filtri)
_JUNK_TOKENS={"transkripsiya","transkriptsiya","audio","json","massiv","so'z","soz","qaytar","ber","qil","bo'lak","bolak"}

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

def stt_words_gcp(clip_path, lang="uz"):
    """Google Cloud Speech-to-Text v2 (Chirp) — HAQIQIY akustik so'z vaqtlari.
    Qaytaradi: [{w,s,e}] (bo'lakka nisbatan)."""
    code=_lang(lang)["code"]
    token=_vertex_token()
    b64=base64.b64encode(open(clip_path,"rb").read()).decode()
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    errs=[]
    # (region, model) — chirp_2 regional; chirp/long global; oxirida v1 zaxira
    for loc,model in [("us-central1","chirp_2"),("us-central1","chirp"),
                      ("global","chirp_2"),("global","long")]:
        host = "speech.googleapis.com" if loc=="global" else f"{loc}-speech.googleapis.com"
        url=f"https://{host}/v2/projects/{GCP_PROJECT}/locations/{loc}/recognizers/_:recognize"
        body={"config":{"autoDecodingConfig":{},"languageCodes":[code],"model":model,
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
        body={"config":{"languageCode":code,"enableWordTimeOffsets":True,
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

def gemini_words_clip(clip_path, lang="uz"):
    """Gemini'дан bo'lak matni (sifatli) — [{w,s,e}] (vaqt taxminiy)."""
    b64=base64.b64encode(open(clip_path,"rb").read()).decode()
    mime="audio/wav" if clip_path.endswith(".wav") else "audio/mp3"
    txt,model=_vertex_audio_call(b64,mime,_prompt_words(lang))
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
            # gseg-only (STT eshitmagan) -> TASHLAB YUBORAMIZ: bu Gemini qo'shgan/ko'rsatma echo, audioda yo'q
    out.sort(key=lambda x:x["s"]); return out

def _drop_junk(words):
    """Ketma-ket 2+ 'ko'rsatma' tokeni (prompt echo)ни olib tashlaydi. Yakka so'zga tegmaydi."""
    n=len(words); keep=[True]*n; i=0
    while i<n:
        if _norm(words[i]["w"]) in _JUNK_TOKENS:
            j=i
            while j<n and _norm(words[j]["w"]) in _JUNK_TOKENS: j+=1
            if j-i>=2:
                for k in range(i,j): keep[k]=False
            i=j
        else: i+=1
    return [w for k,w in zip(keep,words) if k]

def _clip_words_real(clip, lang="uz"):
    """Bir bo'lak uchun: STT vaqti (aniq) + Gemini matni (sifatli), moslashtirilgan.
    Qaytaradi: (words, src, stt_err) — src: 'stt+gem' | 'stt' | 'gem'."""
    stt=[]; stt_err=""
    try: stt=stt_words_gcp(clip, lang)
    except Exception as ex: stt_err=str(ex)[:140]
    gem=[]
    if STT_TEXT_FROM_GEMINI or not stt:
        try: gem=gemini_words_clip(clip, lang)
        except Exception: gem=[]
    if stt and gem: return _align_text_timing(gem,stt),"stt+gem",stt_err
    if stt: return stt,"stt",stt_err
    gem=_drop_junk(gem) if (lang or "uz").lower()=="uz" else gem   # junk filtri faqat o'zbekcha
    return gem,"gem",stt_err

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

def transcribe_words_vertex(wav, chunk=18.0, overlap=1.5, _clip_fn=None, _dur=None, lang="uz"):
    """Audioni qisqa bo'laklarga bo'lib, HAR BIR SO'Z uchun aniq vaqt oladi (drift kam)."""
    dur=_dur if _dur is not None else ffdur(wav)
    if dur<=0: raise RuntimeError("audio uzunligi 0")
    raw=[]; srcs=set(); stt_err=""
    s=0.0
    while s < dur-0.05:
        e=min(dur, s+chunk)
        if _clip_fn is not None:
            ws=_clip_fn(s,e)
        else:
            clip=wav+f".{int(s*100)}.wav"    # WAV — kodek kechikishi yo'q, sample-aniq
            run(["ffmpeg","-y","-loglevel","error","-ss",f"{s:.2f}","-to",f"{e:.2f}","-i",wav,
                 "-ar","16000","-ac","1","-c:a","pcm_s16le",clip])
            ws,src,e1=_clip_words_real(clip, lang)  # STT vaqti + Gemini matni
            srcs.add(src)
            if e1 and not stt_err: stt_err=e1
            try: os.remove(clip)
            except: pass
        raw.append((s, ws))
        if e>=dur: break
        s += (chunk-overlap)
    words=merge_chunk_words(raw)
    if not words: raise RuntimeError("so'z topilmadi")
    full=" ".join(w["w"] for w in words)
    if _clip_fn is not None:
        eng="VERTEX-word:test"
    elif ("stt" in srcs) or ("stt+gem" in srcs):
        eng="STT+Gemini ✅ (aniq akustik vaqt)"
    else:
        eng=f"⚠️ Gemini-word (STT ISHLAMADI: {stt_err or 'nomaʼlum'})"
    return words, full, eng

def transcribe_vertex(wav, lang="uz"):
    """IBORA darajasidagi zaxira yo'l (bir martalik)."""
    mp3=wav+".mp3"; run(["ffmpeg","-y","-loglevel","error","-i",wav,"-b:a","64k",mp3])
    src=mp3 if os.path.exists(mp3) else wav
    mime="audio/mp3" if src.endswith(".mp3") else "audio/wav"
    b64=base64.b64encode(open(src,"rb").read()).decode()
    txt,model=_vertex_audio_call(b64,mime,_prompt_asr(lang))
    segs=_parse_words(json.loads(txt))
    if segs: return segs," ".join(s["text"] for s in segs),f"VERTEX:{model}"
    raise RuntimeError("bo'sh natija")

def transcribe_gemini(wav, lang="uz"):
    mp3=wav+".mp3"; run(["ffmpeg","-y","-loglevel","error","-i",wav,"-b:a","64k",mp3])
    src=mp3 if os.path.exists(mp3) else wav
    mime="audio/mp3" if src.endswith(".mp3") else "audio/wav"
    b64=base64.b64encode(open(src,"rb").read()).decode()
    body={"contents":[{"parts":[{"text":_prompt_asr(lang)},{"inline_data":{"mime_type":mime,"data":b64}}]}],
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

def transcribe_groq(wav, lang="uz"):
    with open(wav,"rb") as f:
        r=requests.post(GROQ_URL,headers={"Authorization":f"Bearer {GROQ_KEY}"},
            files={"file":(os.path.basename(wav),f,"audio/wav")},
            data={"model":GROQ_MODEL,"language":(lang or "uz").lower(),"temperature":"0","response_format":"verbose_json"},timeout=300)
    r.raise_for_status(); j=r.json(); segs=[]
    for s in j.get("segments",[]):
        t=(s.get("text") or "").strip()
        if t: segs.append({"text":t,"start":float(s["start"]),"end":float(s["end"])})
    return segs,(j.get("text") or "").strip(),"GROQ"

def transcribe(wav, lang="uz"):
    if have_vertex():
        try:
            segs,full,eng=transcribe_vertex(wav, lang)
            if segs: return segs,full,eng
            verr="bo'sh"
        except Exception as e: verr=str(e)[:250]
        segs,full,_=transcribe_groq(wav, lang)
        return segs,full,f"GROQ (VERTEX xato: {verr})"
    if GEMINI_KEY:
        try:
            segs,full,eng=transcribe_gemini(wav, lang)
            if segs: return segs,full,eng
        except Exception as e:
            segs,full,_=transcribe_groq(wav, lang); return segs,full,f"GROQ (gemini xato:{str(e)[:120]})"
    segs,full,_=transcribe_groq(wav, lang)
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

def speech_regions(video, dur):
    """Ovoz bor bo'lgan oraliqlar (VAD) — jimlikni teskari qilib olamiz."""
    sils=detect_silences(video, dB=-35, mind=0.15)   # sezgir
    regions=[]; cur=0.0
    for s,e in sils:
        s=max(0.0,float(s))
        if s>cur+0.02: regions.append((cur,s))
        cur = float(e) if e is not None else dur
    if dur>cur+0.02: regions.append((cur,dur))
    return [(round(a,3),round(b,3)) for a,b in regions if b-a>0.05]

def snap_words_to_speech(words, regions):
    """Har bir OVOZ oralig'ining birinchi so'zini haqiqiy ovoz boshiga yopishtiradi
    (subtitr ovozdan oldin chiqib ketmasin). Faqat sezilarli farqда."""
    if not regions or not words: return words
    for (rs,re) in regions:
        # shu oraliqда boshlanadigan birinchi so'z
        for w in words:
            if rs-0.6 <= w["s"] <= re+0.05:
                if abs(w["s"]-rs) <= 0.7:      # onsetga yaqin bo'lsa
                    w["s"]=round(max(0.0, rs-0.02),3)
                break
    words.sort(key=lambda x:x["s"])
    for i in range(len(words)):
        if words[i]["e"]<=words[i]["s"]: words[i]["e"]=round(words[i]["s"]+0.2,3)
        if i>0 and words[i-1]["e"]>words[i]["s"]: words[i-1]["e"]=words[i]["s"]
    return words

def qc_words(words, dur):
    """Final QC: ustma-ust yo'q, juda qisqa emas, oxiri cho'zilmasin, tartibli."""
    words=[w for w in words if str(w.get("w","")).strip()]
    words.sort(key=lambda x:x["s"])
    out=[]
    for w in words:
        s=max(0.0,min(float(w["s"]),dur)); e=min(float(w["e"]),dur)
        if e-s<0.10: e=min(dur,s+0.10)              # juda qisqa emas
        if e-s>1.6: e=s+1.6                          # bitta so'z 1.6s dan ortiq turmasin
        if out and out[-1]["e"]>s: out[-1]["e"]=round(s,3)   # overlap yo'q
        out.append({"w":w["w"].strip(),"s":round(s,3),"e":round(e,3)})
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
    """Takror/qayta boshlangan (dubl) gaplarning BIRINCHISINI olib tashlaydi.
    Har gapni keyingi 2 gap bilan solishtiradi (orasida to'ldiruvchi bo'lsa ham)."""
    import difflib
    n=len(sent_segs); rem=[]; removed=set()
    toks=[[_norm(x) for x in s["text"].split() if _norm(x)] for s in sent_segs]
    for i in range(n):
        if i in removed: continue
        a=toks[i]
        if len(a)<2: continue
        for j in (i+1, i+2):
            if j>=n or j in removed: continue
            b=toks[j]
            if len(b)<2: continue
            sa,sb=set(a),set(b)
            inter=len(sa&sb)
            jac=inter/max(1,len(sa|sb))
            cont=inter/len(sa)                 # a ning qancha qismi b da bor
            ratio=difflib.SequenceMatcher(None,a,b,autojunk=False).ratio()
            # aniq takror yoki qayta-boshlash -> birinchisini (i) olib tashlaymiz
            if ratio>=0.7 or jac>=0.6 or cont>=0.82:
                rem.append((sent_segs[i]["start"]-0.05, sent_segs[i]["end"]+0.06))
                removed.add(i); break
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

# Cyrillic'ni qo'llaydigan shriftlar (rus tili uchun)
FONT_CYR={"Caveat","DejaVu Sans","Inter","Lobster","Manrope","Montserrat","Nunito",
          "Oswald","Pacifico","Roboto Slab","Rubik","Russo One"}
def pick_font(fam, lang="uz"):
    fam=fam or "Anton"
    if (lang or "uz").lower()=="ru" and fam not in FONT_CYR:
        return "Montserrat"   # rus uchun Cyrillic bo'lgan shriftga o'tamiz
    return fam

def build_ass(words, sub, lang="uz", hook=None):
    """sub = dict: font, size, base, active, outline, border, upper, words, delay, margin_v. hook = boshidagi sarlavha matni."""
    base=hexass(sub.get("base","#FFFFFF")); acc=hexass(sub.get("active","#FFEA00"))
    outl=hexass(sub.get("outline","#000000")); upper=sub.get("upper",True)
    font=pick_font(sub.get("font","Anton"), lang); size=int(sub.get("size",90)); border=sub.get("border",4)
    mv=int(sub.get("margin_v",660)); off=float(sub.get("delay",0.20)); n=int(sub.get("words",3))
    anim=sub.get("anim","pop")
    ANIMS={
        "pop":"{\\fad(50,0)\\fscx90\\fscy90\\t(0,120,\\fscx100\\fscy100)}",
        "fade":"{\\fad(170,90)}",
        "bounce":"{\\fad(40,0)\\fscx55\\fscy55\\t(0,140,\\fscx110\\fscy110)\\t(140,230,\\fscx100\\fscy100)}",
        "rise":"{\\fad(70,0)\\fscy55\\t(0,150,\\fscy100)}",
        "zoomin":"{\\fad(40,0)\\fscx130\\fscy130\\t(0,160,\\fscx100\\fscy100)}",
        "none":"",
    }
    aeff=ANIMS.get(anim, ANIMS["pop"])
    head=f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{size},{base},{base},{outl},&H64000000,-1,0,0,0,100,100,0,0,1,{border},2,2,100,100,{mv},1
Style: Hook,{font},{int(size*1.25)},{acc},{acc},{outl},&H64000000,-1,0,0,0,100,100,0,0,1,{max(5,border+2)},3,8,60,60,{int(H*0.30)},1

[Events]
Format: Layer, Start, End, Style, MarginL, MarginR, MarginV, Effect, Text
"""
    lines0=[]
    if hook:
        htxt=(hook.upper() if upper else hook).replace("{","(").replace("}",")")
        heff="{\\fad(120,180)\\fscx40\\fscy40\\t(0,220,\\fscx108\\fscy108)\\t(220,340,\\fscx100\\fscy100)}"
        lines0.append("Dialogue: 1,%s,%s,Hook,0,0,0,,%s%s"%(ts(0.05),ts(2.4),heff,htxt))
    cues=group_cues(words,n)
    flat=[]
    for cue in cues:
        for wi,w in enumerate(cue): flat.append((cue,wi,w))
    # UZLUKSIZ va USTMA-UST TUSHMAYDIGAN qilib qat'iy tartiblash
    lines=[]; prev_end=-1.0
    for idx,(cue,wi,w) in enumerate(flat):
        start=w["s"]
        nxt=flat[idx+1][2]["s"] if idx+1<len(flat) else None
        if nxt is None:
            end=w["e"]+0.30
        elif (nxt - w["e"]) <= 0.35:
            end=nxt                          # so'zlar zich -> uzluksiz (chirillamaydi)
        else:
            end=w["e"]+0.20                  # katta pauza -> jimlikда OSILIB turmaydi
        start=max(start, prev_end)          # oldingi tugagach boshlanadi -> stacking yo'q
        if end<=start: end=start+0.20
        prev_end=end
        disp=[(x["w"].upper() if upper else x["w"]).replace("{","(").replace("}",")") for x in cue]
        parts=["{\\c%s}%s{\\c%s}"%(acc,d,base) if j==wi else d for j,d in enumerate(disp)]
        ov=aeff if wi==0 else ""
        lines.append("Dialogue: 0,%s,%s,Main,0,0,0,,%s%s"%(ts(start+off),ts(end+off),ov," ".join(parts)))
    return head+"\n".join(lines0+lines)+"\n"

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

# ================= EMOJI STIKERLAR (kalit so'zga) =================
_EMOJI_FONT = None
for _p in ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
           "/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf"):
    if os.path.exists(_p): _EMOJI_FONT = _p; break

# kalit so'z (uz/ru/en) -> emoji
EMOJI_MAP = {
 "🔥":["olov","zo'r","zор","fire","огонь","круто","гуруч","hot","top","зажиг"],
 "💰":["pul","daromad","biznes","money","деньги","бизнес","доход","profit","sotuv","boylik"],
 "❤️":["sevgi","yaxshi","love","любовь","сердце","like","yoqdi"],
 "😂":["kulgi","kulish","haha","смех","funny","прикол"],
 "🚀":["tez","o'sish","boshla","rost","launch","рост","старт","быстро","success","muvaffaqiyat"],
 "✅":["to'g'ri","bo'ldi","tayyor","done","готово","правильно","ok","ha"],
 "💡":["g'oya","fikr","idea","идея","совет","maslahat","bil"],
 "🎯":["maqsad","aniq","target","цель","focus","natija","result"],
 "⭐":["eng","zo'r","best","лучший","top","reyting","yulduz","star"],
 "👀":["qara","ko'r","look","смотри","внимание","diqqat"],
 "💪":["kuch","sport","mashq","gym","сила","тренировка","fitnes","strong"],
 "🤯":["hayron","wow","вау","шок","ajoyib","aql"],
 "🎉":["bayram","tabrik","party","праздник","yutuq","win"],
 "📈":["o'sish","grafik","statistika","growth","график","рост","ko'paydi"],
 "🕐":["vaqt","tez","daqiqa","time","время","soat"],
 "🎬":["video","montaj","kino","film","съёмка","montaj"],
 "📱":["telefon","ilova","app","телефон","instagram","tiktok","reels"],
 "🍔":["ovqat","taom","food","еда","restoran","yeb"],
 "✈️":["sayohat","travel","путешествие","dam","sayohat"],
 "🧠":["aql","miya","o'yla","brain","мозг","думай","bilim"],
}
def _norm_kw(s): return re.sub(r"[^\w']","",(s or "").lower())

def _emoji_png(ch, dest, px=240):
    if not _EMOJI_FONT: return False
    try:
        from PIL import Image, ImageFont, ImageDraw
        font=ImageFont.truetype(_EMOJI_FONT, 109)
        img=Image.new("RGBA",(150,150),(0,0,0,0))
        ImageDraw.Draw(img).text((8,8), ch, font=font, embedded_color=True)
        bb=img.getbbox()
        if bb: img=img.crop(bb)
        img=img.resize((px,px), Image.LANCZOS)
        img.save(dest); return True
    except Exception:
        return False

def emoji_plan(words, workdir, n=8, min_gap=1.6):
    """Transkript so'zlariga mos emoji stikerlar rejasi. -> [{path,time,dur,x,y,size}]"""
    if not _EMOJI_FONT or not words: return []
    # kalit so'z -> emoji
    hits=[]
    for w in words:
        kw=_norm_kw(w.get("w",""))
        if len(kw)<3: continue
        for emo, keys in EMOJI_MAP.items():
            if any(kw==k or (len(k)>=4 and k in kw) for k in keys):
                hits.append((float(w.get("s",0)), emo)); break
    # tarqatish (min gap)
    plan=[]; last=-99; import random
    for s,emo in sorted(hits):
        if s-last<min_gap: continue
        last=s; plan.append((s,emo))
        if len(plan)>=n: break
    out=[]; made={}
    poss=[(0.20,0.30),(0.78,0.32),(0.24,0.66),(0.80,0.64),(0.5,0.24)]
    for i,(s,emo) in enumerate(plan):
        code="_".join(f"{ord(c):x}" for c in emo)
        p=made.get(emo)
        if not p:
            p=os.path.join(workdir, f"emo_{code}.png")
            if not os.path.exists(p):
                if not _emoji_png(emo, p): continue
            made[emo]=p
        px_,py_=poss[i%len(poss)]
        out.append({"path":p,"time":round(s,2),"dur":1.4,"x":px_,"y":py_,"size":0.15})
    return out

def hook_text(full_text, lang="uz"):
    t=(full_text or "").strip().split()
    if len(t)>=3: return " ".join(t[:4])
    return {"uz":"KO'RIB CHIQING","ru":"СМОТРИ ДО КОНЦА","en":"WATCH THIS"}.get(lang,"WATCH THIS")

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

# rang bahosi (color grade) — montaj stilistikasi
GRADES={
 "clean":"eq=contrast=1.03:saturation=1.02",
 "vivid":"eq=contrast=1.06:saturation=1.18",
 "warm":"colorbalance=rs=0.06:gs=0.02:bs=-0.06,eq=saturation=1.08",
 "cool":"colorbalance=rs=-0.05:bs=0.08,eq=saturation=1.05",
 "cinema":"colorbalance=rs=0.05:bs=-0.04:rm=-0.03:bm=0.05,eq=contrast=1.08:saturation=1.05",
 "mono":"hue=s=0,eq=contrast=1.1",
 "bright":"eq=brightness=0.05:contrast=1.04:saturation=1.12",
 "moody":"eq=contrast=1.13:saturation=0.9:brightness=-0.03",
 "film":"curves=preset=medium_contrast,eq=saturation=0.95",
 "neon":"eq=contrast=1.1:saturation=1.25,colorbalance=bs=0.05:rm=0.03",
 "none":"",
}
_ZOOM={0:0.0, 1:0.0004, 2:0.0009}  # zoom tezligi
_ZMAX={0:1.0, 1:1.05, 2:1.12}

QMAP={"480":(480,854,23),"540":(540,960,24),"720":(720,1280,21),"1080":(1080,1920,20),"1k":(1080,1920,20)}

# ================= MONTAJ EFFEKTLARI (har uslub o'z ko'rinishi) =================
def _fx_parts(fx, il, ol, tw, th, dur, strong=1):
    """il->ol oralig'ida effekt filtri. strong=0 (yumshoq)/1/2 (kuchli). Bitta oqim (split ishlatilsa ichida)."""
    fx=(fx or "none").lower(); d=max(0.5,float(dur or 3.0))
    s=1.0+0.35*strong  # kuchaytirish
    if fx in ("none",""):
        return [f"{il}null{ol}"]
    if fx=="glow":                       # neon/yorug' porlash
        sg=2.5*s
        return [f"{il}split[gA][gB];[gB]gblur=sigma={sg:.1f},eq=brightness=0.05:saturation=1.25[gbb];[gA][gbb]blend=all_mode=screen:all_opacity={0.5+0.15*strong:.2f}{ol}"]
    if fx=="grain":                      # kino donadorligi + vinetka
        return [f"{il}noise=alls={int(7*s)}:allf=t,curves=preset=medium_contrast,vignette=PI/{5- strong}{ol}"]
    if fx=="leak":                       # harakatlanuvchi yorug' oqim (light-leak)
        w=int(tw*0.34); a=0.10+0.06*strong
        return [f"{il}drawbox=x='iw*0.5-{w//2}+iw*0.33*sin(t*0.9)':y=0:w={w}:h=ih:color=0xffcaa0@{a:.2f}:t=fill,eq=saturation=1.15{ol}"]
    if fx=="glitch":                     # RGB siljish + shovqin (texno/game)
        rh=int(3+2*strong)
        return [f"{il}rgbashift=rh={rh}:bh=-{rh}:gv={rh//2},noise=alls={int(5*s)}:allf=t,curves=preset=strong_contrast{ol}"]
    if fx=="shake":                      # kamera silkinishi
        z=1.04+0.02*strong; amp=5+3*strong
        return [f"{il}scale=iw*{z:.2f}:ih*{z:.2f},crop={tw}:{th}:x='(iw-{tw})/2+{amp}*sin(t*25)':y='(ih-{th})/2+{amp}*cos(t*23)'{ol}"]
    if fx=="letterbox":                  # kino qora chiziqlari + rang
        bh=int(th*0.10)
        return [f"{il}curves=preset=medium_contrast,eq=saturation=1.08,drawbox=x=0:y=0:w=iw:h={bh}:color=black:t=fill,drawbox=x=0:y=ih-{bh}:w=iw:h={bh}:color=black:t=fill{ol}"]
    if fx=="flash":                      # yorug'lik puls (fitnes/hype)
        return [f"{il}eq=brightness='0.14*max(0\\,sin(t*9))':saturation=1.2{ol}"]
    if fx=="progress":                   # pastda progress-bar
        h=int(th*0.012)+4
        return [f"{il}drawbox=x=0:y=ih-{h}:w='iw*mod(t\\,{d:.2f})/{d:.2f}':h={h}:color=white@0.9:t=fill{ol}"]
    if fx=="vhs":                        # retro VHS
        return [f"{il}rgbashift=rh=3:bh=-3,noise=alls={int(9*s)}:allf=t,curves=g='0/0 0.5/0.58 1/1',eq=saturation=0.85:contrast=1.1{ol}"]
    if fx=="duotone":                    # ikki tonli (fashion/street)
        return [f"{il}hue=s=0,curves=r='0/0.05 1/1':b='0/0.15 1/0.9',eq=contrast=1.15:saturation=1.4{ol}"]
    if fx=="dream":                      # yumshoq bloom (beauty/story)
        return [f"{il}split[dA][dB];[dB]gblur=sigma={3*s:.1f}[dbb];[dA][dbb]blend=all_mode=lighten:all_opacity=0.4,eq=saturation=1.1:brightness=0.03{ol}"]
    return [f"{il}null{ol}"]

FX_MAP={  # oila -> effekt
 "viral":"glow","bold":"flash","neon":"glow","clean":"none","cinema":"letterbox",
 "vlog":"progress","biz":"progress","pod":"progress","fashion":"leak","fit":"shake",
 "hype":"glitch","luxe":"grain","retro":"vhs","game":"glitch","story":"dream",
 "news":"progress","beauty":"dream","street":"duotone","tech":"glitch","calm":"none",
}

def render_final(cut, ass_path, outp, brolls, zoom=True, audio_clean=True, grade="vivid", zoom_level=1,
                 quality="1080", fast=False, fx="none", stickers=None):
    """brolls=[...] (Ken Burns), stickers=[{path,time,dur,x,y,size}] (emoji pop). fx=montaj effekti."""
    stickers = stickers or []
    tw,th,crf = QMAP.get(str(quality).lower(), (W,H,20))
    ae=ass_path.replace("\\","/").replace(":","\\:"); fd=FONTS_DIR.replace("\\","/").replace(":","\\:")
    sub=f"ass='{ae}':fontsdir='{fd}'"
    lvl=0 if not zoom else int(zoom_level if zoom_level in (0,1,2) else 1)
    g=GRADES.get(grade, GRADES["vivid"]); geq=("," + g) if g else ""
    dur=_probe_dur(cut) or 3.0
    strong = 2 if lvl>=2 else 1
    # 1) asosiy oqim (scale/crop/zoom/grade) -> [v0]
    if lvl>0:
        spd=_ZOOM[lvl]; zmx=_ZMAX[lvl]
        base0=(f"[0:v]scale={int(tw*1.14)}:{int(th*1.14)}:force_original_aspect_ratio=increase,crop={int(tw*1.14)}:{int(th*1.14)},"
               f"zoompan=z='min(1.0+{spd}*in,{zmx})':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={tw}x{th}:fps=30"
               f"{geq},setsar=1[v0]")
    else:
        base0=f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th}{geq},setsar=1[v0]"
    # 2) effekt [v0]->[vfx]
    fxparts=_fx_parts(fx, "[v0]", "[vfx]", tw, th, dur, strong)
    # 3) subtitr [vfx]->[base]
    subpart=f"[vfx]{sub}[base]"
    af=("highpass=f=85,afftdn=nr=12,equalizer=f=3000:t=q:w=1.5:g=3,acompressor=threshold=-18dB:ratio=3,"
        "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000") if audio_clean else "aresample=48000"
    parts=[base0]+fxparts+[subpart]; cur="[base]"; inputs=["-i",cut]
    idx=0
    # --- B-ROLL: Ken Burns (sekin zoom/pan) ---
    for k,b in enumerate(brolls):
        p=b["path"]; s=float(b["time"]); d=float(b.get("dur",2.5))
        y=float(b.get("y",0.72)); bw=int(tw*float(b.get("w",0.78))); bh=int(bw*0.62); fr=max(1,int(d*30))
        inputs+=["-loop","1","-t",f"{d}","-i",p]; idx+=1
        parts.append(f"[{idx}:v]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
                     f"zoompan=z='min(zoom+0.0016,1.14)':d={fr}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={bw}x{bh}:fps=30,"
                     f"format=yuva420p,fade=t=in:st=0:d=0.3:alpha=1,fade=t=out:st={max(0.01,d-0.3):.2f}:d=0.3:alpha=1,"
                     f"setpts=PTS+{s:.2f}/TB[ov{k}]")
        nxt=f"[vb{k}]"
        parts.append(f"{cur}[ov{k}]overlay=(W-w)/2:{int(th*y)}:enable='between(t,{s:.2f},{s+d:.2f})':eof_action=pass:repeatlast=0{nxt}")
        cur=nxt
    # --- EMOJI/ELEMENT STIKERLAR: turli animatsiya (perexod) ---
    for k,st in enumerate(stickers):
        p=st["path"]; s=float(st["time"]); d=float(st.get("dur",1.4))
        sz=int(tw*float(st.get("size",0.15))); fr=max(1,int(d*30))
        x=int(tw*float(st.get("x",0.5))-sz/2); y=int(th*float(st.get("y",0.4))-sz/2)
        anim=str(st.get("anim","pop"))
        inputs+=["-loop","1","-t",f"{d}","-i",p]; idx+=1
        pre=f"[{idx}:v]scale={sz}:{sz},"
        zp=f"zoompan=z='if(lte(on,6),1.35-0.058*on,1.0)':d={fr}:s={sz}x{sz}:fps=30,"   # pop (default)
        if anim=="bounce": zp=f"zoompan=z='if(lte(on,4),1.6-0.15*on,if(lte(on,9),1.0+0.04*(9-on),1.0))':d={fr}:s={sz}x{sz}:fps=30,"
        elif anim=="grow": zp=f"zoompan=z='min(1.0+0.02*on,1.25)':d={fr}:s={sz}x{sz}:fps=30,"
        elif anim=="pulse": zp=f"zoompan=z='1.0+0.08*sin(on*0.6)':d={fr}:s={sz}x{sz}:fps=30,"
        elif anim in ("fade","slidel","slider","drop"): zp=""   # bu animatsiyalar overlay/fadeda
        fadea="format=yuva420p,fade=t=in:st=0:d=0.12:alpha=1,fade=t=out:st=%.2f:d=0.22:alpha=1,"%max(0.01,d-0.22)
        if anim=="fade": fadea="format=yuva420p,fade=t=in:st=0:d=0.3:alpha=1,fade=t=out:st=%.2f:d=0.3:alpha=1,"%max(0.01,d-0.3)
        parts.append(f"{pre}{zp}{fadea}setpts=PTS+{s:.2f}/TB[stk{k}]")
        # overlay joyi (slide/drop uchun harakat)
        ox=str(x); oy=str(y)
        if anim=="slidel": ox=f"'{x}+{sz}*0.6*max(0,1-(t-{s:.2f})/0.3)'"
        elif anim=="slider": ox=f"'{x}-{sz}*0.6*max(0,1-(t-{s:.2f})/0.3)'"
        elif anim=="drop": oy=f"'{y}-{sz}*0.6*max(0,1-(t-{s:.2f})/0.3)'"
        nxt=f"[sv{k}]"
        parts.append(f"{cur}[stk{k}]overlay={ox}:{oy}:enable='between(t,{s:.2f},{s+d:.2f})':eof_action=pass:repeatlast=0{nxt}")
        cur=nxt
    parts.append(f"[0:a]{af}[aout]")
    fc=";".join(parts)
    preset="veryfast" if fast else "medium"
    cmd=["ffmpeg","-y","-hide_banner","-loglevel","error"]+inputs+[
        "-filter_complex",fc,"-map",cur,"-map","[aout]",
        "-r","30","-c:v","libx264","-preset",preset,"-crf",str(crf),"-pix_fmt","yuv420p",
        "-movflags","+faststart","-c:a","aac","-b:a","160k",outp]
    c,_=run(cmd)
    return c==0 and os.path.exists(outp)

def filmstrip(src, outp, n=None, h=96):
    """Videodan sekundma-sekund kadrlar — 1 qatorli sprite rasm (aniq). Kadrlar sonini qaytaradi."""
    dur = _probe_dur(src) or 1.0
    N = n or min(80, max(6, int(round(dur*1.5))))
    fps = max(0.05, N/dur)
    vf = f"fps={fps:.5f},scale=-2:{int(h)}:flags=bicubic,tile={N}x1"
    c,_ = run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",src,"-frames:v","1","-vf",vf,"-q:v","2",outp])
    if c==0 and os.path.exists(outp) and os.path.getsize(outp)>500:
        return N
    return 0

def _probe_dur(path):
    try:
        c,o=run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=nw=1:nk=1",path], capture=True)
        return float((o or "0").strip().splitlines()[0])
    except Exception:
        return 0.0

def render_preview(src, words, sub, outp, grade="vivid", zoom_level=1, lang="uz", start=None, dur=4.5, workdir=None, fx="none"):
    """Uslub namunasi: foydalanuvchi videosidan qisqa (~4.5s) klip, tanlangan uslubda. Tez (540p)."""
    wd = workdir or os.path.dirname(outp) or "."
    d = _probe_dur(src)
    if start is None:
        start = 0.0
        for w in (words or []):
            try:
                if float(w.get("s",0)) >= 0.8:
                    start = max(0.0, float(w["s"]) - 0.25); break
            except Exception:
                continue
    if d and start + dur > d:
        start = max(0.0, d - dur)
    if d and dur > d:
        dur = max(1.0, d)
    end = start + dur
    trim = os.path.join(wd, "prev_trim.mp4")
    c,_ = run(["ffmpeg","-y","-hide_banner","-loglevel","error","-ss",f"{start:.2f}","-i",src,
               "-t",f"{dur:.2f}","-c:v","libx264","-preset","veryfast","-crf","24",
               "-c:a","aac","-b:a","128k","-r","30",trim])
    if c != 0 or not os.path.exists(trim):
        return False
    rw=[]
    for w in (words or []):
        try:
            s=float(w.get("s",0)); e=float(w.get("e",s+0.3))
        except Exception:
            continue
        if e < start or s > end: continue
        rw.append({"w":w.get("w",""), "s":round(max(0.0,s-start),2), "e":round(max(0.08,e-start),2)})
    ass = os.path.join(wd, "prev.ass")
    open(ass,"w",encoding="utf-8").write(build_ass(rw, sub, lang))
    return render_final(trim, ass, outp, [], zoom=zoom_level>0, audio_clean=False,
                        grade=grade, zoom_level=zoom_level, quality="540", fast=True, fx=fx)

# ================= YUQORI DARAJADAGI BOSQICHLAR =================
def analyze(video_path, work, lang="uz"):
    """Transkripsiya + kesish takliflari. Qaytaradi: dict."""
    wav=os.path.join(work,"a16k.wav")
    run(["ffmpeg","-y","-loglevel","error","-i",video_path,"-ar","16000","-ac","1",wav])
    dur=ffdur(video_path)
    eng=""
    words=None
    if have_vertex():
        try:
            words, full, eng = transcribe_words_vertex(wav, lang=lang)   # QISQA BO'LAKLI, har so'z aniq
            words = normalize_words([{"text":w["w"],"start":w["s"],"end":w["e"]} for w in words], dur)
        except Exception as e:
            eng=f"(word-level xato: {str(e)[:80]})"; words=None
    if not words:                                  # zaxira: ibora darajasi + tekis taqsim
        segs,full,eng2=transcribe(wav, lang)
        segs=normalize_segments(segs,dur)
        words=words_from_segments(segs)
        eng=(eng+" "+eng2).strip()
    # VAD snap + final QC (ovozdan oldin chiqmasin, cho'zilmasin, overlap yo'q)
    try:
        regs=speech_regions(video_path, dur)
        words=snap_words_to_speech(words, regs)
    except Exception:
        pass
    words=qc_words(words, dur)
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
