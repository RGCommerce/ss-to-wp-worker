"""render_body regresijas + Teksta būvētāja (Agent_text_segments) testi.

DIVI mērķi:
  1. BYTE-IDENTICAL sargs: bez segmentiem render_body izvade nedrīkst mainīties
     (salīdzina pret _test_segments_baseline.json, ko ģenerē --dump).
  2. Segmentu loģika: splice aiz mērķa teikuma; orphan krīt daļas beigās; nekad nezūd.

Lietošana:
    python _test_segments.py --dump     # ģenerē baseline (pirms refaktoringa!)
    python _test_segments.py            # palaiž visus testus
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wp_templates import render_body, body_segments  # noqa: E402

HERE = Path(__file__).parent
BASELINE = HERE / "_test_segments_baseline.json"

# ── Fikstūras: (nosaukums, space_group, listing, bp) — sedz galvenos zarus ──
FIXTURES: list = [
    ("birojs_pilns", "Birojs", {
        "price_type": "rent", "area_m2": "120", "floor": "3",
        "Space_condition": "Labs", "Cik_telpas": "4", "Logu_type": "Lielie Logi",
        "Griestu_augstums": "3.2", "Virtuve_check": "1", "cik_WC": "2",
        "Gridas_materials": "Lamināts", "Apkure": "Centrālā",
        "Ventilacijas_sistema_check": "1", "Parkings": "Bezmaksas autostāvvieta",
        "price_per_m2": "12", "Apsaimniekosanas_maksa": "2.5",
    }, {
        "building_name": "Alfa Biznesa Centrs", "is_business_complex": True,
        "bdg_year": "2015", "floors_count": "5", "has_managed": True,
        "has_lift": True, "district": "Centra rajons", "city": "Rīga",
        "full_address": "Brīvības iela 100",
    }),
    ("tc_pardosana", "Tirdzniecība", {
        "price_type": "regular", "area_m2": "300", "floor": "1",
        "Space_condition": "Jauns", "Cik_telpas": "1", "price": "450000",
    }, {
        "building_type": "Tirdzniecības centrs", "building_name": "Mols",
        "floors_count": "3", "bdg_year": "2008", "has_managed": True,
        "district": "Zemgales priekšpilsēta", "city": "Rīga",
        "full_address": "Mūkusalas iela 71",
    }),
    ("noliktava_pelekais_1telpa", "Noliktava", {
        "price_type": "rent", "area_m2": "500", "floor": "1",
        "Space_condition": "Nepabeigts", "Cik_telpas": "1", "Dalama_telpa": "Nē",
        "Gridas_izturiba_kg_m2": "2000", "Pacelamie_varti_check": "1",
        "Pacelamie_varti_count": "2", "price_per_m2": "5",
    }, {
        "building_type": "Industriāla ēka", "floors_count": "1",
        "district": "Kurzemes rajons", "city": "Rīga",
        "full_address": "Daugavgrīvas iela 21",
    }),
    ("birojs_projekts", "Birojs", {
        "price_type": "rent", "area_m2": "80", "floor": "2",
        "Cik_telpas": "2", "is_project": "1", "project_completion": "2026. gada rudenī",
        "price_per_m2": "15",
    }, {
        "Building_description": "Moderna A klases biroju ēka ar stikla fasādi",
        "building_type": "Biroju ēka", "bdg_year": "2026", "floors_count": "7",
        "has_lift": True, "district": "Centra rajons", "city": "Rīga",
        "full_address": "Elizabetes iela 45",
    }),
    ("birojs_ar_extra", "Birojs", {
        "price_type": "rent", "area_m2": "60", "floor": "1",
        "Cik_telpas": "1", "Sava_ieeja_check": "1", "price_per_m2": "10",
        "Agent_text_extra": "Īpašnieks piedāvā pirmo mēnesi bez maksas.\n\nIespējama ilgtermiņa vienošanās.",
    }, {
        "building_type": "Jaukta tipa ēka", "district": "Latgales priekšpilsēta",
        "city": "Rīga", "full_address": "Maskavas iela 250",
    }),
]


def _all_baseline() -> dict:
    return {name: render_body(sg, dict(L), dict(bp))
            for (name, sg, L, bp) in FIXTURES}


def dump():
    BASELINE.write_text(json.dumps(_all_baseline(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"Baseline saglabāts: {BASELINE} ({len(FIXTURES)} fikstūras)")


def test_byte_identical():
    if not BASELINE.exists():
        print("! nav baseline — palaid --dump PIRMS refaktoringa"); return False
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    cur = _all_baseline()
    ok = True
    for name in base:
        if base[name] != cur.get(name):
            ok = False
            print(f"  ✗ MAINĪJIES: {name}")
            print(f"    veca: {base[name]}")
            print(f"    jauna: {cur.get(name)}")
    print("  ✓ byte-identical (bez segmentiem)" if ok else "  ✗ REGRESIJA!")
    return ok


def _office():
    """Neliela birojs-fikstūra ar zināmiem teikumiem visās sekcijās."""
    L = {
        "price_type": "rent", "area_m2": "120", "floor": "3",
        "Space_condition": "Labs", "Cik_telpas": "4", "price_per_m2": "12",
    }
    bp = {
        "building_name": "Alfa Biznesa Centrs", "is_business_complex": True,
        "bdg_year": "2015", "floors_count": "5", "has_managed": True,
        "has_lift": True, "district": "Centra rajons", "city": "Rīga",
        "full_address": "Brīvības iela 100",
    }
    return "Birojs", L, bp


def _check(name, cond):
    print(("  ✓ " if cond else "  ✗ ") + name)
    return cond


def test_body_segments():
    sg, L, bp = _office()
    segs = body_segments(sg, dict(L), dict(bp))
    sids = [s["section"] for s in segs]
    ok = True
    ok &= _check("body_segments atdod eka/telpa/priek/cena",
                 sids == ["eka", "telpa", "priek", "cena"])
    telpa = next(s for s in segs if s["section"] == "telpa")
    ok &= _check("telpa satur teikumu par 4 telpām",
                 any("4 atsevišķas telpas" in x for x in telpa["sentences"]))
    return ok


def test_splice_after_sentence():
    sg, L, bp = _office()
    anchor = "Kopā ir 4 atsevišķas telpas."
    L = dict(L, Agent_text_segments=json.dumps([{
        "section": "telpa", "anchor_text": anchor, "position": "after",
        "text": "Telpas ir savstarpēji savienotas.",
    }]))
    html = render_body(sg, L, dict(bp))
    ok = True
    ok &= _check("papildinājums TIEŠI aiz mērķa teikuma",
                 "atsevišķas telpas. Telpas ir savstarpēji savienotas." in html)
    return ok


def test_section_start_end():
    sg, L, bp = _office()
    L = dict(L, Agent_text_segments=json.dumps([
        {"section": "cena", "anchor_text": "", "position": "end",
         "text": "Cena apspriežama."},
        {"section": "eka", "anchor_text": "", "position": "start",
         "text": "IEVADS."},
    ]))
    html = render_body(sg, L, dict(bp))
    ok = True
    # cena beigas: aiz pēdējās nosacījumu rindas (PVN)
    ok &= _check("cena beigās (aiz PVN rindas)",
                 "pieskaitāms PVN.<br>Cena apspriežama." in html)
    # eka sākumā: pirms pirmā ievada teikuma
    ok &= _check("eka sākumā (pirms 1. teikuma)",
                 "<p>IEVADS. Alfa Biznesa Centrs ir moderns" in html)
    return ok


def test_detach_behavior():
    sg, L, bp = _office()
    # TEIKUMA līmenis: anchor uz teikumu, kura NAV → NErenderē (atsaistīts).
    L1 = dict(L, Agent_text_segments=json.dumps([{
        "section": "telpa", "anchor_text": "Šāda teikuma nav vispār",
        "position": "after", "text": "NEPARADAS.",
    }]))
    html1 = render_body(sg, L1, dict(bp))
    ok = True
    ok &= _check("teikuma līmenis, teikums prom → NErenderē (atsaistīts)",
                 "NEPARADAS." not in html1)
    # SEKCIJAS līmenis (anchor=""), sekcija pazudusi (priek bez ērtībām) →
    # renderē pirms cenas (neatkarīga info).
    bp2 = dict(bp); bp2["has_lift"] = False
    L2 = dict(L, Agent_text_segments=json.dumps([{
        "section": "priek", "anchor_text": "", "position": "end",
        "text": "SEKC-INFO.",
    }]))
    html2 = render_body(sg, L2, dict(bp2))
    ok &= _check("sekcijas līmenis, sekcija prom → renderē pirms cenas",
                 "SEKC-INFO." in html2 and
                 html2.index("SEKC-INFO.") < html2.index("Nomas nosacījumi:"))
    return ok


def test_removed_fact_drops_text():
    """Raimonda WC gadījums: pārraksti teikumu par WC, pēc tam noņem WC →
    ģenerētais WC teikums pazūd → pārrakstītais teksts NEparādās (lieta ir prom)."""
    sg, L, bp = _office()
    L = dict(L, cik_WC="1")  # ģenerē sanitārā mezgla teikumu
    secs = body_segments(sg, dict(L), dict(bp))
    telpa = next(s for s in secs if s["section"] == "telpa")
    wc_sent = next((x for x in telpa["sentences"] if "sanitār" in x.lower()), None)
    ok = _check("WC teikums ir (kad WC=1)", wc_sent is not None)
    if not wc_sent:
        return False
    seg = [{"section": "telpa", "anchor_text": wc_sent, "position": "replace",
            "text": "Sanitārais mezgls ir tikko renovēts."}]
    # ar WC → pārrakstītais parādās
    html_with = render_body(sg, dict(L, Agent_text_segments=json.dumps(seg)), dict(bp))
    ok &= _check("ar WC → pārrakstītais teikums parādās",
                 "tikko renovēts." in html_with)
    # noņem WC → WC teikuma nav → pārrakstītais NEparādās
    L_no = dict(L, cik_WC=None, Agent_text_segments=json.dumps(seg))
    html_no = render_body(sg, L_no, dict(bp))
    ok &= _check("noņem WC → pārrakstītais teksts PAZŪD",
                 "tikko renovēts." not in html_no)
    return ok


def test_replace_sentence():
    sg, L, bp = _office()
    orig = "Kopā ir 4 atsevišķas telpas."
    L1 = dict(L, Agent_text_segments=json.dumps([{
        "section": "telpa", "anchor_text": orig, "position": "replace",
        "text": "Telpās ir 4 gaišas darba telpas ar atsevišķu ieeju.",
    }]))
    html = render_body(sg, L1, dict(bp))
    ok = True
    ok &= _check("aizvieto teikumu (jaunais teksts ir)",
                 "4 gaišas darba telpas ar atsevišķu ieeju." in html)
    ok &= _check("oriģinālais teikums PAZUDIS", orig not in html)
    # replace + append uz TO PAŠU teikumu: abi paliek (append aiz aizvietotā)
    L2 = dict(L, Agent_text_segments=json.dumps([
        {"section": "telpa", "anchor_text": orig, "position": "replace",
         "text": "Telpās ir 4 darba telpas."},
        {"section": "telpa", "anchor_text": orig, "position": "after",
         "text": "Papildus ir noliktava 100 m²."},
    ]))
    html2 = render_body(sg, L2, dict(bp))
    ok &= _check("replace + append kopā (aizvietots + pieraksts aiz tā)",
                 "Telpās ir 4 darba telpas. Papildus ir noliktava 100 m²." in html2)
    # aizvietojamais teikums pazudis → NErenderē (atsaistīts; lieta ir prom)
    L3 = dict(L, Agent_text_segments=json.dumps([{
        "section": "telpa", "anchor_text": "Šāda teikuma nav", "position": "replace",
        "text": "AIZVIET-NEPARADAS.",
    }]))
    html3 = render_body(sg, L3, dict(bp))
    ok &= _check("pazudis aizvietojamais → NErenderē (atsaistīts)",
                 "AIZVIET-NEPARADAS." not in html3)
    return ok


if __name__ == "__main__":
    if "--dump" in sys.argv:
        dump()
    else:
        results = [
            test_byte_identical(),
            test_body_segments(),
            test_splice_after_sentence(),
            test_section_start_end(),
            test_detach_behavior(),
            test_removed_fact_drops_text(),
            test_replace_sentence(),
        ]
        print("\n" + ("VISI ZAĻI ✓" if all(results)
                      else "!!! KĀDS TESTS KRITA"))
        sys.exit(0 if all(results) else 1)
