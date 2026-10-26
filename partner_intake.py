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
IMAP_EXTRA_FOLDERS = [f.strip() for f in os.getenv("PARTNER_IMAP_EXTRA_FOLDERS", "INBOX/spam,Junk,Spam").split(",") if f.strip()]
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
    # kopā rakstīts tips («Ganībudambis», «Brīvībasiela») → atdala (Domuss 2026-10-24: «jauna ēka» dublikāts)
    s = re.sub(r"([a-z]{3,})(dambis|iela|gatve|prospekts|bulvaris|laukums|aleja|skersiela)\b", r"\1 \2", s)
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
    # mājas nr. ar burtu (ss.lv «Dēļu 4s», «Ganību d. 25d») sakrīt ar «4»/«25» — bet ne «41» ar «4» (MBC 2026-10-25)
    toks = [t + "[a-z]?" if t.isdigit() else re.escape(t) for t in toks]
    if len(toks) == 1:
        return r"(^|[^a-z0-9])" + toks[0] + r"($|[^a-z0-9])"
    # starp daļām drīkst būt tikai VĀRDI (tips «iela», «gatve», «bulv.»), ne cipari — citādi «Duntes 3» atrada
    # «Duntes 23a» un «Brīvības 39» → «Brīvības gatve 439» (Skanstes City 2026-10-25)
    return r"(^|[^a-z0-9])" + r"[^a-z0-9]+(?:[a-z.]+[^a-z0-9]+)*?".join(toks) + r"($|[^a-z0-9])"


def _street_short(street: str) -> Optional[str]:
    """Garais oficiālais nosaukums → uzvārds + numurs: «Zigfrīda Annas Meierovica bulvāris 16» → «meierovica 16»,
    «Krišjāņa Barona 3» → «barona 3» (DB parasti īsais). None, ja saīsināt nav ko."""
    toks = _street_key(street).split()
    i = next((k for k, t in enumerate(toks) if t[0].isdigit()), len(toks))
    words, rest = toks[:i], toks[i:]
    if len(words) < 2:
        return None
    return " ".join([words[-1]] + rest)


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
        # Processed mapi nosaka VIENREIZ pirms cikla: _ensure_processed_folder pārslēdz uz INBOX, un, ja to sauktu
        # spama mapes vidū, copy/Deleted ietu uz nepareizu INBOX vēstuli (🟢 JL 2026-10-26)
        processed = _ensure_processed_folder(m)
        # arī SPAM mape: inbox.eu filtrs mūsu pašu pārsūtītās vēstules (Raimonds «Vircava», Ieva «Sadarbības
        # piedāvājums») lika spamā → worker tās neredzēja (2026-10-26). Sūtītāju tāpat filtrē ALLOWED_FORWARDERS.
        for folder_name in [IMAP_FOLDER] + [f for f in IMAP_EXTRA_FOLDERS if f != IMAP_FOLDER]:
            typ, _ = m.select(folder_name)
            if typ != "OK":
                if folder_name == IMAP_FOLDER:
                    raise RuntimeError(f"IMAP select {IMAP_FOLDER} neizdevās")
                continue
            out.extend(_fetch_folder(m, seen, processed))
    finally:
        try:
            m.logout()
        except Exception:
            pass
    _save_seen(seen)
    return out


def _fetch_folder(m: imaplib.IMAP4, seen: set[str], processed: Optional[str]) -> list[dict]:
    """Viena atvērta IMAP mape: neredzētās → intake; apstrādātās pārvieto uz Processed.
    Visur UID komandas (ne kārtas numuri) — drošas arī, ja mape pa vidu mainās."""
    out: list[dict] = []
    typ, ids = m.uid("search", "UNSEEN")
    uids = ids[0].split() if typ == "OK" and ids and ids[0] else []
    moved = False
    for num in uids:
        typ, data = m.uid("fetch", num, "(RFC822)")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            continue
        raw = data[0][1]
        parsed = _parse_message(raw)
        mid = parsed.get("message_id") or f"nomid:{hash(raw)}"
        if mid in seen:
            m.uid("store", num, "+FLAGS", "\\Seen")
            continue
        seen.add(mid)
        dom = parsed["forwarder_email"].split("@")[-1] if parsed.get("forwarder_email") else ""
        if dom not in ALLOWED_FORWARDERS:
            logger.info("partner_intake: ignorē sūtītāju %s (nav atļautajā sarakstā)", parsed.get("forwarder_email"))
            m.uid("store", num, "+FLAGS", "\\Seen")
            continue
        if not parsed["attachments_raw"] and len(parsed.get("body", "")) < 80:
            logger.info("partner_intake: tukša vēstule no %s — izlaiž", parsed.get("forwarder_email"))
            m.uid("store", num, "+FLAGS", "\\Seen")
            continue
        meta = _store_intake(parsed, "imap")
        out.append(meta)
        m.uid("store", num, "+FLAGS", "\\Seen")
        # pārvieto uz Processed — TIKAI ja kopija tiešām izdevās (imaplib uz "NO" nemet kļūdu;
        # inbox.eu mapes ir tikai zem INBOX/ → bez pārbaudes vēstule tiktu izdzēsta, 2026-10-21)
        folder = processed
        if folder:
            typ, _ = m.uid("copy", num, folder)
            if typ == "OK":
                m.uid("store", num, "+FLAGS", "\\Deleted")
                moved = True
            else:
                logger.warning("partner_intake: copy uz %s neizdevās (%s) — vēstule paliek INBOX kā izlasīta", folder, typ)
    if moved:
        try:
            m.expunge()
        except Exception:
            pass
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
    import link_media
    found_links: list[dict] = link_media.links_from_text(body, "e-pasta teksts")
    for a in meta.get("attachments", []):
        p = STORAGE_ROOT / a["path"]
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext == ".pdf":
            t, imgs = _pdf_parts(p, budget)
            parts.append(f"PIELIKUMS PDF «{p.name}»:\n{t}")
            images.extend(imgs)
            try:
                found_links.extend(link_media.links_from_pdf(p.read_bytes(), p.name))
            except Exception:
                pass
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
    # saites uz bildēm (Google Drive u.c.) — katrai ID + tabulas rinda, lai AI piesaista telpai (link_ids)
    links: list[dict] = []
    for fl in found_links:
        if not link_media.classify_url(fl["url"]):
            continue   # tikai atbalstītās (Drive) — mailto/web lapas neinteresē
        key = link_media.classify_url(fl["url"])
        same = next((x for x in links if link_media.classify_url(x["url"]) == key), None)
        geo = {"areas": fl.get("row_areas") or [], "floor": fl.get("row_floor"), "rents": fl.get("row_rents") or []}
        if same:
            same.setdefault("rows", []).append(fl["row_text"])
            same.setdefault("geo", []).append(geo)
            continue
        links.append({**fl, "id": f"L{len(links) + 1}", "rows": [fl["row_text"]], "geo": [geo]})
    # mājaslapas skenējums (partner_web): telpu lapas ar bildēm kā saites L1.. tādā pašā secībā kā body tekstā
    wl_p = intake_dir / "web_links.json"
    if wl_p.is_file():
        try:
            for wl in json.loads(wl_p.read_text(encoding="utf-8")):
                links.append({"url": wl["url"], "source": "mājaslapa", "page": None, "id": f"L{len(links) + 1}",
                              "rows": [wl.get("row_text") or ""], "geo": [{"areas": [], "floor": None, "rents": []}], "kind": "web"})
        except Exception:
            pass
    (intake_dir / "links.json").write_text(json.dumps(links, ensure_ascii=False, indent=1), encoding="utf-8")
    # partnera web lapas (Realto: «Telpas Purvciemā - 104 m2 <realto.lv/property/...>») — tur ir ADRESE, kuras
    # e-pastā nav («Dz74»); izlasām un dodam AI kā vēl vienu avotu
    pages: list[dict] = []
    cache_p = intake_dir / "webpages.json"
    cached = {}
    try:
        cached = {x["url"]: x for x in json.loads(cache_p.read_text(encoding="utf-8"))} if cache_p.is_file() else {}
    except Exception:
        cached = {}
    for url in list(dict.fromkeys(fl["url"] for fl in found_links if link_media.is_web_page(fl["url"])))[:8]:
        pg = cached.get(url) or link_media.fetch_page_text(url)
        if pg and pg.get("text"):
            pg["row_text"] = next((fl["row_text"] for fl in found_links if fl["url"] == url), "")
            pages.append(pg)
    if pages:
        cache_p.write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")
        parts.append("PARTNERA WEB LAPAS (saites no e-pasta; izmanto adresei/cenai/platībai — tā pati telpa, kas tabulā, NAV jauna):\n"
                     + "\n".join(f"[{x['url']}] e-pasta rinda: {x['row_text'][:200]}\n  virsraksts: {x['title']}\n  saturs: {x['text']}"
                                  for x in pages))
    if links:
        parts.append("SAITES UZ BILDĒM/PLĀNIEM (piesaisti katru telpai laukā `link_ids`; viena saite var būt vairākām telpām):\n"
                     + "\n".join(f"{x['id']} [{x['source']}{', ' + str(x['page']) + '. lapa' if x.get('page') else ''}] rinda: "
                                  + " ‖ ".join(dict.fromkeys(x["rows"]))[:400] for x in links))
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
            "link_ids": {"type": "array", "items": {"type": "string"}},
            "partner_notes": {"type": "string"},
            "offer_type": {"type": "string", "enum": ["standard", "flex", "project"]},
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
   ADRESI meklē VISUR: e-pasta teksts, pielikumi, PARTNERA WEB LAPAS (piem. «74, Dzelzavas iela, Purvciems» → street
   «Dzelzavas iela 74»), plāna/bildes uzraksti. Saīsinājums tematā («Dz74») + web lapas adrese = tā pati ēka.
   Partnera biroja adrese parakstā (piem. «Braslas iela 29a» zem vārda) NAV īpašuma adrese.
   Telpa, kas minēta gan ar saiti/teikumu («Telpas – 150 m2»), gan tabulā («150,8») = VIENA telpa (tabulas dati +
   piezīme no teikuma), nevis divas. Ja tabulā ir vairākas līdzīgas platības (104,1 un 105,7), izvēlies pēc web lapas
   CENAS (€520 ÷ 5 €/m² = 104 → 104,1), nevis tuvāko skaitli uz labu laimi.
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
9b. `link_ids` — saišu ID (L1, L2…) no bloka «SAITES UZ BILDĒM/PLĀNIEM», kuru tabulas rinda/teksts atbilst
    šai telpai (stāvs + platība + cena tajā pašā rindā). Ja saites nav vai nav skaidrs — tukšs saraksts.
    Ja vairākas telpas vienā rindā (piem. 46 / 45,8 / 77,2 m² ar vienu saiti) — tā pati saite visām.
9c. Tabula, kas TURPINĀS nākamajā lapā bez jauna sadaļas/ēkas virsraksta (atkārtota tikai galvene) — tās
    rindas pieder PĒDĒJAI iepriekšējā lapā redzētajai ēkai/adresei (nevis jaunai «nezināmai» ēkai).
9d. Platības DIAPAZONS bez atsevišķi uzskaitītiem izmēriem («500–730», «~388», «750–1500») = VIENA telpa:
    `area_m2` = mazākā vērtība, `area_approx`=true, `area_note` = «500–730 m², plānojumu var pielāgot».
    Ja izmēri uzskaitīti atsevišķi (73,45 / 108,67 / 182,12 m²) — katrs ir sava telpa.
9e. Vēl neuzbūvēts objekts / «pēc pieprasījuma» / BTS (build-to-suit) — VIENA telpa ar kopējo aprakstu,
    cena null, `condition`=«projekts/būvniecībā», warning; neveido atsevišķu telpu katrai diapazona robežai.
9f. `partner_notes` — partnera PAŠA piezīmes par šo telpu VERBATIM (tabulas «Piezīmes» kolonna, rindas
    teksts, e-pasta rinda) latviski kā oriģinālā, bez pārfrāzēšanas un bez tavām piebildēm, līdz ~400 zīmēm;
    "" ja nav. Aģents to lasa kā «ko partneris teica».
9g. `offer_type`: "flex" = coworking / serviced office / privātais kabinets coworkingā / darba vietas, kur cena ir
    KOPĒJĀ mēneša maksa ar visu iekļautu (nevis €/m²) — tad `rent_total` = tā maksa, `rent_per_m2_min` = null;
    "project" = vēl neuzbūvēts / BTS / «pēc pieprasījuma»; citādi "standard".
