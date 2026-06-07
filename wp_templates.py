"""Slot-based teksta šabloni WP property publish (NEAI, $0).

JAUNAIS DIZAINS (2026-06-01, Raimonds): pilni teikumi aģenta stilā, 0% live AI
— 100% deterministisks slot-šablons. AI tikai aizpilda DB lauku vērtības, ko
šablons nolasa. (Iepriekšējais bullet-•-stils aizstāts.)

Struktūra (render_body):
  1. Virsraksts (Iznomā/Pārdod {veids} – {area} m²)
  2. Ievads — godīgs: is_business_complex → komplekss; citādi Building_description
     nes ēkas raksturu; + ēkas fakti (nosaukums/stāvi/gads) + stāva teikums.
  3. "Telpu plānojums un tehniskais stāvoklis:" — pilni teikumi.
  4. "Priekšrocības:" — ēkas līmeņa iespējas (≥1 īsta → sadaļa parādās).
  5. "Nomas / Pārdošanas nosacījumi:" — cena + izmaksas (tikai ar reālu vērtību) + PVN.
  6. Noslēgums.

Lauks parādās tikai, ja aizpildīts (tukšus/'unknown' izlaiž klusi). Ēkas-līmeņa
lauki (building_name, has_*, ...) nāk no building_profiles (mig 030); ja NULL →
teksts iztiek bez tiem ("raksta kā raksta"), ja aizpildīts → ievij.

Lietošana:
    from wp_templates import render_body, render_excerpt, SUPPORTED_GROUPS
    body = render_body(space_group, listing, bp)   # bp = building_profile dict
"""
from __future__ import annotations

import re
import sys
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_MISSING = {"", "unknown", "unkown", "nezināms", "nezinams", "nav minēts",
            "nav", "none", "null", "n/a", "-", "~", "nē", "ne"}
_SALE_PT = {"regular", "parastā", "parasta", "sale", "sell", "buy"}

# Space_group → lietojams lietvārds ("Iznomā {X}")
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
# Tirdzniecības centriem — vispārīgs teksts (der VISIEM t/c; NE AI Building_description,
# kas mēdz minēt skatlogus/komerctelpas pirmajos stāvos — neder visiem). Raimonds 2026-06-03.
_TC_STANDARD = ("Centrā ir regulāra apmeklētāju plūsma un ērta piekļuve, kas telpām "
                "nodrošina labu atpazīstamību un pastāvīgu klientu klātbūtni.")
