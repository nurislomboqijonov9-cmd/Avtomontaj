# -*- coding: utf-8 -*-
"""
VIZEN — shriftlarni build vaqtida yuklab oladi (Docker build ichida ishlaydi).
Google Fonts (raw.githubusercontent) dan statik TTF oladi; variable shriftlarni Bold (700) ga instance qiladi.
Idempotent: mavjud fayllarni qayta yuklamaydi. Bitta font tushmasa ham build to'xtamaydi.
"""
import os, sys, urllib.request, shutil

DST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
os.makedirs(DST, exist_ok=True)
BASE = "https://raw.githubusercontent.com/google/fonts/main/"

# (chiqish_fayl_nomi, repo_yo'li, variable?)  — nomlar index.html dagi FONTS bilan bir xil
FONTS = [
 ("Anton-Regular.ttf","ofl/anton/Anton-Regular.ttf",False),
 ("BebasNeue-Regular.ttf","ofl/bebasneue/BebasNeue-Regular.ttf",False),
 ("ArchivoBlack-Regular.ttf","ofl/archivoblack/ArchivoBlack-Regular.ttf",False),
 ("Oswald-Bold.ttf","ofl/oswald/Oswald[wght].ttf",True),
 ("Teko-Bold.ttf","ofl/teko/Teko[wght].ttf",True),
 ("AlfaSlabOne-Regular.ttf","ofl/alfaslabone/AlfaSlabOne-Regular.ttf",False),
 ("TitanOne-Regular.ttf","ofl/titanone/TitanOne-Regular.ttf",False),
 ("RussoOne-Regular.ttf","ofl/russoone/RussoOne-Regular.ttf",False),
 ("Righteous-Regular.ttf","ofl/righteous/Righteous-Regular.ttf",False),
 ("Montserrat-Bold.ttf","ofl/montserrat/Montserrat[wght].ttf",True),
 ("Poppins-Bold.ttf","ofl/poppins/Poppins-Bold.ttf",False),
 ("Inter-Bold.ttf","ofl/inter/Inter[opsz,wght].ttf",True),
 ("Manrope-Bold.ttf","ofl/manrope/Manrope[wght].ttf",True),
 ("Rubik-Bold.ttf","ofl/rubik/Rubik[wght].ttf",True),
 ("Nunito-Bold.ttf","ofl/nunito/Nunito[wght].ttf",True),
 ("RobotoSlab-Bold.ttf","ofl/robotoslab/RobotoSlab[wght].ttf",True),
 ("Baloo2-Bold.ttf","ofl/baloo2/Baloo2[wght].ttf",True),
 ("Bangers-Regular.ttf","ofl/bangers/Bangers-Regular.ttf",False),
 ("LuckiestGuy-Regular.ttf","ofl/luckiestguy/LuckiestGuy-Regular.ttf",False),
 ("Pacifico-Regular.ttf","ofl/pacifico/Pacifico-Regular.ttf",False),
 ("Lobster-Regular.ttf","ofl/lobster/Lobster-Regular.ttf",False),
 ("Caveat-Bold.ttf","ofl/caveat/Caveat[wght].ttf",True),
 ("PermanentMarker-Regular.ttf","apache/permanentmarker/PermanentMarker-Regular.ttf",False),
 # --- qo'shimcha 42 ta ---
 ("FjallaOne-Regular.ttf","ofl/fjallaone/FjallaOne-Regular.ttf",False),
 ("PassionOne-Bold.ttf","ofl/passionone/PassionOne-Bold.ttf",False),
 ("BowlbyOne-Regular.ttf","ofl/bowlbyone/BowlbyOne-Regular.ttf",False),
 ("SigmarOne-Regular.ttf","ofl/sigmarone/SigmarOne-Regular.ttf",False),
 ("Shrikhand-Regular.ttf","ofl/shrikhand/Shrikhand-Regular.ttf",False),
 ("Bungee-Regular.ttf","ofl/bungee/Bungee-Regular.ttf",False),
 ("BungeeInline-Regular.ttf","ofl/bungeeinline/BungeeInline-Regular.ttf",False),
 ("PaytoneOne-Regular.ttf","ofl/paytoneone/PaytoneOne-Regular.ttf",False),
 ("ConcertOne-Regular.ttf","ofl/concertone/ConcertOne-Regular.ttf",False),
 ("Staatliches-Regular.ttf","ofl/staatliches/Staatliches-Regular.ttf",False),
 ("SquadaOne-Regular.ttf","ofl/squadaone/SquadaOne-Regular.ttf",False),
 ("ChangaOne-Regular.ttf","ofl/changaone/ChangaOne-Regular.ttf",False),
 ("Fredoka-Bold.ttf","ofl/fredoka/Fredoka[wdth,wght].ttf",True),
 ("Kanit-Bold.ttf","ofl/kanit/Kanit-Bold.ttf",False),
 ("Rowdies-Bold.ttf","ofl/rowdies/Rowdies-Bold.ttf",False),
 ("Chango-Regular.ttf","ofl/chango/Chango-Regular.ttf",False),
 ("GreatVibes-Regular.ttf","ofl/greatvibes/GreatVibes-Regular.ttf",False),
 ("DancingScript-Bold.ttf","ofl/dancingscript/DancingScript[wght].ttf",True),
 ("Sacramento-Regular.ttf","ofl/sacramento/Sacramento-Regular.ttf",False),
 ("KaushanScript-Regular.ttf","ofl/kaushanscript/KaushanScript-Regular.ttf",False),
 ("Courgette-Regular.ttf","ofl/courgette/Courgette-Regular.ttf",False),
 ("AmaticSC-Bold.ttf","ofl/amaticsc/AmaticSC-Bold.ttf",False),
 ("PatrickHand-Regular.ttf","ofl/patrickhand/PatrickHand-Regular.ttf",False),
 ("GloriaHallelujah.ttf","ofl/gloriahallelujah/GloriaHallelujah.ttf",False),
 ("PlayfairDisplay-Bold.ttf","ofl/playfairdisplay/PlayfairDisplay[wght].ttf",True),
 ("AbrilFatface-Regular.ttf","ofl/abrilfatface/AbrilFatface-Regular.ttf",False),
 ("CormorantGaramond-Bold.ttf","ofl/cormorantgaramond/CormorantGaramond[wght].ttf",True),
 ("YesevaOne-Regular.ttf","ofl/yesevaone/YesevaOne-Regular.ttf",False),
 ("Orbitron-Bold.ttf","ofl/orbitron/Orbitron[wght].ttf",True),
 ("Audiowide-Regular.ttf","ofl/audiowide/Audiowide-Regular.ttf",False),
 ("Monoton-Regular.ttf","ofl/monoton/Monoton-Regular.ttf",False),
 ("BlackOpsOne-Regular.ttf","ofl/blackopsone/BlackOpsOne-Regular.ttf",False),
 ("PressStart2P-Regular.ttf","ofl/pressstart2p/PressStart2P-Regular.ttf",False),
 ("FasterOne-Regular.ttf","ofl/fasterone/FasterOne-Regular.ttf",False),
 ("Play-Bold.ttf","ofl/play/Play-Bold.ttf",False),
 ("Jura-Bold.ttf","ofl/jura/Jura[wght].ttf",True),
 ("Comfortaa-Bold.ttf","ofl/comfortaa/Comfortaa[wght].ttf",True),
 ("Pattaya-Regular.ttf","ofl/pattaya/Pattaya-Regular.ttf",False),
 ("ProstoOne-Regular.ttf","ofl/prostoone/ProstoOne-Regular.ttf",False),
 ("RuslanDisplay-Regular.ttf","ofl/ruslandisplay/RuslanDisplay-Regular.ttf",False),
 ("MarckScript-Regular.ttf","ofl/marckscript/MarckScript-Regular.ttf",False),
 ("Podkova-Bold.ttf","ofl/podkova/Podkova[wght].ttf",True),
]

