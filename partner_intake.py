"""partner_intake.py — PARTNERU PIEDĀVĀJUMU IEPLŪDE (e-pasts → AI → salīdzināšana ar DB → reports).

Raimonda darbplūsma (HANDOFF_2026-10-20_PARTNERU_PIEDAVAJUMU_IEPLUDE_BRAINSTORM.md, melna):
  Ieva/Raimonds PĀRSŪTA partnera e-pastu (PDF/Excel/teksts) uz ai@rgcnoma.lv → šis modulis:
  1. IMAP (inbox.eu) paņem jaunos, saglabā pielikumus uz volume `partner_intake/<id>/`.
  2. AI (OpenAI, strict JSON) izlasa JEBKURU formātu → `vacancy_list` (partneris, adreses, telpas).
  3. Salīdzina ar DB: tās ēkas mūsu listingi (jauns / sakrīt / vairāki / nav sarakstā),
     bilžu avoti (PDF iegultie foto, mūsu listingi tai pašā ēkā, ss.lv inbox tā pati telpa).
  4. report.json + Telegram «AI sagatavoja: N gatavi / M pārbaudīt» pārsūtītājam.
  5. Aģents panelī apstiprina → `apply()` raksta DB (INSERT caur agent_publish importa ceļu,
     UPDATE cenu, «iznomāts») + bildes. Nekas DB nerakstās bez apstiprinājuma.

Vide: PARTNER_IMAP_HOST/PORT/USER/PASSWORD (+ PARTNER_IMAP_FOLDER=INBOX, PARTNER_INTAKE_INTERVAL=120,
PARTNER_ALLOWED_FORWARDERS=rgcommerce.lv,rgcnoma.lv,rgctelpas.lv), OPENAI_API_KEY, MODEL,
TELEGRAM_BOT_TOKEN (pings), PANEL_URL (saites), STORAGE_ROOT, DATABASE_URL.
Bez PARTNER_IMAP_* — pollers guļ (CLI `--file` tests strādā bez pastkastes).
"""
from __future__ import annotations

import asyncio
import base64
import email
import email.policy
import html as html_mod
import imaplib
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).parent))  # agent_publish/sslv_import/ai_text_helpers (arī `python -P`)

logger = logging.getLogger("partner_intake")

STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(Path(__file__).parent / "storage")))
INTAKE_DIR = STORAGE_ROOT / "partner_intake"
DATABASE_URL = os.getenv("DATABASE_URL")

IMAP_HOST = os.getenv("PARTNER_IMAP_HOST")
IMAP_PORT = int(os.getenv("PARTNER_IMAP_PORT", "993"))
IMAP_USER = os.getenv("PARTNER_IMAP_USER")
IMAP_PASSWORD = os.getenv("PARTNER_IMAP_PASSWORD")
IMAP_FOLDER = os.getenv("PARTNER_IMAP_FOLDER", "INBOX")
IMAP_PROCESSED = os.getenv("PARTNER_IMAP_PROCESSED_FOLDER", "Processed")
INTERVAL = int(os.getenv("PARTNER_INTAKE_INTERVAL", "120"))
ALLOWED_FORWARDERS = {
    d.strip().lower()
    for d in os.getenv("PARTNER_ALLOWED_FORWARDERS", "rgcommerce.lv,rgcnoma.lv,rgctelpas.lv").split(",")
    if d.strip()
}
PANEL_URL = os.getenv("PANEL_URL", "https://panel.rgcommerce.lv").rstrip("/")

MAX_ATTACH_BYTES = 25 * 1024 * 1024
ATTACH_EXTS = {".pdf", ".xlsx", ".xlsm", ".xls", ".csv", ".txt", ".docx", ".jpg", ".jpeg", ".png", ".webp"}
MAX_PDF_PAGES_PER_DOC = 8
MAX_VISION_IMAGES = 16
PAGE_MAX_SIDE_PX = 1100
MAX_TEXT_CHARS = 60_000

OUR_SOURCES = ("wp", "agent_anketa_easy", "agent_anketa_full", "agent_pdf", "agent_manual", "agent_partner")

SPACE_GROUPS = ["Birojs", "Tirdzniecība", "Noliktava", "Ražošana", "Medicīna", "Studija",
                "Restorans/Cafe", "Sporta zāle", "Autoserviss", "PVD", "StockOfiss", "Zeme",
                "Investīciju objekts"]
FLOOR_OPTIONS = ["Pagrabs"] + [f"{i}.stāvs" for i in range(1, 13)] + ["Vienstāvu", "Divstāvu", "Trīsstāvu"]


# ───────────────────────────── helpers ─────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_name(name: str, default: str = "fails") -> str:
    base = Path(name or default).name
    base = re.sub(r"[^\w.\-() ]+", "_", base, flags=re.UNICODE).strip() or default
    return base[:120]


def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html_mod.unescape(h)
    h = re.sub(r"[ \t]+", " ", h)
    return re.sub(r"\n\s*\n+", "\n", h).strip()


def _nullable(t: str) -> dict:
    return {"type": [t, "null"]}


def _enum_nullable(vals: list[str]) -> dict:
    return {"type": ["string", "null"], "enum": vals + [None]}


