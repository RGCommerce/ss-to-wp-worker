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


def add_photo(bp_id: int, data: bytes, filename: str, ptype: str = "fasade", by: Optional[str] = None,
              source_listing: Optional[int] = None) -> dict:
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
    import hashlib
    sha1 = hashlib.sha1(data).hexdigest()
    items = list_photos(bp_id)
    dup = next((it for it in items if it.get("sha1") == sha1), None)
    if dup:
        return {**dup, "duplicate": True}
    fn = f"bp_{uuid.uuid4().hex}{ext}"
    _dir(bp_id).mkdir(parents=True, exist_ok=True)
    (_dir(bp_id) / fn).write_bytes(data)
    item = {"fn": fn, "type": ptype, "added_at": datetime.now(timezone.utc).isoformat(), "by": by, "sha1": sha1}
    if source_listing:
        item["source_listing"] = int(source_listing)
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
                     fns: Optional[list[str]] = None, position: str = "front") -> dict:
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

    if position == "end":
        # manuāli no listinga redaktora — beigās (kā «No citiem listingiem»), galveno izvēlas redaktorā
        new_raw = raw + new_front_raw + new_mid_raw
        new_proc = (proc + new_front_proc + new_mid_proc) if (proc or not raw) else proc
    else:
        new_raw = _merge(raw, new_front_raw, new_mid_raw)
        new_proc = _merge(proc, new_front_proc, new_mid_proc) if (proc or not raw) else proc
    conn.execute("""UPDATE properties.listings SET local_image_paths_raw=%s::text[],
                        local_image_paths_processed=%s::text[], updated_at=now() WHERE id=%s""",
                 (new_raw, new_proc or None, listing_id))
    conn.commit()
    out["raw_names"] = [Path(x).name for x in new_raw]
    out["proc_names"] = [Path(x).name for x in (new_proc or [])]
    return out


def apply_to_building(conn, bp_id: int, by: Optional[str] = None, classified_only: bool = False) -> dict:
    """Visiem mūsu listingiem ēkā bez fasādes. Mājaslapā esošos pārpublicē (wp_export_queue)."""
    rows = conn.execute("""SELECT id, wp_post_id FROM properties.listings
                           WHERE building_profile_id=%s AND coalesce(occupancy_status,'') <> 'occupied'
                           ORDER BY id""", (bp_id,)).fetchall()
    res = {"bp_id": bp_id, "listings": [], "added_to": 0, "republish": 0}
    import image_classify
    for r in rows:
        if classified_only:
            man = image_classify.load_manifest(STORAGE_ROOT, int(r["id"])) or {}
            if not man:
                # nav klasificēts: drīkst tikai, ja listingam vispār nav bilžu
                rr = conn.execute("""SELECT coalesce(array_length(local_image_paths_raw,1),0)
                                          + coalesce(array_length(local_image_paths_processed,1),0) n
                                     FROM properties.listings WHERE id=%s""", (r["id"],)).fetchone()
                if rr and rr["n"]:
                    res["listings"].append({"listing_id": r["id"], "skipped": "nav klasificēts"})
                    continue
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


# ───────────────────────────── automātiska fasāžu vākšana ─────────────────────────────

HARVEST_MAX = int(os.getenv("BUILDING_HARVEST_MAX", "3"))


def harvest_from_listings(conn, bp_id: int, max_new: int = HARVEST_MAX) -> dict:
    """Ēkas listingu bildes, ko image_classify jau atzinis par «fasade» (BEZ jaunām AI izmaksām),
    → ēkas bilžu bibliotēkā. Ņem TIKAI tīrās (ai_ready = Seedream/aģenta, wp_raw = jau mūsu mājaslapā),
    nekad ss.lv raw (ūdenszīmes). Dublikātus atmet pēc sha1; ēkas pašas bildes (img_user_bp_) izlaiž."""
    import image_classify
    out = {"bp_id": bp_id, "added": 0, "checked_listings": 0, "candidates": 0}
    have = list_photos(bp_id)
    if sum(1 for h in have if h.get("type") == "fasade") >= max_new:
        out["skipped"] = "ēkai jau pietiek fasāžu"
        return out
    rows = conn.execute("""SELECT id, local_image_paths_processed, local_image_paths_wp_raw
                           FROM properties.listings WHERE building_profile_id=%s ORDER BY id DESC""",
                        (bp_id,)).fetchall()
    for r in rows:
        man = image_classify.load_manifest(STORAGE_ROOT, int(r["id"])) or {}
        if not man:
            continue
        out["checked_listings"] += 1
        for col in ("local_image_paths_processed", "local_image_paths_wp_raw"):
            for pth in (r[col] or []):
                name = Path(_rel(pth)).name
                if name.startswith("img_user_bp_") or (man.get(name) or {}).get("type") != "fasade":
                    continue
                src = STORAGE_ROOT / _rel(pth)
                if not src.is_file():
                    continue
                out["candidates"] += 1
                try:
                    it = add_photo(bp_id, src.read_bytes(), name, "fasade", "auto: ēkas listingi", int(r["id"]))
                except ValueError:
                    continue
                if not it.get("duplicate"):
                    out["added"] += 1
                    if sum(1 for h in list_photos(bp_id) if h.get("type") == "fasade") >= max_new:
                        return out
    return out


def harvest_all(conn, dry_run: bool = True, limit: Optional[int] = None) -> dict:
    """Visām ēkām: savāc fasādes no klasificētajām bildēm, tad ieliek listingiem bez fasādes.
    dry_run=True — tikai saskaita (neko nekopē)."""
    import image_classify
    bps = conn.execute("""SELECT DISTINCT building_profile_id AS bp FROM properties.listings
                          WHERE building_profile_id IS NOT NULL ORDER BY 1""").fetchall()
    res = {"dry_run": dry_run, "buildings": 0, "buildings_with_facade": 0, "photos_added": 0,
           "listings_without_facade": 0, "listings_filled": 0, "republish": 0}
    for i, b in enumerate(bps):
        if limit and i >= limit:
            break
        bp_id = int(b["bp"])
        res["buildings"] += 1
        rows = conn.execute("""SELECT id, local_image_paths_processed, local_image_paths_raw, local_image_paths_wp_raw
                               FROM properties.listings WHERE building_profile_id=%s
                                 AND coalesce(occupancy_status,'') <> 'occupied'""", (bp_id,)).fetchall()
        facade_src, missing = 0, 0
        for r in rows:
            man = image_classify.load_manifest(STORAGE_ROOT, int(r["id"])) or {}
            paths = (r["local_image_paths_processed"] or []) + (r["local_image_paths_wp_raw"] or [])
            names = {Path(_rel(x)).name for x in paths}
            if any((man.get(n) or {}).get("type") == "fasade" for n in names):
                facade_src += 1
            elif man or not (r["local_image_paths_raw"] or paths):
                missing += 1   # klasificēts bez fasādes VAI vispār bez bildēm
        if facade_src or list_photos(bp_id):
            res["buildings_with_facade"] += 1
            res["listings_without_facade"] += missing
        if dry_run or not facade_src:
            continue
        # TIKAI savāc ēkas profilā — listingiem NEKO nepievieno (Raimonds 2026-10-23: pievieno cilvēks
        # pats — importa stadijā vai listinga redaktorā «No ēkas bildēm»)
        res["photos_added"] += harvest_from_listings(conn, bp_id).get("added", 0)
    return res
