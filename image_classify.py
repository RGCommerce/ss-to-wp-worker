"""Bilžu klasifikators — fasade / plans / interjers / cits + quality flag.

Plakstera fix (task #15) priekš tā, lai publish_to_wp un PDF maker zinātu
kura bilde ir kura un varētu izvietot pareizi (fasāde pirmā, plāns uz
Houzez plāna sekciju).

- Modelis: OpenAI **gpt-4o-mini** vision (`detail=low`, ~$0.0001/bilde —
  praktiski $0). Tiešs OpenAI API ar to pašu `OPENAI_API_KEY`, kas jau ir
  `.env`-ā.
- Manifests: `storage/listings/<id>/_image_manifest.json` blakus raw/ai_ready.
  Filename → `{"type": "fasade|plans|interjers|cits", "quality":
  "good_for_website|not_good_for_website", "reason": "..."}`.
- **Kešots** — atkārtoti klasificē TIKAI jaunās raw bildes, kas vēl nav
  manifestā. `--force` lai pārklasificētu visu.
- BEZ DB migrācijas (Raimonda izvēle 2026-05-20).

Galvenā lietošana = library:
    from image_classify import ensure_classified
    manifest = ensure_classified(STORAGE_ROOT, listing_id)
    # manifest = {"img_001.jpg": {"type": "fasade", ...}, ...}

CLI (standalone):
    python crm/image_classify.py --listing 13
    python crm/image_classify.py --listing 13 --force
    python crm/image_classify.py --listing 13 --images img_002.jpg
    python crm/image_classify.py --listing 13 --dry-run
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv(Path(__file__).parent / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "./storage"))

_VERIFY = os.getenv("VERIFY_SSL", os.getenv("WP_VERIFY_SSL", "1")) \
    not in ("0", "false", "False")

MANIFEST_FILENAME = "_image_manifest.json"
VALID_TYPES = ("fasade", "plans", "interjers", "cits")
VALID_QUALITY = ("good_for_website", "not_good_for_website")

CLASSIFY_PROMPT = (
    "You are classifying a real-estate listing photo. Return ONLY a JSON "
    "object with EXACTLY these fields:\n"
    "- \"type\": one of \"fasade\" (exterior of the building, outside view, "
    "façade, street view of the building), \"plans\" (an architectural "
    "floor plan or 2D layout drawing — usually black-and-white technical "
    "diagram showing room outlines), \"interjers\" (an interior view of a "
    "room shot from inside), \"cits\" (anything else — aerial, courtyard, "
    "parking, neighbourhood, sign, abstract texture, etc.).\n"
    "- \"quality\": \"good_for_website\" if the image is sharp enough, "
    "well-composed, clearly shows the space and is suitable to publish on "
    "a real-estate website; \"not_good_for_website\" if it is blurry, too "
    "dark, overexposed, badly cropped, obstructed, low-resolution beyond "
    "use, contains identifiable people, or otherwise poor for publishing.\n"
    "- \"reason\": one short sentence in Latvian explaining the choice.\n"
    "Return ONLY valid JSON, no markdown fences, no extra text."
)


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------

def manifest_path(storage_root: Path, listing_id: int) -> Path:
    return Path(storage_root) / "listings" / str(listing_id) / MANIFEST_FILENAME


def load_manifest(storage_root: Path, listing_id: int) -> dict:
    p = manifest_path(storage_root, listing_id)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # Pieņemam vai nu plakanu {filename: {...}} vai {"images": {...}, ...}
        if isinstance(data, dict) and "images" in data \
                and isinstance(data["images"], dict):
            return data["images"]
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! manifests neder ({p}): {e}")
        return {}


def save_manifest(storage_root: Path, listing_id: int, images: dict) -> None:
    p = manifest_path(storage_root, listing_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "listing_id": listing_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "model": OPENAI_VISION_MODEL,
        "images": images,
    }
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                 encoding="utf-8")


# ---------------------------------------------------------------------------
# OpenAI vision call
# ---------------------------------------------------------------------------

def _classify_one(image_bytes: bytes, filename: str) -> dict | None:
    """gpt-4o-mini vision call. Atgriež dict ar type/quality/reason vai
    None ja kļūda."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:image/jpeg;base64,{b64}"
    body = {
        "model": OPENAI_VISION_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": CLASSIFY_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": data_url, "detail": "low"}},
            ],
        }],
        "max_tokens": 200,
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                         "Content-Type": "application/json"},
                json=body,
                timeout=60,
                verify=_VERIFY,
            )
        except requests.RequestException as e:
            print(f"      ! tīkla kļūda: {str(e)[:160]}")
            if attempt < 3:
                time.sleep(3 * attempt)
                continue
            return None
        if r.status_code == 429:
            print("      … 429, gaidu 15s")
            time.sleep(15)
            continue
        if r.status_code != 200:
            print(f"      ! HTTP {r.status_code}: {r.text[:200]}")
            return None
        try:
            txt = r.json()["choices"][0]["message"]["content"]
            obj = json.loads(txt)
        except Exception as e:
            print(f"      ! atbildes parse: {str(e)[:120]}")
            return None
        t = str(obj.get("type", "")).strip().lower()
        q = str(obj.get("quality", "")).strip().lower()
        if t not in VALID_TYPES:
            t = "cits"
        if q not in VALID_QUALITY:
            q = "good_for_website"  # drošāks default
        return {
            "type": t,
            "quality": q,
            "reason": str(obj.get("reason", "")).strip(),
            "filename": filename,
        }
    return None