def _num(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def _numstr(v: Any) -> str:
    f = _num(v)
    if f is None:
        return "" if v in (None, "") else str(v)
    return str(int(f)) if float(f).is_integer() else f"{f:.2f}".rstrip("0").rstrip(".")


def _floor_norm(v: Any) -> str:
    """Stāvs → FLOOR_OPTIONS formāts (kā pdf_import._floor)."""
    s = ("" if v is None else str(v)).strip()
    if not s:
        return ""
    if s in FLOOR_OPTIONS:
        return s
    low = s.lower()
    if any(k in low for k in ("pagrab", "basement", "cellar", "cokol", "puspagrab")):
        return "Pagrabs"
    m = re.match(r"^\s*(-?\d{1,2})", s)
    if m:
        n = int(m.group(1))
        if n <= 0:
            return "Pagrabs"
        if 1 <= n <= 12:
            return f"{n}.stāvs"
    if low.startswith(("ground", "1st", "first")):
        return "1.stāvs"
    return s


def _floor_digits(v: Any) -> str:
    return re.sub(r"[^0-9\-+/]", "", ("" if v is None else str(v)).lower())


def _street_key(street: str) -> str:
    """Tipa-neatkarīga ielas atslēga salīdzināšanai (kā paneļa street_search / dedup_street)."""
    s = (street or "").lower()
    s = s.translate(str.maketrans("āēīōūčšžģķļņŗ", "aeiouczsgklnr"))
    s = re.sub(r"\b(iela|street|prospekts|pr\.|bulvaris|bulv\.|gatve|gat\.|dambis|d\.|laukums|aleja|skerslela|šķērsiela)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    # iniciāļi («k. valdemāra» → «valdemāra»), lai LIKE neplūst pa visu DB
    return " ".join(t for t in s.split() if len(t) > 1 or t.isdigit())


def _street_like(street: str) -> Optional[str]:
    """POSIX regex ielas sakritībai pa VESELIEM vārdiem (lai 'delu 8' neatrod 'udelu 18'):
    lieto ar SQL `~` uz translate(lower(street))."""
    toks = _street_key(street).split()
    if not toks:
        return None
    return r"(^|[^a-z0-9])" + r"[^a-z0-9]+.*?".join(re.escape(t) for t in toks) + r"($|[^a-z0-9])" if len(toks) > 1         else r"(^|[^a-z0-9])" + re.escape(toks[0]) + r"($|[^a-z0-9])"


# ───────────────────────────── 1) IMAP ─────────────────────────────

def imap_configured() -> bool:
    return bool(IMAP_HOST and IMAP_USER and IMAP_PASSWORD)


def _seen_path() -> Path:
    return INTAKE_DIR / "_seen_message_ids.json"


def _load_seen() -> set[str]:
    try:
        return set(json.loads(_seen_path().read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_seen(seen: set[str]) -> None:
    INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    _seen_path().write_text(json.dumps(sorted(seen)[-5000:]), encoding="utf-8")


def _orig_sender_from_text(text: str) -> dict:
    """Pārsūtītā e-pasta galvene tekstā: 'From: X <x@y>' / 'No: …' / 'Von:' / 'От:'."""
    m = re.search(r"(?im)^\s*(?:from|no|von|от|sūtītājs|sender)\s*:\s*(.+?)\s*$", text or "")
    if not m:
        return {}
    line = m.group(1)
    name, addr = parseaddr(line)
    if not addr:
        em = re.search(r"[\w.+-]+@[\w.-]+\.\w+", line)
        addr = em.group(0) if em else ""
        name = line.replace(addr, "").strip(" <>\"'")
    return {"name": (name or "").strip(" \"'"), "email": addr.lower()}


def _parse_message(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    fwd_name, fwd_addr = parseaddr(str(msg.get("From", "")))
    subject = str(msg.get("Subject", "") or "")
    message_id = str(msg.get("Message-ID", "") or "").strip()
    text_plain, text_html = "", ""
    attachments: list[dict] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition", "") or "")
        if fn or "attachment" in disp:
            ext = Path(fn or "").suffix.lower()
            if ext in ATTACH_EXTS or ctype == "application/pdf":
                data = part.get_payload(decode=True) or b""
                if 0 < len(data) <= MAX_ATTACH_BYTES:
                    if not ext and ctype == "application/pdf":
                        fn = (fn or "pielikums") + ".pdf"
                    attachments.append({"name": _safe_name(fn), "type": ctype, "data": data})
            continue
        if ctype == "text/plain" and not text_plain:
            try:
                text_plain = part.get_content()
            except Exception:
                text_plain = (part.get_payload(decode=True) or b"").decode("utf-8", "ignore")
        elif ctype == "text/html" and not text_html:
            try:
                text_html = part.get_content()
            except Exception:
                text_html = (part.get_payload(decode=True) or b"").decode("utf-8", "ignore")
    body = (text_plain or "").strip() or _html_to_text(text_html or "")
    # Pārsūtītā e-pastā (.eml pielikumā) — paņem arī to
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            try:
                inner = part.get_payload(0)
                inner_raw = inner.as_bytes()
                sub = _parse_message(inner_raw)
                body = (body + "\n\n--- pārsūtītais ---\nFrom: " + sub.get("forwarder_email", "")
                        + "\nSubject: " + sub.get("subject", "") + "\n" + sub.get("body", ""))
                attachments.extend(sub.get("attachments_raw", []))
            except Exception:
                pass
    orig = _orig_sender_from_text(body)
    return {
        "message_id": message_id,
        "forwarder_name": fwd_name,
        "forwarder_email": (fwd_addr or "").lower(),
        "subject": subject,
        "body": body[:MAX_TEXT_CHARS],
        "orig_sender": orig,
        "attachments_raw": attachments,
        "raw": raw,
        "date": str(msg.get("Date", "") or ""),
    }


def _store_intake(parsed: dict, source: str = "imap") -> dict:
    intake_id = _now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    d = INTAKE_DIR / intake_id
    (d / "att").mkdir(parents=True, exist_ok=True)
    atts = []
    for a in parsed.get("attachments_raw", []):
        p = d / "att" / a["name"]
        i = 1
        while p.exists():
            p = d / "att" / f"{Path(a['name']).stem}_{i}{Path(a['name']).suffix}"
            i += 1
        p.write_bytes(a["data"])
        atts.append({"name": p.name, "path": str(p.relative_to(STORAGE_ROOT)).replace("\\", "/"),
                     "type": a.get("type"), "size": len(a["data"])})
    meta = {
        "intake_id": intake_id,
        "source": source,
        "received_at": _now().isoformat(),
        "message_id": parsed.get("message_id"),
        "forwarder_name": parsed.get("forwarder_name"),
        "forwarder_email": parsed.get("forwarder_email"),
        "subject": parsed.get("subject"),
        "orig_sender": parsed.get("orig_sender") or {},
        "date": parsed.get("date"),
        "attachments": atts,
        "status": "new",
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "body.txt").write_text(parsed.get("body", ""), encoding="utf-8")
    if parsed.get("raw"):
        # oriģinālā vēstule — lai var atjaunot/pārsūtīt, ja kas noiet greizi (2026-10-21 mācība)
        (d / "raw.eml").write_bytes(parsed["raw"])
    return meta


def _ensure_processed_folder(m: imaplib.IMAP4) -> str | None:
    """Atrod/izveido arhīva mapi. Mēģina `Processed`, tad `INBOX<delim>Processed`
    (inbox.eu u.c. serveri mapes pieļauj tikai zem INBOX). Atgriež mapes vārdu vai None."""
    cached = getattr(m, "_rgc_processed", None)
    if cached is not None:
        return cached or None
    delim = "/"
    try:
        typ, boxes = m.list()
        if typ == "OK":
            for b in boxes or []:
                line = b.decode(errors="ignore") if isinstance(b, bytes) else str(b)
                mm = re.search(r'\) "?([^" ]+)"? ', line)
                if mm and mm.group(1) != "NIL":
                    delim = mm.group(1)
                    break
    except Exception:
        pass
    candidates = [IMAP_PROCESSED]
    if not IMAP_PROCESSED.upper().startswith("INBOX"):
        candidates.append(f"INBOX{delim}{IMAP_PROCESSED}")
    for name in candidates:
        for op in ("select", "create"):
            try:
                typ, _ = m.select(name, readonly=True) if op == "select" else m.create(name)
            except Exception:
                typ = "NO"
            if typ == "OK":
                m.select(IMAP_FOLDER)
                m._rgc_processed = name  # type: ignore[attr-defined]
                return name
    m.select(IMAP_FOLDER)
    m._rgc_processed = ""  # type: ignore[attr-defined]
    logger.warning("partner_intake: nevar izveidot arhīva mapi %s — vēstules paliek INBOX kā izlasītas", IMAP_PROCESSED)
    return None


def fetch_new_intakes() -> list[dict]:
    """IMAP: neredzētās vēstules → intake mapes. Atgriež meta sarakstu."""
    if not imap_configured():
        return []
    seen = _load_seen()
    out: list[dict] = []
    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=60)
    try:
        m.login(IMAP_USER, IMAP_PASSWORD)
        typ, _ = m.select(IMAP_FOLDER)
        if typ != "OK":
            raise RuntimeError(f"IMAP select {IMAP_FOLDER} neizdevās")
        typ, ids = m.search(None, "UNSEEN")
        uids = ids[0].split() if typ == "OK" and ids and ids[0] else []
        moved = False
        for num in uids:
            typ, data = m.fetch(num, "(RFC822)")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            raw = data[0][1]
            parsed = _parse_message(raw)
            mid = parsed.get("message_id") or f"nomid:{hash(raw)}"
            if mid in seen:
                m.store(num, "+FLAGS", "\\Seen")
                continue
            seen.add(mid)
            dom = parsed["forwarder_email"].split("@")[-1] if parsed.get("forwarder_email") else ""
            if dom not in ALLOWED_FORWARDERS:
                logger.info("partner_intake: ignorē sūtītāju %s (nav atļautajā sarakstā)", parsed.get("forwarder_email"))
                m.store(num, "+FLAGS", "\\Seen")
                continue
            if not parsed["attachments_raw"] and len(parsed.get("body", "")) < 80:
                logger.info("partner_intake: tukša vēstule no %s — izlaiž", parsed.get("forwarder_email"))
                m.store(num, "+FLAGS", "\\Seen")
                continue
            meta = _store_intake(parsed, "imap")
            out.append(meta)
            m.store(num, "+FLAGS", "\\Seen")
            # pārvieto uz Processed — TIKAI ja kopija tiešām izdevās (imaplib uz "NO" nemet kļūdu;
            # inbox.eu mapes ir tikai zem INBOX/ → bez pārbaudes vēstule tiktu izdzēsta, 2026-10-21)
            folder = _ensure_processed_folder(m)
            if folder:
                typ, _ = m.copy(num, folder)
                if typ == "OK":
                    m.store(num, "+FLAGS", "\\Deleted")
                    moved = True
                else:
                    logger.warning("partner_intake: copy uz %s neizdevās (%s) — vēstule paliek INBOX kā izlasīta", folder, typ)
        if moved:
            try:
                m.expunge()
            except Exception:
                pass
    finally:
        try:
            m.logout()
        except Exception:
            pass
    _save_seen(seen)
    return out


# ───────────────────────────── 2) AI normalizācija ─────────────────────────────

def _pdf_parts(path: Path, budget: dict) -> tuple[str, list[dict]]:
    """Teksts (pypdf) + lapu attēli (PyMuPDF). budget['images'] = atlikušās vision bildes."""
    text = ""
    try:
        from pypdf import PdfReader
        r = PdfReader(str(path))
        chunks = []
        for i, pg in enumerate(r.pages, start=1):
            try:
                t = (pg.extract_text() or "").strip()
            except Exception:
                t = ""
            if t:
                chunks.append(f"[{path.name} — {i}. lapa]\n{t}")
        text = "\n\n".join(chunks)
    except Exception as e:
        text = f"[{path.name}: teksta slānis nenolasās: {type(e).__name__}]"
    images: list[dict] = []
    try:
        import pymupdf
        with pymupdf.open(str(path)) as doc:
            for i, page in enumerate(doc):
                if i >= MAX_PDF_PAGES_PER_DOC or budget["images"] <= 0:
                    break
                rect = page.rect
                zoom = min(2.0, PAGE_MAX_SIDE_PX / (max(rect.width, rect.height) or 1.0))
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                images.append({"label": f"{path.name} — {i + 1}. lapa",
                               "data_url": "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()})
                budget["images"] -= 1
    except Exception as e:
        text += f"\n[{path.name}: lapu attēli nav: {type(e).__name__}]"
    return text, images


def _xlsx_text(path: Path) -> str:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    except Exception as e:
        return f"[{path.name}: Excel nenolasās: {type(e).__name__}]"
    names = wb.sheetnames
    # Mēnešu lapas (MBC): ņem PĒDĒJĀS 2 lapas + visas, kuru nosaukumā ir šis/nākamais gads; citādi visas (≤ 6)
    year = str(_now().year)
    pick = [n for n in names if year in n] or []
    pick = list(dict.fromkeys(pick + names[-2:])) if len(names) > 6 else names
    out = [f"[{path.name}] lapas: {', '.join(names)} — nolasītas: {', '.join(pick)}"]
    for n in pick:
        ws = wb[n]
        rows = []
        for row in ws.iter_rows(values_only=True):
            if row is None or all(v is None for v in row):
                continue
            rows.append(" | ".join("" if v is None else str(v).strip() for v in row).rstrip(" |"))
            if len(rows) >= 300:
                rows.append("… (apgriezts)")
                break
        out.append(f"--- lapa «{n}» ---\n" + "\n".join(rows))
    return "\n".join(out)


def _image_attachment(path: Path, budget: dict) -> Optional[dict]:
    if budget["images"] <= 0:
        return None
    try:
        from PIL import Image
        im = Image.open(path)
        im.thumbnail((PAGE_MAX_SIDE_PX, PAGE_MAX_SIDE_PX))
        import io
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=80)
        budget["images"] -= 1
        return {"label": path.name, "data_url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}
    except Exception:
        return None


def build_ai_input(intake_dir: Path, meta: dict) -> tuple[str, list[dict]]:
    body = (intake_dir / "body.txt").read_text(encoding="utf-8") if (intake_dir / "body.txt").is_file() else ""
    parts = [f"E-PASTA TEMATS: {meta.get('subject') or ''}",
             f"PĀRSŪTĪJA: {meta.get('forwarder_email') or ''}",
             f"ORIĢINĀLAIS SŪTĪTĀJS (ja atpazīts): {json.dumps(meta.get('orig_sender') or {}, ensure_ascii=False)}",
             "E-PASTA TEKSTS:\n" + body]
    images: list[dict] = []
    budget = {"images": MAX_VISION_IMAGES}
    for a in meta.get("attachments", []):
        p = STORAGE_ROOT / a["path"]
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext == ".pdf":
            t, imgs = _pdf_parts(p, budget)
            parts.append(f"PIELIKUMS PDF «{p.name}»:\n{t}")
            images.extend(imgs)
        elif ext in (".xlsx", ".xlsm", ".xls"):
            parts.append(f"PIELIKUMS EXCEL «{p.name}»:\n{_xlsx_text(p)}")
        elif ext in (".csv", ".txt"):
            parts.append(f"PIELIKUMS «{p.name}»:\n{p.read_text(encoding='utf-8', errors='ignore')[:20000]}")
        elif ext in (".jpg", ".jpeg", ".png", ".webp"):
            im = _image_attachment(p, budget)
            if im:
                images.append(im)
        elif ext == ".docx":
            try:
                import zipfile
                with zipfile.ZipFile(p) as z:
                    xml = z.read("word/document.xml").decode("utf-8", "ignore")
                parts.append(f"PIELIKUMS DOCX «{p.name}»:\n{_html_to_text(xml)[:20000]}")
            except Exception:
                parts.append(f"[{p.name}: docx nenolasās]")
    text = "\n\n".join(parts)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n[... teksts apgriezts ...]"
    return text, images


def _schema() -> dict:
    unit = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "ref": _nullable("string"),
            "building_part": _nullable("string"),
            "floor": _nullable("string"),
            "area_m2": _nullable("number"),
            "area_approx": {"type": "boolean"},
            "area_note": _nullable("string"),
            "space_type": _enum_nullable(SPACE_GROUPS),
            "deal": {"type": "string", "enum": ["rent", "sale"]},
            "rent_per_m2_min": _nullable("number"),
            "rent_per_m2_max": _nullable("number"),
            "rent_total": _nullable("number"),
            "sale_price": _nullable("number"),
            "maintenance_per_m2": _nullable("number"),
            "utilities_note": _nullable("string"),
            "vat_note": _nullable("string"),
            "condition": _nullable("string"),
            "available_from": _nullable("string"),
            "furnished": {"type": ["boolean", "null"]},
            "ceiling_m": _nullable("number"),
            "features": {"type": "array", "items": {"type": "string"}},
            "description_lv": {"type": "string"},
            "attachment_hint": _nullable("string"),
            "page_hint": _nullable("string"),
            "evidence": {"type": "string"},
            "confidence": {"type": "number"},
        },
    }
    unit["required"] = list(unit["properties"].keys())
    address = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "street": _nullable("string"),
            "city": _nullable("string"),
            "district": _nullable("string"),
            "building_name": _nullable("string"),
            "address_known": {"type": "boolean"},
            "units": {"type": "array", "items": unit},
        },
    }
    address["required"] = list(address["properties"].keys())
    partner = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "company": _nullable("string"), "contact_name": _nullable("string"),
            "email": _nullable("string"), "phone": _nullable("string"), "website": _nullable("string"),
        },
    }
    partner["required"] = list(partner["properties"].keys())
    skipped = {"type": "object", "additionalProperties": False,
               "properties": {"what": {"type": "string"}, "why": {"type": "string"}},
               "required": ["what", "why"]}
    top = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "is_vacancy_offer": {"type": "boolean"},
            "partner": partner,
            "list_date": _nullable("string"),
            "looks_complete": {"type": "boolean"},
            "language": {"type": "string"},
            "addresses": {"type": "array", "items": address},
            "skipped_items": {"type": "array", "items": skipped},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }
    top["required"] = list(top["properties"].keys())
    return top


