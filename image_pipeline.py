"""Image pipeline — ss.lv bildes -> Seedream 5 Lite -> ai_ready storage.

Tas ir PRIMĀRAIS, ekonomiskais pipeline ($0.04/bilde): noņem ss.com
ūdenszīmi un dod mini-enhance. Strādā lielākajai daļai bilžu.

Sliktajām bildēm (ko AI parse vēlāk atzīmēs "not good for website") —
atsevišķs skripts `image_enhance_openai.py` ar OpenAI gpt-image-1 (dārgāks
~$0.05-0.19, "WOW" pārbūve). Tas pārraksta konkrētās ai_ready bildes
selektīvi.

Plūsma:
  1. SELECT local_image_paths_raw + JPG bildes URL no listings DB
  2. Ja raw bildes nav lokāli — auto-lejupielādē no ss.lv (download_images.py)
  3. Katrai raw bildei:
       a. Upload uz Replicate
       b. Seedream 5 Lite predict (~30-40s)
       c. Lejupielādē uz STORAGE_ROOT/listings/<id>/ai_ready/img_NNN.jpg
  4. Saglabā ai_ready/_meta.json (source URL, model, prompt, cost, processed_at)
  5. UPDATE listings.local_image_paths_processed

Lietošana:
  python crm/image_pipeline.py --listing 222
  python crm/image_pipeline.py --listings 222,330,1500
  python crm/image_pipeline.py --listing 222 --force      # pārstrādā, ja jau apstrādāts
  python crm/image_pipeline.py --listing 222 --dry-run    # parāda, kas notiks

Vide (crm/.env):
  DATABASE_URL              — Railway DB
  REPLICATE_API_TOKEN       — r8_...
  STORAGE_ROOT              — default ./storage lokāli, /storage uz Railway
  SEEDREAM_PROMPT           — opcionāli pārraksta default prompt
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.rows import dict_row

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
import download_images  # noqa: E402

load_dotenv(Path(__file__).parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
REPLICATE_TOKEN = os.getenv("REPLICATE_API_TOKEN")
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "./storage"))

SEEDREAM_VERSION = "eeb2857d94c49a5bcbc9d6c6057416e1d3b1a2735a16e08e4def9bf7ee22ec71"
SEEDREAM_MODEL = "bytedance/seedream-5-lite"
COST_PER_IMAGE_USD = 0.04

DEFAULT_PROMPT = (
    "Recreate this exact real estate photo without the SS.com watermark "
    "in the top-left and bottom-right corners. Preserve every detail "
    "identically: walls, floor, ceiling, windows, doors, fixtures, lighting, "
    "and overall composition. Output a clean, professional listing photo "
    "with slightly sharper details and natural colors. "
    "Brighten the image for a bright, airy, well-lit look like professional "
    "real estate photography — especially if the original is dark or "
    "underexposed: lift the shadows and raise the overall exposure and "
    "brightness. Keep colors natural and realistic, and avoid overexposure "
    "or blown-out highlights."
)
PROMPT = os.getenv("SEEDREAM_PROMPT", DEFAULT_PROMPT)

# Lokāli aiz korporatīva proxy CA verifikācija var salūzt (tāpat kā
# wp_publisher WP_VERIFY_SSL). Produkcijā/Railway = 1 (verify ieslēgts).
_VERIFY = os.getenv("VERIFY_SSL", os.getenv("WP_VERIFY_SSL", "1")) \
    not in ("0", "false", "False")
if not _VERIFY:
    try:
        import warnings as _w
        _w.filterwarnings("ignore")
    except Exception:
        pass


# ---------- Replicate helpers ----------

def replicate_upload(image_bytes: bytes, filename: str) -> str:
    resp = requests.post(
        "https://api.replicate.com/v1/files",
        headers={"Authorization": f"Bearer {REPLICATE_TOKEN}"},
        files={"content": (filename, io.BytesIO(image_bytes), "image/jpeg")},
        timeout=60,
        verify=_VERIFY,
    )
    resp.raise_for_status()
    return resp.json()["urls"]["get"]


def seedream_predict(image_url: str) -> str | None:
    """Palaiž Seedream 5 Lite, atgriež output URL vai None ja kļūda."""
    inputs = {
        "prompt": PROMPT,
        "image_input": [image_url],
        "size": "2K",
        "aspect_ratio": "match_input_image",
        "output_format": "jpeg",
        "max_images": 1,
        "sequential_image_generation": "disabled",
    }
    resp = requests.post(
        "https://api.replicate.com/v1/predictions",
        headers={
            "Authorization": f"Bearer {REPLICATE_TOKEN}",
            "Content-Type": "application/json",
            "Prefer": "wait",
        },
        json={"version": SEEDREAM_VERSION, "input": inputs},
        timeout=180,
        verify=_VERIFY,
    )
    if resp.status_code not in (200, 201):
        print(f"      ! Seedream HTTP {resp.status_code}: {resp.text[:300]}")
        return None
    body = resp.json()
    if body.get("status") == "succeeded":
        out = body["output"]
        return out if isinstance(out, str) else out[0]
    # fallback poll
    pred_id = body["id"]
    for _ in range(120):
        time.sleep(2)
        poll = requests.get(
            f"https://api.replicate.com/v1/predictions/{pred_id}",
            headers={"Authorization": f"Bearer {REPLICATE_TOKEN}"},
            timeout=30,
            verify=_VERIFY,
        ).json()
        if poll["status"] == "succeeded":
            out = poll["output"]
            return out if isinstance(out, str) else out[0]
        if poll["status"] == "failed":
            print(f"      ! Seedream failed: {poll.get('error')}")
            return None
    print("      ! Seedream timeout")
    return None


# ---------- DB helpers ----------

def fetch_listing(conn, listing_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, street, city, "JPG bildes" AS jpg_field,
                   local_image_paths_raw, local_image_paths_processed,
                   images_downloaded_at
            FROM properties.listings
            WHERE id = %s
            """,
            (listing_id,),
        )
        r = cur.fetchone()
        return dict(r) if r else None


