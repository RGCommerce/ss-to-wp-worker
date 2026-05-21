"""Slot-based teksta šabloni WP property publish (Etaps 3.4, NEAI, $0).

3-daļu struktūra (Raimonda paraugs 2026-05-17):
  1. IEVADS (bold) — kas tas ir: Pieejama {telpu_veids} {platība} m² {rajons} rajonā
  2. APRAKSTS — par pašu TELPU + par ĒKU (visi pieejamie AI columni)
  3. CENAS (bold) — Cena EUR/m², Apsaimniekošana, Komunālie, Reklāma
  + Noslēgums ar commercial emoji.

Nosacījuma teikumi: ja DB vērtība trūkst/'unknown' → teikums/slots IZLAISTS
(NEdrukā 'unknown' lietotājam). Title vienmēr = adrese (publish_to_wp).

Lietošana:
    from wp_templates import render_body, render_excerpt, SUPPORTED_GROUPS
"""
from __future__ import annotations

import re
import sys
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_MISSING = {"", "unknown", "nezināms", "nezinams", "nav", "none", "null",
            "n/a", "-", "~", "unkown"}
_SALE_PT = {"regular", "parastā", "parasta", "sale", "sell", "buy"}

# Space_group → lietojams lietvārds ("Pieejama {X} ...")
_VEIDS = {
    "Birojs":          "biroja telpas",
    "Tirdzniecība":    "tirdzniecības telpas",
    "Noliktava":       "noliktavas telpas",
    "Ražošana":        "ražošanas telpas",
    "Medicīna":        "medicīnas telpas",
    "Restorans/Cafe":  "ēdināšanas telpas",
    "Studija":         "studijas telpas",
    "Autoserviss":     "autoservisa telpas",
}
# Kam telpas piemērotas (pielietojums)
_PIELIETOJUMS = {
    "Birojs":          "biroja",
    "Tirdzniecība":    "tirdzniecības",
    "Noliktava":       "noliktavas un loģistikas",
    "Ražošana":        "ražošanas",
    "Medicīna":        "medicīnas prakses",
    "Restorans/Cafe":  "ēdināšanas (kafejnīca, restorāns)",
    "Studija":         "studijas vai radoša darba",
    "Autoserviss":     "autoservisa",
}
SUPPORTED_GROUPS = sorted(_VEIDS.keys())


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in _MISSING:
        return None
    s = s.lstrip("~").strip()
    return s if s and s.lower() not in _MISSING else None


def _floor(v) -> Optional[str]:
    """Atgriež tīru stāva apzīmējumu BEZ 'stāvs'/'st' (lai 'Telpas atrodas
    {X}. stāvā' nedubultojas). '1. stāvs'→'1', '3st'→'3', '2+st'→'2+',
    'Nonest'/None→None."""
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


def _num(v) -> Optional[str]:
    s = _clean(v)
    if not s:
        return None
    m = re.search(r"\d+[.,]?\d*", s)
    return m.group(0).replace(",", ".") if m else None


def _money(v) -> str:
    """Skaitlis ar tūkstošu atdalītāju (LV konvencija ar atstarpi):
    '436000' → '436 000', '785' → '785', '3.49' → '3.49'."""
    s = str(v or "").strip()
    if not s:
        return s
    neg = s.startswith("-")
    s = s.lstrip("-")
    intp, _, dec = s.partition(".")
    grouped = ""
    while len(intp) > 3:
        grouped = " " + intp[-3:] + grouped
        intp = intp[:-3]
    grouped = intp + grouped
    out = grouped + (f".{dec}" if dec else "")
    return ("-" + out) if neg else out


def _checked(v) -> bool:
    return str(v or "").strip().lower() == "checked"


def _yes(v) -> bool:
    return str(v or "").strip().lower() in ("jā", "ja")


def _is_sale(pt) -> bool:
    return str(pt or "").strip().lower() in _SALE_PT


def _b(t: str) -> str:
    """Bold rinda (Raimonda <bold>)."""
    return f"<strong>{t}</strong>"


def _sentences(text: str) -> list[str]:
    """Sadala prozu atsevišķos teikumos — katrs teikums = sava rinda
    (Raimonda paraugs: ja runa par logiem, teikums par logiem ir sava rinda).
    Robežas: '.', '!', ';'. Iekšējo punktuāciju noņem (rinda to dabū vēlāk)."""
    text = (text or "").strip()
    if not text:
        return []
    out = []
    for s in re.split(r"(?<=[.!;])\s+", text):
        s = s.strip().rstrip(".;!").strip()
        if s:
            out.append(s)
    return out


