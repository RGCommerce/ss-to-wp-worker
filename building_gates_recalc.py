"""building_gates_recalc.py — «Pārrēķināt vārtus šai ēkai» (Prompta aģents, 2026-10-29).

Līgums (Zilā, AI_KLASIFIKATORS_2026-10-28_ZILA.md):
  POST /agent/building-gates-recalc/{building_profile_id}  (X-RGC-Token, body {"requested_by": "<e-pasts>"})
  → 200 {"updated": <telpu skaits>, "republished": <wp rindu skaits>}

BEZ AI — tikai deterministiskie vārti (tie paši, kas test_runner_db / agent_ai_poller PĒC AI):
  Ievas ēku zināšanas (facts: pieejamība, apsardze, konf. zāle) → Medicīna → Ievas med → PVD → žogs.
Ieeja: ēkas profila pogas (owner_adapts_premises, has_accessibility, has_fenced) + cilvēka saraksti
(ievas_ekas.json, iezogotas_ekas.json) + telpas esošie AI lauki (WC, platība, stāvoklis, grīda, sienas,
ventilācija). Pārrēķina VISĀM ēkas telpām (Debug_status='ok', ne zeme/investīcija/daudzēku):
  Potential_space_group (Medicīna/PVD), Vides_pieejamiba_check (+iemesls), Nozogota_teritorija_check,
  Apsargajama_teritorija_check, Has_conference_room (Ievas facts).
Respektē agent_locked_fields (lockētu lauku NEraksta). Mājaslapas telpām ar izmaiņām → wp_export_queue publish.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Any, Dict, List, Tuple

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("building_gates_recalc")

DATABASE_URL = os.getenv("DATABASE_URL", "")
EXCLUDE_GROUPS = ("Zeme", "Investīciju objekts", "Daudzēku īpašums")

# Lauki, ko vārti drīkst mainīt (tikai šos salīdzina un raksta)
GATE_FIELDS = [
    "Potential_space_group",
    "Vides_pieejamiba_check",
    "Vides_pieejamiba_iemesls",
    "Nozogota_teritorija_check",
    "Apsargajama_teritorija_check",
    "Has_conference_room",
]

_SELECT = """
    SELECT l.id, l.on_website, l.agent_locked_fields, l."Debug_note",
           l."Space_group"::text AS "Space_group", l."Potential_space_group",
           l."Vides_pieejamiba_check", l."Vides_pieejamiba_iemesls", l."Nozogota_teritorija_check",
           l."Apsargajama_teritorija_check", l."Has_conference_room",
           l."cik_WC", l."Space_condition"::text AS "Space_condition", l."Gridas_materials"::text AS "Gridas_materials",
           l."Mazgajamas_sienas_check", l."Ventilacijas_sistema_check",
           l.area_m2, l.floor, l.original_text,
           bp.building_type::text AS bp_building_type
    FROM properties.listings l
    LEFT JOIN properties.building_profiles bp ON bp.id = l.building_profile_id
    WHERE l.building_profile_id = %s
      AND l."Debug_status" = 'ok'
      AND coalesce(l."Space_group"::text, '') <> ALL(%s)
    ORDER BY l.id
