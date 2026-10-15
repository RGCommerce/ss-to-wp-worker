"""pdf_import.py — īpašuma IMPORTS NO PDF (brošūra/piedāvājums → anketas forma).

Plūsma (Raimonds 2026-10-15, dizains HANDOFF_2026-10-15_PDF_MANUAL_IMPORT_DESIGN.md):
  1. Panelis augšupielādē PDF (POST /agent/import/analyze, multipart).
  2. Šeit: saglabā `agent_imports/<token>/<fails>.pdf` uz volume, izvelk teksta
     slāni (pypdf) + renderē lapas PNG (PyMuPDF) un sūta OpenAI (Responses API,
     strict JSON schema) → strukturēti ANKETAS lauki (FormState: building + units[]).
  3. Panelis ieliek rezultātu anketas stāvoklī → aģents pārskata/labo → «Pievienot DB»
     (POST /agent/publish ar intent='import', source='agent_pdf', import_token).
  4. agent_publish.publish_anketa (import zars) INSERT-o listingu ar Debug_status='ok',
     BEZ wp_export_queue, pārvieto PDF uz `listings/<id>/source/`, ieraksta
     `source_file_*` + `pdf_jobs` «done» rindu (LP/panelis var dot avota PDF).

Nekas DB šajā failā NERAKSTĀS — tikai fails uz volume + AI izsaukums.

Vide: OPENAI_API_KEY, STORAGE_ROOT, PDF_IMPORT_MODEL (fallback MODEL, tad gpt-5.4).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional

STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(Path(__file__).parent / "storage")))
IMPORTS_DIR = STORAGE_ROOT / "agent_imports"

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES_VISION = int(os.getenv("PDF_IMPORT_MAX_PAGES", "8"))
MAX_TEXT_CHARS = 40_000
PAGE_MAX_SIDE_PX = 1200


class PdfImportError(ValueError):
    """Lietotājam rādāma kļūda (400)."""


# ---------------------------------------------------------------------------
# Vārdnīcas — SAKRĪT ar paneļa anketa-eka/types.ts un DB enum (pārbaudīts dzīvē 2026-10-15)
# ---------------------------------------------------------------------------
SPACE_GROUPS = [
    "Birojs", "Tirdzniecība", "Noliktava", "Ražošana", "Medicīna", "Studija",
    "Restorans/Cafe", "Sporta zāle", "Autoserviss", "PVD", "StockOfiss",
    "Zeme", "Investīciju objekts",
]
BUILDING_COMPONENT_TYPES = [
    "Birojs", "Tirdzniecība", "Noliktava", "Ražošana", "Angārs", "Medicīna",
    "Restorans/Cafe", "Autoserviss", "Viesnīca", "Daudzstāvu dzīvojamā māja",
    "Privātmāja", "Guļbūve/pirts", "Nojume", "Palīgēka", "Cits",
]
SPACE_CONDITIONS = ["Jauns", "Labs", "Lietots", "Nepabeigts",
                    "Nepieciešams remonts", "Kapitālais remonts"]
PARKINGS = ["Ir vietas", "Ir vietas par maksu", "Ir vietas bezmaksas",
            "Tikai ielas parking", "Nav"]
BUILDING_TYPES = ["Biroju ēka", "Industriāla ēka", "Jaukta tipa ēka",
                  "Tirdzniecības centrs", "Medicīnas centrs", "Autoserviss"]
BUILDING_CLASSES = ["A", "B", "C"]
INVEST_STRATEGIES = ["Core/Core+", "Value-Add", "Distressed"]
CHECK_FIELDS = [
    "Pacelamie_varti_check", "Rampa_logistikai_check", "Virtuve_check",
    "Sava_ieeja_check", "street_entrance", "Apsargajama_teritorija_check",
    "Nozogota_teritorija_check", "Auto_pacelajs_check", "Treifelis_Pacelajs",
    "Ir_izlietne_telpa_check", "Balkons_check", "Sava_eka_check",
]
BUILDING_BOOL_FEATURES = [
    "is_business_complex", "has_managed", "has_multiple_buildings",
    "has_conference_room", "has_canteen", "has_accessibility", "has_fenced",
    "has_lift", "has_freight_lift", "has_gym", "has_underground_parking",
    "has_ev_charging", "has_bike_parking", "has_solar", "has_battery",
    "has_generator", "has_showers", "has_roof_terrace", "has_reception",
    "has_parcel_locker", "has_security_24_7", "has_cctv", "has_access_control",
    "owner_adapts_premises", "has_breeam",
]
FLOOR_OPTIONS = ["Pagrabs"] + [f"{i}.stāvs" for i in range(1, 13)] + \
    ["Vienstāvu", "Divstāvu", "Trīsstāvu"]


def _nullable(t: str, **extra) -> dict:
    return {"type": [t, "null"], **extra}


def _enum_nullable(values: list[str]) -> dict:
    return {"type": ["string", "null"], "enum": values + [None]}


def _build_schema() -> dict:
    check_props = {k: {"type": "string", "enum": ["checked", "not checked", "unknown"]}
                   for k in CHECK_FIELDS}
    feature_props = {k: {"type": ["boolean", "null"]} for k in BUILDING_BOOL_FEATURES}
    unit = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "space_group": _enum_nullable(SPACE_GROUPS),
            "potential_space_groups": {"type": "array", "items": {"type": "string", "enum": SPACE_GROUPS}},
            "area_m2": _nullable("number"),
            "floor": _nullable("string"),
            "price": _nullable("number"),
            "price_per_m2": _nullable("number"),
            "price_type": _enum_nullable(["rent", "sale"]),
            "price_note": _nullable("string"),
            "Apsaimniekosanas_maksa": _nullable("string"),
            "Papildu_maksas": _nullable("string"),
            "available_from": _nullable("string"),
            "Space_condition": _enum_nullable(SPACE_CONDITIONS),
            "cik_telpas": _nullable("string"),
            "cik_WC": _nullable("string"),
            "Griestu_augstums": _nullable("string"),
            "electric_power_kw": _nullable("string"),
            "Gridas_izturiba_kg_m2": _nullable("string"),
            "Parkings": _enum_nullable(PARKINGS),
            "Zemes_gabals_m2": _nullable("number"),
            "building_area_m2": _nullable("number"),
            "land_use": _nullable("string"),
            "buildings": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "type": {"type": "string", "enum": BUILDING_COMPONENT_TYPES},
                    "area_m2": _nullable("number"),
                    "note": _nullable("string"),
                },
                "required": ["type", "area_m2", "note"],
            }},
            "investment_income": _nullable("string"),
            "investment_yield": _nullable("string"),
            "Investiciju_strategija": _enum_nullable(INVEST_STRATEGIES),
            "checks": {"type": "object", "additionalProperties": False,
                       "properties": check_props, "required": CHECK_FIELDS},
            "Agent_comment": {"type": "string"},
            "confidence": {"type": "number"},
            "evidence": {"type": "string"},
        },
    }
    unit["required"] = list(unit["properties"].keys())
    building = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "street": {"type": "string"},
            "city": {"type": "string"},
            "district": _nullable("string"),
            "building_type": _enum_nullable(BUILDING_TYPES),
            "building_class": _enum_nullable(BUILDING_CLASSES),
            "building_description": _nullable("string"),
            "building_name": _nullable("string"),
            "floors_count": _nullable("string"),
            "bdg_year": _nullable("string"),
            "features": {"type": "object", "additionalProperties": False,
                         "properties": feature_props, "required": BUILDING_BOOL_FEATURES},
        },
    }
    building["required"] = list(building["properties"].keys())
    top = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "language": {"type": "string"},
            "document_kind": {"type": "string",
                              "enum": ["single_property", "multi_unit", "investment_object", "land", "other"]},
            "other_broker": _nullable("string"),
            "building": building,
            "units": {"type": "array", "items": unit},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }
    top["required"] = list(top["properties"].keys())
    return top


JSON_SCHEMA = _build_schema()

PROMPT = """# ĪPAŠUMA BROŠŪRAS / PIEDĀVĀJUMA (PDF) ANALĪZE → RGC anketas lauki

