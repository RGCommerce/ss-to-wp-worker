"""Bilžu lejupielāde no ss.lv un WordPress (rgcommerce.lv) uz mūsu storage.

Saskaņots 2026-05-03 — fundamentāla arhitektūras maiņa (sk. memory
`project_storage_architecture.md`).
Paplašināts 2026-05-12 ar WP source atbalstu (Kārtas 2b pabeigšana).

- Listings JPG bildes lejupielādējamas mūsu storage uzreiz pēc inbox→listings
- Raw, ar ūdenszīmi (watermark removal Kārtā 2d, tikai publicējamiem)
- Storage mount path: /storage (Railway volume `listing-images`)
- Struktūra:
    /storage/listings/<id>/raw/        ← ss.lv bildes (ar ūdenszīmi)
    /storage/listings/<id>/wp_raw/     ← WP bildes (Ievas augšupielādētas)
    /storage/listings/<id>/processed/  ← AI made (Kārta 2d, 3. agents)
- Katrā mapē _meta.json ar URL, hash, downloaded_at
- LINKed listings (gan ss.lv, gan WP) dabū ABAS mapes — nepārrakstas

Lietošana:
    # ss.lv plūsma (default — backward compat ar esošo Railway worker)
    python crm/download_images.py --listing 1590
    python crm/download_images.py --limit 10
    python crm/download_images.py --all
    python crm/download_images.py --watch

    # WP plūsma (--source wp)
    python crm/download_images.py --source wp --listing 61299
    python crm/download_images.py --source wp --all

    # Abas plūsmas vienā ciklā (--source both)
    python crm/download_images.py --source both --watch

Vide:
    DATABASE_URL          (no crm/.env)
    STORAGE_ROOT          (default: /storage; lokāli pārraksta uz ./storage)
    REQUEST_DELAY_MS      (default: 500ms — polite rate-limit ss.lv pusē)
    REQUEST_TIMEOUT       (default: 30 sek)
    MAX_RETRIES           (default: 3)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import requests
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DATABASE_URL = os.getenv("DATABASE_URL")
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "/storage"))
REQUEST_DELAY_MS = int(os.getenv("REQUEST_DELAY_MS", "500"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
WATCH_POLL_SECONDS = int(os.getenv("WATCH_POLL_SECONDS", "60"))
USER_AGENT = "Mozilla/5.0 (compatible; RGC-archiver/1.0)"

# JPG bildes formāts: "1. https://... | 2. https://... | 3. https://..."
_URL_RE = re.compile(r"https?://\S+?\.(?:jpg|jpeg|png|webp)", re.IGNORECASE)


def parse_image_urls(jpg_field: str | None) -> list[str]:
    """Iztverēt tikai URL no listings."JPG bildes" lauka."""
    if not jpg_field:
        return []
    return _URL_RE.findall(jpg_field)


def listing_dir(listing_id: int) -> Path:
    return STORAGE_ROOT / "listings" / str(listing_id) / "raw"


def listing_wp_dir(listing_id: int) -> Path:
    return STORAGE_ROOT / "listings" / str(listing_id) / "wp_raw"


def relative_path(listing_id: int, filename: str) -> str:
    """OS-neatkarīgs path uz DB. Kods pievieno STORAGE_ROOT pirms lietošanas."""
    return f"listings/{listing_id}/raw/{filename}"


def relative_wp_path(listing_id: int, filename: str) -> str:
    return f"listings/{listing_id}/wp_raw/{filename}"


def parse_wp_image_urls(wp_field: list[str] | None) -> list[str]:
    """wp_image_urls ir TEXT[] (Postgres array), nevis string. Helper drošības labad
    filtrē tukšus / None ierakstus un saglabā secību."""
    if not wp_field:
        return []
    return [u.strip() for u in wp_field if u and u.strip()]


def download_one(url: str, timeout: int = REQUEST_TIMEOUT) -> bytes | None:
    """Lejupielādē vienu bildi ar retry on transient errors. None ja 404 / gone."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            if resp.status_code == 404 or resp.status_code == 410:
                return None  # gone, neretry
            resp.raise_for_status()
            return resp.content
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == MAX_RETRIES:
                print(f"      ✗ failed after {MAX_RETRIES} retries: {e}")
                return None
            time.sleep(2 ** attempt)  # exp backoff: 2s, 4s, 8s
        except requests.HTTPError as e:
            if attempt == MAX_RETRIES:
                print(f"      ✗ HTTP error: {e}")
                return None
            time.sleep(2 ** attempt)
    return None


