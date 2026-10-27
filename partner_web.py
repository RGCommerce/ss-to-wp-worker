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
AREA_RE = re.compile(r"\d{1,5}(?:[.,]\d{1,2})?\s*(?:sqm|sq\.?\s*m|m²|m\s?2|mc2|m\s*<sup>\s*2\s*</sup>|kv\.?\s*m|kvm)(?![0-9])", re.I)   # «mc2» = Realto kartiņas drukas kļūda
IMG_RE = re.compile(r'(?:data-src|data-lazy|data-original|src|href)\s*=\s*["\']([^"\']+\.(?:jpe?g|png|webp)(?:\?[^"\']*)?)["\']', re.I)
PDF_RE = re.compile(r'href\s*=\s*["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', re.I)
SKIP_PATH = re.compile(r"(logo|icon|favicon|sprite|placeholder|avatar|flag|/themes/|/plugins/|emoji)", re.I)
NAV_WORDS = ("contact", "kontakt", "about", "company", "par-mums", "privacy", "cookie", "login", "cart", "wp-admin",
             "maintenance", "residential", "dzivok", "land", "zeme", "#", "mailto:", "tel:", "javascript:",
             "/agent/", "/agents/", "/author/", "/team/", "/broker")   # aģenta/autora lapas nav telpas (Realto 2026-10-27)


CHROME_CTX = re.compile(r"(agent|author|avatar|widget|related|similar|logo|profile|sidebar|comment|navbar|footer)", re.I)


def _strip_chrome(raw: str) -> str:
    """Vietnes «rāmis» ārā: <head> (favicon), pirmais <header>, <nav>, <aside>, pēdējais <footer> — tur ir izvēlne un
    kājenes logs «Nomai pieejamās telpas» ar CITU kvartālu telpām (Realto Manufaktūra → Čiekurkalns, 2026-10-27)."""
    t = re.sub(r"<head[\s>].*?</head>", " ", raw, count=1, flags=re.S | re.I)
    t = re.sub(r"<header[\s>].*?</header>", " ", t, count=1, flags=re.S | re.I)
    t = re.sub(r"<nav[\s>].*?</nav>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<aside[\s>].*?</aside>", " ", t, flags=re.S | re.I)
    i = t.lower().rfind("<footer")
    if i >= 0:
        j = t.lower().find("</footer>", i)
        t = t[:i] + " " + (t[j + 9:] if j >= 0 else "")
    return t


def _page_title(raw: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.S | re.I) or re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    return " ".join(visible_text(m.group(1)).split())[:160] if m else ""


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
                # Realto/Houzez 2026-10-27: «<span>520</span> <span>m2</span>» — tagi starp skaitli un mērvienību
                ok = bool(AREA_RE.search(ctx)) or bool(AREA_RE.search(visible_text(ctx)))
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
        # aģenta foto, autora avatārs, «līdzīgie objekti» logs, logo — nav telpas bildes (Realto Atlasa 8, 2026-10-27)
        if CHROME_CTX.search(raw[max(0, m.start() - 400): m.start()]):
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


def collect(url: str, recipe: Optional[dict] = None) -> dict:
    """Saraksta lapa + detaļas. → {list_text, fingerprint, details:[{url,row,text,images,pdfs}], needs_recipe}.
    Secība: AI iemācītā recepte (ja ir) → heiristikas (detaļu lapas / bloki vienā lapā) → needs_recipe=True."""
    raw0 = _get(url)
    raw = _strip_chrome(raw0)
    list_text = visible_text(raw)
    fp = hashlib.sha1(_norm_for_fp(list_text).encode("utf-8")).hexdigest()
    out = {"url": url, "title": _page_title(raw0), "list_text": list_text, "fingerprint": fp, "details": [], "needs_recipe": False,
           "list_only": False, "raw_len": len(raw)}
    mb = re.search(r'<base\s+href="([^"]+)"', raw0, re.I)
    base = mb.group(1) if mb else url   # <base href> maina relatīvo saišu bāzi (Eften: «lv/…/408/196/»)
    if recipe:
        try:
            det, list_only = apply_recipe(raw, url, base, recipe)
        except Exception as e:
            logger.warning("partner_web: recepte %s neizdevās: %s", url, e)
            det, list_only = [], False
        # «detaļu lapa», kas ir tā pati saraksta lapa (Eften URL atgriež visu sarakstu) → recepte nederīga
        same = {d["url"] for d in det if _norm_for_fp(d.get("text") or "")[:3000] == _norm_for_fp(list_text)[:3000]}
        det = [d for d in det if d["url"] not in same]
        if det or list_only:
            out["details"] = det
            out["list_only"] = list_only
            out["recipe_used"] = True
            return out
    links = detail_links(raw, base)
    inline = inline_blocks(raw, url)
    if inline and len(inline) >= max(2, len(links)):
        # visas telpas jau ir šajā lapā (modāļi ar galeriju + tabulu, Eften) — bez papildu ielādes
        out["details"] = inline
        return out
    if not links:
        out["needs_recipe"] = True
        out["_raw"] = raw
        return out
    out["details"] = fetch_details(links)
    return out


