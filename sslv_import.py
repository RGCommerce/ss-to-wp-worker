"""sslv_import.py — «Caur linku» imports: viens ss.lv sludinājums → listings DB.

Raimonda plūsma (2026-08-23): iedod ss.lv linku (sludinājums, kas NAV mūsu DB —
piem. citu cilvēku telpas, ko mums vajag ievietot mājaslapā) →
  1. noskrāpē detaļu lapu (adrese, cena, platība, stāvs, bilžu URL, teksts)
  2. AI analīze TIEŠI no ss.lv teksta + bilžu URL (tas pats analyze_with_openai,
     ko lieto agent_ai_poller) — aizpilda Space_group, stāvokli, fīčas, aprakstu
  3. INSERT properties.listings ar Debug_status='ok' JAU NO SĀKUMA →
     test-runner (kas ņem tikai tukša statusa rindas) šo NEKAD neredz → nevar
     nedz pāranalizēt, nedz izdzēst (agent_detected u.c. te būtu nepareizi —
     imports ir APZINĀTS)
  4. bildes lejupielādē esošais image_download_poller ("JPG bildes" aizpildīts +
     images_downloaded_at IS NULL) uz raw/ ~30 sekunžu laikā
  5. UZ WP NEIET AUTOMĀTISKI — kontaktus (savu numuru) aģents ieraksta pats
     listinga redaktorā un tad «Export to WP» (publish brīdī Seedream pārtaisa
     bildes kā parasti ss.lv listingiem: raw → ai_ready)

Drošības īpašības:
  - phone_numbers = NULL + lock 'phone_numbers' → auto_publish_poller (prasa
    verificētu numuru) NEKAD nepublicē pats
  - dedup: ja listings jau satur rindu ar šo pašu ss.lv msg slug → atgriež to,
    dublikātu neveido
  - ja ss.lv lapa vairs nav pieejama → fallback uz scrape_inbox arhīva rindu
    (bez bildēm, bez AI) ar brīdinājumu
"""
from __future__ import annotations

import re
import sys
import uuid as uuid_mod
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import psycopg
import requests
from bs4 import BeautifulSoup
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent))
import agent_publish  # noqa: E402  (_get_or_create_bp, _ensure_street_suffix)

_EET = zoneinfo.ZoneInfo("EET")

# URL kategorijas segments → (listing_type, group) — tas pats kartējums, ko
# lieto sslv-scraper (listing_types.json), lai downstream (zeme, dedup, match)
# importētos atpazīst tāpat kā skrāpētos.
_CATEGORY_MAP = {
    "offices": ("offices", "biroji"),
    "hangars": ("hangars", "noliktavas un ražošanas telpas"),
    "storehouses-and-storages": ("storage", "noliktavas un ražošanas telpas"),
    "production-facilities": ("production_facilities", "noliktavas un ražošanas telpas"),
    "premises-for-service-centers": ("service_centers", "noliktavas un ražošanas telpas"),
    "garages": ("garages", "noliktavas un ražošanas telpas"),
    "saloons": ("salons", "tirdzniecības telpas"),
    "shops": ("stores", "tirdzniecības telpas"),
    "restaurants-cafe-dining-halls": ("restaurants", "tirdzniecības telpas"),
    "playing-halls": ("playing_halls", "tirdzniecības telpas"),
    "training-halls": ("gyms", "tirdzniecības telpas"),
    "plots-and-lands": ("plot", "zeme"),
}

# Pilsētas segments URL → nosaukums (biežākie; pārējiem title-case fallback)
_CITY_SEGMENT = {
    "riga": "Rīga",
    "jurmala": "Jūrmala",
    "riga-region": "Rīgas rajons",
    "liepaja": "Liepāja",
    "daugavpils": "Daugavpils",
    "jelgava": "Jelgava",
    "ogre-and-reg": "Ogre un raj.",
}


class SslvImportError(ValueError):
    """Lietotājam rādāma importa kļūda (LV teksts)."""


def _fetch_html(url: str, headers: dict, timeout: int) -> str:
    """ss.lv lapas ielāde. (ai_text_helpers.fetch_html tur ir miris kods —
    tam modulim trūkst `requests` importa, tāpēc fetch dzīvo šeit.)"""
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _extract_listing_text(html: str) -> str:
    """Sludinājuma teksts AI analīzei (tas pats selektoru saraksts kā
    test-runner; ai_text_helpers versijai trūkst BeautifulSoup importa)."""
    soup = BeautifulSoup(html, "html.parser")
    text_parts: list[str] = []
    for sel in ["#msg_div_msg", ".ads_opt", "#tdo_8", "#tdo_20", "body"]:
        nodes = soup.select(sel)
        if nodes:
            for n in nodes:
                t = n.get_text(" ", strip=True)
                if t and len(t) > 40:
                    text_parts.append(t)
            if text_parts:
                break
    return re.sub(r"\s+", " ", "\n".join(text_parts).strip())


