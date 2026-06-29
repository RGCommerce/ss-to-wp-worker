"""pdf_maker.py — RGC sludinājuma PDF brošūra (1 īpašums = 1 brošūra).

Ģenerē klientam sūtāmu PDF no listing datiem + AI-apstrādātajām bildēm.
HTML+CSS → PDF caur WeasyPrint. RGC brand (navy/red/cream, Playfair/Open Sans).

Struktūra:
  1. Titullapa — hero bilde (fasāde) + adrese + platība/lokācija + cena + €/m²
  2+ Galvenie fakti (visi AI-analīzes lauki) + apraksts + galerija — plūst
  Pēdējā — kontakti ar aģenta foto (Raimonds vai Ieva)

Bildes tiek samazinātas (downscale) pirms iegulšanas → PDF ~2-3 MB, ne 14 MB.

Lietošana:
  python crm/pdf_maker.py --listing 525
  python crm/pdf_maker.py --listing 525 --out C:/tmp/525.pdf

Galvenā lietošana = library (ss-to-wp-worker POST /pdf/{id}):
  from pdf_maker import render_pdf
  pdf_bytes = render_pdf(525)

Vide (crm/.env): DATABASE_URL, STORAGE_ROOT.
Atkarība: weasyprint, Pillow (Railway vajag system libs — sk. Dockerfile).
"""
from __future__ import annotations

import argparse
import base64
import html as _html
import io
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from PIL import Image
from psycopg.rows import dict_row

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).parent))
from wp_templates import (render_body, _floor as _clean_floor,  # noqa: E402
                          _street_nominative, _cap)
import image_classify  # noqa: E402
import image_pipeline  # noqa: E402  # Seedream AI bilžu skaistināšana

load_dotenv(Path(__file__).parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(Path(__file__).parent / "storage")))
ASSETS_DIR = Path(__file__).parent / "assets"

# RGC agentu kontakti (memory project_melna_kaste_agent_assignment: 4238=Ieva,
# 4456=Raimonds). Telefonus precizē šeit — viegli rediģēt.
AGENT_IEVA = 4238
AGENT_RAIMONDS = 4456
IEVA_GROUPS = {"Birojs", "Medicīna", "Tirdzniecība", "Studija"}
_SALE_PT = {"regular", "parastā", "parasta", "sale", "sell", "buy"}
AGENT_CONTACTS = {
    AGENT_IEVA: {
        "name": "Ieva", "phone": "", "email": "",
        "photo": "agent_ieva.jpg",
    },
    AGENT_RAIMONDS: {
        "name": "Raimonds Grīnbergs",
        "phone": "+371 23072 4004",
        "email": "raimonds@rgcommerce.lv",
        "photo": "agent_raimonds.jpg",
    },
}

# Mūsu logo (RGC Commercial Real Estate Firm) — transparent PNG → uzliek
# uz kontaktu lapas navy fona bez baltas kastes ap to
BRAND_LOGO = "rgc_logo.png"

# Houzez Space_group → lietvārds virsrakstam
_VEIDS = {
    "Birojs": "Biroja telpas", "Tirdzniecība": "Tirdzniecības telpas",
    "Noliktava": "Noliktavas telpas", "Ražošana": "Ražošanas telpas",
    "Medicīna": "Medicīnas telpas", "Restorans/Cafe": "Ēdināšanas telpas",
    "Studija": "Studijas telpas", "Autoserviss": "Autoservisa telpas",
}

# Vērtības, kuras uzskatām par "tukšām" (nerādām faktos)
_MISSING = {"", "unknown", "nezināms", "nezinams", "nav", "none", "null",
            "n/a", "-", "~", "0", "[]"}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _fetch(conn, listing_id: int) -> tuple[dict, dict]:
    listing = conn.execute(
        "SELECT * FROM properties.listings WHERE id = %s", (listing_id,)
    ).fetchone()
    if not listing:
        raise SystemExit(f"Listing {listing_id} nav atrasts DB.")
    bp = {}
    if listing.get("building_profile_id"):
        bp = conn.execute(
            "SELECT * FROM properties.building_profiles WHERE id = %s",
            (listing["building_profile_id"],),
        ).fetchone() or {}
    return dict(listing), dict(bp)