def process_listing(conn, listing_id: int, jpg_field: str) -> dict[str, Any]:
    """Lejupielādē visas bildes vienai listings rindai.

    Atgriež dict ar saved_paths (relative paths), missing_count, source_urls.
    """
    urls = parse_image_urls(jpg_field)
    if not urls:
        return {"saved_paths": [], "missing": 0, "source_urls": []}

    out_dir = listing_dir(listing_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[str] = []
    file_hashes: list[str] = []
    missing: list[str] = []

    for idx, url in enumerate(urls, start=1):
        filename = f"img_{idx:03d}.jpg"
        filepath = out_dir / filename
        rel = relative_path(listing_id, filename)
        if filepath.exists():
            # Jau ir — skip (idempotent)
            saved_paths.append(rel)
            file_hashes.append(hashlib.sha256(filepath.read_bytes()).hexdigest())
            continue

        time.sleep(REQUEST_DELAY_MS / 1000.0)
        data = download_one(url)
        if data is None:
            missing.append(url)
            continue
        filepath.write_bytes(data)
        saved_paths.append(rel)
        file_hashes.append(hashlib.sha256(data).hexdigest())

    # Meta.json
    meta = {
        "listing_id": listing_id,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "source_urls": urls,
        "file_hashes_sha256": file_hashes,
        "missing": missing,
    }
    (out_dir / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return {
        "saved_paths": saved_paths,
        "missing": len(missing),
        "source_urls": urls,
    }


def process_listing_wp(conn, listing_id: int, urls: list[str]) -> dict[str, Any]:
    """Lejupielādē WP bildes vienai listings rindai uz wp_raw/ mapi.

    Identiska struktūra kā process_listing, bet izejas mape ir wp_raw/ un
    URL avots ir wp_image_urls (TEXT[]), ne "JPG bildes" string.
    """
    if not urls:
        return {"saved_paths": [], "missing": 0, "source_urls": []}

    out_dir = listing_wp_dir(listing_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[str] = []
    file_hashes: list[str] = []
    missing: list[str] = []

    for idx, url in enumerate(urls, start=1):
        filename = f"img_{idx:03d}.jpg"
        filepath = out_dir / filename
        rel = relative_wp_path(listing_id, filename)
        if filepath.exists():
            saved_paths.append(rel)
            file_hashes.append(hashlib.sha256(filepath.read_bytes()).hexdigest())
            continue

        time.sleep(REQUEST_DELAY_MS / 1000.0)
        data = download_one(url)
        if data is None:
            missing.append(url)
            continue
        filepath.write_bytes(data)
        saved_paths.append(rel)
        file_hashes.append(hashlib.sha256(data).hexdigest())

    meta = {
        "listing_id": listing_id,
        "source": "wp",
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "source_urls": urls,
        "file_hashes_sha256": file_hashes,
        "missing": missing,
    }
    (out_dir / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return {
        "saved_paths": saved_paths,
        "missing": len(missing),
        "source_urls": urls,
    }


def update_listing_paths(conn, listing_id: int, saved_paths: list[str]) -> None:
    """UPDATE listings ar ss.lv local paths un timestamp."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE properties.listings
            SET local_image_paths_raw = %s,
                images_downloaded_at = now()
            WHERE id = %s
            """,
            (saved_paths if saved_paths else None, listing_id),
        )
    conn.commit()


def update_listing_wp_paths(conn, listing_id: int, saved_paths: list[str]) -> None:
    """UPDATE listings ar WP local paths un timestamp."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE properties.listings
            SET local_image_paths_wp_raw = %s,
                wp_images_downloaded_at = now()
            WHERE id = %s
            """,
            (saved_paths if saved_paths else None, listing_id),
        )
    conn.commit()


def fetch_pending_listings(conn, limit: int | None = None) -> list[dict]:
    """ss.lv listings, kurām vēl nav lejupielādētas bildes.

    Filtri:
      - images_downloaded_at IS NULL
      - "JPG bildes" IS NOT NULL un nav tukšs
    """
    sql = """
        SELECT id, "JPG bildes" AS jpg_field
        FROM properties.listings
        WHERE images_downloaded_at IS NULL
          AND "JPG bildes" IS NOT NULL
          AND btrim("JPG bildes") <> ''
        ORDER BY id
    """
    params: tuple = ()
    if limit:
        sql += " LIMIT %s"
        params = (limit,)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def fetch_pending_wp_listings(conn, limit: int | None = None) -> list[dict]:
    """WP listings, kurām vēl nav lejupielādētas WP bildes.

    Filtri:
      - wp_images_downloaded_at IS NULL
      - wp_image_urls IS NOT NULL un nav tukšs array
    """
    sql = """
        SELECT id, wp_image_urls
        FROM properties.listings
        WHERE wp_images_downloaded_at IS NULL
          AND wp_image_urls IS NOT NULL
          AND cardinality(wp_image_urls) > 0
        ORDER BY id
    """
    params: tuple = ()
    if limit:
        sql += " LIMIT %s"
        params = (limit,)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def fetch_one_listing(conn, listing_id: int) -> dict | None:
    """Fetch ss.lv source: 'JPG bildes' lauks."""
    with conn.cursor() as cur:
        cur.execute(
            'SELECT id, "JPG bildes" AS jpg_field FROM properties.listings WHERE id = %s',
            (listing_id,),
        )
        r = cur.fetchone()
        return dict(r) if r else None


def fetch_one_wp_listing(conn, listing_id: int) -> dict | None:
    """Fetch WP source: wp_image_urls array."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, wp_image_urls FROM properties.listings WHERE id = %s",
            (listing_id,),
        )
        r = cur.fetchone()
        return dict(r) if r else None


def run_for_listings(conn, listings: list[dict]) -> dict:
    """ss.lv source: lasa "JPG bildes" lauku, raksta uz /raw/."""
    total = len(listings)
    succeeded = 0
    no_images = 0
    img_total = 0
    img_missing_total = 0
    failed_listings = 0

    for i, row in enumerate(listings, start=1):
        listing_id = row["id"]
        jpg_field = row["jpg_field"]
        urls = parse_image_urls(jpg_field)
        if not urls:
            no_images += 1
            # Atzīmējam kā "downloaded" (nav ko lejupielādēt), lai neapstrādājam atkārtoti
            update_listing_paths(conn, listing_id, [])
            continue

        try:
            print(f"[{i}/{total}] [sslv] listing_id={listing_id} | {len(urls)} URLs")
            result = process_listing(conn, listing_id, jpg_field)
            update_listing_paths(conn, listing_id, result["saved_paths"])
            succeeded += 1
            img_total += len(result["saved_paths"])
            img_missing_total += result["missing"]
            print(
                f"        saved={len(result['saved_paths'])} | "
                f"missing={result['missing']}"
            )
        except Exception as e:
            failed_listings += 1
            print(f"        ✗ ERROR: {str(e)[:200]}")

    return {
        "listings_total": total,
        "listings_succeeded": succeeded,
        "listings_no_images": no_images,
        "listings_failed": failed_listings,
        "images_saved_total": img_total,
        "images_missing_total": img_missing_total,
    }


def run_for_wp_listings(conn, listings: list[dict]) -> dict:
    """WP source: lasa wp_image_urls (TEXT[]) lauku, raksta uz /wp_raw/."""
    total = len(listings)
    succeeded = 0
    no_images = 0
    img_total = 0
    img_missing_total = 0
    failed_listings = 0

    for i, row in enumerate(listings, start=1):
        listing_id = row["id"]
        urls = parse_wp_image_urls(row.get("wp_image_urls"))
        if not urls:
            no_images += 1
            update_listing_wp_paths(conn, listing_id, [])
            continue

        try:
            print(f"[{i}/{total}] [wp] listing_id={listing_id} | {len(urls)} URLs")
            result = process_listing_wp(conn, listing_id, urls)
            update_listing_wp_paths(conn, listing_id, result["saved_paths"])
            succeeded += 1
            img_total += len(result["saved_paths"])
            img_missing_total += result["missing"]
            print(
                f"        saved={len(result['saved_paths'])} | "
                f"missing={result['missing']}"
            )
        except Exception as e:
            failed_listings += 1
            print(f"        ✗ ERROR: {str(e)[:200]}")

    return {
        "listings_total": total,
        "listings_succeeded": succeeded,
        "listings_no_images": no_images,
        "listings_failed": failed_listings,
        "images_saved_total": img_total,
        "images_missing_total": img_missing_total,
    }


def run_one_source(conn, source: str, args) -> dict:
    """Palaiž vienu source (sslv vai wp) atkarībā no args.listing/limit/all."""
    if source == "sslv":
        fetch_one_fn = fetch_one_listing
        fetch_pending_fn = fetch_pending_listings
        run_fn = run_for_listings
    elif source == "wp":
        fetch_one_fn = fetch_one_wp_listing
        fetch_pending_fn = fetch_pending_wp_listings
        run_fn = run_for_wp_listings
    else:
        raise ValueError(f"Unknown source: {source}")

    if args.listing:
        row = fetch_one_fn(conn, args.listing)
        if not row:
            print(f"[{source}] listing_id={args.listing} netika atrasts")
            return {}
        listings = [row]
    elif args.limit:
        listings = fetch_pending_fn(conn, limit=args.limit)
    else:  # --all
        listings = fetch_pending_fn(conn)

    print(f"[{source}] Apstrādāsim {len(listings)} listings")
    return run_fn(conn, listings)


def main():
    if not DATABASE_URL:
        print("DATABASE_URL nav atrasts crm/.env failā")
        sys.exit(1)

    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--listing", type=int, help="Single listing_id")
    g.add_argument("--all", action="store_true", help="Visi listings ar pending bildēm")
    g.add_argument("--limit", type=int, help="Pirmie N listings ar pending bildēm")
    g.add_argument("--watch", action="store_true",
                   help="Loop mode (Railway worker) — poll un download")
    ap.add_argument("--source", choices=["sslv", "wp", "both"], default="sslv",
                    help="Bilžu avots: 'sslv' (default, JPG bildes), 'wp' "
                         "(wp_image_urls), 'both' (abas secīgi)")
    ap.add_argument("--storage-root", help="Override STORAGE_ROOT (lokālajai testēšanai)")
    args = ap.parse_args()

    if args.storage_root:
        global STORAGE_ROOT
        STORAGE_ROOT = Path(args.storage_root)

    sources = ["sslv", "wp"] if args.source == "both" else [args.source]

    print(f"STORAGE_ROOT: {STORAGE_ROOT}")
    print(f"SOURCE: {args.source}")
    print(f"DELAY: {REQUEST_DELAY_MS}ms | TIMEOUT: {REQUEST_TIMEOUT}s | "
          f"MAX_RETRIES: {MAX_RETRIES}")

    if args.watch:
        print(f"WATCH mode | poll every {WATCH_POLL_SECONDS}s")
        while True:
            try:
                with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                    any_work = False
                    for source in sources:
                        if source == "sslv":
                            listings = fetch_pending_listings(conn, limit=20)
                            if listings:
                                stats = run_for_listings(conn, listings)
                                print(f"Watch cikls [sslv]: {stats}")
                                any_work = True
                        else:  # wp
                            listings = fetch_pending_wp_listings(conn, limit=20)
                            if listings:
                                stats = run_for_wp_listings(conn, listings)
                                print(f"Watch cikls [wp]: {stats}")
                                any_work = True
                    if not any_work:
                        print(f"Nav pending listings ({args.source}), "
                              f"sleep {WATCH_POLL_SECONDS}s...")
            except Exception as e:
                print(f"Watch cikls KĻŪDA: {e}")
            time.sleep(WATCH_POLL_SECONDS)
        return

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        for source in sources:
            stats = run_one_source(conn, source, args)
            if stats:
                print(f"\n=== [{source}] KOPĀ ===")
                for k, v in stats.items():
                    print(f"  {k:<25}: {v}")


if __name__ == "__main__":
    main()
