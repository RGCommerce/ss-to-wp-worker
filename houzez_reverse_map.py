"""DB -> Houzez taksonomiju term mapping (Etaps 3.2) — reverse no houzez_type_map.

Forward (houzez_type_map.py): Houzez term -> musu Space_group (WP import laika).
Sis modulis: musu DB vertibas -> Houzez term NOSAUKUMI (WP publish laika).
Term ID iegusana + auto-create notiek caur wp_publisher.WPPublisher.ensure_term.

AUTORITATIVS (invertets no houzez_type_map.HOUZEZ_TO_SPACE_GROUP):
  Space_group -> property_type

PRECIZEJAMS ar Raimondu (Etaps 2/3 — DB distinct values + Houzez UI audit):
  - price_type -> property_status term nosaukumi (sak. pienemums zemak)
  - building_class -> property_label ("A/B/C-Klase")
  property_state taksonomija = NEAIZTIKT (UI haoss — README 748, memory)
"""
from __future__ import annotations

import sys
from typing import Optional

from houzez_type_map import HOUZEZ_TO_SPACE_GROUP

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _invert_type_map() -> dict[str, str]:
    """Space_group -> property_type nosaukums (pirmais Houzez tips, kas to satur).

    HOUZEZ_TO_SPACE_GROUP ir 1 Houzez tips -> N Space_group. Invertejot,
    katrai Space_group vertibai pieskiram pirmo (autoritativako) Houzez tipu.
    """
    out: dict[str, str] = {}
    for houzez_type, space_groups in HOUZEZ_TO_SPACE_GROUP.items():
        for sg in space_groups:
            out.setdefault(sg, houzez_type)
    return out


SPACE_GROUP_TO_HOUZEZ_TYPE: dict[str, str] = _invert_type_map()

# price_type (DB) -> Houzez property_status term. _scratch_wp_publish_test.py
# lietoja "Noma". Pienemums (PRECIZET ar DB distinct + Houzez UI):
PRICE_TYPE_TO_STATUS: dict[str, str] = {
    # Reālās DB vērtības (verificēts 2026-05-17): monthly/mēneša/parastā/regular
    "monthly": "Nomā",
    "mēneša":  "Nomā",
    "mēnesī":  "Nomā",
    "rent":    "Nomā",
    "lease":   "Nomā",
    "noma":    "Nomā",
    "regular": "Pārdod",
    "parastā": "Pārdod",
    "parasta": "Pārdod",
    "sell":    "Pārdod",
    "buy":     "Pārdod",
    "sale":    "Pārdod",
}

# building_class (DB) -> Houzez property_label term (no SKIP_HOUZEZ_LABELS redzam
# autoritativos nosaukumus "A-Klase"/"B-Klase"/"C-Klase").
BUILDING_CLASS_TO_LABEL: dict[str, str] = {
    "A": "A-Klase",
    "B": "B-Klase",
    "C": "C-Klase",
    "A-Klase": "A-Klase",
    "B-Klase": "B-Klase",
    "C-Klase": "C-Klase",
}


def houzez_type_name(space_group: Optional[str]) -> Optional[str]:
    """Musu Space_group -> Houzez property_type term nosaukums (vai None)."""
    if not space_group:
        return None
    return SPACE_GROUP_TO_HOUZEZ_TYPE.get(space_group.strip())


def houzez_status_name(price_type: Optional[str]) -> Optional[str]:
    """DB price_type -> Houzez property_status term nosaukums (vai None).

    PIENEMUMS — precizet ar Raimondu pirms produkcijas publish.
    """
    if not price_type:
        return None
    return PRICE_TYPE_TO_STATUS.get(str(price_type).strip().lower())


def houzez_label_name(building_class: Optional[str]) -> Optional[str]:
    """DB building_class -> Houzez property_label term nosaukums (vai None)."""
    if not building_class:
        return None
    return BUILDING_CLASS_TO_LABEL.get(str(building_class).strip())