def _section(head: str, facts: list[str]) -> Optional[str]:
    """Sadaļa: bold virsraksts + katrs fakts savā rindā (<br>).
    Visas rindas beidzas ar ';', sadaļas pēdējā ar '.' (Raimonda paraugs)."""
    facts = [f.strip().rstrip(" .;") for f in facts if f and f.strip(" .;")]
    if not facts:
        return None
    lines = [f + ("." if i == len(facts) - 1 else ";")
             for i, f in enumerate(facts)]
    return _b(head) + "<br>" + "<br>".join(lines)


# Space_condition → īpašības vārds lokatīvā ("... labā stāvoklī")
_COND_LOC = {
    "labs": "labā", "jauns": "jaunā", "lielisks": "lieliskā",
    "vidējs": "vidējā", "renovēts": "renovētā", "kapitāli renovēts": "renovētā",
    "nepieciešams remonts": "remontējamā", "slikts": "sliktā",
}


def _plur(n_str: str, sing: str, plur: str) -> str:
    """Latviešu skaitļa-lietvārda saskaņa (vienkāršots: ...1 → vsk., cits → dsk.)."""
    try:
        n = int(re.sub(r"\D", "", n_str) or 0)
    except ValueError:
        n = 0
    word = sing if (n % 10 == 1 and n % 100 != 11) else plur
    return f"{n_str} {word}"


# Stāvu skaits → īpašības vārds ēkai ("Divstāvu ēka")
_STAVU_VARDS = {
    1: "Vienstāva", 2: "Divstāvu", 3: "Trīsstāvu", 4: "Četrstāvu",
    5: "Piecstāvu", 6: "Sešstāvu", 7: "Septiņstāvu", 8: "Astoņstāvu",
    9: "Deviņstāvu",
}


def _floor_sentence(floor: Optional[str], own_building: bool) -> str:
    """Ievada stāva/ēkas teikums (Raimonds 2026-05-22).
    - Pašu ēka (Sava_eka_check) → 'Divstāvu ēka.' — floor = stāvu SKAITS,
      nevis kurā stāvā telpa atrodas (citādi maldina, ka telpa ir 2. stāvā).
    - Citādi → 'Telpas atrodas X. stāvā.' (telpa lielākas ēkas stāvā)."""
    if own_building:
        n = None
        m = re.search(r"\d+", str(floor or ""))
        if m:
            n = int(m.group(0))
        if n and n in _STAVU_VARDS:
            return f"{_STAVU_VARDS[n]} ēka."
        if n and n > 9:
            return f"Ēka ar {n} stāviem."
        return "Atsevišķa ēka."  # pašu ēka, bet stāvu skaits nezināms
    if floor:
        return f"Telpas atrodas {floor}. stāvā."
    return ""


