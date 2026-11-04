"""«Pāranalizēt» vienam sludinājumam (Raimonds 2026-11-04).

Bildes pievienotas vēlāk (pēc partnera importa) → aģents nospiež pogu → AI apskata TIKAI šī sludinājuma
mūsu bildes (ai_ready) + tekstu un aizpilda laukus. Cilvēka atzīmētais (agent_locked_fields) netiek aiztikts.
Sludinājums, kas bija «ok», paliek «ok» (citādi tas pazustu no match). Ja mājaslapā — pārpublicē.
Darbojas fonā; statusu var pajautāt.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("listing_reanalyze")
_status: dict[int, dict[str, Any]] = {}
_lock = threading.Lock()


def _row(conn, listing_id: int) -> dict | None:
    r = conn.execute("""
        SELECT l.*,
               bp."Building_description" AS bp_building_description,
               bp.full_address           AS bp_full_address,
               bp.building_type          AS bp_building_type,
               bp.building_class         AS bp_building_class,
               bp.has_conference_room    AS bp_has_conference_room
          FROM properties.listings l
          LEFT JOIN properties.building_profiles bp ON bp.id = l.building_profile_id
         WHERE l.id = %s""", (listing_id,)).fetchone()
    return dict(r) if r else None


def run(listing_id: int, requested_by: str | None = None) -> dict:
    import agent_ai_poller as poller
    out: dict[str, Any] = {"listing_id": listing_id, "state": "running", "started_at": datetime.now(timezone.utc).isoformat()}
    with psycopg.connect(poller.DATABASE_URL, row_factory=dict_row) as conn:
        row = _row(conn, listing_id)
        if not row:
            return {**out, "state": "error", "message": "sludinājums nav atrasts"}
        if not (row.get("local_image_paths_processed") or []):
            return {**out, "state": "error", "message": "nav apstrādātu bilžu"}
        old_status = row.get("Debug_status")
        ok, msg = poller._process_one(row)
        if old_status == "ok":
            conn.execute("""UPDATE properties.listings SET "Debug_status"='ok' WHERE id=%s AND "Debug_status" IS DISTINCT FROM 'ok'
                            AND "Debug_status" NOT IN ('not_commercial','wanted_ad')""", (listing_id,))
        conn.execute("""UPDATE properties.listings SET "Debug_note" = left(coalesce("Debug_note",'') || %s, 1000), updated_at = now()
                         WHERE id = %s""", (f" | pāranalizēts {datetime.now().date()} ({requested_by or 'panelis'})", listing_id))
        republished = False
        if ok and row.get("on_website"):
            conn.execute("""INSERT INTO properties.wp_export_queue (listing_id, action, status, requested_by)
                            VALUES (%s,'publish','pending',%s)
                            ON CONFLICT (listing_id) WHERE status='pending' DO NOTHING""",
                         (listing_id, requested_by or "pāranalizēts"))
            republished = True
        conn.commit()
    return {**out, "state": "done" if ok else "error", "message": msg, "republished": republished,
            "finished_at": datetime.now(timezone.utc).isoformat()}


def start(listing_id: int, requested_by: str | None = None) -> dict:
    with _lock:
        cur = _status.get(listing_id)
        if cur and cur.get("state") == "running":
            return cur
        _status[listing_id] = {"listing_id": listing_id, "state": "running"}

    def _job():
        try:
            res = run(listing_id, requested_by)
        except Exception as e:
            logger.exception("pāranalizēt #%s", listing_id)
            res = {"listing_id": listing_id, "state": "error", "message": f"{type(e).__name__}: {str(e)[:200]}"}
        with _lock:
            _status[listing_id] = res

    threading.Thread(target=_job, name=f"reanalyze-{listing_id}", daemon=True).start()
    return _status[listing_id]


def status(listing_id: int) -> dict:
    return _status.get(listing_id) or {"listing_id": listing_id, "state": "idle"}