Tu saņem komerciāla nekustamā īpašuma PDF (brošūra, piedāvājums, teaser) — teksta slāni
(var būt izjaukts pa blokiem) UN lapu attēlus. Dokuments var būt latviešu, angļu vai krievu
valodā. Izvade: TIKAI JSON pēc shēmas, vērtības LATVISKI (apraksti, piezīmes).

## Noteikumi
1. NEKO neizdomā. Ja lauka dokumentā nav → null (string laukiem — null, nevis "").
   `confidence` 0..1 katrai telpai = cik droši pamatlauki (adrese/platība/cena/darījums).
   `evidence` = īss citāts/atsauce no dokumenta, kur ņemta cena un platība.
2. ADRESE: `building.street` = iela + mājas numurs tieši kā dokumentā (piem. "Ganību dambis 25D");
   bez pilsētas. `city` = pilsēta (noklusēti "Rīga", ja skaidri Rīgas adrese/rajons). `district` =
   Rīgas apkaime/rajons, ja minēts (piem. "Sarkandaugava", "Centrs"), citādi null.
3. DARĪJUMS: `price_type` "rent" = noma (€/mēn), "sale" = pārdošana (kopējā cena €).
   `price` = KOPĒJĀ summa (nomai €/mēnesī par visu telpu, pārdošanai pilnā cena).
   `price_per_m2` = €/m² (nomai mēnesī), ja dokumentā norādīts vai tieši izrēķināms.
   NEKAD nesajauc €/m² ar kopējo cenu. PVN / "plus komunālie" u.tml. → `price_note`.
   Summas rakstīt kā skaitļus bez atstarpēm/valūtas: "€ 3,920,000" → 3920000; "6,05 €/m²" → 6.05.
