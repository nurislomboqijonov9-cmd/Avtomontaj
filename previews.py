# -*- coding: utf-8 -*-
"""
VIZEN — uslub video-misollarini SERVERDA generatsiya qiladi (talab bo'yicha, keshlab).
/preview/m_<montaj_id>.mp4  va  /preview/s_<subtitr_id>.mp4
Repoda tayyor video saqlanmaydi — server o'zi bir marta chizadi va keshlaydi.
"""
import os, subprocess
import montaj

# montaj oilalari (index.html dagi FAMILIES bilan bir xil tartib/qiymat)
FAM = {
 "viral":("Montserrat","#ffea00","#ffffff",96,3,660,1),
 "bold":("Anton","#ff3b30","#ffffff",108,2,660,1),
 "neon":("Bebas Neue","#00e5ff","#ffffff",106,3,660,1),
 "clean":("Inter","#ffffff","#ffffff",74,4,620,0),
 "cinema":("Playfair Display","#f5d38b","#ffffff",80,4,560,0),
 "vlog":("Poppins","#22ff88","#ffffff",86,3,660,1),
 "biz":("Manrope","#37c6ff","#ffffff",80,4,640,0),
 "pod":("Oswald","#ff4fa3","#ffffff",90,3,640,1),
 "fashion":("Great Vibes","#ffffff","#ffd1e8",96,3,640,0),
 "fit":("Bangers","#ffea00","#ffffff",108,2,660,1),
 "hype":("Bungee","#ffe44d","#ffffff",96,2,660,1),
 "luxe":("Cormorant Garamond","#f0d698","#ffffff",96,3,600,0),
 "retro":("Monoton","#ffd000","#ffffff",92,3,660,1),
 "game":("Press Start 2P","#39ff14","#ffffff",64,3,660,1),
 "story":("Fredoka","#ffffff","#fff0fb",88,3,640,0),
 "news":("Staatliches","#ffffff","#ffffff",84,4,700,1),
 "beauty":("Dancing Script","#fff0f6","#ffffff",98,3,640,0),
 "street":("Permanent Marker","#facc15","#ffffff",92,3,660,1),
 "tech":("Orbitron","#22d3ee","#ffffff",80,3,660,1),
 "calm":("Comfortaa","#0ea5e9","#0b3b3b",78,4,620,0),
}
VAR = {  # sfx -> (anim, grade, zoom)
 "pop":("pop","vivid",1),"zoom":("zoomin","bright",2),"cine":("fade","cinema",1),
 "soft":("rise","warm",0),"max":("bounce","neon",2),
}
SCOL = {  # subtitr ranglari
 "yellow":"#ffe44d","cyan":"#00e5ff","red":"#ff3b30","green":"#22ff88","pink":"#ff4fa3",
 "violet":"#a78bfa","orange":"#ff9f1c","white":"#ffffff","gold":"#f0d698","lime":"#c6ff00",
 "sky":"#38bdf8","rose":"#fb7185","mint":"#5eead4","purple":"#c084fc","teal":"#2dd4bf",
 "coral":"#ff6b6b","blue":"#60a5fa","magenta":"#ff2d95","amber":"#fbbf24","ice":"#e0f2fe",
}
SCOL_ORDER = ["yellow","cyan","red","green","pink","violet","orange","white","gold","lime",
 "sky","rose","mint","purple","teal","coral","blue","magenta","amber","ice"]
SLAY = {  # sfx -> (anim, size, words, margin, upper)
 "pop":("pop",96,3,660,1),"karaoke":("none",88,4,640,1),"top":("rise",92,3,1250,1),
 "center":("zoomin",104,2,980,1),"mini":("fade",70,4,600,0),
}
SLAY_ORDER = ["pop","karaoke","top","center","mini"]
FPOOL = ["Anton","Bebas Neue","Montserrat","Poppins","Inter","Oswald","Archivo Black","Fjalla One",
 "Kanit","Rowdies","Bungee","Staatliches","Squada One","Passion One","Paytone One","Teko",
 "Rubik","Nunito","Manrope","Baloo 2","Concert One","Sigmar One","Titan One","Russo One","Righteous"]

