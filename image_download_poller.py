"""image_download_poller.py — 4. fona plūsma: lejupielādē ss.lv + WP bildes uz volume.

Aizstāj atsevišķo `image-downloader` Railway servisu (kuram NAV volume → rakstīja
ephemeral). Šis palaižas ss-to-wp-worker iekšā, kam PIEDER volume, tāpēc faili
nonāk tur, kur serving (image-proxy) tos lasa.

Plūsma (paralēli queue_poller / pdf_poller / agent_ai_poller):
    properties.listings:
      ss.lv: images_downloaded_at IS NULL AND "JPG bildes" not empty
      WP:    wp_images_downloaded_at IS NULL AND wp_image_urls not empty
    → download_images.run_for_listings / run_for_wp_listings (sinhroni, executor-ā)
    → faili uz /storage/listings/<id>/raw|wp_raw/ + DB local_image_paths_* + timestamp

Jaunie ss.lv listingi lejupielādējas UZREIZ pēc scrape (svaigs URL = pareizs saturs).
Vecos NEpārlejupielādē (images_downloaded_at jau uzlikts) — re-download tikai ja
kāds manuāli reset-o timestamp (piem. ~94 zaudētie 05-25→ recovery).

Env:
    IMG_DL_ENABLED   (default "1")
    IMG_DL_INTERVAL  (default "30")  — sekundes starp cikliem
    IMG_DL_BATCH     (default "20")  — cik listingu vienā ciklā (katram source)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent))
import download_images  # noqa: E402

logger = logging.getLogger("image_download_poller")

DATABASE_URL = os.getenv("DATABASE_URL")
IMG_DL_ENABLED = os.getenv("IMG_DL_ENABLED", "1") != "0"
IMG_DL_INTERVAL = float(os.getenv("IMG_DL_INTERVAL", "30"))
IMG_DL_BATCH = int(os.getenv("IMG_DL_BATCH", "20"))

_state = {
    "running": False,
    "last_cycle_at": None,
    "last_result": None,
    "sslv_images_total": 0,
    "wp_images_total": 0,
    "errors_total": 0,
    "loop_iterations": 0,
}


def get_status() -> dict:
    return {
        **_state,
        "enabled": IMG_DL_ENABLED,
        "interval": IMG_DL_INTERVAL,
        "batch": IMG_DL_BATCH,
    }


def _download_cycle() -> dict:
    """Viens cikls — sinhroni (palaiž executor-ā). Apstrādā līdz BATCH listingiem
    katram source. Atgriež stats (vai tukšu, ja nav pending)."""
    out: dict = {"sslv": None, "wp": None}
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        sslv = download_images.fetch_pending_listings(conn, limit=IMG_DL_BATCH)
        if sslv:
            out["sslv"] = download_images.run_for_listings(conn, sslv)
        wp = download_images.fetch_pending_wp_listings(conn, limit=IMG_DL_BATCH)
        if wp:
            out["wp"] = download_images.run_for_wp_listings(conn, wp)
    return out


async def run_loop(stop_event: asyncio.Event) -> None:
    if not IMG_DL_ENABLED:
        logger.info("Image download poller IZSLĒGTS (IMG_DL_ENABLED=0)")
        return
    if not DATABASE_URL:
        logger.warning("Image download poller bez DATABASE_URL — sleeping forever")
        await stop_event.wait()
        return

    logger.info(
        f"Image download poller sākts — interval={IMG_DL_INTERVAL}s batch={IMG_DL_BATCH}"
    )
    _state["running"] = True
    loop = asyncio.get_event_loop()
    try:
        while not stop_event.is_set():
            _state["loop_iterations"] += 1
            _state["last_cycle_at"] = datetime.now(timezone.utc).isoformat()
            try:
                res = await loop.run_in_executor(None, _download_cycle)
                _state["last_result"] = res
                if res.get("sslv"):
                    _state["sslv_images_total"] += res["sslv"].get("images_saved_total", 0)
                if res.get("wp"):
                    _state["wp_images_total"] += res["wp"].get("images_saved_total", 0)
            except Exception as e:
                _state["errors_total"] += 1
                _state["last_result"] = f"error: {type(e).__name__}: {str(e)[:200]}"
                logger.error(f"Image download cycle kļūda: {e}", exc_info=True)
            await _sleep_interruptible(stop_event, IMG_DL_INTERVAL)
    finally:
        _state["running"] = False
        logger.info("Image download poller apstājies")


async def _sleep_interruptible(stop_event: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
