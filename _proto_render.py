"""PROTOTIPS — jaunais teksta dizains (pilni teikumi, godīgs ievads).
Palaiž: python _proto_render.py <listing_id>
NEAIZTIEK produkcijas wp_templates.py. Kad teksts ok → pārceļam kodā.
"""
import io, sys, os, re
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv(Path("..").resolve() / "sslv-ai-runner-railway" / "crm" / ".env")

_MISSING = {"", "unknown", "nezināms", "nav minēts", "none", "null", "n/a", "-", "~", "nē"}
_SALE = {"regular", "parastā", "parasta", "sale", "sell", "buy"}

_VEIDS = {
    "Birojs": "biroja telpas", "Tirdzniecība": "tirdzniecības telpas",
    "Noliktava": "noliktavas telpas", "Ražošana": "ražošanas telpas",
    "Medicīna": "medicīnas telpas", "Restorans/Cafe": "ēdināšanas telpas",
    "Studija": "studijas telpas", "Autoserviss": "autoservisa telpas",
    "Sporta zāle": "sporta telpas", "PVD": "pārtikas ražošanas telpas",
}
_BTYPE = {  # building_type → "kur" frāze (fallback, ja nav Building_description)
    "Biroju ēka": "biroju ēkā", "Tirdzniecības centrs": "tirdzniecības centrā",
    "Industriāla ēka": "ražošanas-noliktavu kompleksā", "Medicīnas centrs": "medicīnas centrā",
    "Autoserviss": "autoservisa ēkā", "Jaukta tipa ēka": "jaukta tipa ēkā",
}
_COND = {  # Space_condition → pilns teikums
    "Jauns": "Telpas ir jaunas, ar nesen veiktu remontu.",
    "Labs": "Telpu iekšējais stāvoklis ir labs — uzturēts un gatavs lietošanai.",
    "Lietots": "Telpas ir lietotas, taču pilnībā funkcionālas.",
    "Nepabeigts": "Telpas tiek nodotas pelēkajā apdarē, ļaujot pielāgot apdari savām vajadzībām.",
    "Nepieciešams remonts": "Telpām nepieciešams remonts.",
    "Kapitālais remonts": "Telpām nepieciešams kapitālais remonts.",
}
_GRIDAS = {
    "PVC / vinils": "vinila grīdas", "Kvarca vinils": "kvarcvinila grīdas",
    "Betona grīda": "betona grīdas", "Betona grīda ar trapiem": "betona grīdas ar trapiem",
    "Slīpēts betons": "slīpēta betona grīdas", "Betons ar hardeneri": "betona grīdas",
    "Epoksīda grīda": "epoksīda grīdas", "Poliuretāna-cementa grīda": "poliuretāna grīdas",
    "Cementa klons": "cementa klona grīdas", "Mikrocements": "mikrocementa grīdas",
    "Keramikas flīzes": "flīžu grīdas", "Porcelāna flīzes": "flīžu grīdas",
    "Dabīgais akmens": "akmens grīdas", "Linolejs": "linoleja grīdas",
    "Gumijas grīda": "gumijas grīdas", "Paklājflīzes": "paklāja segums (vietām flīzes)",
    "Koka grīda": "koka grīdas", "Parkets": "parketa grīdas", "Lamināts": "lamināta grīdas",
}
_LOGI = {"Lielie Logi": "lieliem logiem", "Standarta logi": "standarta logiem"}
_APKURE = {"Centrālā": "centrālā apkure", "Gāzes": "gāzes apkure", "Elektriskā": "elektriskā apkure"}
_PIELIET = {  # Potential_space_group → ģenitīvs ("piemērots arī X vajadzībām")
    "Birojs": "biroja", "Tirdzniecība": "tirdzniecības", "Noliktava": "noliktavas",
    "Ražošana": "ražošanas", "Medicīna": "medicīnas", "Restorans/Cafe": "ēdināšanas",
    "Studija": "studijas", "Autoserviss": "autoservisa", "Sporta zāle": "sporta",
    "PVD": "pārtikas ražošanas",
}
_PARK = {"Ir vietas": "autostāvvieta", "Ir vietas bezmaksas": "bezmaksas autostāvvieta",
         "Ir vietas par maksu": "maksas autostāvvieta", "Tikai ielas parking": "stāvvieta ielas malā"}