def _title(listing: dict, bp: dict) -> str:
    full = (bp.get("full_address") or "").strip()
    if full:
        return full
    parts = [listing.get("street"), listing.get("city")]
    addr = ", ".join(p.strip() for p in parts if p and str(p).strip())
    return addr or f"Komercīpašums (listing {listing['id']})"


def _num(v):
    import re
    if v is None:
        return None
    m = re.search(r"\d+[.,]?\d*", str(v))
    return m.group(0).replace(",", ".") if m else None


def _money(v) -> str:
    s = str(v or "").strip()
    if not s:
        return s
    intp, _, dec = s.partition(".")
    grouped, intp = "", intp
    while len(intp) > 3:
        grouped = " " + intp[-3:] + grouped
        intp = intp[:-3]
    return (intp + grouped) + (f".{dec}" if dec else "")


def _clean(v):
    """Atgriež tīru string vai None, ja vērtība ir tukša/unknown."""
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in _MISSING else s


def _yn(v):
    """check-lauks → 'Jā'/'Nē'/None (unknown → None)."""
    s = str(v or "").strip().lower()
    if s in ("checked", "jā", "ja", "yes", "true"):
        return "Jā"
    if s in ("not checked", "nē", "ne", "no", "false"):
        return "Nē"
    return None


# ---------------------------------------------------------------------------
# Galvenie fakti — VISI ne-tukšie AI-analīzes lauki, cilvēciskā valodā
# ---------------------------------------------------------------------------

# Specializētām telpām "Birojs" kā potenciāls nav reāls (tāpat kā wp_templates).
# Pašu telpas tipu arī neatkārtojam. (Raimonds 2026-06-05)
_NO_OFFICE_GROUPS = {"Restorans/Cafe", "PVD", "Ražošana", "Noliktava",
                     "Autoserviss", "Medicīna", "Sporta zāle"}


def _filter_potential(pot, space_group):
    """Potential_space_group → tīrs saraksts: bez pašu telpas tipa un bez 'Birojs'
    specializētām telpām (citādi nereāls pielietojums)."""
    s = _clean(pot)
    if not s:
        return None
    sg = (space_group or "").strip()
    items = [p.strip() for p in s.split(",") if p.strip() and p.strip() != sg]
    if sg in _NO_OFFICE_GROUPS:
        items = [p for p in items if p != "Birojs"]
    return ", ".join(items) or None


