"""Sadarbības partneru Google Drive mapju AUTOMĀTISKA skenēšana (Raimonds 2026-10-25).

Skanstes City (Mārtiņš Škapars) savus brīvos birojus tur publiskā Drive mapē: katra telpa = apakšmape
«<platība> m² (<stāvs>), <adrese>[ - piezīme]» ar foto + plānu; cenas atsevišķā PDF (tabula: platība,
stāvs, tips, ēkas kods, statuss, noma, fit-out, apsaimniekošana; FLEX tabulas). Tādu partneru var būt vairāki.

Princips: skenējam KATRU DIENU (mapes saraksts + cenu fails — bez AI, nemaksā), un tikai tad, ja kaut kas
MAINĪJIES (jauna/dzēsta mape, cits cenu fails), uzbūvējam ieplūdi tieši tādu pašu kā e-pastam
(partner_intake: analyze → reconcile → Telegram → panelī «Partneru piedāvājumi» → apstiprina Raimonds/Ieva).
Ja nekas nav mainījies — nekas nenotiek. Ja iepriekšējais šī avota piedāvājums vēl nav apstiprināts —
jaunu neveido (lai nekrājas), nākamajā dienā mēģina vēlreiz.

Avoti glabājas volume failā partner_intake/_drive_sources.json (bez DB migrācijas); panelī var pievienot/
dzēst/«Skenēt tagad».
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("partner_drive")

SCHEDULES = {"daily": timedelta(hours=24), "weekly": timedelta(days=7)}
MAX_FOLDER_DEPTH = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sources_path() -> Path:
    import partner_intake
    partner_intake.INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    return partner_intake.INTAKE_DIR / "_drive_sources.json"


def load_sources() -> list[dict]:
    p = _sources_path()
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("partner_drive: _drive_sources.json nenolasās")
        return []


def save_sources(sources: list[dict]) -> None:
    _sources_path().write_text(json.dumps(sources, ensure_ascii=False, indent=1), encoding="utf-8")


def _drive_id(value: str, kind: str) -> Optional[str]:
    """Saite vai tīrs ID → ID. kind = folder|file."""
    import link_media
    v = (value or "").strip()
    if not v:
        return None
    c = link_media.classify_url(v)
    if c and c[0] == f"gdrive_{kind}":
        return c[1]
    if re.fullmatch(r"[A-Za-z0-9_-]{15,}", v):
        return v
    return None


def upsert_source(data: dict) -> dict:
    """Panelis: {id?, name, folder, price_files: [saite...], agent_email, schedule, enabled}."""
    folder_id = _drive_id(data.get("folder") or data.get("folder_id") or "", "folder")
    if not folder_id:
        raise ValueError("Drive mapes saite nav atpazīta")
    price_ids = []
    for x in data.get("price_files") or []:
        fid = _drive_id(x, "file")
        if fid:
            price_ids.append(fid)
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Partnera nosaukums ir obligāts")
    sources = load_sources()
    sid = (data.get("id") or "").strip() or uuid.uuid4().hex[:8]
    cur = next((s for s in sources if s.get("id") == sid), None)
    src = {
        **(cur or {}),
        "id": sid, "name": name, "folder_id": folder_id, "price_file_ids": price_ids,
        "agent_email": (data.get("agent_email") or (cur or {}).get("agent_email") or "raimonds@rgcommerce.lv").strip().lower(),
        "contact_name": (data.get("contact_name") or (cur or {}).get("contact_name") or "").strip(),
        "contact_email": (data.get("contact_email") or (cur or {}).get("contact_email") or "").strip(),
        "contact_phone": (data.get("contact_phone") or (cur or {}).get("contact_phone") or "").strip(),
        "notes": (data.get("notes") or (cur or {}).get("notes") or "").strip(),
        "schedule": data.get("schedule") if data.get("schedule") in SCHEDULES else (cur or {}).get("schedule") or "daily",
        "enabled": bool(data.get("enabled", (cur or {}).get("enabled", True))),
        "created_at": (cur or {}).get("created_at") or _now().isoformat(),
    }
    if cur is None:
        sources.append(src)
    else:
        sources[sources.index(cur)] = src
    save_sources(sources)
    return src


def delete_source(sid: str) -> bool:
    sources = load_sources()
    new = [s for s in sources if s.get("id") != sid]
    if len(new) == len(sources):
        return False
    save_sources(new)
    return True


# ───────────────────────────── skenēšana ─────────────────────────────

def _fingerprint(entries: list[dict], price_blobs: dict[str, bytes]) -> str:
    h = hashlib.sha1()
    for e in sorted(entries, key=lambda x: (x.get("path") or "", x.get("id") or "")):
        h.update(f"{e.get('kind')}|{e.get('id')}|{e.get('path')}\n".encode("utf-8"))
    for fid in sorted(price_blobs):
        h.update(f"price|{fid}|{hashlib.sha1(price_blobs[fid]).hexdigest()}\n".encode("utf-8"))
    return h.hexdigest()


def _unit_folders(entries: list[dict]) -> list[dict]:
    """Mapes, kas izskatās pēc telpām: nosaukumā platība (m²) VAI dziļākā «Nr.N» mape (flex). Vecākmapes,
    kurām ir apakšmapes ar platību, izlaiž (lai «FLEX Biroji» nenonāk kā telpa)."""
    folders = [e for e in entries if e.get("kind") == "folder"]
    paths = {f["path"] for f in folders}
    out = []
    for f in folders:
        name = f["path"].split("/")[-1]
        has_children = any(p.startswith(f["path"] + "/") for p in paths)
        is_unit = bool(re.search(r"\d\s*(?:m2|m²|kv\.?m)", name, re.I)) or bool(re.search(r"\bnr\.?\s*\d", name, re.I))
        if is_unit and not has_children:
            out.append(f)
    return out


def _body_text(src: dict, entries: list[dict], scanned_at: datetime) -> str:
    units = _unit_folders(entries)
    files_by_folder: dict[str, int] = {}
    for e in entries:
        if e.get("kind") != "folder" and "/" in (e.get("path") or ""):
            files_by_folder[e["path"].rsplit("/", 1)[0]] = files_by_folder.get(e["path"].rsplit("/", 1)[0], 0) + 1
    lines = [
        f"AUTOMĀTISKS SKENĒJUMS — partnera {src['name']} publiskā Google Drive mape ({scanned_at.strftime('%Y-%m-%d')}).",
        f"Partneris: {src['name']}" + (f"; kontakts: {src.get('contact_name')}" if src.get("contact_name") else "")
        + (f" {src.get('contact_email')}" if src.get("contact_email") else "")
        + (f" {src.get('contact_phone')}" if src.get("contact_phone") else ""),
        "Cenas, statuss (Pieejams / Q3 2026 = pieejams no …) un apsaimniekošana ir pielikumā (cenu fails). "
        "Katra TELPU MAPE zemāk = viena telpa: mapes nosaukumā ir platība, stāvs, adrese un piezīme "
        "(piem. «VAR DALĪT», «REMONTS», «ĒDINĀŠANAI» → partner_notes); mapē ir šīs telpas foto un plāns. "
        "Cenu failā ēkas rakstītas ar kodiem (piem. S54A = Skanstes 54A, B39 = Brīvības 39, D3 = Duntes 3) — "
        "atšifrē pēc mapju adresēm. Sasaisti katru telpu ar tās mapi laukā link_ids (platība + stāvs + adrese).",
        "",
        "TELPU MAPES (bildes):",
    ]
    if src.get("notes"):
        lines.insert(3, f"Piezīmes par partneri: {src['notes']}")
    for f in units:
        n = files_by_folder.get(f["path"], 0)
        lines.append(f"- {f['path'].replace('/', ' / ')} ({n} faili): https://drive.google.com/drive/folders/{f['id']}")
    other = [e for e in entries if e.get("kind") != "folder" and "/" not in (e.get("path") or "")]
    if other:
        lines.append("")
        lines.append("Faili mapes saknē: " + ", ".join(e["path"] for e in other[:30]))
    return "\n".join(lines)


def scan_source(src: dict, force: bool = False) -> dict:
    """Viens avots: mapes saraksts + cenu faili → nospiedums → ja mainījies (vai force) → ieplūde."""
    import link_media
    import partner_intake
    scanned_at = _now()
    entries = link_media.list_gdrive_folder(src["folder_id"], depth=MAX_FOLDER_DEPTH)
    if not entries:
        raise RuntimeError("Drive mape tukša vai nav pieejama (vai ir publiska?)")
    price_blobs: dict[str, bytes] = {}
    for fid in src.get("price_file_ids") or []:
        try:
            data = link_media.download_gdrive_file(fid)
        except Exception as e:
            logger.warning("partner_drive %s: cenu fails %s: %s", src["name"], fid, e)
            data = None
        if data:
            price_blobs[fid] = data
    fp = _fingerprint(entries, price_blobs)
    result: dict[str, Any] = {"source_id": src["id"], "scanned_at": scanned_at.isoformat(),
                              "folders": sum(1 for e in entries if e.get("kind") == "folder"),
                              "files": sum(1 for e in entries if e.get("kind") != "folder"),
                              "unit_folders": len(_unit_folders(entries)), "price_files": len(price_blobs),
                              "changed": fp != src.get("last_fingerprint"), "fingerprint": fp, "intake_id": None, "skipped": None}
    if not result["changed"] and not force:
        return result
    # iepriekšējais vēl gaida apstiprināšanu → nekrājam
    prev = src.get("last_intake_id")
    if prev and not force:
        mp = partner_intake.INTAKE_DIR / prev / "meta.json"
        if mp.is_file():
            try:
                st = json.loads(mp.read_text(encoding="utf-8")).get("status")
            except Exception:
                st = None
            if st in ("new", "ready"):
                result["skipped"] = f"iepriekšējais piedāvājums {prev} vēl nav apstiprināts"
                return result
    atts = []
    for fid, data in price_blobs.items():
        name = f"cenas_{fid[:8]}.pdf" if data[:5] == b"%PDF-" else f"cenas_{fid[:8]}.bin"
        if data[:4] == b"PK\x03\x04":
            name = f"cenas_{fid[:8]}.xlsx"
        atts.append({"name": name, "type": "application/pdf" if name.endswith(".pdf") else "application/octet-stream", "data": data})
    parsed = {
        "message_id": f"drive:{src['id']}:{fp[:12]}",
        "forwarder_name": src.get("name"), "forwarder_email": src.get("agent_email") or "raimonds@rgcommerce.lv",
        "subject": f"{src['name']} — Drive skenējums {scanned_at.strftime('%Y-%m-%d')}",
        "body": _body_text(src, entries, scanned_at),
        "orig_sender": {"name": src.get("contact_name") or src.get("name"), "email": src.get("contact_email") or ""},
        "attachments_raw": atts, "date": scanned_at.isoformat(),
    }
    meta = partner_intake._store_intake(parsed, "drive")
    meta["drive_source_id"] = src["id"]
    meta["drive_fingerprint"] = fp
    (partner_intake.INTAKE_DIR / meta["intake_id"] / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    result["intake_id"] = meta["intake_id"]
    rep = partner_intake.process_intake(meta["intake_id"], do_notify=True)
    result["summary"] = rep.get("summary")
    return result


def _apply_result(src: dict, result: dict, error: Optional[str] = None) -> None:
    sources = load_sources()
    for s in sources:
        if s.get("id") == src["id"]:
            s["last_scan_at"] = result.get("scanned_at") or _now().isoformat()
            s["last_result"] = {k: result.get(k) for k in ("changed", "intake_id", "skipped", "unit_folders", "price_files", "summary")}
            s["last_error"] = error
            if result.get("intake_id") or (not result.get("changed") and not error):
                # nospiedumu atjauno tikai, ja ieplūde izveidota vai nekas nav mainījies; «skipped» → rīt vēlreiz
                s["last_fingerprint"] = result.get("fingerprint") or s.get("last_fingerprint")
            if result.get("intake_id"):
                s["last_intake_id"] = result["intake_id"]
    save_sources(sources)


def run_scan(sid: str, force: bool = False) -> dict:
    src = next((s for s in load_sources() if s.get("id") == sid), None)
    if src is None:
        raise ValueError("Nav tāda avota")
    try:
        res = scan_source(src, force=force)
    except Exception as e:
        _apply_result(src, {"scanned_at": _now().isoformat()}, error=f"{type(e).__name__}: {str(e)[:200]}")
        raise
    _apply_result(src, res)
    return res


def maybe_scan_all() -> list[dict]:
    """Sauc partner_intake.run_loop katrā ciklā: skenē avotus, kam pienācis laiks (daily/weekly)."""
    out = []
    for src in load_sources():
        if not src.get("enabled", True):
            continue
        last = src.get("last_scan_at")
        due = True
        if last:
            try:
                due = _now() - datetime.fromisoformat(last) >= SCHEDULES.get(src.get("schedule") or "daily", SCHEDULES["daily"])
            except Exception:
                due = True
        if not due:
            continue
        try:
            res = run_scan(src["id"])
            out.append(res)
            logger.info("partner_drive %s: %s", src["name"], {k: res.get(k) for k in ("changed", "intake_id", "skipped")})
        except Exception as e:
            logger.error("partner_drive %s: %s", src.get("name"), e)
    return out