_STAVU = {1: "Vienstāva", 2: "Divstāvu", 3: "Trīsstāvu", 4: "Četrstāvu", 5: "Piecstāvu",
          6: "Sešstāvu", 7: "Septiņstāvu", 8: "Astoņstāvu", 9: "Deviņstāvu"}

# Priekšrocības — dzīvākas frāzes (ne tikai lietvārds)
_PRIEK = {
    "has_lift": "lifts ērtai piekļuvei augšējiem stāviem",
    "has_freight_lift": "kravas lifts smagumu pārvietošanai",
    "has_gym": "sporta zāle aktīvai atpūtai un treniņiem",
    "has_conference_room": "koplietošanas konferenču zāle, ko iespējams rezervēt tikšanām un prezentācijām",
    "has_underground_parking": "pazemes autostāvvieta, kas pasargā auto no laikapstākļiem",
    "has_ev_charging": "elektromobiļu uzlādes stacijas",
    "has_bike_parking": "velosipēdu novietne ikdienas pārvietošanās ērtībai",
    "has_solar": "saules paneļi uz jumta, kas samazina enerģijas izmaksas",
    "has_battery": "enerģijas uzkrāšanas sistēma nepārtrauktai darbībai",
    "has_generator": "rezerves ģenerators, kas garantē darbību arī elektrības pārtraukumu laikā",
    "has_showers": "dušas un ģērbtuves darbinieku ērtībām",
    "has_roof_terrace": "jumta terase darba pasākumiem vai pusdienu pārtraukumiem",
    "has_reception": "recepcija, kas sagaida klientus un apmeklētājus",
    "has_parcel_locker": "pakomāts sūtījumu ērtai saņemšanai",
    "has_security_24_7": "diennakts apsardze drošai darba videi",
    "has_cctv": "videonovērošana visā teritorijā",
    "has_access_control": "karšu piekļuves kontrole",
}
_PRIEK_ORDER = ["has_lift", "has_freight_lift", "has_gym", "has_conference_room",
                "has_underground_parking", "has_ev_charging", "has_bike_parking",
                "has_solar", "has_battery", "has_generator", "has_showers",
                "has_roof_terrace", "has_reception", "has_parcel_locker",
                "has_security_24_7", "has_cctv", "has_access_control"]


def c(v):
    if v is None:
        return None
    s = str(v).strip().lstrip("~").strip()
    return None if s.lower() in _MISSING else s

def chk(v):
    return str(v or "").strip().lower() == "checked"

def num(v):
    s = c(v)
    if not s:
        return None
    m = re.search(r"\d+[.,]?\d*", s)
    return m.group(0).replace(",", ".") if m else None

def dec_lv(v):  # 2.8 -> "2,8", 3.0 -> "3"
    s = num(v)
    if not s:
        return None
    f = float(s)
    return (str(int(f)) if f == int(f) else f"{f:.2f}".rstrip("0").rstrip(".")).replace(".", ",")

def money(v):
    s = num(v)
    if not s:
        return None
    f = float(s)
    s2 = str(int(f)) if f == int(f) else f"{f:.2f}"
    intp, _, dp = s2.partition(".")
    out = ""
    while len(intp) > 3:
        out = " " + intp[-3:] + out
        intp = intp[:-3]
    return intp + out + (f",{dp}" if dp else "")

def is_sale(pt):
    return str(pt or "").strip().lower() in _SALE

def join_lv(items):
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " un " + items[-1]