def update_processed_paths(conn, listing_id: int, paths: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE properties.listings SET local_image_paths_processed = %s WHERE id = %s",
            (paths if paths else None, listing_id),
        )
    conn.commit()


# ---------- Storage helpers ----------

def ai_dir(listing_id: int) -> Path:
    return STORAGE_ROOT / "listings" / str(listing_id) / "ai_ready"


def raw_dir(listing_id: int) -> Path:
    return STORAGE_ROOT / "listings" / str(listing_id) / "raw"


def relative_ai_path(listing_id: int, filename: str) -> str:
    return f"listings/{listing_id}/ai_ready/{filename}"


def ensure_raw_local(conn, listing: dict) -> list[Path]:
    """Pārliecinās ka raw bildes ir lokāli (STORAGE_ROOT). Ja nav — lejupielādē.

    Atgriež sarakstu ar lokālajiem raw file paths.
    """
    listing_id = listing["id"]
    out_dir = raw_dir(listing_id)

    # Skenē kas jau ir lokāli
    existing = sorted(out_dir.glob("img_*.jpg")) if out_dir.exists() else []

    expected_count = 0
    if listing["jpg_field"]:
        expected_count = len(download_images.parse_image_urls(listing["jpg_field"]))

    if existing and len(existing) >= expected_count:
        print(f"  raw bildes jau lokāli: {len(existing)} gab.")
        return existing

    # Lejupielādē trūkstošās. download_images.process_listing ir idempotents (skip ja eksistē).
    print(f"  lejupielādē raw no ss.lv ({expected_count} URLs)...")
    # download_images STORAGE_ROOT izmanto modulā kā globālu mainīgo — uzstādīt
    download_images.STORAGE_ROOT = STORAGE_ROOT
    result = download_images.process_listing(conn, listing_id, listing["jpg_field"])
    print(f"    saved={len(result['saved_paths'])} missing={result['missing']}")
    # Reģistrē DB-ā arī raw paths
    download_images.update_listing_paths(conn, listing_id, result["saved_paths"])
    return sorted(out_dir.glob("img_*.jpg"))


# ---------- Main pipeline ----------

