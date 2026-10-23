"""building_photos.py — ēkas noklusējuma bildes (fasāde / pagalms), Raimonds 2026-10-23.

Partneri (piem. PEBO) bieži nesūta fasādes bildi; ss.lv galerijās arī mēdz būt tikai interjeri.
Ēkai var vienreiz ielikt savas fasādes/pagalma bildes, un tās automātiski pievienojas
listingiem tajā ēkā, KURIEM NAV fasādes (pēc image_classify manifesta).

Glabātuve (worker volume):
    STORAGE_ROOT/buildings/<bp_id>/bp_<hex>.<ext>
    STORAGE_ROOT/buildings/<bp_id>/_photos.json   = [{"fn", "type", "added_at", "by"}] (secība = rādīšanas secība)

Listingam pievienotās bildes ir MŪSU (tīras) → raw/ + ai_ready/ kā `img_user_bp_<fn>` (bez Seedream),
manifestā type = fasade|cits; nosaukums idempotents (atkārtoti nepievieno).
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("building_photos")

STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "/storage"))
TYPES = ("fasade", "pagalms", "cits")
_EXTS = (".jpg", ".jpeg", ".png", ".webp")
MAX_BYTES = 20 * 1024 * 1024
FN_RE = re.compile(r"^bp_[0-9a-f]{32}\.(jpg|jpeg|png|webp)$")


def _dir(bp_id: int) -> Path:
    return STORAGE_ROOT / "buildings" / str(int(bp_id))


def _meta_path(bp_id: int) -> Path:
    return _dir(bp_id) / "_photos.json"


def list_photos(bp_id: int) -> list[dict]:
    p = _meta_path(bp_id)
    if not p.is_file():
        return []
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [it for it in items if (_dir(bp_id) / it.get("fn", "")).is_file()]


def _save(bp_id: int, items: list[dict]) -> None:
    _dir(bp_id).mkdir(parents=True, exist_ok=True)
    tmp = _meta_path(bp_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_meta_path(bp_id))


def file_path(bp_id: int, fn: str) -> Optional[Path]:
    if not FN_RE.match(fn or ""):
        return None
    p = _dir(bp_id) / fn
    return p if p.is_file() else None


def add_photo(bp_id: int, data: bytes, filename: str, ptype: str = "fasade", by: Optional[str] = None) -> dict:
    if ptype not in TYPES:
        raise ValueError(f"Nederīgs tips: {ptype}")
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Fails tukšs vai par lielu (max 20 MB)")
    ext = Path(filename or "").suffix.lower()
    if ext not in _EXTS:
        ext = ".jpg"
    # validē, ka tā tiešām ir bilde (un HEIC/u.c. pārvērš uz JPG)
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.verify()
        im = Image.open(io.BytesIO(data))
        if ext not in _EXTS or im.format not in ("JPEG", "PNG", "WEBP"):
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=90)
            data, ext = buf.getvalue(), ".jpg"
    except Exception as e:
        raise ValueError(f"Nav derīga bilde: {type(e).__name__}")
    fn = f"bp_{uuid.uuid4().hex}{ext}"
    _dir(bp_id).mkdir(parents=True, exist_ok=True)
    (_dir(bp_id) / fn).write_bytes(data)
    items = list_photos(bp_id)
    item = {"fn": fn, "type": ptype, "added_at": datetime.now(timezone.utc).isoformat(), "by": by}
    items.append(item)
    _save(bp_id, items)
    return item


def delete_photo(bp_id: int, fn: str) -> bool:
    p = file_path(bp_id, fn)
    items = [it for it in list_photos(bp_id) if it.get("fn") != fn]
    _save(bp_id, items)
    if p:
        p.unlink(missing_ok=True)
        return True
    return False


def set_type(bp_id: int, fn: str, ptype: str) -> bool:
    if ptype not in TYPES:
        raise ValueError(f"Nederīgs tips: {ptype}")
    items = list_photos(bp_id)
    hit = False
    for it in items:
        if it.get("fn") == fn:
            it["type"] = ptype
            hit = True
    _save(bp_id, items)
    return hit


# ───────────────────────────── pievienošana listingam ─────────────────────────────

def _rel(p: str) -> str:
    s = str(p).replace("\\", "/")
    return s[s.index("listings/"):] if "listings/" in s else s


def listing_has_facade(listing_id: int, paths: list[str]) -> bool:
    """Vai kāda no listinga bildēm manifestā ir «fasade»."""
    import image_classify
    man = image_classify.load_manifest(STORAGE_ROOT, listing_id) or {}
    names = {Path(_rel(p)).name for p in (paths or [])}
    return any((man.get(n) or {}).get("type") == "fasade" for n in names)


def apply_to_listing(conn, listing_id: int, bp_id: int, mode: str = "auto",
                     fns: Optional[list[str]] = None) -> dict:
    """Pievieno ēkas bildes listingam.
    mode: "auto" — tikai ja listingam NAV fasādes; "always" — vienmēr; "never" — neko.
    fns: ja dots — tikai šīs (aģenta izvēle importa stadijā), citādi visas ēkas bildes.
    Fasādes bildes ieliek PIRMĀS (galvenā bilde), pagalmu/citas — aiz tām; plāni paliek beigās."""
    out = {"listing_id": listing_id, "added": 0, "skipped": None}
    if mode == "never":
        out["skipped"] = "izslēgts"
        return out
    photos = list_photos(bp_id)
    if fns is not None:
        want = set(fns)
        photos = [p for p in photos if p["fn"] in want]
    if not photos:
        out["skipped"] = "ēkai nav bilžu"
        return out
    row = conn.execute("""SELECT local_image_paths_raw, local_image_paths_processed, "JPG bildes" AS jpg,
                                 images_downloaded_at
                          FROM properties.listings WHERE id=%s""", (listing_id,)).fetchone()
    if not row:
        out["skipped"] = "listings nav"
        return out
    # ss.lv listings, kura bildes vēl NAV lejupielādētas: raw/ mapē ielikta ēkas bilde padarītu
    # raw par «autoritāti» (image_pipeline.ensure_raw_local) → ss.lv bildes nekad nelejupielādētos.
    if (row["jpg"] or "").strip() and not row["images_downloaded_at"] and not (row["local_image_paths_raw"] or []):
        out["skipped"] = "ss.lv bildes vēl nav lejupielādētas"
        return out
    raw = [_rel(p) for p in (row["local_image_paths_raw"] or [])]
    proc = [_rel(p) for p in (row["local_image_paths_processed"] or [])]
    if mode == "auto" and listing_has_facade(listing_id, proc or raw):
        out["skipped"] = "jau ir fasāde"
        return out
    import image_classify
    ldir = STORAGE_ROOT / "listings" / str(listing_id)
    (ldir / "raw").mkdir(parents=True, exist_ok=True)
    (ldir / "ai_ready").mkdir(parents=True, exist_ok=True)
    man = image_classify.load_manifest(STORAGE_ROOT, listing_id) or {}
    new_front_raw, new_front_proc, new_mid_raw, new_mid_proc = [], [], [], []
    for ph in photos:
        src = _dir(bp_id) / ph["fn"]
        if not src.is_file():
            continue
        fn = f"img_user_bp_{ph['fn'][3:]}"   # img_user_* = tīrs (bez Seedream) visos pipeline
        r_rel = f"listings/{listing_id}/raw/{fn}"
        a_rel = f"listings/{listing_id}/ai_ready/{fn}"
        if r_rel in raw or a_rel in proc:
            continue
        shutil.copy2(src, ldir / "raw" / fn)
        shutil.copy2(src, ldir / "ai_ready" / fn)
        man[fn] = {"type": "fasade" if ph["type"] == "fasade" else "cits", "quality": "good_for_website",
                   "reason": f"ēkas bilde ({ph['type']})", "filename": fn}
        if ph["type"] == "fasade":
            new_front_raw.append(r_rel); new_front_proc.append(a_rel)
        else:
            new_mid_raw.append(r_rel); new_mid_proc.append(a_rel)
        out["added"] += 1
    if not out["added"]:
        out["skipped"] = "jau pievienotas"
        return out
    image_classify.save_manifest(STORAGE_ROOT, listing_id, man)

    def _merge(cur: list[str], front: list[str], mid: list[str]) -> list[str]:
        # fasāde pirmā; pagalms aiz pirmās bildes; plāni (manifestā «plans») paliek beigās
        plans = [p for p in cur if (man.get(Path(p).name) or {}).get("type") == "plans"]
        rest = [p for p in cur if p not in plans]
        return front + rest[:1] + mid + rest[1:] + plans

    new_raw = _merge(raw, new_front_raw, new_mid_raw)
    new_proc = _merge(proc, new_front_proc, new_mid_proc) if (proc or not raw) else proc
    conn.execute("""UPDATE properties.listings SET local_image_paths_raw=%s::text[],
                        local_image_paths_processed=%s::text[], updated_at=now() WHERE id=%s""",
                 (new_raw, new_proc or None, listing_id))
    conn.commit()
    return out


def apply_to_building(conn, bp_id: int, by: Optional[str] = None) -> dict:
    """Visiem mūsu listingiem ēkā bez fasādes. Mājaslapā esošos pārpublicē (wp_export_queue)."""
    rows = conn.execute("""SELECT id, wp_post_id FROM properties.listings
                           WHERE building_profile_id=%s AND coalesce(occupancy_status,'') <> 'occupied'
                           ORDER BY id""", (bp_id,)).fetchall()
    res = {"bp_id": bp_id, "listings": [], "added_to": 0, "republish": 0}
    for r in rows:
        try:
            o = apply_to_listing(conn, int(r["id"]), bp_id, "auto")
        except Exception as e:
            conn.rollback()
            o = {"listing_id": r["id"], "error": f"{type(e).__name__}: {str(e)[:120]}"}
        res["listings"].append(o)
        if o.get("added"):
            res["added_to"] += 1
            if r["wp_post_id"]:
                try:
                    conn.execute("""INSERT INTO properties.wp_export_queue (listing_id, status, requested_by)
                                    VALUES (%s,'pending',%s)
                                    ON CONFLICT (listing_id) WHERE status IN ('pending','processing') DO NOTHING""",
                                 (r["id"], by or "building_photos"))
                    conn.commit()
                    res["republish"] += 1
                except Exception:
                    conn.rollback()
    return res