def _get(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()

def _instance_bold(path):
    try:
        from fontTools.ttLib import TTFont
        from fontTools.varLib.instancer import instantiateVariableFont
        f = TTFont(path)
        if "fvar" not in f:
            return
        axes = {}
        for a in f["fvar"].axes:
            if a.axisTag == "wght":
                axes[a.axisTag] = 700 if a.minValue <= 700 <= a.maxValue else a.maxValue
            else:
                axes[a.axisTag] = a.defaultValue
        instantiateVariableFont(f, axes, inplace=True)
        f.save(path)
    except Exception as e:
        print("  instance fail:", os.path.basename(path), e)

def main():
    ok = 0; fail = []
    for outname, repo, is_var in FONTS:
        outp = os.path.join(DST, outname)
        if os.path.exists(outp) and os.path.getsize(outp) > 2000:
            ok += 1; continue
        got = False
        for attempt in range(2):
            try:
                data = _get(BASE + repo)
                if data[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf"):
                    break
                open(outp, "wb").write(data)
                if is_var:
                    _instance_bold(outp)
                got = True; ok += 1; break
            except Exception:
                continue
        if not got:
            fail.append(outname)
            print("  MISS:", outname)
    # DejaVu (system) — zaxira shrift
    dj = os.path.join(DST, "DejaVuSans-Bold.ttf")
    if not os.path.exists(dj):
        for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
            if os.path.exists(cand):
                try: shutil.copy(cand, dj)
                except Exception: pass
                break
    print(f"[fetch_fonts] tayyor: {ok}/{len(FONTS)} shrift, tushmadi: {len(fail)}")

if __name__ == "__main__":
    main()