def _facts(listing: dict, bp: dict) -> list[tuple[str, str]]:
    """Sakārtots (etiķete, vērtība) saraksts no AI-analīzes. Tukšos /
    'unknown' laukus izlaiž. Raimonds vēlāk pasaka, ko ņemt ārā."""
    L = listing
    out: list[tuple[str, str]] = []

    def add(label, val):
        if val is None:
            return
        s = str(val).strip()
        if s and s.lower() not in _MISSING:
            out.append((label, s))

    # --- Izmēri / ģeometrija ---
    # Platība, Cena par m², Zemes platība — IZLAISTI: tie jau ir titullapā
    add("Telpu skaits", _num(L.get("Cik_telpas")))
    add("Sanmezgli (WC)", _clean(L.get("cik_WC")))
    floor = _clean_floor(L.get("floor"))
    add("Stāvi", _clean(L.get("floor")) if floor else None)
    add("Griestu augstums", _clean(L.get("Griestu_augstums")))
    add("Grīdas materiāls", _clean(L.get("Gridas_materials")))
    izt = _num(L.get("Gridas_izturiba_kg_m2"))
    add("Grīdu slodze", f"~{izt} kg/m²" if izt else None)
    pwr = _num(L.get("electric_power_kw"))
    add("Elektrības jauda", f"{pwr} kW" if pwr else None)

    # --- Ēka ---
    add("Ēkas tips", _clean(L.get("building_type")))
    bc = _clean(L.get("building_class"))
    add("Ēkas klase", f"{bc} klase" if bc else None)
    add("Telpu stāvoklis", _clean(L.get("Space_condition")))
    add("Apkure", _clean(L.get("Apkure")))
    add("Logi", _clean(L.get("Logu_type")))
    add("Mēbelēta", _clean(L.get("Mebeleta_telpa")))
    add("Autostāvvieta", _clean(L.get("Parkings")))

    # --- Check-lauki (Jā/Nē) ---
    add("Ventilācijas sistēma", _yn(L.get("Ventilacijas_sistema_check")))
    add("Atsevišķa ieeja", _yn(L.get("Sava_ieeja_check")))
    add("Ieeja no ielas", _yn(L.get("street_entrance")))
    add("Atsevišķa ēka", _yn(L.get("Sava_eka_check")))
    add("Virtuve", _yn(L.get("Virtuve_check")))
    add("Izlietne telpā", _yn(L.get("Ir_izlietne_telpa_check")))
    add("Balkons", _yn(L.get("Balkons_check")))
    add("Dalāma telpa", _yn(L.get("Dalama_telpa")))
    add("Vides pieejamība", _yn(L.get("Vides_pieejamiba_check")))
    add("Mazgājamas sienas", _yn(L.get("Mazgajamas_sienas_check")))
    add("Apsargāta teritorija", _yn(L.get("Apsargajama_teritorija_check")))
    add("Nožogota teritorija", _yn(L.get("Nozogota_teritorija_check")))

    pv = _yn(L.get("Pacelamie_varti_check"))
    if pv == "Jā":
        cnt = _num(L.get("Pacelamie_varti_count"))
        add("Paceļamie vārti", f"Jā ({cnt})" if cnt else "Jā")
    elif pv:
        add("Paceļamie vārti", pv)
    rp = _yn(L.get("Rampa_logistikai_check"))
    if rp == "Jā":
        cnt = _num(L.get("Rampa_logistikai_count"))
        add("Rampa loģistikai", f"Jā ({cnt})" if cnt else "Jā")
    elif rp:
        add("Rampa loģistikai", rp)
    add("Auto pacēlājs", _yn(L.get("Auto_pacelajs_check")))
    add("Treilera pacēlājs", _yn(L.get("Treifelis_Pacelajs")))

    # --- Pielietojums / izmaksas ---
    add("Pielietojuma potenciāls", _filter_potential(L.get("Potential_space_group"),
                                                     L.get("Space_group")))
    # Investiciju_strategija = IEKŠĒJA klasifikācija (mums), NE klienta brošūrai.
    # Raimonds 2026-06-25 — izņemts gan no sludinājuma virsraksta, gan no PDF.
    add("Apsaimniekošana", _clean(L.get("Apsaimniekosanas_maksa")))
    add("NĪN", _clean(L.get("NIN")))
    add("Komunālie maksājumi", _clean(L.get("Komunalie")))
    add("Papildu maksas", _clean(L.get("Papildu_maksas")))

    return out


# ---------------------------------------------------------------------------
# Bildes — fasāde pirmā, plāns izlaists; downscale pirms iegulšanas
# ---------------------------------------------------------------------------

def _ordered_images(listing_id: int) -> list[Path]:
    """ai_ready bildes, sakārtotas fasāde→interjers→cits, plāns IZLAISTS."""
    ai_dir = STORAGE_ROOT / "listings" / str(listing_id) / "ai_ready"
    if not ai_dir.is_dir():
        return []
    imgs = sorted(ai_dir.glob("img_*.jpg"))
    try:
        manifest = image_classify.load_manifest(STORAGE_ROOT, listing_id)
    except Exception:
        manifest = {}
    order = {"fasade": 0, "interjers": 1, "cits": 2}
    gallery = [p for p in imgs
               if (manifest.get(p.name) or {}).get("type") != "plans"]
    gallery.sort(key=lambda p: order.get(
        (manifest.get(p.name) or {}).get("type", "interjers"), 1))
    return gallery


def _data_uri(path: Path, max_w: int, quality: int = 82) -> str:
    """Ielādē bildi, samazina līdz max_w platumam, atgriež base64 data-URI.
    Tā PDF nav atkarīgs no failu ceļiem un ir krietni mazāks (downscale)."""
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return ""
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _logo_data_uri(path: Path, max_w: int) -> str:
    """Logo PNG ar alpha kanālu — saglabā transparency, neuzliek baltu fonu."""
    try:
        im = Image.open(path)
    except Exception:
        return ""
    if im.mode not in ("RGBA", "LA"):
        im = im.convert("RGBA")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
@page { size: A4; margin: 18mm 16mm; }
@page cover { margin: 0; }
@page contact { margin: 0; }

* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Open Sans', 'DejaVu Sans', sans-serif;
       color: #1a2638; font-size: 10.5pt; line-height: 1.45; }