9h. Cena «No 10.00» / «no 10 €/m²» nozīmē: 10 IR cena (telpa «kā ir»; ar pielāgojumiem var būt vairāk) →
    `rent_per_m2_min` = 10, `rent_per_m2_max` = null, BEZ warning par to.
9i. AUTOMĀTISKS DRIVE SKENĒJUMS (teksts sākas ar «AUTOMĀTISKS SKENĒJUMS»): telpas = cenu faila tabulas rindas
    (platība, stāvs, tips, ēkas kods, statuss, noma, noma ar fit-out, apsaimniekošana) + TELPU MAPES rindas
    (nosaukums = platība, stāvs, adrese, piezīme). Katrai telpai `link_ids` = tās mape (sakrīt platība + stāvs +
    adrese). Ēkas kodu (S54A, B39, D3) atšifrē pēc mapju adresēm → `street`. Statuss «Pieejams» →
    `available_from` «tagad»; «Q3 2026» → `available_from` «Q3 2026». Ja ir «Noma» un «Noma (ar Fit-Out)» —
    `rent_per_m2_min` = noma (ja ir), citādi fit-out cena ar `partner_notes` «cena ar fit-out». FLEX tabulas
    (Office no., workstations, Price EUR, IZNOMĀTS/PIEEJAMS) → tikai PIEEJAMS kā `offer_type` flex ar
    `rent_total`; IZNOMĀTS → skipped_items. `looks_complete` = true (mape ir viss, kas partnerim brīvs).
9j. MĀJASLAPAS SKENĒJUMS (teksts sākas ar «AUTOMĀTISKS SKENĒJUMS — partnera … mājaslapa»): katra «TELPAS LAPA Ln»
    = viena telpa ar `link_ids` [«Ln»]; adrese no kartiņas/lapas (angļu «street» → iela, «K. Barona street 15» →
    «K. Barona iela 15»); «Size 500 sqm» = platība; «Floor 4» = stāvs; «EUR 10.50++ / sqm» → `rent_per_m2_min` 10.5 un
    `utilities_note` «plus apsaimniekošana/komunālie»; «EUR 325.00++ / sqm» pie 65 m² pagraba = visticamāk kopējā cena
    mēnesī (325/65 = 5 €/m²) → `rent_total`; apsaimniekošanas maksu no apraksta («service fee EUR 2.95/sqm») →
    `maintenance_per_m2`; `looks_complete` = true. Ēkas aprakstu (visa ēka, citas platības «from 110 to 335 sqm»)
    NEpārvērt par telpām.
10. `looks_complete` = vai dokuments izskatās kā PILNS šī partnera brīvo telpu saraksts (vakanču saraksts,
    «aktuālais piedāvājums», mēneša lapa) — true; vai tikai dažas telpas/viena brošūra — false.