_BDESC_BAD = ("nav redzam", "ārpus", "arpus", "nav saskat", "nezin", "neredz",
              "bilde", "foto", "attēl", "attel")

def _clean_bdesc(s):
    """Izņem AI meta-klauzulas no Building_description (piem. 'Ārpuse nav redzama;').
    Sadala pa ';' un '.', izmet klauzulas ar aizliegtiem vārdiem, atlikušo saliek."""
    s = c(s)
    if not s:
        return None
    parts = re.split(r"[;.]", s)
    good = [p.strip() for p in parts
            if p.strip() and not any(b in p.lower() for b in _BDESC_BAD)]
    if not good:
        return None
    out = ". ".join(g[0].upper() + g[1:] for g in good)
    return out

_VALUE_OK = {"pēc skaitītāja", "pēc skaitītājiem", "iekļauts", "iekļauts cenā",
             "iekļauts nomas maksā", "atsevišķi", "bezmaksas"}

def value_like(s):
    """True, ja izmaksu lauks izskatās pēc reālas vērtības (cipars vai zināma
    īsa frāze), nevis AI teikums/junk ('fakts bez cenas')."""
    s = c(s)
    if not s:
        return False
    if re.search(r"\d", s):
        return True
    return s.strip().lower().rstrip(".") in _VALUE_OK


def parse_wc(v):
    """cik_WC = brīvteksts ('1 WC telpā' / '2 WC koplietošanā' / '3 WC').
    Atgriež (skaits|None, 'own'|'shared'|None)."""
    s = c(v)
    if not s:
        return None, None
    m = re.search(r"\d+", s)
    n = int(m.group(0)) if m else None
    low = s.lower()
    loc = "own" if "telp" in low else ("shared" if "koplieto" in low else None)
    return n, loc


def floor_n(v):
    s = c(v)
    if not s:
        return None, False
    low = s.lower()
    base = any(k in low for k in ("cokol", "pagrab", "suter"))
    m = re.search(r"\d+", s)
    return (int(m.group(0)) if m else None), base