_COND = {  # Space_condition → pilns teikums
    "Jauns": "Telpas ir jaunas, ar nesen veiktu remontu.",
    "Labs": "Telpu iekšējais stāvoklis ir labs — uzturēts un gatavs lietošanai.",
    "Lietots": "Telpas ir lietotas, taču pilnībā funkcionālas.",
    "Nepabeigts": "Telpas tiek nodotas pelēkajā apdarē, ļaujot pielāgot apdari savām vajadzībām. To varat darīt gan Jūs kopā ar profesionāliem meistariem, vai arī īpašnieks pēc atsevišķas vienošanās.",
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
# Specializētām telpām (virtuve/ražošana/medicīna/serviss/sports) "Birojs" kā
# alternatīva NAV reāls — prasa pavisam citu apdari. Tirdzniecību u.c. atstājam.
# (Raimonds 2026-06-05; AI Potential_space_group mēdz pārģenerēt.)
_NO_OFFICE_GROUPS = {"Restorans/Cafe", "PVD", "Ražošana", "Noliktava",
                     "Autoserviss", "Medicīna", "Sporta zāle"}
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

SUPPORTED_GROUPS = sorted(_VEIDS.keys())

# Rīgas rajons/apkaime → ĢENITĪVS (virsrakstam "X rajonā"). Tikai Rīgas
# apkaimes — ārpus-Rīgas pilsētām/pagastiem (Jelgava, "mārupes pag.") "rajonā"
# neder → tos NAV šeit → frāzi izlaiž. Key = lowercase (DB case jaukts).
_DISTRICT_GEN = {
    # Labais krasts
    "centrs": "Centra", "vecrīga": "Vecrīgas", "klusais centrs": "Klusā centra",
    "latgales rajons": "Latgales", "maskavas rajons": "Maskavas",
    "latgales priekšpilsēta": "Latgales priekšpilsētas",
    "dārzciems": "Dārzciema", "pļavnieki": "Pļavnieku", "pļavinieki": "Pļavinieku",
    "purvciems": "Purvciema", "ķengarags": "Ķengaraga", "šķirotava": "Šķirotavas",
    "dreiliņi": "Dreiliņu", "pētersala": "Pētersalas", "brasa": "Brasas",
    "skanste": "Skanstes", "grīziņkalns": "Grīziņkalna", "teika": "Teikas",
    "čiekurkalns": "Čiekurkalna", "vef": "VEF", "sarkandaugava": "Sarkandaugavas",
    "mežaparks": "Mežaparka", "jaunciems": "Jaunciema", "mežciems": "Mežciema",
    "jugla": "Juglas", "berģi": "Berģu", "rumbula": "Rumbulas",
    "dārziņi": "Dārziņu", "vecmīlgrāvis": "Vecmīlgrāvja", "jaunmīlgrāvis": "Jaunmīlgrāvja",
    "vecdaugava": "Vecdaugavas", "trīsciems": "Trīsciema", "bukulti": "Bukultu",
    "berkši": "Berkšu", "brekši": "Brekšu", "mangaļsala": "Mangaļsalas",
    "mangaļi": "Mangaļu", "vecāķi": "Vecāķu", "jaunmīlgravis": "Jaunmīlgrāvja",
    "andrejsala": "Andrejsalas", "pētersalas-andrejsala": "Pētersalas-Andrejsalas",
    "avoti": "Avotu", "atgāzene": "Atgāzenes", "kundziņsala": "Kundziņsalas",
    # Kreisais krasts
    "torņkalns": "Torņkalna", "torņakalns": "Torņakalna", "āgenskalns": "Āgenskalna",
    "ziepniekalns": "Ziepniekalna", "ziepniekkalns": "Ziepniekkalna",
    "iļģuciems": "Iļģuciema", "zolitūde": "Zolitūdes", "šampēteris": "Šampētera",
    "pleskodāle": "Pleskodāles", "šampēteris-pleskodāle": "Šampētera-Pleskodāles",
    "dzirciems": "Dzirciema", "imanta": "Imantas", "kleisti": "Kleistu",
    "bieriņi": "Bieriņu", "dzegužkalns": "Dzegužkalna", "zasulauks": "Zasulauka",
    "bolderāja": "Bolderājas", "daugavgrīva": "Daugavgrīvas", "buļļi": "Buļļu",
    "beberbeķi": "Beberbeķu", "ķīpsala": "Ķīpsalas", "kleisti-suži": "Kleistu",
    "klīversala": "Klīversalas", "lucavsala": "Lucavsalas", "bieķēnsala": "Bieķēnsalas",
    "katlakalns": "Katlakalna", "voleri": "Voleru", "zasulauks-bišumuiža": "Zasulauka",
}


def _districts_phrase(raw) -> str:
    """district (1 vai vairāki pa komatam) → 'X rajonā' ģenitīvā (Rīgas apkaimes).
      'purvciems'                       → 'Purvciema rajonā'
      'Purvciems, Dzirciems'            → 'Purvciema un Dzirciema rajonā'
      'Purvciems, Dzirciems, Latgales rajons' → 'Purvciema, Dzirciema un Latgales rajonā'
    Tikai zināmas Rīgas apkaimes; nepazīstams → izlaiž ('')."""
    raw = _clean(raw)
    if not raw:
        return ""
    gens = []
    for p in raw.split(","):
        p = p.strip().lower()
        g = _DISTRICT_GEN.get(p)
        if g and g not in gens:
            gens.append(g)
    if not gens:
        return ""
    if len(gens) == 1:
        body = gens[0]
    elif len(gens) == 2:
        body = gens[0] + " un " + gens[1]
    else:
        body = ", ".join(gens[:-1]) + " un " + gens[-1]
    return body + " rajonā"


# Ārpus-Rīgas pilsēta/novads/pagasts → LOKATĪVS ('kur?'). Key = lowercase.
# city DB bieži "Jelgava un raj." → _norm_city nogriež " un raj./novads".
_CITY_LOC = {
    "jelgava": "Jelgavā", "daugavpils": "Daugavpilī", "liepāja": "Liepājā",
    "rēzekne": "Rēzeknē", "jēkabpils": "Jēkabpilī", "valmiera": "Valmierā",
    "ventspils": "Ventspilī", "jūrmala": "Jūrmalā", "ogre": "Ogrē",
    "tukums": "Tukumā", "cēsis": "Cēsīs", "bauska": "Bauskā", "sigulda": "Siguldā",
    "salaspils": "Salaspilī", "olaine": "Olainē", "ķekava": "Ķekavā",
    "preiļi": "Preiļos", "kuldīga": "Kuldīgā", "dobele": "Dobelē",
    "limbaži": "Limbažos", "talsi": "Talsos", "madona": "Madonā",
    "lielvārde": "Lielvārdē", "saldus": "Saldū", "krāslava": "Krāslavā",
    "aizkraukle": "Aizkrauklē", "alūksne": "Alūksnē", "gulbene": "Gulbenē",
    "ropaži": "Ropažos", "ikšķile": "Ikšķilē", "ķegums": "Ķegumā",
    "baldone": "Baldonē", "saulkrasti": "Saulkrastos", "vangaži": "Vangažos",
    "baloži": "Baložos", "carnikava": "Carnikavā", "līvāni": "Līvānos",
    "dunava": "Dunavā", "ļaudona": "Ļaudonā", "ādaži": "Ādažos",
    "mārupe": "Mārupē", "babīte": "Babītē", "stopiņi": "Stopiņos",
    "garkalne": "Garkalnē", "mālpils": "Mālpilī", "sēja": "Sējā",
    "vecumnieki": "Vecumniekos", "iecava": "Iecavā", "ozolnieki": "Ozolniekos",
    "sloka": "Slokā", "kauguri": "Kauguros", "bulduri": "Bulduros",
    "lielupe": "Lielupē", "ķekava": "Ķekavā", "līgatne": "Līgatnē",
    "sigulda": "Siguldā", "krimulda": "Krimuldā", "ropaži": "Ropažos",
    # DB pagastu/novadu formas (district) → centra lokatīvs
    "ādažu nov.": "Ādažos", "mārupes pag.": "Mārupē", "babītes pag.": "Babītē",
    "ķekavas pag.": "Ķekavā", "stopiņu nov.": "Stopiņos", "ropažu nov.": "Ropažos",
    "ozolnieku pag.": "Ozolniekos", "garkalnes nov.": "Garkalnē",
    "mālpils pag.": "Mālpilī", "sējas nov.": "Sējā", "vecumnieku pag.": "Vecumniekos",
    "iecavas nov.": "Iecavā", "ikšķiles l. t.": "Ikšķilē", "salaspils l. t.": "Salaspilī",
    "saulkrastu l. t.": "Saulkrastos", "baldones l. t.": "Baldonē",
    "skultes pag.": "Skultē", "krimuldas pag.": "Krimuldā", "sējas nov.": "Sējā",
    "daugmales pag.": "Daugmalē", "ķeguma": "Ķegumā", "olaines nov.": "Olainē",
}


def _norm_city(s) -> str:
    """city DB → tīrs lowercase: 'Jelgava un raj.' → 'jelgava',
    'Bauska un novads.' → 'bauska', 'Rīgas rajons' → 'rīgas rajons' (nav pilsēta)."""
    s = (_clean(s) or "").lower()
    for tail in (" un raj.", " un raj", " un novads.", " un novads", " un nov.", " un nov"):
        if s.endswith(tail):
            s = s[: -len(tail)]
            break
    return s.strip()


def _location_phrase(district, city) -> str:
    """Virsraksta lokācija: Rīgas apkaime → 'X rajonā'; ārpus-Rīga → pilsēta
    lokatīvā 'Jelgavā'. Nezināms → ''. district prioritāte (specifiskāks)."""
    rp = _districts_phrase(district)
    if rp:
        return rp
    for src in (district, _norm_city(city)):
        loc = _CITY_LOC.get((_clean(src) or "").lower())
        if loc:
            return loc
    return ""


# ─── Helperi ──────────────────────────────────────────────────────────────
def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().lstrip("~").strip()
    return None if s.lower() in _MISSING else (s or None)


def _truthy(v) -> bool:
    """True ja boolean True (building_profiles has_* lauki, mig 030) VAI
    listings-stila teksts 'checked' / 'jā' / 'true' / '1'. NULL/False/'not
    checked' → False. Tā render strādā gan ar bp boolean, gan listings text."""
    if v is True:
        return True
    if v is None or v is False:
        return False
    return str(v).strip().lower() in ("checked", "true", "yes", "jā", "ja", "1", "t")


def _num(v) -> Optional[str]:
    s = _clean(v)
    if not s:
        return None
    m = re.search(r"\d+[.,]?\d*", s)
    return m.group(0).replace(",", ".") if m else None


def _dec_lv(v) -> Optional[str]:  # 2.8 -> "2,8", 3.0 -> "3"
    s = _num(v)
    if not s:
        return None
    f = float(s)
    return (str(int(f)) if f == int(f) else f"{f:.2f}".rstrip("0").rstrip(".")).replace(".", ",")


def _trim_dec(v) -> str:
    """Cenas decimāldaļas likums: vesels → bez decimālēm; kapeikas → 2 cipari.
    BEZ tūkstošu atstarpēm. Nečīkst, ja nav skaitlis (importē publish_to_wp)."""
    s = str(v or "").strip()
    if not s:
        return s
    try:
        f = float(s.replace(",", "."))
    except ValueError:
        return s
    return str(int(f)) if f == int(f) else f"{f:.2f}"


def _money(v) -> str:
    """Cena ar tūkstošu atstarpi, decimāldaļa ar PUNKTU ('436000'→'436 000',
    '12.5'→'12.50'). Lieto image_alt EUR/m² aprēķins."""
    s = str(v or "").strip()
    if not s:
        return s
    neg = s.startswith("-")
    s = _trim_dec(s.lstrip("-"))
    intp, _, dec = s.partition(".")
    grouped = ""
    while len(intp) > 3:
        grouped = " " + intp[-3:] + grouped
        intp = intp[:-3]
    grouped = intp + grouped
    out = grouped + (f".{dec}" if dec else "")
    return ("-" + out) if neg else out


def _money_lv(v) -> Optional[str]:
    """LV cena teksta šablonam: tūkstošu atstarpe + decimāldaļa ar KOMATU
    ('436000'→'436 000', '30.23'→'30,23', '6'→'6'). Aģenta stila teksts."""
    s = _num(v)
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


def _floor(v) -> Optional[str]:
    """Tīrs stāva apzīmējums BEZ 'stāvs'/'st' (importē pdf_maker, publish_to_wp).
    '1. stāvs'→'1', '3st'→'3', '2+st'→'2+', None→None."""
    s = _clean(v)
    if not s or "none" in s.lower() or "unknown" in s.lower():
        return None
    s = re.sub(r"st[āa]v[su]*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\bst\b", "", s, flags=re.IGNORECASE)
    s = s.replace(".", "").strip()
    m = re.search(r"\d+\s*\+?", s)
    if m:
        return m.group(0).replace(" ", "")
    s = s.strip(" .,-")
    return s or None


def _is_sale(pt) -> bool:
    return str(pt or "").strip().lower() in _SALE_PT


def _b(t: str) -> str:
    return f"<strong>{t}</strong>"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _join_lv(items) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " un " + items[-1]


_BDESC_BAD = ("nav redzam", "ārpus", "arpus", "nav saskat", "nezin", "neredz",
              "bilde", "foto", "attēl", "attel")


def _clean_bdesc(s) -> Optional[str]:
    """Izņem AI meta-klauzulas no Building_description (piem. 'Ārpuse nav redzama;')."""
    s = _clean(s)
    if not s:
        return None
    parts = re.split(r"[;.]", s)
    good = [p.strip() for p in parts
            if p.strip() and not any(b in p.lower() for b in _BDESC_BAD)]
    if not good:
        return None
    return ". ".join(g[0].upper() + g[1:] for g in good)


_VALUE_OK = {"pēc skaitītāja", "pēc skaitītājiem", "iekļauts", "iekļauts cenā",
             "iekļauts nomas maksā", "atsevišķi", "bezmaksas"}


def _value_like(s) -> bool:
    """True, ja izmaksu lauks izskatās pēc reālas vērtības (cipars vai zināma
    īsa frāze), nevis AI teikums/junk ('fakts bez cenas')."""
    s = _clean(s)
    if not s:
        return False
    if re.search(r"\d", s):
        return True
    return s.strip().lower().rstrip(".") in _VALUE_OK


def _parse_wc(v) -> tuple[Optional[int], Optional[str]]:
    """cik_WC = brīvteksts → (skaits|None, 'own'|'shared'|None)."""
    s = _clean(v)
    if not s:
        return None, None
    m = re.search(r"\d+", s)
    n = int(m.group(0)) if m else None
    low = s.lower()
    loc = "own" if "telp" in low else ("shared" if "koplieto" in low else None)
    return n, loc


def _floor_n(v) -> tuple[Optional[int], bool]:
    s = _clean(v)
    if not s:
        return None, False
    low = s.lower()
    base = any(k in low for k in ("cokol", "pagrab", "suter"))
    m = re.search(r"\d+", s)
    return (int(m.group(0)) if m else None), base


# Ielas tipa tokens (bez punkta, lowercase) → LOKATĪVS pilnā forma.
# Ievada teikumam: "Tiek iznomātas telpas Stirnu IELĀ 25" (ne "iela"/"Stirnu").
# Sedz gan pilnos vārdus, gan LV adrešu saīsinājumus (pr.=prospekts, l.=līnija).
_SFX_LOC_TOKENS = {
    "iela": "ielā", "gatve": "gatvē", "bulvāris": "bulvārī", "bulvaris": "bulvārī",
    "bulv": "bulvārī", "prospekts": "prospektā", "pr": "prospektā",
    "šoseja": "šosejā", "soseja": "šosejā", "ceļš": "ceļā", "cels": "ceļā",
    "laukums": "laukumā", "lauk": "laukumā", "aleja": "alejā",
    "krastmala": "krastmalā", "līnija": "līnijā", "linija": "līnijā", "līn": "līnijā",
    "l": "līnijā", "tilts": "tiltā", "pasāža": "pasāžā", "pasaza": "pasāžā",
}


_SFX_NOM_TOKENS = {  # tas pats kā _SFX_LOC_TOKENS, bet NOMINATĪVS (cover etiķete)
    "iela": "iela", "gatve": "gatve", "bulvāris": "bulvāris", "bulvaris": "bulvāris",
    "bulv": "bulvāris", "prospekts": "prospekts", "pr": "prospekts",
    "šoseja": "šoseja", "soseja": "šoseja", "ceļš": "ceļš", "cels": "ceļš",
    "laukums": "laukums", "lauk": "laukums", "aleja": "aleja",
    "krastmala": "krastmala", "līnija": "līnija", "linija": "līnija", "līn": "līnija",
    "l": "līnija", "tilts": "tilts", "pasāža": "pasāža", "pasaza": "pasāža",
}


def _street_decline(s, table) -> str:
    """Adrese ar ielas sufiksu, izvēlētajā locījumā (table = _SFX_LOC/_NOM_TOKENS).
    Pilsētu (aiz komata) atmet. 'Stirnu 25' → '<Stirnu> <iela/ielā> 25'."""
    s = _clean(s)
    if not s:
        return ""
    s = s.split(",")[0].strip()
    tokens = s.split()
    if not tokens:
        return ""
    low = [t.lower().rstrip(".") for t in tokens]
    for i, lt in enumerate(low):
        if lt in table:
            tokens[i] = table[lt]
            return " ".join(tokens)
    dflt = table["iela"]  # 'iela' vai 'ielā' atkarībā no tabulas
    if len(tokens) >= 2 and any(c.isdigit() for c in tokens[-1]):
        return " ".join(tokens[:-1]) + f" {dflt} " + tokens[-1]
    return s + f" {dflt}"


def _street_nominative(s) -> str:
    """Adrese NOMINATĪVĀ ar sufiksu (cover etiķete): 'Stirnu 25'→'Stirnu iela 25',
    'Kurzemes pr. 3g'→'Kurzemes prospekts 3g'."""
    return _street_decline(s, _SFX_NOM_TOKENS)


def _street_locative(s) -> str:
    """Adrese ievada teikumam LOKATĪVĀ ('kur?').
      'Stirnu 25'            → 'Stirnu ielā 25'   (nav sufiksa → iespraud 'ielā')
      'Stirnu iela 25'       → 'Stirnu ielā 25'   (iela → ielā)
      'Brīvības gatve 411'   → 'Brīvības gatvē 411'
      'Kurzemes pr. 3g'      → 'Kurzemes prospektā 3g'
      'Čiekurkalna 1. l. 84' → 'Čiekurkalna 1. līnijā 84'
    Pilsētu (aiz komata) atmet — ievadā tikai iela+numurs."""
    return _street_decline(s, _SFX_LOC_TOKENS)


# ─── Galvenais: render_body ────────────────────────────────────────────────
def render_body(space_group: str, listing: dict, bp: Optional[dict] = None) -> str:
    """Jaunais pilnu-teikumu teksts → HTML (<p>/<strong>/<br>).

    listing = properties.listings rinda (telpas līmeņa lauki).
    bp = properties.building_profiles rinda (ēkas līmeņa: Building_description,
         building_name, has_* ...). None → tukšs (bp-bloki izlaisti)."""
    bp = bp or {}
    L = listing
    g = lambda k: _clean(L.get(k))
    gb = lambda k: _clean(bp.get(k))
    sg = (space_group or "").strip()
    veids = _VEIDS.get(sg, "komerctelpas")
    sale = _is_sale(L.get("price_type"))
    area = _num(L.get("area_m2"))
    blocks: list[tuple[str, object]] = []

    # Ēkas konteksts (vajadzīgs gan virsrakstam, gan ievadam)
    addr_nom = _street_nominative(g("street") or gb("full_address"))  # virsraksta adrese
    bdesc = _clean_bdesc(gb("Building_description") or g("Building_description"))
    btype = gb("building_type") or g("building_type")
    bname = gb("building_name")
    is_complex = _truthy(bp.get("is_business_complex"))
    is_tc = (btype or "").strip().lower() == "tirdzniecības centrs"

    # 1. VIRSRAKSTS — veids + lokācija + (nosaukums VAI adrese) + platība.
    # Adresi/nosaukumu liekam ŠEIT (ne ievada teikumā) — citādi teksts atkārtojas
    # ("Iznomā X telpas Centra rajonā... Tiek iznomātas X telpas Y ielā"). 2026-06-05.
    if sale:
        inv = g("Investiciju_strategija")
        head = "Pārdod " + veids + (f" ({inv})" if inv else "")
    else:
        head = "Iznomā " + veids
    dist = _location_phrase(g("district") or gb("district"), g("city") or gb("city"))
    if dist:
        head += " " + dist
    place = bname if ((is_complex or is_tc) and bname) else addr_nom
    if place:
        head += (f", {place}" if dist else f" {place}")
    if area:
        head += f" – {area} m²"
    blocks.append(("B", head))

    # 2. IEVADS — ēkas raksturs. BEZ "Iznomā {veids} {adrese}" atkārtojuma (jau virsrakstā).
    fy = _num(bp.get("bdg_year"))
    fcount = _num(bp.get("floors_count"))
    managed = _truthy(bp.get("has_managed"))
    intro: list[str] = []
    # 2a. Ēkas raksturojuma teikums
    if is_tc:
        intro.append(_TC_STANDARD)
    elif is_complex:
        intro.append(f"{bname} ir moderns un aktīvs biznesa komplekss."
                     if bname else "Telpas atrodas modernā un aktīvā biznesa kompleksā.")
    elif bdesc:
        intro.append(bdesc.strip().rstrip(".") + ".")
    # 2b. Ēkas fakti (stāvi/gads/apsaimniekošana). Ja ēku JAU apraksta Building_description
    # vai komplekss/t-c teikums — NEatkārtojam ēkas tipu (citādi "jaukta tipa ēka" 2×).
    if is_tc:
        floor_w = (_STAVU[int(float(fcount))].lower()
                   if fcount and int(float(fcount)) in _STAVU else "")
        if fy:
            intro.append(f"Centrs celts {fy}. gadā.")
        if managed:
            subj_tc = f"{floor_w.capitalize()} centru" if floor_w else "Centru"
            intro.append(f"{subj_tc} apsaimnieko profesionāla apsaimniekošanas kompānija.")
        elif floor_w:
            intro.append(f"Tas ir {floor_w} tirdzniecības centrs.")
    elif is_complex and bname:
        if fy:
            intro.append(f"Komplekss celts {fy}. gadā.")
        if managed:
            intro.append("Kompleksu apsaimnieko profesionāla apsaimniekošanas kompānija.")
    elif bdesc:
        # Building_description jau apraksta ēku → tikai gads + apsaimniekošana (BEZ tipa).
        if fy and managed:
            intro.append(f"Ēka celta {fy}. gadā, un to apsaimnieko profesionāla apsaimniekošanas kompānija.")
        elif fy:
            intro.append(f"Ēka celta {fy}. gadā.")
        elif managed:
            intro.append("Ēku apsaimnieko profesionāla apsaimniekošanas kompānija.")
    else:
        # Nav apraksta → ēkas tips ir vienīgais ēkas raksturojums.
        subj = bname or "Ēka"
        btype_phrase = (btype or "").strip().lower() or "biznesa ēka"
        eka_desc = ""
        if fcount and int(float(fcount)) in _STAVU:
            eka_desc = _STAVU[int(float(fcount))].lower() + " " + btype_phrase
        elif bname or btype:
            eka_desc = btype_phrase
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
    # vairākas ēkas kompleksā (mig 031)
    if _truthy(bp.get("has_multiple_buildings")):
        intro.append("Komplekss sastāv no vairākām ēkām dažādos stāvos.")
    # BREEAM ilgtspējas sertifikāts (mig 034) — ēkas fakts (Raimonds 2026-06-08)
    if _truthy(bp.get("has_breeam")):
        intro.append("Ēkai ir BREEAM ilgtspējas sertifikāts.")
    # stāva teikums
    fn, base = _floor_n(L.get("floor"))
    own_entr = _truthy(L.get("Sava_ieeja_check"))
    has_lift = _truthy(bp.get("has_lift"))
    if base:
        intro.append("Telpas atrodas cokolstāvā, kas labi piemērots saimnieciskām un noliktavas vajadzībām.")
    elif fn == 1:
        if own_entr:
            intro.append("Telpas atrodas 1. stāvā, kas nodrošina ērtu klientu plūsmu un labu redzamību.")
        else:
            intro.append("Telpas atrodas ērti pieejamā 1. stāvā.")
    elif fn and fn >= 2:
        lift = ", ēkā ar liftu" if has_lift else ""
        # "tirdzniecības vidē" tikai t/c telpai t/c ēkā; birojam u.c. t/c ēkā → "darba vidē".
        vide = ("reprezentablā tirdzniecības vidē"
                if (is_tc and sg == "Tirdzniecība")
                else "klusā un reprezentablā darba vidē")
        intro.append(f"Telpas atrodas {fn}. stāvā{lift}, {vide}.")
    blocks.append(("P", " ".join(intro)))

    # 3. TELPU PLĀNOJUMS UN TEHNISKAIS STĀVOKLIS
    tech: list[str] = []
    cond = g("Space_condition")
    is_grey = cond == "Nepabeigts"   # pelēkā apdare → fit-out vēl nav, telpu var pielāgot
    if cond and cond in _COND:
        tech.append(_COND[cond])
    rooms = _num(L.get("Cik_telpas"))
    logi = _LOGI.get(g("Logu_type") or "")
    ceil = _dec_lv(L.get("Griestu_augstums"))
    if rooms:
        n = int(float(rooms))
        if is_grey and n == 1:
            # 1 telpa pelēkā apdarē = atvērts plānojums, ko var pielāgot (Raimonds 2026-06-08)
            tech.append("Kopā ir 1 atvērtā plānojuma telpa (open space), kuru var pielāgot kā vēlaties.")
        elif n == 1:
            tech.append("Kopā ir 1 atsevišķa telpa.")
        else:
            tech.append(f"Kopā ir {n} atsevišķas telpas.")
    # Logi + griesti = telpu VISPĀRĪGA īpašība, NE piesaistīta telpu skaitam.
    # Iepriekš "8 telpas ar lieliem logiem" implicēja, ka visām 8 ir lielie logi,
    # kas datos nav apgalvots (Logu_type ir viens telpu-līm. lauks). Raimonds 2026-06-06.
    ext = []
    if logi:
        ext.append(logi)
    if ceil:
        ext.append(f"{ceil} m augstiem griestiem")
    if ext:
        tech.append("Telpas ir ar " + _join_lv(ext) + ".")
    # iekšā: virtuve, WC, balkons, izlietne. Pelēkā apdarē fit-out lietas (virtuve,
    # sanitārie mezgli, izlietne) VĒL nav ierīkotas → "iespēja aprīkot ..." (akuzatīvs),
    # nevis "Tajā ir aprīkota virtuve" (būtu nepatiesi). Balkons = strukturāls → paliek.
    inside = []     # esošie/strukturālie: "Tajā ir ..."
    fitout = []     # pelēkā apdarē iespējamie: "Tajā ir iespēja aprīkot ..." (akuzatīvs)
    if _truthy(L.get("Virtuve_check")):
        (fitout if is_grey else inside).append("virtuvi" if is_grey else "aprīkota virtuve")
    wc_n, wc_loc = _parse_wc(L.get("cik_WC"))
    shared_wc = None
    if wc_loc == "own" or (wc_n and wc_loc is None):
        if is_grey:
            fitout.append("sanitāro mezglu" if (wc_n or 1) == 1 else "sanitāros mezglus")
        elif (wc_n or 1) == 1:
            inside.append("savs sanitārais mezgls" if wc_loc == "own" else "sanitārais mezgls")
        else:
            inside.append(f"{wc_n} sanitārie mezgli")
    elif wc_loc == "shared":
        shared_wc = wc_n
    if _truthy(L.get("Balkons_check")):
        inside.append("balkons")
    if _truthy(L.get("Ir_izlietne_telpa_check")):
        (fitout if is_grey else inside).append("izlietni" if is_grey else "sava izlietne")
    pron = "Tajā" if (rooms and int(float(rooms)) == 1) else "Tajās"
    if fitout:
        tech.append(f"{pron} ir iespēja aprīkot " + _join_lv(fitout) + ".")
    if inside:
        tech.append(f"{pron} ir " + _join_lv(inside) + ".")
    if shared_wc is not None:
        if shared_wc and shared_wc > 1:
            tech.append(f"Pieejami {shared_wc} koplietošanas sanitārie mezgli.")
        else:
            tech.append("Pieejams koplietošanas sanitārais mezgls.")
    # aprīkojums / inženierija
    clauses = []
    gm = _GRIDAS.get(g("Gridas_materials") or "")
    if gm:
        fv = "ieklāts" if "segums" in gm else "ieklātas"
        clauses.append(f"{fv} {gm}")
    eng = []
    ap = _APKURE.get(g("Apkure") or "")
    if ap:
        eng.append(ap)
    if _truthy(L.get("Ventilacijas_sistema_check")):
        eng.append("ventilācijas sistēma")
    if eng:
        clauses.append("ierīkota " + _join_lv(eng))
    if clauses:
        s = _join_lv(clauses)
        tech.append(s[0].upper() + s[1:] + ".")
    # mēbeles
    mb = str(L.get("Mebeleta_telpa") or "").strip().lower()
    if mb in ("jā", "ja"):
        tech.append("Tās šobrīd ir mēbelētas.")
    elif mb in ("daļēji", "daleji"):
        tech.append("Tās šobrīd ir daļēji mēbelētas.")
    # ražošanas/noliktavas specifika
    if sg in ("Ražošana", "Noliktava", "Autoserviss"):
        kg = _num(L.get("Gridas_izturiba_kg_m2"))
        if kg:
            tech.append(f"Grīdu nestspēja ir {kg} kg/m².")
        heavy = []
        pv = _num(L.get("Pacelamie_varti_count"))
        if _truthy(L.get("Pacelamie_varti_check")):
            heavy.append(f"{pv} paceļamie vārti" if pv and pv != "0" else "paceļamie vārti")
        rp = _num(L.get("Rampa_logistikai_count"))
        if _truthy(L.get("Rampa_logistikai_check")):
            heavy.append(f"{rp} iekraušanas rampa" if rp and rp != "0" else "iekraušanas rampa")
        if _truthy(L.get("Treifelis_Pacelajs")):
            heavy.append("kravas pacēlājs (telferis)")
        if _truthy(L.get("Auto_pacelajs_check")):
            heavy.append("auto pacēlājs")
        if heavy:
            tech.append("Loģistikai: " + _join_lv(heavy) + ".")
    pot = g("Potential_space_group")
    if pot:
        pots = [p.strip() for p in pot.split(",") if p.strip()]
        pots = [p for p in pots if p != sg]  # neiekļauj pašu telpas tipu (lieki)
        if sg in _NO_OFFICE_GROUPS:           # specializētai telpai birojs nav reāls
            pots = [p for p in pots if p != "Birojs"]
        if pots:
            gen = _join_lv([_PIELIET.get(p, p.lower()) for p in pots])
            tech.append(f"Piemērotas arī {gen} vajadzībām.")
    if tech:
        blocks.append(("S", ("Telpu plānojums un tehniskais stāvoklis:", " ".join(tech))))

    # 4. PRIEKŠROCĪBAS (ēkas līmenis; slieksnis: ≥1 īsta iespēja)
    bld = []
    real_amen = 0
    for fld in _PRIEK_ORDER:
        # 1. stāva / cokola telpām pasažieru lifts nav priekšrocība
        if fld == "has_lift" and (fn == 1 or base):
            continue
        if _truthy(bp.get(fld)):
            bld.append(_PRIEK[fld])
            real_amen += 1
    if _truthy(bp.get("has_canteen")):
        nm = _clean(bp.get("ednica_nosaukums"))
        bld.append(f'ēdnīca "{nm}" ērtām pusdienām uz vietas' if nm
                   else "ēdnīca ērtām pusdienām uz vietas")
        real_amen += 1
    if _truthy(L.get("Apsargajama_teritorija_check")) or _truthy(L.get("Nozogota_teritorija_check")) or _truthy(bp.get("has_fenced")):
        bld.append("apsargāta teritorija")
        real_amen += 1
    if _truthy(L.get("Vides_pieejamiba_check")) or _truthy(bp.get("has_accessibility")):
        bld.append("vides pieejamība")
        real_amen += 1
    park = _PARK.get(g("Parkings") or "")
    if park:
        bld.append((park + " darbiniekiem un klientiem") if "autostāvvieta" in park else park)
    if real_amen >= 1 and bld:
        blocks.append(("S", ("Priekšrocības:", "Ēkā ir " + _join_lv(bld) + ".")))

    # 5. NOSACĪJUMI
    ppm2 = g("price_per_m2")
    price = g("price")
    av = _num(L.get("area_m2"))
    if not ppm2 and price and av:
        try:
            ppm2 = str(round(float(price) / float(av), 2))
        except (ValueError, ZeroDivisionError):
            ppm2 = None
    cost = []
    if sale:
        if price:
            s = f"Cena ir {_money_lv(price)} EUR"
            if ppm2:
                s += f" ({_money_lv(ppm2)} EUR/m²)"
            cost.append(s + ".")
    else:
        if ppm2:
            s = f"Nomas maksa ir {_money_lv(ppm2)} EUR/m² mēnesī"
            if price:
                s += f" (kopā {_money_lv(price)} EUR)"
            cost.append(s + ".")
        elif price:
            cost.append(f"Nomas maksa ir {_money_lv(price)} EUR mēnesī.")
    # Izmaksu lauki: TIKAI ja reāla vērtība (cipars / zināma frāze).
    extra = []
    for label, val in [("apsaimniekošana", g("Apsaimniekosanas_maksa")),
                       ("NĪN", g("NIN")),
                       ("komunālie maksājumi", g("Komunalie"))]:
        if _value_like(val):
            extra.append(f"{label} {val}")
    if extra:
        cost.append("Papildus " + _join_lv(extra) + ".")
    # Citi maksājumi (brīvs teksts, anketā komatatdalīts) — sava rinda, katrs pēdiņās.
    # Raimonds 2026-06-07: "Citi maksājumi kā: „xxx 50 EUR/mēnesī", „yyy 49 EUR/mēnesī"."
    papildu = g("Papildu_maksas")
    if _value_like(papildu):
        items = [p.strip() for p in papildu.split(",") if p.strip()]
        if items:
            cost.append("Citi maksājumi kā: "
                        + ", ".join(f"„{it}”" for it in items) + ".")
    cost.append("Visām cenām pieskaitāms PVN.")
    # Katrs nosacījumu teikums savā rindā (Raimonds 2026-06-07) — <br>, ne atstarpe.
    blocks.append(("S", ("Pārdošanas nosacījumi:" if sale else "Nomas nosacījumi:", "<br>".join(cost))))

    # 6. NOSLĒGUMS
    blocks.append(("P", "Sazinieties ar mums, lai uzzinātu vairāk vai vienotos par telpu apskati. 🏢"))

    # ── HTML ──
    html: list[str] = []
    for typ, val in blocks:
        if typ == "B":
            html.append(f"<p>{_b(val)}</p>")
        elif typ == "P":
            if val:
                html.append(f"<p>{val}</p>")
        elif typ == "S":
            head, body = val  # type: ignore
            html.append(f"<p>{_b(head)}<br>{body}</p>")
    return "".join(html)


# ─── SEO / excerpt / alt (nemainīti — lieto tdata ar district/city) ─────────
def render_excerpt(space_group: str, raw: dict) -> str:
    """Īss konspekts = Yoast meta description (auto no excerpt). ≤100 zīmes."""
    g = lambda k: _clean(raw.get(k))
    veids = _VEIDS.get(space_group, "komerctelpas")
    sale = _is_sale(raw.get("price_type"))
    district = _cap(g("district") or "")
    city = _cap(g("city") or "")
    a = _num(raw.get("area_m2"))
    bc = g("building_class")
    sak = "Pārdod" if sale else "Nomā"
    txt = f"{sak} {veids}"
    if a:
        txt += f", {a} m²"
    loc = district or city
    if loc:
        txt += f", {loc}"
    if bc:
        txt += f", {bc} klase"
    txt += "."
    return (txt[:97].rstrip(" ,") + "…") if len(txt) > 100 else txt


def _status_word(raw: dict) -> str:
    return "pārdošana" if _is_sale(raw.get("price_type")) else "noma"


def seo_focus_keyphrase(space_group: str, raw: dict) -> str:
    """Yoast focus keyphrase = (telpu veids) (status) (rajons)."""
    g = lambda k: _clean(raw.get(k))
    veids = _VEIDS.get(space_group, "komerctelpas")
    loc = g("district") or g("city") or ""
    return f"{veids} {_status_word(raw)} {loc}".strip().lower()


def seo_title(space_group: str, raw: dict, address: str) -> str:
    """Yoast SEO title = TIKAI iela + tips + platība. BEZ pilsētas/rajona
    (Rīga/Centrs) un BEZ "RG Commerce" (Yoast sitename pats pieliktu).
    Raimonds 2026-06-05. `address` jau nāk bez pilsētas (_title nogriež)."""
    veids = _VEIDS.get(space_group, "komerctelpas")
    a = _num(raw.get("area_m2"))
    parts = []
    if address and address.strip():
        parts.append(address.strip())
    parts.append(_cap(veids))
    if a:
        parts.append(f"{a} m²")
    return ", ".join(parts)


def meta_description(body_html: str, limit: int = 155) -> str:
    """Yoast meta description = sludinājuma apraksta KONSPEKTS (ievada prozas
    teikums(i)), max ~limit zīmes (Google rāda ~155). NE keyword-līnija.
    Ņem ievada rindkopu no jau-renderētā body HTML (1. <p> bez <strong> =
    virsraksts/sekciju heading izlaisti) un nogriež pie teikuma/vārda robežas.
    (Raimonds 2026-06-05)"""
    if not body_html:
        return ""
    text = ""
    for p in re.findall(r"<p>(.*?)</p>", body_html, flags=re.S):
        if p.lstrip().startswith("<strong>"):   # virsraksts vai sekcijas heading
            continue
        cand = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", p)).strip()
        if cand and not cand.startswith("Sazinieties"):  # izlaiž noslēguma CTA
            text = cand
            break
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Teikuma beigas = .?! aiz NE-cipara (LV kārtas skaitļi "3. stāvā" / "2008. gadā"
    # satur ". " bet NAV teikuma beigas — citādi apraksts nogriežas "...atrodas 3.").
    ends = [m.start() for m in re.finditer(r"(?<=\D)[.!?](?=\s)", cut)]
    dot = max(ends) if ends else -1
    if dot >= int(limit * 0.5):
        return cut[:dot + 1].strip()
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip(" ,;:–-") + "…"


def image_alt(space_group: str, raw: dict) -> str:
    """Bildes ALT = (veids) (rajons) (platība) (m²cena)."""
    g = lambda k: _clean(raw.get(k))
    veids = _VEIDS.get(space_group, "komerctelpas")
    loc = _cap(g("district") or g("city") or "")
    a = _num(raw.get("area_m2"))
    price = _num(raw.get("price"))
    bits = [veids]
    if loc:
        bits.append(loc)
    if a:
        bits.append(f"{a} m²")
    if price and a:
        try:
            bits.append(f"{_money(round(float(price)/float(a), 2))} EUR/m²")
        except (ValueError, ZeroDivisionError):
            pass
    return " ".join(bits)


if __name__ == "__main__":
    import os
    from pathlib import Path
    import psycopg
    from psycopg.rows import dict_row
    from dotenv import load_dotenv
    load_dotenv(Path("..").resolve() / "sslv-ai-runner-railway" / "crm" / ".env")
    LID = int(sys.argv[1]) if len(sys.argv) > 1 else 759
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        L = conn.execute("SELECT * FROM properties.listings WHERE id=%s", (LID,)).fetchone()
        bp = {}
        if L.get("building_profile_id"):
            bp = conn.execute("SELECT * FROM properties.building_profiles WHERE id=%s",
                              (L["building_profile_id"],)).fetchone() or {}
    print(render_body(L.get("Space_group", ""), L, bp))