def process_listing(conn, listing_id: int, force: bool = False, dry_run: bool = False) -> dict:
    """Apstrādā vienu listing — atgriež statistiku."""
    listing = fetch_listing(conn, listing_id)
    if not listing:
        return {"listing_id": listing_id, "status": "not_found"}

    label = f"id={listing_id} | {listing['street']} ({listing['city']})"
    print(f"\n>>> {label}")

    already = listing["local_image_paths_processed"] or []
    if already and not force:
        print(f"  jau apstrādāts ({len(already)} bildes). --force lai pārstrādātu.")
        return {"listing_id": listing_id, "status": "skipped_already_processed",
                "processed_count": len(already)}

    if not listing["jpg_field"]:
        print(f"  nav JPG bildes URLs")
        return {"listing_id": listing_id, "status": "no_images"}

    if dry_run:
        urls = download_images.parse_image_urls(listing["jpg_field"])
        cost = len(urls) * COST_PER_IMAGE_USD
        print(f"  [DRY RUN] {len(urls)} bildes, paredz. cena: ${cost:.2f}")
        return {"listing_id": listing_id, "status": "dry_run",
                "image_count": len(urls), "estimated_cost_usd": cost}

    # 1. Nodrošināt raw bildes lokāli
    raw_files = ensure_raw_local(conn, listing)
    if not raw_files:
        return {"listing_id": listing_id, "status": "no_raw_images"}

    # 2. AI mape
    out_dir = ai_dir(listing_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 3. Process katru
    processed_paths: list[str] = []
    failed: list[str] = []
    grand_start = time.time()

    for i, raw_path in enumerate(raw_files, start=1):
        filename = raw_path.name  # img_001.jpg
        out_path = out_dir / filename

        if out_path.exists() and not force:
            print(f"  [{i}/{len(raw_files)}] {filename} — jau eksistē, skip")
            processed_paths.append(relative_ai_path(listing_id, filename))
            continue

        print(f"  [{i}/{len(raw_files)}] {filename}...")
        t0 = time.time()

        try:
            with open(raw_path, "rb") as f:
                image_bytes = f.read()
            image_url = replicate_upload(image_bytes, filename)
            out_url = seedream_predict(image_url)
            if not out_url:
                failed.append(filename)
                continue
            r = requests.get(out_url, timeout=120, verify=_VERIFY)
            r.raise_for_status()
            out_path.write_bytes(r.content)
            processed_paths.append(relative_ai_path(listing_id, filename))
            print(f"      OK {time.time() - t0:.1f}s | {len(r.content) // 1024} KB")
        except Exception as e:
            print(f"      ! ERROR: {str(e)[:200]}")
            failed.append(filename)

    elapsed = time.time() - grand_start
    cost = len(processed_paths) * COST_PER_IMAGE_USD

    # 4. Meta
    meta = {
        "listing_id": listing_id,
        "model": SEEDREAM_MODEL,
        "model_version": SEEDREAM_VERSION,
        "prompt": PROMPT,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "raw_count": len(raw_files),
        "processed_count": len(processed_paths),
        "failed": failed,
        "elapsed_seconds": round(elapsed, 1),
        "cost_usd": round(cost, 3),
    }
    (out_dir / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # 5. DB
    update_processed_paths(conn, listing_id, processed_paths)

    print(f"  KOPĀ: {len(processed_paths)} apstrādāts, {len(failed)} fail | "
          f"{elapsed:.1f}s | ${cost:.2f}")

    return {
        "listing_id": listing_id,
        "status": "ok",
        "processed_count": len(processed_paths),
        "failed_count": len(failed),
        "elapsed_seconds": round(elapsed, 1),
        "cost_usd": round(cost, 3),
    }


def main():
    if not DATABASE_URL:
        print("DATABASE_URL trūkst crm/.env failā")
        sys.exit(1)
    if not REPLICATE_TOKEN:
        print("REPLICATE_API_TOKEN trūkst crm/.env failā")
        sys.exit(1)

    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--listing", type=int, help="Viens listing_id")
    g.add_argument("--listings", help="Komatu atdalīts listing_id saraksts (piem., 4,17,42)")
    ap.add_argument("--force", action="store_true", help="Pārstrādā, ja jau ir apstrādāts")
    ap.add_argument("--dry-run", action="store_true", help="Parāda kas tiks darīts, neapstrādā")
    ap.add_argument("--storage-root", help="Override STORAGE_ROOT")
    args = ap.parse_args()

    global STORAGE_ROOT
    if args.storage_root:
        STORAGE_ROOT = Path(args.storage_root)

    print(f"STORAGE_ROOT: {STORAGE_ROOT}")
    print(f"Model: {SEEDREAM_MODEL}")
    print(f"Cost per image: ${COST_PER_IMAGE_USD}")
    if args.force:
        print("Mode: FORCE (pārstrādās jau apstrādātus)")
    if args.dry_run:
        print("Mode: DRY RUN")

    listing_ids: list[int] = []
    if args.listing:
        listing_ids = [args.listing]
    elif args.listings:
        listing_ids = [int(x.strip()) for x in args.listings.split(",") if x.strip()]

    results = []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        for lid in listing_ids:
            results.append(process_listing(conn, lid, force=args.force, dry_run=args.dry_run))

    print("\n=== KOPSAVILKUMS ===")
    total_cost = sum(r.get("cost_usd", 0) or 0 for r in results)
    total_imgs = sum(r.get("processed_count", 0) or 0 for r in results)
    for r in results:
        print(f"  {r}")
    print(f"\n  KOPĀ apstrādātas: {total_imgs} bildes | ${total_cost:.2f}")


if __name__ == "__main__":
    main()
