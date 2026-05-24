"""pdf_poller.py — properties.pdf_jobs fona apstrādātājs.

Paralēli wp_export queue_poller — abi strādā vienā servisā, vienā Python
procesā. Threadpool izpilda blokējošos zvanus (pdf_maker.render_pdf_bulk,
kas iet caur image_pipeline + WeasyPrint), event loop paliek atbildīgs.

Plūsma:
    pending → processing → done | error

Saglabā galu uz `/storage/pdf_jobs/<job_id>.pdf` un raksta `file_path` DB.
Broker Panel pēc tam GET /pdf-jobs/{id}/file atgriež failu pārlūkam.

Stale recovery — kā queue_poller — pie startup atjauno >30 min "processing"
rindas, kas iesprūdušas (Railway redeploy).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Optional

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent))
import pdf_maker  # noqa: E402

logger = logging.getLogger("pdf_poller")

DATABASE_URL = os.getenv("DATABASE_URL")
PDF_POLLER_ENABLED = os.getenv("PDF_POLLER_ENABLED", "1") != "0"
PDF_POLLER_INTERVAL = float(os.getenv("PDF_POLLER_INTERVAL", "10"))
PDF_STALE_MIN = int(os.getenv("PDF_STALE_MIN", "30"))

# Saglabāšanas mape (Railway volume = /storage). pdf_jobs/ apakšmape.
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "/storage"))
PDF_DIR = STORAGE_ROOT / "pdf_jobs"


# ---------- DB helpers ----------

def _recover_stale() -> int:
    if not DATABASE_URL:
        return 0
    with psycopg.connect(DATABASE_URL) as conn:
        r = conn.execute(f"""
            UPDATE properties.pdf_jobs
            SET status = 'pending', started_at = NULL
            WHERE status = 'processing'
              AND started_at < now() - INTERVAL '{PDF_STALE_MIN} minutes'
        """)
        return r.rowcount


def _claim_next() -> Optional[dict]:
    """Atomāri paņem nākamo pending rindu un atzīmē processing."""
    if not DATABASE_URL:
        return None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.transaction():
            cur = conn.execute("""
                SELECT id, listing_ids, attempts
                FROM properties.pdf_jobs
                WHERE status = 'pending'
                ORDER BY requested_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """)
            row = cur.fetchone()
            if not row:
                return None
            conn.execute("""
                UPDATE properties.pdf_jobs
                SET status = 'processing', started_at = now()
                WHERE id = %s
            """, (row["id"],))
            return row


def _mark_done(job_id: int, rel_path: str) -> None:
    if not DATABASE_URL:
        return
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("""
            UPDATE properties.pdf_jobs
            SET status = 'done', finished_at = now(), file_path = %s
            WHERE id = %s
        """, (rel_path, job_id))


def _mark_error(job_id: int, attempts: int, error_msg: str) -> None:
    if not DATABASE_URL:
        return
    truncated = (error_msg or "")[:2000]
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("""
            UPDATE properties.pdf_jobs
            SET status = 'error',
                finished_at = now(),
                error = %s,
                attempts = %s
            WHERE id = %s
        """, (truncated, attempts + 1, job_id))


# ---------- Process one job ----------

def _process(job: dict) -> tuple[bool, str, Optional[str]]:
    """Izveido salikto PDF un saglabā uz volume.

    Atgriež (ok, error_msg, rel_path).
    """
    job_id = int(job["id"])
    listing_ids = [int(x) for x in job["listing_ids"]]
    if not listing_ids:
        return False, "listing_ids tukšs", None

    try:
        pdf_bytes = pdf_maker.render_pdf_bulk(listing_ids)
    except SystemExit as e:
        return False, f"SystemExit: {e}", None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}", None

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    rel_path = f"pdf_jobs/{job_id}.pdf"
    abs_path = STORAGE_ROOT / rel_path
    abs_path.write_bytes(pdf_bytes)
    return True, "", rel_path


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
    return {**_state, "enabled": PDF_POLLER_ENABLED, "interval": PDF_POLLER_INTERVAL}


async def run_loop(stop_event: asyncio.Event) -> None:
    if not PDF_POLLER_ENABLED:
        logger.info("PDF poller IZSLĒGTS (PDF_POLLER_ENABLED=0)")
        return
    if not DATABASE_URL:
        logger.warning("PDF poller bez DATABASE_URL — sleeping forever")
        await stop_event.wait()
        return

    logger.info(
        f"PDF poller sākts — interval={PDF_POLLER_INTERVAL}s, "
        f"stale_threshold={PDF_STALE_MIN}min, output_dir={PDF_DIR}"
    )
    _state["running"] = True
    loop = asyncio.get_event_loop()

    # Recovery uz startup
    try:
        recovered = await loop.run_in_executor(None, _recover_stale)
        if recovered:
            logger.warning(
                f"PDF recovery: atjaunoti {recovered} stale 'processing' ieraksti"
            )
    except Exception as e:
        logger.error(f"PDF _recover_stale: {e}", exc_info=True)

    try:
        while not stop_event.is_set():
            try:
                row = await loop.run_in_executor(None, _claim_next)
            except Exception as e:
                logger.error(f"_claim_next: {e}", exc_info=True)
                await _sleep_interruptible(stop_event, PDF_POLLER_INTERVAL)
                continue

            if row is None:
                await _sleep_interruptible(stop_event, PDF_POLLER_INTERVAL)
                continue

            jid = int(row["id"])
            n_listings = len(row["listing_ids"] or [])
            attempts = int(row.get("attempts") or 0)
            logger.info(f"PDF picked job#{jid} ({n_listings} listingi, attempts={attempts})")
            _state["last_started"] = {"job_id": jid, "n_listings": n_listings}

            ok, err, rel_path = await loop.run_in_executor(None, _process, row)

            try:
                if ok and rel_path:
                    await loop.run_in_executor(None, _mark_done, jid, rel_path)
                    _state["processed_total"] += 1
                    _state["last_result"] = {
                        "job_id": jid, "status": "done", "file_path": rel_path
                    }
                    logger.info(f"PDF job#{jid} → done ({rel_path})")
                else:
                    await loop.run_in_executor(None, _mark_error, jid, attempts, err)
                    _state["errors_total"] += 1
                    _state["last_result"] = {
                        "job_id": jid, "status": "error", "error": err[:200]
                    }
                    logger.warning(f"PDF job#{jid} → error: {err[:200]}")
            except Exception as e:
                logger.error(f"Status atjaunošana neizdevās: {e}", exc_info=True)

            _state["last_finished"] = _state["last_started"]
            await asyncio.sleep(0.5)
    finally:
        _state["running"] = False
        logger.info("PDF poller apstājies")


async def _sleep_interruptible(stop_event: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