def render(L, bp):
    g = lambda k: c(L.get(k))
    gb = lambda k: c(bp.get(k))
    sg = (L.get("Space_group") or "").strip()
    veids = _VEIDS.get(sg, "komerctelpas")
    sale = is_sale(L.get("price_type"))
    area = num(L.get("area_m2"))
    blocks = []

    # 1. VIRSRAKSTS
    if sale:
        inv = g("Investiciju_strategija")
        head = "Pārdod " + veids + (f" ({inv})" if inv else "")
    else:
        head = "Iznomā " + veids
    if area:
        head += f" – {area} m²"
    blocks.append(("B", head))

    # 2. IEVADS
    addr = gb("full_address") or g("street") or ""
    bdesc = _clean_bdesc(gb("Building_description") or g("Building_description"))
    bclass = (gb("building_class") or g("building_class") or "").strip().upper()
    btype = gb("building_type") or g("building_type")
    is_complex = chk(bp.get("is_business_complex"))  # jauns lauks, vēl nav DB
    verb = "Tiek pārdotas" if sale else "Tiek iznomātas"
    intro = []
    if is_complex:
        intro.append(f"{verb} {veids} modernā un aktīvā biznesa kompleksā {addr}.")
    elif bdesc:
        intro.append(f"{verb} {veids} {addr}.")
    else:
        kur = _BTYPE.get(btype, "")
        adj = "modernā " if bclass == "A" else ""
        kur_s = f" {adj}{kur}" if kur else ""
        intro.append(f"{verb} {veids}{kur_s} {addr}.".replace("  ", " "))
    if bdesc and not is_complex:
        intro.append(bdesc.strip().rstrip(".") + ".")
    # ēkas faktu teikums (nosaukums/stāvi/gads/managed)
    bname = gb("building_name")
    fy = num(bp.get("bdg_year"))
    fcount = num(bp.get("floors_count"))
    managed = chk(bp.get("has_managed"))
    subj = bname or "Ēka"
    eka_desc = ""
    if fcount and int(float(fcount)) in _STAVU:
        eka_desc = _STAVU[int(float(fcount))].lower() + " biznesa ēka"
    elif bname:
        eka_desc = "biznesa ēka"
    if eka_desc:
        s = f"{subj} ir {eka_desc}"
        if fy:
            s += f", celta {fy}. gadā"
        if managed:
            s += ", ko apsaimnieko profesionāla apsaimniekošanas kompānija"
        intro.append(s + ".")
    elif fy:
        intro.append(f"{subj} celta {fy}. gadā.")
    elif managed:
        intro.append("Ēku apsaimnieko profesionāla apsaimniekošanas kompānija.")
    # stāva teikums
    fn, base = floor_n(L.get("floor"))
    own_entr = chk(L.get("Sava_ieeja_check"))
    has_lift = chk(bp.get("has_lift"))
    if base:
        intro.append("Telpas atrodas cokolstāvā, kas labi piemērots saimnieciskām un noliktavas vajadzībām.")
    elif fn == 1:
        if own_entr:
            intro.append("Telpas atrodas 1. stāvā, kas nodrošina ērtu klientu plūsmu un labu redzamību.")
        else:
            intro.append("Telpas atrodas ērti pieejamā 1. stāvā.")
    elif fn and fn >= 2:
        lift = ", ēkā ar liftu" if has_lift else ""
        intro.append(f"Telpas atrodas {fn}. stāvā{lift}, klusā un reprezentablā darba vidē.")
    blocks.append(("P", " ".join(intro)))

    # 3. TELPU PLĀNOJUMS UN TEHNISKAIS STĀVOKLIS
    tech = []
    cond = g("Space_condition")
    if cond and cond in _COND:
        tech.append(_COND[cond])
    # plānojums: telpas + logi + griesti
    rooms = num(L.get("Cik_telpas"))
    logi = _LOGI.get(g("Logu_type") or "")
    ceil = dec_lv(L.get("Griestu_augstums"))
    if rooms:
        n = int(float(rooms))
        base_s = f"Kopā ir {n} atsevišķa telpa" if n == 1 else f"Kopā ir {n} atsevišķas telpas"
        ext = []
        if logi:
            ext.append(f"lieliem logiem" if logi == "lieliem logiem" else logi)
        if ceil:
            ext.append(f"{ceil} m augstiem griestiem")
        if ext:
            base_s += " ar " + join_lv(ext)
        tech.append(base_s + ".")
    elif logi or ceil:
        ext = []
        if logi:
            ext.append(logi)
        if ceil:
            ext.append(f"{ceil} m augstiem griestiem")
        tech.append("Telpas ir ar " + join_lv(ext) + ".")
    # iekšā: virtuve, WC, balkons, izlietne
    inside = []
    if chk(L.get("Virtuve_check")):
        inside.append("aprīkota virtuve")
    wc_n, wc_loc = parse_wc(L.get("cik_WC"))
    shared_wc = None
    if wc_loc == "own" or (wc_n and wc_loc is None):
        if (wc_n or 1) == 1:
            inside.append("savs sanitārais mezgls" if wc_loc == "own" else "sanitārais mezgls")
        else:
            inside.append(f"{wc_n} sanitārie mezgli")
    elif wc_loc == "shared":
        shared_wc = wc_n  # atsevišķi (nav telpā)
    if chk(L.get("Balkons_check")):
        inside.append("balkons")
    if chk(L.get("Ir_izlietne_telpa_check")):
        inside.append("sava izlietne")
    if inside:
        pron = "Tajā ir" if (rooms and int(float(rooms)) == 1) else "Tajās ir"
        tech.append(pron + " " + join_lv(inside) + ".")
    if shared_wc is not None:
        if shared_wc and shared_wc > 1:
            tech.append(f"Pieejami {shared_wc} koplietošanas sanitārie mezgli.")
        else:
            tech.append("Pieejams koplietošanas sanitārais mezgls.")
    # aprīkojums / inženierija — pa klauzulām (latviešu locījumi atšķiras pa verbiem)
    clauses = []
    gm = _GRIDAS.get(g("Gridas_materials") or "")
    if gm:
        fv = "ieklāts" if "segums" in gm else "ieklātas"
        clauses.append(f"{fv} {gm}")
    eng = []
    ap = _APKURE.get(g("Apkure") or "")  # nominatīvs: "centrālā apkure"
    if ap:
        eng.append(ap)
    if chk(L.get("Ventilacijas_sistema_check")):
        eng.append("ventilācijas sistēma")
    if eng:
        clauses.append("ierīkota " + join_lv(eng))
    if clauses:
        s = join_lv(clauses)
        tech.append(s[0].upper() + s[1:] + ".")
    # mēbeles
    mb = str(L.get("Mebeleta_telpa") or "").strip().lower()
    if mb in ("jā", "ja"):
        tech.append("Tās šobrīd ir mēbelētas.")
    elif mb in ("daļēji", "daleji"):
        tech.append("Tās šobrīd ir daļēji mēbelētas.")
    # ražošanas/noliktavas specifika
    if sg in ("Ražošana", "Noliktava", "Autoserviss"):
        kg = num(L.get("Gridas_izturiba_kg_m2"))
        if kg:
            tech.append(f"Grīdu nestspēja ir {kg} kg/m².")
        heavy = []
        pv = num(L.get("Pacelamie_varti_count"))
        if chk(L.get("Pacelamie_varti_check")):
            heavy.append(f"{pv} paceļamie vārti" if pv and pv != "0" else "paceļamie vārti")
        rp = num(L.get("Rampa_logistikai_count"))
        if chk(L.get("Rampa_logistikai_check")):
            heavy.append(f"{rp} iekraušanas rampa" if rp and rp != "0" else "iekraušanas rampa")
        if chk(L.get("Treifelis_Pacelajs")):
            heavy.append("kravas pacēlājs (telferis)")
        if chk(L.get("Auto_pacelajs_check")):
            heavy.append("auto pacēlājs")
        if heavy:
            tech.append("Loģistikai: " + join_lv(heavy) + ".")
    pot = g("Potential_space_group")
    if pot:
        gen = join_lv([_PIELIET.get(p.strip(), p.strip().lower())
                       for p in pot.split(",") if p.strip()])
        tech.append(f"Piemērotas arī {gen} vajadzībām.")
    if tech:
        blocks.append(("S", ("Telpu plānojums un tehniskais stāvoklis:", " ".join(tech))))

    # 4. PRIEKŠROCĪBAS (ēkas līmenis; dzīvākas frāzes; slieksnis: ≥1 īsta iespēja)
    bld = []
    real_amen = 0
    for fld in _PRIEK_ORDER:
        on = chk(bp.get(fld)) or (fld == "has_conference_room" and chk(bp.get("Has_conference_room")))
        if on:
            bld.append(_PRIEK[fld])
            real_amen += 1
    if chk(bp.get("has_canteen")):
        nm = c(bp.get("ednica_nosaukums"))
        bld.append(f'ēdnīca "{nm}" ērtām pusdienām uz vietas' if nm
                   else "ēdnīca ērtām pusdienām uz vietas")
        real_amen += 1
    if chk(L.get("Apsargajama_teritorija_check")) or chk(L.get("Nozogota_teritorija_check")) or chk(bp.get("has_fenced")):
        bld.append("apsargāta teritorija")
        real_amen += 1
    if chk(L.get("Vides_pieejamiba_check")) or chk(bp.get("has_accessibility")):
        bld.append("vides pieejamība")
        real_amen += 1
    park = _PARK.get(g("Parkings") or "")
    if park:
        bld.append((park + " darbiniekiem un klientiem") if "autostāvvieta" in park else park)
    if real_amen >= 1 and bld:
        blocks.append(("S", ("Priekšrocības:", "Ēkā ir " + join_lv(bld) + ".")))

    # 5. NOSACĪJUMI
    ppm2 = g("price_per_m2")
    price = g("price")
    av = num(L.get("area_m2"))
    if not ppm2 and price and av:
        try:
            ppm2 = str(round(float(price) / float(av), 2))
        except Exception:
            ppm2 = None
    cost = []
    if sale:
        if price:
            s = f"Cena ir {money(price)} EUR"
            if ppm2:
                s += f" ({money(ppm2)} EUR/m²)"
            cost.append(s + ".")
    else:
        if ppm2:
            s = f"Nomas maksa ir {money(ppm2)} EUR/m² mēnesī"
            if price:
                s += f" (kopā {money(price)} EUR)"
            cost.append(s + ".")
        elif price:
            cost.append(f"Nomas maksa ir {money(price)} EUR mēnesī.")
    # Izmaksu lauki: rāda TIKAI ja ir reāla vērtība (cipars / zināma frāze).
    # Ja tikai "fakts bez cenas" vai vāgs apraksts bez summas → neminām vispār.
    extra = []
    for label, val in [("apsaimniekošana", g("Apsaimniekosanas_maksa")),
                       ("NĪN", g("NIN")),
                       ("komunālie maksājumi", g("Komunalie")),
                       ("citi maksājumi", g("Papildu_maksas"))]:
        if value_like(val):
            extra.append(f"{label} {val}")
    if extra:
        cost.append("Papildus " + join_lv(extra) + ".")
    cost.append("Visām cenām pieskaitāms PVN.")
    blocks.append(("S", ("Nomas nosacījumi:" if not sale else "Pārdošanas nosacījumi:", " ".join(cost))))

    # 6. NOSLĒGUMS
    blocks.append(("P", "Sazinieties ar mums, lai uzzinātu vairāk vai vienotos par telpu apskati. 🏢"))
    return blocks