# ---------------------------------------------------------------------------
# Galvenais API
# ---------------------------------------------------------------------------

def _raw_files(storage_root: Path, listing_id: int) -> list[Path]:
    raw_dir = Path(storage_root) / "listings" / str(listing_id) / "raw"
    if not raw_dir.is_dir():
        return []
    exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
    return sorted(p for p in raw_dir.glob("img_*.*") if p.suffix.lower() in exts)


def ensure_classified(storage_root: Path, listing_id: int,
                      raw_files: Iterable[Path] | None = None,
                      force: bool = False,
                      filter_names: list[str] | None = None) -> dict:
    """Galvenā library funkcija. Pārliecinās, ka manifestā ir ieraksts par
    KATRU raw bildi (vai par filtrētajām, ja `filter_names` dots).
    Atgriež plakanu `{filename: {type, quality, reason, filename}}` dict.

    Pārklasificē tikai trūkstošās vai (ja `force=True`) visas. Manifestu
    atjauno uz diska."""
    if not OPENAI_API_KEY:
        raise SystemExit("OPENAI_API_KEY trūkst crm/.env failā")

    raws = list(raw_files) if raw_files is not None \
        else _raw_files(storage_root, listing_id)
    if not raws:
        print(f"  nav raw bilžu ({listing_id})")
        return {}

    if filter_names:
        wanted = {n.strip().lower() for n in filter_names if n.strip()}
        raws = [p for p in raws if p.name.lower() in wanted]

    manifest = load_manifest(storage_root, listing_id)
    todo = raws if force else [p for p in raws if p.name not in manifest]

    if not todo:
        print(f"  manifestā jau ir visas {len(raws)} bildes — skip")
        return manifest

    print(f"  klasificē {len(todo)}/{len(raws)} bildes ar "
          f"{OPENAI_VISION_MODEL}...")
    for i, p in enumerate(todo, start=1):
        try:
            data = p.read_bytes()
        except OSError as e:
            print(f"    [{i}/{len(todo)}] {p.name} read ERR: {e}")
            continue
        t0 = time.time()
        res = _classify_one(data, p.name)
        if not res:
            print(f"    [{i}/{len(todo)}] {p.name} FAIL")
            continue
        manifest[p.name] = res
        print(f"    [{i}/{len(todo)}] {p.name} → {res['type']:<9} "
              f"{res['quality']:<22} ({time.time() - t0:.1f}s) "
              f"— {res['reason'][:60]}")

    save_manifest(storage_root, listing_id, manifest)
    print(f"  manifests saglabāts: "
          f"{manifest_path(storage_root, listing_id)}")
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--listing", type=int, required=True, help="listing_id")
    ap.add_argument("--images", help="Komatu atdalīts raw bilžu saraksts")
    ap.add_argument("--force", action="store_true",
                    help="Pārklasificē, pat ja manifestā jau ir")
    ap.add_argument("--dry-run", action="store_true",
                    help="Tikai parāda, kas tiks klasificēts")
    args = ap.parse_args()

    if not OPENAI_API_KEY:
        print("OPENAI_API_KEY trūkst crm/.env failā")
        sys.exit(1)

    print(f"STORAGE_ROOT: {STORAGE_ROOT}")
    print(f"Vision model: {OPENAI_VISION_MODEL}")
    img_filter = ([s.strip() for s in args.images.split(",") if s.strip()]
                  if args.images else None)

    raws = _raw_files(STORAGE_ROOT, args.listing)
    if not raws:
        print(f"Nav raw bilžu listing {args.listing}")
        sys.exit(0)
    if img_filter:
        wanted = {n.strip().lower() for n in img_filter if n.strip()}
        raws = [p for p in raws if p.name.lower() in wanted]
        print(f"Filtrs: {len(raws)} bildes (no {img_filter})")

    if args.dry_run:
        existing = load_manifest(STORAGE_ROOT, args.listing)
        for p in raws:
            cur = existing.get(p.name)
            tag = "[cache]" if cur and not args.force else "[classify]"
            cur_str = f"  → {cur['type']}/{cur['quality']}" if cur else ""
            print(f"  {tag} {p.name}{cur_str}")
        print(f"\nKopā: {len(raws)} bildes, paredz. cena ~"
              f"${len(raws) * 0.001:.3f} (gpt-4o-mini detail=low)")
        return

    print(f"\n>>> listing {args.listing}")
    manifest = ensure_classified(STORAGE_ROOT, args.listing,
                                 raws, force=args.force,
                                 filter_names=img_filter)

    print("\n=== KOPSAVILKUMS ===")
    by_type = {}
    for name, info in manifest.items():
        by_type.setdefault(info["type"], []).append(name)
    for t in VALID_TYPES:
        if t in by_type:
            print(f"  {t:<9}: {len(by_type[t])} bildes — {by_type[t]}")


if __name__ == "__main__":
    main()