4. VIENS vai VAIRĀKI ĪPAŠUMI:
   - Viena telpa/ēka → 1 unit.
   - Brošūra ar VAIRĀKĀM atsevišķi iznomājamām telpām (katrai sava platība un cena) → katrai sava unit.
   - INVESTĪCIJU OBJEKTS (pārdod ēku/kompleksu ar nomniekiem; minēti īres ienākumi NOI, yield,
     iznomātības %, vairākas ēkas/funkcijas) → TIEŠI 1 unit ar `space_group`="Investīciju objekts":
     `buildings` = ēku/komponenšu saraksts (tips + platība), `investment_income` (piem.
     "147 350 €/gadā (pašreiz), 300 000 €/gadā pie 100% iznomātības"), `investment_yield`
     (piem. "7,85 %"), `Investiciju_strategija` (Core/Core+ = stabils, pilnībā iznomāts;
     Value-Add = ar attīstības/iznomāšanas potenciālu; Distressed = problemātisks),
     `area_m2` = iznomājamā platība (NLA), `building_area_m2` = kopējā ēku platība (GBA),
     `Zemes_gabals_m2` = zemes platība, `potential_space_groups` = komerc-tipi ēkās.
   - ZEME (apbūves gabals bez ēkas) → `space_group`="Zeme", `Zemes_gabals_m2`, `land_use`
     (pielietojums: Komerciāla apbūve / Ražošanas apbūve / Daudzstāvu dzīvojamā apbūve /
     Privātmāju apbūve / Lauksaimniecība / Cits).
5. `space_group` = galvenais telpu tips (Birojs, Tirdzniecība, Noliktava, Ražošana, StockOfiss =
   noliktava+birojs vienā, Medicīna, Restorans/Cafe, Studija, Sporta zāle, Autoserviss, PVD =
   pārtikas ražošana). `potential_space_groups` = citi tipi, kam telpa der.