# ---------------------------------------------------------------------------
# URL parsēšana
# ---------------------------------------------------------------------------

def parse_sslv_url(url: str) -> dict:
    """Akceptē visus variantus (https://www.ss.lv/..., ss.lv/..., ss.com, ar
    #photo-N fragmentu). Atgriež {path, canonical_link, fetch_url, slug,
    listing_type, group, city_guess}."""
    raw = (url or "").strip()
    if not raw:
        raise SslvImportError("Tukšs links.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    p = urlparse(raw)
    host = (p.netloc or "").lower()
    if not re.search(r"(^|\.)ss\.(lv|com)$", host):
        raise SslvImportError("Links nav ss.lv sludinājums.")
    path = p.path or ""
    if "/msg/" not in path or not path.endswith(".html"):
        raise SslvImportError(
            "Links nav sludinājuma lapa (gaidīju .../msg/.../xxxxx.html)."
        )
    slug = path.rstrip("/").split("/")[-1]  # piem. "cbpomp.html"
    segments = [s for s in path.split("/") if s]

    listing_type, group = None, None
    for seg in segments:
        if seg in _CATEGORY_MAP:
            listing_type, group = _CATEGORY_MAP[seg]
            break

    # Pilsētas minējums no URL (segments aiz kategorijas), ja lapā nebūs tdo_20
    city_guess = None
    for seg in segments:
        if seg in _CITY_SEGMENT:
            city_guess = _CITY_SEGMENT[seg]
            break

    return {
        "path": path,
        "canonical_link": "ss.lv" + path,   # tas pats formāts kā scrape_inbox
        "fetch_url": f"https://www.ss.lv{path}",
        "slug": slug,
        "listing_type": listing_type,
        "group": group,
        "city_guess": city_guess,
    }


# ---------------------------------------------------------------------------
# Detaļu lapas parsēšana (tdo_* lauki — tāpat kā sslv-scraper DetailPageParser)
# ---------------------------------------------------------------------------

def _get_price_and_type(price_tag: str) -> Optional[tuple[float, str]]:
    try:
        if "mēn." in price_tag:
            price_type = "monthly"
        elif "dienā" in price_tag:
            price_type = "daily"
        elif "ned" in price_tag:
            price_type = "weekly"
        elif "maiņai" in price_tag or "st." in price_tag:
            return None
        else:
            price_type = "regular"
        price_str = price_tag.split("/")[0][:-2].replace(" ", "").replace(",", "")
        return float(price_str), price_type
    except Exception:
        return None


def parse_detail_page(html: str) -> dict:
    """Izvelk strukturētos laukus no ss.lv detaļu lapas HTML."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, Any] = {}

    if not soup.find(id="msg_div_msg"):
        raise SslvImportError("Sludinājuma saturs lapā nav atrasts (dzēsts?).")

    # Cena + tips
    try:
        tag = soup.find(id="tdo_8").text.strip().split("(")[0]
        res = _get_price_and_type(tag)
        if res:
            out["price"], out["price_type"] = res
    except Exception:
        pass

    # Platība (ha → m² zemei)
    try:
        raw = soup.find(id="tdo_3").text.strip()
        if "ha" in raw.lower():
            num = re.sub(r"[^0-9.]", "", raw.lower().split("ha")[0].replace(",", "."))
            out["area_m2"] = int(round(float(num) * 10000)) if num else None
        else:
            out["area_m2"] = int(raw.split(".")[0].split(",")[0].split(" ")[0])
    except Exception:
        pass

    # Stāvs
    try:
        out["floor"] = int(soup.find(id="tdo_4").text.strip().split("/")[0])
    except Exception:
        pass

    # Pilsēta / rajons / iela
    try:
        out["city"] = soup.find(id="tdo_20").find_all("b")[0].text.strip() or None
    except Exception:
        pass
    try:
        el = soup.find(id="tdo_856")
        try:
            out["district"] = el.find_all("b")[0].text.strip() or None
        except IndexError:
            out["district"] = el.text.strip() or None
    except Exception:
        pass
    try:
        out["street"] = soup.find(id="tdo_11").find_all("b")[0].text.strip() or None
    except Exception:
        pass

    # Zemes pielietojums
    try:
        el = soup.find(id="tdo_228")
        if el is not None:
            out["land_use"] = el.get_text(strip=True) or None
    except Exception:
        pass

    # Publicēšanas datums
    try:
        td = soup.find("td", class_="msg_footer", string=lambda t: t and "Datums" in t)
        raw = td.text.split("Datums: ")[1].strip()
        date_part, time_part = raw.split(" ")
        day, month, year = (int(x) for x in date_part.split("."))
        hour, minute = (int(x) for x in time_part.split(":"))
        out["date_posted"] = datetime(year, month, day, hour, minute, tzinfo=_EET)
    except Exception:
        pass

    return out


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _find_existing(conn, slug: str) -> Optional[dict]:
    """Vai listings jau satur šo ss.lv sludinājumu (pēc unikālā msg slug)?"""
    row = conn.execute(
        """SELECT id, street, city, "Debug_status" AS status, on_website
           FROM properties.listings
           WHERE link IS NOT NULL AND link LIKE %s
           ORDER BY id LIMIT 1""",
        (f"%/{slug}",),
    ).fetchone()
    return dict(row) if row else None


def _inbox_fallback(conn, canonical_link: str, slug: str) -> Optional[dict]:
    """ss.lv lapa nav pieejama → jaunākā scrape_inbox rinda ar šo linku."""
    row = conn.execute(
        """SELECT street, city, district, price, price_type, area_m2, floor,
                  listing_type, "group", date_posted, land_use
           FROM properties.scrape_inbox
           WHERE link = %s OR link LIKE %s
           ORDER BY id DESC LIMIT 1""",
        (canonical_link, f"%/{slug}"),
    ).fetchone()
    return dict(row) if row else None


def _format_jpg_field(urls: list[str]) -> str:
    return " | ".join(f"{i}. {u}" for i, u in enumerate(urls, start=1))


# ---------------------------------------------------------------------------
# Galvenais imports
# ---------------------------------------------------------------------------

def import_from_url(url: str, wp_user_id: int = 0) -> dict:
    info = parse_sslv_url(url)
    warnings: list[str] = []

    with psycopg.connect(agent_publish.DATABASE_URL, row_factory=dict_row) as conn:
        existing = _find_existing(conn, info["slug"])
        if existing:
            return {
                "ok": True,
                "already_exists": True,
                "listing_id": int(existing["id"]),
                "street": existing.get("street"),
                "city": existing.get("city"),
                "on_website": bool(existing.get("on_website")),
                "warnings": ["Šis ss.lv sludinājums jau IR datubāzē."],
            }

    # ── 1) Lapas ielāde + parsēšana ────────────────────────────────────────
    import ai_text_helpers as helpers  # lazy — prasa OPENAI_API_KEY moduļa ielādē

    html = None
    fields: dict[str, Any] = {}
    gallery: list[str] = []
    text = ""
    try:
        html = _fetch_html(info["fetch_url"], helpers.HEADERS, helpers.REQUEST_TIMEOUT)
        fields = parse_detail_page(html)
        text = _extract_listing_text(html)
        gallery = helpers.extract_gallery_urls(html)
    except SslvImportError:
        html = None
    except Exception as e:
        warnings.append(f"ss.lv lapu neizdevās ielādēt ({type(e).__name__}).")
        html = None

    if html is None:
        # Fallback — arhīva dati no scrape_inbox (bez bildēm, bez AI)
        with psycopg.connect(agent_publish.DATABASE_URL, row_factory=dict_row) as conn:
            inbox = _inbox_fallback(conn, info["canonical_link"], info["slug"])
        if not inbox:
            raise SslvImportError(
                "Sludinājums ss.lv vairs nav pieejams un nav arī mūsu arhīvā "
                "(scrape_inbox) — nav no kā importēt."
            )
        warnings.append(
            "ss.lv lapa vairs nav pieejama — imports no arhīva datiem, "
            "BEZ bildēm un BEZ AI analīzes."
        )
        fields = {
            "price": inbox.get("price"),
            "price_type": inbox.get("price_type"),
            "area_m2": inbox.get("area_m2"),
            "floor": inbox.get("floor"),
            "street": inbox.get("street"),
            "city": inbox.get("city"),
            "district": inbox.get("district"),
            "date_posted": inbox.get("date_posted"),
            "land_use": inbox.get("land_use"),
        }
        if inbox.get("listing_type"):
            info["listing_type"] = inbox["listing_type"]
        if inbox.get("group"):
            info["group"] = inbox["group"]

    street = (fields.get("street") or "").strip()
    if not street:
        raise SslvImportError(
            "Sludinājumā nav atrodama adrese (iela) — bez tās listingu izveidot nevar."
        )
    city = (fields.get("city") or "").strip() or info.get("city_guess") or "Rīga"
    district = (fields.get("district") or "").strip() or None
    if district:
        district = district.title() if district.islower() else district

    is_land = (info.get("group") == "zeme")

    # ── 2) AI analīze (ss.lv teksts + galerijas URL — bez lejupielādes) ────
    ai_fields: dict[str, Any] = {}
    ai_ok = False
    ai_note = None
    if html is not None:
        try:
            if is_land:
                import land_ai
                result = land_ai.analyze_land(
                    helpers.client, helpers.MODEL, info["fetch_url"], text, gallery
                )
                row_stub = {
                    "price_type": fields.get("price_type"),
                    "area_m2": fields.get("area_m2"),
                    "Zemes_gabals_m2": fields.get("area_m2"),
                    "land_use": fields.get("land_use"),
                }
                result["land_description"] = land_ai.build_land_description(row_stub, result)
                allowed = list(land_ai.LAND_OUTPUT_FIELDS)
            elif gallery:
                result = helpers.analyze_with_openai(info["fetch_url"], text, gallery)
                from agent_ai_poller import AI_OUTPUT_FIELDS
                allowed = list(AI_OUTPUT_FIELDS)
            else:
                result = None
                allowed = []
                warnings.append("Galerijas bildes lapā netika atrastas — AI analīze izlaista.")

            if result:
                orig_status = str(result.get("Debug_status") or "").strip()
                if orig_status and orig_status != "ok":
                    # Apzināts imports — statuss vienmēr 'ok', bet piezīmi paturam
                    ai_note = f"AI atzīme importējot: {orig_status}"
                for k in allowed:
                    v = result.get(k)
                    if v in (None, "", "unknown"):
                        continue
                    ai_fields[k] = v
                ai_ok = True
        except Exception as e:
            warnings.append(
                f"AI analīze neizdevās ({type(e).__name__}: {str(e)[:150]}) — "
                "lauki jāaizpilda pašam listinga redaktorā."
            )

    # ── 3) Building profile + listings INSERT ──────────────────────────────
    with psycopg.connect(agent_publish.DATABASE_URL, row_factory=dict_row) as conn:
        bp_id = agent_publish._get_or_create_bp(
            conn, {"street": street, "city": city, "district": district}, wp_user_id
        )

        price = fields.get("price")
        area = fields.get("area_m2")
        price_per_m2 = None
        try:
            if price and area and float(area) > 0:
                price_per_m2 = round(float(price) / float(area), 2)
        except Exception:
            pass

        note_bits = ["Imports caur linku (agent_link)"]
        if ai_note:
            note_bits.append(ai_note)
        debug_note = "; ".join(note_bits)[:500]

        cols: dict[str, Any] = {
            "building_profile_id": bp_id,
            "street": agent_publish._ensure_street_suffix(street) or street,
            "city": city,
            "district": district,
            "source": "agent_link",
            "agent_user_id": wp_user_id or None,
            # Kontaktus aģents ieraksta PATS (imports = citu cilvēku sludinājums,
            # viņu numuru NEpārņemam). Lock, lai backfill/AI to neaizpilda.
            "agent_locked_fields": ["phone_numbers"],
            "Debug_status": "ok",
            "Debug_note": debug_note,
            "link": info["canonical_link"],
            "uuid": str(uuid_mod.uuid4()),
            "price": price,
            "price_type": fields.get("price_type"),
            "price_per_m2": price_per_m2,
            "area_m2": str(area) if area is not None else None,
            "floor": str(fields["floor"]) if fields.get("floor") is not None else None,
            "date_posted": fields.get("date_posted"),
        }
        if info.get("listing_type"):
            cols["listing_type"] = info["listing_type"]
        if info.get("group"):
            cols["group"] = info["group"]
        if gallery:
            cols["JPG bildes"] = _format_jpg_field(gallery)
        if is_land:
            cols["Space_group"] = "Zeme"
            if area is not None:
                cols["Zemes_gabals_m2"] = str(area)
            if fields.get("land_use"):
                cols["land_use"] = fields["land_use"]

        # AI lauki pa virsu (nepārraksta jau saliktos pamatlaukus)
        for k, v in ai_fields.items():
            if k not in cols:
                cols[k] = v

        # Drop None vērtības — ļauj DB defaults
        cols = {k: v for k, v in cols.items() if v is not None}

        col_list = ", ".join(f'"{k}"' for k in cols)
        val_list = ", ".join(["%s"] * len(cols))
        cur = conn.execute(
            f"INSERT INTO properties.listings ({col_list}) VALUES ({val_list}) RETURNING id",
            tuple(cols.values()),
        )
        listing_id = int(cur.fetchone()["id"])
        conn.commit()

    return {
        "ok": True,
        "already_exists": False,
        "listing_id": listing_id,
        "building_profile_id": bp_id,
        "street": cols.get("street"),
        "city": city,
        "district": district,
        "price": price,
        "price_type": cols.get("price_type"),
        "area_m2": cols.get("area_m2"),
        "floor": cols.get("floor"),
        "space_group": cols.get("Space_group"),
        "image_count": len(gallery),
        "ai_ok": ai_ok,
        "warnings": warnings,
    }
