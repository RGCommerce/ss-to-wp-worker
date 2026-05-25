"""agent_ai_poller.py — 3. AI plūsma: aģenta anketa → listings → AI papildina laukus.

Paralēli queue_poller un pdf_poller iekš ss-to-wp-worker.

Plūsma:
    properties.listings WHERE source LIKE 'agent_anketa%' AND Debug_status IS NULL
        → lasa Agent_comment + building_profiles.Building_description (caur JOIN)
        → lasa /storage/listings/<id>/ai_ready/img_*.jpg kā image-proxy URLs (ar tokenu)
        → sauc OpenAI Vision ar ai_text_helpers.PROMPT + JSON_SCHEMA (tas pats, ko WP inbox lieto)
        → UPDATE listings AI rezultātos, IZŅEMOT agent_locked_fields kolonnas
        → Debug_status='ok' → queue_poller pēc tam paķer un publicē uz WP

Konfigurējams ar env:
    AGENT_AI_ENABLED   (default "1")
    AGENT_AI_INTERVAL  (default "15")  — pollu intervals sekundēs
    AGENT_AI_STALE_MIN (default "60")  — recovery threshold (Debug_status="processing" iesprūdis)
    OPENAI_API_KEY     — obligāts
    SS_TO_WP_BASE_URL  — publiskā bāze priekš image-proxy URL (default Railway production)
    RGC_MK_TOKEN       — token publiska image-proxy autentifikācijai
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger("agent_ai_poller")

DATABASE_URL = os.getenv("DATABASE_URL")
AGENT_AI_ENABLED = os.getenv("AGENT_AI_ENABLED", "1") != "0"
AGENT_AI_INTERVAL = float(os.getenv("AGENT_AI_INTERVAL", "15"))
AGENT_AI_STALE_MIN = int(os.getenv("AGENT_AI_STALE_MIN", "60"))

# Publiskā bāze image-proxy URL veidošanai (OpenAI Vision fetch'os bildes)
SS_TO_WP_BASE_URL = os.getenv(
    "SS_TO_WP_BASE_URL",
    "https://ss-to-wp-worker-production.up.railway.app",
).rstrip("/")
RGC_MK_TOKEN = os.getenv("RGC_MK_TOKEN")


_state = {
    "running": False,
    "last_started": None,
    "last_finished": None,
    "last_result": None,
    "processed_total": 0,
    "errors_total": 0,
    "skipped_no_key": 0,
    # Diagnostika — kas notika pēdējā cyclā
    "last_claim_at": None,
    "last_claim_result": None,  # "row_id=82428" vai "None" vai "error: ..."
    "loop_iterations": 0,
}


def get_status() -> dict:
    return {
        **_state,
        "enabled": AGENT_AI_ENABLED,
        "interval": AGENT_AI_INTERVAL,
        "has_openai_key": bool(os.getenv("OPENAI_API_KEY")),
    }


# ---------- DB ----------

def _recover_stale() -> int:
    """No-op: Debug_status enum neatbalsta 'processing', tāpēc claim laikā
    neuzliek processing flag. Mūsu paši pollers WAIT'os caur await
    run_in_executor — dubultprocess viena workera ietvaros nenotiks.
    Recovery vajadzīgs tikai, ja Railway scales > 1 instance (paliek TODO).
    """
    return 0


def _claim_next() -> Optional[dict]:
    """Paņem nākamo agent_anketa listings ar Debug_status NULL.

    NB: Debug_status enum NEATĻAUJ 'processing' vērtību, tāpēc claim laikā
    NEUZLIEK lock flag. Atstāj NULL kamēr AI pabeidz, tad uzliek 'ok'.

    Iekš viena workera process (pašreiz) — `await run_in_executor` blokē
    nākamo cikla iterāciju, tāpēc race condition nav iespējama. Ja Railway
    kādreiz scales > 1, vajadzēs pārveidot uz vienu kopīgu transakciju visam
    procesam (claim → AI → update) ar SELECT FOR UPDATE SKIP LOCKED.
    """
    if not DATABASE_URL:
        return None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        cur = conn.execute("""
            SELECT l.*,
                   bp."Building_description" AS bp_building_description,
                   bp.full_address           AS bp_full_address,
                   bp.building_type          AS bp_building_type,
                   bp.building_class         AS bp_building_class,
                   bp.has_conference_room    AS bp_has_conference_room
            FROM properties.listings l
            LEFT JOIN properties.building_profiles bp ON bp.id = l.building_profile_id
            WHERE l.source LIKE %s
              AND l."Debug_status" IS NULL
            ORDER BY l.id
            LIMIT 1
        """, ("agent_anketa%",))
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)


def _mark_error(listing_id: int, error_msg: str) -> None:
    """Marks listing as failed AI. Uzliek 'low_evidence' enum (lai _claim_next
    to vairs neredz), saglabā kļūdu Debug_note. Lai retry — manuāli noliek
    Debug_status=NULL caur DB skriptu."""
    if not DATABASE_URL:
        return
    truncated = (error_msg or "")[:500]
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """UPDATE properties.listings
               SET "Debug_status" = 'low_evidence',
                   "Debug_note"   = %s
               WHERE id = %s""",
            (truncated, listing_id),
        )


# ---------- Tekst- un bilžu sagatavošana ----------

def _build_text_from_listing(row: Dict[str, Any]) -> str:
    """Salikt sludinājuma tekstu AI vajadzībām no listings + BP join.

    Aģenta anketā nav HTML apraksta — saliekam strukturēto datu apkopojumu +
    Agent_comment + Building_description.
    """
    parts: List[str] = []
    addr_bits = []
    if row.get("street"):
        addr_bits.append(str(row["street"]))
    if row.get("city"):
        addr_bits.append(str(row["city"]))
    if row.get("district"):
        addr_bits.append(f"rajons {row['district']}")
    if addr_bits:
        parts.append("Adrese: " + ", ".join(addr_bits))

    if row.get("bp_building_type"):
        parts.append(f"Ēkas tips: {row['bp_building_type']}")
    if row.get("bp_building_class"):
        parts.append(f"Ēkas klase: {row['bp_building_class']}")

    structural = []
    if row.get("Space_group"):
        structural.append(f"Telpas tips: {row['Space_group']}")
    if row.get("area_m2"):
        structural.append(f"Platība: {row['area_m2']} m²")
    if row.get("floor"):
        structural.append(f"Stāvs: {row['floor']}")
    if row.get("price"):
        pt = row.get("price_type")
        suffix = " EUR/mēn" if pt in ("monthly", "mēneša") else " EUR"
        structural.append(f"Cena: {row['price']}{suffix}")
    if structural:
        parts.append(" · ".join(structural))

    if row.get("bp_building_description"):
        parts.append(f"Ēkas apraksts (no aģenta): {row['bp_building_description']}")
    if row.get("Agent_comment"):
        parts.append(f"Aģenta komentārs: {row['Agent_comment']}")

    return "\n".join(parts) if parts else "Aģenta anketa bez teksta — analizē tikai bildes."


def _build_image_urls_for_openai(row: Dict[str, Any]) -> List[str]:
    """Salikt publiskos image-proxy URLs (ar tokenu), ko OpenAI Vision fetch'os.

    local_image_paths_processed var saturēt:
      - relatīvos ceļus: "listings/<id>/ai_ready/img_001.jpg"
      - absolūtos ceļus: "/storage/listings/<id>/ai_ready/img_001.jpg"
    Regex atrod 'listings/<id>/<folder>/<file>' segmentu jebkurā formātā.
    """
    if not RGC_MK_TOKEN:
        return []
    import re
    paths = row.get("local_image_paths_processed") or []
    pattern = re.compile(r"listings/(\d+)/(ai_ready|raw)/([^/\\\\]+)$")
    urls: List[str] = []
    for rel in paths:
        rel_str = str(rel).replace("\\", "/")  # Windows path safety
        m = pattern.search(rel_str)
        if not m:
            continue
        listing_id, folder, filename = m.groups()
        url = (
            f"{SS_TO_WP_BASE_URL}/agent/image-proxy/"
            f"{listing_id}/{folder}/{filename}?token={RGC_MK_TOKEN}"
        )
        urls.append(url)
    return urls


# ---------- AI un update ----------

# Lauki, kurus AI worker GENERĒ. Mēs respektējam agent_locked_fields un nepārrakstam tos.
AI_OUTPUT_FIELDS = [
    "building_type", "building_class", "Building_description", "electric_power_kw",
    "Apsaimniekosanas_maksa", "NIN", "Komunalie", "Papildu_maksas",
    "Parkings", "Space_group", "Potential_space_group", "Space_condition",
    "Agent_comment", "Mebeleta_telpa", "Logu_type", "Griestu_augstums",
    "Gridas_materials", "Gridas_izturiba_kg_m2", "Dalama_telpa", "street_entrance",
    "Cik_telpas", "cik_WC", "Apkure", "Treifelis_Pacelajs", "Virtuve_check",
    "Balkons_check", "Apsargajama_teritorija_check", "Ventilacijas_sistema_check",
    "Rampa_logistikai_check", "Rampa_logistikai_count", "Pacelamie_varti_check",
    "Pacelamie_varti_count", "Auto_pacelajs_check", "Sava_ieeja_check",
    "Ir_izlietne_telpa_check", "Sava_eka_check", "Nozogota_teritorija_check",
    "Zemes_gabals_m2", "Investiciju_strategija", "Confidence",
]


def _update_listing_with_ai(
    listing_id: int,
    locked_fields: List[str],
    ai_result: Dict[str, Any],
) -> None:
    """UPDATE listings ar AI rezultātiem, IZŅEMOT agent_locked_fields kolonnas.

    Plus uzliek Debug_status='ok' (vai cita statusu, ja AI atgrieza problēmu).
    """
    if not DATABASE_URL:
        return
    locked = set(locked_fields or [])
    updates: Dict[str, Any] = {}

    for k in AI_OUTPUT_FIELDS:
        if k in locked:
            continue  # aģenta ievadi nepārraksta
        if k not in ai_result:
            continue
        v = ai_result[k]
        if k == "Potential_space_group" and str(v) == "unknown":
            v = None
        updates[k] = v

    # Debug fields uzstāj vienmēr
    updates["Debug_status"] = ai_result.get("Debug_status") or "ok"
    if ai_result.get("Debug_note"):
        updates["Debug_note"] = ai_result["Debug_note"][:500]

    if not updates:
        return

    cols = list(updates.keys())
    set_parts = ", ".join(f'"{c}" = %s' for c in cols)
    params = list(updates.values()) + [listing_id]
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            f'UPDATE properties.listings SET {set_parts} WHERE id = %s',
            params,
        )


# ---------- Proceses iter ----------

def _process_one(row: Dict[str, Any]) -> tuple[bool, str]:
    """Apstrādā vienu listing rindu — AI sauciens + UPDATE."""
    listing_id = int(row["id"])
    try:
        # Lazy import — ja OPENAI_API_KEY trūkst, ai_text_helpers nokritīs pie modules ielādes
        import ai_text_helpers as helpers  # noqa: F401
    except Exception as e:
        return False, f"ai_text_helpers nepieejams: {e}"

    text = _build_text_from_listing(row)
    image_urls = _build_image_urls_for_openai(row)
    if not image_urls:
        return False, "Nav piejamu bilžu (local_image_paths_processed tukšs vai nav RGC_MK_TOKEN)"

    listing_url_for_prompt = f"agent_anketa://listings/{listing_id}"
    try:
        result = helpers.analyze_with_openai(listing_url_for_prompt, text, image_urls)
    except Exception as e:
        return False, f"OpenAI kļūda: {type(e).__name__}: {str(e)[:300]}"

    locked = row.get("agent_locked_fields") or []
    try:
        _update_listing_with_ai(listing_id, locked, result)
    except Exception as e:
        return False, f"DB update kļūda: {type(e).__name__}: {str(e)[:300]}"

    return True, f"AI papildināja, locked fields respektēti ({len(locked)})"


# ---------- Async loop ----------

async def run_loop(stop_event: asyncio.Event) -> None:
    if not AGENT_AI_ENABLED:
        logger.info("Agent AI poller IZSLĒGTS (AGENT_AI_ENABLED=0)")
        return
    if not DATABASE_URL:
        logger.warning("Agent AI poller bez DATABASE_URL — sleeping forever")
        await stop_event.wait()
        return
    if not os.getenv("OPENAI_API_KEY"):
        logger.warning(
            "Agent AI poller: OPENAI_API_KEY trūkst — paliek idle. Iestati env "
            "mainīgo Railway servisā un restartē, lai sāktu AI papildināšanu."
        )
        _state["skipped_no_key"] = 1

    logger.info(
        f"Agent AI poller sākts — interval={AGENT_AI_INTERVAL}s, "
        f"stale_threshold={AGENT_AI_STALE_MIN}min"
    )
    _state["running"] = True
    loop = asyncio.get_event_loop()

    # Recovery uz startup
    try:
        recovered = await loop.run_in_executor(None, _recover_stale)
        if recovered:
            logger.warning(
                f"Agent AI recovery: atjaunoti {recovered} stale 'processing' listingi"
            )
    except Exception as e:
        logger.error(f"Recovery kļūda: {e}", exc_info=True)

    try:
        from datetime import datetime, timezone
        while not stop_event.is_set():
            _state["loop_iterations"] += 1
            _state["last_claim_at"] = datetime.now(timezone.utc).isoformat()
            # Ja nav API key, gaida un atkārto pārbaudi
            if not os.getenv("OPENAI_API_KEY"):
                _state["last_claim_result"] = "skipped: no OPENAI_API_KEY"
                await _sleep_interruptible(stop_event, AGENT_AI_INTERVAL)
                continue

            try:
                row = await loop.run_in_executor(None, _claim_next)
                _state["last_claim_result"] = (
                    f"row_id={row['id']}" if row else "None (no pending rows)"
                )
            except Exception as e:
                _state["last_claim_result"] = f"error: {type(e).__name__}: {str(e)[:200]}"
                logger.error(f"_claim_next: {e}", exc_info=True)
                await _sleep_interruptible(stop_event, AGENT_AI_INTERVAL)
                continue

            if row is None:
                await _sleep_interruptible(stop_event, AGENT_AI_INTERVAL)
                continue

            lid = int(row["id"])
            logger.info(f"Agent AI picked listing#{lid}")
            _state["last_started"] = {"listing_id": lid}

            ok, msg = await loop.run_in_executor(None, _process_one, row)

            if ok:
                _state["processed_total"] += 1
                _state["last_result"] = {"listing_id": lid, "status": "ok", "note": msg}
                logger.info(f"Agent AI listing#{lid} → ok ({msg})")
            else:
                try:
                    await loop.run_in_executor(None, _mark_error, lid, msg)
                except Exception as e:
                    logger.error(f"_mark_error: {e}", exc_info=True)
                _state["errors_total"] += 1
                _state["last_result"] = {"listing_id": lid, "status": "error", "error": msg[:200]}
                logger.warning(f"Agent AI listing#{lid} → error: {msg[:200]}")

            _state["last_finished"] = _state["last_started"]
            await asyncio.sleep(0.5)
    finally:
        _state["running"] = False
        logger.info("Agent AI poller apstājies")


async def _sleep_interruptible(stop_event: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