6. `floor` — rakstīt kā "1.stāvs", "2.stāvs", "Pagrabs", "Vienstāvu", "Divstāvu" u.tml.; ja nav → null.
7. `checks`: "checked" tikai ja dokumentā SKAIDRI ir (vārti, rampa, virtuve, sava ieeja,
   ieeja no ielas, apsargāta/nožogota teritorija, auto pacēlājs, kravas lifts, izlietne,
   balkons, sava ēka); "not checked" tikai ja skaidri NAV; citādi "unknown".
8. `building.features`: true/false TIKAI ja dokumentā skaidri; citādi null. (žogs, video,
   apsardze, piekļuves kontrole, saules paneļi, EV uzlāde, lifts, kravas lifts, pazemes
   stāvvieta, vides pieejamība, biznesa komplekss, vairākas ēkas, konferenču zāle, ēdnīca …)
9. `Agent_comment` = LATVISKI 2–6 teikumi: kas tas ir par īpašumu, galvenās priekšrocības,
   tehniskie fakti (griesti, jauda, apkure, piekļuve). Bez kontaktiem, bez aģentūru nosaukumiem,
   bez mārketinga saukļiem. `building_description` = 1–3 teikumi par pašu ēku/kompleksu.
10. KONTAKTUS (vārdi, tālruņi, e-pasti) NEIZVILKT nekur. Citas aģentūras/brokera zīmolu,
    ja redzams, ieraksti `other_broker` (tikai informācijai).
11. `warnings` — īsi LV brīdinājumi aģentam: kas nav skaidrs, kur cena/platība var būt
    pārprasta, vai dokuments satur vairākus īpašumus u.tml.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    base = Path(name or "dokuments.pdf").name
    base = re.sub(r"[^\w.\-() ]+", "_", base, flags=re.UNICODE).strip() or "dokuments.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base[:120]


def extract_text(pdf_path: Path) -> tuple[str, int]:
    """pypdf teksta slānis visām lapām. Atgriež (teksts, lapu skaits)."""
    from pypdf import PdfReader  # lazy (JAU requirements — pdf_maker bulk)
    reader = PdfReader(str(pdf_path))
    parts: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        t = re.sub(r"[ \t]+", " ", t).strip()
        if t:
            parts.append(f"--- {i}. lapa ---\n{t}")
    text = "\n\n".join(parts)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n[... teksts apgriezts ...]"
    return text, len(reader.pages)


def render_pages(pdf_path: Path, max_pages: int = MAX_PAGES_VISION) -> list[str]:
    """PyMuPDF → PNG data-URL saraksts (garākā mala ≤ PAGE_MAX_SIDE_PX)."""
    import pymupdf  # lazy
    urls: list[str] = []
    with pymupdf.open(str(pdf_path)) as doc:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            rect = page.rect
            longest = max(rect.width, rect.height) or 1.0
            zoom = min(2.0, PAGE_MAX_SIDE_PX / longest)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
            urls.append(f"data:image/png;base64,{b64}")
    return urls


def _build_messages(file_name: str, text: str, page_urls: list[str]) -> list[dict]:
    content: list[dict[str, Any]] = [
        {"type": "input_text",
         "text": ("Tu analizē komerciāla nekustamā īpašuma PDF brošūru RGC brokeru panelim. "
                  "Izskati VISAS lapas (teksts + attēli) un atbildi TIKAI ar JSON pēc shēmas.")},
        {"type": "input_text", "text": f"Faila nosaukums: {file_name}"},
        {"type": "input_text",
         "text": ("PDF teksta slānis (var būt izjaukts pa blokiem; ja tukšs — lasi no attēliem):\n"
                  + (text or "(nav teksta slāņa)"))},
        {"type": "input_text", "text": f"Lapu attēlu skaits: {len(page_urls)} (oriģinālajā secībā)."},
    ]
    for i, url in enumerate(page_urls, start=1):
        content.append({"type": "input_text", "text": f"#page-{i}"})
        content.append({"type": "input_image", "image_url": url, "detail": "high"})
    content.append({"type": "input_text", "text": PROMPT})
    return [{"role": "user", "content": content}]


