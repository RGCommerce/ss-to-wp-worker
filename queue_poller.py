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
import shutil
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Optional

import psycopg
import requests
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent))
import publish_to_wp  # noqa: E402
from wp_publisher import WPPublisher, WPPublisherError  # noqa: E402

logger = logging.getLogger("queue_poller")

DATABASE_URL = os.getenv("DATABASE_URL")
POLLER_ENABLED = os.getenv("POLLER_ENABLED", "1") != "0"
POLLER_INTERVAL = float(os.getenv("POLLER_INTERVAL", "10"))
# Cik laikā 'processing' ieraksts tiek uzskatīts par stale (worker mira/restart-ēja)
STALE_PROCESSING_MIN = int(os.getenv("STALE_PROCESSING_MIN", "30"))
# WP LP auto-pin (#44) — pēc publicēšanas uzliek property_state termu klienta LP-am
WP_URL = (os.getenv("WP_URL") or "").rstrip("/")
RGC_MK_TOKEN = os.getenv("RGC_MK_TOKEN")


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

    GATE: publish rindām listings.Debug_status MUST = 'ok'. Tā agent_anketa EASY
    rindas, kuras vēl gaida uz AI worker (3. plūsma) papildinājumu, NETIEK
    paķertas, kamēr AI nav uzlicis Debug_status='ok'. publish_to_wp.publish()
    vienalga atteiktos ar NULL Debug_status — labāk poller pats atliek.
    UNPUBLISH/DELETE rindām (action='unpublish'/'delete') gate NAV — noņemšanai
    Debug_status nav svarīgs. DELETE rindām listinga rinda JAU ir dzēsta panelī
    (purgeListing) → LEFT JOIN, lai rinda joprojām tiek paķerta.

    Atgriež claim-oto rindu vai None. FOR UPDATE SKIP LOCKED — droši paralēli.
    """
    if not DATABASE_URL:
        return None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.transaction():
            cur = conn.execute("""
                SELECT q.id, q.listing_id, q.attempts, q.action, q.wp_post_id
                FROM properties.wp_export_queue q
                LEFT JOIN properties.listings l ON l.id = q.listing_id
                WHERE q.status = 'pending'
                  AND (q.action IN ('unpublish', 'delete')
                       OR l."Debug_status" = 'ok')
                ORDER BY q.priority DESC, q.requested_at ASC
                LIMIT 1
                FOR UPDATE OF q SKIP LOCKED
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


def _mark_done(queue_id: int, listing_id: int, action: str = "publish") -> None:
    """Veiksmīgi pabeigts: status='done'.
    publish → listing on_website=true; unpublish → on_website jau iestatīts
    _unpublish() (false + wp_post_id notīrīts), tāpēc to NEpieskaram."""
    if not DATABASE_URL:
        return
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.transaction():
            if action == "delete":
                # _delete_full() jau izdzēsa listings rindu → cascade (FK ON
                # DELETE CASCADE) notīrīja arī šo queue rindu, tāpēc šis UPDATE
                # skars 0 rindas = nekaitīgi. Nelasām listings (vairs nav).
                conn.execute("""
                    UPDATE properties.wp_export_queue
                    SET status = 'done', finished_at = now()
                    WHERE id = %s
                """, (queue_id,))
                return

            # publish_to_wp.publish() / _unpublish() jau atjaunoja
            # listings.wp_post_id — paņemam to reference queue rindas saturam.
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

            if action == "publish":
                # Atzīmēt listings.on_website = true (publish_to_wp.py to neatjauno)
                conn.execute("""
                    UPDATE properties.listings
                    SET on_website = true
                    WHERE id = %s
                """, (listing_id,))

        # PĒC commit (ārpus txn): #44 LP auto-pin. Ja kāds bija atzīmējis šo listingu
        # klienta LP-am pirms publicēšanas (broker_pin, ko panelis ieliek pie "Export
        # to WP" no klienta lapas), uzliek WP property_state termu tagad, kad wp_post_id
        # eksistē → parādās publiskajā LP. Ārpus txn, lai HTTP neturētu DB lock.
        if action == "publish" and wp_post_id:
            _tag_pending_lps(conn, listing_id, int(wp_post_id))


