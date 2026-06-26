"""auto_publish_poller.py — auto-publish reconciliation (savācējs).

Problēma: auto-publish ieliek `wp_export_queue` DIVOS brīžos — (a) panelī slēdža
ieslēgšanas mirklī (`setAutoPublish` backfill), (b) `test-runner-db` pēc svaigas
AI parse (`_maybe_enqueue_auto_publish`). Listingi, kas kļūst `ok` CITĀ brīdī
(ēku ieslēdz par auto-publish vēlāk; trigeris kādu palaiž garām), paliek `ok` bet
neaiziet uz mājaslapu — neviens tos vairs nepamana.

Šis pollers periodiski tos izķer un ieliek `wp_export_queue` (queue_poller publicē).
Listings ir tiesīgs TIKAI ja:
  - Debug_status='ok'
  - zem ēkas ar auto_publish=true
  - vēl NAV uz web (wp_post_id IS NULL)
  - VERIFICĒTS numurs — listinga telefons sakrīt ar ēkas primary/secondary (pēdējie
    8 cipari). Drošības vārti: NEKAD nepublicē nepārbaudītu (piem. vecos, kas pa
    veco "pēc adreses" ceļu ienāca bez numura).
  - UNIKĀLS — nav cita JAU publicēta listinga tajā pašā ēkā ar to pašu telpas
    atslēgu (area+floor+price_type). Nepublicē dublikātus.
  - nav jau rindā vai kļūdā publicēšanai (pending/processing/error).

Loop drošība:
  - `wp_post_id` = gala atzīme: publicēts → NOT NULL → vairs nesakrīt → neatkārtojas.
  - Kļūdainos (queue status='error') NEATKĀRTO (NOT EXISTS error) — citādi bezgalīgs
    re-publish ar to pašu kļūdu (piem. neatbalstīts Space_group).
  - Numura-vārti izslēdz bez-numura listingus → tie nekad nenonāk rindā.

Konfigurējams ar env:
  AUTO_PUBLISH_ENABLED       (default "1") — "0" izslēdz
  AUTO_PUBLISH_INTERVAL      (default "900") — sekundes starp cikliem (15 min)
  AUTO_PUBLISH_MAX_PER_CYCLE (default "20") — maks. enqueue vienā ciklā
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("auto_publish_poller")

DATABASE_URL = os.getenv("DATABASE_URL")
AUTO_PUBLISH_ENABLED = os.getenv("AUTO_PUBLISH_ENABLED", "1") != "0"
AUTO_PUBLISH_INTERVAL = float(os.getenv("AUTO_PUBLISH_INTERVAL", "900"))
AUTO_PUBLISH_MAX_PER_CYCLE = int(os.getenv("AUTO_PUBLISH_MAX_PER_CYCLE", "20"))


def _tails(s) -> set[str]:
    """Telefona teksts (var būt vairāki, atdalīti ,;/|) → {pēdējo-8-ciparu kopa}."""
    if not s:
        return set()
    out: set[str] = set()
    for part in re.split(r"[,;/|]", str(s)):
        digits = re.sub(r"\D", "", part)
        if len(digits) >= 7:
            out.add(digits[-8:])
    return out


# ---------- DB ----------

def _fetch_eligible() -> list[dict]:
    """Listingi, kas gatavi auto-publicēšanai, bet vēl nav rindā/uz web.

    Heavy filtri SQL pusē (ok + auto_publish + wp_post_id null + nav rindā/kļūdā +
    unikāls). Numura-verifikācija Python pusē (robusts pret vairāku numuru lauku)."""
    if not DATABASE_URL:
        return []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute("""
            SELECT l.id, l.street, l.phone_numbers,
                   b.primary_phone, b.secondary_phone
            FROM properties.listings l
            JOIN properties.building_profiles b ON b.id = l.building_profile_id
            WHERE l."Debug_status" = 'ok'
              AND b.auto_publish IS TRUE
              AND l.wp_post_id IS NULL
              AND l.link IS NOT NULL AND l.link <> ''
              -- nav jau rindā vai kļūdā publicēšanai (loop drošība)
              AND NOT EXISTS (
                    SELECT 1 FROM properties.wp_export_queue q
                    WHERE q.listing_id = l.id
                      AND q.action = 'publish'
                      AND q.status IN ('pending', 'processing', 'error')
              )
              -- unikāls: nav cita JAU publicēta tās pašas ēkas listinga ar to pašu
              -- telpas atslēgu (area + floor + price_type) → nepublicē dublikātu
              AND NOT EXISTS (
                    SELECT 1 FROM properties.listings l2
                    WHERE l2.building_profile_id = l.building_profile_id
                      AND l2.id <> l.id
                      AND l2.wp_post_id IS NOT NULL
                      AND coalesce(btrim(l2.area_m2), '') = coalesce(btrim(l.area_m2), '')
                      AND coalesce(btrim(l2.floor), '')  = coalesce(btrim(l.floor), '')
                      AND coalesce(l2.price_type, '')    = coalesce(l.price_type, '')
              )
            ORDER BY l.id
        """).fetchall()

    out = []
    for r in rows:
        # Drošības vārti: listinga numuram JĀSAKRĪT ar ēkas primary/secondary.
        owner = _tails(r["primary_phone"]) | _tails(r["secondary_phone"])
        listing_tails = _tails(r["phone_numbers"])
        if not listing_tails or not (listing_tails & owner):
            continue  # nepārbaudīts numurs → NEKAD nepublicē (bez-numura mantojums)
        out.append(r)
    return out


def _enqueue(listing_id: int) -> bool:
    """Ieliek listingu wp_export_queue (publish). Atgriež True, ja tika ielikts.
    Atkārtoti pārbauda pending/processing/error tieši pirms INSERT (race drošība)."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.transaction():
            active = conn.execute("""
                SELECT 1 FROM properties.wp_export_queue
                WHERE listing_id = %s AND action = 'publish'
                  AND status IN ('pending', 'processing', 'error')
                LIMIT 1
            """, (listing_id,)).fetchone()
            if active:
                return False
            conn.execute("""
                INSERT INTO properties.wp_export_queue
                    (listing_id, status, action, requested_by)
                VALUES (%s, 'pending', 'publish', 'auto_publish')
            """, (listing_id,))
    return True