# DB feature lauks → (present-condition, Houzez property_feature term nosaukums).
# Lielākā daļa *_check: 'checked'/'not checked'/'unknown' → ir ja 'checked'.
FEATURE_CHECK_FIELDS: dict[str, str] = {
    "Virtuve_check":                "Aprīkota virtuve",
    "Balkons_check":                "Balkons",
    "Apsargajama_teritorija_check": "Apsargāta teritorija",
    "Ventilacijas_sistema_check":   "Ventilācijas sistēma",
    "Rampa_logistikai_check":       "Rampa loģistikai",
    "Pacelamie_varti_check":        "Paceļamie vārti",
    "Auto_pacelajs_check":          "Auto pacēlājs",
    "Sava_ieeja_check":             "Sava ieeja",
    "Ir_izlietne_telpa_check":      "Izlietne telpā",
    "Sava_eka_check":               "Visa ēka",
    "Nozogota_teritorija_check":    "Nožogota teritorija",
    "Has_conference_room":          "Konferenču telpa",
    "Treifelis_Pacelajs":           "Treiferis / Pacēlājs",
    "Vides_pieejamiba_check":       "Vides pieejamība",
    "Mazgajamas_sienas_check":      "Mazgājamas sienas",
}


# Jaunizveidoto property_feature term-u ikona (Raimonds 2026-05-21):
# RGC logo SVG. attachment ID 7647 = istais-logo.svg — tas pats, ko Raimonds
# manuāli uzlika ESOŠAJIEM features. Plugin to iestata TIKAI pie CREATE;
# esošie termi paliek neskarti.
FEATURE_ICON_TYPE = "custom"
FEATURE_ICON_IMAGE_ID = 7647


def resolve_feature_terms(publisher, raw: dict) -> list[int]:
    """DB feature lauki → Houzez property_feature term_id saraksts (ensure_term).
    Jaunizveidotiem term-iem automātiski tiek uzlikta RGC logo ikona."""
    ids: list[int] = []

    def _ensure(name: str):
        term = publisher.ensure_term(
            "property_feature", name,
            icon_type=FEATURE_ICON_TYPE,
            icon_image_id=FEATURE_ICON_IMAGE_ID,
        )
        tid = term.get("term_id")
        if tid:
            ids.append(int(tid))

    for field, term_name in FEATURE_CHECK_FIELDS.items():
        if str(raw.get(field) or "").strip().lower() == "checked":
            _ensure(term_name)

    # "Dalāma telpa" NEKAD netiek auto-pievienota kā WP īpašība (Raimonds 2026-05-28):
    # AI joprojām nosaka Dalama_telpa DB laukā, bet sludinājumā to kā funkciju neliek.

    mb = str(raw.get("Mebeleta_telpa") or "").strip().lower()
    if mb in ("jā", "ja", "daļēji", "daleji"):
        # "Mēbelēts" — kanoniskā vērtība (esošajā WP 28 īp.; "Mēbelēta" 2 īp. tika
        # nepareizi izveidots kā dublikāts). Labots 2026-05-25.
        _ensure("Mēbelēts")

    if str(raw.get("Logu_type") or "").strip() == "Lielie Logi":
        _ensure("Lieli logi")

    return ids


# District → upes krasts (autoritatīvs Raimonda saraksts, memory
# reference_rgc_districts). Atslēga = district lowercase bez diakr. variācijas.
_KRASTS = {}
for _d in ["centrs", "vecrīga", "klusais centrs", "latgales rajons",
           "vecais maskavas rajons", "dārzciems", "pļavinieki", "purvciems",
           "ķengarags", "šķirotava", "dreiliņi", "pētersala", "brasa",
           "skanste", "grīziņkalns", "teika", "čiekurkalns", "vef",
           "sarkandaugava", "mežaparks", "jaunciems", "mežciems", "jugla",
           "berģi"]:
    _KRASTS[_d] = "Labais Krasts"