11. `is_vacancy_offer=false`, ja e-pasts nav telpu piedāvājums (rēķins, sarakste, spams) — tad units tukšs.
12. `warnings` — īsi LV: kas neskaidrs (cena diapazons, platība «~», adrese nezināma, vairāki dokumenti u.c.).
"""


ANALYZE_RUNS = int(os.getenv("PARTNER_ANALYZE_RUNS", "2"))


def _ai_call(client, model: str, content: list[dict]) -> dict:
    resp = client.responses.create(
        model=model,
        input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": "vacancy_list", "schema": _schema(), "strict": True}},
    )
    return json.loads(resp.output_text)


def _unit_key(addr: dict, u: dict) -> tuple:
    """Telpas identitāte salīdzināšanai starp AI izlasījumiem: iela (bez tipa) + stāva cipars + platība."""
    area = _num(u.get("area_m2"))
    return (_street_key(addr.get("street") or addr.get("building_name") or ""), _floor_digits(_floor_norm(u.get("floor"))),
            round(area, 1) if area else None)


def _doc_areas(text: str) -> set[float]:
    """Platības, kas dokumenta tekstā minētas ar m²/kv.m — pārbaudei, vai AI kādu nav palaidis garām."""
    out = set()
    for m in re.finditer(r"(\d{1,5}(?:[.,]\d{1,2})?)\s*(?:m2|m²|kv\.?\s*m|kvm)\b", text or "", re.I):
        try:
            v = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if 8 <= v <= 50000:
            out.add(round(v, 1))
    return out


def _merge_runs(runs: list[dict]) -> tuple[dict, dict]:
    """Vairāku AI izlasījumu apvienošana (Raimonds 2026-10-24: «nevar būt, ka neizlasa pareizi»):
    bāze = izlasījums ar visvairāk telpām; telpas, kas ir CITĀ izlasījumā, bet bāzē nav — pievieno
    (pareizajai adresei); telpas, ko redzēja tikai viens no ≥2 izlasījumiem — `confidence` ≤0.55 →
    panelī «pārbaudīt». Atgriež (apvienotais, statistika)."""
    runs = [r for r in runs if r]
    if len(runs) == 1:
        runs = runs + [runs[0]]   # arī viens izlasījums iet caur dublikātu tīrīšanu
    counts = [sum(len(a.get("units") or []) for a in r.get("addresses") or []) for r in runs]
    base = json.loads(json.dumps(runs[counts.index(max(counts))]))
    seen: dict[tuple, int] = {}
    for r in runs:
        keys = set()
        for a in r.get("addresses") or []:
            for u in a.get("units") or []:
                keys.add(_unit_key(a, u))
        for k in keys:
            seen[k] = seen.get(k, 0) + 1
    base_keys = {_unit_key(a, u) for a in base.get("addresses") or [] for u in a.get("units") or []}
    added = 0
    for r in runs:
        if r is base:
            continue
        for a in r.get("addresses") or []:
            for u in a.get("units") or []:
                k = _unit_key(a, u)
                if k in base_keys or k[2] is None:
                    continue
                tgt = next((ba for ba in base.get("addresses") or [] if _street_key(ba.get("street") or ba.get("building_name") or "") == k[0]), None)
                if tgt is None and not a.get("street") and a.get("building_name"):
                    # tas pats nosaukums ar garāku/īsāku piedēkli («Dominante Parks "Ciedri"» vs «… , Lielvāržos …»)
                    nm = _street_key(a["building_name"])[:14]
                    tgt = next((ba for ba in base.get("addresses") or [] if not ba.get("street") and ba.get("building_name")
                                and (_street_key(ba["building_name"])[:14] == nm)), None)
                if tgt is None:
                    tgt = {**{kk: vv for kk, vv in a.items() if kk != "units"}, "units": []}
                    base.setdefault("addresses", []).append(tgt)
                tgt["units"].append(u)
                base_keys.add(k)
                added += 1
    # viena un tā pati telpa dokumentā minēta divreiz (Domuss: GD25D.E16.1 plānā divās vietās) → viena
    dups = 0
    for a in base.get("addresses") or []:
        uniq, keys_ref = [], set()
        for u in a.get("units") or []:
            kr = (_unit_key(a, u), (u.get("ref") or "").strip().lower())
            if kr[0][2] is not None and kr[1] and kr in keys_ref:
                dups += 1
                continue
            keys_ref.add(kr)
            uniq.append(u)
        a["units"] = uniq
    # «Telpas – 150 m2» (saite e-pastā) + tabulā «3.st. 150,8» → viena (Realto Dz74, 2026-10-24)
    for a in base.get("addresses") or []:
        a["units"], n = _merge_near_duplicates(a.get("units") or [])
        dups += n
    # viens izlasījums telpas lika pie ielas, otrs — pie «FLEX biroji» bez ielas (Skanstes City 2026-10-25) → tā pati
    # telpa (stāvs + platība) paliek TIKAI pie adreses ar ielu; adrese bez ielas un bez telpām pazūd
    # + divi izlasījumi to pašu ēku bez ielas nosauc dažādi («Dominante Parks "Ciedri"» / «… Lielvāržos, Ķekavas
    # pagastā») → tās pašas telpas (stāvs + platība) paliek tikai pirmajā; adreses bez telpām pazūd (Realto 2026-10-26)
    seen_fa: set[tuple] = {(_floor_digits(_floor_norm(u.get("floor"))), round(_num(u.get("area_m2")) or 0, 1))
                           for a in base.get("addresses") or [] if a.get("street") for u in a.get("units") or []}
    kept_addrs = []
    for a in base.get("addresses") or []:
        if not a.get("street"):
            before = len(a.get("units") or [])
            kept_units = []
            for u in a.get("units") or []:
                k = (_floor_digits(_floor_norm(u.get("floor"))), round(_num(u.get("area_m2")) or 0, 1))
                if k[1] and k in seen_fa:
                    continue
                seen_fa.add(k)
                kept_units.append(u)
            a["units"] = kept_units
            dups += before - len(kept_units)
        if not a.get("units"):
            continue
        kept_addrs.append(a)
    base["addresses"] = kept_addrs
    # absurdas platības (ha nolasīts kā m²: «3,92 ha» → 3.92 m², Realto Vircava 2026-10-26) → ārā + warning
    for a in base.get("addresses") or []:
        bad = [u for u in a.get("units") or [] if _num(u.get("area_m2")) is not None and 0 < _num(u.get("area_m2")) < 5]
        if bad:
            a["units"] = [u for u in a["units"] if u not in bad]
            base.setdefault("warnings", []).append(
                "Izlaistas telpas ar platību zem 5 m² (visticamāk ha nolasīts kā m²): " + ", ".join(str(u.get("area_m2")) for u in bad))
    weak = 0
    for a in base.get("addresses") or []:
        for u in a.get("units") or []:
            if seen.get(_unit_key(a, u), 0) < len(runs):
                u["confidence"] = min(float(u.get("confidence") or 0), 0.55)
                weak += 1
    return base, {"runs": len(runs), "units_per_run": counts, "added_from_other_runs": added, "seen_once": weak, "duplicates_removed": dups}


def _unit_richness(u: dict) -> int:
    return sum(1 for k in ("ref", "floor", "rent_per_m2_min", "rent_total", "sale_price", "maintenance_per_m2") if u.get(k))


def _merge_near_duplicates(units: list[dict]) -> tuple[list[dict], int]:
    """Telpa BEZ ref + tieši viena telpa AR ref ar ~vienādu platību (±1 m² / 1%) un nekonfliktējošu stāvu →
    tā pati telpa no diviem avotiem (teikums + tabula). Bagātākā paliek, nabadzīgākās piezīmes/saites pievienojas."""
    out: list[dict] = []
    merged = 0
    for u in sorted(units, key=lambda x: -_unit_richness(x)):
        area = _num(u.get("area_m2"))
        tgt = None
        if area and not u.get("ref"):
            fd = _floor_digits(_floor_norm(u.get("floor")))
            cands = [o for o in out if o.get("ref") and _num(o.get("area_m2"))
                     and abs(_num(o.get("area_m2")) - area) <= max(1.0, 0.01 * area)
                     and (not fd or _floor_digits(_floor_norm(o.get("floor"))) in ("", fd))]
            if len(cands) == 1:
                tgt = cands[0]
        if tgt is None:
            out.append(u)
            continue
        merged += 1
        notes = [x for x in (tgt.get("partner_notes"), u.get("partner_notes")) if x]
        tgt["partner_notes"] = " ".join(dict.fromkeys(notes)) or None
        tgt["link_ids"] = list(dict.fromkeys((tgt.get("link_ids") or []) + (u.get("link_ids") or [])))
        tgt["features"] = list(dict.fromkeys((tgt.get("features") or []) + (u.get("features") or [])))
        for k, v in u.items():
            if v not in (None, "", []) and tgt.get(k) in (None, "", []):
                tgt[k] = v
    order = {id(u): i for i, u in enumerate(units)}
    out.sort(key=lambda x: order.get(id(x), 0))
    return out, merged


def _name_matches(name: str, place: str) -> bool:
    tr = str.maketrans("āēīōūčšžģķļņŗ", "aeiouczsgklnr")
    toks = [t for t in re.findall(r"[a-z0-9]{3,}", name.lower().translate(tr)) if t not in ("eka", "centrs", "nams", "biroju")]
    p = place.lower().translate(tr)
    return bool(toks) and all(t in p for t in toks)


def _geocode_name(name: str, city: str) -> Optional[dict]:
    """Ēkas nosaukums («Basteja Pasāža») → {street, district}: Google Places teksta meklēšana (atrod biznesa
    centrus pēc nosaukuma). Tikai LV, tikai ar mājas numuru un tikai, ja vietas nosaukums sakrīt."""
    key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not key or not name:
        return None
    import urllib.parse
    import urllib.request
    q = f"{name}, {city or 'Rīga'}"
    try:
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchText",
            data=json.dumps({"textQuery": q, "languageCode": "lv", "regionCode": "LV"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                     "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.addressComponents"})
        with urllib.request.urlopen(req, timeout=15) as r:
            for pl in (json.loads(r.read().decode("utf-8")).get("places") or [])[:3]:
                comp = {t: c["longText"] for c in pl.get("addressComponents") or [] for t in c.get("types") or []}
                # vietas nosaukumā jābūt nosaukuma galvenajam vārdam (citādi «MCITY 2. ēka» → svešs objekts)
                if not _name_matches(name, (pl.get("displayName") or {}).get("text") or ""):
                    continue
                if comp.get("route") and comp.get("street_number") and comp.get("country", "Latvija") in ("Latvija", "Latvia"):
                    return {"street": f"{comp['route']} {comp['street_number']}",
                            "district": comp.get("neighborhood") or None, "formatted": pl.get("formattedAddress")}
    except Exception as e:
        logger.info("partner_intake: places %r neizdevās: %s", name, e)
    return None


def _resolve_missing_addresses(data: dict) -> int:
    """Adrese nav atrasta nekur dokumentā → ēkas nosaukums caur Google. Atrastā adrese = «pārbaudīt»."""
    n = 0
    for a in data.get("addresses") or []:
        if a.get("street") or not a.get("building_name"):
            continue
        g = _geocode_name(a["building_name"], a.get("city") or "Rīga")
        if not g:
            continue
        a["street"] = g["street"]
        a["district"] = a.get("district") or g.get("district")
        a["address_known"] = True
        a["address_source"] = "google"
        for u in a.get("units") or []:
            u["confidence"] = min(float(u.get("confidence") or 0), 0.6)
        data.setdefault("warnings", []).append(
            f"Adrese «{a['building_name']}» atrasta pēc nosaukuma Google kartē: {g['formatted']} — pārbaudi.")
        n += 1
    return n


def analyze(intake_id: str) -> dict:
    """AI izlasa dokumentu ANALYZE_RUNS reizes (paralēli) → apvieno → pārbauda pret dokumenta platībām:
    ja dokumentā minēta platība, kas nav ne telpā, ne skipped_items, — vēl viens mērķēts izlasījums ar
    šo sarakstu. Tā AI nejaušība (Domuss: 5 vs 8 telpas tam pašam PDF) neaiziet līdz aģentam."""
    d = INTAKE_DIR / intake_id
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    text, images = build_ai_input(d, meta)
    from openai import OpenAI
    from concurrent.futures import ThreadPoolExecutor
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
    runs: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, ANALYZE_RUNS)) as ex:
        for res in ex.map(lambda _: _safe_ai(client, model, content), range(max(1, ANALYZE_RUNS))):
            if res:
                runs.append(res)
    if not runs:
        raise RuntimeError("AI neatbildēja nevienā izlasījumā")
    data, stats = _merge_runs(runs)
    # pārklājuma pārbaude: dokumenta platības, kas nekur neparādās
    covered = set()
    for a in data.get("addresses") or []:
        for u in a.get("units") or []:
            if _num(u.get("area_m2")):
                covered.add(round(_num(u.get("area_m2")), 1))
    # platības, kas minētas telpu piezīmēs (sastāvdaļas «32.7m2+22.4m2+10%») vai izlaistajos, arī skaitās redzētas
    skipped_txt = " ".join(json.dumps(x, ensure_ascii=False) for x in data.get("skipped_items") or [])
    skipped_txt += " " + " ".join(f"{u.get('area_note') or ''} {u.get('partner_notes') or ''} {u.get('ref') or ''}"
                                  for a in data.get("addresses") or [] for u in a.get("units") or [])
    skipped_nums = {round(float(x.replace(",", ".")), 1) for x in re.findall(r"\d{1,5}(?:[.,]\d{1,2})?", skipped_txt)}
    missing = sorted(x for x in _doc_areas(text.split("PARTNERA WEB LAPAS")[0]) if not any(abs(x - c) <= max(0.06, 0.005 * x) for c in covered | skipped_nums))
    stats["doc_areas_missing"] = missing
    if missing and len(missing) <= 40:
        hint = ("PĀRBAUDE: dokumentā minētas šīs platības, kas tavā iepriekšējā atbildē NAV ne `units`, ne `skipped_items`: "
                + ", ".join(f"{x:g} m²" for x in missing)
                + ". Izlasi vēlreiz un atgriez PILNU sarakstu: katra no tām ir vai nu telpa (units), vai skaidri ielikta "
                  "skipped_items ar iemeslu (piem. kopplatība, terase, balkons, cita ēka, jau iznomāta).")
        fix = _safe_ai(client, model, content + [{"type": "input_text", "text": hint}])
        if fix:
            data, st2 = _merge_runs([data, fix])
            stats["coverage_rerun"] = st2
    stats["addresses_geocoded"] = _resolve_missing_addresses(data)
    data["_meta"] = {"model": model, "seconds": round(time.time() - t0, 1), "text_chars": len(text),
                     "images": len(images), "consistency": stats}
    (d / "analysis.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("partner_intake %s: analīze %s", intake_id, stats)
    return data


def _safe_ai(client, model: str, content: list[dict]) -> Optional[dict]:
    for attempt in range(2):
        try:
            return _ai_call(client, model, content)
        except Exception as e:
            logger.warning("partner_intake: AI izlasījums neizdevās (%s/2): %s", attempt + 1, e)
            time.sleep(3)
    return None


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
    if (main is None or main.get("how") in ("name", "partner")) and street and _street_short(street):
        # «Zigfrīda Annas Meierovica bulvāris 16» (Google/oficiāli) ↔ DB «Meierovica bulv. 16» (Basteja 2026-10-24)
        short = _find_bp(conn, _street_short(street), city, district, None)
        if short:
            main, street = short, _street_short(street)
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
    """Mūsu sludinājumi ēkā: mūsu avoti + VISS, kas ir mājaslapā (arī auto-publicētie ss.lv — Basteja 332 m²
    #111385 bija mājaslapā, bet netika atpazīts, Raimonds 2026-10-24)."""
    return conn.execute(
        """SELECT id, street, area_m2, floor, price, price_type, price_per_m2, "Space_group", source,
                  on_website, wp_post_id, wp_synced_at, created_at, occupancy_status,
                  "Apsaimniekosanas_maksa", partner_unit_ref,
                  cardinality(local_image_paths_processed) AS n_processed,
                  cardinality(local_image_paths_raw) AS n_raw
             FROM properties.listings
            WHERE building_profile_id = %s AND "Debug_status" = 'ok'
              AND (source = ANY(%s) OR wp_post_id IS NOT NULL OR on_website = true)
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
                  AND (source = ANY(%s) OR wp_post_id IS NOT NULL OR on_website = true)
                ORDER BY id DESC""",
            (bp_id, list(OUR_SOURCES)),
        ).fetchall()


def _area_close(a: Optional[float], b: Optional[float], approx: bool) -> bool:
    if a is None or b is None or a <= 0 or b <= 0:
        return False
    tol = 0.15 if approx else 0.03
    return abs(a - b) <= max(1.0, tol * max(a, b))


def _alt_areas(u: dict) -> list[float]:
    """Citas platības, ar ko tā pati telpa var būt ss.lv: partnera piezīmēs minētās («194.83+10%» → 194.83;
    «nr.201-9;32.7m2+201-20;22.4m2+10%» → 32.7, 22.4, 55.1) un neto bez ~10 % koplietošanas (MBC 2026-10-25:
    aģents ss.lv liek neto 194, partneris sarakstā bruto 214,32)."""
    area = _num(u.get("area_m2"))
    nums: list[float] = []
    for txt in (u.get("area_note"), u.get("partner_notes")):
        t = str(txt or "")
        t = re.sub(r"\d+(?:[.,]\d+)?\s*%", " ", t)          # «+10%» nav platība
        t = re.sub(r"\b\d+\s*-\s*\d+\b", " ", t)             # telpu kodi «201-9» nav platība
        for m in re.findall(r"(\d{1,5}(?:[.,]\d{1,2})?)\s*(?:m2|m²|kv)?", t):
            try:
                v = float(m.replace(",", "."))
            except ValueError:
                continue
            if 8 <= v <= 50000 and (not area or abs(v - area) > 0.5):
                nums.append(round(v, 2))
    nums = list(dict.fromkeys(nums))   # area_note un partner_notes bieži atkārto to pašu skaitli
    out: list[float] = list(nums)
    if 2 <= len(nums) <= 4:
        out.append(round(sum(nums), 2))
    if area and area >= 20:
        out.append(round(area / 1.1, 2))   # bruto → neto (koplietošanas +10 %)
    return [x for x in dict.fromkeys(out) if x]


def _inbox_candidates(conn, street: str, city: Optional[str], floor: str, area: Optional[float], approx: bool,
                      alt_areas: Optional[list[float]] = None) -> list[dict]:
    """ss.lv inbox: tā pati iela + stāvs + platība (vai alternatīvā neto platība) → tie paši sludinājumi (foto avots)."""
    like = _street_like(street)
    if not like or area is None:
        return []
    rows = conn.execute(
        """SELECT id, street, area_m2, floor, price, link, is_agent, date_posted, last_updated, skip_reason, phone_numbers
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
        primary = _area_close(area, ra, True if approx else False) or bool(ra and abs(area - ra) <= max(2.0, 0.05 * area))
        alt_hit = None if primary else next((a for a in (alt_areas or []) if ra and abs(a - ra) <= max(1.0, 0.02 * a)), None)
        if not primary and alt_hit is None:
            continue
        rf = _floor_digits(r["floor"])
        floor_ok = (not fd) or (not rf) or fd == rf
        if not primary and not floor_ok:
            continue   # neto platība sakrīt, bet stāvs cits → ne tā pati telpa (74 m² 1.st ↔ 68 m² 2.st)
        score = 0.0
        if ra:
            base_area = area if primary else alt_hit
            score = 1.0 - min(1.0, abs(base_area - ra) / max(base_area, 1.0))
            if not primary:
                score -= 0.15   # neto/alternatīvā platība — mazāk droši nekā tieša sakritība
        if floor_ok and fd and rf:
            score += 0.3
        out.append({"inbox_id": int(r["id"]), "street": r["street"], "area_m2": r["area_m2"], "floor": r["floor"],
                    "price": r["price"], "link": r["link"], "is_agent": str(r["is_agent"]).lower() == "true",
                    "phone": r.get("phone_numbers"),
                    "last_updated": str(r["last_updated"])[:10] if r["last_updated"] else None,
                    "same_room_score": round(score, 2), "floor_ok": floor_ok,
                    "via_alt_area": None if primary else alt_hit})
    # score, tad īpašnieks pirms aģenta, tad jaunākais (Raimonds 2026-10-26: «rāda to, kas nav aģents, ja tāds ir»)
    out.sort(key=lambda x: (-round(x["same_room_score"], 1), 1 if x["is_agent"] else 0, x["last_updated"] or ""))
    return out[:5]



# ───────────────────────────── kontakta numurs ─────────────────────────────

def _fmt_phone(raw) -> Optional[str]:
    """«29188067», «+371 29 188 067», «(+371) 29188067» → «+371 29 188 067» (aģentu formāts). Tikai LV mobilie/fiksētie."""
    if not raw:
        return None
    first = re.split(r"[,;/]| un ", str(raw))[0]
    dg = re.sub(r"\D", "", first)
    if dg.startswith("00371"):
        dg = dg[5:]
    elif dg.startswith("371") and len(dg) == 11:
        dg = dg[3:]
    if len(dg) != 8 or dg[0] not in "2679":
        return None
    return f"+371 {dg[:2]} {dg[2:5]} {dg[5:]}"


def _resolve_contact_phone(conn, partner: dict, bp: Optional[dict], ours: list[dict], units: list[dict]) -> dict:
    """Kuru numuru likt partnera listingiem (Raimonds 2026-10-23): 1) numurs no partnera e-pasta/PDF;
    2) ja tur nav — mūsu DB (partneru reģistrs → ēkas apstiprinātie/īpašnieka numuri → mūsu listingi ēkā);
    3) tad ss.lv tā pati telpa (privātpersona pirms aģenta). Atgriež izvēlēto + alternatīvas aģentam."""
    alts: list[dict] = []

    def add(phone, source):
        f = _fmt_phone(phone)
        if f and all(a["phone"] != f for a in alts):
            alts.append({"phone": f, "source": source})

    add((partner or {}).get("phone"), "partnera e-pasts / PDF")
    try:
        em = ((partner or {}).get("email") or "").lower()
        dom = em.split("@")[-1] if "@" in em else ""
        if em or dom:
            r = conn.execute("""SELECT phone FROM properties.partner_sources
                                WHERE lower(coalesce(contact_email,'')) = %s OR (%s <> '' AND lower(coalesce(email_domain,'')) = %s)
                                ORDER BY id LIMIT 1""", (em, dom, dom)).fetchone()
            if r:
                add(r["phone"], "partneru reģistrs")
    except Exception:
        conn.rollback()
    if bp and bp.get("id"):
        try:
            r = conn.execute("""SELECT auto_publish_phones, primary_phone, secondary_phone
                                FROM properties.building_profiles WHERE id=%s""", (bp["id"],)).fetchone()
            if r:
                for k, label in (("auto_publish_phones", "ēkas apstiprinātais nr."), ("primary_phone", "ēkas galvenais nr."),
                                 ("secondary_phone", "ēkas otrais nr.")):
                    for ph in re.split(r"[,;]", str(r[k] or "")):
                        add(ph, label)
        except Exception:
            conn.rollback()
        try:
            ids = [int(l["id"]) for l in ours]
            if ids:
                for row in conn.execute("""SELECT phone_numbers, count(*) n FROM properties.listings
                                           WHERE id = ANY(%s) AND coalesce(phone_numbers,'') <> ''
                                           GROUP BY 1 ORDER BY n DESC LIMIT 3""", (ids,)).fetchall():
                    add(row["phone_numbers"], "mūsu listingi šajā ēkā")
        except Exception:
            conn.rollback()
    inbox = [c for u in units for c in ((u.get("images") or {}).get("sslv_inbox") or [])]
    for c in sorted(inbox, key=lambda c: bool(c.get("is_agent"))):
        add(c.get("phone"), "ss.lv " + ("aģents" if c.get("is_agent") else "privātpersona"))
    return {"phone": alts[0]["phone"] if alts else None, "source": alts[0]["source"] if alts else None,
            "alternatives": alts}


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
            # Plāna lapa = maz teksta (PEBO: «3.st 58.40m2 birojs»). Piedāvājuma TABULA ar saitēm «bildes un
            # plānojums» (MCITY, 3000+ zīmes) NAV plāns — agrāk visa lapa tika renderēta kā «plāns».
            short = len(text.strip()) < 400
            is_plan_page = (single_plan_doc and short) or (short and any(k in text for k in ("plān", "plan", "layout")))
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


def _image_attachments_for_unit(meta: dict, unit: dict, single_unit: bool) -> dict:
    """E-pasta JPG/PNG pielikumi (Basteja: «plans biroja.jpg» + 8 WhatsApp foto) → kandidāti. Telpai pieder:
    pielikums, kas nosaukts tās attachment_hint, vai VISI, ja telpa dokumentā ir viena. «plan/plān» nosaukumā = plāns."""
    out = {"photos": [], "plans": []}
    hint = ((unit.get("attachment_hint") or "") + " " + (unit.get("page_hint") or "")).lower()
    for a in meta.get("attachments") or []:
        name = a.get("name") or ""
        if Path(name).suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        p = STORAGE_ROOT / a["path"]
        if not p.is_file() or p.stat().st_size < 15_000:
            continue   # paraksta ikonas (image001.png 7 KB) nav foto
        stem = Path(name).stem.lower()
        named = name.lower() in hint or stem in hint or (stem[:12] in hint and len(stem) >= 12)
        if not (single_unit or named):
            continue
        rel = str(a["path"]).replace("\\", "/")
        (out["plans"] if re.search(r"pl[aā]n|layout|shema|schem", stem) else out["photos"]).append(rel)
    return out


_SSLV_MSG_RE = re.compile(r"https?://(?:www\.)?ss\.(?:lv|com)/msg/[^\s<>\")\]]+", re.I)


def _email_sslv_candidates(d: Path) -> list[dict]:
    """Partnera e-pastā/PDF ieliktie ss.lv/ss.com sludinājumu linki (Realto «Vircava»: «Ir aktīvs sludinājums … <ss.com/msg/…>»)
    → tādi paši kandidāti kā no mūsu inbox (galerija → bildes), arī ja reģionu mūsu skrāperis neskenē.
    Kešs intake mapē sslv_links.json."""
    cache_p = d / "sslv_links.json"
    if cache_p.is_file():
        try:
            return json.loads(cache_p.read_text(encoding="utf-8"))
        except Exception:
            pass
    texts = []
    if (d / "body.txt").is_file():
        texts.append((d / "body.txt").read_text(encoding="utf-8", errors="ignore"))
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        for a in meta.get("attachments") or []:
            if str(a.get("name", "")).lower().endswith(".pdf"):
                import pymupdf
                with pymupdf.open(str(STORAGE_ROOT / a["path"])) as doc:
                    for page in doc:
                        texts.append(page.get_text() or "")
                        for l in page.get_links():
                            if l.get("uri"):
                                texts.append(l["uri"])
    except Exception:
        pass
    urls = list(dict.fromkeys(m.group(0).split("#")[0].rstrip(".,;") for t in texts for m in _SSLV_MSG_RE.finditer(t)))
    out: list[dict] = []
    for url in urls[:10]:
        try:
            import ai_text_helpers as helpers
            import sslv_import
            info = sslv_import.parse_sslv_url(url)
            html_doc = sslv_import._fetch_html(info["fetch_url"], helpers.HEADERS, helpers.REQUEST_TIMEOUT)
            det = sslv_import.parse_detail_page(html_doc)
            gallery = (helpers.extract_gallery_urls(html_doc) or [])[:SSLV_MAX_PHOTOS]
            out.append({"inbox_id": 0, "street": det.get("street"), "city": det.get("city"), "area_m2": str(det["area_m2"]) if det.get("area_m2") else None,
                        "floor": str(det["floor"]) if det.get("floor") is not None else None, "price": str(det["price"]) if det.get("price") is not None else None,
                        "link": info["canonical_link"], "is_agent": False, "phone": None, "last_updated": None,
                        "same_room_score": 1.5, "floor_ok": True, "from_email": True, "gallery_n": len(gallery)})
        except Exception as e:
            logger.info("partner_intake: e-pasta ss.lv links %s nenolasās: %s", url, e)
    cache_p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _email_sslv_for_unit(cands: list[dict], u: dict, n_units_total: int) -> list[dict]:
    """Kurš e-pasta ss.lv links pieder šai telpai: platība ±5 % (vai ss.lv bez platības); ja dokumentā ir tikai
    1 telpa (vai 1 ēka ar nomas+pārdošanas variantiem — Vircava) un 1 links → tas."""
    if not cands:
        return []
    area = _num(u.get("area_m2"))
    hits = []
    for c in cands:
        ca = _num(c.get("area_m2"))
        if area and ca and abs(area - ca) <= max(2.0, 0.05 * max(area, ca)):
            hits.append(c)
        elif not ca and (len(cands) == 1):
            hits.append(c)
    if not hits and len(cands) == 1 and n_units_total <= 2:
        hits = cands
    return hits


def _merge_same_addresses(addresses: list[dict]) -> list[dict]:
    """AI tabulu pāri lapām mēdz sadalīt divās «adresēs» ar to pašu ielu (MCITY Ulbrokas 23 → 13 + 6 telpas,
    «nav sarakstā» dubultā). Apvieno pēc ielas atslēgas + pilsētas (vai ēkas nosaukuma)."""
    out: list[dict] = []
    index: dict[str, dict] = {}
    for a in addresses:
        key = (_street_key(a.get("street") or "") or (a.get("building_name") or "").strip().lower()) + "|" + (a.get("city") or "rīga").strip().lower()
        hit = index.get(key) if key.strip("|") else None
        if hit is None:
            a = dict(a)
            a["units"] = list(a.get("units") or [])
            index[key] = a
            out.append(a)
            continue
        hit["units"].extend(a.get("units") or [])
        for k in ("district", "building_name"):
            if not hit.get(k) and a.get(k):
                hit[k] = a[k]
        hit["address_known"] = bool(hit.get("address_known", True)) or bool(a.get("address_known", True))
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
        has_doc = any(Path(x.get("name") or x.get("path") or "").suffix.lower() in (".pdf", ".xlsx", ".xlsm", ".xls", ".docx", ".csv")
                      for x in meta.get("attachments") or [])
        total_units = sum(len(a.get("units") or []) for a in analysis.get("addresses") or [])
        email_sslv = _email_sslv_candidates(d)   # partnera e-pastā ieliktie ss.lv sludinājumi (galerija bildēm)
        for addr in _merge_same_addresses(analysis.get("addresses") or []):
            bps = []
            ov = (meta.get("address_overrides") or {}).get(str(len(report["addresses"])))
            if ov and ov.get("bp_id"):
                # aģents panelī sasaistīja ar DB ēku («Dominante Parks» → «Lielvārži, Ķekavas novads», 2026-10-26)
                row = conn.execute("SELECT id, full_address, city, street, building_name FROM properties.building_profiles WHERE id = %s",
                                   (int(ov["bp_id"]),)).fetchone()
                if row:
                    addr = {**addr, "street": row["full_address"] or row["street"] or addr.get("street"),
                            "city": row["city"] or addr.get("city") or "Rīga", "address_known": True, "address_source": "agent"}
                    bps = [{"id": int(row["id"]), "full_address": row["full_address"], "city": row["city"], "how": "agent"}]
            if not bps and addr.get("address_source") == "google" and addr.get("building_name"):
                # mūsu DB ēkas nosaukums ir drošāks par Google atrasto adresi
                by_name = _find_bp(conn, None, addr.get("city"), addr.get("district"), addr.get("building_name"))
                bps = [by_name] if by_name else []
            bps = bps or _find_bps(conn, addr.get("street"), addr.get("city"), addr.get("district"), addr.get("building_name"))
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
            # Google atrastais garais nosaukums → DB ēkas adrese (jaunie listingi ar «Meierovica bulv. 16», ne «Zigfrīda Annas …»)
            street_out = (bp.get("full_address") if (bp and addr.get("address_source") == "google" and bp.get("full_address")) else addr.get("street"))
            a_out = {"street": street_out, "city": addr.get("city") or "Rīga", "district": addr.get("district"),
                     "building_name": addr.get("building_name"), "address_known": bool(addr.get("address_known", True)),
                     "bp": bp, "new_building": bp is None,
                     "our_count": sum(1 for l in ours if l.get("occupancy_status") != "occupied"),
                     "units": [], "missing_in_list": []}
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
                # vairāki kandidāti, bet tikai viens brīvs → tas (iznomātais = vēsturisks dvīnis)
                if len(cands) > 1:
                    free = [c for c in cands if c.get("occupancy_status") != "occupied"]
                    if len(free) == 1:
                        cands = free
                status = "new"
                match = None
                diff = {}
                reactivate = False
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
                    # atzīmēts «iznomāts», bet partneris to atkal piedāvā → atjaunot kā brīvu
                    reactivate = match.get("occupancy_status") == "occupied"
                elif len(cands) > 1:
                    status = "ambiguous"
                pdf_imgs = _pdf_images_for_unit(d, meta, u) if addr.get("street") or addr.get("building_name") else {"photos": [], "plans": []}
                att_imgs = _image_attachments_for_unit(meta, u, total_units == 1)
                pdf_imgs = {"photos": pdf_imgs["photos"] + att_imgs["photos"], "plans": pdf_imgs["plans"] + att_imgs["plans"]}
                images = {"pdf": pdf_imgs,
                          "links": _link_images_for_unit(d, intake_id, u),
                          "own_listings_with_images": [int(l["id"]) for l in ours if (l.get("n_processed") or 0) + (l.get("n_raw") or 0) > 0][:5],
                          "sslv_inbox": _email_sslv_for_unit(email_sslv, u, total_units)
                          + (_inbox_candidates(conn, (bp or {}).get("full_address") or addr.get("street") or "", addr.get("city"), floor, area, approx, _alt_areas(u))
                             if addr.get("street") else [])}
                seen_links: set[str] = set()
                images["sslv_inbox"] = [c for c in images["sslv_inbox"] if not (c["link"] in seen_links or seen_links.add(c["link"]))]
                has_photo = bool(images["pdf"]["photos"]) or bool(images["links"]["photos"]) or bool(images["sslv_inbox"]) or (match and ((match.get("n_processed") or 0) + (match.get("n_raw") or 0) > 0))
                conf = float(u.get("confidence") or 0)
                price_missing = u.get("deal") == "rent" and u.get("rent_per_m2_min") is None and u.get("rent_total") is None
                # telpa JAU ir DB (sakrīt) → trūkstoša cena/tips nav iemesls «izlaist» — cena jau ir DB; noklusētais
                # «jau DB — nemainīt» (Raimonds 2026-10-26: «kāpēc uzreiz nepiedāvāja jau ir DB?»)
                needs_review = (status == "ambiguous" or conf < 0.6 or not a_out["address_known"]
                                or (status != "match" and (not u.get("space_type") or price_missing)))
                decision = ("review" if (needs_review and status != "match") else
                            ("update" if (status == "match" and (diff or reactivate)) else
                             ("keep" if status == "match" else "create")))
                unit_out = {k: v for k, v in u.items() if not k.startswith("_")}
                unit_out.update({"idx": idx, "floor_norm": floor, "status": status, "decision": decision,
                                 "matched_listing": {k: (str(v) if isinstance(v, datetime) else v)
                                                     for k, v in match.items()} if match else None,
                                 "candidates": [int(c["id"]) for c in cands] if status == "ambiguous" else [],
                                 "diff": diff, "reactivate": reactivate, "images": images, "has_photo_source": has_photo,
                                 "link_source": u.get("link_source"),
                                 "needs_review": needs_review})
                a_out["units"].append(unit_out)
                report["summary"]["units"] += 1
                report["summary"][status if status in ("new", "match", "ambiguous") else "new"] += 1
                if needs_review:
                    report["summary"]["needs_review"] += 1
                elif status == "new":
                    report["summary"]["ready"] += 1
            a_out["contact_phone"] = _resolve_contact_phone(conn, report.get("partner") or {}, bp, ours, a_out["units"])
            # mūsu telpas šajā ēkā, kuru partnera sarakstā nav → piedāvā atzīmēt kā iznomātas VIENMĒR (agrāk tikai, ja
            # AI nolēma «saraksts pilns» — Domuss 2026-10-24: AI teica «nē», un Ganību d. 25d 1512 m² netika ieteikts).
            # Ķeksis pēc noklusējuma (Raimonds 2026-10-24): atsūtīts dokuments (PDF/Excel) ar telpām vai vairākas telpas
            # = pilns saraksts; tikai viens birojs e-pastā → bez ķekša (var nebūt viss stāsts). Aģents vienmēr var noņemt.
            default_on = bool(report["looks_complete"]) or has_doc or total_units >= 2
            if bp:
                for l in ours:
                    if int(l["id"]) in matched_ids or l.get("occupancy_status") == "occupied":
                        continue   # jau iznomātos nepiedāvā vēlreiz atzīmēt
                    a_out["missing_in_list"].append({
                        "id": int(l["id"]), "street": l["street"], "area_m2": l["area_m2"], "floor": l["floor"],
                        "price": l["price"], "source": l["source"], "on_website": l["on_website"],
                        "wp_synced_at": str(l["wp_synced_at"])[:10] if l["wp_synced_at"] else None,
                        "created_at": str(l["created_at"])[:10] if l["created_at"] else None,
                        "decision": "ask", "default_checked": default_on,
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
    if u.get("offer_type") == "flex":
        extra_bits.append("Flex / coworking birojs — cena ir kopējā mēneša maksa ar visu iekļautu (ne €/m²)")
    elif u.get("offer_type") == "project":
        extra_bits.append("Projekts / būvniecībā — nosacījumi pēc pieprasījuma")
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
        "price_per_m2": _numstr(rate) if (deal == "rent" and u.get("offer_type") != "flex") else "",
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
    want_at = (decisions.get("report_at") or "").strip()
    if want_at and report.get("generated_at") and want_at != report["generated_at"]:
        raise ApplyError("Reports pa to laiku pārlasīts — pārlādē lapu un pārskati lēmumus vēlreiz")
    result = {"created": [], "updated": [], "occupied": [], "errors": [], "images": [], "created_units": []}
    # Atkārtota apstiprināšana (iepriekš bija kļūdas → status «partial»): jau izdarīto NEatkārto.
    prev = (json.loads((d / "meta.json").read_text(encoding="utf-8")).get("apply_result") or {}) if dry_run is False else {}
    prev_created_idx = {str(p[1]) for p in (prev.get("created_units") or [])}
    prev_occupied = {int(x) for x in (prev.get("occupied") or [])}
    image_jobs: list = []
    unit_dec = decisions.get("units") or {}
    web_dec = decisions.get("web") or {}   # {idx: true} — «Arī mājaslapā» (pēc bilžu apstrādes)
    img_dec = decisions.get("images") or {}
    phone_dec = decisions.get("phones") or {}   # {addr_index: "+371 ..."} — aģenta izvēlētais kontakta nr.  # {idx: {sslv, pdf, uploads, own, building, building_fns}} — aģenta bilžu izvēle
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

        result["undo"] = {"updated": [], "occupied": [], "partner_list_id": list_id}
        for ai_, addr in enumerate(report["addresses"]):
            addr_phone = _fmt_phone(phone_dec.get(str(ai_))) if phone_dec.get(str(ai_)) else None
            if not addr_phone and str(ai_) not in phone_dec:
                cp = addr.get("contact_phone")
                if cp is None:   # vecs reports (pirms numura loģikas) — aprēķina tagad
                    try:
                        bp_ = addr.get("bp")
                        cp = _resolve_contact_phone(conn, partner, bp_, _our_listings_safe(conn, bp_["id"]) if bp_ else [], addr["units"])
                    except Exception:
                        conn.rollback()
                        cp = {}
                addr_phone = (cp or {}).get("phone")
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
                                            phone_numbers = COALESCE(%s, phone_numbers),
                                            agent_locked_fields = %s WHERE id=%s""",
                                         (partner_id, u.get("ref"), list_id,
                                          f"Partneris: {partner.get('company') or partner.get('email') or '?'}; saraksts {report.get('list_date') or report.get('received_at','')[:10]}; ref {u.get('ref') or '-'}",
                                          payload["unit"].get("Agent_text_extra") or "", addr_phone, _partner_locked(payload["unit"]), lid))
                            conn.commit()
                        except Exception:
                            conn.rollback()
                        if isinstance(img_dec.get(str(u["idx"])), dict):
                            u["_img_sel"] = img_dec[str(u["idx"])]
                        # flex/coworking (cena par vietu/mēn ar visu iekļautu) — NEKAD uz mājaslapu pa tiešo
                        image_jobs.append((lid, u, bool(web_dec.get(str(u["idx"]))) and u.get("offer_type") != "flex"))
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
                    reactivate = bool(u.get("reactivate")) or (u["matched_listing"].get("occupancy_status") == "occupied")
                    if reactivate:
                        sets.append("occupancy_status = NULL"); sets.append("occupied_from = NULL")
                        sets.append("occupied_note = %s"); vals.append(
                            f"Atkal brīvs — partnera ({partner.get('company') or '?'}) sarakstā {report.get('list_date') or report.get('received_at','')[:10]}")
                        sets.append("occupancy_set_by = %s"); vals.append(requested_by)
                        sets.append("occupancy_set_at = now()")
                    if sets:
                        if diff:
                            sets.append("""agent_locked_fields = (SELECT ARRAY(SELECT DISTINCT x FROM unnest(COALESCE(agent_locked_fields,'{}'::text[]) || ARRAY['price','price_per_m2']) x))""")
                        sets.append("updated_at = now()")
                        try:
                            snap = conn.execute("""SELECT id, price, price_per_m2, agent_locked_fields, partner_id, partner_unit_ref, partner_list_id,
                                                          occupancy_status, occupied_from, occupied_note, occupancy_set_by, occupancy_set_at, wp_post_id
                                                   FROM properties.listings WHERE id=%s""", (lid,)).fetchone()
                            result["undo"]["updated"].append({k: (str(v) if isinstance(v, datetime) else v) for k, v in dict(snap).items()} if snap else {"id": lid})
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
                        snap = conn.execute("""SELECT id, occupancy_status, occupied_from, occupied_note, occupancy_set_by, occupancy_set_at, wp_post_id, on_website
                                               FROM properties.listings WHERE id=%s""", (lid,)).fetchone()
                        result["undo"]["occupied"].append({k: (str(v) if isinstance(v, (datetime,)) else v) for k, v in dict(snap).items()} if snap else {"id": lid})
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
    meta["decisions"] = decisions   # audits: ko aģents tiešām izvēlējās
    meta["status"] = "partial" if result["errors"] else "applied"
    meta["applied_at"] = _now().isoformat()
    meta["apply_result"] = result
    meta["images_status"] = "processing" if image_jobs else None
    meta["image_jobs"] = [[j[0], j[1].get("idx"), bool(j[2]), j[1].get("_img_sel")] for j in image_jobs]
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
    keymap: dict[str, str] = {}   # kandidāta atslēga (URL / rel / fn) → raw faila nosaukums (secībai)
    imgs = (u.get("images") or {}).get("pdf") or {}
    # Aģenta izvēle importa stadijā (decisions.images[idx]); None = automātiski (viss atrastais)
    sel = u.get("_img_sel") or {}
    sel_sslv = sel.get("sslv")            # list[url] | None
    sel_pdf = sel.get("pdf")              # list[rel] | None
    sel_uploads = sel.get("uploads") or []

    # 1) ss.lv — vispirms (download_images var tīrīt img_*.* mapē, ja mainās URL saraksts)
    sslv = (u.get("images") or {}).get("sslv_inbox") or []
    if sel.get("sslv_link"):
        want_link = _bare_sslv(sel["sslv_link"])
        sslv = [c for c in sslv if c.get("link") == want_link] + [c for c in sslv if c.get("link") != want_link]
    if sel_sslv is not None:
        sslv = sslv[:1] if sel_sslv else []   # izvēlētie URL nāk no izvēlētā (1.) kandidāta galerijas
    elif ((u.get("images") or {}).get("links") or {}).get("photos"):
        sslv = []   # partnerim ir savas (Drive) bildes — ss.lv (ar ūdenszīmēm) pēc noklusējuma neņem
    for cand in sslv[:2]:
        try:
            import ai_text_helpers as helpers
            import download_images
            import sslv_import
            info = sslv_import.parse_sslv_url(cand["link"])
            html_doc = sslv_import._fetch_html(info["fetch_url"], helpers.HEADERS, helpers.REQUEST_TIMEOUT)
            gallery = _alive_sslv_images((helpers.extract_gallery_urls(html_doc) or [])[:SSLV_MAX_PHOTOS])
            if sel_sslv is not None:
                want = set(sel_sslv)
                gallery = [g for g in gallery if g in want] or [g for g in sel_sslv if g.startswith("http")]
            if not gallery:
                continue
            jpg_field = sslv_import._format_jpg_field(gallery)
            download_images.STORAGE_ROOT = STORAGE_ROOT
            res = download_images.process_listing(conn, listing_id, jpg_field)
            got = res.get("saved_paths") or []
            if not got:
                continue
            photos.extend(got)
            for gi, gurl in enumerate(gallery):
                keymap[gurl] = f"img_{gi + 1:03d}.jpg"
            conn.execute('UPDATE properties.listings SET "JPG bildes" = %s WHERE id = %s', (jpg_field, listing_id))
            conn.commit()
            out["sslv"] = len(got)
            out["sslv_link"] = cand["link"]
            break
        except Exception as e:
            conn.rollback()
            out["sslv_error"] = f"{type(e).__name__}: {str(e)[:120]}"

    # 2) PDF foto + plāni (tīri) + partnera saišu (Drive) foto/plāni + aģenta augšupielādētās
    links = (u.get("images") or {}).get("links") or {}
    sel_links = sel.get("links")          # list[rel] | None = visas
    # secība pēc AI tipa (image_types_cache): fasāde → interjers → cits (tualete/tehniskās pēdējās) — galvenā
    # bilde mājaslapā = pirmā (prod MCITY 36,3 m² titulbilde bija pods)
    try:
        _tp = json.loads((INTAKE_DIR / intake_id / "image_types_cache.json").read_text(encoding="utf-8"))
    except Exception:
        _tp = {}
    _rank = {"fasade": 0, "interjers": 1, "cits": 3, "plans": 4}
    links = {**links, "photos": sorted(links.get("photos") or [], key=lambda r: _rank.get(_tp.get(r, "interjers"), 2))}
    for kind in ("link_photos", "photos", "plans", "link_plans", "uploads"):
        if kind == "uploads":
            rels = sel_uploads
        elif kind.startswith("link_"):
            rels = links.get(kind[5:], [])
            if sel_links is not None:
                rels = [r for r in rels if r in set(sel_links)]
        else:
            rels = imgs.get(kind, [])
            if sel_pdf is not None:
                rels = [r for r in rels if r in set(sel_pdf)]
        for rel in rels:
            if ".." in str(rel).split("/") or not str(rel).startswith("partner_intake/"):
                continue
            src = STORAGE_ROOT / rel
            if not src.is_file():
                continue
            fn = f"{CLEAN_PREFIX}{uuid.uuid4().hex}{src.suffix.lower() or '.png'}"
            shutil.copy2(src, raw_dir / fn)
            keymap[str(rel)] = fn
            p = f"listings/{listing_id}/raw/{fn}"
            if kind in ("plans", "link_plans"):
                plans.append(p)
                types[fn] = "plans"
                out["plans"] += 1
            else:
                photos.append(p)
                out["pdf"] += 1

    # 3) fasāde no mūsu listinga tajā pašā ēkā — tikai ja nav neviena foto
    own = (u.get("images") or {}).get("own_listings_with_images") or []
    if sel.get("own") is not True:
        own = []   # nekad automātiski — tikai ja aģents importa stadijā izvēlējās
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
    # aģenta secība (1 = titulbilde) → _order.json; piemēro _process_images beigās (pēc ēkas bildēm)
    order = [k for k in (sel.get("order") or []) if isinstance(k, str)]
    if order:
        try:
            (ldir / "_order.json").write_text(json.dumps({"order": order, "keymap": keymap}, ensure_ascii=False), encoding="utf-8")
            out["order"] = len(order)
        except Exception:
            pass
    return out