def fetch_details(links: list[dict]) -> list[dict]:
    """Detaļu lapas → teksts + bildes + PDF."""
    out = []
    for d in links:
        try:
            draw = _get(d["url"])
        except Exception as e:
            logger.info("partner_web: %s nenolasās: %s", d["url"], e)
            continue
        draw = _strip_chrome(draw)
        text = visible_text(draw)
        imgs = _best_images(draw, d["url"]) or _best_images(draw, d["url"], any_domain=True)   # bildes var būt citā domēnā (CDN)
        pdfs = [urljoin(d["url"], html_mod.unescape(x)) for x in PDF_RE.findall(draw)]
        pdfs = [p for p in dict.fromkeys(pdfs) if _same_site(p, d["url"]) or "wp-content" in p][:3]
        out.append({**d, "text": text, "images": imgs, "pdfs": pdfs})
    return out


# ───────────────────────────── AI recepte nezināmai lapai ─────────────────────────────
# Raimonds 2026-10-27: «katra lapa būs citādāka — negribu katru reizi iet caur tevi». Ja heiristikas lapu
# nesaprot, AI VIENREIZ izpēta HTML un atdod recepti (regex), ko glabā pie avota; turpmāk lapu lasa bez AI.

RECIPE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "mode": {"type": "string", "enum": ["detail_links", "inline_blocks", "list_only", "unsupported"]},
        "detail_url_regex": {"type": ["string", "null"]},
        "block_start_regex": {"type": ["string", "null"]},
        "image_url_regex": {"type": ["string", "null"]},
        "notes": {"type": "string"},
    },
    "required": ["mode", "detail_url_regex", "block_start_regex", "image_url_regex", "notes"],
}

RECIPE_PROMPT = """Tu analizē nekustamo īpašumu partnera MĀJASLAPAS HTML (saraksts ar brīvajām nomas telpām).
Uzdevums: pateikt, KĀ no šī HTML mašīna var izvilkt KATRU telpu atsevišķi. Atbildi TIKAI JSON pēc shēmas.
mode:
- "detail_links": katrai telpai ir sava lapa → `detail_url_regex` = Python regex, kas sakrīt ar ŠO telpu lapu URL
  (pilniem vai relatīviem href/onclick URL; piem. "/listings/[^/]+/[^/]+/$"), bet NE ar navigāciju/valodu saitēm.
- "inline_blocks": visas telpas ir šajā pašā lapā atkārtotos blokos (kartiņas, modāļi, akordeoni, tabulas rindas) →
  `block_start_regex` = regex, kas sakrīt tieši ar KATRA bloka sākuma HTML (piem. '<div class="property-item' vai
  '<tr class="unit"' vai 'id="object_\\d+"'); bloks turpinās līdz nākamajai sakritībai.
- "list_only": telpas ir tikai kā teksta rindas bez struktūras/bilžu → nekādi regex (tiks lasīts viss teksts).
- "unsupported": lapa ielādē telpas ar JavaScript/API un HTML nav datu.
`image_url_regex` (nav obligāts): regex bilžu URL, ja tās nav parastos <img src>.
Regex rakstīt bez / / iekavām, Python sintaksē, īsus un drošus. `notes` — 1 teikums LV, ko redzēji."""


def _html_sample(raw: str, limit: int = 90000) -> str:
    t = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    if len(t) <= limit:
        return t
    # sākums + vidus (saraksti parasti vidū) — lai AI redz atkārtojumus
    head = t[: int(limit * 0.45)]
    mid0 = len(t) // 2
    return head + "\n…[izlaists]…\n" + t[mid0: mid0 + int(limit * 0.55)]