WORDS = [{"w":"MANA","s":0.10,"e":0.60},{"w":"BU","s":0.62,"e":0.95},
         {"w":"VIZEN","s":1.00,"e":1.65},{"w":"USLUB","s":1.70,"e":2.25},
         {"w":"ZO'R","s":2.30,"e":2.70}]

def _sample_bg(cache_dir):
    bg = os.path.join(cache_dir, "_bg.mp4")
    if os.path.exists(bg) and os.path.getsize(bg) > 2000:
        return bg
    cmd = ["ffmpeg","-y","-hide_banner","-loglevel","error",
           "-f","lavfi","-i","gradients=s=1280x720:speed=0.015:x0=0:y0=0:x1=1280:y1=720:c0=0x2a1e5c:c1=0x0e7490:c2=0xb1257a:duration=3:rate=30",
           "-f","lavfi","-i","anullsrc=r=48000:cl=stereo",
           "-t","3","-c:v","libx264","-preset","veryfast","-crf","22","-c:a","aac","-shortest",bg]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return bg if os.path.exists(bg) else None

# ---- HAQIQIY STOK-VIDEO FON (Pexels, bepul, tijoratga ruxsat) ----
# oila -> qidiruv mavzusi (vertikal stok-video)
THEME_FAM = {
 "viral":"influencer talking phone selfie","bold":"city street fast walk","neon":"night city neon lights",
 "clean":"minimal desk workspace","cinema":"cinematic drone landscape","vlog":"travel vlog walking pov",
 "biz":"business office meeting","pod":"podcast microphone studio","fashion":"fashion model portrait",
 "fit":"gym workout fitness training","hype":"party crowd concert","luxe":"luxury lifestyle gold",
 "retro":"retro vintage film grain","game":"gaming setup rgb neon","story":"nature calm forest",
 "news":"city skyline aerial","beauty":"beauty makeup portrait woman","street":"skateboard street style",
 "tech":"technology data abstract","calm":"ocean sunset calm waves",
}
SUB_THEME = "person talking closeup portrait"

def _theme_for(fname):
    name = fname[:-4] if fname.endswith(".mp4") else fname
    if name.startswith("m_"):
        fid = name[2:].rsplit("_",1)[0]
        return THEME_FAM.get(fid, "cinematic lifestyle"), "m_"+fid
    return SUB_THEME, "s_generic"

def _norm_bg(src, dst):
    """Stok klipni 3s, 720 bo'y, jimjit ovoz bilan normallaymiz (render_final [0:a] talab qiladi)."""
    cmd = ["ffmpeg","-y","-hide_banner","-loglevel","error","-i",src,
           "-f","lavfi","-i","anullsrc=r=48000:cl=stereo",
           "-t","3","-map","0:v:0","-map","1:a:0","-vf","scale=-2:720,fps=30",
           "-c:v","libx264","-preset","veryfast","-crf","23","-c:a","aac","-shortest",dst]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.exists(dst) and os.path.getsize(dst) > 2000

def _pexels_video_url(query):
    import json as _json, urllib.request
    key = os.environ.get("PEXELS_API_KEY","").strip()
    if not key: return None
    try:
        u = "https://api.pexels.com/videos/search?orientation=portrait&size=small&per_page=8&query=" + urllib.parse.quote(query)
        req = urllib.request.Request(u, headers={"Authorization": key, "User-Agent":"VIZEN"})
        data = _json.loads(urllib.request.urlopen(req, timeout=25).read().decode("utf-8"))
        best = None
        for v in data.get("videos", []):
            for f in v.get("video_files", []):
                w = f.get("width") or 0; h = f.get("height") or 0
                if f.get("file_type") != "video/mp4": continue
                if h and w and h >= w and 400 <= w <= 900:   # vertikal, kichik
                    if best is None or w < best[0]:
                        best = (w, f.get("link"))
        if best: return best[1]
        # bo'lmasa har qanday mp4
        for v in data.get("videos", []):
            for f in v.get("video_files", []):
                if f.get("file_type")=="video/mp4" and f.get("link"):
                    return f["link"]
    except Exception:
        return None
    return None

