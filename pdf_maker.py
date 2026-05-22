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
from wp_templates import render_body, _floor as _clean_floor  # noqa: E402
import image_classify  # noqa: E402

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
        "name": "Raimonds Grīnbergs", "phone": "", "email": "raimonds@rgcommerce.lv",
        "photo": "agent_raimonds.jpg",
    },
}

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
    area = _num(L.get("area_m2"))
    add("Platība", f"{_money(area)} m²" if area else None)
    ppm2 = _num(L.get("price_per_m2"))
    add("Cena par m²", f"{ppm2} EUR/m²" if ppm2 else None)
    land = _num(L.get("Zemes_gabals_m2"))
    add("Zemes platība", f"{_money(land)} m²" if land else None)
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
    add("Pielietojuma potenciāls", _clean(L.get("Potential_space_group")))
    add("Investīciju stratēģija", _clean(L.get("Investiciju_strategija")))
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
.cf-col { display: inline-block; vertical-align: top; }
.cf-col + .cf-col { margin-left: 22mm; }
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
.section { margin-bottom: 10mm; }

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

/* ---- Kontaktu lapa ---- */
.contact { page: contact; page-break-before: always;
           background: #1a2638; color: #f7f3ed; height: 297mm;
           display: flex; flex-direction: column;
           justify-content: center; align-items: center;
           text-align: center; padding: 20mm; }
.agent-photo { width: 50mm; height: 64mm; object-fit: cover;
               border-radius: 4pt; border: 2.5pt solid #c8202a; }
.contact-h { font-family: 'Playfair Display', 'DejaVu Serif', serif;
             font-size: 24pt; font-weight: 700; margin: 12mm 0 7mm; }
.agent-name { font-size: 15pt; font-weight: 700; margin-bottom: 4mm; }
.contact-row { font-size: 12pt; color: #d4dae2; margin-bottom: 2mm; }
.contact-brand { color: #c8202a; font-weight: 700; font-size: 13pt;
                 letter-spacing: 2px; margin-top: 16mm; }
.contact-sep { width: 28mm; height: 2pt; background: #c8202a;
               margin: 16mm auto 0; }
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
    addr = _title(listing, bp)
    area = _num(listing.get("area_m2"))
    price = _num(listing.get("price"))
    ppm2 = _num(listing.get("price_per_m2"))
    if not ppm2 and price and area:
        try:
            ppm2 = str(round(float(price) / float(area), 2))
        except (ValueError, ZeroDivisionError):
            ppm2 = None
    city = (bp.get("city") or listing.get("city") or "").strip()
    district = (bp.get("district") or listing.get("district") or "").strip()
    loc_v = ", ".join(p for p in (district, city) if p)

    # ---- Bildes (downscale) ----
    imgs = _ordered_images(listing_id)
    hero = _data_uri(imgs[0], 1900, 84) if imgs else ""
    gallery_imgs = imgs[1:] if len(imgs) > 1 else []

    # ---- Apraksts — tas pats render_body teksts ----
    tdata = dict(listing)
    for loc in ("city", "district", "street"):
        if bp.get(loc):
            tdata[loc] = bp[loc]
    desc_html = render_body(sg if sg in _VEIDS else "Birojs", tdata)

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
    facts = _facts(listing, bp)
    facts.append(("Tips", veids))
    if loc_v:
        facts.append(("Atrašanās vieta", loc_v))
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

    # ---- Kontaktu lapa ----
    photo_path = ASSETS_DIR / agent.get("photo", "")
    photo_uri = _data_uri(photo_path, 620, 86) if photo_path.is_file() else ""
    contact_rows = ""
    if agent.get("phone"):
        contact_rows += f'<div class="contact-row">Tālrunis: {_esc(agent["phone"])}</div>'
    if agent.get("email"):
        contact_rows += f'<div class="contact-row">E-pasts: {_esc(agent["email"])}</div>'

    hero_html = (f'<img class="cover-hero" src="{hero}">' if hero
                 else '<div class="cover-hero" style="background:#243349"></div>')
    photo_html = (f'<img class="agent-photo" src="{photo_uri}">' if photo_uri
                  else "")

    html_doc = f"""<!DOCTYPE html>
<html lang="lv"><head><meta charset="utf-8">
<style>@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Open+Sans:wght@400;600;700&display=swap');
{_CSS}</style></head><body>

<div class="cover">
  {hero_html}
  <div class="cover-band">
    <div class="cover-kicker">{_esc(veids)}</div>
    <div class="cover-title">{_esc(addr)}</div>
    <div>
      <div class="cf-col">
        <div class="cf-v">{_esc(_money(area) + ' m²' if area else '—')}</div>
        <div class="cf-l">Platība</div>
      </div>
      <div class="cf-col">
        <div class="cf-v">{_esc(loc_v or city or '—')}</div>
        <div class="cf-l">Lokācija</div>
      </div>
    </div>
    <div class="cover-price">{_esc(price_lbl)}: {_esc(price_str)}</div>
    {f'<div class="cover-ppm2">{_esc(ppm2)} EUR/m²</div>' if ppm2 else ''}
  </div>
</div>

<div class="section">
  <div class="h2">Galvenie fakti</div>
  <table class="facts">{facts_rows}</table>
</div>

<div class="section">
  <div class="h2">Apraksts</div>
  <div class="desc">{desc_html}</div>
</div>

{gallery_section}

<div class="contact">
  {photo_html}
  <div class="contact-h">Ieinteresēja šis īpašums?</div>
  <div class="agent-name">{_esc(agent["name"])}</div>
  {contact_rows}
  <div class="contact-sep"></div>
  <div class="contact-brand">RG COMMERCE &nbsp;|&nbsp; rgcommerce.lv</div>
</div>

</body></html>"""
    return html_doc, str(STORAGE_ROOT)


# ---------------------------------------------------------------------------
# PDF render
# ---------------------------------------------------------------------------

def render_pdf(listing_id: int) -> bytes:
    """Galvenā API — atgriež PDF baitus."""
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL nav .env failā.")
    from weasyprint import HTML  # imports šeit — ja libs trūkst, skaidra kļūda
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        listing, bp = _fetch(conn, listing_id)
    html_doc, base_url = build_html(listing, bp, listing_id)
    return HTML(string=html_doc, base_url=base_url).write_pdf()


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