/* ---- Titullapa ---- */
.cover { page: cover; page-break-after: always; }
.cover-hero { width: 210mm; height: 172mm; object-fit: cover; display: block; }
.cover-band { background: #1a2638; color: #f7f3ed;
              min-height: 125mm; padding: 15mm 18mm 16mm; }
.cover-kicker { color: #c8202a; font-size: 11pt; letter-spacing: 3px;
                text-transform: uppercase; font-weight: 700; }
.cover-title { font-family: 'Playfair Display', 'DejaVu Serif', serif;
               font-size: 32pt; font-weight: 700; margin: 5mm 0 9mm; }
.cover-facts { line-height: 1.3; }
.cf-col { display: inline-block; vertical-align: top; }
.cf-col + .cf-col { margin-left: 22mm; }
/* 3-kolonu režīms (kad ir Zemes platība) — tuvāki gapi, mazāks fonts vērtībai */
.cover-facts.cf-3col .cf-col + .cf-col { margin-left: 13mm; }
.cover-facts.cf-3col .cf-v { font-size: 13.5pt; }
.cf-v { font-weight: 700; font-size: 15pt; }
.cf-l { color: #9aa6b6; font-size: 8pt; text-transform: uppercase;
        letter-spacing: 2px; margin-top: 1mm; }
.cover-price { font-family: 'Playfair Display', 'DejaVu Serif', serif;
               font-size: 23pt; font-weight: 700; color: #c8202a;
               margin-top: 11mm; }
