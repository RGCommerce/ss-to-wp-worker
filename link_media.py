"""link_media.py — partneru piedāvājumos iekļautās saites uz bildēm (Google Drive mapes/faili).

Raimonds 2026-10-23: MCITY (un citi) sūta PDF tabulu, kur katrai telpai ir saite «bildes un plānojums»
uz publisku Google Drive mapi. Mērķis — tās bildes/plānus ievilkt mūsu sistēmā pie pareizās telpas.

Publiskas Drive mapes nolasa BEZ API atslēgas:
    https://drive.google.com/embeddedfolderview?id=<folder>   → faili + apakšmapes (HTML)
    https://drive.google.com/uc?export=download&id=<file>       → faila saturs
Privātas mapes (nav «Anyone with the link») → atgriež tukšu + kļūdas piezīmi (aģents redz panelī).

Saglabā: <intake_dir>/links/<link_id>/<drošs_nosaukums>; manifests links/<link_id>/_files.json.
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

logger = logging.getLogger("link_media")

UA = {"User-Agent": "Mozilla/5.0 (compatible; RGC-partner-intake/1.0)"}
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic")
DOC_EXT = (".pdf", ".docx")
MAX_FILES_PER_LINK = 40
MAX_BYTES = 25 * 1024 * 1024

_FOLDER_RE = re.compile(r"drive\.google\.com/(?:drive/(?:u/\d+/)?folders/|open\?id=|embeddedfolderview\?id=)([A-Za-z0-9_-]{10,})")
_FILE_RE = re.compile(r"drive\.google\.com/(?:file/d/|uc\?(?:export=\w+&)?id=)([A-Za-z0-9_-]{10,})")


def classify_url(url: str) -> Optional[tuple[str, str]]:
    """→ ("gdrive_folder", id) | ("gdrive_file", id) | None (neatbalstīts)."""
    u = url or ""
    m = _FOLDER_RE.search(u)
    if m:
        return "gdrive_folder", m.group(1)
    m = _FILE_RE.search(u)
    if m:
        return "gdrive_file", m.group(1)
    return None


def _get(url: str, timeout: int = 40) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(MAX_BYTES + 1)


def list_gdrive_folder(folder_id: str, depth: int = 1) -> list[dict]:
    """[{id, name, kind: file|folder, path}] — rekursīvi līdz `depth` apakšmapju līmenim."""
    html = _get(f"https://drive.google.com/embeddedfolderview?id={folder_id}").decode("utf-8", "ignore")
    out: list[dict] = []
    for m in re.finditer(r'<div class="flip-entry" id="entry-([^"]+)".*?<a href="([^"]+)".*?'
                         r'<div class="flip-entry-title">([^<]*)</div>', html, re.S):
        fid, href, name = m.group(1), m.group(2), _html_unescape(m.group(3))
        if "/folders/" in href:
            out.append({"id": fid, "name": name, "kind": "folder", "path": name})
            if depth > 0:
                try:
                    for sub in list_gdrive_folder(fid, depth - 1):
                        out.append({**sub, "path": f"{name}/{sub['path']}"})
                except Exception as e:
                    logger.info("link_media: apakšmape %s: %s", fid, e)
        else:
            out.append({"id": fid, "name": name, "kind": "file", "path": name})
    return out


def _html_unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s or "").strip()


def _safe(name: str) -> str:
    n = re.sub(r"[^\w.\- ]+", "_", name, flags=re.U).strip(" .") or "fails"
    return n[:120]


def download_gdrive_file(file_id: str) -> Optional[bytes]:
    data = _get(f"https://drive.google.com/uc?export=download&id={file_id}", timeout=60)
    if len(data) > MAX_BYTES:
        return None
    # lieliem failiem Google atgriež brīdinājuma HTML (vīrusu skenēšana) — tad mēģina ar confirm
    if data[:200].lstrip().lower().startswith(b"<!doctype html") or b"<html" in data[:200].lower():
        m = re.search(rb'name="uuid" value="([^"]+)"', data)
        url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
        if m:
            url += "&uuid=" + m.group(1).decode()
        data = _get(url, timeout=90)
        if data[:200].lstrip().lower().startswith(b"<!doctype html"):
            return None
    return data


def fetch_link(intake_dir: Path, link_id: str, url: str) -> dict:
    """Lejupielādē saites saturu (bildes + PDF/DOCX) → links/<link_id>/. Idempotents (manifests)."""
    d = intake_dir / "links" / link_id
    man_p = d / "_files.json"
    kind = classify_url(url)
    if man_p.is_file():
        try:
            man = json.loads(man_p.read_text(encoding="utf-8"))
            # Kešs pēc L-numura, bet pārlasot numerācija mainās (prod MCITY: L9 bija 388 m² mape, tagad
            # 36,3 m²) → derīgs TIKAI, ja tā pati Drive mape; citādi mapi izmet un lejupielādē no jauna
            if classify_url(man.get("url") or "") == kind:
                return man
        except Exception:
            pass
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    res: dict = {"link_id": link_id, "url": url, "kind": kind[0] if kind else None, "files": [], "error": None}
    if not kind:
        res["error"] = "neatbalstīta saite"
        man_p.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        return res
    try:
        if kind[0] == "gdrive_folder":
            entries = [e for e in list_gdrive_folder(kind[1]) if e["kind"] == "file"]
            if not entries:
                res["error"] = "mape tukša vai nav publiska"
        else:
            entries = [{"id": kind[1], "name": f"{kind[1]}", "kind": "file", "path": kind[1]}]
        wanted = [e for e in entries if Path(e["name"]).suffix.lower() in IMG_EXT + DOC_EXT
                  or kind[0] == "gdrive_file"][:MAX_FILES_PER_LINK]

        def _one(e):
            try:
                data = download_gdrive_file(e["id"])
                return e, data
            except Exception as ex:
                return e, ex

        with ThreadPoolExecutor(max_workers=6) as ex:
            for e, data in ex.map(_one, wanted):
                if isinstance(data, Exception) or not data:
                    res["files"].append({"name": e["path"], "error": type(data).__name__ if isinstance(data, Exception) else "nav"})
                    continue
                name = _safe(e["path"].replace("/", "__"))
                ext = Path(name).suffix.lower()
                if not ext:   # viens fails bez nosaukuma → nosaka pēc satura
                    ext = ".pdf" if data[:4] == b"%PDF" else ".jpg"
                    name += ext
                (d / name).write_bytes(data)
                res["files"].append({"name": e["path"], "file": name, "size": len(data),
                                     "type": "image" if ext in IMG_EXT else ("pdf" if ext == ".pdf" else "doc")})
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    man_p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _nums(text: str) -> list[float]:
    out = []
    for m in _NUM_RE.finditer(text or ""):
        try:
            out.append(float(m.group(0).replace(",", ".")))
        except ValueError:
            pass
    return out


def _row_bands(page) -> list[float]:
    """Tabulas rindu robežas = garas horizontālas līnijas (zīmējumi)."""
    ys = set()
    try:
        for d in page.get_drawings():
            for it in d.get("items") or []:
                if it[0] == "l":
                    p1, p2 = it[1], it[2]
                    if abs(p1.y - p2.y) < 0.6 and abs(p1.x - p2.x) > 150:
                        ys.add(round(p1.y, 1))
                elif it[0] == "re":
                    r = it[1]
                    if r.height < 1.5 and r.width > 150:
                        ys.add(round(r.y0, 1))
    except Exception:
        pass
    return sorted(ys)


def _header_cols(words) -> dict:
    """Kolonnu x-diapazoni pēc galvenes vārdiem (Stāvs / Platība / Nomas maksa / Apsaimniekošanas)."""
    cols = {}
    for w in words:
        t = w[4].lower()
        for key, pats in (("floor", ("stāvs", "stavs", "floor")), ("area", ("platība", "platiba", "m2", "m²", "area")),
                          ("rent", ("nomas", "noma", "rent", "cena", "price")), ("maint", ("apsaimniekošanas", "apsaimn", "maint"))):
            if key not in cols and any(t.startswith(p) for p in pats) and w[1] < 300:
                cols[key] = (w[0] - 25, w[2] + 25)
    return cols


def links_from_pdf(pdf_bytes: bytes, source_name: str) -> list[dict]:
    """PDF hipersaites + TĀS TABULAS RINDAS saturs (rinda = josla starp zīmētajām līnijām; ja līniju nav —
    ±2 pt ap saiti). Katrai saitei arī strukturēti: row_areas (Platība kolonna), row_floor, row_rents —
    lai telpa↔saite piesaisti var noteikt DETERMINISTISKI (AI kļūdījās: MCITY 36,3 m² dabūja 388 m² mapi).
    → [{url, page, row_text, row_areas, row_floor, row_rents, y}]"""
    out: list[dict] = []
    try:
        import pymupdf
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return out
    for pno, page in enumerate(doc):
        words = page.get_text("words")  # x0,y0,x1,y1,text,...
        bands = _row_bands(page)
        cols = _header_cols(words)
        for l in page.get_links():
            uri = l.get("uri") or ""
            if not uri.startswith("http"):
                continue
            r = l["from"]
            above = max([y for y in bands if y <= r.y0 + 0.5] or [r.y0 - 2])
            below = min([y for y in bands if y >= r.y1 - 0.5] or [r.y1 + 2])
            if below - above > 400:   # bez līnijām / pārāk plata josla → tikai saites rinda
                above, below = r.y0 - 2, r.y1 + 2
            row = [w for w in words if not (w[3] < above - 1 or w[1] > below + 1)]
            row.sort(key=lambda w: (round(w[1] / 4), w[0]))

            def _col(key):
                c = cols.get(key)
                return " ".join(w[4] for w in row if c and c[0] <= (w[0] + w[2]) / 2 <= c[1])

            area_txt, floor_txt, rent_txt = _col("area"), _col("floor"), _col("rent")
            out.append({"url": uri, "page": pno + 1, "source": source_name, "y": round(r.y0, 1),
                        "row_text": " ".join(w[4] for w in row)[:400],
                        "row_areas": _nums(area_txt) if cols.get("area") else [],
                        "row_floor": floor_txt.strip() or None,
                        "row_rents": _nums(rent_txt) if cols.get("rent") else []})
    return out


def links_from_text(text: str, source_name: str) -> list[dict]:
    """Saites e-pasta tekstā (piem. «Bildes: https://drive...») ar to pašu rindu kā kontekstu."""
    out = []
    for line in (text or "").splitlines():
        for m in re.finditer(r"https?://[^\s<>\")\]]+", line):
            out.append({"url": m.group(0).rstrip(".,;"), "page": None, "source": source_name,
                        "row_text": line.strip()[:300]})
    return out


_SKIP_HOSTS = ("facebook.", "linkedin.", "instagram.", "twitter.", "x.com", "youtube.", "wa.me", "whatsapp.",
               "rgcommerce.", "rgcnoma.", "google.com/maps", "goo.gl/maps", "maps.app.goo.gl", "mailchimp", "unsubscribe")


def is_web_page(url: str) -> bool:
    """Parasta partnera web lapa (piem. realto.lv/property/...), ko vērts izlasīt — ne Drive, ne soc. tīkli, ne mūsu."""
    u = (url or "").lower()
    if not u.startswith(("http://", "https://")) or classify_url(url):
        return False
    if any(h in u for h in _SKIP_HOSTS):
        return False
    path = re.sub(r"^https?://[^/]+", "", u).strip("/")
    return bool(path) and not re.search(r"\.(jpg|jpeg|png|gif|pdf|zip|docx?|xlsx?)(\?|$)", path)


def fetch_page_text(url: str, limit: int = 2500) -> Optional[dict]:
    """Web lapa → {title, text}: teksts no virsraksta otrās parādīšanās (pēc izvēlnes) — tur ir adrese/cena/platība."""
    import html as _h
    try:
        raw = _get(url, timeout=20)[:3_000_000].decode("utf-8", "ignore")
    except Exception as e:
        logger.info("link_media: lapa %s nenolasās: %s", url, e)
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    title = _h.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    t = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    t = _h.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"\s+", " ", t).strip()
    head = re.split(r"\s[|–-]\s", title)[0].strip()
    start = 0
    if head and len(head) > 5:
        hits = [x.start() for x in re.finditer(re.escape(head[:40]), t)]
        if len(hits) >= 2:
            start = hits[1]
        elif hits:
            start = hits[0]
    return {"url": url, "title": title, "text": t[max(0, start - 50): start + limit]}


def render_pdf_first_page(pdf_bytes: bytes, out_path: Path, dpi: int = 110) -> bool:
    """Plāna PDF → PNG (kā plāns listingam)."""
    try:
        import pymupdf
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        if doc.page_count == 0:
            return False
        doc[0].get_pixmap(dpi=dpi).save(str(out_path))
        return True
    except Exception:
        return False