"""


def _norm(v: Any) -> str:
    return "" if v is None else str(v)


def _pot_set(v: Any) -> set:
    return {p.strip() for p in _norm(v).split(",") if p.strip() and p.strip().lower() != "unknown"}


def _run_gates(row: Dict[str, Any], bp_id: int, bf: Dict[str, Any], helpers) -> Tuple[Dict[str, Any], List[str]]:
    """Uzbūvē `result` no telpas DB laukiem, izlaiž cauri vārtiem. Atgriež (result, iemeslu saraksts)."""
    result: Dict[str, Any] = {
        "Space_group": _norm(row.get("Space_group")),
        "Potential_space_group": _norm(row.get("Potential_space_group")) or "unknown",
        "Vides_pieejamiba_check": _norm(row.get("Vides_pieejamiba_check")) or "unknown",
        "Vides_pieejamiba_iemesls": _norm(row.get("Vides_pieejamiba_iemesls")),
        "Nozogota_teritorija_check": _norm(row.get("Nozogota_teritorija_check")) or "unknown",
        "Apsargajama_teritorija_check": _norm(row.get("Apsargajama_teritorija_check")) or "unknown",
        "Has_conference_room": _norm(row.get("Has_conference_room")) or "unknown",
        "cik_WC": _norm(row.get("cik_WC")),
        "Space_condition": _norm(row.get("Space_condition")),
        "Gridas_materials": _norm(row.get("Gridas_materials")),
        "Mazgajamas_sienas_check": _norm(row.get("Mazgajamas_sienas_check")),
        "Ventilacijas_sistema_check": _norm(row.get("Ventilacijas_sistema_check")),
        "building_type": _norm(row.get("bp_building_type")),
        "area_m2": _norm(row.get("area_m2")),
        "Debug_note": "",  # vārtu piezīmes krājas te atsevišķi
    }
    why: List[str] = []
    r = helpers.apply_ieva_gates(result, bp_id, "facts")
    if r and r != "bez izmaiņām" and r != "nav Ievas datu":
        why.append(f"Ieva: {r}")
    why.append(f"med: {helpers.apply_medicine_gates(result, row.get('area_m2'), bf)}")
    r = helpers.apply_ieva_gates(result, bp_id, "med")
    if r and r not in ("bez izmaiņām", "nav Ievas datu"):
        why.append(f"Ieva: {r}")
    why.append(f"pvd: {helpers.apply_pvd_gates(result, row.get('floor'), bf, row.get('original_text') or '')}")
    why.append(f"žogs: {helpers.apply_fence_gate(result, bp_id, bf)}")
    return result, why


def recalc_building(building_profile_id: int, requested_by: str = "", dry_run: bool = False) -> Dict[str, Any]:
    import ai_text_helpers as helpers  # lazy (OPENAI_API_KEY pie ielādes; AI te NEsauc)

    bp_id = int(building_profile_id)
    stamp = _dt.date.today().isoformat()
    who = (requested_by or "panelis").strip()[:80]
    updated = republished = 0
    details: List[Dict[str, Any]] = []

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM properties.building_profiles WHERE id = %s", (bp_id,))
            if not cur.fetchone():
                raise LookupError(f"Ēkas profils {bp_id} nav atrasts")
        bf = helpers.fetch_building_flags(conn, bp_id)
        with conn.cursor() as cur:
            cur.execute(_SELECT, (bp_id, list(EXCLUDE_GROUPS)))
            rows = cur.fetchall()

        for row in rows:
            locked = set(row.get("agent_locked_fields") or [])
            result, why = _run_gates(row, bp_id, bf, helpers)
            upd: Dict[str, Any] = {}
            diffs: List[Tuple[str, str, str]] = []
            for f in GATE_FIELDS:
                if f in locked:
                    continue
                new = result.get(f)
                if f == "Potential_space_group" and _norm(new).lower() in ("", "unknown"):
                    new = None
                old = row.get(f)
                if _norm(old) == _norm(new):
                    continue
                if f == "Potential_space_group" and _pot_set(old) == _pot_set(new):
                    continue  # tikai secība atšķiras — nav izmaiņa
                upd[f] = new
                diffs.append((f, _norm(old), _norm(new)))
            if not diffs:
                continue
            note = _norm(row.get("Debug_note"))
            gate_note = "; ".join(w for w in why if w)[:200]
            upd["Debug_note"] = f"{note[:260]} | vārti {stamp} ({who}): {gate_note}"[:500]
            details.append({"id": row["id"], "on_website": bool(row.get("on_website")), "diffs": diffs})
            if dry_run:
                updated += 1
                if row.get("on_website"):
                    republished += 1
                continue
            cols = list(upd.keys())
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE properties.listings SET {', '.join(f'\"{c}\" = %s' for c in cols)} WHERE id = %s",
                    list(upd.values()) + [row["id"]],
                )
                updated += 1
                if row.get("on_website"):
                    cur.execute(
                        """INSERT INTO properties.wp_export_queue (listing_id, status, priority, requested_by, action)
                           SELECT %s, 'pending', 0, %s, 'publish'
                           WHERE NOT EXISTS (SELECT 1 FROM properties.wp_export_queue
                                             WHERE listing_id = %s AND status = 'pending' AND action = 'publish')""",
                        (row["id"], f"gates-recalc:{who}"[:100], row["id"]),
                    )
                    republished += cur.rowcount or 0
        if not dry_run:
            conn.commit()

    logger.info("building-gates-recalc bp=%s by=%s: telpas=%d, mainītas=%d, wp=%d%s",
                bp_id, who, len(rows), updated, republished, " (DRY)" if dry_run else "")
    return {"updated": updated, "republished": republished, "listings": len(rows),
            "flags": bf, "details": details}


if __name__ == "__main__":  # lokāls tests: python -X utf8 building_gates_recalc.py <bp_id> [--execute]
    import json
    import sys

    _bp = int(sys.argv[1])
    _dry = "--execute" not in sys.argv
    out = recalc_building(_bp, "lokāls tests", dry_run=_dry)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