def render_body(space_group: str, raw: dict) -> str:
    g = lambda k: _clean(raw.get(k))
    veids = _VEIDS.get(space_group, "komerctelpas")
    sale = _is_sale(raw.get("price_type"))

    area = _num(raw.get("area_m2"))
    district = _cap(g("district") or "") or None
    city = _cap(g("city") or "") or None
    floor = _floor(raw.get("floor"))

    blocks: list[str] = []

    # ---- 1. IEVADS (bold) — kas tas ir -----------------------------------
    loc = f"{district} rajonā" if district else (city or "")
    sakums = "Pārdošanā" if sale else "Pieejamas nomai"
    intro = f"{sakums} {veids}"
    if area:
        intro += f" ar kopējo platību {area} m²"
    if loc:
        intro += f" {loc}"
    ievads = _b(intro.strip().rstrip(".") + ".")
    # Pašu ēka → "Divstāvu ēka."; citādi → "Telpas atrodas X. stāvā."
    own_building = _checked(raw.get("Sava_eka_check"))
    floor_line = _floor_sentence(floor, own_building)
    if floor_line:
        ievads += f"<br>{floor_line}"
    blocks.append(ievads)

    # ---- 2. APRAKSTS — TELPA (bold virsraksts) ---------------------------
    # Katrs fakts/teikums = sava rinda (Raimonda paraugs 2026-05-17).
    telpa: list[str] = []
    cond = g("Space_condition")
    logu = g("Logu_type")
    ceiling = _num(raw.get("Griestu_augstums"))
    # Telpas stāvoklis — VIENS NO PIRMAJIEM tekstiem (Raimonds 2026-05-21)
    if cond:
        loc_adj = _COND_LOC.get(cond.lower())
        telpa.append(_cap(f"telpas ir {loc_adj} stāvoklī") if loc_adj
                     else _cap(f"telpu stāvoklis — {cond.lower()}"))
    # Agent_comment = AI jau uzrakstītā proza — sadalām pa teikumiem
    ac = g("Agent_comment")
    if ac and len(ac) > 12:
        telpa.extend(_cap(s) for s in _sentences(ac))
    if logu:
        telpa.append(_cap(logu.lower()))
    if ceiling:
        telpa.append(f"Griestu augstums vidēji {ceiling} m")

    rooms = _num(raw.get("Cik_telpas"))
    wc = _num(raw.get("cik_WC"))
    rb = []
    if rooms:
        rb.append(_plur(rooms, "telpa", "telpas"))
    if wc:
        rb.append(_plur(wc, "sanmezgls", "sanmezgli"))
    if rb:
        telpa.append("Plānojumā " + " un ".join(rb) + ".")

    floor_mat = g("Gridas_materials")
    floor_kg = _num(raw.get("Gridas_izturiba_kg_m2"))
    if floor_mat:
        mat = re.sub(r"\s*gr[īi]d[au]?s?\s*$", "", floor_mat.strip(),
                     flags=re.IGNORECASE).strip().lower() or floor_mat.lower()
        telpa.append(f"Grīdas ir {mat}")
    if floor_kg:
        telpa.append(f"Grīdu slodze ir vidēji līdz {floor_kg} kg/m²")

    # Dalama_telpa — Raimonds 2026-05-18: IGNORĒT (nerakstam 'dalāma')
    extras = []
    mb = str(raw.get("Mebeleta_telpa") or "").strip().lower()
    if mb in ("jā", "ja"):
        extras.append("mēbelēta")
    elif mb in ("daļēji", "daleji"):
        extras.append("daļēji mēbelēta")
    if _checked(raw.get("Sava_ieeja_check")):
        extras.append("ar atsevišķu ieeju")
    if extras:
        telpa.append("Telpa ir " + ", ".join(extras) + ".")
    iekartas = []
    if _checked(raw.get("Ir_izlietne_telpa_check")):
        iekartas.append("izlietne")
    if _checked(raw.get("Virtuve_check")):
        iekartas.append("aprīkota virtuve")
    if _checked(raw.get("Balkons_check")):
        iekartas.append("balkons")
    if iekartas:
        telpa.append("Telpā ir " + ", ".join(iekartas) + ".")

    pot_raw = g("Potential_space_group") or ""
    others = [p.strip() for p in pot_raw.split(",")
              if p.strip() and p.strip().lower() != (space_group or "").lower()]
    piel = _PIELIETOJUMS.get(space_group)
    if piel:
        s = f"Telpas piemērotas {piel} vajadzībām"
        if others:
            s += f", kā arī {', '.join(o.lower() for o in others)} telpām"
        telpa.append(s + ".")

    sec = _section("Par telpām:", telpa)
    if sec:
        blocks.append(sec)

    # ---- 2b. APRAKSTS — ĒKA ----------------------------------------------
    eka: list[str] = []
    btype = g("building_type")
    bclass = g("building_class")
    eb = []
    if btype:
        bt = re.sub(r"\s*ēka\s*$", "", btype.strip(),
                    flags=re.IGNORECASE).strip().lower()
        eb.append(bt if bt.endswith("tipa") else f"{bt} tipa")
    if bclass:
        eb.append(f"{bclass} klases")
    if eb:
        eka.append(_cap("Ēka ir " + ", ".join(eb)))
    bdesc = g("Building_description")
    if bdesc and len(bdesc) > 8:
        eka.extend(_cap(s) for s in _sentences(bdesc))

    sys_bits = []
    apk = g("Apkure")
    if apk:
        sys_bits.append(f"{apk.lower()} apkure")
    if _checked(raw.get("Ventilacijas_sistema_check")):
        sys_bits.append("ventilācijas sistēma")
    pwr = _num(raw.get("electric_power_kw"))
    if pwr:
        sys_bits.append(f"elektrojauda {pwr} kW")
    if sys_bits:
        eka.append("Ēkā ir " + ", ".join(sys_bits) + ".")

    log_bits = []
    if _checked(raw.get("Rampa_logistikai_check")):
        log_bits.append("rampa loģistikai")
    if _checked(raw.get("Pacelamie_varti_check")):
        log_bits.append("paceļamie vārti")
    if _checked(raw.get("Auto_pacelajs_check")):
        log_bits.append("auto pacēlājs")
    if _checked(raw.get("Apsargajama_teritorija_check")):
        log_bits.append("apsargāta teritorija")
    if _checked(raw.get("Nozogota_teritorija_check")):
        log_bits.append("nožogota teritorija")
    if _checked(raw.get("Sava_eka_check")):
        log_bits.append("pieejama visa ēka")
    if log_bits:
        eka.append(_cap("Papildus: " + ", ".join(log_bits)) + ".")

    park = g("Parkings")
    if park:
        pl = park.lower()
        if "bezmaksas" in pl:
            eka.append("Ir pieejama bezmaksas autostāvvieta")
        elif "maksas" in pl:
            eka.append("Ir pieejama maksas autostāvvieta")
        else:
            eka.append("Ir pieejama autostāvvieta")

    # Zemes platība — tikai ja DB Zemes_gabals_m2 nav NULL (Raimonds 2026-05-21)
    land = _num(raw.get("Zemes_gabals_m2"))
    if land:
        eka.append(f"Zemes platība ir {_money(land)} m²")

    sec = _section("Par ēku:", eka)
    if sec:
        blocks.append(sec)

    # ---- 3. IZMAKSAS (bold virsraksts) — katra rinda atsevišķi -----------
    ppm2 = g("price_per_m2")
    price = g("price")
    area_v = _num(raw.get("area_m2"))
    cost_lines: list[str] = []
    if price:
        lbl = "Cena" if sale else "Noma mēnesī"
        cost_lines.append(f"{lbl}: {_money(price)} EUR.")
        if not ppm2 and area_v:
            try:
                ppm2 = str(round(float(price) / float(area_v), 2))
            except (ValueError, ZeroDivisionError):
                ppm2 = None
    if ppm2:
        cost_lines.append(f"Cena par m²: {_money(ppm2)} EUR/m².")
    mgmt = g("Apsaimniekosanas_maksa")
    if mgmt:
        cost_lines.append(f"Apsaimniekošana: {mgmt}.")
    util = g("Komunalie")
    if util:
        cost_lines.append(f"Komunālie: {util}.")
    extra_fee = g("Papildu_maksas")
    if extra_fee:
        cost_lines.append(f"Papildu maksas: {extra_fee}.")
    if cost_lines:
        cl = [c if c.rstrip().endswith(".") else c + "." for c in cost_lines]
        blocks.append(_b("Izmaksas:") + "<br>" + "<br>".join(cl))

    # ---- Noslēgums --------------------------------------------------------
    blocks.append("Sazinieties ar mums, lai uzzinātu vairāk vai vienotos "
                  "par apskati. 🏢")

    return "".join(f"<p>{b}</p>" for b in blocks if b)


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


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _status_word(raw: dict) -> str:
    return "pārdošana" if _is_sale(raw.get("price_type")) else "noma"