PROMPT = """# PARTNERA PIEDĀVĀJUMS (e-pasts + pielikumi) → RGC telpu saraksts

Tu esi RGC (komerciālo telpu brokeri) asistents. Ieva/Raimonds pārsūtīja tev e-pastu no sadarbības partnera
(īpašnieks/pārvaldnieks/cita aģentūra) ar PIEEJAMAJĀM telpām — jebkurā formātā (teksts, PDF brošūra,
PDF tabula, Excel, plāni kā PDF/attēli). Izvade: TIKAI JSON pēc shēmas, teksti LATVISKI.

## Noteikumi
1. NEKO neizdomā; nav → null. `confidence` 0..1 katrai telpai (adrese+platība+cena+darījums droši?).
   `evidence` = īss citāts, kur ņemta platība un cena.
2. `partner`: uzņēmums, kontaktpersona, e-pasts, tālrunis — no paraksta / dokumenta / «From:» (NE no RGC pārsūtītāja).
3. `addresses`: katra atsevišķa ēka/adrese = savs ieraksts (`street` = iela + numurs tieši kā dokumentā, bez pilsētas;
   `city`; `district` Rīgas apkaime, ja minēta). Ja dokumentā ir tikai zīmols/parka nosaukums bez adreses
   (piem. «INDI centrs», «MCITY 2. ēka») → `building_name` un `address_known=false`, `street` null.
   Vairāki parki/ēkas vienā PDF → vairākas adreses. Telpas, kurām adrese nav skaidra, liec pie tuvākās sadaļas.
4. KATRA iznomājamā telpa = sava `units` rinda. `ref` = partnera telpas kods/numurs, ja ir («GD25D.E3», «33-18»,
   «C ieeja 2.st 29.8», «201-9»). `building_part` = ēka/korpuss/ieeja/sekcija (C ēka, D ieeja, 42D).
   `floor` kā «1.stāvs», «2.stāvs», «Pagrabs», «Vienstāvu» (divlīmeņu → zemākais + piezīme `area_note`).
   `area_m2` = GALVENĀ iznomājamā platība (biroja platība bez balkona/terases; ja tās ir — `area_note`:
   «+ balkons 4,4 m², terase 26 m²»). «around/aprox./~/apm.» → `area_approx=true`.
5. `deal`: rent (noma) / sale (pārdošana). Noma: `rent_per_m2_min/max` €/m² mēnesī (viena cena → abi vienādi;
   «no 10» → min 10, max null), `rent_total` = kopējā mēneša maksa, ja dokumentā («Kopējais maksājums 880 EUR»)
   vai ja viegli izrēķināma (platība × €/m²). `maintenance_per_m2` = apsaimniekošana €/m². `utilities_note` =
   komunālie/elektrība/«+10 % koplietošanas»/«viss iekļauts». `vat_note` = «+PVN»/«bez PVN»/«ar PVN».
   Cena pa TIPIEM (cenrādis «apkurināma noliktava 4,50 €/m²») → piemēro katrai atbilstošajai telpai.
6. `space_type`: Birojs, Tirdzniecība, Noliktava, Ražošana, StockOfiss (noliktava+birojs), Medicīna,
   Restorans/Cafe, Studija, Sporta zāle, Autoserviss, PVD (pārtikas ražošana), Zeme, Investīciju objekts.
   Laukumi/nojumes/stāvvietas/dzīvokļi/zeme ārzemēs — NELIEC units, bet `skipped_items` ar iemeslu.
7. `features` — īsi LV: «vārti 4×4,5 m», «rampa», «griesti 8 m», «400 A», «bez lifta», «ar mēbelēm», «terase».
   `condition` — renovēts / jāremontē / būvniecībā / kā ir. `available_from` — «tagad» vai datums.
   `furnished` — true/false/null.
8. `description_lv` — 2–5 teikumi latviski par telpu (bez kontaktiem, bez partnera zīmola, bez saukļiem).
9. `attachment_hint` — kurš pielikums/lapa attiecas uz šo telpu (plāns/foto), piem. «KV33 18.pdf» vai
   «NPP_vacancy.pdf 7.-9. lapa»; `page_hint` — lapas numuri.
10. `looks_complete` = vai dokuments izskatās kā PILNS šī partnera brīvo telpu saraksts (vakanču saraksts,
    «aktuālais piedāvājums», mēneša lapa) — true; vai tikai dažas telpas/viena brošūra — false.
11. `is_vacancy_offer=false`, ja e-pasts nav telpu piedāvājums (rēķins, sarakste, spams) — tad units tukšs.
12. `warnings` — īsi LV: kas neskaidrs (cena diapazons, platība «~», adrese nezināma, vairāki dokumenti u.c.).
"""