def _openai_extract(file_name: str, text: str, page_urls: list[str]) -> dict:
    from openai import OpenAI  # lazy
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise PdfImportError("OPENAI_API_KEY nav konfigurēts workerī — PDF analīze nav iespējama.")
    model = os.getenv("PDF_IMPORT_MODEL") or os.getenv("MODEL") or "gpt-5.4"
    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        input=_build_messages(file_name, text, page_urls),
        text={"format": {"type": "json_schema", "name": "pdf_import",
                         "schema": JSON_SCHEMA, "strict": True}},
    )
    return json.loads(response.output_text)


# ---------------------------------------------------------------------------
# AI JSON → anketas FormState (building + units) — paneļa types.ts forma
# ---------------------------------------------------------------------------

def _numstr(v: Any) -> str:
    if v is None or v == "":
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v).strip()
    if f.is_integer():
        return str(int(f))
    return f"{f:.2f}".rstrip("0").rstrip(".")


def _str(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _floor(v: Any) -> str:
    s = _str(v)
    if not s:
        return ""
    if s in FLOOR_OPTIONS:
        return s
    m = re.match(r"^\s*(-?\d{1,2})\b", s)
    if m:
        n = int(m.group(1))
        if n == 0 or n < 0:
            return "Pagrabs"
        if 1 <= n <= 12:
            return f"{n}.stāvs"
    low = s.lower()
    if "pagrab" in low or "basement" in low or "cellar" in low:
        return "Pagrabs"
    if low.startswith("ground") or low.startswith("1st") or low.startswith("first"):
        return "1.stāvs"
    return s


def to_form_state(ai: dict) -> tuple[dict, list[dict], list[str]]:
    """Atgriež (form, units_meta, warnings). form = {mode, building, units, active_unit_id}."""
    warnings: list[str] = list(ai.get("warnings") or [])
    b = ai.get("building") or {}
    feats = b.get("features") or {}
    building = {
        "existing_bp_id": None,
        "street": _str(b.get("street")),
        "city": _str(b.get("city")) or "Rīga",
        "district": _str(b.get("district")),
        "building_type": b.get("building_type") or None,
        "building_class": b.get("building_class") or None,
        "building_description": _str(b.get("building_description")),
        "apkure": [], "ventilacija": [], "ugunsdrosiba": [],
        "building_name": _str(b.get("building_name")),
        "floors_count": _numstr(b.get("floors_count")),
        "bdg_year": _numstr(b.get("bdg_year")),
        "ednica_nosaukums": "",
        "owner": "", "responsible": "", "primary_phone": "", "secondary_phone": "", "email": "",
        "partnership_status": None,
    }
    for k in BUILDING_BOOL_FEATURES:
        v = feats.get(k)
        building[k] = v if isinstance(v, bool) else None

    units: list[dict] = []
    meta: list[dict] = []
    for i, u in enumerate(ai.get("units") or [], start=1):
        checks = u.get("checks") or {}
        pt = u.get("price_type")
        unit: dict[str, Any] = {
            "space_group": u.get("space_group") or None,
            "potential_space_groups": [g for g in (u.get("potential_space_groups") or [])
                                       if g in SPACE_GROUPS and g != u.get("space_group")],
            "area_m2": _numstr(u.get("area_m2")),
            # Zemei / investīciju objektam stāvs nav jēgpilns (AI mēdz likt "Jaukts") → tukšs.
            "floor": ("" if u.get("space_group") in ("Zeme", "Investīciju objekts")
                      else _floor(u.get("floor"))),
            "price": _numstr(u.get("price")),
            "price_per_m2": _numstr(u.get("price_per_m2")),
            "price_type": "monthly" if pt == "rent" else ("regular" if pt == "sale" else None),
            "Apsaimniekosanas_maksa": _str(u.get("Apsaimniekosanas_maksa")),
            "Papildu_maksas": _str(u.get("Papildu_maksas")),
            "price_includes": [],
            "is_project": False,
            "project_completion": "",
            "images": [],
            "agent_wp_id": None,
            "Space_condition": u.get("Space_condition") or None,
            "cik_telpas": _numstr(u.get("cik_telpas")),
            "cik_WC": _numstr(u.get("cik_WC")),
            "WC_location": None,
            "Griestu_augstums": _str(u.get("Griestu_augstums")),
            "electric_power_kw": _str(u.get("electric_power_kw")),
            "Gridas_izturiba_kg_m2": _str(u.get("Gridas_izturiba_kg_m2")),
            "Zemes_gabals_m2": _numstr(u.get("Zemes_gabals_m2")),
            "land_use": u.get("land_use") or None,
            "communications": [],
            "building_on_plot": None,
            "building_area_m2": _numstr(u.get("building_area_m2")),
            "buildings": [
                {"type": bb.get("type") or "Cits", "area_m2": _numstr(bb.get("area_m2")),
                 "note": _str(bb.get("note"))}
                for bb in (u.get("buildings") or []) if isinstance(bb, dict)
            ],
            "investment_income": _str(u.get("investment_income")),
            "investment_yield": _str(u.get("investment_yield")),
            "Investiciju_strategija": u.get("Investiciju_strategija") or None,
            "Parkings": u.get("Parkings") or None,
            "Agent_comment": _str(u.get("Agent_comment")),
        }
        for cf in CHECK_FIELDS:
            v = checks.get(cf)
            unit[cf] = v if v in ("checked", "not checked") else None
        # Investīciju objekts bez komponentēm — anketa to pieļauj, bet brīdinām
        if unit["space_group"] == "Investīciju objekts" and not unit["buildings"]:
            warnings.append(f"Telpa #{i}: investīciju objektam nav atrastas ēkas/komponentes — pievieno pats.")
        if unit["space_group"] == "Investīciju objekts" and not unit["Investiciju_strategija"]:
            warnings.append(f"Telpa #{i}: izvēlies investīciju stratēģiju (match to prasa).")
        if not unit["space_group"]:
            warnings.append(f"Telpa #{i}: AI nenoteica telpu tipu — izvēlies pats (obligāts matcham).")
        if not unit["area_m2"]:
            warnings.append(f"Telpa #{i}: nav atrasta platība.")
        if not unit["price"]:
            warnings.append(f"Telpa #{i}: nav atrasta cena.")
        if u.get("price_note"):
            warnings.append(f"Telpa #{i}: cenas piezīme — {u['price_note']}")
        if u.get("available_from"):
            warnings.append(f"Telpa #{i}: pieejams no — {u['available_from']}")
        units.append(unit)
        meta.append({"confidence": u.get("confidence"), "evidence": u.get("evidence")})

    if not building["street"]:
        warnings.append("AI neatrada adresi — ieraksti ielu un numuru pats (obligāts).")
    if ai.get("other_broker"):
        warnings.append(f"Dokumentā redzams cits brokeris/aģentūra: {ai['other_broker']} — kontaktus nepārņemam.")
    form = {"mode": "full", "building": building, "units": units, "active_unit_id": None}
    return form, meta, warnings


# ---------------------------------------------------------------------------
# Publiskā API
# ---------------------------------------------------------------------------

def token_dir(token: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise PdfImportError("Nederīgs importa token.")
    return IMPORTS_DIR / token


def find_token_pdf(token: str) -> Optional[Path]:
    d = token_dir(token)
    if not d.is_dir():
        return None
    pdfs = sorted(p for p in d.glob("*.pdf") if p.is_file())
    return pdfs[0] if pdfs else None


def analyze_pdf(file_bytes: bytes, file_name: str, wp_user_id: int = 0) -> dict:
    """Saglabā PDF + AI analīze. Atgriež anketas FormState + meta. DB netiek aiztikta."""
    if not file_bytes:
        raise PdfImportError("Tukšs fails.")
    if len(file_bytes) > MAX_PDF_BYTES:
        raise PdfImportError(f"PDF pārāk liels ({len(file_bytes) // (1024 * 1024)} MB; max 25 MB).")
    if not file_bytes[:5].startswith(b"%PDF"):
        raise PdfImportError("Fails nav PDF (trūkst %PDF galvenes).")

    token = uuid.uuid4().hex
    d = token_dir(token)
    d.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(file_name)
    pdf_path = d / safe_name
    pdf_path.write_bytes(file_bytes)

    warnings: list[str] = []
    try:
        text, page_count = extract_text(pdf_path)
    except Exception as e:
        text, page_count = "", 0
        warnings.append(f"Teksta slāni neizdevās nolasīt ({type(e).__name__}) — analīze tikai no attēliem.")
    try:
        page_urls = render_pages(pdf_path)
    except Exception as e:
        page_urls = []
        warnings.append(f"Lapu attēlus neizdevās renderēt ({type(e).__name__}) — analīze tikai no teksta.")
    if not text.strip() and not page_urls:
        shutil.rmtree(d, ignore_errors=True)
        raise PdfImportError("No PDF neizdevās iegūt ne tekstu, ne attēlus.")
    if len(text.strip()) < 200 and page_urls:
        warnings.append("PDF gandrīz bez teksta slāņa (skenēts/dizaina fails) — dati nolasīti no attēliem, pārbaudi skaitļus.")
    if page_count > MAX_PAGES_VISION:
        warnings.append(f"PDF ir {page_count} lapas — attēlos analizētas pirmās {MAX_PAGES_VISION} (teksts — visām).")

    try:
        ai = _openai_extract(safe_name, text, page_urls)
    except PdfImportError:
        raise
    except Exception as e:
        raise PdfImportError(f"AI analīze neizdevās: {type(e).__name__}: {str(e)[:300]}")

    try:
        (d / "analysis.json").write_text(json.dumps(ai, ensure_ascii=False, indent=1), encoding="utf-8")
        (d / "meta.json").write_text(json.dumps({
            "file_name": file_name, "safe_name": safe_name, "wp_user_id": wp_user_id,
            "page_count": page_count, "text_chars": len(text),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    form, meta, w2 = to_form_state(ai)
    warnings.extend(w2)
    return {
        "ok": True,
        "draft_token": token,
        "file_name": safe_name,
        "page_count": page_count,
        "text_chars": len(text),
        "document_kind": ai.get("document_kind"),
        "language": ai.get("language"),
        "other_broker": ai.get("other_broker"),
        "form": form,
        "units_meta": meta,
        "warnings": warnings,
    }


def attach_pdf_to_listing(token: str, listing_id: int) -> Optional[dict]:
    """Pārvieto `agent_imports/<token>/<fails>.pdf` → `listings/<id>/source/<uuid>.pdf`.
    Atgriež {path (STORAGE_ROOT relatīvs), name (oriģinālais), raw_json} vai None, ja faila nav."""
    src = find_token_pdf(token)
    if not src:
        return None
    dest_dir = STORAGE_ROOT / "listings" / str(listing_id) / "source"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}.pdf"
    shutil.move(str(src), str(dest))
    raw_json = None
    try:
        aj = src.parent / "analysis.json"
        if aj.is_file():
            raw_json = aj.read_text(encoding="utf-8")
    except Exception:
        raw_json = None
    shutil.rmtree(src.parent, ignore_errors=True)
    return {
        "path": str(dest.relative_to(STORAGE_ROOT)).replace("\\", "/"),
        "name": src.name,
        "raw_json": raw_json,
    }


def cleanup_stale_imports(max_age_days: int = 7) -> int:
    """Nepabeigti importi (analyze bez commit) — dzēš pēc max_age_days. Best-effort."""
    import time
    if not IMPORTS_DIR.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    n = 0
    for d in IMPORTS_DIR.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except Exception:
            pass
    return n
