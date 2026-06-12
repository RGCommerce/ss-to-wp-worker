"""price_sync_poller.py — cenas auto-sinhronizācija ss.lv → listings → WP.

Problēma: kad ss.lv īpašnieks PĀRPUBLICĒ sludinājumu ar citu cenu, scraper
atjauno `scrape_inbox` (price_history, republish_count), BET `listings` paliek
iesaldēts uz veco cenu — `inbox_to_listings.py` lieto `ON CONFLICT DO NOTHING`
un re-publicētu inbox rindu vairs neapskata (skip_reason jau uzlikts). Rezultāts:
panelis (un mājaslapa, ja publicēts) rāda novecojušu cenu.

Šis pollers periodiski:
  1. Salīdzina `listings.price` ar JAUNĀKO `scrape_inbox.price` pa to pašu `link`.
  2. Kur atšķiras (un darījuma tips sakrīt — noma↔noma / pārdošana↔pārdošana):
     UPDATE listings SET price + price_per_m2 (pārrēķina no area).
  3. Ja listings ir mājaslapā (on_website + wp_post_id) → ieliek wp_export_queue
     rindā (action='publish'). queue_poller pārpublicē = `update_property`
     (atjauno ESOŠO WP postu, NEdublē, pārizmanto galeriju → bez AI izmaksām).

Tikai ss.lv-source listingiem (agent_anketa nav ss.lv link → JOIN tos izlaiž).

Konfigurējams ar env:
  PRICE_SYNC_ENABLED   (default "1") — "0" izslēdz
  PRICE_SYNC_INTERVAL  (default "1800") — sekundes starp cikliem (30 min)
  PRICE_SYNC_MAX_REPUB (default "25") — maks. WP re-publish rindu vienā ciklā
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("price_sync_poller")

DATABASE_URL = os.getenv("DATABASE_URL")
PRICE_SYNC_ENABLED = os.getenv("PRICE_SYNC_ENABLED", "1") != "0"
PRICE_SYNC_INTERVAL = float(os.getenv("PRICE_SYNC_INTERVAL", "1800"))
PRICE_SYNC_MAX_REPUB = int(os.getenv("PRICE_SYNC_MAX_REPUB", "25"))

# Darījuma tipu kanonizācija — sinhronizējam cenu TIKAI ja abās pusēs tas pats
# darījums (citādi noma↔pārdošana cenu lēciens nav reāla cenas izmaiņa).
_RENT = {"monthly", "mēneša", "menesa", "mēnesī", "menesi", "daily", "diennakts"}
_SALE = {"regular", "parastā", "parasta", "pārdošana", "pardosana"}


def _deal(price_type: Optional[str]) -> Optional[str]:
    pt = (price_type or "").strip().lower()
    if pt in _RENT:
        return "rent"
    if pt in _SALE:
        return "sale"
    return None


def _num(v) -> Optional[int]:
    """Cenu teksts ('1 200', '1200 €') → int. None, ja nav cipara/unknown."""
    if v is None:
        return None
    digits = re.findall(r"\d+", str(v).replace(" ", ""))
    if not digits:
        return None
    try:
        return int("".join(digits))
    except ValueError:
        return None


# ---------- DB ----------

def _fetch_diffs() -> list[dict]:
    """Atgriež listingus, kuru cena atšķiras no jaunākās ss.lv cenas."""
    if not DATABASE_URL:
        return []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        rows = conn.execute("""
            SELECT l.id, l.price AS l_price, l.price_type AS l_pt,
                   l.area_m2 AS area, l.on_website, l.wp_post_id,
                   si.price AS s_price, si.price_type AS s_pt
            FROM properties.listings l
            JOIN LATERAL (
                SELECT price, price_type
                FROM properties.scrape_inbox si2
                WHERE si2.link = l.link
                ORDER BY si2.date_posted DESC NULLS LAST, si2.id DESC
                LIMIT 1
            ) si ON true
            WHERE l."Debug_status" = 'ok'
              AND l.link IS NOT NULL AND l.link <> ''
        """).fetchall()

    out = []
    for r in rows:
        lp, sp = _num(r["l_price"]), _num(r["s_price"])
        if lp is None or sp is None or lp == sp:
            continue
        # Darījuma tips jāsakrīt (vai inbox tipam jābūt nezināmam → pieņemam).
        ld, sd = _deal(r["l_pt"]), _deal(r["s_pt"])
        if ld and sd and ld != sd:
            continue
        r["_new_price"] = sp
        out.append(r)
    return out


def _apply(diff: dict) -> bool:
    """UPDATE listings cenu; ja on_website → ieliek WP re-publish rindā.
    Atgriež True, ja tika ierindota re-publish (lai ciklā skaitām limitu)."""
    lid = int(diff["id"])
    new_price = int(diff["_new_price"])
    area = _num(diff["area"])
    ppm = round(new_price / area, 2) if area and area > 0 else None
    enqueued = False
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute("""
                UPDATE properties.listings
                SET price = %s, price_per_m2 = %s
                WHERE id = %s
            """, (str(new_price), ppm, lid))

            # Re-publish tikai ja reāli mājaslapā.
            if diff["on_website"] and diff["wp_post_id"] is not None:
                active = conn.execute("""
                    SELECT 1 FROM properties.wp_export_queue
                    WHERE listing_id = %s AND status IN ('pending', 'processing')
                    LIMIT 1
                """, (lid,)).fetchone()
                if not active:
                    conn.execute("""
                        INSERT INTO properties.wp_export_queue
                            (listing_id, status, action, requested_by)
                        VALUES (%s, 'pending', 'publish', 'price_sync')
                    """, (lid,))
                    enqueued = True
    logger.info(
        "listing#%d cena %s → %s%s",
        lid, diff["l_price"], new_price,
        " + WP re-publish rindā" if enqueued else "",
    )
    return enqueued


# ---------- Loop ----------

_state = {
    "running": False,
    "last_cycle": None,
    "updated_total": 0,
    "republished_total": 0,
    "errors_total": 0,
}


def get_status() -> dict:
    return {**_state, "enabled": PRICE_SYNC_ENABLED, "interval": PRICE_SYNC_INTERVAL}


async def run_loop(stop_event: asyncio.Event) -> None:
    if not PRICE_SYNC_ENABLED:
        logger.info("Price sync poller IZSLĒGTS (PRICE_SYNC_ENABLED=0)")
        return
    if not DATABASE_URL:
        logger.warning("Price sync poller bez DATABASE_URL — sleeping forever")
        await stop_event.wait()
        return

    logger.info("Price sync poller sākts — interval=%ss, max_repub/cikls=%d",
                PRICE_SYNC_INTERVAL, PRICE_SYNC_MAX_REPUB)
    _state["running"] = True
    loop = asyncio.get_event_loop()
    try:
        while not stop_event.is_set():
            try:
                diffs = await loop.run_in_executor(None, _fetch_diffs)
                updated = republished = 0
                for d in diffs:
                    if republished >= PRICE_SYNC_MAX_REPUB and d["on_website"]:
                        # Pārējos mājaslapas re-publish atstāj nākamajam ciklam.
                        continue
                    try:
                        if await loop.run_in_executor(None, _apply, d):
                            republished += 1
                        updated += 1
                    except Exception as e:
                        _state["errors_total"] += 1
                        logger.error("listing#%s cenas sync kļūda: %s",
                                     d.get("id"), e, exc_info=True)
                _state["updated_total"] += updated
                _state["republished_total"] += republished
                _state["last_cycle"] = {"updated": updated, "republished": republished}
                if updated:
                    logger.info("Price sync cikls: atjaunoti=%d, WP re-publish=%d",
                                updated, republished)
            except Exception as e:
                _state["errors_total"] += 1
                logger.error("Price sync cikls neizdevās: %s", e, exc_info=True)

            await _sleep_interruptible(stop_event, PRICE_SYNC_INTERVAL)
    finally:
        _state["running"] = False
        logger.info("Price sync poller apstājies")


async def _sleep_interruptible(stop_event: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
