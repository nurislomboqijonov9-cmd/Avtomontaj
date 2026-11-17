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
    full=ffdur(inp)
    if not segs or (len(segs)==1 and segs[0][0]<=0.02 and segs[0][1]>=full-0.06):
        shutil.copy(inp,outp); return [(0,full)]
    sel="+".join("between(t,%.3f,%.3f)"%(a,b) for a,b in segs)
    fc=("[0:v]select='%s',setpts=N/FRAME_RATE/TB[v];"
        "[0:a]aselect='%s',asetpts=N/SR/TB[a]"%(sel,sel))
    c,_=run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",inp,
        "-filter_complex",fc,"-map","[v]","-map","[a]",
        "-c:v","libx264","-preset","veryfast","-crf","20","-c:a","aac","-b:a","160k",outp])
    if c!=0: shutil.copy(inp,outp)
    return segs

def ffsize(p):
    c,o=run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height","-of","csv=p=0:s=x",p],capture=True)
    try: w,h=o.strip().split("\n")[0].split("x")[:2]; return int(w),int(h)
    except Exception: return 1080,1920

def _atempo(v):
    """atempo 0.5..2 oralig'ida ishlaydi -> zanjir qilib ulanadi."""
    out=[]; v=max(0.1,min(10.0,float(v)))
    while v>2.0: out.append("atempo=2.0"); v/=2.0
    while v<0.5: out.append("atempo=0.5"); v/=0.5
    out.append("atempo=%.4f"%v); return ",".join(out)

def cut_video_speed(inp,outp,segs,work=None):
    """segs=[(a,b,tezlik)] — har bo'lak o'z tezligida (0.25x..4x) kesilib, ketma-ket ulanadi."""
    work=work or os.path.dirname(outp); parts=[]
    for i,sg in enumerate(segs):
        a,b,v=sg[0],sg[1],sg[2]; rev=len(sg)>3 and sg[3] and (b-a)<=30   # teskari o'ynatish (30s gacha bo'lak)
        v=max(0.25,min(4.0,float(v or 1))); pp=os.path.join(work,"seg_%03d.mp4"%i)
        c,_=run(["ffmpeg","-y","-hide_banner","-loglevel","error","-ss","%.3f"%a,"-to","%.3f"%b,"-i",inp,
            "-vf",("reverse," if rev else "")+"setpts=PTS/%.4f"%v,"-af",("areverse," if rev else "")+_atempo(v),"-r","30","-c:v","libx264","-preset","veryfast","-crf","18",
            "-pix_fmt","yuv420p","-c:a","aac","-ar","48000","-ac","2","-b:a","160k",pp])
        if c==0 and os.path.exists(pp) and os.path.getsize(pp)>500: parts.append(pp)
    if not parts: return False
    lst=os.path.join(work,"seg_list.txt")
    open(lst,"w").write("".join("file '%s'\n"%x.replace("'","'\\''") for x in parts))
    c,_=run(["ffmpeg","-y","-hide_banner","-loglevel","error","-f","concat","-safe","0","-i",lst,
             "-c:v","copy","-c:a","aac","-ar","48000","-b:a","160k","-movflags","+faststart",outp])
    for x in parts+[lst]:
        try: os.remove(x)
        except Exception: pass
    return c==0 and os.path.exists(outp)

def append_clip(cur, new, outp):
    """Yangi klipni joriy video oxiriga ulaydi (o'lcham/kadr/audio bir xil qilinadi)."""
    w,h=ffsize(cur)
    def _has_audio(pth):
        c,o=run(["ffprobe","-v","error","-select_streams","a","-show_entries","stream=index","-of","csv=p=0",pth],capture=True)
        return bool((o or "").strip())
    a0="[0:a]" if _has_audio(cur) else None; a1="[1:a]" if _has_audio(new) else None
    extra=[]
    if not (a0 and a1): extra=["-f","lavfi","-i","anullsrc=r=48000:cl=stereo"]
    def _aud(lbl,src,dur_):
        return (f"{src}aresample=48000,aformat=channel_layouts=stereo[{lbl}];" if src else f"[2:a]atrim=0:{dur_:.3f}[{lbl}];")
    fc=(f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30,setsar=1,format=yuv420p[v1];"
        f"[0:v]fps=30,setsar=1,format=yuv420p[v0];"+_aud("a0",a0,ffdur(cur))+_aud("a1",a1,ffdur(new))+"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    c,_=run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",cur,"-i",new]+extra+["-filter_complex",fc,"-map","[v]","-map","[a]",
             "-c:v","libx264","-preset","veryfast","-crf","18","-c:a","aac","-b:a","160k","-movflags","+faststart",outp])
    return c==0 and os.path.exists(outp)
    fc=(f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30,setsar=1,format=yuv420p[v1];"
        f"[0:v]fps=30,setsar=1,format=yuv420p[v0];[0:a]aresample=48000,aformat=channel_layouts=stereo[a0];"
        f"[1:a]aresample=48000,aformat=channel_layouts=stereo[a1];[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    c,_=run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",cur,"-i",new,"-filter_complex",fc,"-map","[v]","-map","[a]",
             "-c:v","libx264","-preset","veryfast","-crf","18","-c:a","aac","-b:a","160k","-movflags","+faststart",outp])
    return c==0 and os.path.exists(outp)

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
def split_rows(n):
    """n ta so'zni 3 qatorga (kam bo'lsa 1-2 qatorga) taqsimlaydi; ortiqchasi o'rta qatorga."""
    if n<=0: return []
    if n==1: return [1]
    if n==2: return [1,1]
    a=n//3; r=n%3; c=[a,a,a]
    if r>=1: c[1]+=1
    if r==2: c[2]+=1
    return c

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

