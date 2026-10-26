"""Partnera MĀJASLAPAS automātiska nolasīšana (Raimonds 2026-10-26: «spirebaltic.lv/office-and-retail — tur vienmēr
ir aktuālās telpas; vajag saprast, kas mums ir, kas iznomāts, un atjaunot»).

Vispārīgs (bez koda katrai lapai): saraksta lapa → redzamais teksts (nospiedumam un AI) + telpu DETAĻU lapas
(tā paša domēna saites, kuru apkārtnē HTML ir platība «sqm / m² / m2») → katrai teksts + bildes (lielākais izmērs
katram failam, bez logo/ikonām) + PDF plāni. Rezultāts = ieplūdes body teksts + `web_links.json` (L-saites ar
bilžu URL), ko partner_intake.build_ai_input pievieno saitēm, bet link_media.fetch_link lejupielādē.

AI vēlāk nolasa: adrese, platība, stāvs, cena (€/m² «++» = plus apsaimniekošana/komunālie), apraksts → telpas;
reconcile pats parāda, kas sakrīt ar mūsējiem, kas jauns un kas mūsu DB vairs nav partnera lapā (iznomāts).
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("partner_web")

MAX_DETAIL_PAGES = 60
MAX_IMAGES_PER_PAGE = 25
AREA_RE = re.compile(r"\d{1,5}(?:[.,]\d{1,2})?\s*(?:sqm|sq\.?\s*m|m²|m2|kv\.?\s*m|kvm)\b", re.I)
IMG_RE = re.compile(r'(?:data-src|data-lazy|data-original|src|href)\s*=\s*["\']([^"\']+\.(?:jpe?g|png|webp)(?:\?[^"\']*)?)["\']', re.I)
PDF_RE = re.compile(r'href\s*=\s*["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', re.I)
SKIP_PATH = re.compile(r"(logo|icon|favicon|sprite|placeholder|avatar|flag|/themes/|/plugins/|emoji)", re.I)
NAV_WORDS = ("contact", "kontakt", "about", "company", "par-mums", "privacy", "cookie", "login", "cart", "wp-admin",
             "maintenance", "residential", "dzivok", "land", "zeme", "#", "mailto:", "tel:", "javascript:")


def _get(url: str, timeout: int = 30) -> str:
    import link_media
    return link_media._get(url, timeout=timeout).decode("utf-8", "ignore")


def visible_text(raw: str) -> str:
    t = re.sub(r"<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</(p|div|li|tr|h\d|section|article)>", "\n", t, flags=re.I)
    t = html_mod.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t)
    return t.strip()


def _norm_for_fp(text: str) -> str:
    """Nospiedumam: bez mainīgajiem skaitītājiem («Saved ( 0 )»), bez lielā/mazā burta un atstarpēm."""
    t = re.sub(r"saved\s*\(\s*\d+\s*\)", "", text, flags=re.I)
    return re.sub(r"\s+", " ", t).strip().lower()


def _same_site(u: str, base: str) -> bool:
    try:
        a, b = urlparse(u).netloc.lower().replace("www.", ""), urlparse(base).netloc.lower().replace("www.", "")
        return a == b
    except Exception:
        return False


def detail_links(raw: str, base_url: str) -> list[dict]:
    """Saraksta lapas saites uz telpu lapām: tā paša domēna URL, kura HTML apkārtnē (±700 zīmes) ir platība
    un kas nav navigācija/valoda/pati lapa. Atgriež [{url, row}] ar kartiņas tekstu (adrese/nosaukums/cena/platība)."""
    base_path = urlparse(base_url).path.rstrip("/")
    seen: dict[str, str] = {}
    for m in re.finditer(r"""(?:href|location\.href)\s*=\s*['"]([^'"]+)['"]""", raw):
        u = html_mod.unescape(m.group(1)).strip()
        if not u or any(w in u.lower() for w in NAV_WORDS):
            continue
        u = urljoin(base_url, u).split("#")[0].split("?")[0]
        u = u.rstrip("/") + "/"   # «…/b171-office-no-400» un «…/b171-office-no-400/» = viena lapa (Spire: dublis izspieda 11. telpu)
        if not _same_site(u, base_url):
            continue
        p = urlparse(u).path.rstrip("/")
        if not p or p == base_path or p.count("/") < 2:
            continue
        if re.search(r"/(lv|ru|en|de)(/|$)", p) and not re.search(r"/(lv|ru|en|de)/", base_url):
            continue   # citas valodas dublikāti
        ctx = raw[max(0, m.start() - 700): m.end() + 700]
        if not AREA_RE.search(ctx):
            continue
        # nogriež pusēs pārgrieztos tagus («e-mobile" data-lazy=…»), lai rindā paliek tikai redzamais teksts
        ctx = ctx[ctx.find(">") + 1: ctx.rfind("<")] if ">" in ctx and "<" in ctx else ctx
        row = visible_text(ctx)
        row = " ".join(x for x in row.split("\n") if x.strip())[:300]
        if u not in seen:
            seen[u] = row
        if len(seen) >= MAX_DETAIL_PAGES:
            break
    return [{"url": u, "row": r} for u, r in seen.items()]


def _best_images(raw: str, page_url: str) -> list[str]:
    """Bildes: lielākais variants katram failam (WP «-300x200» sīktēli ārā), bez logo/ikonām, tā paša domēna."""
    best: dict[str, tuple[int, str]] = {}
    for m in IMG_RE.finditer(raw):
        u = urljoin(page_url, html_mod.unescape(m.group(1)))
        if not _same_site(u, page_url) and "wp-content" not in u:
            continue
        if SKIP_PATH.search(u):
            continue
        path = urlparse(u).path
        mm = re.search(r"-(\d{2,4})x(\d{2,4})(\.\w+)$", path)
        if mm:
            base = path[: mm.start()] + mm.group(3)
            score = int(mm.group(1)) * int(mm.group(2))
        else:
            base, score = path, 10_000_000   # oriģināls bez izmēra = lielākais
        if base not in best or score > best[base][0]:
            best[base] = (score, u)
    return [u for _, u in list(best.values())[:MAX_IMAGES_PER_PAGE]]


def collect(url: str) -> dict:
    """Saraksta lapa + detaļas. → {list_text, fingerprint, details:[{url,row,text,images,pdfs}]}"""
    raw = _get(url)
    list_text = visible_text(raw)
    fp = hashlib.sha1(_norm_for_fp(list_text).encode("utf-8")).hexdigest()
    out = {"url": url, "list_text": list_text, "fingerprint": fp, "details": []}
    for d in detail_links(raw, url):
        try:
            draw = _get(d["url"])
        except Exception as e:
            logger.info("partner_web: %s nenolasās: %s", d["url"], e)
            continue
        text = visible_text(draw)
        imgs = _best_images(draw, d["url"])
        pdfs = [urljoin(d["url"], html_mod.unescape(x)) for x in PDF_RE.findall(draw)]
        pdfs = [p for p in dict.fromkeys(pdfs) if _same_site(p, d["url"]) or "wp-content" in p][:3]
        out["details"].append({**d, "text": text, "images": imgs, "pdfs": pdfs})
    return out


def body_text(src: dict, col: dict, scanned_at) -> str:
    """Ieplūdes teksts AI: saraksta lapa + katra telpa ar savu L-id (saites rindā = kartiņas teksts)."""
    lines = [
        f"AUTOMĀTISKS SKENĒJUMS — partnera {src['name']} mājaslapa {col['url']} ({scanned_at.strftime('%Y-%m-%d')}).",
        f"Partneris: {src['name']}" + (f"; kontakts: {src.get('contact_name')}" if src.get("contact_name") else "")
        + (f" {src.get('contact_email')}" if src.get("contact_email") else "")
        + (f" {src.get('contact_phone')}" if src.get("contact_phone") else ""),
        "Mājaslapā ir VISAS partnera brīvās telpas (saraksts pilns). Katra TELPAS LAPA zemāk = viena telpa; tās bildes ir "
        "saitē ar to pašu L-numuru (link_ids). Cena «EUR 10.50++ / sqm» = 10,50 €/m² mēnesī PLUS apsaimniekošana un "
        "komunālie (++); «sqm» = m². Adrese ir kartiņā/lapā (angliski «Brivibas street 171» = Brīvības iela 171).",
    ]
    if src.get("notes"):
        lines.append(f"Piezīmes par partneri: {src['notes']}")
    lines += ["", "SARAKSTA LAPA:", col["list_text"][:6000], ""]
    for i, d in enumerate(col["details"], 1):
        lines.append(f"TELPAS LAPA L{i} — {d['row'][:200]} — {d['url']} ({len(d['images'])} bildes):")
        lines.append(d["text"][:2500])
        lines.append("")
    return "\n".join(lines)


def web_links(col: dict) -> list[dict]:
    """`web_links.json` saturs: saites ar bilžu/PDF URL, ko fetch_link lejupielādē (kind=web)."""
    return [{"url": d["url"], "source": "mājaslapa", "page": None, "row_text": d["row"][:300], "kind": "web",
             "images": d["images"], "pdfs": d["pdfs"]} for d in col["details"]]
