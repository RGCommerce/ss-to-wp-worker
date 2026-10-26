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
AREA_RE = re.compile(r"\d{1,5}(?:[.,]\d{1,2})?\s*(?:sqm|sq\.?\s*m|m²|m\s?2|m\s*<sup>\s*2\s*</sup>|kv\.?\s*m|kvm)(?![0-9])", re.I)
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
    t = re.sub(r"<[^>]+>", " ", t)   # atribūtos ieslēpts HTML (&lt;div …&gt;) pēc unescape — vēlreiz ārā
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
    un kas nav navigācija/valoda/pati lapa. Kartiņas teksts (adrese/nosaukums/cena/platība) = redzamais teksts
    no saites PIRMĀS vietas līdz NĀKAMĀS citas saites vietai (kartiņa sākas ar slaideri, info seko pēc tā —
    ±logs deva iepriekšējās kartiņas tekstu, Spire 2026-10-26)."""
    base_path = urlparse(base_url).path.rstrip("/")
    occ: list[tuple[int, str]] = []   # (pozīcija, URL) visām saitēm uz telpu lapām, dokumenta secībā
    valid: dict[str, bool] = {}
    for m in re.finditer(r"""(?:href|location\.href)\s*=\s*['"]([^'"]+)['"]""", raw):
        u = html_mod.unescape(m.group(1)).strip()
        if not u or any(w in u.lower() for w in NAV_WORDS):
            continue
        u = urljoin(base_url, u).split("#")[0].split("?")[0]
        u = u.rstrip("/") + "/"   # «…/b171-office-no-400» un «…/b171-office-no-400/» = viena lapa
        if not valid.get(u):   # pietiek, ja KĀDĀ vietā blakus ir platība (pirmā vieta var būt <link>/meta bez teksta)
            ok = _same_site(u, base_url)
            p = urlparse(u).path.rstrip("/")
            ok = ok and bool(p) and p != base_path and p.count("/") >= 2
            if ok and re.search(r"/(lv|ru|en|de)(/|$)", p) and not re.search(r"/(lv|ru|en|de)/", base_url):
                ok = False   # citas valodas dublikāti
            if ok:
                ctx = raw[max(0, m.start() - 700): m.end() + 700]
                ok = bool(AREA_RE.search(ctx))
            valid[u] = valid.get(u) or ok
        if valid[u]:
            occ.append((m.start(), u))
    order = list(dict.fromkeys(u for _, u in occ))[:MAX_DETAIL_PAGES]
    # kartiņas teksts: no saites vietas līdz nākamajai CITAI saitei; URL atkārtojas (slaidera bildes, virsraksts) —
    # ņem to vietu, kurai seko visvairāk redzamā teksta (Spire: pirmā vieta bija slaiderī bez teksta)
    rows: dict[str, str] = {}
    for k, (pos, u) in enumerate(occ):
        nxt = next((p2 for p2, u2 in occ[k + 1:] if u2 != u), pos + 2500)
        seg = raw[pos: min(nxt, pos + 2500)]
        seg = seg[seg.find(">") + 1: seg.rfind("<")] if ">" in seg and "<" in seg else seg   # bez pusēs pārgrieztiem tagiem
        row = " ".join(x for x in visible_text(seg).split("\n") if x.strip())[:300]
        if len(row) > len(rows.get(u, "")):
            rows[u] = row
    return [{"url": u, "row": rows.get(u, "")} for u in order]


def _best_images(raw: str, page_url: str, any_domain: bool = False) -> list[str]:
    """Bildes: lielākais variants katram failam (WP «-300x200», «_tn», «_thumb», «_web2» varianti), bez logo/ikonām.
    any_domain=True — bilžu serveris var būt cits (Eften: rems.eften.lv), lieto lapas iekšējiem blokiem."""
    best: dict[str, tuple[int, str]] = {}
    for m in IMG_RE.finditer(raw):
        u = urljoin(page_url, html_mod.unescape(m.group(1)))
        if not any_domain and not _same_site(u, page_url) and "wp-content" not in u:
            continue
        if SKIP_PATH.search(u):
            continue
        path = urlparse(u).netloc + urlparse(u).path
        mm = re.search(r"-(\d{2,4})x(\d{2,4})(\.\w+)$", path)
        ms = re.search(r"_(tn|thumb|thumbnail|small|sm|xs|web\d*|md|medium|large|lg)(\.\w+)$", path, re.I)
        if mm:
            base = path[: mm.start()] + mm.group(3)
            score = int(mm.group(1)) * int(mm.group(2))
        elif ms:
            base = path[: ms.start()] + ms.group(2)
            tag = ms.group(1).lower()
            score = 100 if tag in ("tn", "thumb", "thumbnail", "small", "sm", "xs") else (8_000_000 if tag in ("large", "lg") else 5_000_000)
        else:
            base, score = path, 10_000_000   # oriģināls bez izmēra = lielākais
        if base not in best or score > best[base][0]:
            best[base] = (score, u)
    return [u for sc, u in list(best.values()) if sc > 100][:MAX_IMAGES_PER_PAGE] or [u for _, u in list(best.values())][:MAX_IMAGES_PER_PAGE]


def inline_blocks(raw: str, base_url: str) -> list[dict]:
    """Visas telpas VIENĀ lapā (bez atsevišķām lapām — Eften: modāļi «object_196» ar galeriju + tabulu).
    Atrod atkārtotus elementus ar id «<prefikss><numurs>», kuru saturā ir platība → katrs = telpa."""
    hits = [(m.start(), m.group(1), m.group(1) + m.group(2)) for m in re.finditer(r'<(?:div|section|article|li)[^>]*\sid="([a-zA-Z_-]+?)(\d+)"', raw)]
    by_prefix: dict[str, list[tuple[int, str]]] = {}
    for pos, pref, full in hits:
        if pref.startswith(("menu", "nav", "post-", "comment")):
            continue
        by_prefix.setdefault(pref, []).append((pos, full))
    best: list[dict] = []
    for pref, occ in by_prefix.items():
        if len(occ) < 2:
            continue
        blocks = []
        for k, (pos, full) in enumerate(occ):
            end = occ[k + 1][0] if k + 1 < len(occ) else min(len(raw), pos + 40000)
            seg = raw[pos:end]
            if not AREA_RE.search(seg):
                continue
            blocks.append({"url": f"{base_url.split('#')[0]}#{full}", "row": "", "html": seg})
        # vienāds skaits (kartiņas + modāļi ar tām pašām telpām) → ņem bagātākos (vairāk teksta + bilžu)
        def _rich(bl):
            return sum(len(x["html"]) for x in bl)
        if len(blocks) > len(best) or (len(blocks) == len(best) and _rich(blocks) > _rich(best)):
            best = blocks
    out = []
    for b in best[:MAX_DETAIL_PAGES]:
        text = visible_text(b["html"])
        row = " ".join(x for x in text.split("\n") if x.strip())[:300]
        out.append({"url": b["url"], "row": row, "text": text, "images": _best_images(b["html"], base_url, any_domain=True), "pdfs": []})
    return out


def collect(url: str) -> dict:
    """Saraksta lapa + detaļas. → {list_text, fingerprint, details:[{url,row,text,images,pdfs}]}"""
    raw = _get(url)
    list_text = visible_text(raw)
    fp = hashlib.sha1(_norm_for_fp(list_text).encode("utf-8")).hexdigest()
    out = {"url": url, "list_text": list_text, "fingerprint": fp, "details": []}
    mb = re.search(r'<base\s+href="([^"]+)"', raw, re.I)
    base = mb.group(1) if mb else url   # <base href> maina relatīvo saišu bāzi (Eften: «lv/…/408/196/»)
    links = detail_links(raw, base)
    inline = inline_blocks(raw, url)
    if inline and len(inline) >= max(2, len(links)):
        # visas telpas jau ir šajā lapā (modāļi ar galeriju + tabulu, Eften) — bez papildu ielādes
        out["details"] = inline
        return out
    for d in links:
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
