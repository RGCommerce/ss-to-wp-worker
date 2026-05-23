"""queue_poller.py — wp_export_queue fona apstrādātājs (ss-to-wp-worker iekšā).

Lasa `properties.wp_export_queue` pa vienam ierakstam un palaiž
`publish_to_wp.publish()`. Statusa pārejas:

    pending → processing → done | error

Saskaņots ar Broker Panel UI (publish lapa) un LAUNCH_PLAN.md kontraktu:

  1. SELECT pending row (race-safe ar FOR UPDATE SKIP LOCKED)
  2. UPDATE status='processing', started_at=now()
  3. Run publish_to_wp.publish() (sync, threadpool)
  4. Veiksmē:   status='done', finished_at=now(), wp_post_id=X
              + UPDATE listings SET on_website=true
  5. Kļūdā:    status='error', error=msg, attempts=attempts+1

Poll interval: 10s, kad rinda tukša. Kad atrasts darbs — uzreiz nākamais
poll bez pauzes (cikls nepārtraucas, kamēr ir pending).

Konfigurējams ar env:
  POLLER_ENABLED   (default "1") — "0" izslēdz pollera startup
  POLLER_INTERVAL  (default "10") — sekundes starp tukšiem polliem
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Optional

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent))
import publish_to_wp  # noqa: E402

logger = logging.getLogger("queue_poller")

DATABASE_URL = os.getenv("DATABASE_URL")
POLLER_ENABLED = os.getenv("POLLER_ENABLED", "1") != "0"
POLLER_INTERVAL = float(os.getenv("POLLER_INTERVAL", "10"))
# Cik laikā 'processing' ieraksts tiek uzskatīts par stale (worker mira/restart-ēja)
STALE_PROCESSING_MIN = int(os.getenv("STALE_PROCESSING_MIN", "30"))


# ---------- DB helpers ----------

def _recover_stale() -> int:
    """Pie startup — atjauno 'processing' ierakstus, kas iesprūduši pēc worker
    restarta. Railway auto-redeploy nogalina vidū esošo publish_to_wp.publish(),
    bet DB ieraksts paliek 'processing' un nekad netiek paņemts atpakaļ.

    Atgriež atjaunoto rindu skaitu."""
    if not DATABASE_URL:
        return 0
    with psycopg.connect(DATABASE_URL) as conn:
        r = conn.execute(f"""
            UPDATE properties.wp_export_queue
            SET status = 'pending', started_at = NULL
            WHERE status = 'processing'
              AND started_at < now() - INTERVAL '{STALE_PROCESSING_MIN} minutes'
        """)
        return r.rowcount


def _claim_next() -> Optional[dict]:
    """Atomāri paņem nākamo pending rindu un atzīmē processing.

    Atgriež claim-oto rindu vai None, ja nav pending. FOR UPDATE SKIP LOCKED
    nodrošina, ka pat ar vairākiem pollers vienlaicīgi neviens neatkārtos."""
    if not DATABASE_URL:
        return None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.transaction():
            cur = conn.execute("""
                SELECT id, listing_id, attempts
                FROM properties.wp_export_queue
                WHERE status = 'pending'
                ORDER BY priority DESC, requested_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """)
            row = cur.fetchone()
            if not row:
                return None
            conn.execute("""
                UPDATE properties.wp_export_queue
                SET status = 'processing', started_at = now()
                WHERE id = %s
            """, (row["id"],))
            return row


def _mark_done(queue_id: int, listing_id: int) -> None:
    """Veiksmīgi pabeigts: status='done', listing on_website=true."""
    if not DATABASE_URL:
        return
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.transaction():
            # publish_to_wp.publish() jau atjaunoja listings.wp_post_id
            # un wp_synced_at — paņemam to reference DB rindas saturam
            wp_id = conn.execute(
                "SELECT wp_post_id FROM properties.listings WHERE id = %s",
                (listing_id,),
            ).fetchone()
            wp_post_id = wp_id["wp_post_id"] if wp_id else None

            conn.execute("""
                UPDATE properties.wp_export_queue
                SET status = 'done', finished_at = now(), wp_post_id = %s
                WHERE id = %s
            """, (wp_post_id, queue_id))

            # Atzīmēt listings.on_website = true (publish_to_wp.py to neatjauno)
            conn.execute("""
                UPDATE properties.listings
                SET on_website = true
                WHERE id = %s
            """, (listing_id,))


def _mark_error(queue_id: int, attempts: int, error_msg: str) -> None:
    if not DATABASE_URL:
        return
    truncated = (error_msg or "")[:2000]
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("""
            UPDATE properties.wp_export_queue
            SET status = 'error',
                finished_at = now(),
                error = %s,
                attempts = %s
            WHERE id = %s
        """, (truncated, attempts + 1, queue_id))


# ---------- Process one ----------

def _process(queue_row: dict) -> tuple[bool, str]:
    """Palaiž publish_to_wp.publish() vienam listingam. Atgriež (ok, log)."""
    listing_id = int(queue_row["listing_id"])
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            publish_to_wp.publish(listing_id)
        return True, buf.getvalue()
    except SystemExit as e:
        return False, f"SystemExit: {e}\n{buf.getvalue()}"
    except Exception as e:
        traceback.print_exc(file=buf)
        return False, f"{type(e).__name__}: {e}\n{buf.getvalue()}"


# ---------- Async loop ----------

_state = {
    "running": False,
    "last_started": None,
    "last_finished": None,
    "last_result": None,
    "processed_total": 0,
    "errors_total": 0,
}


def get_status() -> dict:
    """Status snapshot priekš /health vai /poller/status endpointa."""
    return {**_state, "enabled": POLLER_ENABLED, "interval": POLLER_INTERVAL}


async def run_loop(stop_event: asyncio.Event) -> None:
    """Galvenais cikls. Iet, kamēr stop_event nav uzlikts.

    Strādājot vienā FastAPI workerī. Ja Railway uzliek vairākus workers,
    SKIP LOCKED nodrošina, ka konfliktu nav (testēts ar pdf un publish
    paralēli)."""
    if not POLLER_ENABLED:
        logger.info("Queue poller IZSLĒGTS (POLLER_ENABLED=0)")
        return
    if not DATABASE_URL:
        logger.warning("Queue poller bez DATABASE_URL — sleeping forever")
        await stop_event.wait()
        return

    logger.info(
        f"Queue poller sākts — interval={POLLER_INTERVAL}s, "
        f"stale_threshold={STALE_PROCESSING_MIN}min"
    )
    _state["running"] = True
    loop = asyncio.get_event_loop()

    # Recovery — ja kāds ieraksts iesprūdis 'processing' (worker mira)
    try:
        recovered = await loop.run_in_executor(None, _recover_stale)
        if recovered:
            logger.warning(
                f"Recovery: atjaunoti {recovered} 'processing' ieraksti uz "
                f"'pending' (vecāki par {STALE_PROCESSING_MIN} min)"
            )
    except Exception as e:
        logger.error(f"_recover_stale neizdevās: {e}", exc_info=True)

    try:
        while not stop_event.is_set():
            try:
                row = await loop.run_in_executor(None, _claim_next)
            except Exception as e:
                logger.error(f"_claim_next kļūda: {e}", exc_info=True)
                await _sleep_interruptible(stop_event, POLLER_INTERVAL)
                continue

            if row is None:
                # Rinda tukša — pauze
                await _sleep_interruptible(stop_event, POLLER_INTERVAL)
                continue

            qid = int(row["id"])
            lid = int(row["listing_id"])
            attempts = int(row.get("attempts") or 0)
            logger.info(f"Picked queue#{qid} listing#{lid} (attempts={attempts})")
            _state["last_started"] = {"queue_id": qid, "listing_id": lid}

            ok, log_text = await loop.run_in_executor(None, _process, row)

            try:
                if ok:
                    await loop.run_in_executor(None, _mark_done, qid, lid)
                    _state["processed_total"] += 1
                    _state["last_result"] = {
                        "queue_id": qid, "listing_id": lid, "status": "done"
                    }
                    logger.info(f"Queue#{qid} listing#{lid} → done")
                else:
                    await loop.run_in_executor(
                        None, _mark_error, qid, attempts, log_text
                    )
                    _state["errors_total"] += 1
                    _state["last_result"] = {
                        "queue_id": qid, "listing_id": lid, "status": "error"
                    }
                    logger.warning(
                        f"Queue#{qid} listing#{lid} → error: "
                        f"{log_text[:200]}"
                    )
            except Exception as e:
                logger.error(f"Status atjaunošana neizdevās: {e}",
                             exc_info=True)

            _state["last_finished"] = _state["last_started"]
            # Nemākslīgi neaizkavējam — uzreiz nākamo pollu (varbūt ir vēl
            # darbs); bet ievietojam mini sleep, lai nestress DB
            await asyncio.sleep(0.5)
    finally:
        _state["running"] = False
        logger.info("Queue poller apstājies")


async def _sleep_interruptible(stop_event: asyncio.Event, seconds: float):
    """Asinhrons sleeps, kas pārtraucās, ja shutdown signāls."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
