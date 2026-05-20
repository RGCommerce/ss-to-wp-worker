"""
Houzez property_type taksonomijas → mūsu Space_group enum vērtību mapping.

Lieto:
  - wordpress_backfill_potential.py — vienreizējs cleanup esošajiem WP listings
  - wordpress_inbox_to_listings.py — automātiski katram jaunam WP listings

Princips: Houzez tips ir BIZNESA labs (Ieva to pievieno apzināti). AI vizuālā
klasifikācija var maldīties (Bug 5). Tāpēc Houzez tips pievienots Potential_space_group
kā uzticams signāls — nepārraksta AI primary, tikai paplašina potenciālo izmantojumu sarakstu.
"""
from __future__ import annotations
import json
from typing import Optional, List


# Houzez property_type taksonomijas nosaukums → mūsu Space_group enum vērtības
HOUZEZ_TO_SPACE_GROUP: dict[str, List[str]] = {
    "Medicīnas telpas":      ["Medicīna"],
    "HoReCa telpas":         ["Restorans/Cafe", "PVD"],
    "Tirdzniecības telpas":  ["Tirdzniecība"],
    "Noliktavas / ražošana": ["Noliktava", "Ražošana"],
    "Biroji":                ["Birojs"],
    "Studijas telpas":       ["Studija"],
    "Autoserviss":           ["Autoserviss"],
}

# Tipi, ko NEMAPPOJAM (īpaši — paliek tikai listing_type laukā)
SKIP_HOUZEZ_TYPES = {
    "Investīciju objekti",  # → Investiciju_strategija lauks
    "Zemesgabali",          # → zemes telpa, atsevišķa kategorija
    "Gatavs bizness",       # → nav ekvivalenta Space_group
}

# Houzez property_label taksonomijas nosaukums → mūsu Space_group enum vērtības.
# Labels ir papildu signāli (Ieva apzināti pievieno). Pievienojam Potential_space_group
# kopā ar property_type derived.
HOUZEZ_LABEL_TO_SPACE_GROUP: dict[str, List[str]] = {
    "Ofisam":             ["Birojs"],
    "Noliktavai":         ["Noliktava"],
    "Medicīnai":          ["Medicīna"],
    "Ražošanai":          ["Ražošana"],
    "Veikalam":           ["Tirdzniecība"],
    "Piemērots Salonām":  ["Studija"],
    "Pārtikas razošanai": ["PVD", "Ražošana"],
    "Studijas telpas":    ["Studija"],
    "Servisam":           ["Autoserviss"],
    "Ēdināšanai":         ["Restorans/Cafe", "PVD"],
    "Stock Ofiss":        ["StockOfiss"],
    "Sporta zālei":       ["Sporta zāle"],
    "bāram":              ["Restorans/Cafe"],
}

# Labels, ko nemappojam uz Space_group (info citās jomās vai bez ekvivalenta)
SKIP_HOUZEZ_LABELS = {
    "A-Klase", "B-Klase", "C-Klase",      # → building_class, AI handles
    "Izglītībai",                          # → nav skaidra Space_group
    "Bērnu dārzam",                        # → nav skaidra Space_group
    "Cash flow", "Investīcija", "Flip",   # → Investiciju_strategija related, skip
}


def derive_potential_from_houzez_types(houzez_types: list[str]) -> list[str]:
    """No Houzez property_type list → mūsu Space_group enum vērtības (unique, preserved order)."""
    out: list[str] = []
    seen: set[str] = set()
    for ht in houzez_types or []:
        if ht in SKIP_HOUZEZ_TYPES:
            continue
        for sg in HOUZEZ_TO_SPACE_GROUP.get(ht, []):
            if sg not in seen:
                seen.add(sg)
                out.append(sg)
    return out


def derive_potential_from_houzez_labels(houzez_labels: list[str]) -> list[str]:
    """No Houzez property_label list → mūsu Space_group enum vērtības (unique, preserved order)."""
    out: list[str] = []
    seen: set[str] = set()
    for hl in houzez_labels or []:
        if hl in SKIP_HOUZEZ_LABELS:
            continue
        for sg in HOUZEZ_LABEL_TO_SPACE_GROUP.get(hl, []):
            if sg not in seen:
                seen.add(sg)
                out.append(sg)
    return out


def derive_potential_from_houzez(houzez_types: list[str], houzez_labels: list[str]) -> list[str]:
    """Apvienoti type + label → potential Space_group list (deduped)."""
    out: list[str] = []
    seen: set[str] = set()
    for sg in derive_potential_from_houzez_types(houzez_types):
        if sg not in seen:
            seen.add(sg)
            out.append(sg)
    for sg in derive_potential_from_houzez_labels(houzez_labels):
        if sg not in seen:
            seen.add(sg)
            out.append(sg)
    return out


def merge_potential_space_groups(existing: Optional[str], new_list: list[str]) -> Optional[str]:
    """Apvieno existing Potential_space_group (komatu atdalīts string) ar jauniem.

    - Saglabā oriģinālo secību existing rindām.
    - Pievieno tikai tos jaunos, kuru nav.
    - NEPĀRRAKSTA esošos.
    - Atgriež comma-separated string vai None ja tukšs.
    """
    existing_list: list[str] = []
    if existing:
        existing_list = [s.strip() for s in str(existing).split(",") if s.strip()]
    seen = set(existing_list)
    out = list(existing_list)
    for sg in new_list:
        if sg not in seen:
            seen.add(sg)
            out.append(sg)
    return ", ".join(out) if out else None


def extract_houzez_property_types(raw_json_str: Optional[str]) -> list[str]:
    """No raw_json string (WP REST API response) izvelk visus property_type taxonomijas nosaukumus."""
    return _extract_taxonomy_names(raw_json_str, "property_type")


def extract_houzez_property_labels(raw_json_str: Optional[str]) -> list[str]:
    """No raw_json string izvelk visus property_label taxonomijas nosaukumus."""
    return _extract_taxonomy_names(raw_json_str, "property_label")


def _extract_taxonomy_names(raw_json_str: Optional[str], taxonomy: str) -> list[str]:
    """Helper — izvelk visus terms nosaukumus konkrētai taksonomijai no raw_json."""
    if not raw_json_str:
        return []
    try:
        data = json.loads(raw_json_str)
    except Exception:
        return []
    embedded = data.get("_embedded", {})
    names: list[str] = []
    for tax_group in embedded.get("wp:term", []):
        for term in tax_group:
            if term.get("taxonomy") == taxonomy:
                name = term.get("name")
                if name:
                    names.append(name)
    return names