def analyze(intake_id: str) -> dict:
    d = INTAKE_DIR / intake_id
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    text, images = build_ai_input(d, meta)
    from openai import OpenAI
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY nav konfigurēts")
    model = os.getenv("PARTNER_INTAKE_MODEL") or os.getenv("MODEL") or "gpt-5.4"
    content: list[dict] = [
        {"type": "input_text", "text": "Izlasi VISU (e-pasta teksts + pielikumi + lapu attēli) un atgriez TIKAI JSON pēc shēmas."},
        {"type": "input_text", "text": text},
    ]
    for im in images:
        content.append({"type": "input_text", "text": f"#attēls: {im['label']}"})
        content.append({"type": "input_image", "image_url": im["data_url"], "detail": "high"})
    content.append({"type": "input_text", "text": PROMPT})
    client = OpenAI(api_key=api_key)
    t0 = time.time()
    resp = client.responses.create(
        model=model,
        input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": "vacancy_list", "schema": _schema(), "strict": True}},
    )
    data = json.loads(resp.output_text)
    data["_meta"] = {"model": model, "seconds": round(time.time() - t0, 1),
                     "text_chars": len(text), "images": len(images)}
    (d / "analysis.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


# ───────────────────────────── 3) salīdzināšana ─────────────────────────────

def _find_bp(conn, street: Optional[str], city: Optional[str], district: Optional[str],
             building_name: Optional[str]) -> Optional[dict]:
    """Ēkas profils: kanoniskā atslēga (agent_publish) → street_search → building_name → partneru reģistrs."""
    import agent_publish
    if street:
        try:
            key = agent_publish._canonical_bp_key(street, city or "Rīga", district or "")
            row = conn.execute("SELECT id, full_address, city FROM properties.building_profiles WHERE building_key = %s",
                               (key,)).fetchone()
            if row:
                return {"id": int(row["id"]), "full_address": row["full_address"], "city": row["city"], "how": "key"}
        except Exception:
            pass
        like = _street_like(street)
        if like:
            row = conn.execute(
                """SELECT building_profile_id AS id, min(street) AS full_address, min(city) AS city, count(*) n
                     FROM properties.listings
                    WHERE building_profile_id IS NOT NULL
                      AND translate(lower(street),'āēīōūčšžģķļņŗ','aeiouczsgklnr') ~ %s
                      AND (%s = '' OR lower(city) LIKE %s)
                    GROUP BY 1 ORDER BY n DESC LIMIT 1""",
                (like, (city or "").lower(), f"%{(city or '').lower()}%"),
            ).fetchone()
            if row and row["id"]:
                return {"id": int(row["id"]), "full_address": row["full_address"], "city": row["city"], "how": "street"}
    if building_name:
        row = conn.execute(
            "SELECT id, full_address, city FROM properties.building_profiles WHERE building_name ILIKE %s LIMIT 1",
            (f"%{building_name}%",)).fetchone()
        if row:
            return {"id": int(row["id"]), "full_address": row["full_address"], "city": row["city"], "how": "name"}
        try:
            row = conn.execute(
                "SELECT bp_ids FROM properties.partner_sources WHERE %s = ANY(brand_names) LIMIT 1",
                (building_name,)).fetchone()
            if row and row["bp_ids"]:
                bp = conn.execute("SELECT id, full_address, city FROM properties.building_profiles WHERE id = %s",
                                  (row["bp_ids"][0],)).fetchone()
                if bp:
                    return {"id": int(bp["id"]), "full_address": bp["full_address"], "city": bp["city"], "how": "partner"}
        except Exception:
            conn.rollback()
    return None


def _find_bps(conn, street, city, district, building_name) -> list[dict]:
    """Visi ticamie BP (DB ir dublikāti, piem. 'rupnicu|4|olaine' un 'rupnicu 4, olaine|olaine')."""
    out: list[dict] = []
    seen: set[int] = set()
    main = _find_bp(conn, street, city, district, building_name)
    if main:
        out.append(main); seen.add(main["id"])
    like = _street_like(street or "")
    if like:
        try:
            rows = conn.execute(
                """SELECT bp.id, bp.full_address, bp.city,
                          (SELECT count(*) FROM properties.listings l WHERE l.building_profile_id = bp.id AND l."Debug_status"='ok') AS n
                     FROM properties.building_profiles bp
                    WHERE translate(lower(coalesce(bp.full_address, bp.street || ' ' || coalesce(bp.house_number,''))),'āēīōūčšžģķļņŗ','aeiouczsgklnr') ~ %s
                      AND (%s = '' OR lower(coalesce(bp.city,'')) LIKE %s OR lower(coalesce(bp.full_address,'')) LIKE %s)
                    ORDER BY n DESC LIMIT 4""",
                (like, (city or "").lower(), f"%{(city or '').lower()}%", f"%{(city or '').lower()}%"),
            ).fetchall()
            for r in rows:
                if int(r["id"]) not in seen:
                    out.append({"id": int(r["id"]), "full_address": r["full_address"], "city": r["city"], "how": "street"})
                    seen.add(int(r["id"]))
        except Exception:
            conn.rollback()
    return out


def _our_listings(conn, bp_id: int) -> list[dict]:
    return conn.execute(
        """SELECT id, street, area_m2, floor, price, price_type, price_per_m2, "Space_group", source,
                  on_website, wp_post_id, wp_synced_at, created_at, occupancy_status,
                  "Apsaimniekosanas_maksa", partner_unit_ref,
                  cardinality(local_image_paths_processed) AS n_processed,
                  cardinality(local_image_paths_raw) AS n_raw
             FROM properties.listings
            WHERE building_profile_id = %s AND "Debug_status" = 'ok'
              AND occupancy_status IS DISTINCT FROM 'occupied'
              AND source = ANY(%s)
            ORDER BY id DESC""",
        (bp_id, list(OUR_SOURCES)),
    ).fetchall()


def _our_listings_safe(conn, bp_id: int) -> list[dict]:
    try:
        return _our_listings(conn, bp_id)
    except psycopg.errors.UndefinedColumn:
        conn.rollback()  # partner_unit_ref vēl nav (migrācija) → bez tā
        return conn.execute(
            """SELECT id, street, area_m2, floor, price, price_type, price_per_m2, "Space_group", source,
                      on_website, wp_post_id, wp_synced_at, created_at, occupancy_status,
                      "Apsaimniekosanas_maksa", NULL::text AS partner_unit_ref,
                      cardinality(local_image_paths_processed) AS n_processed,
                      cardinality(local_image_paths_raw) AS n_raw
                 FROM properties.listings
                WHERE building_profile_id = %s AND "Debug_status" = 'ok'
                  AND occupancy_status IS DISTINCT FROM 'occupied'
                  AND source = ANY(%s)
                ORDER BY id DESC""",
            (bp_id, list(OUR_SOURCES)),
        ).fetchall()


def _area_close(a: Optional[float], b: Optional[float], approx: bool) -> bool:
    if a is None or b is None or a <= 0 or b <= 0:
        return False
    tol = 0.15 if approx else 0.03
    return abs(a - b) <= max(1.0, tol * max(a, b))


def _inbox_candidates(conn, street: str, city: Optional[str], floor: str, area: Optional[float], approx: bool) -> list[dict]:
    """ss.lv inbox: tā pati iela + stāvs + platība → tie paši sludinājumi (foto avots)."""
    like = _street_like(street)
    if not like or area is None:
        return []
    rows = conn.execute(
        """SELECT id, street, area_m2, floor, price, link, is_agent, date_posted, last_updated, skip_reason
             FROM properties.scrape_inbox
            WHERE translate(lower(street),'āēīōūčšžģķļņŗ','aeiouczsgklnr') ~ %s
              AND (%s = '' OR lower(coalesce(city,'')) LIKE %s)
              AND link IS NOT NULL
            ORDER BY last_updated DESC NULLS LAST LIMIT 300""",
        (like, (city or "").lower(), f"%{(city or '').lower()}%"),
    ).fetchall()
    out = []
    fd = _floor_digits(floor)
    for r in rows:
        ra = _num(r["area_m2"])
        if not _area_close(area, ra, True if approx else False) and not (ra and abs(area - ra) <= max(2.0, 0.05 * area)):
            continue
        rf = _floor_digits(r["floor"])
        floor_ok = (not fd) or (not rf) or fd == rf
        score = 0.0
        if ra:
            score = 1.0 - min(1.0, abs(area - ra) / max(area, 1.0))
        if floor_ok and fd and rf:
            score += 0.3
        out.append({"inbox_id": int(r["id"]), "street": r["street"], "area_m2": r["area_m2"], "floor": r["floor"],
                    "price": r["price"], "link": r["link"], "is_agent": str(r["is_agent"]).lower() == "true",
                    "last_updated": str(r["last_updated"])[:10] if r["last_updated"] else None,
                    "same_room_score": round(score, 2), "floor_ok": floor_ok})
    out.sort(key=lambda x: (-x["same_room_score"], x["last_updated"] or ""), reverse=False)
    out.sort(key=lambda x: x["same_room_score"], reverse=True)
    return out[:4]


def _pdf_images_for_unit(intake_dir: Path, meta: dict, unit: dict) -> dict:
    """Iegultie attēli no pielikuma lapām, uz ko norāda attachment_hint/page_hint (vai mazs pielikums = plāns)."""
    hint = (unit.get("attachment_hint") or "") + " " + (unit.get("page_hint") or "")
    out = {"photos": [], "plans": []}
    if not meta.get("attachments"):
        return out
    try:
        import pymupdf
    except Exception:
        return out
    pages_wanted = {int(x) for x in re.findall(r"\b(\d{1,3})\b", unit.get("page_hint") or "")}
    img_dir = intake_dir / "img" / str(unit["_idx"])
    for a in meta["attachments"]:
        p = STORAGE_ROOT / a["path"]
        if p.suffix.lower() != ".pdf" or not p.is_file():
            continue
        named = a["name"].lower() in hint.lower() or Path(a["name"]).stem.lower() in hint.lower()
        try:
            doc = pymupdf.open(str(p))
        except Exception:
            continue
        single_plan_doc = len(doc) == 1 and named
        for i, page in enumerate(doc):
            pno = i + 1
            if not (named and (not pages_wanted or pno in pages_wanted or single_plan_doc)):
                continue
            img_dir.mkdir(parents=True, exist_ok=True)
            text = (page.get_text() or "").lower()
            is_plan_page = single_plan_doc or any(k in text for k in ("plān", "plan", "layout"))
            got = 0
            for im in page.get_images(full=True):
                xref, w, h = im[0], im[2], im[3]
                if w < 500 or h < 350:
                    continue
                try:
                    pix = pymupdf.Pixmap(doc, xref)
                    if pix.n - pix.alpha >= 4:
                        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                    fn = img_dir / f"{Path(a['name']).stem}_p{pno}_{xref}.png"
                    pix.save(str(fn))
                    rel = str(fn.relative_to(STORAGE_ROOT)).replace("\\", "/")
                    (out["plans"] if (single_plan_doc or (is_plan_page and w < h)) else out["photos"]).append(rel)
                    got += 1
                except Exception:
                    continue
            if got == 0 and is_plan_page:
                # vektoru plāns (PEBO) → renderē lapu
                try:
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                    fn = img_dir / f"{Path(a['name']).stem}_p{pno}_render.png"
                    pix.save(str(fn))
                    out["plans"].append(str(fn.relative_to(STORAGE_ROOT)).replace("\\", "/"))
                except Exception:
                    pass
        doc.close()
    return out


def reconcile(intake_id: str, analysis: Optional[dict] = None) -> dict:
    d = INTAKE_DIR / intake_id
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    if analysis is None:
        analysis = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
    report: dict[str, Any] = {
        "intake_id": intake_id, "received_at": meta.get("received_at"),
        "forwarder": meta.get("forwarder_email"), "subject": meta.get("subject"),
        "partner": analysis.get("partner") or {}, "list_date": analysis.get("list_date"),
        "looks_complete": bool(analysis.get("looks_complete")),
        "is_vacancy_offer": bool(analysis.get("is_vacancy_offer", True)),
        "warnings": list(analysis.get("warnings") or []),
        "skipped_items": analysis.get("skipped_items") or [],
        "addresses": [], "summary": {"new": 0, "match": 0, "ambiguous": 0, "missing": 0, "needs_review": 0,
                                     "ready": 0, "units": 0},
        "generated_at": _now().isoformat(),
    }
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL nav")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        idx = 0
        for addr in analysis.get("addresses") or []:
            bps = _find_bps(conn, addr.get("street"), addr.get("city"), addr.get("district"), addr.get("building_name"))
            ours = []
            per_bp = []
            for b in bps:
                ls = _our_listings_safe(conn, b["id"])
                per_bp.append((len(ls), b))
                ours.extend(ls)
            per_bp.sort(key=lambda x: -x[0])
            bp = per_bp[0][1] if per_bp else None
            if bp:
                bp = {**bp, "alternatives": [{**b, "our_count": n} for n, b in per_bp[1:]]}
            matched_ids: set[int] = set()
            a_out = {"street": addr.get("street"), "city": addr.get("city") or "Rīga", "district": addr.get("district"),
                     "building_name": addr.get("building_name"), "address_known": bool(addr.get("address_known", True)),
                     "bp": bp, "new_building": bp is None, "our_count": len(ours), "units": [], "missing_in_list": []}
            for u in addr.get("units") or []:
                idx += 1
                u = dict(u)
                u["_idx"] = idx
                area = _num(u.get("area_m2"))
                floor = _floor_norm(u.get("floor"))
                fd = _floor_digits(floor)
                approx = bool(u.get("area_approx"))
                cands = []
                for l in ours:
                    if l.get("partner_unit_ref") and u.get("ref") and l["partner_unit_ref"].strip().lower() == str(u["ref"]).strip().lower():
                        cands = [l]
                        break
                    la = _num(l.get("area_m2"))
                    if not _area_close(area, la, approx):
                        continue
                    lf = _floor_digits(l.get("floor"))
                    if fd and lf and fd != lf:
                        continue
                    cands.append(l)
                status = "new"
                match = None
                diff = {}
                if len(cands) == 1:
                    status = "match"
                    match = cands[0]
                    matched_ids.add(int(match["id"]))
                    new_rate = u.get("rent_per_m2_min")
                    old_rate = _num(match.get("price_per_m2"))
                    if new_rate is not None and old_rate is not None and abs(new_rate - old_rate) > 0.05:
                        diff["price_per_m2"] = {"old": old_rate, "new": new_rate}
                    new_total = u.get("rent_total") or (area * new_rate if (area and new_rate) else None)
                    old_total = _num(match.get("price"))
                    if new_total and old_total and abs(new_total - old_total) > max(5.0, 0.02 * old_total):
                        diff["price"] = {"old": old_total, "new": round(new_total, 2)}
                elif len(cands) > 1:
                    status = "ambiguous"
                images = {"pdf": _pdf_images_for_unit(d, meta, u) if addr.get("street") or addr.get("building_name") else {"photos": [], "plans": []},
                          "own_listings_with_images": [int(l["id"]) for l in ours if (l.get("n_processed") or 0) + (l.get("n_raw") or 0) > 0][:5],
                          "sslv_inbox": _inbox_candidates(conn, addr.get("street") or "", addr.get("city"), floor, area, approx)
                          if addr.get("street") else []}
                has_photo = bool(images["pdf"]["photos"]) or bool(images["sslv_inbox"]) or (match and ((match.get("n_processed") or 0) + (match.get("n_raw") or 0) > 0))
                conf = float(u.get("confidence") or 0)
                needs_review = (status == "ambiguous" or conf < 0.6 or not u.get("space_type")
                                or (u.get("deal") == "rent" and u.get("rent_per_m2_min") is None and u.get("rent_total") is None)
                                or not a_out["address_known"])
                decision = ("review" if needs_review else ("update" if (status == "match" and diff) else
                            ("keep" if status == "match" else "create")))
                unit_out = {k: v for k, v in u.items() if not k.startswith("_")}
                unit_out.update({"idx": idx, "floor_norm": floor, "status": status, "decision": decision,
                                 "matched_listing": {k: (str(v) if isinstance(v, datetime) else v)
                                                     for k, v in match.items()} if match else None,
                                 "candidates": [int(c["id"]) for c in cands] if status == "ambiguous" else [],
                                 "diff": diff, "images": images, "has_photo_source": has_photo,
                                 "needs_review": needs_review})
                a_out["units"].append(unit_out)
                report["summary"]["units"] += 1
                report["summary"][status if status in ("new", "match", "ambiguous") else "new"] += 1
                if needs_review:
                    report["summary"]["needs_review"] += 1
                elif status == "new":
                    report["summary"]["ready"] += 1
            if report["looks_complete"] and bp:
                for l in ours:
                    if int(l["id"]) in matched_ids:
                        continue
                    a_out["missing_in_list"].append({
                        "id": int(l["id"]), "street": l["street"], "area_m2": l["area_m2"], "floor": l["floor"],
                        "price": l["price"], "source": l["source"], "on_website": l["on_website"],
                        "wp_synced_at": str(l["wp_synced_at"])[:10] if l["wp_synced_at"] else None,
                        "created_at": str(l["created_at"])[:10] if l["created_at"] else None,
                        "decision": "ask",
                    })
                report["summary"]["missing"] += len(a_out["missing_in_list"])
            report["addresses"].append(a_out)
    (d / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    meta["status"] = "ready"
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


# ───────────────────────────── 4) Telegram ─────────────────────────────

def _telegram_send(chat_id: str, html: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return False
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": html, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=15)
        return r.ok
    except Exception:
        return False


def notify(report: dict) -> int:
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not DATABASE_URL:
        return 0
    s = report["summary"]
    partner = (report.get("partner") or {}).get("company") or (report.get("partner") or {}).get("email") or "partneris"
    addrs = ", ".join(f"{a.get('street') or a.get('building_name') or '?'}" for a in report["addresses"][:4])
    link = f"{PANEL_URL}/partner-intake/{report['intake_id']}"
    if not report.get("is_vacancy_offer") or s["units"] == 0:
        html = (f"⚠ <b>AI nesaprata piedāvājumu</b> — {html_mod.escape(str(report.get('subject') or ''))}\n"
                f"Nav atrastas telpas. Paskaties pats: {link}")
    else:
        html = (f"🟢 <b>AI sagatavoja: {html_mod.escape(str(partner))}</b> — {s['units']} telpas · {html_mod.escape(addrs)}\n"
                f"✅ gatavas: {s['ready']}   ⚠ pārbaudīt: {s['needs_review']}   🔁 sakrīt: {s['match']}"
                f"   ✖ mūsu nav sarakstā: {s['missing']}\n{link}")
    sent = 0
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        subs = conn.execute("SELECT agent_email, chat_id FROM properties.telegram_subscribers WHERE active IS TRUE AND chat_id IS NOT NULL").fetchall()
    fwd = (report.get("forwarder") or "").lower()
    targets = [r for r in subs if (r["agent_email"] or "").lower() == fwd] or subs
    for r in targets:
        if _telegram_send(str(r["chat_id"]), html):
            sent += 1
    return sent


# ───────────────────────────── 5) apstiprināšana → DB ─────────────────────────────

class ApplyError(ValueError):
    pass


def _partner_locked(unit: dict) -> list[str]:
    """Lock TIKAI to, ko partneris tiešām iedeva (cena/platība/stāvs/tips/apraksts + skaidri
    minētās pazīmes). Pārējo (ēkas tips/klase, stāvoklis, logi, apkure, Potential_space_group…)
    aizpilda ss.lv AI analizators no bildēm — importa FULL lock to bloķēja (2026-10-23)."""
    import agent_publish
    locked = list(agent_publish.EASY_LOCKED_FIELDS) + ["phone_numbers"]
    # Investiciju_strategija: AI parastai nomai mēdz likt «Value-Add» (tests 111701) → lock, ja mēs to noteicām
    for k in ("Balkons_check", "Treifelis_Pacelajs", "Space_condition", "Mebeleta_telpa", "Griestu_augstums", "Investiciju_strategija"):
        if unit.get(k):
            locked.append(k)
    return locked


def _derived_fields(u: dict) -> dict:
    """AI teksta pazīmes → listinga enum lauki (DB enum vērtības verificētas 2026-10-23).
    Liek TIKAI, ja pazīme skaidri minēta — nezināmais paliek NULL (AI/aģents var papildināt)."""
    out: dict = {}
    feats = " | ".join(str(x) for x in (u.get("features") or [])).lower()
    text = (feats + " | " + str(u.get("condition") or "") + " | " + str(u.get("description_lv") or "")).lower()
    if re.search(r"balkon|terase|terasē|lodžij", feats):
        out["Balkons_check"] = "checked"
    if re.search(r"bez lift", text):
        out["Treifelis_Pacelajs"] = "not checked"
    elif re.search(r"(^|[^a-zāēīū])lift(s|u|a|ā)?([^a-zāēīū]|$)", feats):
        out["Treifelis_Pacelajs"] = "checked"
    cond = str(u.get("condition") or "").lower()
    if cond:
        if re.search(r"kapitāl", cond):
            out["Space_condition"] = "Kapitālais remonts"
        elif re.search(r"nepieciešams remont|jāremontē|bez remonta|remontējam", cond):
            out["Space_condition"] = "Nepieciešams remonts"
        elif re.search(r"nepabeigt|pelēk|white box|baltā apdare", cond):
            out["Space_condition"] = "Nepabeigts"
        elif re.search(r"jaun|renovēt|pēc remonta|svaig", cond):
            out["Space_condition"] = "Jauns"
        elif re.search(r"lab|kopt|kosmētisk", cond):
            out["Space_condition"] = "Labs"
    if (u.get("space_type") or "") != "Investīciju objekts":
        out["Investiciju_strategija"] = "Nav investīciju objekts"
    return out


def _unit_to_anketa(u: dict, addr: dict, partner: dict, list_date: Optional[str]) -> dict:
    area = _num(u.get("area_m2"))
    rate = u.get("rent_per_m2_min")
    deal = u.get("deal") or "rent"
    price = None
    if deal == "rent":
        price = u.get("rent_total") or (round(area * rate, 2) if (area and rate) else None)
    else:
        price = u.get("sale_price")
    extra_bits = []
    if u.get("building_part"):
        extra_bits.append(str(u["building_part"]))
    if u.get("ref"):
        extra_bits.append(f"telpa {u['ref']}")
    if u.get("area_note"):
        extra_bits.append(str(u["area_note"]))
    if u.get("rent_per_m2_max") and u.get("rent_per_m2_min") and u["rent_per_m2_max"] != u["rent_per_m2_min"]:
        extra_bits.append(f"nomas maksa {u['rent_per_m2_min']}–{u['rent_per_m2_max']} €/m²")
    if u.get("available_from"):
        extra_bits.append(f"pieejams no {u['available_from']}")
    if u.get("condition"):
        extra_bits.append(str(u["condition"]))
    # pazīmes bez atkārtojumiem (stāvs jau ir laukā; «balkons» jau ir «+ balkons 4,40 m²»)
    for f in (u.get("features") or []):
        fl = str(f).strip()
        if not fl or re.fullmatch(r"\d+\.?\s*stāvs", fl.lower()):
            continue
        if fl.lower() in " ".join(extra_bits).lower():
            continue
        extra_bits.append(fl)
    papildu = "; ".join(x for x in [u.get("utilities_note"), u.get("vat_note")] if x)
    unit = {
        "space_group": u.get("space_type") or "Birojs",
        "potential_space_groups": [],
        "area_m2": _numstr(area),
        "floor": _floor_norm(u.get("floor")),
        "price": _numstr(price),
        "price_per_m2": _numstr(rate) if deal == "rent" else "",
        "price_type": "monthly" if deal == "rent" else "regular",
        "Apsaimniekosanas_maksa": (f"{_numstr(u['maintenance_per_m2'])} €/m²" if u.get("maintenance_per_m2") is not None else ""),
        "Papildu_maksas": papildu,
        "price_includes": [],
        "is_project": False,
        "project_completion": "",
        "images": [],
        "agent_wp_id": None,
        "Space_condition": None,
        "Griestu_augstums": (f"{_numstr(u['ceiling_m'])} m" if u.get("ceiling_m") else ""),
        "Mebeleta_telpa": "Jā" if u.get("furnished") is True else ("Nē" if u.get("furnished") is False else None),
        "Agent_comment": (u.get("description_lv") or "").strip(),
        "Agent_text_extra": ", ".join(extra_bits)[:900],
        "buildings": [], "investment_income": "", "investment_yield": "",
    }
    unit.update(_derived_fields(u))   # _insert_listing (import = full režīms) tos ieraksta
    if unit["space_group"] == "Zeme" and area:
        unit["Zemes_gabals_m2"] = _numstr(area)
    building = {
        "existing_bp_id": (addr.get("bp") or {}).get("id"),
        "street": addr.get("street") or "",
        "city": addr.get("city") or "Rīga",
        "district": addr.get("district") or "",
        "building_name": addr.get("building_name") or "",
    }
    return {"building": building, "unit": unit}


def _guess_district(conn, street: Optional[str], city: Optional[str], bp_id: Optional[int]) -> str:
    """Rajons, ja partneris to nenorāda (bieži): 1) ēkas profils; 2) mūsu listingi tajā ēkā;
    3) citi listingi TAJĀ PAŠĀ mājā (iela + numurs); 4) iela bez numura — TIKAI ja ≥75 % ielas listingu
    ir vienā rajonā (garām ielām kā Brīvības gatve minējums būtu nepareizs → labāk tukšs).
    Tukšs rajons = listings neparādās lokācijas filtros/match (PEBO 2026-10-23)."""
    def _canon(d: str) -> str:
        d = (d or "").strip()
        return d[:1].upper() + d[1:] if d else ""
    try:
        if bp_id:
            r = conn.execute("SELECT district FROM properties.building_profiles WHERE id=%s", (bp_id,)).fetchone()
            if r and (r["district"] or "").strip():
                return _canon(r["district"])
            r = conn.execute("""SELECT lower(btrim(district)) d, count(*) n FROM properties.listings
                                WHERE building_profile_id=%s AND coalesce(btrim(district),'') <> ''
                                GROUP BY 1 ORDER BY n DESC LIMIT 1""", (bp_id,)).fetchone()
            if r:
                return _canon(r["d"])
        city_sql = "translate(lower(coalesce(city,'')),'āēīōūčšžģķļņŗ','aeiouczsgklnr') = translate(lower(%s),'āēīōūčšžģķļņŗ','aeiouczsgklnr')"
        street_sql = "translate(lower(street),'āēīōūčšžģķļņŗ','aeiouczsgklnr') ~ %s"
        full = _street_like(street or "")
        if full and any(ch.isdigit() for ch in (street or "")):
            r = conn.execute(f"""SELECT lower(btrim(district)) d, count(*) n FROM properties.listings
                                 WHERE coalesce(btrim(district),'') <> '' AND {city_sql} AND {street_sql}
                                 GROUP BY 1 ORDER BY n DESC LIMIT 1""", (city or "Rīga", full)).fetchone()
            if r:
                return _canon(r["d"])
        toks = [t for t in _street_key(street or "").split() if not any(ch.isdigit() for ch in t)]
        if toks:
            rows = conn.execute(f"""SELECT lower(btrim(district)) d, count(*) n FROM properties.listings
                                    WHERE coalesce(btrim(district),'') <> '' AND {city_sql} AND {street_sql}
                                    GROUP BY 1 ORDER BY n DESC""", (city or "Rīga", _street_like(" ".join(toks)))).fetchall()
            total = sum(int(r["n"]) for r in rows)
            if rows and total >= 3 and int(rows[0]["n"]) / total >= 0.75:
                return _canon(rows[0]["d"])
    except Exception:
        conn.rollback()
    return ""


def apply(intake_id: str, decisions: dict, requested_by: Optional[str], wp_user_id: int = 0,
          dry_run: bool = False) -> dict:
    """decisions = {"units": {"<idx>": "create"|"update"|"skip"|"keep"}, "missing": {"<listing_id>": "occupied"|"skip"},
                    "partner": {"complete": bool}}"""
    import agent_publish
    d = INTAKE_DIR / intake_id
    report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    partner = report.get("partner") or {}
    result = {"created": [], "updated": [], "occupied": [], "errors": [], "images": [], "created_units": []}
    # Atkārtota apstiprināšana (iepriekš bija kļūdas → status «partial»): jau izdarīto NEatkārto.
    prev = (json.loads((d / "meta.json").read_text(encoding="utf-8")).get("apply_result") or {}) if dry_run is False else {}
    prev_created_idx = {str(p[1]) for p in (prev.get("created_units") or [])}
    prev_occupied = {int(x) for x in (prev.get("occupied") or [])}
    image_jobs: list = []
    unit_dec = decisions.get("units") or {}
    web_dec = decisions.get("web") or {}   # {idx: true} — «Arī mājaslapā» (pēc bilžu apstrādes)
    miss_dec = decisions.get("missing") or {}
    addr_dec = decisions.get("addresses") or {}
    unit_fields = decisions.get("unit_fields") or {}   # {idx: {space_type: ...}} — aģenta labojumi
    for addr in report["addresses"]:
        for u in addr["units"]:
            ov = unit_fields.get(str(u["idx"])) or {}
            if ov.get("space_type") in SPACE_GROUPS:
                u["space_type"] = ov["space_type"]
    for i, addr in enumerate(report["addresses"]):
        ov = addr_dec.get(str(i)) or {}
        if ov.get("bp_id"):
            addr["bp"] = {**(addr.get("bp") or {}), "id": int(ov["bp_id"]), "how": "agent"}
        if ov.get("street"):
            addr["street"] = ov["street"]
        if ov.get("city"):
            addr["city"] = ov["city"]
    if dry_run:
        payloads = []
        for addr in report["addresses"]:
            for u in addr["units"]:
                dec = unit_dec.get(str(u["idx"]), u.get("decision"))
                if dec == "create":
                    payloads.append({"idx": u["idx"], **_unit_to_anketa(u, addr, partner, report.get("list_date"))})
        return {"dry_run": True, "payloads": payloads,
                "would_update": [u["matched_listing"]["id"] for a in report["addresses"] for u in a["units"]
                                 if unit_dec.get(str(u["idx"]), u.get("decision")) == "update" and u.get("matched_listing")],
                "would_occupy": [int(k) for k, v in miss_dec.items() if v == "occupied"]}
    partner_id = None
    list_id = None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        # partneru reģistrs (best-effort — tabulas pēc migrācijas)
        try:
            dom = ((partner.get("email") or "").split("@")[-1] or "").lower() or None
            row = None
            if dom:
                row = conn.execute("SELECT id FROM properties.partner_sources WHERE email_domain = %s", (dom,)).fetchone()
            if not row and partner.get("company"):
                row = conn.execute("SELECT id FROM properties.partner_sources WHERE lower(name) = lower(%s)", (partner["company"],)).fetchone()
            if row:
                partner_id = int(row["id"])
                conn.execute("""UPDATE properties.partner_sources SET contact_email = COALESCE(%s, contact_email),
                                contact_name = COALESCE(%s, contact_name), phone = COALESCE(%s, phone),
                                list_complete = COALESCE(%s, list_complete), last_list_at = now(), updated_at = now() WHERE id = %s""",
                             (partner.get("email"), partner.get("contact_name"), partner.get("phone"),
                              (decisions.get("partner") or {}).get("complete"), partner_id))
            else:
                r = conn.execute("""INSERT INTO properties.partner_sources (name, email_domain, contact_email, contact_name, phone, list_complete, owner_agent, last_list_at)
                                    VALUES (%s,%s,%s,%s,%s,%s,%s, now()) RETURNING id""",
                                 (partner.get("company") or dom or "Partneris", dom, partner.get("email"), partner.get("contact_name"),
                                  partner.get("phone"), bool((decisions.get("partner") or {}).get("complete", report.get("looks_complete"))), requested_by)).fetchone()
                partner_id = int(r["id"])
            r = conn.execute("""INSERT INTO properties.partner_lists (partner_id, intake_id, received_at, list_date, forwarder, files, report, status)
                                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,'approved') ON CONFLICT (intake_id) DO UPDATE SET status='approved', applied_at=now()
                                RETURNING id""",
                             (partner_id, intake_id, report.get("received_at"), report.get("list_date"), report.get("forwarder"),
                              json.dumps([a["name"] for a in json.loads((d / "meta.json").read_text(encoding="utf-8")).get("attachments", [])]),
                              json.dumps(report, ensure_ascii=False, default=str))).fetchone()
            list_id = int(r["id"])
            conn.commit()
        except Exception as e:
            conn.rollback()
            result["errors"].append(f"partneru reģistrs: {type(e).__name__} (migrācija?) — turpinu bez tā")
            partner_id, list_id = None, None

        for addr in report["addresses"]:
            for u in addr["units"]:
                dec = unit_dec.get(str(u["idx"]), u.get("decision"))
                if dec == "create" and str(u["idx"]) in prev_created_idx:
                    continue  # jau izveidots iepriekšējā apstiprināšanā
                if dec == "create":
                    try:
                        if not (addr.get("district") or "").strip():
                            addr["district"] = _guess_district(conn, addr.get("street"), addr.get("city"),
                                                               (addr.get("bp") or {}).get("id"))
                        payload = _unit_to_anketa(u, addr, partner, report.get("list_date"))
                        res = agent_publish.publish_anketa({
                            "mode": "full", "wp_user_id": wp_user_id, "requested_by_email": requested_by,
                            "building": payload["building"], "units": [payload["unit"]],
                            "intent": "import", "source": "agent_partner",
                            "import_file_name": None, "import_token": None,
                        })
                        lid = int(res["queued"][0]["listing_id"])
                        result["created"].append(lid)
                        try:
                            conn.execute("""UPDATE properties.listings SET partner_id=%s, partner_unit_ref=%s, partner_list_id=%s,
                                            "Debug_note" = %s,
                                            "Agent_text_extra" = COALESCE(NULLIF(%s, ''), "Agent_text_extra"),
                                            agent_locked_fields = %s WHERE id=%s""",
                                         (partner_id, u.get("ref"), list_id,
                                          f"Partneris: {partner.get('company') or partner.get('email') or '?'}; saraksts {report.get('list_date') or report.get('received_at','')[:10]}; ref {u.get('ref') or '-'}",
                                          payload["unit"].get("Agent_text_extra") or "", _partner_locked(payload["unit"]), lid))
                            conn.commit()
                        except Exception:
                            conn.rollback()
                        image_jobs.append((lid, u, bool(web_dec.get(str(u["idx"])))))
                        result["created_units"].append([lid, u["idx"]])
                    except agent_publish.DuplicateListingError as e:
                        result["errors"].append(f"telpa #{u['idx']}: {e}")
                    except Exception as e:
                        result["errors"].append(f"telpa #{u['idx']}: {type(e).__name__}: {str(e)[:200]}")
                elif dec == "update" and u.get("matched_listing"):
                    lid = int(u["matched_listing"]["id"])
                    diff = u.get("diff") or {}
                    sets, vals = [], []
                    if "price" in diff:
                        sets.append("price = %s"); vals.append(_numstr(diff["price"]["new"]))
                    if "price_per_m2" in diff:
                        sets.append("price_per_m2 = %s"); vals.append(_numstr(diff["price_per_m2"]["new"]))
                    if sets:
                        sets.append("""agent_locked_fields = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(COALESCE(agent_locked_fields,'{}'::text[]) || ARRAY['price','price_per_m2']) x))""")
                        sets.append("updated_at = now()")
                        try:
                            conn.execute(f"UPDATE properties.listings SET {', '.join(sets)} WHERE id = %s", (*vals, lid))
                            try:
                                conn.execute("UPDATE properties.listings SET partner_id=COALESCE(%s,partner_id), partner_unit_ref=COALESCE(%s,partner_unit_ref), partner_list_id=%s WHERE id=%s",
                                             (partner_id, u.get("ref"), list_id, lid))
                            except Exception:
                                pass
                            if u["matched_listing"].get("wp_post_id"):
                                conn.execute("""INSERT INTO properties.wp_export_queue (listing_id, status, requested_by)
                                                VALUES (%s,'pending',%s) ON CONFLICT (listing_id) WHERE status IN ('pending','processing') DO NOTHING""",
                                             (lid, requested_by))
                            conn.commit()
                            result["updated"].append(lid)
                        except Exception as e:
                            conn.rollback()
                            result["errors"].append(f"atjaunot #{lid}: {type(e).__name__}: {str(e)[:150]}")
            for m in addr.get("missing_in_list") or []:
                if miss_dec.get(str(m["id"])) == "occupied" and int(m["id"]) not in prev_occupied:
                    lid = int(m["id"])
                    try:
                        conn.execute("""UPDATE properties.listings SET occupancy_status='occupied', occupied_from=CURRENT_DATE,
                                        occupied_note=%s, occupancy_set_by=%s, occupancy_set_at=now(), updated_at=now() WHERE id=%s""",
                                     (f"Nav partnera ({partner.get('company') or '?'}) vakanču sarakstā {report.get('list_date') or report.get('received_at','')[:10]}",
                                      requested_by, lid))
                        if m.get("on_website"):
                            conn.execute("""INSERT INTO properties.wp_export_queue (listing_id, action, status, requested_by)
                                            VALUES (%s,'unpublish','pending',%s)""", (lid, requested_by))
                        conn.commit()
                        result["occupied"].append(lid)
                    except Exception as e:
                        conn.rollback()
                        result["errors"].append(f"iznomāts #{lid}: {type(e).__name__}: {str(e)[:150]}")
    meta_p = d / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    if prev:
        meta.setdefault("apply_history", []).append(prev)
        for k in ("created", "updated", "occupied", "created_units"):
            result[k] = list(prev.get(k) or []) + [x for x in result[k] if x not in (prev.get(k) or [])]
    # kļūdas → «partial»: panelis ļauj labot un apstiprināt vēlreiz (izveidotie netiek dublēti)
    meta["status"] = "partial" if result["errors"] else "applied"
    meta["applied_at"] = _now().isoformat()
    meta["apply_result"] = result
    meta["images_status"] = "processing" if image_jobs else None
    meta["image_jobs"] = [[j[0], j[1].get("idx"), bool(j[2])] for j in image_jobs]
    meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    # bildes (ss.lv lejupielāde + Seedream) aizņem minūtes → fonā, lai apstiprināšana atbild uzreiz
    start_images_job(intake_id, image_jobs)
    result["images_status"] = "processing" if image_jobs else None
    return result


CLEAN_PREFIX = "img_user_"   # tīri avoti (PDF plāni/foto, mūsu apstrādātā fasāde) → ai_ready bez Seedream
SSLV_MAX_PHOTOS = int(os.getenv("PARTNER_SSLV_MAX_PHOTOS", "15"))


def _attach_images(conn, listing_id: int, u: dict, intake_id: str) -> dict:
    """Bilžu kaskāde pēc INSERT (Raimonds 2026-10-21: «bildes iet kopā un iziet caur AI»).

    VISAS atrastās bildes apvieno VIENĀ listinga galerijā:
      1) ss.lv «tā pati telpa» → galerija lejupielādēta UZREIZ uz raw/ (img_001.jpg…);
      2) PDF foto + plāni → raw/img_user_* (tīri);
      3) ja nav neviena foto → mūsu listinga fasāde no tās pašas ēkas.
    local_image_paths_raw = foto → fasāde → plāni (plāns pēdējais); images_downloaded_at=now(),
    lai image_download_poller NEpārraksta sarakstu ar tikai ss.lv bildēm (agrāk tā plāns pazuda).
    Pēc tam `_process_images`: klasifikācija + ai_ready — ss.lv bildes caur Seedream
    (ūdenszīmes), tīrās kopē tieši → local_image_paths_processed (publish vārts izpildīts)."""
    import image_classify
    out: dict = {"listing_id": listing_id, "pdf": 0, "plans": 0, "sslv": 0, "own": 0}
    ldir = STORAGE_ROOT / "listings" / str(listing_id)
    raw_dir = ldir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    photos: list[str] = []
    plans: list[str] = []
    facade: list[str] = []
    types: dict[str, str] = {}
    imgs = (u.get("images") or {}).get("pdf") or {}

    # 1) ss.lv — vispirms (download_images var tīrīt img_*.* mapē, ja mainās URL saraksts)
    sslv = (u.get("images") or {}).get("sslv_inbox") or []
    for cand in sslv[:2]:
        try:
            import ai_text_helpers as helpers
            import download_images
            import sslv_import
            info = sslv_import.parse_sslv_url(cand["link"])
            html_doc = sslv_import._fetch_html(info["fetch_url"], helpers.HEADERS, helpers.REQUEST_TIMEOUT)
            gallery = (helpers.extract_gallery_urls(html_doc) or [])[:SSLV_MAX_PHOTOS]
            if not gallery:
                continue
            jpg_field = sslv_import._format_jpg_field(gallery)
            download_images.STORAGE_ROOT = STORAGE_ROOT
            res = download_images.process_listing(conn, listing_id, jpg_field)
            got = res.get("saved_paths") or []
            if not got:
                continue
            photos.extend(got)
            conn.execute('UPDATE properties.listings SET "JPG bildes" = %s WHERE id = %s', (jpg_field, listing_id))
            conn.commit()
            out["sslv"] = len(got)
            out["sslv_link"] = cand["link"]
            break
        except Exception as e:
            conn.rollback()
            out["sslv_error"] = f"{type(e).__name__}: {str(e)[:120]}"

    # 2) PDF foto + plāni (tīri)
    for kind in ("photos", "plans"):
        for rel in imgs.get(kind, []):
            src = STORAGE_ROOT / rel
            if not src.is_file():
                continue
            fn = f"{CLEAN_PREFIX}{uuid.uuid4().hex}{src.suffix.lower() or '.png'}"
            shutil.copy2(src, raw_dir / fn)
            p = f"listings/{listing_id}/raw/{fn}"
            if kind == "plans":
                plans.append(p)
                types[fn] = "plans"
                out["plans"] += 1
            else:
                photos.append(p)
                out["pdf"] += 1

    # 3) fasāde no mūsu listinga tajā pašā ēkā — tikai ja nav neviena foto
    own = (u.get("images") or {}).get("own_listings_with_images") or []
    if not photos and own:
        try:
            row = conn.execute("SELECT local_image_paths_processed, local_image_paths_raw FROM properties.listings WHERE id=%s",
                               (own[0],)).fetchone()
            clean = bool(row and row["local_image_paths_processed"])
            arr = (row["local_image_paths_processed"] or row["local_image_paths_raw"] or []) if row else []
            if arr:
                rel = str(arr[0]).replace("\\", "/")
                rel = rel[rel.index("listings/"):] if "listings/" in rel else rel
                src = STORAGE_ROOT / rel
                if src.is_file():
                    # apstrādāta → tīra; raw (ss.lv ūdenszīme) → img_fac_* iet caur Seedream
                    fn = f"{CLEAN_PREFIX if clean else 'img_fac_'}{uuid.uuid4().hex}{src.suffix.lower() or '.jpg'}"
                    shutil.copy2(src, raw_dir / fn)
                    facade.append(f"listings/{listing_id}/raw/{fn}")
                    types[fn] = "fasade"
                    out["own"] = 1
        except Exception:
            conn.rollback()

    paths = photos + facade + plans
    if paths:
        try:
            conn.execute("""UPDATE properties.listings
                            SET local_image_paths_raw = %s::text[], images_downloaded_at = now()
                            WHERE id = %s""", (paths, listing_id))
            conn.commit()
        except Exception as e:
            conn.rollback()
            out["db_error"] = f"{type(e).__name__}"
        if types:
            try:
                man = image_classify.load_manifest(STORAGE_ROOT, listing_id)
                man = man if isinstance(man, dict) else {}
                for fn, t in types.items():
                    man[fn] = {"type": t, "quality": "good_for_website", "reason": "partneru ieplūde", "filename": fn}
                image_classify.save_manifest(STORAGE_ROOT, listing_id, man)
            except Exception:
                pass
    out["raw_paths"] = len(paths)
    return out


def _process_images(listing_id: int) -> dict:
    """AI posms vienam listingam. Klasificē neklasificētās (ss.lv) bildes, tad ai_ready:
    img_user_* → kopija, pārējās → Seedream + ūdenszīmes pārbaude. Raksta
    local_image_paths_processed tajā pašā secībā kā raw (foto → fasāde → plāni).
    Idempotents (esošos ai_ready failus izlaiž)."""
    import image_classify
    import image_pipeline
    out: dict = {"listing_id": listing_id, "processed": 0, "failed": 0}
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute("SELECT local_image_paths_raw FROM properties.listings WHERE id=%s", (listing_id,)).fetchone()
        raws = [str(p).replace("\\", "/") for p in ((row or {}).get("local_image_paths_raw") or [])]
        if not raws:
            out["status"] = "no_images"
            return out
        ldir = STORAGE_ROOT / "listings" / str(listing_id)
        files = [STORAGE_ROOT / (r[r.index("listings/"):] if "listings/" in r else r) for r in raws]
        files = [f for f in files if f.is_file()]
        try:
            image_classify.ensure_classified(STORAGE_ROOT, listing_id, raw_files=files)
        except BaseException as e:  # bez OPENAI atslēgas met SystemExit — klasifikācija nav obligāta
            out["classify_error"] = f"{type(e).__name__}"
        ai_dir = ldir / "ai_ready"
        ai_dir.mkdir(parents=True, exist_ok=True)
        processed: list[str] = []
        for f in files:
            dst = ai_dir / f.name
            try:
                if not dst.exists():
                    if f.name.startswith(CLEAN_PREFIX):
                        shutil.copy2(f, dst)
                    else:
                        data = image_pipeline.process_bytes_checked(f.read_bytes(), f.name)
                        if data is None:
                            out["failed"] += 1
                            continue
                        dst.write_bytes(data)
                processed.append(f"listings/{listing_id}/ai_ready/{f.name}")
            except Exception as e:
                logger.warning("partner_intake: bilde %s/%s neizdevās: %s", listing_id, f.name, e)
                out["failed"] += 1
        if processed:
            conn.execute("UPDATE properties.listings SET local_image_paths_processed = %s::text[] WHERE id=%s",
                         (processed, listing_id))
            # partnera listingi → ss.lv AI analizators (agent_ai_poller paņem Debug_status NULL,
            # aizpilda neaizslēgtos laukus no bildēm + teksta, atliek 'ok')
            conn.execute("""UPDATE properties.listings SET "Debug_status" = NULL
                            WHERE id=%s AND source='agent_partner'""", (listing_id,))
            conn.commit()
            out["ai_analyze"] = "queued"
        out["processed"] = len(processed)
        out["status"] = "ok" if processed else "failed"
    return out


_ACTIVE_IMAGE_JOBS: set[str] = set()


def _images_job(intake_id: str, jobs: list, prior: Optional[list] = None) -> None:
    """Fona pavediens: bildes katram jaunajam listingam → meta.json images_result/images_status.
    `prior` = jau pabeigtie rezultāti (atsākšana pēc worker restarta)."""
    results: list[dict] = list(prior or [])
    total = len(results) + len(jobs)
    try:
        _images_job_inner(intake_id, jobs, results, total)
    finally:
        _ACTIVE_IMAGE_JOBS.discard(intake_id)


def _images_job_inner(intake_id: str, jobs: list, results: list, total: int) -> None:
    for job in jobs:
        lid, u = job[0], job[1]
        web = bool(job[2]) if len(job) > 2 else False
        r: dict = {"listing_id": lid, "web": web}
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                row = conn.execute("""SELECT coalesce(array_length(local_image_paths_raw,1),0) n, images_downloaded_at
                                      FROM properties.listings WHERE id=%s""", (lid,)).fetchone()
                if row and row["n"] and row["images_downloaded_at"]:
                    r["resumed"] = True   # raw jau salikts (pārtraukts AI posmā) — nedublē plānus
                else:
                    r.update(_attach_images(conn, lid, u, intake_id))
            r["ai"] = _process_images(lid)
            if web:
                r["web_queued"] = _queue_web(lid, intake_id)
        except Exception as e:
            r["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        results.append(r)
        try:
            meta_p = INTAKE_DIR / intake_id / "meta.json"
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            meta["images_result"] = results
            meta["images_status"] = "done" if len(results) >= total else f"{len(results)}/{total}"
            meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        except Exception:
            pass
    logger.info("partner_intake: bildes %s gatavas: %s", intake_id, results)


def _queue_web(listing_id: int, intake_id: str) -> str:
    """«Arī mājaslapā»: publicē TIKAI ar AI-apstrādātām bildēm (publish vārts) → wp_export_queue."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute("SELECT coalesce(array_length(local_image_paths_processed,1),0) n FROM properties.listings WHERE id=%s",
                           (listing_id,)).fetchone()
        if not row or not row["n"]:
            return "nav bilžu — mājaslapā nelikts (pievieno bildes un «Eksportēt uz WP»)"
        cur = conn.execute("""INSERT INTO properties.wp_export_queue (listing_id, status, requested_by)
                              VALUES (%s, 'pending', %s)
                              ON CONFLICT (listing_id) WHERE status IN ('pending','processing') DO NOTHING
                              RETURNING id""", (listing_id, f"partner_intake:{intake_id}"))
        got = cur.fetchone()
        conn.commit()
        return "rindā" if got else "jau rindā"


def start_images_job(intake_id: str, jobs: list, prior: Optional[list] = None) -> None:
    import threading
    if jobs and intake_id not in _ACTIVE_IMAGE_JOBS:
        _ACTIVE_IMAGE_JOBS.add(intake_id)
        threading.Thread(target=_images_job, args=(intake_id, jobs, prior), name=f"pi-images-{intake_id}", daemon=True).start()


def resume_pending_images() -> int:
    """Worker restarts (deploy) nogalina bilžu pavedienu → images_status iesprūst «k/n».
    Katrā ciklā: neaktīvām ieplūdēm ar nepabeigtu bilžu darbu turpina atlikušās telpas."""
    if not INTAKE_DIR.is_dir():
        return 0
    started = 0
    for d in INTAKE_DIR.iterdir():
        mp = d / "meta.json"
        if not mp.is_file() or d.name in _ACTIVE_IMAGE_JOBS:
            continue
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
            st = meta.get("images_status")
            if not st or st == "done":
                continue
            report = json.loads((d / "report.json").read_text(encoding="utf-8"))
            units = {str(u["idx"]): u for a in report.get("addresses", []) for u in a.get("units", [])}
            prior = [r for r in (meta.get("images_result") or []) if not r.get("error")]
            done_ids = {int(r["listing_id"]) for r in prior}
            web_map = {int(j[0]): bool(j[2]) for j in (meta.get("image_jobs") or []) if len(j) > 2}
            jobs = []
            for lid, idx in (meta.get("apply_result") or {}).get("created_units") or []:
                if int(lid) in done_ids or str(idx) not in units:
                    continue
                jobs.append((int(lid), units[str(idx)], web_map.get(int(lid), False)))
            if jobs:
                logger.info("partner_intake: atsāku bildes %s (%d telpas)", d.name, len(jobs))
                start_images_job(d.name, jobs, prior)
                started += 1
            else:
                meta["images_status"] = "done"
                mp.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        except Exception as e:
            logger.warning("partner_intake: resume %s: %s", d.name, e)
    return started


def reprocess_listing_images(intake_id: str, listing_id: int) -> dict:
    """Labošanai: atrod telpu pēc apply rezultāta (created_units) un pārlaiž bildes sinhroni."""
    d = INTAKE_DIR / intake_id
    report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    pairs = (meta.get("apply_result") or {}).get("created_units") or []
    idx = next((p[1] for p in pairs if int(p[0]) == int(listing_id)), None)
    unit = next((u for a in report["addresses"] for u in a["units"] if str(u["idx"]) == str(idx)), None)
    if unit is None:
        raise ApplyError(f"listings {listing_id} nav atrasts šīs ieplūdes apply rezultātā")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        r = _attach_images(conn, listing_id, unit, intake_id)
    r["ai"] = _process_images(listing_id)
    return r


# ───────────────────────────── saraksts / CLI / pollers ─────────────────────────────

def list_intakes(limit: int = 50) -> list[dict]:
    if not INTAKE_DIR.is_dir():
        return []
    out = []
    for d in sorted(INTAKE_DIR.iterdir(), reverse=True):
        if not d.is_dir() or not (d / "meta.json").is_file():
            continue
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        rep = None
        if (d / "report.json").is_file():
            try:
                r = json.loads((d / "report.json").read_text(encoding="utf-8"))
                rep = {"summary": r.get("summary"), "partner": r.get("partner"), "list_date": r.get("list_date"),
                       "addresses": [a.get("street") or a.get("building_name") for a in r.get("addresses", [])],
                       "is_vacancy_offer": r.get("is_vacancy_offer")}
            except Exception:
                pass
        out.append({"intake_id": meta["intake_id"], "status": meta.get("status"), "received_at": meta.get("received_at"),
                    "forwarder": meta.get("forwarder_email"), "subject": meta.get("subject"),
                    "attachments": [a["name"] for a in meta.get("attachments", [])], "report": rep,
                    "error": meta.get("error"),
                    # progresa josla panelī: apstiprināšana + bilžu fona darbs
                    "applied_at": meta.get("applied_at"),
                    "images_status": meta.get("images_status"),
                    "images_total": len((meta.get("apply_result") or {}).get("created_units") or []),
                    "images_done": len(meta.get("images_result") or []),
                    "applied": {k: len((meta.get("apply_result") or {}).get(k) or []) for k in ("created", "updated", "occupied", "errors")}
                               if meta.get("apply_result") else None})
        if len(out) >= limit:
            break
    return out


def process_intake(intake_id: str, do_notify: bool = True) -> dict:
    d = INTAKE_DIR / intake_id
    meta_p = d / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    try:
        analysis = analyze(intake_id)
        report = reconcile(intake_id, analysis)
        if do_notify:
            report["telegram_sent"] = notify(report)
        return report
    except Exception as e:
        meta["status"] = "error"
        meta["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.exception("partner_intake %s neizdevās", intake_id)
        if do_notify:
            try:
                notify({"intake_id": intake_id, "subject": meta.get("subject"), "forwarder": meta.get("forwarder_email"),
                        "summary": {"units": 0, "ready": 0, "needs_review": 0, "match": 0, "missing": 0},
                        "addresses": [], "partner": {}, "is_vacancy_offer": False})
            except Exception:
                pass
        raise


def intake_from_files(files: list[str], body_text: str = "", subject: str = "lokāls tests",
                      forwarder: str = "raimonds@rgcommerce.lv") -> dict:
    atts = []
    for f in files:
        p = Path(f)
        atts.append({"name": _safe_name(p.name), "type": "", "data": p.read_bytes()})
    parsed = {"message_id": f"local:{uuid.uuid4().hex}", "forwarder_name": "", "forwarder_email": forwarder,
              "subject": subject, "body": body_text, "orig_sender": _orig_sender_from_text(body_text),
              "attachments_raw": atts, "date": _now().isoformat()}
    return _store_intake(parsed, "local")


_state = {"running": False, "last_cycle_at": None, "fetched_total": 0, "processed_total": 0, "errors_total": 0, "last_error": None}


def get_status() -> dict:
    return {**_state, "configured": imap_configured(), "interval": INTERVAL, "user": IMAP_USER}


async def run_loop(stop_event: asyncio.Event) -> None:
    if not imap_configured():
        logger.info("partner_intake: PARTNER_IMAP_* nav — pollers guļ")
        return
    _state["running"] = True
    loop = asyncio.get_event_loop()
    try:
        while not stop_event.is_set():
            _state["last_cycle_at"] = _now().isoformat()
            try:
                metas = await loop.run_in_executor(None, fetch_new_intakes)
                _state["fetched_total"] += len(metas)
                for meta in metas:
                    try:
                        await loop.run_in_executor(None, process_intake, meta["intake_id"], True)
                        _state["processed_total"] += 1
                    except Exception as e:
                        _state["errors_total"] += 1
                        _state["last_error"] = f"{meta['intake_id']}: {type(e).__name__}"
            except Exception as e:
                _state["errors_total"] += 1
                _state["last_error"] = f"imap: {type(e).__name__}: {str(e)[:120]}"
                logger.error("partner_intake cikls: %s", e)
            try:
                resume_pending_images()
            except Exception as e:
                logger.warning("partner_intake: resume: %s", e)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=INTERVAL)
            except asyncio.TimeoutError:
                pass
    finally:
        _state["running"] = False


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Partneru ieplūde — CLI tests")
    ap.add_argument("--file", action="append", default=[], help="pielikums (PDF/XLSX/...), var vairākus")
    ap.add_argument("--text", default="", help="e-pasta teksts (vai @fails.txt)")
    ap.add_argument("--subject", default="lokāls tests")
    ap.add_argument("--fetch", action="store_true", help="paņemt jaunos no IMAP")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()
    body = args.text
    if body.startswith("@"):
        body = Path(body[1:]).read_text(encoding="utf-8")
    ids = []
    if args.fetch:
        ids = [m["intake_id"] for m in fetch_new_intakes()]
        print("IMAP jaunie:", ids)
    elif args.file or body:
        ids = [intake_from_files(args.file, body, args.subject)["intake_id"]]
    for i in ids:
        rep = process_intake(i, do_notify=not args.no_notify)
        print(json.dumps({"intake_id": i, "summary": rep["summary"], "partner": rep["partner"],
                          "addresses": [(a.get("street") or a.get("building_name"), (a.get("bp") or {}).get("id"), len(a["units"]), len(a["missing_in_list"]))
                                        for a in rep["addresses"]], "warnings": rep["warnings"][:8]}, ensure_ascii=False, indent=1))