def _process_images(listing_id: int, building_mode: str = "never", building_fns: Optional[list] = None) -> dict:
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
            # nav nevienas bildes — vismaz ēkas noklusējuma bildes (ja ēkai tādas ir)
            try:
                import building_photos
                bp = conn.execute("SELECT building_profile_id FROM properties.listings WHERE id=%s", (listing_id,)).fetchone()
                if bp and bp["building_profile_id"] and building_mode != "never" and building_fns:
                    try:  # bez AI izmaksām: ēkas citu listingu jau atpazītās fasādes → ēkas bibliotēkā
                        out["harvest"] = building_photos.harvest_from_listings(conn, int(bp["building_profile_id"])).get("added", 0)
                    except Exception:
                        conn.rollback()
                    out["building"] = building_photos.apply_to_listing(conn, listing_id, int(bp["building_profile_id"]),
                                                                       building_mode or "auto", building_fns)
                    if out["building"].get("added"):
                        conn.execute("""UPDATE properties.listings SET "Debug_status" = NULL
                                        WHERE id=%s AND source='agent_partner'""", (listing_id,))
                        conn.commit()
                        out["processed"] = out["building"]["added"]
                        out["ai_analyze"] = "queued"
                        out["status"] = "ok"
                        return out
            except Exception as e:
                conn.rollback()
                out["building_error"] = f"{type(e).__name__}: {str(e)[:120]}"
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
            conn.commit()
        # ēkas noklusējuma bildes (fasāde/pagalms) — ja klasifikācija neatrada fasādi
        try:
            import building_photos
            bp = conn.execute("SELECT building_profile_id FROM properties.listings WHERE id=%s", (listing_id,)).fetchone()
            if bp and bp["building_profile_id"]:
                try:  # bez AI izmaksām: ēkas citu listingu jau atpazītās fasādes → ēkas bibliotēkā
                    out["harvest"] = building_photos.harvest_from_listings(conn, int(bp["building_profile_id"])).get("added", 0)
                except Exception:
                    conn.rollback()
                if building_mode != "never" and building_fns:   # tikai aģenta izvēlētās — nekad automātiski
                    _has_order = (ldir / "_order.json").is_file()
                    out["building"] = building_photos.apply_to_listing(conn, listing_id, int(bp["building_profile_id"]),
                                                                       "always", building_fns,
                                                                       position="end" if _has_order else "front")
                row2 = conn.execute("SELECT local_image_paths_processed FROM properties.listings WHERE id=%s",
                                    (listing_id,)).fetchone()
                processed = [str(x) for x in ((row2 or {}).get("local_image_paths_processed") or [])]
        except Exception as e:
            conn.rollback()
            out["building_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        try:
            processed = _apply_order(conn, listing_id, ldir, processed) or processed
        except Exception as e:
            logger.warning("partner_intake: secība %s: %s", listing_id, e)
        if processed:
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


def _apply_order(conn, listing_id: int, ldir: Path, processed: list[str]) -> Optional[list[str]]:
    """Aģenta izvēlētā bilžu secība (importa stadijā «Secība»: 1 = titulbilde). Numurētās pirmās
    norādītajā secībā, pārējās aiz tām automātiskajā kārtībā. Pārkārto raw + processed (WP ņem pirmo)."""
    op = ldir / "_order.json"
    if not op.is_file():
        return None
    o = json.loads(op.read_text(encoding="utf-8"))
    keymap = o.get("keymap") or {}
    first: list[str] = []
    for k in o.get("order") or []:
        fn = keymap.get(k) or (f"img_user_bp_{k[3:]}" if k.startswith("bp_") else None)
        if fn and fn not in first:
            first.append(fn)
    if not first:
        return None

    def _sort(paths: list[str]) -> list[str]:
        byname = {Path(str(p).replace("\\", "/")).name: p for p in paths}
        ordered = [byname[f] for f in first if f in byname]
        return ordered + [p for p in paths if p not in ordered]

    row = conn.execute("SELECT local_image_paths_raw, local_image_paths_processed FROM properties.listings WHERE id=%s",
                       (listing_id,)).fetchone()
    raw = _sort([str(x) for x in (row["local_image_paths_raw"] or [])]) if row else None
    proc = _sort([str(x) for x in (processed or (row["local_image_paths_processed"] if row else []) or [])])
    conn.execute("UPDATE properties.listings SET local_image_paths_raw=%s::text[], local_image_paths_processed=%s::text[] WHERE id=%s",
                 (raw, proc or None, listing_id))
    conn.commit()
    try:
        import image_classify
        man = image_classify.load_manifest(STORAGE_ROOT, listing_id) or {}
        for fn, info in man.items():
            if isinstance(info, dict):
                info["featured"] = (fn == first[0])
        image_classify.save_manifest(STORAGE_ROOT, listing_id, man)
    except Exception:
        pass
    return proc


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
            sel = u.get("_img_sel") or {}
            fns = sel.get("building_fns") or []
            r["ai"] = _process_images(lid, "always" if fns else "never", fns or None)
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
            sel_map = {int(j[0]): j[3] for j in (meta.get("image_jobs") or []) if len(j) > 3 and isinstance(j[3], dict)}
            jobs = []
            for lid, idx in (meta.get("apply_result") or {}).get("created_units") or []:
                if int(lid) in done_ids or str(idx) not in units:
                    continue
                uu = dict(units[str(idx)])
                if int(lid) in sel_map:
                    uu["_img_sel"] = sel_map[int(lid)]
                jobs.append((int(lid), uu, web_map.get(int(lid), False)))
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


def _alive_sslv_images(urls: list[str]) -> list[str]:
    """i.ss.lv dzēstam sludinājumam atdod 200 + ~49 baitu tukšu GIF — panelī tukši sīktēli (Brīvības g. 432, 2026-10-26).
    Pārbauda 1. bildi; ja tā mirusi — pārbauda visas un patur tikai dzīvās (>2 KB)."""
    if not urls:
        return []
    import download_images

    def alive(u: str) -> bool:
        try:
            data = download_images.download_one(u)
            return bool(data) and len(data) > 2000
        except Exception:
            return False
    if alive(urls[0]):
        return urls
    return [u for u in urls if alive(u)]


def address_override(intake_id: str, addr_idx: int, bp_id: Optional[int]) -> dict:
    """Panelis: aģents sasaista piedāvājuma adresi ar DB ēku (vai noņem sasaisti) → salīdzināšana no jauna
    (mūsējie šajā ēkā, «nav sarakstā», ss.lv kandidāti pēc DB adreses) + bilžu sagatavošana."""
    d = INTAKE_DIR / intake_id
    meta_p = d / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    if meta.get("status") in ("applied",):
        raise ApplyError("Piedāvājums jau apstiprināts — ēku maini listingā")
    ov = meta.get("address_overrides") or {}
    if bp_id:
        ov[str(addr_idx)] = {"bp_id": int(bp_id)}
    else:
        ov.pop(str(addr_idx), None)
    meta["address_overrides"] = ov
    meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    rep = reconcile(intake_id)
    start_prepare_unit_images(intake_id)
    return {"summary": rep["summary"], "address": rep["addresses"][addr_idx] if addr_idx < len(rep["addresses"]) else None}


def search_buildings(conn, q: str, limit: int = 12) -> list[dict]:
    """Ēku meklēšana panelim: adrese / nosaukums / pilsēta, bez diakritikas, ar listingu skaitu (aktuālie dati)."""
    # PAREIZĀ diakritikas tabula (š→s, ž→z). Pārējā kodā vēsturiski š↔ž ir samainīti vietām — tur abās pusēs vienādi,
    # bet šeit lietotājs raksta ASCII («lielvarzi»), tāpēc jābūt pareizi (2026-10-26)
    tr = str.maketrans("āēīōūčšžģķļņŗ", "aeioucszgklnr")
    toks = [t for t in re.sub(r"[^a-z0-9]+", " ", (q or "").lower().translate(tr)).split() if t]
    if not toks:
        return []
    # lower() pirms translate neapstrādā «Ķ» (DB locale) → translate arī lielos burtus, tad lower (Ķekavas novads)
    T = ("lower(translate(coalesce(bp.full_address,'') || ' ' || coalesce(bp.building_name,'') || ' ' || coalesce(bp.city,'') || ' ' || coalesce(bp.street,''),"
         "'āēīōūčšžģķļņŗĀĒĪŌŪČŠŽĢĶĻŅŖ','aeioucszgklnraeioucszgklnr'))")
    where = " AND ".join(f"{T} LIKE %s" for _ in toks)
    rows = conn.execute(
        f"""SELECT bp.id, bp.full_address, bp.city, bp.building_name,
                   (SELECT count(*) FROM properties.listings l WHERE l.building_profile_id = bp.id AND l."Debug_status" = 'ok') AS n,
                   (SELECT count(*) FROM properties.listings l WHERE l.building_profile_id = bp.id AND l."Debug_status" = 'ok' AND l.on_website) AS n_web
              FROM properties.building_profiles bp
             WHERE {where}
             ORDER BY n DESC, bp.full_address LIMIT %s""",
        tuple(f"%{t}%" for t in toks) + (limit,),
    ).fetchall()
    return [{"id": int(r["id"]), "full_address": r["full_address"], "city": r["city"], "building_name": r["building_name"],
             "listings": int(r["n"]), "on_website": int(r["n_web"])} for r in rows]


def _bare_sslv(link: Optional[str]) -> Optional[str]:
    """«https://www.ss.lv/msg/...» / «ss.com/msg/...» → «ss.lv/msg/...» (kā scrape_inbox.link un kandidātos)."""
    if not link:
        return None
    l = re.sub(r"^https?://", "", link.strip()).replace("www.", "", 1)
    return re.sub(r"^ss\.com/", "ss.lv/", l).split("#")[0]


def unit_image_candidates(intake_id: str, idx: str, prepare: bool = False, pick: Optional[str] = None) -> dict:
    """Importa stadijai: ko pievienos šai telpai — ss.lv galerija (dzīvā, kešota), PDF foto/plāni,
    aģenta augšupielādes, ēkas noklusējuma bildes, mūsu listinga fasāde."""
    d = INTAKE_DIR / intake_id
    report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    addr, unit = None, None
    for a in report.get("addresses", []):
        for u in a.get("units", []):
            if str(u.get("idx")) == str(idx):
                addr, unit = a, u
    if unit is None:
        raise ApplyError("telpa nav atrasta")
    imgs = unit.get("images") or {}
    out: dict = {"idx": unit["idx"], "pdf": imgs.get("pdf") or {"photos": [], "plans": []},
                 "sslv": [], "sslv_link": None, "uploads": [], "building": [], "bp_id": None,
                 "own_listing": (imgs.get("own_listings_with_images") or [None])[0]}
    cache_p = d / "sslv_gallery_cache.json"
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.is_file() else {}
    cands = imgs.get("sslv_inbox") or []
    # visi kandidāti panelim («vēl 3 sludinājumi» — aģents var pārslēgt); pick = aģenta izvēlētais links
    out["sslv_candidates"] = [{"link": c["link"], "url": c["link"] if c["link"].startswith("http") else f"https://www.{c['link']}",
                               "area_m2": c.get("area_m2"), "floor": c.get("floor"), "price": c.get("price"),
                               "is_agent": bool(c.get("is_agent")), "from_email": bool(c.get("from_email")),
                               "score": c.get("same_room_score"), "n": len(cache.get(c["link"]) or []) if c["link"] in cache else None}
                              for c in cands[:5]]
    pick = _bare_sslv(pick)
    if pick:
        cands = [c for c in cands if c["link"] == pick] + [c for c in cands if c["link"] != pick]
    # Raimonds 2026-10-26: ja 1. ss.lv sludinājums miris (tukšas bildes) — ņem NĀKAMO kandidātu; ja neviena —
    # skaidri «nav», nevis tukši rāmīši. Kešā (sslv_gallery_cache.json) mirušas galerijas pārbauda vēlreiz.
    out["sslv_pending"] = False
    for cand in cands[:3]:
        link = cand["link"]
        if link not in cache:
            if not prepare and link != pick:
                out["sslv_pending"] = True
                out["sslv_link"] = link if link.startswith("http") else f"https://www.{link}"
                break
            try:
                import ai_text_helpers as helpers
                import sslv_import
                info = sslv_import.parse_sslv_url(link)
                html_doc = sslv_import._fetch_html(info["fetch_url"], helpers.HEADERS, helpers.REQUEST_TIMEOUT)
                urls = (helpers.extract_gallery_urls(html_doc) or [])[:SSLV_MAX_PHOTOS]
                cache[link] = _alive_sslv_images(urls)
            except Exception as e:
                cache[link] = []
                out["sslv_error"] = f"{type(e).__name__}"
            cache_p.write_text(json.dumps(cache), encoding="utf-8")
        elif cache.get(link) and (prepare or link == pick) and not _alive_sslv_images(cache[link][:1]):
            # vecs kešs ar mirušām bildēm (pirms 2026-10-26 pārbaudes) → pārvērtē
            cache[link] = _alive_sslv_images(cache[link])
            cache_p.write_text(json.dumps(cache), encoding="utf-8")
        if cache.get(link):
            out["sslv"] = cache[link]
            out["sslv_link"] = link if link.startswith("http") else f"https://www.{link}"
            out.pop("sslv_error", None)
            break
    if cands and not out["sslv"] and not out["sslv_pending"]:
        out["sslv_error"] = "ss.lv sludinājuma bildes vairs nav pieejamas (dzēsts?)"
    L = imgs.get("links") or {"photos": [], "plans": [], "errors": []}
    # dublikāti arī rādot (vecie reporti pirms Drive-ID dedupe)
    out["links"] = {"photos": _dedupe_files(L.get("photos") or []), "plans": _dedupe_files(L.get("plans") or []), "errors": L.get("errors") or []}
    want = out["sslv"] + (out["pdf"].get("photos") or []) + (out["links"].get("photos") or [])[:20]
    out["types"] = _candidate_types(d, want, prepare)
    out["types_pending"] = bool(out.get("sslv_pending")) or any(k not in out["types"] for k in want)
    up = d / "uploads" / str(unit["idx"])
    if up.is_dir():
        out["uploads"] = sorted(f"partner_intake/{intake_id}/uploads/{unit['idx']}/{f.name}" for f in up.iterdir() if f.is_file())
    bp_id = (addr.get("bp") or {}).get("id")
    if bp_id:
        import building_photos
        out["bp_id"] = int(bp_id)
        out["building"] = building_photos.list_photos(int(bp_id))
    return out


def _candidate_types(d: Path, keys: list[str], prepare: bool = False, limit: int = 40, workers: int = 6) -> dict:
    """Importa stadijai: kuras kandidātbildes ir fasāde (image_classify, kešots intake mapē).
    Lai aģentam PIEDĀVĀTU ēkas fasādi, ja izvēlētajās nav nevienas. ~0,1 ¢/bilde, paralēli.
    prepare=False → tikai kešs (panelis nedrīkst gaidīt OpenAI); prepare=True → fona sagatavošana."""
    cache_p = d / "image_types_cache.json"
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.is_file() else {}
    todo = [k for k in keys if k not in cache] if prepare else []
    if todo:
        try:
            import image_classify
            from concurrent.futures import ThreadPoolExecutor

            def _one(key: str):
                try:
                    if key.startswith("http"):
                        import download_images
                        data = download_images.download_one(key)
                    else:
                        p = STORAGE_ROOT / key
                        data = p.read_bytes() if p.is_file() else None
                    if not data:
                        return key, None
                    r = image_classify._classify_one(data, key.rsplit("/", 1)[-1])
                    return key, (r or {}).get("type")
                except Exception:
                    return key, None

            with ThreadPoolExecutor(max_workers=workers) as ex:
                for k, t in ex.map(_one, todo[:limit]):
                    if t:
                        cache[k] = t
            cache_p.write_text(json.dumps(cache), encoding="utf-8")
        except Exception as e:
            logger.warning("partner_intake: bilžu tipi: %s", e)
    return {k: cache[k] for k in keys if k in cache}


THUMB_W = 320


def make_thumb(base: Path, rel: str, w: int = THUMB_W) -> Optional[Path]:
    """Sīktēls intake mapē `_thumbs/<sha1(rel|w)>.jpg` (to pašu lieto agent_api file endpoint ?w=)."""
    import hashlib
    p = base / rel
    if not p.is_file() or p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        return None
    tdir = base / "_thumbs"
    tdir.mkdir(exist_ok=True)
    tp = tdir / (hashlib.sha1(f"{rel}|{w}".encode()).hexdigest() + ".jpg")
    if tp.is_file():
        return tp
    try:
        from PIL import Image
        im = Image.open(p)
        im.thumbnail((w, w))
        im.convert("RGB").save(tp, "JPEG", quality=80)
        return tp
    except Exception:
        return None


def prepare_unit_images(intake_id: str) -> dict:
    """Fonā pēc reconcile: visām telpām ss.lv galerija + bilžu tipi (fasāde?) → kešs, lai panelis atver
    «mainīt bildes» uzreiz (agrāk katra atvēršana sauca OpenAI un karājās 20–40 s)."""
    d = INTAKE_DIR / intake_id
    report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    done, errs = 0, 0
    # 1) ss.lv galerijas (kešs) + visu telpu kandidātu atslēgas; 2) VIENA klasifikācija visām reizē
    #    (10 paralēli, atslēgas, kas kopīgas vairākām telpām — vienreiz)
    keys: list[str] = []
    for a in report.get("addresses", []):
        for u in a.get("units", []):
            try:
                c = unit_image_candidates(intake_id, str(u["idx"]), prepare=False)
                if c.get("sslv_pending"):
                    c = _prep_sslv(d, u) or c
                keys.extend(c.get("sslv") or [])
                keys.extend((c["pdf"].get("photos") or []) + (c["links"].get("photos") or [])[:20])
                done += 1
            except Exception as e:
                errs += 1
                logger.info("partner_intake: prep %s/%s: %s", intake_id, u.get("idx"), e)
    keys = list(dict.fromkeys(keys))
    # sīktēli lokālajiem failiem jau tagad (panelis pirmo reizi atver uzreiz, ne pa vienam ģenerējot)
    base = d.resolve()
    prefix = f"partner_intake/{intake_id}/"
    for k in keys:
        if k.startswith(prefix):
            make_thumb(base, k[len(prefix):])
    for a in report.get("addresses", []):
        for u in a.get("units", []):
            for rel in (((u.get("images") or {}).get("links") or {}).get("plans") or []) + (((u.get("images") or {}).get("pdf") or {}).get("plans") or []):
                if rel.startswith(prefix):
                    make_thumb(base, rel[len(prefix):])
    try:
        _candidate_types(d, keys, prepare=True, limit=600, workers=10)
    except Exception as e:
        errs += 1
        logger.warning("partner_intake: prep tipi %s: %s", intake_id, e)
    try:
        meta_p = d / "meta.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        meta["images_prep"] = "done"
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    except Exception:
        pass
    return {"units": done, "errors": errs}


def _prep_sslv(d: Path, unit: dict) -> Optional[dict]:
    """Telpas ss.lv galerija → kešs (sslv_gallery_cache.json). Atgriež {sslv: [...]} vai None."""
    cands = (unit.get("images") or {}).get("sslv_inbox") or []
    if not cands:
        return None
    link = cands[0]["link"]
    cache_p = d / "sslv_gallery_cache.json"
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.is_file() else {}
    if link not in cache:
        try:
            import ai_text_helpers as helpers
            import sslv_import
            info = sslv_import.parse_sslv_url(link)
            html_doc = sslv_import._fetch_html(info["fetch_url"], helpers.HEADERS, helpers.REQUEST_TIMEOUT)
            cache[link] = (helpers.extract_gallery_urls(html_doc) or [])[:SSLV_MAX_PHOTOS]
        except Exception:
            cache[link] = []
        cache_p.write_text(json.dumps(cache), encoding="utf-8")
    imgs = unit.get("images") or {}
    return {"sslv": cache.get(link) or [], "pdf": imgs.get("pdf") or {"photos": []}, "links": imgs.get("links") or {"photos": []}}


def start_prepare_unit_images(intake_id: str) -> None:
    import threading
    threading.Thread(target=prepare_unit_images, args=(intake_id,), name=f"pi-prep-{intake_id}", daemon=True).start()


def save_unit_upload(intake_id: str, idx: str, filename: str, data: bytes) -> str:
    """Aģenta bilde konkrētai telpai importa stadijā → partner_intake/<id>/uploads/<idx>/."""
    if not re.fullmatch(r"\d{1,4}", str(idx)):
        raise ApplyError("nederīgs idx")
    if not data or len(data) > 20 * 1024 * 1024:
        raise ApplyError("fails tukšs vai par lielu")
    try:
        from PIL import Image
        import io as _io
        Image.open(_io.BytesIO(data)).verify()
    except Exception:
        raise ApplyError("nav derīga bilde")
    ext = Path(filename or "").suffix.lower()
    ext = ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
    d = INTAKE_DIR / intake_id / "uploads" / str(idx)
    d.mkdir(parents=True, exist_ok=True)
    fn = f"up_{uuid.uuid4().hex}{ext}"
    (d / fn).write_bytes(data)
    return f"partner_intake/{intake_id}/uploads/{idx}/{fn}"


def rollback(intake_id: str, requested_by: Optional[str]) -> dict:
    """Atsauc apstiprināšanu: IZVEIDOTOS listingus dzēš pilnībā (queue action=delete → worker: WP posts,
    bildes, pdf, rinda); atjaunotos/iznomātos NEaiztiek (partnera dati paliek). Meta → «ready», lai var
    apstiprināt vēlreiz. Raimonds 2026-10-23 (MCITY nepareizas bildes)."""
    d = INTAKE_DIR / intake_id
    meta_p = d / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    ar = meta.get("apply_result") or {}
    created = [int(x) for x in (ar.get("created") or [])]
    undo = ar.get("undo") or {}
    out = {"deleted_queued": 0, "already_gone": 0, "reverted_updated": 0, "reverted_occupied": 0, "republished": 0}
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        # atjaunotie → vecās vērtības (cena, lock, partner_*)
        for sn in undo.get("updated") or []:
            if not sn.get("id") or "price" not in sn:
                continue
            conn.execute("""UPDATE properties.listings SET price=%s, price_per_m2=%s, agent_locked_fields=%s, partner_id=%s,
                            partner_unit_ref=%s, partner_list_id=%s, occupancy_status=%s, occupied_from=%s, occupied_note=%s,
                            occupancy_set_by=%s, updated_at=now() WHERE id=%s""",
                         (sn.get("price"), sn.get("price_per_m2"), sn.get("agent_locked_fields"), sn.get("partner_id"),
                          sn.get("partner_unit_ref"), sn.get("partner_list_id"), sn.get("occupancy_status"), sn.get("occupied_from"),
                          sn.get("occupied_note"), sn.get("occupancy_set_by"), sn["id"]))
            if sn.get("wp_post_id"):
                conn.execute("""INSERT INTO properties.wp_export_queue (listing_id, status, requested_by) VALUES (%s,'pending',%s)
                                ON CONFLICT (listing_id) WHERE status IN ('pending','processing') DO NOTHING""", (sn["id"], requested_by or "rollback"))
            out["reverted_updated"] += 1
        # iznomātie → atpakaļ brīvi (+ atpakaļ mājaslapā, ja tur bija)
        for sn in undo.get("occupied") or []:
            if not sn.get("id"):
                continue
            conn.execute("""UPDATE properties.listings SET occupancy_status=%s, occupied_from=%s, occupied_note=%s, occupancy_set_by=%s,
                            partner_id=NULL, partner_unit_ref=NULL, partner_list_id=NULL, updated_at=now() WHERE id=%s""",
                         (sn.get("occupancy_status"), sn.get("occupied_from"), sn.get("occupied_note"), sn.get("occupancy_set_by"), sn["id"]))
            out["reverted_occupied"] += 1
            if sn.get("wp_post_id") or sn.get("on_website"):
                conn.execute("UPDATE properties.wp_export_queue SET status='cancelled' WHERE listing_id=%s AND status IN ('pending','processing')", (sn["id"],))
                conn.execute("""INSERT INTO properties.wp_export_queue (listing_id, status, requested_by) VALUES (%s,'pending',%s)""",
                             (sn["id"], requested_by or "rollback (atpakaļ webā)"))
                out["republished"] += 1
        if undo.get("partner_list_id"):
            try:
                conn.execute("DELETE FROM properties.partner_lists WHERE id=%s", (undo["partner_list_id"],))
            except Exception:
                conn.rollback()
        for lid in created:
            row = conn.execute("SELECT id, wp_post_id FROM properties.listings WHERE id=%s AND source='agent_partner'", (lid,)).fetchone()
            if not row:
                out["already_gone"] += 1
                continue
            conn.execute("UPDATE properties.wp_export_queue SET status='cancelled' WHERE listing_id=%s AND status IN ('pending','processing')", (lid,))
            conn.execute("""INSERT INTO properties.wp_export_queue (listing_id, action, status, wp_post_id, requested_by)
                            VALUES (%s,'delete','pending',%s,%s)""", (lid, row["wp_post_id"], requested_by or "partner_intake rollback"))
            out["deleted_queued"] += 1
        conn.commit()
    hist = meta.setdefault("rollback_history", [])
    hist.append({"at": _now().isoformat(), "by": requested_by, "apply_result": ar, "decisions": meta.get("decisions")})
    for k in ("apply_result", "applied_at", "images_status", "images_result", "image_jobs", "decisions", "apply_history"):
        meta.pop(k, None)
    meta["status"] = "ready"
    meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return out


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
                    "source": meta.get("source") or "imap",   # imap | panel | drive | web — paneļa cilnēm
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


def _floor_num(x) -> Optional[int]:
    m = re.search(r"\d+", str(x or ""))
    if m:
        return int(m.group(0))
    s = str(x or "").lower()
    if "pagrab" in s or "cokol" in s or s.strip().startswith("0"):
        return 0
    return None


def _assign_links_by_geometry(intake_id: str, analysis: dict) -> dict:
    """Telpa ↔ saite PĒC PDF TABULAS ĢEOMETRIJAS (links.json geo: rindas platības/stāvs/noma), nevis AI.
    AI piesaiste ir nestabila (prod: 36,3 m² ← 388 m² mape ar 36 foto, 77,2 m² ← cita mape). Ja tieši
    viena saite satur telpas platību (±0,06 m² vai ±0,5 %) — tā; ja vairākas — stāvs + nomas likme izšķir;
    ja neviena — paliek AI izvēle. Saglabā `link_source` (geometry|ai)."""
    d = INTAKE_DIR / intake_id
    if not (d / "links.json").is_file():
        return {}
    links = json.loads((d / "links.json").read_text(encoding="utf-8"))
    # mapes failu nosaukumos minētās platības (neatkarīgs signāls, der arī PDF bez tabulas līnijām)
    for l in links:
        names = []
        mp = d / "links" / l["id"] / "_files.json"
        if mp.is_file():
            try:
                names = [f.get("name") or "" for f in json.loads(mp.read_text(encoding="utf-8")).get("files") or []]
            except Exception:
                names = []
        names = names + [str(x) for x in (l.get("rows") or [])]   # Drive skenējums: mapes NOSAUKUMS ir rindā («1000 m² (2.stāvs), Skanstes 54A»)
        l["name_areas"] = sorted({float(m.group(1).replace(",", ".")) for n in names
                                  for m in re.finditer(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:m2|m²|kv\.?m|kvm|sqm|sq\.?\s*m)", n, re.I)})
        l["name_floors"] = sorted({int(m.group(1)) for n in names for m in re.finditer(r"(?<![\d.])(\d{1,2})\.?\s*st(?:āvs|avs|\.|_|)", n, re.I)})
    stats = {"geometry": 0, "folder": 0, "ai": 0, "none": 0}
    for a in analysis.get("addresses") or []:
        for u in a.get("units") or []:
            area = _num(u.get("area_m2"))
            if not area:
                stats["ai" if u.get("link_ids") else "none"] += 1
                continue
            fl = _floor_num(u.get("floor"))
            rate = _num(u.get("rent_per_m2_min"))
            best: list[tuple[int, str]] = []
            for l in links:
                score = 0
                if any(abs(x - area) <= max(0.06, 0.005 * area) for x in l.get("name_areas") or []):
                    score = 6   # mapes failu nosaukumā šī platība
                    if fl is not None and l.get("name_floors"):
                        score += 2 if fl in l["name_floors"] else -4
                for g in l.get("geo") or []:
                    if not any(abs(x - area) <= max(0.06, 0.005 * area) for x in g.get("areas") or []):
                        continue
                    sc = 10 + (6 if score == 6 else 0)
                    gf = _floor_num(g.get("floor"))
                    if fl is not None and gf is not None:
                        sc += 3 if gf == fl else -5
                    if rate and any(abs(x - rate) < 0.01 for x in g.get("rents") or []):
                        sc += 2
                    score = max(score, sc)
                if score > 0:
                    best.append((score, l["id"]))
            if best:
                best.sort(key=lambda t: -t[0])
                top = best[0][0]
                chosen = [lid for sc, lid in best if sc == top]
                u["link_ids"] = chosen[:1] if len(chosen) > 1 and fl is None else chosen
                u["link_source"] = "geometry" if top >= 10 else "folder"
                stats[u["link_source"]] += 1
            else:
                u["link_source"] = "ai" if u.get("link_ids") else "none"
                stats["ai" if u.get("link_ids") else "none"] += 1
    (d / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("partner_intake %s: saišu piesaiste %s", intake_id, stats)
    return stats


MAX_LINKS_FETCH = int(os.getenv("PARTNER_MAX_LINKS", "40"))


def fetch_unit_links(intake_id: str, analysis: dict, all_links: bool = False) -> dict:
    """Saites (Drive mapes) → links/<Lid>/ (bildes + plānu PDF → PNG). all_links=True — visas (līdz
    MAX_LINKS_FETCH), lai mapju failu nosaukumi («3.st 58.40m2», «MCU_1.st 56.2m2») var kalpot piesaistei."""
    import link_media
    d = INTAKE_DIR / intake_id
    links = json.loads((d / "links.json").read_text(encoding="utf-8")) if (d / "links.json").is_file() else []
    used = ({l["id"] for l in links[:MAX_LINKS_FETCH]} if all_links else
            {lid for a in analysis.get("addresses") or [] for u in a.get("units") or [] for lid in (u.get("link_ids") or [])})
    out = {}
    from concurrent.futures import ThreadPoolExecutor
    todo = [l for l in links if l["id"] in used]
    with ThreadPoolExecutor(max_workers=4) as ex:   # mapes paralēli (katrā vēl 6 faili paralēli)
        fetched = dict(zip([l["id"] for l in todo], ex.map(lambda l: link_media.fetch_link(d, l["id"], l["url"]), todo)))
    for l in todo:
        res = fetched[l["id"]]
        for f in res.get("files") or []:
            if f.get("type") == "pdf" and f.get("file"):
                png = d / "links" / l["id"] / (f["file"] + ".plan.png")
                if not png.exists():
                    link_media.render_pdf_first_page((d / "links" / l["id"] / f["file"]).read_bytes(), png)
        out[l["id"]] = {"files": len(res.get("files") or []), "error": res.get("error")}
    return out


def _link_images_for_unit(d: Path, intake_id: str, unit: dict) -> dict:
    """Telpas saišu bildes: foto (partnera paša — tīras) un plāni (PDF 1. lapa / faila nosaukumā «plān»)."""
    photos, plans, errors = [], [], []
    for lid in unit.get("link_ids") or []:
        man_p = d / "links" / lid / "_files.json"
        if not man_p.is_file():
            continue
        man = json.loads(man_p.read_text(encoding="utf-8"))
        if man.get("error"):
            errors.append(f"{lid}: {man['error']}")
        for f in man.get("files") or []:
            fn = f.get("file")
            if not fn:
                continue
            rel = f"partner_intake/{intake_id}/links/{lid}/{fn}"
            if f.get("type") == "image":
                (plans if re.search(r"pl[aā]n|layout|floor", fn, re.I) else photos).append(rel)
            elif f.get("type") == "pdf" and (d / "links" / lid / (fn + ".plan.png")).is_file():
                plans.append(rel + ".plan.png")
    return {"photos": _dedupe_files(photos), "plans": _dedupe_files(plans), "errors": errors}


def _dedupe_files(rels: list[str]) -> list[str]:
    """Tie paši faili no dažādām saitēm (tā pati Drive mape divreiz, kopijas) → viens."""
    import hashlib
    seen: set[str] = set()
    out: list[str] = []
    for rel in dict.fromkeys(rels):
        p = STORAGE_ROOT / rel
        try:
            h = hashlib.sha1(p.read_bytes()).hexdigest() if p.is_file() else rel
        except Exception:
            h = rel
        if h in seen:
            continue
        seen.add(h)
        out.append(rel)
    return out


def process_intake(intake_id: str, do_notify: bool = True) -> dict:
    d = INTAKE_DIR / intake_id
    meta_p = d / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    try:
        analysis = analyze(intake_id)
        try:
            fetch_unit_links(intake_id, analysis, all_links=True)   # vispirms VISAS (mapes nosaukumi = signāls)
        except Exception as e:
            logger.warning("partner_intake %s: saišu lejupielāde: %s", intake_id, e)
        try:
            _assign_links_by_geometry(intake_id, analysis)
        except Exception as e:
            logger.warning("partner_intake %s: saišu piesaiste: %s", intake_id, e)
        try:
            fetch_unit_links(intake_id, analysis)   # plāni (PDF → PNG) piesaistītajām
        except Exception as e:
            logger.warning("partner_intake %s: saišu plāni: %s", intake_id, e)
        report = reconcile(intake_id, analysis)
        start_prepare_unit_images(intake_id)
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
                import partner_drive
                await loop.run_in_executor(None, partner_drive.maybe_scan_all)
            except Exception as e:
                logger.warning("partner_intake: drive skenējums: %s", e)
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