# ---------- Loop ----------

_state = {
    "running": False,
    "last_cycle": None,
    "enqueued_total": 0,
    "errors_total": 0,
}


def get_status() -> dict:
    return {
        **_state,
        "enabled": AUTO_PUBLISH_ENABLED,
        "interval": AUTO_PUBLISH_INTERVAL,
        "max_per_cycle": AUTO_PUBLISH_MAX_PER_CYCLE,
    }


async def run_loop(stop_event: asyncio.Event) -> None:
    if not AUTO_PUBLISH_ENABLED:
        logger.info("Auto-publish poller IZSLĒGTS (AUTO_PUBLISH_ENABLED=0)")
        return
    if not DATABASE_URL:
        logger.warning("Auto-publish poller bez DATABASE_URL — sleeping forever")
        await stop_event.wait()
        return

    logger.info("Auto-publish poller sākts — interval=%ss, max/cikls=%d",
                AUTO_PUBLISH_INTERVAL, AUTO_PUBLISH_MAX_PER_CYCLE)
    _state["running"] = True
    loop = asyncio.get_event_loop()
    try:
        while not stop_event.is_set():
            try:
                eligible = await loop.run_in_executor(None, _fetch_eligible)
                enqueued = 0
                for r in eligible:
                    if enqueued >= AUTO_PUBLISH_MAX_PER_CYCLE:
                        break  # pārējos nākamajam ciklam
                    try:
                        if await loop.run_in_executor(None, _enqueue, int(r["id"])):
                            enqueued += 1
                            logger.info("Auto-publish rindā | listing#%s | %s",
                                        r["id"], r.get("street"))
                    except Exception as e:
                        _state["errors_total"] += 1
                        logger.error("listing#%s enqueue kļūda: %s",
                                     r.get("id"), e, exc_info=True)
                _state["enqueued_total"] += enqueued
                _state["last_cycle"] = {"eligible": len(eligible), "enqueued": enqueued}
                if enqueued:
                    logger.info("Auto-publish cikls: tiesīgi=%d, ierindoti=%d",
                                len(eligible), enqueued)
            except Exception as e:
                _state["errors_total"] += 1
                logger.error("Auto-publish cikls neizdevās: %s", e, exc_info=True)

            await _sleep_interruptible(stop_event, AUTO_PUBLISH_INTERVAL)
    finally:
        _state["running"] = False
        logger.info("Auto-publish poller apstājies")


async def _sleep_interruptible(stop_event: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