def build_ass(words, sub, lang="uz", hook=None, res=None):
    """sub = dict: font, size, base, active, outline, border, upper, words, delay, margin_v. hook = boshidagi sarlavha matni."""
    base=hexass(sub.get("base","#FFFFFF")); acc=hexass(sub.get("active","#FFEA00"))
    outl=hexass(sub.get("outline","#000000")); upper=sub.get("upper",True)
    font=pick_font(sub.get("font","Anton"), lang); size=int(sub.get("size",90)); border=sub.get("border",4)
    mv=int(sub.get("margin_v",660)); off=float(sub.get("delay",0.20)); n=int(sub.get("words",3))
    anim=sub.get("anim","pop")
    stack=(sub.get("layout")=="stack3")
    lscale=[float(x) for x in (sub.get("line_scale") or [0.62,1.0,0.78])][:3] or [1.0]
    lcols=[hexass(sub.get(k) or sub.get("base","#FFFFFF")) for k in ("c1","c2","c3")]
    ANIMS={
        "pop":"{\\fad(50,0)\\fscx90\\fscy90\\t(0,120,\\fscx100\\fscy100)}",
        "fade":"{\\fad(170,90)}",
        "bounce":"{\\fad(40,0)\\fscx55\\fscy55\\t(0,140,\\fscx110\\fscy110)\\t(140,230,\\fscx100\\fscy100)}",
        "rise":"{\\fad(70,0)\\fscy55\\t(0,150,\\fscy100)}",
        "zoomin":"{\\fad(40,0)\\fscx130\\fscy130\\t(0,160,\\fscx100\\fscy100)}",
        "none":"",
    }
    aeff=ANIMS.get(anim, ANIMS["pop"])
    Wr,Hr=res or (W,H)   # kadr formati (9:16, 1:1, 16:9, 4:5)
    head=f"""[Script Info]
ScriptType: v4.00+
PlayResX: {Wr}
PlayResY: {Hr}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{size},{base},{base},{outl},&H64000000,-1,0,0,0,100,100,0,0,1,{border},2,2,100,100,{mv},1
Style: Hook,{font},{int(size*1.25)},{acc},{acc},{outl},&H64000000,-1,0,0,0,100,100,0,0,1,{max(5,border+2)},3,8,60,60,{int(Hr*0.30)},1

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
        ov=aeff if wi==0 else ""
        if stack:
            # UCH POG'ONALI: so'zlar 3 qatorga bo'linadi, har qator o'z o'lchami/rangida, aytilayotgan so'z ajralib turadi
            rows=[]; k=0
            for li,cnt in enumerate(split_rows(len(disp))):
                fs=max(10,int(round(size*lscale[min(li,len(lscale)-1)]))); lc=lcols[min(li,len(lcols)-1)]
                toks=[]
                for j in range(k,k+cnt):
                    toks.append("{\\c%s}%s{\\c%s}"%(acc,disp[j],lc) if j==wi else disp[j])
                rows.append("{\\fs%d\\c%s}%s"%(fs,lc," ".join(toks))); k+=cnt
            text="\\N".join(rows)
        else:
            parts=["{\\c%s}%s{\\c%s}"%(acc,d,base) if j==wi else d for j,d in enumerate(disp)]
            text=" ".join(parts)
        lines.append("Dialogue: 0,%s,%s,Main,0,0,0,,%s%s"%(ts(start+off),ts(end+off),ov,text))
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

# Diniy va nozik mavzular: bunday joylarga rasm/stiker QO'YILMAYDI (tasvirlash mumkin emas yoki o'rinsiz)
_SENS=re.compile(r"(payg.?ambar|пайғамбар|пророк|prophet|rasul|расул|muhammad|мухаммад|муҳаммад|alloh|аллоҳ|аллах|\ballah|xudo\b|худо|парвардигор|parvardigor|"
                 r"qur.?on|қуръон|коран|quran|oyat|оят|hadis|ҳадис|хадис|sahoba|саҳоба|сахаб|farishta|фаришта|ангел|jannat|жаннат|do.?zax|дўзах|"
                 r"namoz|намоз|намаз|masjid|масжид|мечет|\biso\b|\bmuso\b|ibrohim|\bnabi|наби[йя]|s\.?a\.?v|\(sav\)|vafot|вафот|\bo.?lim\b|ўлим|janoza|жаноза|qabr|қабр)",re.I)
_SENS_EN=re.compile(r"(prophet|muhammad|allah|\bgod\b|deity|angel|quran|koran|mosque interior with people praying|jesus|moses|saint|holy figure|religious figure|heaven|hell|funeral|corpse|grave)",re.I)
def is_sensitive(text): return bool(_SENS.search(text or ""))

def _repeat_rules(words):
    """AIsiz zaxira: ketma-ket takrorlangan 2-5 so'zli bo'laklarning birinchisini topadi. -> [(i0,i1)] (so'z indekslari, i1 kiradi)"""
    nw=[_norm(w["w"]) for w in words]; out=[]; i=0
    while i<len(nw):
        hit=0
        for L in (5,4,3,2):
            if i+2*L<=len(nw) and all(nw[i+k] and nw[i+k]==nw[i+L+k] for k in range(L)) and words[i+L]["s"]-words[i+L-1]["e"]<2.5:
                hit=L; break
        if hit: out.append((i,i+hit-1)); i+=hit
        else: i+=1
    return out

def smart_cut(words, lang="uz"):
    """Montajchi ko'zi bilan: qayta boshlangan gaplar, takror dubl, to'ldiruvchi iboralarni topadi; sarlavha va urg'u joylarini beradi.
    -> {"removes":[[s,e,sabab]], "hook":str, "emph":[[s,e]], "source":"ai"|"rules"}"""
    if not words: return {"removes":[],"hook":"","emph":[],"cards":[],"source":"rules"}
    def rng(i0,i1):
        i0=max(0,min(len(words)-1,int(i0))); i1=max(i0,min(len(words)-1,int(i1)))
        return i0,i1
    if have_vertex() or GEMINI_KEY:
        try:
            toks=[{"i":i,"w":w["w"],"t":round(w["s"],1)} for i,w in enumerate(words)][:1500]
            prompt=("Sen professional video montajchisan (Reels/Shorts). Quyida gapirilgan videoning so'zlari tartib raqami (i) va vaqti (t) bilan berilgan.\n"
                    +json.dumps(toks,ensure_ascii=False)+"\n"
                    "VAZIFA: videoni zich va ravon qilish uchun OLIB TASHLANADIGAN joylarni top:\n"
                    "1) qayta boshlangan gap (so'zlovchi adashib, gapni boshidan aytgan) — BIRINCHI, chala urinishni olib tashla, oxirgi to'liq variantni qoldir;\n"
                    "2) ikki marta aytilgan dubl — birinchisini olib tashla;\n"
                    "3) ma'nosiz to'ldiruvchilar: eee, hmm, xo'sh, demak-demak, 'nima desam ekan' kabi;\n"
                    "4) mavzuga aloqasi yo'q chetga chiqishlar (faqat aniq bo'lsa).\n"
                    "MUHIM: mazmunni buzma, gap ma'nosi uzilmasin, shubha bo'lsa OLIB TASHLAMA. Diniy iboralar, duolar va ismlarga tegma.\n"
                    "Shuningdek: 'hook' = video boshiga qo'yiladigan 3-6 so'zli qiziqtiruvchi sarlavha (video tilida, mazmunga mos, clickbait yolg'onisiz); "
                    "'emph' = eng muhim 3-6 ta ibora (urg'u/zoom uchun) — har biri [boshlanish_i, tugash_i]; "
                    "'cards' = ekranga animatsiyali kartochka bo'lib chiqadigan 4-7 ta KALIT SO'Z: har biri {\"from\":i,\"to\":i,\"text\":\"1-3 so'zli qisqa ibora (video tilida, aytilgan gapdan)\",\"emoji\":\"mos bitta emoji yoki bo'sh\"}. "
                    "Raqamlar, sanab o'tilgan bandlar, asosiy tushunchalar va natijalarni tanla; diniy shaxs yoki muqaddas iboralarga kartochka qo'yma. Video bo'ylab tarqat.\n"
                    "FAQAT JSON qaytar: {\"remove\":[{\"from\":i,\"to\":i,\"why\":\"qisqa sabab\"}],\"hook\":\"...\",\"emph\":[[i,i]],\"cards\":[{\"from\":i,\"to\":i,\"text\":\"...\",\"emoji\":\"...\"}]}")
            d=json.loads(_ai_json(prompt)); rem=[]; total=words[-1]["e"]-words[0]["s"]; cut=0.0
            for x in (d.get("remove") or []):
                i0,i1=rng(x.get("from",0),x.get("to",0))
                if is_sensitive(" ".join(w["w"] for w in words[i0:i1+1])): continue
                a=max(0.0,words[i0]["s"]-0.04); b=words[i1]["e"]+0.04
                if b-a<0.12 or b-a>25: continue
                cut+=b-a
                if cut>total*0.45: break                         # xavfsizlik: videoning yarmidan ko'pini kesmaymiz
                rem.append([round(a,2),round(b,2),str(x.get("why") or "takror")[:60]])
            em=[]
            for x in (d.get("emph") or [])[:8]:
                try: i0,i1=rng(x[0],x[1])
                except Exception: continue
                em.append([round(words[i0]["s"],2),round(words[i1]["e"],2)])
            cards=[]
            for x in (d.get("cards") or [])[:8]:
                try: i0,i1=rng(x.get("from",0),x.get("to",0))
                except Exception: continue
                tx=" ".join(str(x.get("text") or "").split())[:40]
                if not tx or is_sensitive(tx) or is_sensitive(" ".join(w["w"] for w in words[max(0,i0-4):i1+5])): continue
                cards.append({"s":round(words[i0]["s"],2),"e":round(words[i1]["e"],2),"text":tx,"emoji":str(x.get("emoji") or "")[:4]})
            return {"removes":rem,"hook":str(d.get("hook") or "").strip()[:60],"emph":em,"cards":cards,"source":"ai"}
        except Exception:
            pass
    rem=[[round(max(0.0,words[a]["s"]-0.04),2),round(words[b]["e"]+0.04,2),"takror"] for a,b in _repeat_rules(words)]
    return {"removes":rem,"hook":"","emph":[],"cards":[],"source":"rules"}

def broll_suggest(full_text, dur, n=4, words=None):
    """B-roll g'oyalari. words berilsa, har bir g'oya aniq gapga bog'lanadi va diniy/nozik gaplar tashlab ketiladi."""
    rules=("QOIDALAR: uslub jiddiy va hujjatli — fotorealistik, kinematik; multfilm, bolalarcha yoki hazil rasm EMAS. Rasm ichida MATN/YOZUV bo'lmasin. "
           "Faqat aniq narsa, joy yoki harakat tilga olingan gaplarga rasm ber; umumiy, mavhum yoki his-tuyg'u haqidagi gaplarga berma. "
           "HECH QACHON Alloh, payg'ambarlar, farishtalar, sahobalar yoki boshqa muqaddas shaxslarni tasvirlama; diniy shaxs yoki muqaddas matn tilga olingan gapni BUTUNLAY tashlab ket. "
           "O'lim, kasallik, fojia haqidagi gaplarga ham rasm berma. Mos gap kam bo'lsa, kamroq g'oya qaytar (0 ta ham bo'lishi mumkin).")
    sents=sentences_from_words(words) if words else []
    if sents:
        lines=[{"i":i,"t":round(x["start"],1),"text":x["text"]} for i,x in enumerate(sents) if not is_sensitive(x["text"])][:200]
        if not lines: return []
        prompt=("Bu videoning vaqt belgili gaplari:\n"+json.dumps(lines,ensure_ascii=False)+"\n"
                f"Shu gaplardan ko'pi bilan {n} tasini tanlab, har biriga B-ROLL rasm g'oyasini ber. {rules} "
                "Har biri uchun 'i' = gap raqami, 'prompt' = INGLIZCHA aniq tasviriy rasm prompti. Butun video bo'ylab tarqat. "
                "JSON qaytar: [{\"i\":0,\"prompt\":\"...\"}].")
    else:
        prompt=(f"Video transkripti: \"{full_text[:1500]}\"\nVideo uzunligi: {int(dur)} soniya.\n"
                f"Mazmunga mos ko'pi bilan {n} ta B-ROLL rasm g'oyasini ber. {rules} "
                "Har biri uchun 'prompt' = INGLIZCHA aniq tasviriy rasm prompti va 'at' = videodagi joyi 0.0 dan 1.0 gacha. "
                "JSON qaytar: [{\"prompt\":\"...\",\"at\":0.0}].")
    txt=_ai_json(prompt); arr=json.loads(txt)
    if isinstance(arr,dict): arr=arr.get("brolls") or arr.get("data") or arr.get("items") or []
    out=[]
    for it in arr:
        q=str(it.get("prompt") or it.get("keyword") or "").strip()
        if not q or _SENS_EN.search(q) or is_sensitive(q): continue
        if sents and it.get("i") is not None:
            try: x=sents[int(it["i"])]
            except Exception: continue
            if is_sensitive(x["text"]): continue
            at=(x["start"]+x["end"])/2
        else:
            at=max(0.0,min(1.0,float(it.get("at",0) or 0)))*dur
            if words and is_sensitive(" ".join(w["w"] for w in words if at-3<=w["s"]<=at+3)): continue
        out.append({"prompt":q,"at":round(at,2)})
    return out[:n]

def gemini_image_vertex(prompt, dest, raw=False):
    token=_vertex_token()
    # rasm modellari REGIONAL — us-central1 da bo'ladi (global emas)
    host="us-central1-aiplatform.googleapis.com"; loc="us-central1"
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    full=prompt if raw else ("Generate a single photorealistic, cinematic 16:9 image. "
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

def imagen_vertex(prompt, dest, aspect="16:9"):
    token=_vertex_token()
    if GCP_LOCATION=="global": host="us-central1-aiplatform.googleapis.com"; loc="us-central1"
    else: host=f"{GCP_LOCATION}-aiplatform.googleapis.com"; loc=GCP_LOCATION
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    body={"instances":[{"prompt":prompt}],"parameters":{"sampleCount":1,"aspectRatio":aspect if aspect in ("16:9","9:16","1:1","4:3","3:4") else "16:9"}}
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

def gen_image(prompt, dest, aspect="16:9"):
    """Vertex bo'lsa: gemini-image -> imagen. Aks holda Pexels. Xatoni ko'taradi. aspect: 16:9 | 9:16 | 1:1"""
    if have_vertex():
        e1=""
        try:
            if aspect=="16:9": return gemini_image_vertex(prompt,dest)
            ori="VERTICAL portrait 9:16 (taller than wide)" if aspect=="9:16" else "square 1:1"
            return gemini_image_vertex("Generate a single photorealistic, cinematic, documentary-style photograph in "+ori+" format, subject centered with room around it. "
                                       "NO text, NO letters, NO captions, NO watermark, NO cartoon. Subject: "+prompt,dest,raw=True)
        except Exception as ex: e1=str(ex)[:80]
        try: return imagen_vertex(prompt,dest,aspect)
        except Exception as ex: raise RuntimeError(f"gem-img[{e1}] / imagen[{str(ex)[:80]}]")
    got=pexels_image(prompt,dest)
    if not got: raise RuntimeError("Pexels bo'sh (yoki kalit yo'q)")
    return got

# rang bahosi (color grade) — montaj stilistikasi
# ================= ANIMATSIYALI QATLAMLAR (stiker / rasm / matn): In + Out + Loop =================
# Bu formulalar studio.html dagi animState() bilan AYNAN bir xil -> preview eksportga mos keladi.
import math as _m
ANIM_IN=("none","fade","pop","grow","zoomin","slide_l","slide_r","slide_u","slide_d","spin","bounce","drop","flip","elastic","swingin","roll","rise","stretch","wipe","type")
ANIM_OUT=("none","fade","shrink","zoomout","slide_l","slide_r","slide_u","slide_d","spin","fall","popout","roll","rise","flip","wipe")
ANIM_LOOP=("none","pulse","shake","wobble","float","spin","swing","heartbeat","blink","jelly","bounce","tilt","zoom","glitch","flash")
def _clip01(v): return 0.0 if v<0 else (1.0 if v>1 else v)
def _back_out(p): c1=1.70158; c3=c1+1; return 1+c3*(p-1)**3+c1*(p-1)**2
def _bounce_out(p):
    n1=7.5625; d1=2.75
    if p<1/d1: return n1*p*p
    if p<2/d1: p-=1.5/d1; return n1*p*p+0.75
    if p<2.5/d1: p-=2.25/d1; return n1*p*p+0.9375
    p-=2.625/d1; return n1*p*p+0.984375

def anim_state(tau, d, a_in="pop", a_out="fade", a_loop="none", in_d=0.35, out_d=0.3):
    """tau = qatlam boshidan o'tgan vaqt. -> (sx, sy, rot_deg, alpha, dx, dy); dx/dy element o'lchami birligida."""
    sx=sy=1.0; rot=0.0; al=1.0; dx=dy=0.0
    in_d=min(max(0.0,in_d), d*0.5); out_d=min(max(0.0,out_d), d*0.5)
    p=1.0 if in_d<=0 else _clip01(tau/in_d)
    if p<1 and a_in!="none":
        e=1-(1-p)**3
        if a_in=="fade": al*=p
        elif a_in=="pop": k=max(0.0,_back_out(p)); sx*=k; sy*=k; al*=min(1.0,p*3)
        elif a_in=="grow": k=0.3+0.7*e; sx*=k; sy*=k; al*=e
        elif a_in=="zoomin": k=1.7-0.7*e; sx*=k; sy*=k; al*=e
        elif a_in=="slide_l": dx-=(1-e)*0.7; al*=e
        elif a_in=="slide_r": dx+=(1-e)*0.7; al*=e
        elif a_in=="slide_u": dy-=(1-e)*0.7; al*=e
        elif a_in=="slide_d": dy+=(1-e)*0.7; al*=e
        elif a_in=="spin": rot-=(1-e)*270; sx*=e; sy*=e; al*=min(1.0,p*2)
        elif a_in=="bounce": k=_bounce_out(p); sx*=k; sy*=k
        elif a_in=="drop": dy-=(1-_bounce_out(p))*0.9; al*=min(1.0,p*4)
        elif a_in=="flip": sx*=e; al*=min(1.0,p*3)
        elif a_in=="elastic": k=1-_m.cos(p*_m.pi*4.5)*_m.exp(-p*5.5); sx*=k; sy*=k; al*=min(1.0,p*4)
        elif a_in=="swingin": rot-=(1-e)*80*_m.cos(p*_m.pi*1.5); al*=min(1.0,p*3)
        elif a_in=="roll": dx-=(1-e)*0.9; rot-=(1-e)*200; al*=e
        elif a_in=="rise": dy+=(1-e)*0.35; al*=p
        elif a_in=="stretch": sx*=1+(1-e)*1.2; sy*=0.25+0.75*e; al*=min(1.0,p*3)
    q=0.0 if out_d<=0 else _clip01((tau-(d-out_d))/out_d)
    if q>0 and a_out!="none":
        e=q**3
        if a_out=="fade": al*=1-q
        elif a_out=="shrink": sx*=1-e; sy*=1-e
        elif a_out=="zoomout": sx*=1+0.7*e; sy*=1+0.7*e; al*=1-q
        elif a_out=="slide_l": dx-=e*0.7; al*=1-e
        elif a_out=="slide_r": dx+=e*0.7; al*=1-e
        elif a_out=="slide_u": dy-=e*0.7; al*=1-e
        elif a_out=="slide_d": dy+=e*0.7; al*=1-e
        elif a_out=="spin": rot+=e*270; sx*=1-e; sy*=1-e
        elif a_out=="fall": dy+=e*0.9; rot+=e*40; al*=1-q
        elif a_out=="popout": k=(1+0.25*_m.sin(q*_m.pi))*(1-e); sx*=k; sy*=k
        elif a_out=="roll": dx+=e*0.9; rot+=e*200; al*=1-e
        elif a_out=="rise": dy-=e*0.35; al*=1-q
        elif a_out=="flip": sx*=1-e; al*=1-e
    T=2*_m.pi*tau
    if a_loop=="pulse": k=1+0.08*_m.sin(T*1.5); sx*=k; sy*=k
    elif a_loop=="shake": dx+=0.04*_m.sin(T*9); dy+=0.03*_m.cos(T*11)
    elif a_loop=="wobble": rot+=9*_m.sin(T*1.3)
    elif a_loop=="float": dy+=0.07*_m.sin(T*0.7)
    elif a_loop=="spin": rot+=360*tau/2.0
    elif a_loop=="swing": rot+=16*_m.sin(T*0.8)
    elif a_loop=="heartbeat":
        h=(tau*1.2)%1.0; k=1+(0.16*max(0.0,_m.sin(T*1.2*2)) if h<0.5 else 0.0); sx*=k; sy*=k
    elif a_loop=="blink": al*=0.55+0.45*(0.5+0.5*_m.sin(T*2))
    elif a_loop=="jelly": j=0.07*_m.sin(T*2); sx*=1+j; sy*=1-j
    elif a_loop=="bounce": dy-=0.16*abs(_m.sin(T*1.1))
    elif a_loop=="tilt": rot+=4*_m.sin(T*0.35)
    elif a_loop=="zoom": k=1+0.12*_clip01(tau/max(0.1,d)); sx*=k; sy*=k
    elif a_loop=="glitch":
        g=int(tau*12); r1=((g*7919)%13)/13.0
        if r1>0.72: dx+=(r1-0.86)*0.3; sx*=1+(r1-0.86)*0.2
    elif a_loop=="flash": al*=1.0 if int(tau*6)%2==0 else 0.35
    return sx,sy,rot,_clip01(al),dx,dy

def anim_clip(tau, d, a_in="none", a_out="none", in_d=0.35, out_d=0.3, steps=0):
    """Ochilish (reveal): chapdan qancha qismi ko'rinadi (0..1). 'wipe' silliq, 'type' harfma-harf. JS animClip bilan bir xil."""
    cl=1.0
    if a_in in ("wipe","type"):
        idur=min(max(0.05,(in_d*1.6 if a_in=="wipe" else max(in_d*2,0.045*max(1,steps)))), d*0.6); p=_clip01(tau/idur)
        cl=(1-(1-p)**3) if a_in=="wipe" else (_m.ceil(p*max(1,steps))/max(1,steps))
    if a_out=="wipe":
        od=min(max(0.05,out_d*1.6), d*0.4); q=_clip01((tau-(d-od))/od); cl=min(cl,1-q**3)
    return cl

def kf_ease(p, kind=None):
    """Keyframe orasidagi harakat egri chizig'i (JS kfEase bilan bir xil)."""
    if kind=="linear": return p
    if kind=="in": return p*p*p
    if kind=="out": return 1-(1-p)**3
    if kind=="back": c1=1.70158; c3=c1+1; return 1+c3*(p-1)**3+c1*(p-1)**2
    if kind=="hold": return 0.0 if p<1 else 1.0
    return p*p*(3-2*p)
def _kf_at(keys, tau):
    """Keyframe'lar orasida silliq (smoothstep) interpolatsiya. -> dict yoki None."""
    if not keys: return None
    if tau<=keys[0]["t"]: return keys[0]
    if tau>=keys[-1]["t"]: return keys[-1]
    for a,b in zip(keys,keys[1:]):
        if a["t"]<=tau<=b["t"]:
            p=(tau-a["t"])/max(1e-6,b["t"]-a["t"]); e=kf_ease(p, a.get("ease"))
            return {k:a[k]+(b[k]-a[k])*e for k in ("x","y","s","rot","op")}
    return keys[-1]

def layer_frames(img, outdir, dur, L, fps=30, frame=None):
    """PIL rasm (RGBA) yoki [(rasm, tugash_vaqti)] (GIF) -> animatsiya kadrlari (PNG). -> (kadrlar soni, kanvas eni, bo'yi).
    L["keys"] bo'lsa (keyframe) kadrlar butun ekran (frame=(tw,th)) kanvasiga chiziladi."""
    from PIL import Image
    os.makedirs(outdir, exist_ok=True)
    srcs=img if isinstance(img,list) else None
    if srcs: img=srcs[0][0]; gtot=max(0.05,srcs[-1][1])
    keys=[]
    for kf in (L.get("keys") or []):
        try: keys.append({"t":float(kf.get("t",0)),"x":float(kf.get("x",0.5)),"y":float(kf.get("y",0.5)),"s":float(kf.get("s",1)),"rot":float(kf.get("rot",0)),"op":float(kf.get("op",1)),"ease":str(kf.get("ease") or "")})
        except Exception: pass
    keys.sort(key=lambda z:z["t"])
    if not frame: keys=[]
    w0,h0=img.size; n=max(1,int(round(dur*fps)))
    a_in=L.get("in","pop"); a_out=L.get("out","fade"); a_loop=L.get("loop","none")
    in_d=float(L.get("in_d",0.35)); out_d=float(L.get("out_d",0.3))
    rot0=float(L.get("rot",0) or 0); op=_clip01(float(L.get("op",1) if L.get("op") is not None else 1))
    sts=[anim_state(i/fps, dur, a_in, a_out, a_loop, in_d, out_d) for i in range(n)]
    steps=int(L.get("steps",0) or 0)
    clips=[anim_clip(i/fps, dur, a_in, a_out, in_d, out_d, steps) for i in range(n)] if (a_in in ("wipe","type") or a_out=="wipe") else None
    kfs=[_kf_at(keys,i/fps) for i in range(n)] if keys else None
    if kfs:   # keyframe: masshtab/burilish/shaffoflik holatga qo'shiladi, joylashuv butun ekranda
        sts=[(sx*k["s"],sy*k["s"],rot+k["rot"],al*_clip01(k["op"]),dx,dy) for (sx,sy,rot,al,dx,dy),k in zip(sts,kfs)]
    hx=hy=1.0
    for sx,sy,rot,al,dx,dy in sts:
        ww=w0*abs(sx); hh=h0*abs(sy)
        if abs(rot+rot0)>0.01: r=_m.hypot(ww,hh)/2; ex=ey=r
        else: ex=ww/2; ey=hh/2
        hx=max(hx,abs(dx)*w0+ex); hy=max(hy,abs(dy)*h0+ey)
    cw=int(_m.ceil(hx))*2+4; ch=int(_m.ceil(hy))*2+4
    cw+=cw%2; ch+=ch%2
    if kfs: cw,ch=int(frame[0]),int(frame[1])
    prev_key=None; prev_path=None
    for i,(sx,sy,rot,al,dx,dy) in enumerate(sts):
        path=os.path.join(outdir,"%05d.png"%i); gi=0
        if srcs:
            tt=(i/fps)%gtot
            for gi,(gim,gend) in enumerate(srcs):
                if tt<gend: break
            img=srcs[gi][0]
        ox,oy=(kfs[i]["x"]*cw, kfs[i]["y"]*ch) if kfs else (cw/2, ch/2)
        cl=clips[i] if clips else 1.0
        key=(round(sx,3),round(sy,3),round(rot,2),round(al,3),round(dx,4),round(dy,4),gi,round(ox,1),round(oy,1),round(cl,3))
        if key==prev_key and prev_path:
            try: os.link(prev_path,path)
            except Exception: shutil.copy(prev_path,path)
            continue
        canvas=Image.new("RGBA",(cw,ch),(0,0,0,0))
        ww=int(round(w0*sx)); hh=int(round(h0*sy)); a=al*op
        if ww>=2 and hh>=2 and a>0.004 and cl>0.004:
            fr=img if (ww==w0 and hh==h0) else img.resize((ww,hh), Image.BILINEAR)
            if cl<0.999:                                   # chapdan ochilish
                cwid=max(1,int(round(fr.size[0]*cl))); m_=Image.new("RGBA",fr.size,(0,0,0,0)); m_.paste(fr.crop((0,0,cwid,fr.size[1])),(0,0)); fr=m_
            if abs(rot+rot0)>0.01: fr=fr.rotate(-(rot+rot0), expand=True, resample=Image.BICUBIC)
            if a<0.999:
                fr=fr.copy(); fr.putalpha(fr.getchannel("A").point(lambda v,a=a:int(v*a)))
            px_=int(round(ox+dx*w0-fr.size[0]/2)); py_=int(round(oy+dy*h0-fr.size[1]/2))
            if px_<0 or py_<0 or px_+fr.size[0]>cw or py_+fr.size[1]>ch:      # kanvasdan chiqsa qirqamiz
                l=max(0,-px_); t_=max(0,-py_); r=min(fr.size[0],cw-px_); b_=min(fr.size[1],ch-py_)
                if r>l and b_>t_: fr=fr.crop((l,t_,r,b_)); px_+=l; py_+=t_
                else: fr=None
            if fr is not None: canvas.alpha_composite(fr,(px_,py_))
        canvas.save(path, compress_level=1)
        prev_key=key; prev_path=path
    return n,cw,ch

_FONT_FILES={}
def font_file(family):
    """Shrift oilasi nomi -> fayl yo'li (FONTS_DIR ichidan)."""
    if not _FONT_FILES:
        try:
            from fontTools.ttLib import TTFont
            for fn in sorted(os.listdir(FONTS_DIR)):
                if not fn.lower().endswith((".ttf",".otf")): continue
                try:
                    f=TTFont(os.path.join(FONTS_DIR,fn),lazy=True,fontNumber=0); fam=f["name"].getBestFamilyName(); f.close()
                    if fam: _FONT_FILES.setdefault(fam, os.path.join(FONTS_DIR,fn))
                except Exception: continue
        except Exception: pass
    return _FONT_FILES.get(family) or _FONT_FILES.get("Anton") or (next(iter(_FONT_FILES.values())) if _FONT_FILES else None)

def _rgb(h, d=(255,255,255)):
    h=str(h or "").lstrip("#")
    try: return (int(h[0:2],16),int(h[2:4],16),int(h[4:6],16)) if len(h)==6 else d
    except Exception: return d

def text_png(L, k, lang="uz"):
    """Erkin matn qatlami -> shaffof PNG (PIL). o'lcham 1920 balandlik birligida (subtitr kabi)."""
    from PIL import Image, ImageDraw, ImageFont
    txt=str(L.get("text") or "").replace("\r","")[:300]
    if L.get("upper"): txt=txt.upper()
    if not txt.strip(): return None
    px=max(8,int(round(float(L.get("size",110))*k))); bw=max(0,int(round(float(L.get("border",5))*k)))
    ff=font_file(pick_font(L.get("font","Anton"), lang))
    font=ImageFont.truetype(ff, px) if ff else ImageFont.load_default()
    dr=ImageDraw.Draw(Image.new("RGBA",(8,8)))
    sp=int(px*0.12)
    x0,y0,x1,y1=dr.multiline_textbbox((0,0),txt,font=font,stroke_width=bw,align="center",spacing=sp)
    bg=L.get("bg"); pad=int(px*0.28) if bg else int(px*0.08)+2
    w=int(x1-x0)+2*pad; h=int(y1-y0)+2*pad
    img=Image.new("RGBA",(w+w%2,h+h%2),(0,0,0,0)); d=ImageDraw.Draw(img)
    if bg: d.rounded_rectangle((0,0,img.size[0]-1,img.size[1]-1), radius=int(px*0.22), fill=_rgb(bg,(0,0,0))+(235,))
    fx=L.get("fx")
    if fx=="3d":                       # hajmli (ekstruziya): matn ostiga pastga-o'ngga siljigan to'q qatlamlar
        dep=max(3,int(px*0.11)); ext=Image.new("RGBA",(img.size[0]+dep,img.size[1]+dep),(0,0,0,0))
        if bg: ext.alpha_composite(img)
        de=ImageDraw.Draw(ext); c3=_rgb(L.get("fxc"),(40,20,90))
        for kk in range(dep,0,-1):
            de.multiline_text((pad-x0+kk,pad-y0+kk),txt,font=font,fill=c3+(255,),stroke_width=bw,stroke_fill=c3+(255,),align="center",spacing=sp)
        img=ext; d=ImageDraw.Draw(img)
    d.multiline_text((pad-x0,pad-y0),txt,font=font,fill=_rgb(L.get("color"))+(255,),stroke_width=bw,
                     stroke_fill=_rgb(L.get("outline"),(0,0,0))+(255,),align="center",spacing=sp)
    if fx=="grad":                     # ikki rangli gradient (tepadan pastga)
        mk=Image.new("L",img.size,0); ImageDraw.Draw(mk).multiline_text((pad-x0,pad-y0),txt,font=font,fill=255,align="center",spacing=sp)
        c1=_rgb(L.get("color")); c2=_rgb(L.get("fxc"),(255,79,163)); hh_=img.size[1]
        gr=Image.new("RGBA",(1,hh_)); gp=gr.load()
        for yy in range(hh_):
            t_=yy/max(1,hh_-1); gp[0,yy]=tuple(int(c1[j]+(c2[j]-c1[j])*t_) for j in range(3))+(255,)
        gr=gr.resize(img.size); img.paste(gr,(0,0),mk)
    if fx in ("shadow","glow"):     # soya yoki porlash: matn ostiga xiralashtirilgan nusxa
        from PIL import ImageFilter
        m=int(px*0.45); big=Image.new("RGBA",(img.size[0]+2*m,img.size[1]+2*m),(0,0,0,0))
        col=(0,0,0) if fx=="shadow" else _rgb(L.get("fxc"),(57,231,95))
        under=Image.new("RGBA",big.size,col+(0,)); al=Image.new("L",big.size,0)
        al.paste(img.getchannel("A"),(m,m+(int(px*0.07) if fx=="shadow" else 0)))
        al=al.filter(ImageFilter.GaussianBlur(px*(0.07 if fx=="shadow" else 0.14)))
        if fx=="glow": al=al.point(lambda v:min(255,int(v*1.8)))
        else: al=al.point(lambda v:int(v*0.75))
        under.putalpha(al); big.alpha_composite(under); big.alpha_composite(img,(m,m)); img=big
    return img

def count_text(L, p):
    """Sanagich matni: p (0..1) bo'yicha from -> to. Matndagi {n} o'rniga qo'yiladi (bo'lmasa oxiriga)."""
    c=L.get("count") or {}; a=float(c.get("from",0)); b=float(c.get("to",100)); dec=int(c.get("dec",0) or 0)
    e=1-(1-_clip01(p))**3; v=a+(b-a)*e
    num=("%.*f"%(dec,v)) if dec>0 else "{:,}".format(int(round(v))).replace(","," ")
    t=str(L.get("text") or "{n}")
    return t.replace("{n}",num) if "{n}" in t else (num+" "+t).strip()

def counter_frames(L, k, lang, dur, steps=24):
    """Sanagich: har qadam uchun alohida matn rasmi -> [(img, davomiylik)] (bir xil o'lchamga keltiriladi)."""
    from PIL import Image
    cd=min(max(0.4,float((L.get("count") or {}).get("dur",1.2))), max(0.4,dur*0.8)); fr=[]
    for i in range(steps+1):
        im=text_png(dict(L,text=count_text(L,i/steps)),k,lang)
        if im is not None: fr.append(im)
    if not fr: return None
    W=max(f.size[0] for f in fr); H=max(f.size[1] for f in fr); out=[]
    for i,f in enumerate(fr):
        c=Image.new("RGBA",(W+W%2,H+H%2),(0,0,0,0)); c.alpha_composite(f,((c.size[0]-f.size[0])//2,(c.size[1]-f.size[1])//2))
        acc=(cd*(i+1)/steps) if i<len(fr)-1 else dur+1.0          # (rasm, shu kadr tugash vaqti) — GIF kadrlari bilan bir xil format
        out.append((c, acc))
    return out

# ---------- OVOZ EFFEKTLARI (sintez — litsenziya kerak emas) ----------
SFX={"whoosh":("anoisesrc=d=0.5:c=pink:a=0.9,highpass=f=350,lowpass=f=5000,afade=t=in:d=0.28:curve=qsin,afade=t=out:st=0.28:d=0.22,volume=1.6",None),
     "pop":(None,"aevalsrc='0.7*sin(2*PI*(900-3600*t)*t)*exp(-26*t)':d=0.14:s=48000"),
     "ding":(None,"aevalsrc='0.45*sin(2*PI*1318*t)*exp(-5*t)+0.2*sin(2*PI*2637*t)*exp(-7*t)':d=0.9:s=48000"),
     "boom":(None,"aevalsrc='0.9*sin(2*PI*(70-40*t)*t)*exp(-5*t)':d=0.8:s=48000"),
     "click":(None,"aevalsrc='0.6*sin(2*PI*2200*t)*exp(-90*t)':d=0.05:s=48000")}
def sfx_file(kind, cache_dir):
    if kind not in SFX: return None
    os.makedirs(cache_dir, exist_ok=True); p=os.path.join(cache_dir, kind+".wav")
    if os.path.exists(p) and os.path.getsize(p)>200: return p
    chain,src=SFX[kind]
    run(["ffmpeg","-y","-hide_banner","-loglevel","error","-f","lavfi","-i",src or chain,"-ar","48000","-ac","2",p])
    return p if os.path.exists(p) else None

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
def grade_filter(grade):
    """grade = preset nomi YOKI Studio slayderlari: {sat,con,bri,temp}. ffmpeg filtr matnini qaytaradi."""
    if isinstance(grade,dict):
        def num(k,d,lo,hi):
            try: return max(lo,min(hi,float(grade.get(k,d))))
            except Exception: return d
        sat=num("sat",1.0,0.0,2.0); con=num("con",1.0,0.5,1.6); bri=num("bri",0.0,-0.3,0.3); temp=num("temp",0.0,-1.0,1.0)
        f="eq=contrast=%.3f:saturation=%.3f:brightness=%.3f"%(con,sat,bri)
        if abs(temp)>0.01: f="colorbalance=rs=%.3f:bs=%.3f:rm=%.3f:bm=%.3f,"%(0.14*temp,-0.14*temp,0.07*temp,-0.07*temp)+f
        shd=num("shd",0.0,-1.0,1.0); hil=num("hil",0.0,-1.0,1.0); vig=num("vig",0.0,0.0,1.0); shp=num("sharp",0.0,0.0,1.0)
        if abs(shd)>0.01 or abs(hil)>0.01: f+=",curves=all='0/0 0.25/%.3f 0.75/%.3f 1/1'"%(0.25+0.12*shd,0.75+0.12*hil)
        if vig>0.01: f+=",vignette=a=%.3f"%(0.25+0.6*vig)
        if shp>0.01: f+=",unsharp=5:5:%.2f"%(1.6*shp)
        hue=num("hue",0.0,-180.0,180.0); grn=num("grain",0.0,0.0,1.0); fde=num("fade",0.0,0.0,1.0)
        if abs(hue)>0.5: f+=",hue=h=%.1f"%hue
        if fde>0.01: f+=",curves=all='0/%.3f 1/%.3f'"%(0.12*fde,1-0.06*fde)
        lut=grade.get("lut") if isinstance(grade.get("lut"),dict) else None      # egri chiziq + rang g'ildiraklari (mijoz hisoblagan jadval)
        if lut:
            try:
                segs=[]
                for ch in ("r","g","b"):
                    v=[max(0.0,min(1.0,float(x))) for x in (lut.get(ch) or [])][:33]
                    if len(v)>=3: segs.append(ch+"='"+" ".join("%.4f/%.4f"%(i/(len(v)-1),y) for i,y in enumerate(v))+"'")
                if len(segs)==3: f+=",curves="+":".join(segs)
            except Exception: pass
        if grn>0.01: f+=",noise=alls=%d:allf=t"%int(4+22*grn)
        return f
    return GRADES.get(grade, GRADES["vivid"])

_ZOOM={0:0.0, 1:0.0004, 2:0.0009}  # zoom tezligi
_ZMAX={0:1.0, 1:1.05, 2:1.12}

QMAP={"480":(480,854,23),"540":(540,960,24),"720":(720,1280,21),"1080":(1080,1920,20),"1k":(1080,1920,20),"1440":(1440,2560,19),"2160":(2160,3840,18),"4k":(2160,3840,18)}
QSCALE={"480":480,"540":540,"720":720,"1080":1080,"1k":1080,"1440":1440,"2160":2160,"4k":2160}

# ovoz o'zgartirgich: nom -> ffmpeg audio filtri (48 kHz oqimga qo'llanadi)
VOICE_FX={
 "deep":"asetrate=48000*0.84,aresample=48000,atempo=1.190476",
 "high":"asetrate=48000*1.22,aresample=48000,atempo=0.819672",
 "chip":"asetrate=48000*1.5,aresample=48000,atempo=0.666667",
 "robot":"afftfilt=real='hypot(re,im)*sin(0)':imag='hypot(re,im)*cos(0)':win_size=512:overlap=0.75",
 "echo":"aecho=0.8:0.7:120:0.4",
 "hall":"aecho=0.8:0.6:40|75:0.35|0.25",
 "phone":"highpass=f=400,lowpass=f=3200,acompressor=threshold=-20dB:ratio=4",
 "radio":"highpass=f=250,lowpass=f=4200,acrusher=bits=10:mix=0.25",
}
def _hex0x(c, d="00ff00"):
    c=str(c or "").lstrip("#")
    return "0x"+(c if re.fullmatch(r"[0-9a-fA-F]{6}",c) else d)

def img_fx(im, L):
    """Rasm qatlamiga niqob (doira/yumaloq burchak) va rang bo'yicha fonni olib tashlash (chroma key)."""
    from PIL import Image, ImageDraw, ImageChops
    im=im.convert("RGBA"); w,h=im.size
    ch=L.get("chroma") if isinstance(L.get("chroma"),dict) else None
    if ch and ch.get("on"):
        c=_rgb(ch.get("color") or "#00ff00"); sim=max(0.02,min(0.8,float(ch.get("sim",0.3))))
        thr=sim*255*1.7320508; soft=max(8.0,thr*0.35)
        r,g,b,a=im.split()
        def dist(band,v): return band.point(lambda x:abs(x-v))
        dr=dist(r,c[0]); dg=dist(g,c[1]); db=dist(b,c[2])
        mx=ImageChops.lighter(ImageChops.lighter(dr,dg),db)            # eng katta kanal farqi (tez va barqaror)
        lim=thr/1.7320508
        al=mx.point(lambda x:0 if x<=lim else (255 if x>=lim+soft else int((x-lim)/soft*255)))
        im.putalpha(ImageChops.darker(a,al))
    mk=str(L.get("mask") or "")
    if mk in ("circle","round"):
        m=Image.new("L",(w*2,h*2),0); d=ImageDraw.Draw(m)
        if mk=="circle":
            s_=min(w,h)*2; d.ellipse(((w*2-s_)//2,(h*2-s_)//2,(w*2+s_)//2,(h*2+s_)//2),fill=255)
        else: d.rounded_rectangle((0,0,w*2-1,h*2-1),radius=int(min(w,h)*0.36),fill=255)
        m=m.resize((w,h),Image.LANCZOS); im.putalpha(ImageChops.darker(im.getchannel("A"),m))
    return im

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
    if fx=="bw":                         # oq-qora
        return [f"{il}hue=s=0,curves=preset=medium_contrast{ol}"]
    if fx=="sepia":                      # eski foto
        return [f"{il}colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131,eq=contrast=1.05{ol}"]
    if fx=="oldfilm":                    # eski kino: sepia + dona + miltillash + chiziqlar
        return [f"{il}colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131,noise=alls={int(14*s)}:allf=t,eq=brightness='0.03*sin(t*31)',vignette=PI/4,"
                f"drawbox=x='mod(t*{int(tw*1.7)}\\,iw)':y=0:w=2:h=ih:color=white@0.12:t=fill{ol}"]
    if fx=="pixel":                      # piksel (mozaika)
        f_=10+6*strong
        return [f"{il}scale=iw/{f_}:ih/{f_}:flags=neighbor,scale={tw}:{th}:flags=neighbor{ol}"]
    if fx=="poster":                     # plakat (kam rang)
        return [f"{il}elbg=codebook_length={12-3*strong}:nb_steps=1,eq=saturation=1.3{ol}"]
    if fx=="invert":                     # negativ
        return [f"{il}negate{ol}"]
    if fx=="edge":                       # chizgi (kontur)
        return [f"{il}edgedetect=low=0.08:high=0.25:mode=colormix{ol}"]
    if fx=="rgb":                        # rang ajralishi (xromatik aberratsiya) pulsi
        return [f"{il}rgbashift=rh='{2+2*strong}*sin(t*5)':bh='-{2+2*strong}*sin(t*5)':gv='{1+strong}*cos(t*4)'{ol}"]
    if fx=="mirror":                     # ko'zgu (chap yarmi o'ngga aks etadi)
        return [f"{il}crop=iw/2:ih:0:0,split[mA][mB];[mB]hflip[mC];[mA][mC]hstack,scale={tw}:{th}{ol}"]
    if fx=="tilt":                       # tilt-shift: tepa va past xira
        return [f"{il}split[tA][tB];[tB]gblur=sigma={6*s:.1f}[tC];[tA][tC]blend=all_expr='A*(1-min(1\\,pow(abs(Y/H-0.5)*2.3\\,3)))+B*min(1\\,pow(abs(Y/H-0.5)*2.3\\,3))',eq=saturation=1.25{ol}"]
    if fx=="scan":                       # skaner chiziqlari (CRT)
        return [f"{il}drawgrid=w=iw:h=4:t=1:c=black@0.28,eq=contrast=1.12:saturation=1.15{ol}"]
    if fx=="pulse":                      # ritmik zoom pulsi
        return [f"{il}scale=iw*1.06:ih*1.06,crop={tw}:{th}:x='(iw-{tw})/2':y='(ih-{th})/2',zoompan=z='1+0.03*abs(sin(in/30*3.2))':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={tw}x{th}:fps=30{ol}"]
    if fx=="sunset":                     # iliq quyosh botishi
        return [f"{il}curves=r='0/0.05 0.5/0.6 1/1':b='0/0 0.5/0.42 1/0.85',eq=saturation=1.2,vignette=PI/5{ol}"]
    if fx=="cyber":                      # kiberpank: binafsha-firuza
        return [f"{il}curves=r='0/0.1 0.5/0.55 1/1':g='0/0 0.5/0.42 1/0.9':b='0/0.18 0.5/0.62 1/1',eq=contrast=1.2:saturation=1.45{ol}"]
    return [f"{il}null{ol}"]

FX_MAP={  # oila -> effekt
 "viral":"glow","bold":"flash","neon":"glow","clean":"none","cinema":"letterbox",
 "vlog":"progress","biz":"progress","pod":"progress","fashion":"leak","fit":"shake",
 "hype":"glitch","luxe":"grain","retro":"vhs","game":"glitch","story":"dream",
 "news":"progress","beauty":"dream","street":"duotone","tech":"glitch","calm":"none",
}

def render_final(cut, ass_path, outp, brolls, zoom=True, audio_clean=True, grade="vivid", zoom_level=1,
                 quality="1080", fast=False, fx="none", stickers=None,
                 music=None, music_vol=0.28, orig_vol=1.0,
                 anim_layers=None, transitions=None, zooms=None, sfx=None, music_fade=False, size=None, frame=None, stab=False, voice_fx=None, duck=False, music_novocal=False):
    """brolls=[...] (Ken Burns), stickers=[{path,time,dur,x,y,size}], music=audio yo'li, orig_vol/music_vol=ovoz.
    anim_layers=[{dir,time,dur,cx,cy,cw,ch}] (layer_frames kadrlari), transitions=[{t,type,dur}] (chiqish vaqtida),
    zooms=[{a,b,scale,ramp}], sfx=[{path,t,vol}]."""
    stickers = stickers or []; anim_layers = anim_layers or []
    transitions = [x for x in (transitions or []) if x.get("type") not in (None,"","none")][:60]
    zooms = (zooms or [])[:40]; sfx = (sfx or [])[:60]
    tw,th,crf = QMAP.get(str(quality).lower(), (W,H,20))
    if size: tw,th=int(size[0]),int(size[1])
    frame=frame or {}
    fz=max(1.0,min(3.0,float(frame.get("zoom",1) or 1))); fpx=max(-1.0,min(1.0,float(frame.get("x",0) or 0))); fpy=max(-1.0,min(1.0,float(frame.get("y",0) or 0)))
    frot=int(frame.get("rot",0) or 0)%360
    fpre=("deshake=rx=32:ry=32:edge=mirror," if stab else "")+("hflip," if frame.get("mirror") else "")+("hflip,vflip," if frame.get("flip") else "")+({90:"transpose=1,",180:"hflip,vflip,",270:"transpose=2,"}.get(frot,""))
    def _fit(pw,ph):   # kadr: aks/ag'darish -> kattalashtirib to'ldirish -> kerakli joyini qirqish
        return (f"{fpre}scale={int(pw*fz)//2*2}:{int(ph*fz)//2*2}:force_original_aspect_ratio=increase,"
                f"crop={pw}:{ph}:(iw-{pw})/2*{1+fpx:.3f}:(ih-{ph})/2*{1+fpy:.3f}")
    ae=ass_path.replace("\\","/").replace(":","\\:"); fd=FONTS_DIR.replace("\\","/").replace(":","\\:")
    sub=f"ass='{ae}':fontsdir='{fd}'"
    lvl=0 if not zoom else int(zoom_level if zoom_level in (0,1,2) else 1)
    g=grade_filter(grade); geq=("," + g) if g else ""
    dur=_probe_dur(cut) or 3.0
    strong = 2 if lvl>=2 else 1
    # --- kesimlardagi o'tishlar va zoom zarbalari (vaqt = in/30, chunki kirish 30fps CFR) ---
    T="(in/30)"; zadd=[]; shk=[]; win={}
    for tr in transitions:
        c=float(tr.get("t",0)); hh=max(0.08,float(tr.get("dur",0.3))/2); ty=str(tr.get("type"))
        win.setdefault(ty,[]).append((c,hh))
        if ty=="spin": zadd.append(f"0.45*max(0,1-abs({T}-{c:.3f})/{hh:.3f})")
        if ty=="zoom": zadd.append(f"0.16*max(0,1-abs({T}-{c:.3f})/{hh:.3f})")
        elif ty=="shake": zadd.append(f"0.07*max(0,1-abs({T}-{c:.3f})/{hh:.3f})"); shk.append(f"max(0,1-abs({T}-{c:.3f})/{hh:.3f})")
    fxs=[]; fys=[]
    for z in zooms:
        a_=float(z.get("a",0)); b_=float(z.get("b",0)); sc=max(1.0,min(2.0,float(z.get("scale",1.15)))); rp=max(0.0,float(z.get("ramp",0.12)))
        if b_-a_<0.1 or sc<=1.001: continue
        w_=(f"between({T},{a_:.3f},{b_:.3f})" if rp<0.02 else f"clip(({T}-{a_:.3f})/{rp:.3f},0,1)*clip(({b_:.3f}-{T})/{rp:.3f},0,1)")
        zadd.append(f"{sc-1:.3f}*{w_}")
        zx=max(-1.0,min(1.0,float(z.get("x",0) or 0))); zy=max(-1.0,min(1.0,float(z.get("y",0) or 0)))   # fokus: zoom paytida kadr qayerga suriladi
        if abs(zx)>0.01: fxs.append(f"{zx:.3f}*{w_}")
        if abs(zy)>0.01: fys.append(f"{zy:.3f}*{w_}")
    fcx=("*(1+clip("+"+".join(fxs)+",-1,1))") if fxs else ""; fcy=("*(1+clip("+"+".join(fys)+",-1,1))") if fys else ""
    zplus=("+"+"+".join(zadd)) if zadd else ""
    sx_=(f"+{int(tw*0.012)}*sin(in*2.3)*("+"+".join(shk)+")") if shk else ""
    sy_=(f"+{int(tw*0.010)}*cos(in*1.9)*("+"+".join(shk)+")") if shk else ""
    def _sumwin(ty, var="t"): return "+".join(f"max(0,1-abs({var}-{c:.3f})/{hh:.3f})" for c,hh in win.get(ty,[]))
    def _betw(ty): return "+".join(f"between(t,{c-hh:.3f},{c+hh:.3f})" for c,hh in win.get(ty,[]))
    post=""
    if win.get("flash") or win.get("dip"):
        ex="+".join(x for x in [("0.85*("+_sumwin("flash")+")") if win.get("flash") else "", ("-0.95*("+_sumwin("dip")+")") if win.get("dip") else ""] if x)
        post+=f",eq=brightness='max(-1,min(1,{ex}))':eval=frame"
    if win.get("blur"): post+=f",gblur=sigma={max(6,tw//60)}:steps=1:enable='{_betw('blur')}'"
    if win.get("spin"):
        ang="+".join(f"(PI*pow(clip((t-{c-hh:.3f})/{hh:.3f},0,1),2)*lt(t,{c:.3f})-PI*pow(1-clip((t-{c:.3f})/{hh:.3f},0,1),2)*gte(t,{c:.3f})*lt(t,{c+hh:.3f}))" for c,hh in win["spin"])
        post+=f",rotate=a='{ang}':c=black:ow=iw:oh=ih"
    def _whip(ty, size, sign):
        return "+".join(f"({sign}{size}*(-pow(clip((t-{c-hh:.3f})/{hh:.3f},0,1),2)*lt(t,{c:.3f})+pow(1-clip((t-{c:.3f})/{hh:.3f},0,1),2)*gte(t,{c:.3f})*lt(t,{c+hh:.3f})))" for c,hh in win.get(ty,[]))
    wx="+".join(x for x in [_whip("slide_l",tw,""),_whip("slide_r",tw,"-")] if x)
    wy="+".join(x for x in [_whip("slide_u",th,""),_whip("slide_d",th,"-")] if x)
    if win.get("glitch"): post+=f",rgbashift=rh={max(6,tw//80)}:bh=-{max(6,tw//80)}:gv={max(3,tw//160)}:enable='{_betw('glitch')}'"
    # 1) asosiy oqim (scale/crop/zoom/grade) -> [v0]
    if lvl>0 or zadd:
        spd=_ZOOM[lvl]; zmx=_ZMAX[lvl]
        base0=(f"[0:v]{_fit(int(tw*1.14)//2*2,int(th*1.14)//2*2)},"
               f"zoompan=z='min(1.0+{spd}*in,{zmx}){zplus}':d=1:x='(iw-iw/zoom)/2{fcx}{sx_}':y='(ih-ih/zoom)/2{fcy}{sy_}':s={tw}x{th}:fps=30"
               f"{geq}{post},setsar=1[v0]")
    else:
        base0=f"[0:v]{_fit(tw,th)}{geq}{post},setsar=1[v0]"
    whip=[]
    if wx or wy:   # surilish o'tishi: kadr xiralashgan nusxasi ustida tez surilib chiqadi/kiradi
        base0=base0[:-len("[v0]")]+"[vw]"
        whip=[f"[vw]split[wa][wb]",f"[wb]scale={max(2,tw//12)}:-2,gblur=sigma=3,scale={tw}:{th},eq=brightness=-0.2[wbg]",
              f"[wbg][wa]overlay=x='{wx or 0}':y='{wy or 0}':eval=frame[v0]"]
    # 2) effekt [v0]->[vfx]
    fxparts=_fx_parts(fx, "[v0]", "[vfx]", tw, th, dur, strong)
    # 3) subtitr [vfx]->[base]
    subpart=f"[vfx]{sub}[base]"
    ov=max(0.0,float(orig_vol))
    af=(f"volume={ov:.2f},highpass=f=85,afftdn=nr=12,equalizer=f=3000:t=q:w=1.5:g=3,acompressor=threshold=-18dB:ratio=3,"
        "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000") if audio_clean else f"volume={ov:.2f},aresample=48000"
    if voice_fx and VOICE_FX.get(str(voice_fx)): af="aresample=48000,"+VOICE_FX[str(voice_fx)]+","+af
    parts=[base0]+whip+fxparts; cur="[vfx]"; inputs=["-i",cut]
    idx=0
    # --- B-ROLL: avval to'liq ekranlilar (subtitr ULARNING USTIDA qoladi), keyin subtitr, keyin kartochkalar ---
    order=[(k,b) for k,b in enumerate(brolls) if b.get("full")]+[(None,None)]+[(k,b) for k,b in enumerate(brolls) if not b.get("full")]
    for k,b in order:
        if b is None:
            parts.append(f"{cur}{sub}[base]"); cur="[base]"; continue
        p=b["path"]; s=float(b["time"]); d=float(b.get("dur",2.5))
        y=float(b.get("y",0.72)); bw=int(tw*float(b.get("w",0.78))); bh=int(bw*0.62); fr=max(1,int(d*30))
        if b.get("full"): bw,bh,y=tw,th,0.0          # to'liq ekran (haqiqiy b-roll kesim)
        fd_=0.12 if b.get("full") else 0.3
        fades=f"format=yuva420p,fade=t=in:st=0:d={fd_}:alpha=1,fade=t=out:st={max(0.01,d-fd_):.2f}:d={fd_}:alpha=1,"
        ck=b.get("chroma") if isinstance(b.get("chroma"),dict) else None
        if ck and ck.get("on"): fades=f"chromakey={_hex0x(ck.get('color'))}:{max(0.02,min(0.8,float(ck.get('sim',0.3)))):.2f}:0.08,"+fades
        bop=max(0.05,min(1.0,float(b.get("op",1) or 1)))
        if bop<0.995: fades+=f"colorchannelmixer=aa={bop:.2f},"
        if os.path.splitext(p)[1].lower() in (".mp4",".mov",".webm",".mkv",".m4v"):   # video b-roll
            inputs+=["-stream_loop","-1","-t",f"{d}","-i",p]; idx+=1
            parts.append(f"[{idx}:v]fps=30,scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},setsar=1,"
                         f"{fades}setpts=PTS-STARTPTS+{s:.2f}/TB[ov{k}]")
        else:
            inputs+=["-loop","1","-t",f"{d}","-i",p]; idx+=1
            parts.append(f"[{idx}:v]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
                     f"zoompan=z='min(zoom+0.0016,1.14)':d={fr}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={bw}x{bh}:fps=30,"
                     f"{fades}setpts=PTS+{s:.2f}/TB[ov{k}]")
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
    # --- ANIMATSIYALI QATLAMLAR (stiker/rasm/matn): tayyor PNG kadrlar ---
    for k,L in enumerate(anim_layers):
        s=float(L["time"]); d=float(L["dur"])
        inputs+=["-framerate","30","-i",os.path.join(L["dir"],"%05d.png")]; idx+=1
        parts.append(f"[{idx}:v]format=rgba,setpts=PTS+{s:.3f}/TB[al{k}]")
        nxt=f"[av{k}]"
        parts.append(f"{cur}[al{k}]overlay={int(L['cx']-L['cw']/2)}:{int(L['cy']-L['ch']/2)}:enable='between(t,{s:.3f},{s+d:.3f})':eof_action=pass:repeatlast=0{nxt}")
        cur=nxt
    # --- AUDIO: original + (ixtiyoriy) musiqa + ovoz effektlari ---
    mixin=["[a0]"]; parts.append(f"[0:a]{af}[a0]")
    if music and os.path.exists(music):
        inputs+=["-stream_loop","-1","-i",music]; idx+=1
        mf=(f",afade=t=in:d=0.6,afade=t=out:st={max(0.1,dur-1.3):.2f}:d=1.2" if music_fade else "")
        if music_novocal and achannels(music)>=2:      # markazdagi vokalni fazaviy ayirish bilan so'ndirish (karaoke)
            mf=",pan=stereo|c0=c0-c1|c1=c1-c0,volume=1.6"+mf
        if duck:      # gap paytida musiqa o'zi pasayadi (sidechain)
            parts[-1]=parts[-1][:-len("[a0]")]+"[a0p]"; parts.append("[a0p]asplit[a0][a0k]")
            parts.append(f"[{idx}:a]atrim=0:{dur:.2f},volume={max(0.0,float(music_vol)):.2f}{mf},aresample=48000[mus0]")
            parts.append("[mus0][a0k]sidechaincompress=threshold=0.03:ratio=8:attack=25:release=450[mus]"); mixin.append("[mus]")
        else:
            parts.append(f"[{idx}:a]atrim=0:{dur:.2f},volume={max(0.0,float(music_vol)):.2f}{mf},aresample=48000[mus]"); mixin.append("[mus]")
    for k,x in enumerate(sfx):
        if not x.get("path") or not os.path.exists(x["path"]): continue
        inputs+=["-i",x["path"]]; idx+=1
        tr_=(f"atrim=0:{float(x['dur']):.3f},afade=t=out:st={max(0.0,float(x['dur'])-0.15):.3f}:d=0.15," if x.get("dur") else "")
        parts.append(f"[{idx}:a]{tr_}adelay={max(0,int(float(x.get('t',0))*1000))}:all=1,volume={max(0.0,min(2.0,float(x.get('vol',0.5)))):.2f},aresample=48000[sx{k}]"); mixin.append(f"[sx{k}]")
    if len(mixin)>1:
        parts.append("".join(mixin)+f"amix=inputs={len(mixin)}:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[aout]")
    else:
        parts.append("[a0]anull[aout]")
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
    try: fine=[[round(a,2),round(b if b else dur,2)] for a,b in detect_silences(video_path, dB=-33, mind=0.22)]
    except Exception: fine=[]
    return {"duration":round(dur,2),"words":words,"full_text":full,"engine":eng,"silences":fine,
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

# ================= NAMUNA VIDEODAN USLUB PROFILI (Studio) =================
_LOOKS={"clean":{"sat":1.02,"con":1.03,"bri":0.0,"temp":0.0},"vivid":{"sat":1.18,"con":1.06,"bri":0.0,"temp":0.0},
        "warm":{"sat":1.08,"con":1.04,"bri":0.01,"temp":0.45},"cool":{"sat":1.05,"con":1.05,"bri":0.0,"temp":-0.4},
        "cinema":{"sat":1.05,"con":1.08,"bri":0.0,"temp":0.2},"mono":{"sat":0.0,"con":1.1,"bri":0.0,"temp":0.0},
        "moody":{"sat":0.9,"con":1.13,"bri":-0.03,"temp":0.1}}
_STYLE_PROMPT=("You are a video editing analyst. Watch this short vertical social-media video and describe its EDITING STYLE "
    "so that another video can be edited the same way. Look at the on-screen captions, cuts, zooms, colors, overlays. "
    "Return ONLY a JSON object with exactly these keys: "
    "{\"subtitle\":{\"present\":true,\"lines\":1,\"words_per_screen\":3,\"uppercase\":true,\"position\":\"bottom|center|top\","
    "\"color_main\":\"#RRGGBB\",\"color_accent\":\"#RRGGBB\",\"size\":\"small|medium|large\",\"animation\":\"pop|bounce|fade|none\"},"
    "\"pace\":\"slow|medium|fast\",\"zoom\":\"none|light|strong\",\"look\":\"clean|vivid|warm|cool|cinema|mono|moody\","
    "\"effect\":\"none|glow|grain|glitch|shake|letterbox|vhs|dream|flash\",\"broll_per_30s\":0,\"emoji_stickers\":false,"
    "\"hook_title\":false,\"notes\":\"one short sentence in Uzbek (Latin) describing the style\"}. "
    "lines = how many text lines the captions occupy at once (1, 2 or 3). color_accent = color of the highlighted/spoken word "
    "(same as color_main if none). broll_per_30s = how many cutaway images/clips appear per 30 seconds.")

def _hexok(v, d):
    v=str(v or "").strip()
    return v.lower() if re.match(r"^#[0-9a-fA-F]{6}$", v) else d

def style_measure(video_path, limit=60):
    """AI'siz o'lchanadigan narsalar: kadr almashish tezligi va pauzalar uzunligi."""
    dur=ffdur(video_path); span=max(1.0,min(dur,float(limit)))
    c,o=run(["ffmpeg","-hide_banner","-t",str(limit),"-i",video_path,"-an",
             "-vf","scale=320:-2,select='gt(scene,0.32)',showinfo","-f","null","-"],capture=True)
    cuts=len(re.findall(r"pts_time:",o or ""))
    sils=[(a,b) for a,b in detect_silences(video_path, dB=-32, mind=0.2) if b is not None and a<limit]
    lens=sorted(b-a for a,b in sils)
    if len(lens)>=3: pmax=lens[min(len(lens)-1,int(len(lens)*0.75))]
    elif lens: pmax=lens[-1]
    else: pmax=0.3
    return {"duration":round(dur,2),"cuts_per_min":round(cuts/(span/60.0),1),"pause_max":round(max(0.25,min(1.0,pmax)),2)}

def analyze_style(video_path, work):
    """Namuna videoning montaj uslubi -> Studio qo'llay oladigan profil.
    Vertex bo'lsa Gemini videoni ko'rib tavsiflaydi; bo'lmasa faqat o'lchovlar (temp/pauza)."""
    m=style_measure(video_path)
    fast=m["cuts_per_min"]>=14 or m["pause_max"]<=0.35
    prof={"source":"measured","measured":m,
          "subtitle":{"layout":"stack3","words":6,"upper":True,"position":"bottom","color_main":"#ffffff",
                      "color_second":"#ffe14a","color_accent":"#39e75f","size":"medium","anim":"pop"},
          "pace":{"pause_max":m["pause_max"],"cuts_per_min":m["cuts_per_min"]},
          "look":dict(_LOOKS["vivid"]),"zoom":2 if fast else 1,"fx":"none","broll":4 if fast else 2,
          "stickers":fast,"hook":True,
          "notes":"Faqat temp o'lchandi (AI ulanmagan): %s kesish/daqiqa, pauza %.2fs gacha."%(m["cuts_per_min"],m["pause_max"])}
    if not have_vertex(): return prof
    try:
        small=os.path.join(work,"style_small.mp4")
        c,_=run(["ffmpeg","-y","-hide_banner","-loglevel","error","-t","40","-i",video_path,
                 "-vf","scale=-2:640,fps=8","-c:v","libx264","-preset","veryfast","-crf","32",
                 "-c:a","aac","-b:a","32k","-ac","1","-movflags","+faststart",small])
        if c!=0 or not os.path.exists(small) or os.path.getsize(small)>18*1024*1024: raise RuntimeError("namuna klip tayyorlanmadi")
        b64=base64.b64encode(open(small,"rb").read()).decode()
        txt,model=_vertex_audio_call(b64,"video/mp4",_STYLE_PROMPT)
        j=json.loads(txt)
        if isinstance(j,list): j=j[0] if j else {}
        su=j.get("subtitle") or {}
        lines=int(su.get("lines",1) or 1); wps=int(su.get("words_per_screen",3) or 3)
        layout="stack3" if lines>=3 else "line"
        main=_hexok(su.get("color_main"),"#ffffff"); acc=_hexok(su.get("color_accent"),"#ffea00")
        prof["subtitle"]={"present":su.get("present",True) is not False,"layout":layout,
            "words":max(3 if layout=="stack3" else 1,min(9,wps)),"upper":bool(su.get("uppercase",True)),
            "position":su.get("position") if su.get("position") in ("bottom","center","top") else "bottom",
            "color_main":main,"color_second":main,"color_accent":acc,
            "size":su.get("size") if su.get("size") in ("small","medium","large") else "medium",
            "anim":{"pop":"pop","bounce":"bounce","fade":"fade","none":"none"}.get(str(su.get("animation","pop")),"pop")}
        pace=str(j.get("pace","medium"))
        ai_p={"fast":0.3,"medium":0.5,"slow":0.8}.get(pace,0.5)
        prof["pace"]["pause_max"]=round(max(0.25,min(1.0,(ai_p+m["pause_max"])/2)),2); prof["pace"]["label"]=pace
        prof["look"]=dict(_LOOKS.get(str(j.get("look","vivid")),_LOOKS["vivid"])); prof["look_name"]=str(j.get("look","vivid"))
        prof["zoom"]={"none":0,"light":1,"strong":2}.get(str(j.get("zoom","light")),1)
        fx=str(j.get("effect","none")); prof["fx"]=fx if fx in ("none","glow","grain","glitch","shake","letterbox","vhs","dream","flash") else "none"
        prof["broll"]=max(0,min(8,int(j.get("broll_per_30s",0) or 0)))
        prof["stickers"]=bool(j.get("emoji_stickers",False)); prof["hook"]=bool(j.get("hook_title",False))
        prof["notes"]=str(j.get("notes") or "")[:240]; prof["source"]="ai:"+model
    except Exception as e:
        prof["notes"]="AI tahlil ishlamadi (%s). Faqat temp o'lchandi."%str(e)[:120]
    return prof

# ================= AI: OVOZ, TARJIMA, STIKER, STOK, ENG YAXSHI JOYLAR =================
TTS_VOICES=("Kore","Puck","Charon","Aoede","Fenrir","Leda","Orus","Zephyr")
def tts_gemini(text, dest_wav, voice="Kore"):
    """Matnni ovozga aylantiradi (Gemini TTS). Vertex yoki GEMINI_API_KEY kerak. -> dest_wav"""
    text=(text or "").strip()[:2000]
    if not text: raise RuntimeError("matn bo'sh")
    voice=voice if voice in TTS_VOICES else "Kore"
    body={"contents":[{"role":"user","parts":[{"text":text}]}],
          "generationConfig":{"responseModalities":["AUDIO"],"speechConfig":{"voiceConfig":{"prebuiltVoiceConfig":{"voiceName":voice}}}}}
    errs=[]; tries=[]
    models=["gemini-2.5-flash-tts","gemini-2.5-flash-preview-tts","gemini-3.1-flash-tts-preview"]
    if have_vertex():
        token=_vertex_token(); H={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
        for loc in ("global","us-central1"):
            host="aiplatform.googleapis.com" if loc=="global" else f"{loc}-aiplatform.googleapis.com"
            for m in models: tries.append((f"https://{host}/v1/projects/{GCP_PROJECT}/locations/{loc}/publishers/google/models/{m}:generateContent",H,m))
    if GEMINI_KEY:
        for m in models: tries.append((f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={GEMINI_KEY}",{"Content-Type":"application/json"},m))
    if not tries: raise RuntimeError("AI ulanmagan (Vertex yoki GEMINI_API_KEY kerak)")
    for url,H,m in tries:
        try:
            r=requests.post(url,headers=H,json=body,timeout=120)
            if r.status_code>=400: errs.append(f"{m}:{r.status_code}"); continue
            for cand in r.json().get("candidates",[]):
                for part in cand.get("content",{}).get("parts",[]):
                    inl=part.get("inlineData") or part.get("inline_data")
                    if inl and inl.get("data"):
                        mime=str(inl.get("mimeType") or inl.get("mime_type") or ""); raw=base64.b64decode(inl["data"])
                        if "wav" in mime or raw[:4]==b"RIFF":
                            open(dest_wav,"wb").write(raw); return dest_wav
                        mr=re.search(r"rate=(\d+)",mime); rate=mr.group(1) if mr else "24000"
                        pcm=dest_wav+".pcm"; open(pcm,"wb").write(raw)
                        run(["ffmpeg","-y","-hide_banner","-loglevel","error","-f","s16le","-ar",rate,"-ac","1","-i",pcm,"-ar","48000",dest_wav])
                        try: os.remove(pcm)
                        except Exception: pass
                        if os.path.exists(dest_wav): return dest_wav
            errs.append(f"{m}:audio yo'q")
        except Exception as e:
            errs.append(f"{m}:{str(e)[:40]}")
    raise RuntimeError("ovoz yaratilmadi: "+" | ".join(errs[:3]))

LANG_NAMES={"uz":"Uzbek (Latin script)","ru":"Russian","en":"English","tr":"Turkish","kk":"Kazakh","ar":"Arabic","es":"Spanish","de":"German","fr":"French"}
def translate_words(words, target):
    """So'zma-so'z subtitrni boshqa tilga o'giradi: gaplar tarjima qilinadi, vaqt gap ichida teng taqsimlanadi."""
    if not words: return []
    if not (have_vertex() or GEMINI_KEY): raise RuntimeError("AI ulanmagan (Vertex yoki GEMINI_API_KEY kerak)")
    groups=[]; cur=[]
    for w in words:
        if cur and (w["s"]-cur[-1]["e"]>0.6 or len(cur)>=14): groups.append(cur); cur=[]
        cur.append(w)
    if cur: groups.append(cur)
    src=[" ".join(x["w"] for x in g) for g in groups]
    prompt=("Translate each of these subtitle lines into "+LANG_NAMES.get(target,target)+". Keep them short and natural for on-screen captions. "
            "Return ONLY a JSON array of strings with EXACTLY "+str(len(src))+" items, same order, no notes.\n"+json.dumps(src,ensure_ascii=False))
    out=json.loads(_ai_json(prompt))
    if isinstance(out,dict): out=next((v for v in out.values() if isinstance(v,list)),[])
    if not isinstance(out,list) or len(out)!=len(src): raise RuntimeError("tarjima javobi mos kelmadi")
    res=[]
    for g,t in zip(groups,out):
        toks=str(t).split() or [str(t)]
        a=g[0]["s"]; b=g[-1]["e"]; step=(b-a)/len(toks)
        for i,tk in enumerate(toks): res.append({"w":tk,"s":round(a+i*step,3),"e":round(a+(i+1)*step-0.02,3)})
    return res

def cutout_plain_bg(src, dest, tol=38):
    """Bir xil rangli fonni (burchaklardan boshlab) shaffof qiladi — AI stiker va oddiy fonli rasmlar uchun."""
    from PIL import Image, ImageDraw, ImageFilter
    im=Image.open(src).convert("RGB"); w,h=im.size
    mask=Image.new("L",(w,h),255); rgb=im.copy()
    for xy in ((0,0),(w-1,0),(0,h-1),(w-1,h-1),(w//2,0),(w//2,h-1),(0,h//2),(w-1,h//2)):
        if rgb.getpixel(xy)!=(1,254,1): ImageDraw.floodfill(rgb,xy,(1,254,1),thresh=tol)
    px=rgb.load(); mp=mask.load()
    for y in range(h):
        for x in range(w):
            if px[x,y]==(1,254,1): mp[x,y]=0
    mask=mask.filter(ImageFilter.GaussianBlur(1.2)).point(lambda v:0 if v<90 else min(255,int((v-90)*1.6)))
    out=im.convert("RGBA"); out.putalpha(mask); bb=out.getbbox()
    if bb: out=out.crop(bb)
    out.save(dest); return dest

def ai_sticker(prompt, dest):
    """Tavsif bo'yicha stiker: rasm generatsiya qilinadi, oq fon olib tashlanadi."""
    tmp=dest+".src.png"
    full=("A single sticker illustration of: "+prompt+". Bold flat cartoon style, thick white outline around the subject, vivid colors, "
          "centered, isolated on a pure solid white background, no text, no shadow, no other objects.")
    if have_vertex(): gemini_image_vertex(full,tmp,raw=True)
    else: raise RuntimeError("AI ulanmagan (Vertex kerak)")
    from PIL import Image
    im=Image.open(tmp); im.thumbnail((640,640)); im.save(tmp)
    cutout_plain_bg(tmp,dest,tol=30)
    try: os.remove(tmp)
    except Exception: pass
    return dest

def stock_search(q, kind="photo", n=12):
    """Pexels'dan tayyor rasm/video qidirish. -> [{thumb,url,dur,by}]"""
    if not PEXELS_KEY: raise RuntimeError("Stok kutubxona ulanmagan (PEXELS_API_KEY kerak)")
    H={"Authorization":PEXELS_KEY}; out=[]
    if kind=="video":
        r=requests.get("https://api.pexels.com/videos/search",headers=H,params={"query":q,"per_page":n,"orientation":"portrait"},timeout=30); r.raise_for_status()
        for v in r.json().get("videos",[]):
            fs=sorted([f for f in v.get("video_files",[]) if f.get("link") and (f.get("height") or 0)<=1920],key=lambda f:-(f.get("height") or 0))
            if fs: out.append({"thumb":v.get("image"),"url":fs[0]["link"],"dur":v.get("duration"),"by":(v.get("user") or {}).get("name","")})
    else:
        r=requests.get("https://api.pexels.com/v1/search",headers=H,params={"query":q,"per_page":n},timeout=30); r.raise_for_status()
        for ph in r.json().get("photos",[]):
            out.append({"thumb":ph["src"].get("medium"),"url":ph["src"].get("large2x") or ph["src"].get("large"),"dur":None,"by":ph.get("photographer","")})
    return out

def highlights(words, dur, n=4, target=35.0):
    """Uzun videodan eng yaxshi qisqa bo'laklar. AI bo'lsa mazmun bo'yicha, bo'lmasa gap zichligi bo'yicha. -> [{a,b,title}]"""
    sents=sentences_from_words(words)
    if not sents: return []
    if have_vertex() or GEMINI_KEY:
        try:
            lines=[{"i":i,"t":round(x["start"],1),"e":round(x["end"],1),"text":x["text"]} for i,x in enumerate(sents)][:400]
            prompt=("These are timed transcript lines of a long video. Pick the "+str(n)+" best self-contained moments for short vertical clips "
                    "(strong hook, complete thought), each about "+str(int(target))+" seconds (20-60s), not overlapping. "
                    "Return ONLY a JSON array of objects {\"start_i\":int,\"end_i\":int,\"title\":\"short catchy title in the transcript language\"}.\n"+json.dumps(lines,ensure_ascii=False))
            arr=json.loads(_ai_json(prompt)); out=[]
            if isinstance(arr,dict): arr=next((v for v in arr.values() if isinstance(v,list)),[])
            for x in arr:
                i0=max(0,min(len(sents)-1,int(x.get("start_i",0)))); i1=max(i0,min(len(sents)-1,int(x.get("end_i",i0))))
                a=max(0.0,sents[i0]["start"]-0.25); b=min(dur,sents[i1]["end"]+0.35)
                if b-a>=6: out.append({"a":round(a,2),"b":round(b,2),"title":str(x.get("title") or sents[i0]["text"])[:70]})
            if out: return out[:n]
        except Exception:
            pass
    cands=[]                                    # zaxira: so'z zichligi eng yuqori oynalar
    for i in range(len(sents)):
        j=i
        while j+1<len(sents) and sents[j+1]["end"]-sents[i]["start"]<=target: j+=1
        a=sents[i]["start"]; b=sents[j]["end"]
        if b-a<min(12.0,target*0.4): continue
        nw=sum(len(sents[k]["text"].split()) for k in range(i,j+1))
        cands.append((nw/(b-a),a,b,sents[i]["text"]))
    cands.sort(reverse=True); out=[]
    for sc,a,b,t in cands:
        if all(b<=o["a"] or a>=o["b"] for o in out):
            out.append({"a":round(max(0,a-0.25),2),"b":round(min(dur,b+0.35),2),"title":" ".join(t.split()[:7])})
        if len(out)>=n: break
    return sorted(out,key=lambda o:o["a"])

def achannels(path):
    c,o=run(["ffprobe","-v","error","-select_streams","a:0","-show_entries","stream=channels","-of","default=noprint_wrappers=1:nokey=1",path],capture=True)
    try: return int(o.strip().split()[0])
    except Exception: return 0

def fit_filter(frame, pw, ph):
    """render_final dagi kadrlash bilan bir xil: aks/burish -> to'ldirish -> qirqish."""
    frame=frame or {}
    fz=max(1.0,min(3.0,float(frame.get("zoom",1) or 1))); fpx=max(-1.0,min(1.0,float(frame.get("x",0) or 0))); fpy=max(-1.0,min(1.0,float(frame.get("y",0) or 0)))
    frot=int(frame.get("rot",0) or 0)%360
    fpre=("hflip," if frame.get("mirror") else "")+("hflip,vflip," if frame.get("flip") else "")+({90:"transpose=1,",180:"hflip,vflip,",270:"transpose=2,"}.get(frot,""))
    return (f"{fpre}scale={int(pw*fz)//2*2}:{int(ph*fz)//2*2}:force_original_aspect_ratio=increase,"
            f"crop={pw}:{ph}:(iw-{pw})/2*{1+fpx:.3f}:(ih-{ph})/2*{1+fpy:.3f}")

def _gray_frames(video, t0, dur, vf, w, h, fps):
    import numpy as np
    p=subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-ss",f"{max(0.0,t0):.3f}","-t",f"{dur:.3f}","-i",video,"-an",
                      "-vf",f"{vf},fps={fps},format=gray","-f","rawvideo","-"],stdout=subprocess.PIPE)
    n=len(p.stdout)//(w*h)
    return np.frombuffer(p.stdout[:n*w*h],dtype=np.uint8).reshape(n,h,w).astype(np.float32) if n else np.zeros((0,h,w),np.float32)

def track_point(video, t0, dur, x, y, frame=None, ratio=(9,16), fps=10):
    """Kadrning (x,y) nuqtasidagi ob'ektni kuzatadi (andoza taqqoslash). -> [{t,x,y}] (qatlam boshidan vaqt, 0..1 koordinata)."""
    import numpy as np
    w=144; h=int(round(w*ratio[1]/ratio[0]/2))*2; dur=max(0.3,min(20.0,float(dur)))
    F=_gray_frames(video,t0,dur,fit_filter(frame,w,h),w,h,fps)
    if len(F)<2: raise RuntimeError("kuzatish uchun kadrlar o'qilmadi")
    r=max(7,w//9); sr=max(8,w//8)                        # andoza yarim o'lchami, qidiruv radiusi
    cx=int(round(max(0,min(1,x))*w)); cy=int(round(max(0,min(1,y))*h))
    cx=max(r,min(w-r-1,cx)); cy=max(r,min(h-r-1,cy))
    tpl=F[0][cy-r:cy+r+1,cx-r:cx+r+1].copy(); out=[{"t":0.0,"x":round(cx/w,4),"y":round(cy/h,4)}]
    for i in range(1,len(F)):
        f=F[i]; best=None; bx,by=cx,cy; tm=tpl-tpl.mean()
        for yy in range(max(r,cy-sr),min(h-r-1,cy+sr)+1):
            for xx in range(max(r,cx-sr),min(w-r-1,cx+sr)+1):
                pt=f[yy-r:yy+r+1,xx-r:xx+r+1]; d=pt-pt.mean()-tm
                sc=float((d*d).sum())+0.6*((xx-cx)**2+(yy-cy)**2)        # uzoqqa sakrashga kichik jarima
                if best is None or sc<best: best=sc; bx,by=xx,yy
        cx,cy=bx,by
        tpl=0.9*tpl+0.1*f[cy-r:cy+r+1,cx-r:cx+r+1]                      # andoza sekin yangilanadi
        out.append({"t":round(i/fps,3),"x":round(cx/w,4),"y":round(cy/h,4)})
    keep=[out[0]]                                         # siyraklashtirish: ~0.2s yoki sezilarli siljish
    for k in out[1:-1]:
        if k["t"]-keep[-1]["t"]>=0.2: keep.append(k)
    keep.append(out[-1])
    return keep

def smart_center(video, ratio=(9,16)):
    """Avto kadrlash: harakat va detal eng ko'p joyni topib, kadr siljishini qaytaradi. -> {x,y} (-1..1)."""
    import numpy as np
    sw,sh=ffsize(video); d=ffdur(video)
    if not sw or not sh or d<=0: raise RuntimeError("video o'qilmadi")
    w=96; h=max(2,int(round(w*sh/sw/2))*2); n=14
    F=_gray_frames(video,0,d,f"scale={w}:{h}",w,h,max(0.2,min(4.0,n/d)))
    if len(F)<2: raise RuntimeError("kadrlar yetarli emas")
    mot=np.abs(np.diff(F,axis=0)).mean(axis=0)
    det=np.abs(np.diff(F,axis=2)).mean(axis=0); det=np.pad(det,((0,0),(0,1)))
    E=mot*2.0+det*0.5; E=E-np.percentile(E,40); E[E<0]=0
    if E.sum()<=1e-6: return {"x":0.0,"y":0.0}
    cx=float((E.sum(axis=0)*np.arange(w)).sum()/E.sum())/w; cy=float((E.sum(axis=1)*np.arange(h)).sum()/E.sum())/h
    sar=sw/sh; tar=ratio[0]/ratio[1]; fx=fy=0.0
    if sar>tar*1.02:                                      # manba kengroq: gorizontal siljish
        vis=tar/sar; off=min(max(cx-vis/2,0),1-vis); fx=off/((1-vis)/2)-1
    elif sar<tar/1.02:
        vis=sar/tar; off=min(max(cy-vis/2,0),1-vis); fy=off/((1-vis)/2)-1
    return {"x":round(max(-1,min(1,fx)),2),"y":round(max(-1,min(1,fy)),2)}

def script_video(text, wav, outp, size=(1080,1920)):
    """Skript ovozi (wav) uchun asos video va so'z vaqtlarini tayyorlaydi. -> (davomiylik, words)"""
    d=ffdur(wav)
    if d<=0: raise RuntimeError("ovoz fayli o'qilmadi")
    run(["ffmpeg","-y","-hide_banner","-loglevel","error","-f","lavfi","-i",f"color=c=0x121218:s={size[0]}x{size[1]}:r=30:d={d+0.4:.2f}",
         "-i",wav,"-c:v","libx264","-preset","veryfast","-crf","23","-pix_fmt","yuv420p","-c:a","aac","-b:a","160k","-ar","48000","-shortest",outp])
    if not os.path.exists(outp): raise RuntimeError("asos video yaratilmadi")
    toks=text.split(); tot=sum(len(t)+1 for t in toks) or 1; t=0.12; span=max(0.3,d-0.3); words=[]
    for tk in toks:
        dd=span*(len(tk)+1)/tot; words.append({"w":tk,"s":round(t,3),"e":round(t+dd-0.02,3)}); t+=dd
    return ffdur(outp), words

# ================= FON MUSIQA (sintez — litsenziya kerak emas) =================
MUSIC_KINDS={"calm":("Sokin",72),"lofi":("Lo-fi",80),"energy":("Energiya",122),"cinema":("Kino",64),"happy":("Quvnoq",108),"corp":("Biznes",100)}
def gen_music(kind, dest, bars=16):
    """Oddiy, mualliflik huquqisiz fon musiqa: akkord yostig'i + bas + (ixtiyoriy) baraban. numpy bilan sintez -> WAV (stereo, 44.1k)."""
    import numpy as np, wave
    if kind not in MUSIC_KINDS: kind="calm"
    bpm=MUSIC_KINDS[kind][1]; sr=44100; beat=60.0/bpm; bar=beat*4; n=int(bars*bar*sr); t=np.arange(n)/sr
    A=220.0; nt=lambda k: A*2**(k/12.0)
    PROG={"calm":[(0,4,7,11),(-3,0,4,7),(-7,-3,0,4),(-5,-1,2,5)],            # Cmaj7 Am7 Fmaj7 G
          "lofi":[(-3,0,4,7),(-7,-3,0,4),(0,4,7,11),(-5,-1,2,5)],
          "energy":[(-3,0,4),(-7,-3,0),(0,4,7),(-5,-1,2)],
          "cinema":[(-3,0,4),(-3,0,4),(-7,-3,0),(-8,-5,-1)],
          "happy":[(0,4,7),(-5,-1,2),(-3,0,4),(-7,-3,0)],
          "corp":[(0,4,7),(-3,0,4),(-7,-3,0),(-5,-1,2)]}[kind]
    base=3 if kind!="cinema" else -9                       # C4 atrofida (kino pastroq)
    L=np.zeros(n); R=np.zeros(n)
    def tone(f,tt,ph=0.0,harm=(1,0.35,0.15)):
        return sum(a*np.sin(2*np.pi*f*(i+1)*tt+ph) for i,a in enumerate(harm))
    for b in range(bars):
        ch=PROG[b%len(PROG)]; i0=int(b*bar*sr); i1=min(n,int((b+1)*bar*sr)); seg=slice(i0,i1); m=i1-i0
        env=np.minimum(1,np.arange(m)/(0.25*sr))*np.minimum(1,(m-np.arange(m))/(0.35*sr))     # har takt silliq kiradi/chiqadi
        for j,k in enumerate(ch):
            f=nt(base+k); w=(tone(f,t[seg])+0.6*tone(f*1.004,t[seg],0.7))*env*0.055
            pan=0.5+0.28*(j-(len(ch)-1)/2)/max(1,len(ch)-1); L[seg]+=w*(1-pan)*2; R[seg]+=w*pan*2
        root=nt(base+ch[0]-12)                               # bas
        if kind in ("energy","happy","corp"):
            for q in range(8):
                a=i0+int(q*beat/2*sr); e=min(n,a+int(beat/2*sr)); tt=np.arange(e-a)/sr
                w=np.sin(2*np.pi*root*tt)*np.exp(-tt*7)*0.16; L[a:e]+=w; R[a:e]+=w
        else:
            w=np.sin(2*np.pi*root*t[seg])*env*0.11; L[seg]+=w; R[seg]+=w
        if kind in ("happy","lofi","corp"):                  # arpedjio
            notes=[ch[i%len(ch)]+(12 if kind=="happy" else 0) for i in range(8)]
            for q,k in enumerate(notes):
                if kind=="lofi" and q%2: continue
                a=i0+int(q*beat/2*sr); e=min(n,a+int(beat*0.9*sr)); tt=np.arange(e-a)/sr
                w=np.sin(2*np.pi*nt(base+k+12)*tt)*np.exp(-tt*5)*0.06; pan=0.3 if q%2 else 0.7; L[a:e]+=w*(1-pan)*2; R[a:e]+=w*pan*2
    rng=np.random.default_rng(7)
    def hit(at,kindh):
        a=int(at*sr)
        if a>=n: return
        if kindh=="kick":
            ln=int(0.22*sr); e=min(n,a+ln); tt=np.arange(e-a)/sr; w=np.sin(2*np.pi*(48+70*np.exp(-tt*28))*tt)*np.exp(-tt*13)*0.5
        elif kindh=="snare":
            ln=int(0.16*sr); e=min(n,a+ln); tt=np.arange(e-a)/sr; w=(rng.standard_normal(e-a)*0.5+np.sin(2*np.pi*190*tt)*0.4)*np.exp(-tt*24)*0.2
        else:
            ln=int(0.05*sr); e=min(n,a+ln); tt=np.arange(e-a)/sr; w=rng.standard_normal(e-a)*np.exp(-tt*90)*0.07
        L[a:e]+=w; R[a:e]+=w
    if kind in ("energy","happy","lofi","corp"):
        for b in range(bars):
            for q in range(4):
                tb=b*bar+q*beat
                if kind=="energy" or q in (0,2): hit(tb,"kick")
                if q in (1,3): hit(tb,"snare")
                hit(tb+beat/2,"hat")
                if kind=="energy": hit(tb,"hat")
    if kind=="cinema":                                       # sekin puls
        for b in range(bars):
            hit(b*bar,"kick")
    mx=max(1e-6,float(max(np.abs(L).max(),np.abs(R).max()))); g=0.8/mx
    fade=int(0.02*sr)
    for ch_ in (L,R):
        ch_[:fade]*=np.linspace(0,1,fade); ch_[-fade:]*=np.linspace(1,0,fade)
    pcm=(np.stack([L,R],axis=1)*g*32767).astype("<i2")
    with wave.open(dest,"wb") as wf:
        wf.setnchannels(2); wf.setsampwidth(2); wf.setframerate(sr); wf.writeframes(pcm.tobytes())
    return dest

# ================= RITM (beat) ANIQLASH — musiqaga kesimlarni moslash uchun =================
def detect_beats(path, limit=240):
    """Musiqadagi zarbalar vaqtlari (soniya). Sof Python: energiya o'zgarishi + avtokorrelyatsiya bilan temp."""
    import array, subprocess as sp
    try:
        raw=sp.run(["ffmpeg","-hide_banner","-loglevel","error","-t",str(limit),"-i",path,"-ac","1","-ar","8000","-f","s16le","-"],capture_output=True,timeout=120).stdout
    except Exception: return {"bpm":0,"beats":[]}
    a=array.array("h"); a.frombytes(raw[:len(raw)//2*2]); hop=160; n=len(a)//hop     # 20 ms qadam
    if n<100: return {"bpm":0,"beats":[]}
    en=[]
    for i in range(n):
        seg=a[i*hop:(i+1)*hop]; en.append(sum(v*v for v in seg)/hop)
    flux=[0.0]+[max(0.0,en[i]-en[i-1]) for i in range(1,n)]
    mx=max(flux) or 1.0; flux=[v/mx for v in flux]
    m=sum(flux)/n; f=[v-m for v in flux]
    best=(0,-1e9)
    for lag in range(15,51):                 # 0.30..1.00 s -> 60..200 BPM
        c=sum(f[i]*f[i+lag] for i in range(0,n-lag,2))
        if c>best[1]: best=(lag,c)
    lag=best[0] or 25
    ph=max(range(lag),key=lambda o:sum(flux[i] for i in range(o,n,lag)))
    beats=[]; i=ph
    while i<n:
        j=max(range(max(0,i-2),min(n,i+3)),key=lambda q:flux[q]); beats.append(round(j*hop/8000.0,3)); i+=lag
    return {"bpm":round(60.0/(lag*hop/8000.0),1),"beats":beats}

# ================= MATNLI BUYRUQ -> MONTAJ REJASI (Studio "prompt" va AI chat) =================
_PLAN_PROMPT=("You convert a video editor's free-text request into editing settings. The request may be in Uzbek, Russian or English. "
    "Return ONLY a JSON object. Include ONLY the keys the request actually asks to set or change; omit everything else. Allowed keys: "
    "\"subtitle\":{\"on\":bool,\"layout\":\"line|stack3\",\"words\":1-9,\"upper\":bool,\"position\":\"bottom|center|top\",\"size\":\"small|medium|large\","
    "\"color_main\":\"#RRGGBB\",\"color_second\":\"#RRGGBB\",\"color_accent\":\"#RRGGBB\",\"anim\":\"pop|bounce|rise|zoomin|fade|none\"}, "
    "\"pause_max\": seconds 0.25-1.5 (smaller = tighter, faster pacing; 0.3 fast, 0.5 medium, 0.8 relaxed), "
    "\"look\":\"clean|vivid|warm|cool|cinema|mono|moody\", \"slow_zoom\":0|1|2, \"auto_zoom\":bool (punch-in zooms on cuts), "
    "\"effect\":\"none|glow|grain|leak|glitch|shake|letterbox|flash|progress|vhs|duotone|dream\", "
    "\"transition\":\"none|zoom|flash|dip|blur|glitch|shake|slide_l|slide_r|slide_u|slide_d|spin\", "
    "\"broll\":0-8 (number of cutaway images), \"stickers\":bool (emoji stickers), \"hook\":bool (title at the start), \"hook_text\":string, "
    "\"ratio\":\"9:16|1:1|4:5|16:9\", \"music_vol\":0-1, \"voice_clean\":bool, \"sfx\":bool (sound effects), "
    "\"reply\": one short sentence in Uzbek (Latin script) saying what you set. Request: ")
_PLAN_ENUM={"look":("clean","vivid","warm","cool","cinema","mono","moody"),
    "effect":("none","glow","grain","leak","glitch","shake","letterbox","flash","progress","vhs","duotone","dream"),
    "transition":("none","zoom","flash","dip","blur","glitch","shake","slide_l","slide_r","slide_u","slide_d","spin"),
    "ratio":("9:16","1:1","4:5","16:9")}

def _plan_clean(j):
    """AI yoki qoidalardan kelgan rejani tekshirib, faqat ruxsat etilgan qiymatlarni qoldiradi."""
    if not isinstance(j,dict): return {}
    out={}
    su=j.get("subtitle")
    if isinstance(su,dict):
        o={}
        if "on" in su: o["on"]=bool(su["on"])
        if su.get("layout") in ("line","stack3"): o["layout"]=su["layout"]
        try:
            if "words" in su: o["words"]=max(1,min(9,int(su["words"])))
        except Exception: pass
        if "upper" in su: o["upper"]=bool(su["upper"])
        if su.get("position") in ("bottom","center","top"): o["position"]=su["position"]
        if su.get("size") in ("small","medium","large"): o["size"]=su["size"]
        for k in ("color_main","color_second","color_accent"):
            if _hexok(su.get(k),None): o[k]=_hexok(su.get(k),None)
        if su.get("anim") in ("pop","bounce","rise","zoomin","fade","none"): o["anim"]=su["anim"]
        if o: out["subtitle"]=o
    try:
        if "pause_max" in j: out["pause_max"]=round(max(0.25,min(1.5,float(j["pause_max"]))),2)
    except Exception: pass
    for k,vals in _PLAN_ENUM.items():
        if j.get(k) in vals: out[k]=j[k]
    if out.get("look"): out["look_values"]=dict(_LOOKS[out["look"]])
    try:
        if "slow_zoom" in j: out["slow_zoom"]=max(0,min(2,int(j["slow_zoom"])))
        if "broll" in j: out["broll"]=max(0,min(8,int(j["broll"])))
        if "music_vol" in j: out["music_vol"]=round(max(0.0,min(1.0,float(j["music_vol"]))),2)
    except Exception: pass
    for k in ("auto_zoom","stickers","hook","voice_clean","sfx"):
        if k in j: out[k]=bool(j[k])
    if isinstance(j.get("hook_text"),str) and j["hook_text"].strip(): out["hook_text"]=j["hook_text"].strip()[:60]
    if isinstance(j.get("reply"),str): out["reply"]=j["reply"].strip()[:200]
    return out

def _plan_rules(text):
    """AI ulanmagan bo'lsa: kalit so'zlar bo'yicha oddiy reja (uz/ru/en)."""
    t=" "+(text or "").lower()+" "; j={}; su={}; did=[]
    def has(*ws): return any(w in t for w in ws)
    neg=lambda *ws: any((w+" yo'q" in t) or (w+"siz" in t) or ("без "+w in t) or ("no "+w in t) or (w+" kerak emas" in t) or (w+" qo'yma" in t) for w in ws)
    if has("zich","tez temp","tezkor","dinamik","быстр","динамич","fast","tight","energetic"): j["pause_max"]=0.32; did.append("zich temp")
    elif has("sokin","sekin","erkin","спокойн","медлен","calm","slow","relaxed"): j["pause_max"]=0.9; did.append("sokin temp")
    if neg("subtitr","субтитр","subtitle","caption"): su["on"]=False; did.append("subtitrsiz")
    elif has("subtitr","субтитр","subtitle","caption"):
        su["on"]=True
        if has("3 pog'ona","uch pog'ona","3 qator","uch qator","three line","3 line","три строк"): su["layout"]="stack3"
        if has("1 qator","bir qator","karaoke","one line","одна строк","одну строк"): su["layout"]="line"
        if has("bitta so'z","bir so'z","one word","по одному слову"): su.update(layout="line",words=1)
    for names,col in ((("sariq","желт","yellow"),"#ffe14a"),(("yashil","зелен","зелён","green"),"#39e75f"),(("qizil","красн","red"),"#ff4d4d"),
                      (("ko'k","син","blue"),"#4d8dff"),(("pushti","розов","pink"),"#ff4fa3"),(("oq subtitr","белые субтитр","white caption"),"#ffffff")):
        if has(*names) and has("subtitr","субтитр","caption","subtitle","matn","текст","text"):
            su["color_second"]=col; su["color_accent"]=col; did.append("subtitr rangi"); break
    if has("katta harf","заглавн","uppercase","caps"): su["upper"]=True
    if has("kichik harf","lowercase","строчн"): su["upper"]=False
    if has("katta subtitr","крупные субтитр","big caption","large caption"): su["size"]="large"
    if has("markaz","по центру","в центр","center"): su["position"]="center"; did.append("subtitr markazda")
    if su: j["subtitle"]=su
    for names,look in ((("kino","cinema","кино"),"cinema"),(("iliq","тепл","warm"),"warm"),(("sovuq","холодн","cool"),"cool"),
                       (("oq-qora","oq qora","черно-бел","black and white","b&w","monochrome"),"mono"),(("yorqin","ярк","vivid","bright"),"vivid"),(("toza","clean","чист"),"clean")):
        if has(*names): j["look"]=look; did.append(look+" rang"); break
    if neg("b-roll","broll","rasm"): j["broll"]=0; did.append("b-rollsiz")
    else:
        mm=re.search(r"(\d)\s*(?:ta\s*)?(?:b-?roll|rasm|картин|image)",t)
        if mm: j["broll"]=int(mm.group(1)); did.append("%s ta b-roll"%mm.group(1))
        elif has("b-roll","broll"): j["broll"]=4; did.append("b-roll")
    if neg("stiker","emoji","стикер","эмодзи","sticker"): j["stickers"]=False; did.append("stikersiz")
    elif has("stiker","emoji","стикер","эмодзи","sticker"): j["stickers"]=True; did.append("stikerlar")
    if neg("zoom","зум"): j["auto_zoom"]=False; j["slow_zoom"]=0; did.append("zoomsiz")
    elif has("zoom","зум","yaqinlash"): j["auto_zoom"]=True; j["slow_zoom"]=2 if has("kuchli","сильн","strong") else 1; did.append("zoom")
    if neg("hook","sarlavha","заголов","title"): j["hook"]=False
    elif has("hook","sarlavha","заголов","title"): j["hook"]=True; did.append("hook sarlavha")
    for names,tr in ((("chaqnash","вспышк","flash"),"flash"),(("glitch","глитч"),"glitch"),(("xira","blur","размыт"),"blur"),(("aylan","spin","вращ"),"spin"),
                     (("surish","slide","сдвиг","свайп"),"slide_l"),(("silkin","shake","тряск"),"shake")):
        if has(*names): j["transition"]=tr; did.append(tr+" o'tish"); break
    if neg("o'tish","переход","transition"): j["transition"]="none"
    for r_ in ("9:16","1:1","4:5","16:9"):
        if r_ in t: j["ratio"]=r_; did.append("format "+r_)
    if has("kvadrat","квадрат","square"): j["ratio"]="1:1"
    if has("youtube","gorizontal","горизонт","horizontal","landscape"): j["ratio"]="16:9"
    if has("vhs","retro","ретро"): j["effect"]="vhs"
    elif has("letterbox","kino chiziq","черные полос"): j["effect"]="letterbox"
    elif has("grain","dona","зерн"): j["effect"]="grain"
    if neg("ovoz effekt","sfx","звуковые эффект"): j["sfx"]=False
    j["reply"]=("Qo'llandi: "+", ".join(did)+".") if did else "Aniq buyruq topilmadi. Masalan: «zich temp, sariq subtitr, 3 ta b-roll, stikersiz»."
    return j

def plan_from_prompt(text):
    """Erkin matn -> montaj rejasi. -> (reja, manba)"""
    text=(text or "").strip()[:800]
    if not text: return {"reply":"Buyruq bo'sh."},"rules"
    if have_vertex() or GEMINI_KEY:
        try:
            j=json.loads(_ai_json(_PLAN_PROMPT+json.dumps(text,ensure_ascii=False)))
            if isinstance(j,list): j=j[0] if j else {}
            pl=_plan_clean(j)
            if pl: return pl,"ai"
        except Exception:
            pass
    return _plan_clean(_plan_rules(text)),"rules"

def apply_cut(video_path, words, removes, work, dur):
    """removes = [[s,e],...] (o'chiriladigan). Kesadi, so'zlarni qayta moslaydi."""
    keep=subtract_ranges([(0.0,dur)], [tuple(r) for r in removes]) if removes else [(0.0,dur)]
    keep=[(a,b) for a,b in keep if b-a>0.1] or [(0.0,dur)]
    cut=os.path.join(work,"cut.mp4"); cut_video(video_path,cut,keep)
    w2=remap_words(words,keep) if len(keep)>1 else words
    return cut, w2, round(ffdur(cut),2)