def to_text(blocks):
    out = []
    for typ, val in blocks:
        if typ == "B":
            out.append("**" + val + "**")
        elif typ == "P":
            out.append(val)
        elif typ == "S":
            head, body = val
            out.append("**" + head + "**\n" + body)
    return "\n\n".join(out)


if __name__ == "__main__":
    LID = int(sys.argv[1]) if len(sys.argv) > 1 else 759
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        L = conn.execute("SELECT * FROM properties.listings WHERE id=%s", (LID,)).fetchone()
        bp = {}
        if L.get("building_profile_id"):
            bp = conn.execute("SELECT * FROM properties.building_profiles WHERE id=%s",
                              (L["building_profile_id"],)).fetchone() or {}
    fields = ["Space_group", "building_type", "building_class", "area_m2", "floor", "price",
              "price_per_m2", "price_type", "Space_condition", "Cik_telpas", "Logu_type",
              "Griestu_augstums", "Gridas_materials", "Apkure", "Mebeleta_telpa", "Virtuve_check",
              "cik_WC", "Ventilacijas_sistema_check", "Parkings", "Sava_ieeja_check",
              "Apsargajama_teritorija_check", "Nozogota_teritorija_check", "Vides_pieejamiba_check",
              "Treifelis_Pacelajs", "Pacelamie_varti_check", "Rampa_logistikai_check",
              "Potential_space_group", "Building_description", "Investiciju_strategija"]
    print(f"### LISTING {LID}  ({L.get('street')}, {L.get('city')})  bp_id={L.get('building_profile_id')}")
    for f in fields:
        v = L.get(f)
        if v not in (None, "", "unknown"):
            print(f"   {f} = {v}")
    print("\n" + "=" * 70 + "\n")
    print(to_text(render(L, bp)))
