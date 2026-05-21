"""pdf_maker.py — RGC sludinājuma PDF brošūra (1 īpašums = 1 brošūra).

Ģenerē klientam sūtāmu PDF no listing datiem + AI-apstrādātajām bildēm.
HTML+CSS → PDF caur WeasyPrint. RGC brand (navy/red/cream, Playfair/Open Sans).

Struktūra:
  1. Titullapa — hero bilde (fasāde) + adrese + cena + m² + tips
  2. Galvenie fakti + apraksts (tas pats 3-daļu teksts kā WP, render_body)
  3+ Bilžu galerija — AI-apstrādātās bildes (fasāde pirmā, plāns izlaists)
  Pēdējā — kontakti (agents + RGC)

Lietošana:
  python crm/pdf_maker.py --listing 525
  python crm/pdf_maker.py --listing 525 --out C:/tmp/525.pdf

Galvenā lietošana = library (ss-to-wp-worker POST /pdf/{id}):
  from pdf_maker import render_pdf
  pdf_bytes = render_pdf(525)

Vide (crm/.env): DATABASE_URL, STORAGE_ROOT.
Atkarība: weasyprint (Railway vajag system libs — pango/cairo, sk. nixpacks).
"""
from __future__ import annotations

import argparse
import html as _html
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
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

# RGC agentu kontakti (memory project_melna_kaste_agent_assignment: 4238=Ieva,
# 4456=Raimonds). Telefonus/e-pastus Raimonds precizē — viegli rediģēt šeit.
AGENT_IEVA = 4238
AGENT_RAIMONDS = 4456
IEVA_GROUPS = {"Birojs", "Medicīna", "Tirdzniecība", "Studija"}
_SALE_PT = {"regular", "parastā", "parasta", "sale", "sell", "buy"}
AGENT_CONTACTS = {
    AGENT_IEVA:     {"name": "Ieva",     "phone": "", "email": ""},
    AGENT_RAIMONDS: {"name": "Raimonds", "phone": "", "email": "raimonds@rgcommerce.lv"},
}

# Houzez Space_group → lietvārds virsrakstam
_VEIDS = {
    "Birojs": "Biroja telpas", "Tirdzniecība": "Tirdzniecības telpas",
    "Noliktava": "Noliktavas telpas", "Ražošana": "Ražošanas telpas",
    "Medicīna": "Medicīnas telpas", "Restorans/Cafe": "Ēdināšanas telpas",
    "Studija": "Studijas telpas", "Autoserviss": "Autoservisa telpas",
}


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


# ---------------------------------------------------------------------------
# Bildes — fasāde pirmā, plāns izlaists (manifest)
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


# ---------------------------------------------------------------------------
# HTML būve
# ---------------------------------------------------------------------------

_CSS = """
@page { size: A4; margin: 0; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Open Sans', 'DejaVu Sans', sans-serif;
       color: #1a2638; font-size: 11pt; }
.page { width: 210mm; min-height: 297mm; position: relative;
        page-break-after: always; }
.page:last-child { page-break-after: auto; }

/* ---- Titullapa ---- */
.cover-hero { width: 210mm; height: 170mm; object-fit: cover; display: block; }
.cover-band { background: #1a2638; color: #f7f3ed; padding: 14mm 16mm 12mm; }
.cover-kicker { color: #c8202a; font-size: 11pt; letter-spacing: 2px;
                text-transform: uppercase; font-weight: 700; }
.cover-title { font-family: 'Playfair Display', 'DejaVu Serif', serif;
               font-size: 30pt; font-weight: 700; margin: 4mm 0 6mm; }
.cover-facts { display: flex; gap: 10mm; font-size: 13pt; }
.cover-facts .v { font-weight: 700; font-size: 16pt; }
.cover-facts .l { color: #b8c0cc; font-size: 9pt; text-transform: uppercase;
                  letter-spacing: 1px; }
.cover-price { font-family: 'Playfair Display', serif; font-size: 22pt;
               font-weight: 700; color: #c8202a; margin-top: 8mm; }

/* ---- Iekšlapas ---- */
.inner { padding: 16mm; }
.h2 { font-family: 'Playfair Display', 'DejaVu Serif', serif;
      font-size: 18pt; font-weight: 700; color: #1a2638;
      border-bottom: 2pt solid #c8202a; padding-bottom: 2mm;
      margin-bottom: 6mm; }
.facts-grid { display: flex; flex-wrap: wrap; gap: 4mm; margin-bottom: 9mm; }
.fact { width: 53mm; background: #f7f3ed; border: 1pt solid #e8e2d8;
        border-radius: 3pt; padding: 4mm 5mm; }
.fact .l { color: #8a93a0; font-size: 8pt; text-transform: uppercase;
           letter-spacing: 1px; }
.fact .v { font-weight: 700; font-size: 13pt; margin-top: 1mm; }
.desc p { margin-bottom: 3mm; line-height: 1.5; }
.desc strong { color: #1a2638; }

/* ---- Galerija ---- */
.gallery { display: flex; flex-wrap: wrap; gap: 4mm; }
.gallery img { width: 86mm; height: 60mm; object-fit: cover;
               border-radius: 3pt; }

/* ---- Kontakti ---- */
.contact { background: #1a2638; color: #f7f3ed; padding: 16mm;
           position: absolute; bottom: 0; left: 0; right: 0; }
.contact .h { font-family: 'Playfair Display', serif; font-size: 20pt;
              font-weight: 700; margin-bottom: 5mm; }
.contact .row { font-size: 12pt; margin-bottom: 2mm; }
.contact .brand { color: #c8202a; font-weight: 700; margin-top: 6mm;
                  font-size: 13pt; letter-spacing: 1px; }
"""


