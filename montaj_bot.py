# -*- coding: utf-8 -*-
"""
Telegram MONTAJ BOT — video tashlaysiz, tayyor Reels qaytadi.
  • O'zbekcha aniq subtitr (Groq Whisper large-v3)
  • Aqlli kesish (gap o'rtasidan kesmaydi, uzun pauzalarni oladi)
  • So'z-ba-so'z animatsiyali subtitr (viral uslub)
  • 9:16 format + silliq zoom + ovoz tozalash

Ishga tushirish (kompyuter yoki serverda, internet OCHIQ bo'lishi kerak):
  1) pip install requests
  2) ffmpeg o'rnatilgan bo'lsin
  3) muhit o'zgaruvchilari:
       TELEGRAM_TOKEN=...   (@BotFather'dan)
       GROQ_API_KEY=...     (console.groq.com — bepul)
  4) python montaj_bot.py
"""
import os, re, json, time, tempfile, subprocess, shutil, requests

HERE = os.path.dirname(os.path.abspath(__file__))

# .env faylini o'qish (agar bor bo'lsa) — qo'shimcha kutubxonasiz
_envf = os.path.join(HERE, ".env")
if os.path.exists(_envf):
    for _line in open(_envf, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# ================= SOZLAMALAR =================
CFG = {
    "til": "uz",
    "groq_model": "whisper-large-v3",
    "jimlik_dB": -30,
    "jimlik_min_soniya": 0.9,      # faqat uzun pauzalar kesiladi (gap buzilmasin)
    "kesish_pad": 0.12,
    "sozlar_soni": 3,
    "shrift": "Anton",
    "shrift_olcham": 90,
    "asosiy_rang": "#FFFFFF",
    "faol_rang": "#FFEA00",
    "chegara_rang": "#000000",
    "chegara": 4,
    "bosh_harf": True,
    "past_chetdan": 340,
    "zoom": True,
    "ovoz_tozalash": True,
}
FONTS_DIR = os.path.join(HERE, "fonts") if os.path.isdir(os.path.join(HERE, "fonts")) else HERE

# ================= YORDAMCHI =================
def run(cmd, capture=False):
    if capture:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout
    return subprocess.run(cmd).returncode, ""

def ffdur(path):
    c,o = run(["ffprobe","-v","error","-show_entries","format=duration",
               "-of","default=noprint_wrappers=1:nokey=1", path], capture=True)
    try: return float(o.strip())
    except: return 0.0

def hexass(h):
    h=h.lstrip("#");
    if len(h)==3: h="".join(c*2 for c in h)
    return "&H00%s%s%s" % (h[4:6].upper(), h[2:4].upper(), h[0:2].upper())

def ts(t):
    if t<0: t=0
    return "%d:%02d:%05.2f" % (int(t//3600), int((t%3600)//60), t%60)

# ================= 1. TRANSKRIPSIYA (Groq) =================
def transcribe(audio_path):
    """Groq Whisper -> so'zlar [{w,s,e}] (haqiqiy vaqtlar bilan)."""
    with open(audio_path,"rb") as f:
        files={"file":(os.path.basename(audio_path), f, "audio/wav")}
        data={"model":CFG["groq_model"], "language":CFG["til"],
              "response_format":"verbose_json",
              "timestamp_granularities[]":"word"}
        headers={"Authorization":f"Bearer {GROQ_KEY}"}
        r=requests.post(GROQ_URL, headers=headers, files=files, data=data, timeout=180)
    r.raise_for_status()
    j=r.json()
    words=[]
    if j.get("words"):
        for w in j["words"]:
            t=(w.get("word") or "").strip()
            if t: words.append({"w":t,"s":float(w["start"]),"e":float(w["end"])})
    else:  # zaxira: segmentlardan
        for seg in j.get("segments",[]):
            t=(seg.get("text") or "").strip()
            if t: words.append({"w":t,"s":float(seg["start"]),"e":float(seg["end"])})
    return words, j.get("text","")

# ================= 2. AQLLI KESISH =================
def detect_silences(path):
    c,out=run(["ffmpeg","-hide_banner","-i",path,"-af",
        "silencedetect=noise=%ddB:d=%s"%(CFG["jimlik_dB"],CFG["jimlik_min_soniya"]),
        "-f","null","-"], capture=True)
    st=[float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)",out)]
    en=[float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)",out)]
    return list(zip(st, en+[None]*(len(st)-len(en))))

def keep_segments(dur, sils, pad):
    keep=[]; cur=0.0
    for s,e in sils:
        seg_end=max(cur, s+pad)
        if seg_end-cur>=0.3: keep.append((cur,seg_end))
        cur=max(0.0,(e-pad) if e else dur)
    if dur-cur>=0.3: keep.append((cur,dur))
    m=[]
    for a,b in keep:
        a,b=max(0,a),min(dur,b)
        if b-a<0.1: continue
        if m and a-m[-1][1]<0.08: m[-1]=(m[-1][0],b)
        else: m.append((a,b))
    return m

def cut_video(inp, outp, segs):
    if not segs or len(segs)<=1 and segs and segs[0][1]-segs[0][0]>=ffdur(inp)-0.5:
        shutil.copy(inp,outp); return [(0,ffdur(inp))]
    vsel="+".join("between(t,%.3f,%.3f)"%(a,b) for a,b in segs)
    fc=("[0:v]select='%s',setpts=N/FRAME_RATE/TB[v];"
        "[0:a]aselect='%s',asetpts=N/SR/TB[a]"%(vsel,vsel))
    c,_=run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",inp,
        "-filter_complex",fc,"-map","[v]","-map","[a]",
        "-c:v","libx264","-preset","veryfast","-crf","20","-c:a","aac","-b:a","160k",outp])
    if c!=0: shutil.copy(inp,outp)
    return segs

def remap_words_to_cut(words, segs):
    """Asl vaqtdagi so'zlarni KESILGAN videoning vaqtiga o'tkazadi."""
    # segs: asl [(a,b)]; kesilgan pozitsiya = oldingi segmentlar yig'indisi
    out=[]; cut_pos=0.0
    for a,b in segs:
        for w in words:
            if w["s"]>=a-0.05 and w["s"]<b+0.05:
                ns=cut_pos+(max(a,w["s"])-a); ne=cut_pos+(min(b,w["e"])-a)
                if ne>ns: out.append({"w":w["w"],"s":round(ns,3),"e":round(ne,3)})
        cut_pos+=(b-a)
    out.sort(key=lambda x:x["s"])
    return out

# ================= 3. ANIMATSIYALI SUBTITR =================
def group_cues(words, n):
    cues=[]; cur=[]
    for w in words:
        if cur and (len(cur)>=n or w["s"]-cur[-1]["e"]>0.7):
            cues.append(cur); cur=[]
        cur.append(w)
    if cur: cues.append(cur)
    return cues

def build_ass(words, W, H):
    base=hexass(CFG["asosiy_rang"]); acc=hexass(CFG["faol_rang"]); outl=hexass(CFG["chegara_rang"])
    mv=CFG["past_chetdan"]; upper=CFG["bosh_harf"]
    head=f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{CFG['shrift']},{CFG['shrift_olcham']},{base},{base},{outl},&H64000000,-1,0,0,0,100,100,0,0,1,{CFG['chegara']},2,2,100,100,{mv},1

[Events]
Format: Layer, Start, End, Style, MarginL, MarginR, MarginV, Effect, Text
"""
    lines=[]
    for cue in group_cues(words, CFG["sozlar_soni"]):
        disp=[ (w["w"].upper() if upper else w["w"]).replace("{","(").replace("}",")") for w in cue ]
        for i,w in enumerate(cue):
            parts=[]
            for j,d in enumerate(disp):
                parts.append("{\\c%s}%s{\\c%s}"%(acc,d,base) if j==i else d)
            txt=" ".join(parts)
            ov="{\\fad(50,0)\\fscx92\\fscy92\\t(0,110,\\fscx100\\fscy100)}" if i==0 else ""
            lines.append("Dialogue: 0,%s,%s,Main,0,0,0,,%s%s"%(ts(w["s"]),ts(w["e"]),ov,txt))
    return head+"\n".join(lines)+"\n"

# ================= 4. RENDER =================
def render(cut_video_path, ass_path, outp):
    W,H=1080,1920
    ass_esc=ass_path.replace("\\","/").replace(":","\\:")
    fdir=FONTS_DIR.replace("\\","/").replace(":","\\:")
    sub=f"ass='{ass_esc}':fontsdir='{fdir}'"
    if CFG["zoom"]:
        vf=(f"scale={int(W*1.12)}:{int(H*1.12)}:force_original_aspect_ratio=increase,"
            f"crop={int(W*1.12)}:{int(H*1.12)},"
            f"zoompan=z='min(1.0+0.0004*in,1.05)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps=30,"
            f"eq=contrast=1.05:saturation=1.08,setsar=1,{sub}")
    else:
        vf=(f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,{sub}")
    af="highpass=f=85,afftdn=nr=12,equalizer=f=3000:t=q:w=1.5:g=3,acompressor=threshold=-18dB:ratio=3,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000" if CFG["ovoz_tozalash"] else "aresample=48000"
    c,_=run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",cut_video_path,
        "-vf",vf,"-af",af,"-r","30","-c:v","libx264","-preset","medium","-crf","20",
        "-pix_fmt","yuv420p","-c:a","aac","-b:a","160k",outp])
    return c==0 and os.path.exists(outp)

def process_video(inp, workdir):
    base=os.path.join(workdir,"job")
    wav=base+"_16k.wav"
    run(["ffmpeg","-y","-loglevel","error","-i",inp,"-ar","16000","-ac","1",wav])
    words_orig,_=transcribe(wav)
    dur=ffdur(inp)
    sils=detect_silences(inp)
    segs=keep_segments(dur,sils,CFG["kesish_pad"])
    cut=base+"_cut.mp4"
    segs=cut_video(inp,cut,segs)
    words=remap_words_to_cut(words_orig,segs) if len(segs)>1 else words_orig
    ass=base+".ass"
    open(ass,"w",encoding="utf-8").write(build_ass(words,1080,1920))
    out=base+"_final.mp4"
    ok=render(cut,ass,out)
    return out if ok else None, len(words), dur, ffdur(cut)

# ================= TELEGRAM =================
def tg(method, **kw):
    r=requests.post(f"{TG_API}/{method}", timeout=60, **kw); return r.json()

def download_file(file_id, dest):
    j=tg("getFile", data={"file_id":file_id})
    if not j.get("ok"): return False
    path=j["result"]["file_path"]
    url=f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}"
    with requests.get(url,stream=True,timeout=300) as r:
        r.raise_for_status()
        with open(dest,"wb") as f:
            for c in r.iter_content(1<<16): f.write(c)
    return True

def main():
    if not TG_TOKEN or not GROQ_KEY:
        print("! TELEGRAM_TOKEN va GROQ_API_KEY muhit o'zgaruvchilarini o'rnating."); return
    print("Bot ishga tushdi. Telegram'da botga video tashlang.")
    offset=None
    while True:
        try:
            j=tg("getUpdates", data={"offset":offset,"timeout":50})
            for upd in j.get("result",[]):
                offset=upd["update_id"]+1
                msg=upd.get("message") or {}
                chat=msg.get("chat",{}).get("id")
                if not chat: continue
                vid=msg.get("video") or (msg.get("document") if (msg.get("document",{}).get("mime_type","").startswith("video")) else None)
                if msg.get("text","").startswith("/start"):
                    tg("sendMessage", data={"chat_id":chat,"text":"Salom! Menga video tashlang — kesib, o'zbekcha subtitr qo'yib, tayyor Reels qaytaraman."}); continue
                if not vid:
                    tg("sendMessage", data={"chat_id":chat,"text":"Iltimos, video yuboring."}); continue
                tg("sendMessage", data={"chat_id":chat,"text":"⏳ Qabul qildim, ishlayapman... (1-3 daqiqa)"})
                work=tempfile.mkdtemp()
                try:
                    inp=os.path.join(work,"in.mp4")
                    if not download_file(vid["file_id"], inp):
                        tg("sendMessage", data={"chat_id":chat,"text":"Videoni yuklab bo'lmadi (20MB dan katta bo'lishi mumkin)."}); continue
                    out,nwords,d0,d1=process_video(inp,work)
                    if not out:
                        tg("sendMessage", data={"chat_id":chat,"text":"Xatolik yuz berdi."}); continue
                    with open(out,"rb") as f:
                        tg("sendVideo", data={"chat_id":chat,
                            "caption":f"✅ Tayyor! {d0:.0f}s→{d1:.0f}s, {nwords} so'z subtitr."},
                            files={"video":f})
                except Exception as e:
                    tg("sendMessage", data={"chat_id":chat,"text":f"Xatolik: {str(e)[:200]}"})
                finally:
                    shutil.rmtree(work, ignore_errors=True)
        except Exception as e:
            print("loop err:", e); time.sleep(3)

if __name__=="__main__":
    main()