for _d in ["torņkalns", "torņakalns", "āgenskalns", "agenskalns",
           "ziepniekalns", "ziepniekkalns", "iļģuciems", "zolitūde",
           "šampēteris", "pleskodāle", "dzirciems", "imanta", "kleisti"]:
    _KRASTS[_d] = "Kreisais Krasts"


def krasts_name(district: Optional[str]) -> Optional[str]:
    """District → 'Labais Krasts'/'Kreisais Krasts' vai None (ārpus Rīgas)."""
    if not district:
        return None
    return _KRASTS.get(district.strip().lower())


# Space_group → Houzez property_label nosaukumi (reverse no
# houzez_type_map.HOUZEZ_LABEL_TO_SPACE_GROUP — biznesa label-i, ko Ieva liek).
SPACE_GROUP_TO_LABELS: dict[str, list[str]] = {
    "Birojs":         ["Ofisam"],
    "Tirdzniecība":   ["Veikalam"],
    "Noliktava":      ["Noliktavai"],
    "Ražošana":       ["Ražošanai"],
    "Medicīna":       ["Medicīnai"],
    "Studija":        ["Studijas telpas", "Piemērots Salonām"],
    "Autoserviss":    ["Servisam"],
    "Restorans/Cafe": ["Ēdināšanai"],
}


def labels_for(space_group: Optional[str],
               potential: Optional[list[str]] = None) -> list[str]:
    """Space_group (+ potential) → Houzez property_label nosaukumu saraksts."""
    out: list[str] = []
    seen: set[str] = set()
    groups = [space_group] + list(potential or [])
    for gr in groups:
        for lab in SPACE_GROUP_TO_LABELS.get((gr or "").strip(), []):
            if lab not in seen:
                seen.add(lab)
                out.append(lab)
    return out


def resolve_taxonomy_terms(
    publisher,
    *,
    space_group: Optional[str] = None,
    price_type: Optional[str] = None,
    building_class: Optional[str] = None,
    city: Optional[str] = None,
    district: Optional[str] = None,
    potential: Optional[list[str]] = None,
    features: Optional[list[str]] = None,
    country: str = "Latvija",
) -> dict[str, list[int]]:
    """DB vertibas -> {taxonomy: [term_id,...]} caur publisher.ensure_term.

    city/district/features padod ka tiesos nosaukumus (ensure_term auto-create,
    Etap 1.8). Atgriez tikai tas taksonomijas, kam ir vismaz 1 term.
    """
    result: dict[str, list[int]] = {}

    def _add(taxonomy: str, name: Optional[str], parent: Optional[str] = None):
        if not name:
            return
        term = publisher.ensure_term(taxonomy, name, parent_name=parent)
        tid = term.get("term_id")
        if tid:
            result.setdefault(taxonomy, []).append(int(tid))

    _add("property_type", houzez_type_name(space_group))
    _add("property_status", houzez_status_name(price_type))
    _add("property_country", country)            # Latvija — vienmēr
    _add("property_label", houzez_label_name(building_class))
    for lab in labels_for(space_group, potential):  # Veikalam/Salonām utt.
        _add("property_label", lab)
    _add("property_city", city)
    _add("property_area", district)
    _add("property_area", krasts_name(district))  # Kreisais/Labais Krasts
    for feat in features or []:
        _add("property_feature", feat)

    return result


if __name__ == "__main__":
    print("Space_group -> property_type (autoritativs, invertets):")
    for sg, ht in sorted(SPACE_GROUP_TO_HOUZEZ_TYPE.items()):
        print(f"  {sg:20s} -> {ht}")
    print("\nprice_type -> property_status (PIENEMUMS, precizet):")
    for k, v in PRICE_TYPE_TO_STATUS.items():
        print(f"  {k:10s} -> {v}")