def _esc(s) -> str:
    return _html.escape(str(s or ""))


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
    rooms = _num(listing.get("Cik_telpas"))
    land = _num(listing.get("Zemes_gabals_m2"))

    imgs = _ordered_images(listing_id)
    hero = imgs[0].as_uri() if imgs else ""
    gallery = imgs[1:] if len(imgs) > 1 else []

    # Apraksts — tas pats render_body teksts (HTML <p>/<strong>/<br>)
    tdata = dict(listing)
    for loc in ("city", "district", "street"):
        if bp.get(loc):
            tdata[loc] = bp[loc]
    desc_html = render_body(sg if sg in _VEIDS else "Birojs", tdata)

    # Agents
    is_sale_agent = str(listing.get("price_type") or "").lower() in _SALE_PT
    agent_id = (AGENT_RAIMONDS if is_sale_agent
                else (AGENT_IEVA if sg in IEVA_GROUPS else AGENT_RAIMONDS))
    agent = AGENT_CONTACTS.get(agent_id, AGENT_CONTACTS[AGENT_RAIMONDS])

    price_lbl = "Cena" if sale else "Noma mēnesī"
    price_str = (f"{_money(price)} EUR" if price else "Cena pēc pieprasījuma")
    if price and not sale:
        price_str += " / mēn."

    # ---- Fakti ----
    facts = []
    if area:
        facts.append(("Platība", f"{_money(area)} m²"))
    if ppm2:
        facts.append(("Cena par m²", f"{ppm2} EUR/m²"))
    if rooms:
        facts.append(("Telpu skaits", rooms))
    if land:
        facts.append(("Zemes platība", f"{_money(land)} m²"))
    loc_v = ", ".join(p for p in (district, city) if p)
    if loc_v:
        facts.append(("Atrašanās vieta", loc_v))
    facts.append(("Tips", veids))
    facts_html = "".join(
        f'<div class="fact"><div class="l">{_esc(l)}</div>'
        f'<div class="v">{_esc(v)}</div></div>' for l, v in facts)

    gallery_html = "".join(
        f'<img src="{p.as_uri()}">' for p in gallery)

    contact_lines = [f'<div class="row">Aģents: {_esc(agent["name"])}</div>']
    if agent.get("phone"):
        contact_lines.append(f'<div class="row">Tālr.: {_esc(agent["phone"])}</div>')
    if agent.get("email"):
        contact_lines.append(f'<div class="row">E-pasts: {_esc(agent["email"])}</div>')

    html_doc = f"""<!DOCTYPE html>
<html lang="lv"><head><meta charset="utf-8">
<style>@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Open+Sans:wght@400;600;700&display=swap');
{_CSS}</style></head><body>

<div class="page">
  {'<img class="cover-hero" src="' + hero + '">' if hero else '<div class="cover-hero" style="background:#243349"></div>'}
  <div class="cover-band">
    <div class="cover-kicker">{_esc(veids)}</div>
    <div class="cover-title">{_esc(addr)}</div>
    <div class="cover-facts">
      <div><div class="v">{_esc(_money(area) + ' m²' if area else '—')}</div><div class="l">Platība</div></div>
      <div><div class="v">{_esc(loc_v or city or '—')}</div><div class="l">Lokācija</div></div>
    </div>
    <div class="cover-price">{_esc(price_lbl)}: {_esc(price_str)}</div>
  </div>
</div>

<div class="page"><div class="inner">
  <div class="h2">Galvenie fakti</div>
  <div class="facts-grid">{facts_html}</div>
  <div class="h2">Apraksts</div>
  <div class="desc">{desc_html}</div>
</div></div>

{('<div class="page"><div class="inner"><div class="h2">Galerija</div>'
  '<div class="gallery">' + gallery_html + '</div></div>'
  '<div class="contact"><div class="h">Ieinteresēja šis īpašums?</div>'
  + ''.join(contact_lines) +
  '<div class="brand">RG COMMERCE &nbsp;|&nbsp; rgcommerce.lv</div></div></div>')
 if gallery_html else
 ('<div class="page"><div class="contact" style="position:static;min-height:297mm">'
  '<div class="h">Ieinteresēja šis īpašums?</div>' + ''.join(contact_lines) +
  '<div class="brand">RG COMMERCE &nbsp;|&nbsp; rgcommerce.lv</div></div></div>')}

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