.cover-ppm2 { color: #c0c8d2; font-size: 12pt; margin-top: 2mm; }

/* ---- Iekšlapas ---- */
.h2 { font-family: 'Playfair Display', 'DejaVu Serif', serif;
      font-size: 17pt; font-weight: 700; color: #1a2638;
      border-bottom: 2pt solid #c8202a; padding-bottom: 2mm;
      margin: 0 0 6mm; page-break-after: avoid; }
/* Katrai sadaļai sava lapa — apraksts / fakti / bildes (contact pati
   ar page-break-before) */
.section { margin-bottom: 10mm; page-break-after: always; }

/* ---- Faktu tabula (3 kolonas) ---- */
.facts { width: 100%; border-collapse: separate; border-spacing: 3mm;
         table-layout: fixed; }
.facts td { width: 33.33%; background: #f7f3ed; border: 1pt solid #e8e2d8;
            border-radius: 3pt; padding: 3.5mm 4mm; vertical-align: top; }
.facts td.empty { background: none; border: none; }
.fact-l { color: #8a93a0; font-size: 7.5pt; text-transform: uppercase;
          letter-spacing: 1px; }
.fact-v { font-weight: 700; font-size: 11.5pt; margin-top: 1mm; }

/* ---- Apraksts ---- */
.desc p { margin-bottom: 3mm; }
.desc strong { color: #1a2638; }

/* ---- Galerija (2 kolonas, tabula → korekta lapošana) ---- */
.gallery { width: 100%; border-collapse: separate; border-spacing: 3mm;
           table-layout: fixed; }
.gallery td { width: 50%; padding: 0; }
.gallery tr { page-break-inside: avoid; }
.gallery img { width: 100%; height: 60mm; object-fit: cover;
               border-radius: 3pt; display: block; }

/* ---- Kontaktu lapa (logo augšā, bilde apakšā) ---- */
.contact { page: contact; page-break-before: always;
           background: #1a2638; color: #f7f3ed; height: 297mm;
           display: flex; flex-direction: column;
           justify-content: center; align-items: center;
           text-align: center; padding: 20mm; }
.brand-logo { width: 110mm; height: auto; display: block;
              margin: 0 auto 4mm; }
.contact-h { font-family: 'Playfair Display', 'DejaVu Serif', serif;
             font-size: 24pt; font-weight: 700; margin: 9mm 0 6mm; }
.agent-name { font-size: 16pt; font-weight: 700; margin-bottom: 4mm; }
.contact-row { font-size: 12.5pt; color: #d4dae2; margin-bottom: 2mm; }
.contact-sep { width: 32mm; height: 2pt; background: #c8202a;
               margin: 10mm auto 8mm; }
.agent-photo { width: 72mm; height: 92mm; object-fit: cover;
               border-radius: 4pt; border: 2.5pt solid #c8202a;
               display: block; margin: 0 auto; }
.contact-url { color: #c8202a; font-weight: 700; font-size: 12pt;
               letter-spacing: 2px; margin-top: 6mm;
               text-decoration: none; display: inline-block; }
a.contact-url:visited { color: #c8202a; }
"""


def _esc(s) -> str:
    return _html.escape(str(s or ""))


# ---------------------------------------------------------------------------
# HTML būve
# ---------------------------------------------------------------------------

def build_html(listing: dict, bp: dict, listing_id: int) -> tuple[str, str]:
    """Atgriež (html_str, base_url) WeasyPrint-am."""
    sg = (listing.get("Space_group") or "").strip()
    veids = _VEIDS.get(sg, "Komerctelpas")
    sale = str(listing.get("price_type") or "").lower() in _SALE_PT
    # Cover virsraksts NOMINATĪVĀ ar 'iela' ("Stirnu iela 25"); fallback _title.
    addr = _street_nominative(listing.get("street") or bp.get("full_address")) \
        or _title(listing, bp)
    area = _num(listing.get("area_m2"))
    price = _num(listing.get("price"))
    ppm2 = _num(listing.get("price_per_m2"))
    if not ppm2 and price and area:
        try:
            ppm2 = str(round(float(price) / float(area), 2))
        except (ValueError, ZeroDivisionError):
            ppm2 = None
    city = _cap((bp.get("city") or listing.get("city") or "").strip())
    district = _cap((bp.get("district") or listing.get("district") or "").strip())
    loc_v = ", ".join(p for p in (district, city) if p)
    land = _num(listing.get("Zemes_gabals_m2"))

    # ---- Bildes (downscale) ----
    imgs = _ordered_images(listing_id)
    hero = _data_uri(imgs[0], 1900, 84) if imgs else ""
    gallery_imgs = imgs[1:] if len(imgs) > 1 else []

    # ---- Apraksts — tas pats render_body teksts ----
    # listing + bp ATSEVIŠĶI: street no listing = pilns "Stirnu 25";
    # bp.street = "Stirnu" (numurs atsevišķi house_number) → NEpārrakstīt.
    desc_html = render_body(sg if sg in _VEIDS else "Birojs", listing, bp)

    # ---- Agents ----
    is_sale_agent = str(listing.get("price_type") or "").lower() in _SALE_PT
    agent_id = (AGENT_RAIMONDS if is_sale_agent
                else (AGENT_IEVA if sg in IEVA_GROUPS else AGENT_RAIMONDS))
    agent = AGENT_CONTACTS.get(agent_id, AGENT_CONTACTS[AGENT_RAIMONDS])

    price_lbl = "Cena" if sale else "Noma mēnesī"
    price_str = (f"{_money(price)} EUR" if price else "Cena pēc pieprasījuma")
    if price and not sale:
        price_str += " / mēn."

    # ---- Faktu tabula (3 kolonas) ----
    # Tips un Atrašanās vieta — IZLAISTI: tie jau ir titullapā (kicker + lokācija).
    facts = _facts(listing, bp)
    cells = [
        f'<td><div class="fact-l">{_esc(l)}</div>'
        f'<div class="fact-v">{_esc(v)}</div></td>'
        for l, v in facts
    ]
    while len(cells) % 3:
        cells.append('<td class="empty"></td>')
    facts_rows = "".join(
        "<tr>" + "".join(cells[i:i + 3]) + "</tr>"
        for i in range(0, len(cells), 3)
    )

    # ---- Galerija (2 kolonas) ----
    gallery_rows = ""
    if gallery_imgs:
        gcells = [f'<td><img src="{_data_uri(p, 1200, 80)}"></td>'
                  for p in gallery_imgs]
        while len(gcells) % 2:
            gcells.append("<td></td>")
        gallery_rows = "".join(
            "<tr>" + "".join(gcells[i:i + 2]) + "</tr>"
            for i in range(0, len(gcells), 2)
        )
    gallery_section = (
        f'<div class="section"><div class="h2">Galerija</div>'
        f'<table class="gallery">{gallery_rows}</table></div>'
        if gallery_rows else "")

    # ---- Kontaktu lapa: aģenta foto + brand logo ----
    photo_path = ASSETS_DIR / agent.get("photo", "")
    photo_uri = _data_uri(photo_path, 800, 88) if photo_path.is_file() else ""
    logo_path = ASSETS_DIR / BRAND_LOGO
    logo_uri = _logo_data_uri(logo_path, 1200) if logo_path.is_file() else ""
    contact_rows = ""
    if agent.get("phone"):
        contact_rows += f'<div class="contact-row">Tālrunis: {_esc(agent["phone"])}</div>'
    if agent.get("email"):
        contact_rows += f'<div class="contact-row">E-pasts: {_esc(agent["email"])}</div>'

    hero_html = (f'<img class="cover-hero" src="{hero}">' if hero
                 else '<div class="cover-hero" style="background:#243349"></div>')
    photo_html = (f'<img class="agent-photo" src="{photo_uri}">' if photo_uri
                  else "")
    logo_html = (f'<img class="brand-logo" src="{logo_uri}">' if logo_uri
                 else '<div class="contact-url">RG COMMERCE</div>')

    # ---- Cover facts: 2 vai 3 kolonas (Zemes platība → 3.) ----
    cf_parts = [
        ('Platība',  _money(area) + ' m²' if area else '—'),
        ('Lokācija', loc_v or city or '—'),
    ]
    if land:
        cf_parts.append(('Zemes platība', _money(land) + ' m²'))
    cf_inner = "".join(
        f'<div class="cf-col"><div class="cf-v">{_esc(v)}</div>'
        f'<div class="cf-l">{_esc(l)}</div></div>'
        for l, v in cf_parts
    )
    cf_class = 'cover-facts' + (' cf-3col' if land else '')
    cover_facts_html = f'<div class="{cf_class}">{cf_inner}</div>'

    html_doc = f"""<!DOCTYPE html>
<html lang="lv"><head><meta charset="utf-8">
<style>@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Open+Sans:wght@400;600;700&display=swap');
{_CSS}</style></head><body>

<div class="cover">
  {hero_html}
  <div class="cover-band">
    <div class="cover-kicker">{_esc(veids)}</div>
    <div class="cover-title">{_esc(addr)}</div>
    {cover_facts_html}
    <div class="cover-price">{_esc(price_lbl)}: {_esc(price_str)}</div>
    {f'<div class="cover-ppm2">{_esc(ppm2)} EUR/m²</div>' if ppm2 else ''}
  </div>
</div>

<div class="section">
  <div class="h2">Apraksts</div>
  <div class="desc">{desc_html}</div>
</div>

<div class="section">
  <div class="h2">Galvenie fakti</div>
  <table class="facts">{facts_rows}</table>
</div>

{gallery_section}

<div class="contact">
  {logo_html}
  <div class="contact-h">Ieinteresēja šis īpašums?</div>
  <div class="agent-name">{_esc(agent["name"])}</div>
  {contact_rows}
  <div class="contact-sep"></div>
  {photo_html}
  <a class="contact-url" href="https://www.rgcommerce.lv">www.rgcommerce.lv</a>
</div>

</body></html>"""
    return html_doc, str(STORAGE_ROOT)


# ---------------------------------------------------------------------------
# PDF render
# ---------------------------------------------------------------------------

def _ensure_ai_ready(listing_id: int) -> None:
    """Ja ai_ready/ tukšs → palaiž image_pipeline + classify.

    Identiska loģika kā `publish_to_wp.publish()` — abi nodrošina, ka pirms
    rendera ai_ready/ ir aizpildīta. PDF tagad dara TO PAŠU, lai PDF nekad
    neiznāk bez bildēm.

    KRITISKI: NEPĀRBAUDA source! DB ir source='sslv' un 'wp' — abi var trūkt
    ai_ready/ (ss vēl nav publicēts; wp no inbox-to-listings ar manuāli
    pievienotām raw bildēm). image_pipeline.process_listing ir idempotents:
    ja ai_ready jau ir, tas neko nedara (~ms).

    Cold path (ai_ready/ tukša): image_pipeline (Seedream ~30s × bilžu skaits)
    + classify (gpt-4o-mini). ~3-8 min, $0.20-0.60.
    """
    ai_dir = STORAGE_ROOT / "listings" / str(listing_id) / "ai_ready"
    if ai_dir.is_dir() and any(ai_dir.glob("img_*.jpg")):
        return  # cache hit

    print(f"[pdf_maker] AI bildes vēl nav listingam {listing_id}, "
          f"palaižu image_pipeline...")
    if DATABASE_URL is None:
        raise SystemExit("DATABASE_URL nav konfigurēts")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        res = image_pipeline.process_listing(conn, listing_id, force=False,
                                              dry_run=False)
        print(f"[pdf_maker] image_pipeline: {res.get('status')} "
              f"({res.get('processed', 0)} bildes apstrādātas)")

    # Klasifikācija — fasāde/plāns/interjers tagging
    try:
        image_classify.ensure_classified(STORAGE_ROOT, listing_id, None,
                                          force=False, only_images=None)
    except Exception as e:
        print(f"[pdf_maker] image_classify brīdinājums: {e}")


def render_pdf_bulk(listing_ids: list[int]) -> bytes:
    """Apvieno N listingu PDF lapas vienā brošūrā (klientam dot kā saliktu
    piedāvājumu).

    Strādā secīgi — katram listingam:
      1. `_ensure_ai_ready` (ss source: auto-image_pipeline)
      2. `render_pdf` (WeasyPrint) → atsevišķs PDF
      3. pypdf saliek lapas vienā gala dokumentā

    Ilgums skaidri liels: ~5-10 min × N (pirmajā reizē uz ss-source).
    Otrajā reizē — cache hit ai_ready/ → ātri.
    """
    from pypdf import PdfReader, PdfWriter  # lazy import (Railway)

    if not listing_ids:
        raise SystemExit("listing_ids tukšs saraksts")

    writer = PdfWriter()
    for idx, lid in enumerate(listing_ids, start=1):
        print(f"[pdf_maker.bulk] {idx}/{len(listing_ids)} listing#{lid}")
        try:
            pdf_bytes = render_pdf(lid)
        except Exception as e:
            print(f"[pdf_maker.bulk] listing#{lid} izlaists: {e}")
            continue
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            writer.add_page(page)

    if len(writer.pages) == 0:
        raise SystemExit("Nevienam listingam neizdevās izveidot PDF")

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def saved_pdf_path(listing_id: int) -> Path:
    """Saglabātā vienas-listinga PDF ceļš uz volume.

    Panelis to var lejupielādēt uzreiz (bez atkārtota rendera). Jauns render
    pārraksta veco failu (Raimonds: jauns → vecais izdzēšas)."""
    return STORAGE_ROOT / "listings" / str(listing_id) / "offer.pdf"


def render_pdf(listing_id: int) -> bytes:
    """Galvenā API — atgriež PDF baitus.

    PIRMS render — VIENMĒR nodrošina AI bildes (image_pipeline + classify), ja
    ai_ready/ trūkst. Tā PDF nekad nedabū tukšu galeriju.

    PĒC render — saglabā kopiju uz volume (`listings/<id>/offer.pdf`), pārrakstot
    veco, lai panelis to var lejupielādēt uzreiz.
    """
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL nav .env failā.")
    _ensure_ai_ready(listing_id)
    from weasyprint import HTML  # imports šeit — ja libs trūkst, skaidra kļūda
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        listing, bp = _fetch(conn, listing_id)
    html_doc, base_url = build_html(listing, bp, listing_id)
    pdf_bytes = HTML(string=html_doc, base_url=base_url).write_pdf()
    # Saglabā/pārraksta saglabāto kopiju (nekritiski — ja neizdodas, PDF tāpat
    # atgriežas pārlūkam). write_bytes pārraksta esošo → vecais izdzēšas.
    try:
        dest = saved_pdf_path(listing_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pdf_bytes)
    except Exception as e:
        print(f"[pdf_maker] PDF saglabāšana neizdevās (nekritiski): {e}")
    return pdf_bytes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--listing", type=int, required=True)
    ap.add_argument("--out", help="Izvades PDF ceļš (default: ./listing_<id>.pdf)")
    args = ap.parse_args()

    print(f"Ģenerēju PDF listing {args.listing}...")
    pdf = render_pdf(args.listing)
    out = Path(args.out) if args.out else Path(f"listing_{args.listing}.pdf")
    out.write_bytes(pdf)
    print(f"GATAVS: {out}  ({len(pdf) // 1024} KB)")


if __name__ == "__main__":
    main()