def learn_recipe(url: str, raw: str) -> dict:
    """AI izpēta HTML un atdod recepti (vienreizēja izmaksa ~daži centi)."""
    import os
    from openai import OpenAI
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY nav konfigurēts")
    model = os.getenv("PARTNER_INTAKE_MODEL") or os.getenv("MODEL") or "gpt-5.4"
    client = OpenAI(api_key=api_key)
    content = [
        {"type": "input_text", "text": f"LAPA: {url}\nREDZAMAIS TEKSTS (fragments):\n{visible_text(raw)[:4000]}"},
        {"type": "input_text", "text": "HTML (bez script/style):\n" + _html_sample(raw)},
        {"type": "input_text", "text": RECIPE_PROMPT},
    ]
    resp = client.responses.create(
        model=model, input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": "web_recipe", "schema": RECIPE_SCHEMA, "strict": True}},
    )
    rec = json.loads(resp.output_text)
    rec["learned_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    rec["model"] = model
    return rec


def _compile(rx: Optional[str]):
    if not rx:
        return None
    try:
        return re.compile(rx, re.I)
    except re.error as e:
        logger.warning("partner_web: nederīgs regex %r: %s", rx, e)
        return None


def apply_recipe(raw: str, url: str, base: str, recipe: dict) -> tuple[list[dict], bool]:
    """Recepte → (detaļas, list_only)."""
    mode = recipe.get("mode")
    if mode == "list_only":
        return [], True
    if mode == "detail_links":
        rx = _compile(recipe.get("detail_url_regex"))
        if not rx:
            return [], False
        occ: list[tuple[int, str]] = []
        for m in re.finditer(r"""(?:href|location\.href)\s*=\s*['"]([^'"]+)['"]""", raw):
            rawu = html_mod.unescape(m.group(1)).strip()
            if not rawu or rawu.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            full = urljoin(base, rawu).split("#")[0].split("?")[0].rstrip("/") + "/"
            if not (rx.search(rawu) or rx.search(full)):
                continue
            if not _same_site(full, url):
                continue
            if full.rstrip("/") == url.split("#")[0].split("?")[0].rstrip("/"):
                continue
            occ.append((m.start(), full))
        order = list(dict.fromkeys(u for _, u in occ))[:MAX_DETAIL_PAGES]
        rows: dict[str, str] = {}
        for k, (pos, u) in enumerate(occ):
            nxt = next((p2 for p2, u2 in occ[k + 1:] if u2 != u), pos + 2500)
            seg = raw[pos: min(nxt, pos + 2500)]
            seg = seg[seg.find(">") + 1: seg.rfind("<")] if ">" in seg and "<" in seg else seg
            row = " ".join(x for x in visible_text(seg).split("\n") if x.strip())[:300]
            if len(row) > len(rows.get(u, "")):
                rows[u] = row
        return fetch_details([{"url": u, "row": rows.get(u, "")} for u in order]), False
    if mode == "inline_blocks":
        rx = _compile(recipe.get("block_start_regex"))
        if not rx:
            return [], False
        starts = [m.start() for m in rx.finditer(raw)]
        img_rx = _compile(recipe.get("image_url_regex"))
        out = []
        for k, pos in enumerate(starts[:MAX_DETAIL_PAGES]):
            end = starts[k + 1] if k + 1 < len(starts) else min(len(raw), pos + 40000)
            seg = raw[pos:end]
            text = visible_text(seg)
            row = " ".join(x for x in text.split("\n") if x.strip())[:300]
            imgs = _best_images(seg, url, any_domain=True)
            if img_rx:
                extra = [urljoin(url, html_mod.unescape(x if isinstance(x, str) else x[0])) for x in img_rx.findall(seg)]
                imgs = list(dict.fromkeys(imgs + extra))[:MAX_IMAGES_PER_PAGE]
            out.append({"url": f"{url.split('#')[0]}#blk{k + 1}", "row": row, "text": text, "images": imgs, "pdfs": []})
        return out, False
    return [], False


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
    if col.get("title"):
        lines.append(f"LAPA IR PAR: «{col['title']}» — telpas CITĀ adresē/kvartālā nekā šī ēka ir tikai pieminētas garāmejot "
                     "(tām adresēm `list_complete_here` = false).")
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