import urllib.parse
def _real_bg(theme, tag, cache_dir):
    """theme mavzusiga mos haqiqiy stok-video foni; topilmasa gradient."""
    bgdir = os.path.join(cache_dir, "bg"); os.makedirs(bgdir, exist_ok=True)
    dst = os.path.join(bgdir, tag + ".mp4")
    if os.path.exists(dst) and os.path.getsize(dst) > 2000:
        return dst
    link = _pexels_video_url(theme)
    if link:
        try:
            import urllib.request
            raw = os.path.join(bgdir, tag + "_raw.mp4")
            req = urllib.request.Request(link, headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as r, open(raw,"wb") as f:
                f.write(r.read(12*1024*1024))   # <=12MB
            if _norm_bg(raw, dst):
                try: os.remove(raw)
                except Exception: pass
                return dst
        except Exception:
            pass
    return _sample_bg(cache_dir)   # zaxira: gradient

def params_for(fname):
    """fname -> (sub_dict, grade, zoom) yoki None."""
    name = fname[:-4] if fname.endswith(".mp4") else fname
    if name.startswith("m_"):
        rest = name[2:]
        # oxirgi bo'lak = variant sfx
        parts = rest.rsplit("_", 1)
        if len(parts) != 2: return None
        fid, sfx = parts
        if fid not in FAM or sfx not in VAR: return None
        font, ac, ba, sz, wd, mg, up = FAM[fid]
        anim, grade, zoom = VAR[sfx]
        sub = {"delay":0,"margin_v":mg,"size":sz,"words":wd,"active":ac,"base":ba,
               "upper":bool(up),"anim":anim,"font":font,"outline":"#000000","border":4}
        fx = montaj.FX_MAP.get(fid, "none")
        return sub, grade, zoom, fx
    if name.startswith("s_"):
        rest = name[2:]
        parts = rest.rsplit("_", 1)
        if len(parts) != 2: return None
        cid, sfx = parts
        if cid not in SCOL or sfx not in SLAY: return None
        anim, sz, wd, mg, up = SLAY[sfx]
        ci = SCOL_ORDER.index(cid); li = SLAY_ORDER.index(sfx)
        font = FPOOL[(ci*len(SLAY_ORDER)+li) % len(FPOOL)]
        sub = {"delay":0,"margin_v":mg,"size":sz,"words":wd,"active":SCOL[cid],"base":"#ffffff",
               "upper":bool(up),"anim":anim,"font":font,"outline":"#000000","border":4}
        return sub, "vivid", 0, "none"
    return None

def build_preview(fname, cache_dir):
    p = params_for(fname)
    if not p: return None
    sub, grade, zoom, fx = p
    os.makedirs(cache_dir, exist_ok=True)
    theme, tag = _theme_for(fname)
    bg = _real_bg(theme, tag, cache_dir)   # haqiqiy stok-video, bo'lmasa gradient
    if not bg: bg = _sample_bg(cache_dir)
    if not bg: return None
    montaj.QMAP["thumb"] = (264, 468, 31)
    ass = os.path.join(cache_dir, "_" + fname.replace(".mp4","") + ".ass")
    open(ass, "w", encoding="utf-8").write(montaj.build_ass(WORDS, sub, "uz"))
    out = os.path.join(cache_dir, fname)
    ok = montaj.render_final(bg, ass, out, [], zoom=zoom > 0, audio_clean=False,
                             grade=grade, zoom_level=zoom, quality="thumb", fast=True, fx=fx)
    try: os.remove(ass)
    except Exception: pass
    return out if ok else None