def _tag_pending_lps(conn, listing_id: int, wp_post_id: int) -> None:
    """#44: atrod aktīvos broker_pin šim listingam un uzliek WP property_state termu
    katra klienta LP slug-am (caur WP /anketa-v2/lp-tag). Best-effort — kļūda WP
    pusē neapgāž publicēšanu (listings.on_website jau true, broker_pin paliek →
    nākamā publish/pin mēģinās vēlreiz). lp-tag arī stampo "Jauns" laiku."""
    if not (WP_URL and RGC_MK_TOKEN):
        return
    rows = conn.execute("""
        SELECT DISTINCT state_slug
          FROM properties.client_listing_actions
         WHERE listing_id = %s AND action_type = 'broker_pin' AND active
    """, (listing_id,)).fetchall()
    for r in rows:
        slug = r["state_slug"]
        if not slug:
            continue
        try:
            resp = requests.post(
                f"{WP_URL}/wp-json/rgc/v1/anketa-v2/lp-tag/{slug}",
                headers={"X-RGC-Token": RGC_MK_TOKEN},
                json={"wp_post_ids": [wp_post_id]},
                timeout=20,
            )
            if resp.status_code != 200:
                logger.warning("lp-tag fail slug=%s listing=%s → HTTP %s: %s",
                               slug, listing_id, resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("lp-tag exc slug=%s listing=%s: %s", slug, listing_id, e)


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
    """Apstrādā vienu rindas ierakstu. action='unpublish' → noņem no WP;
    citādi publish_to_wp.publish(). Atgriež (ok, log)."""
    listing_id = int(queue_row["listing_id"])
    action = (queue_row.get("action") or "publish")
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            if action == "unpublish":
                _unpublish(listing_id)
            elif action == "delete":
                _delete_full(listing_id, queue_row.get("wp_post_id"))
            else:
                publish_to_wp.publish(listing_id)
        return True, buf.getvalue()
    except SystemExit as e:
        return False, f"SystemExit: {e}\n{buf.getvalue()}"
    except Exception as e:
        traceback.print_exc(file=buf)
        return False, f"{type(e).__name__}: {e}\n{buf.getvalue()}"


def _unpublish(listing_id: int) -> None:
    """Noņem listingu no mājaslapas (bug #34 — "Aizņemts/Iznomāts").
    WP posts → Atkritne (delete force=False, atgriežams 30 dienas WP pusē).
    DB: on_website=false, wp_post_id=NULL (lai vēlāk var publicēt no jauna).
    Listings paliek DB ar visu info + occupancy_* laukiem (uzliek panelis)."""
    if not DATABASE_URL:
        raise RuntimeError("Trūkst DATABASE_URL")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT wp_post_id FROM properties.listings WHERE id = %s",
            (listing_id,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Listings #{listing_id} nav atrasts")
        wp_post_id = row["wp_post_id"]

        if wp_post_id:
            wp = WPPublisher()
            wp.delete_property(int(wp_post_id), force=False)  # → Atkritne
            print(f"  ✓ WP posts #{wp_post_id} pārvietots uz Atkritni")
        else:
            print("  · nav wp_post_id — nekas nav jānoņem no WP")

        with conn.transaction():
            conn.execute("""
                UPDATE properties.listings
                SET on_website = false, wp_post_id = NULL, wp_synced_at = now()
                WHERE id = %s
            """, (listing_id,))
        print(f"  ✓ DB: listings #{listing_id} on_website=false, wp_post_id=NULL")


def _delete_full(listing_id: int, wp_post_id: Optional[int]) -> None:
    """PILNĪGA dzēšana (action='delete' — paneļa sarkanā "Dzēst sludinājumu").
    Iztīra VISU, ko panelis nevar, un PĒDĒJO izdzēš pašu listings rindu:

      1. WP posts → DZĒŠ NEATGRIEZENISKI (force=True; wp_post_id no QUEUE rindas).
      2. /storage/listings/<id>/  — visas bildes (raw + wp_raw + ai_ready +
         processed) vienā mapē, tāpēc pietiek ar listing_id.
      3. PDF faili + pdf_jobs rindas, kur šis listings ir VIENĪGAIS (legacy
         multi-listing PDF NEdzēšam, lai nesabojātu citu listingu piedāvājumu).
      4. listings rinda — PĒDĒJĀ. ⚠ wp_export_queue.listing_id FK ir DB līmenī
         ON DELETE CASCADE (Prisma to nerāda!) → ja panelis dzēstu listingu, tas
         iznīcinātu pašu delete-rindu pirms worker to redz. Tāpēc listingu dzēš
         ŠEIT, beigās; cascade tad notīra arī šo queue rindu (_mark_done UPDATE
         skars 0 rindas = nekaitīgi). matches_listings arī = cascade.

    Idempotents: soļi 1-3 droši atkārtojas (404/iztrūkstoši faili = izlaiž). Ja
    kāds solis krīt PIRMS 4., listings paliek → FK apmierināts → retry pēc
    _recover_stale. Listingu dzēš tikai pēc tam, kad WP+faili tiešām prom."""
    if not DATABASE_URL:
        raise RuntimeError("Trūkst DATABASE_URL")

    # 1) WP posts → neatgriezeniski (force=True). 404 = jau dzēsts (drošs retry).
    if wp_post_id:
        try:
            wp = WPPublisher()
            wp.delete_property(int(wp_post_id), force=True)
            print(f"  ✓ WP posts #{wp_post_id} DZĒSTS neatgriezeniski (force)")
        except WPPublisherError as e:
            if "HTTP 404" in str(e):
                print(f"  · WP posts #{wp_post_id} jau nav (404) — izlaiž")
            else:
                raise
    else:
        print("  · nav wp_post_id — nekas nav jānoņem no WP")

    # 2) Bilžu mape
    storage_root = os.getenv("STORAGE_ROOT", "/storage")
    img_dir = Path(storage_root) / "listings" / str(listing_id)
    if img_dir.exists():
        shutil.rmtree(img_dir, ignore_errors=True)
        print(f"  ✓ Bildes dzēstas: {img_dir}")
    else:
        print(f"  · nav bilžu mapes ({img_dir})")

    # 3) PDF faili + pdf_jobs rindas (tikai šī listinga vienpošu darbi)
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        jobs = conn.execute(
            """SELECT id, file_path FROM properties.pdf_jobs
               WHERE listing_ids = ARRAY[%s]::bigint[]""",
            (listing_id,),
        ).fetchall()
        for j in jobs:
            fp = j.get("file_path")
            if fp:
                pdf_path = Path(storage_root) / fp
                try:
                    pdf_path.unlink()
                    print(f"  ✓ PDF dzēsts: {pdf_path}")
                except FileNotFoundError:
                    pass
        if jobs:
            with conn.transaction():
                conn.execute(
                    """DELETE FROM properties.pdf_jobs
                       WHERE listing_ids = ARRAY[%s]::bigint[]""",
                    (listing_id,),
                )
            print(f"  ✓ pdf_jobs rindas dzēstas: {len(jobs)}")

    # 4) PĒDĒJAIS — pati listings rinda (cascade: matches_listings +
    #    wp_export_queue, t.sk. šī delete-rinda).
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM properties.listings WHERE id = %s", (listing_id,)
            )
    print(f"  ✓ DB: listings #{listing_id} dzēsts (cascade: matches + queue)")


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
            action = (row.get("action") or "publish")
            attempts = int(row.get("attempts") or 0)
            logger.info(
                f"Picked queue#{qid} listing#{lid} action={action} "
                f"(attempts={attempts})"
            )
            _state["last_started"] = {"queue_id": qid, "listing_id": lid}

            ok, log_text = await loop.run_in_executor(None, _process, row)

            try:
                if ok:
                    await loop.run_in_executor(None, _mark_done, qid, lid, action)
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