def seo_focus_keyphrase(space_group: str, raw: dict) -> str:
    """Yoast focus keyphrase = (telpu veids) (status) (rajons)."""
    g = lambda k: _clean(raw.get(k))
    veids = _VEIDS.get(space_group, "komerctelpas")
    loc = g("district") or g("city") or ""
    return f"{veids} {_status_word(raw)} {loc}".strip().lower()


def seo_title(space_group: str, raw: dict, address: str) -> str:
    """Yoast SEO title — adrese + veids + platība + lokācija."""
    g = lambda k: _clean(raw.get(k))
    veids = _VEIDS.get(space_group, "komerctelpas")
    a = _num(raw.get("area_m2"))
    loc = _cap(g("district") or g("city") or "")
    parts = [address, _cap(veids)]
    if a:
        parts.append(f"{a} m²")
    if loc:
        parts.append(loc)
    return ", ".join(parts) + " | RG Commerce"


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
            bits.append(f"{round(float(price)/float(a), 2)} EUR/m²")
        except (ValueError, ZeroDivisionError):
            pass
    return " ".join(bits)


if __name__ == "__main__":
    demo = {
        "Space_group": "Birojs", "city": "Rīga", "district": "Centrs",
        "street": "Dzirnavu 93", "area_m2": "135", "floor": "4st",
        "Cik_telpas": "~4", "cik_WC": "2", "Griestu_augstums": "~2.7",
        "price": "807", "price_per_m2": "6.5", "price_type": "monthly",
        "building_class": "B", "building_type": "Biroju ēka",
        "Building_description": "Renovēta biroju ēka ar liftu",
        "Parkings": "Ir vietas par maksu", "Apkure": "Centrālā",
        "Space_condition": "Labs", "Logu_type": "Lielie Logi",
        "Apsaimniekosanas_maksa": "1.50 EUR/m²", "Komunalie": "pēc skaitītājiem",
        "Ventilacijas_sistema_check": "checked", "Virtuve_check": "checked",
        "Sava_ieeja_check": "checked", "Mebeleta_telpa": "Daļēji",
    }
    print(render_body("Birojs", demo))
    print("\nEXCERPT:", render_excerpt("Birojs", demo))
    print("SEO kw:", seo_focus_keyword("Birojs", demo))
