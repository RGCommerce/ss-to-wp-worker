"""agent_publish.py — Anketas-par-eku publicēšanas pipeline (Ceļš B).

Pieņem JSON no /agent/publish endpoint un izpilda:
  1. INSERT building_profiles (vai SELECT esošo)
  2. Bilžu pārkopēšana no /storage/agent_drafts/<id>/ uz
     /storage/listings/<listing_id>/raw/ + ai_ready/
  3. INSERT N listings ar source='agent_anketa_easy'|'_full'
  4. EASY režīms: agent_locked_fields aizpildīts, Debug_status=NULL → AI worker
     paķers (modificētais test_runner_db ar --respect-locked-fields)
     FULL režīms: Debug_status='ok' uzreiz, AI neaiztiek
  5. Pa katru listing → publish_to_wp.publish(listing_id, skip_ai=True)
     (skip_ai=True jo bildes JAU ir mūsu pašu, ne ss.lv → nav Seedream)
  6. Multi-units savienošana (rebuild_multi_units)
  7. Atgriež { ok, wp_post_ids, urls, warnings }
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent))
import publish_to_wp  # publish(listing_id, dry_run, force, skip_ai)

DATABASE_URL = os.getenv("DATABASE_URL")
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(Path(__file__).parent / "storage")))

# Lauki, ko aģents tieši ievada caur anketu — AI worker tos NEDRĪKST pārrakstīt
EASY_LOCKED_FIELDS = ["Space_group", "area_m2", "floor", "Cik_telpas", "cik_WC",
                      "price", "price_type", "Agent_comment"]
FULL_LOCKED_FIELDS = EASY_LOCKED_FIELDS + [
    "Space_condition", "Apkure", "Logu_type", "Gridas_materials",
    "Mebeleta_telpa", "Dalama_telpa", "Griestu_augstums", "electric_power_kw",
    "Gridas_izturiba_kg_m2", "Investiciju_strategija", "Building_description",
    # Visi *_check lauki
    "Sava_ieeja_check", "street_entrance", "Sava_eka_check", "Virtuve_check",
    "Ir_izlietne_telpa_check", "Balkons_check", "Vides_pieejamiba_check",
    "Mazgajamas_sienas_check", "Ventilacijas_sistema_check",
    "Pacelamie_varti_check", "Rampa_logistikai_check", "Auto_pacelajs_check",
    "Treifelis_Pacelajs", "Pacelamie_varti_count", "Rampa_logistikai_count",
]


def _composite_key(street: str, city: str, area_m2: str, space_group: str) -> str:
    """Tas pats composite_key kā ss.lv (mig 016/017). Lai izvairītos no
    duplikātiem ar scraper-iem."""
    parts = [
        (street or "").strip().lower(),
        (city or "").strip().lower(),
        str(area_m2 or "").strip(),
        (space_group or "").strip(),
    ]
    return "|".join(parts)


def _bget(building: dict, *keys):
    """Paskata vairākus key variantus (atļauj abus kapitalizācijas formātus
    no UI vs backend)."""
    for k in keys:
        v = building.get(k)
        if v is not None and v != "":
            return v
    return None


def _get_or_create_bp(conn, building: dict, wp_user_id: int) -> int:
    """Atgriež building_profile_id. Ja existing_building_id (vai existing_bp_id)
    ir norādīts, UPDATE-o tukšos laukus; pretējā gadījumā INSERT jaunu."""
    existing_id = _bget(building, "existing_building_id", "existing_bp_id")
    if existing_id:
        bp_id = int(existing_id)
        # UPDATE tikai tos laukus, ko aģents tagad ievada (un kuri DB-ā ir NULL)
        sets = []
        params = []
        for k in ("city", "district", "building_type", "building_class",
                  "Apkure", "Parkings", "Building_description",
                  "Apsaimniekosanas_maksa", "NIN", "Komunalie",
                  "Zemes_gabals_m2"):
            v = building.get(k)
            if v:
                sets.append(f'"{k}" = COALESCE(NULLIF("{k}", \'\'), %s)')
                params.append(v)
        if sets:
            params.append(bp_id)
            conn.execute(
                f'UPDATE properties.building_profiles SET {", ".join(sets)}, '
                f'updated_at = now() WHERE id = %s',
                tuple(params),
            )
        return bp_id

    # Jauns BP
    street = (building.get("street") or "").strip()
    city = (building.get("city") or "").strip()
    if not street or not city:
        raise ValueError("street + city ir obligāti")
    full_address = f"{street}, {city}".strip(", ")
    building_key = f"agent:{street.lower()}|{city.lower()}|{int(time.time())}"

    cur = conn.execute(
        """
        INSERT INTO properties.building_profiles
            (building_key, street, city, district, full_address,
             building_type, building_class, "Apkure", "Parkings",
             "Building_description", "Apsaimniekosanas_maksa", "NIN",
             "Komunalie", "Zemes_gabals_m2",
             listing_count_total, listing_count_active,
             first_seen_at, last_seen_at, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                0, 0, now(), now(), now(), now())
        RETURNING id
        """,
        (
            building_key, street, city, building.get("district"), full_address,
            building.get("building_type"), building.get("building_class"),
            building.get("Apkure"), building.get("Parkings"),
            building.get("Building_description"),
            building.get("Apsaimniekosanas_maksa"), building.get("NIN"),
            building.get("Komunalie"), building.get("Zemes_gabals_m2"),
        ),
    )
    return cur.fetchone()[0]


# Bildes secības prioritāte (manifest order). Featured bilde TIEK pārvietota uz
# pirmo vietu IEKŠ fasade kategorijas; plans iet pēdējās, jo publish_to_wp.py
# tās novietos atsevišķi (fave_floor_plans) un izņems no galvenās galerijas.
_TYPE_PRIORITY = {"fasade": 0, "interjers": 1, "cits": 2, "plans": 3}


def _normalize_images(raw: list) -> list[dict]:
    """Akceptē abus formātus:
      list[str]           — tikai paths (vecais)
      list[dict]          — ar 'path', 'type' (opcionāls), 'featured' (opcionāls)
    Atgriež normalizētu list[dict].
    """
    out: list[dict] = []
    for it in raw or []:
        if isinstance(it, str):
            out.append({"path": it, "type": None, "featured": False})
        elif isinstance(it, dict) and it.get("path"):
            out.append({
                "path": it["path"],
                "type": it.get("type"),
                "featured": bool(it.get("featured")),
                "enhanced_path": it.get("enhanced_path"),
            })
    return out


def _sort_images_for_publish(images: list[dict]) -> list[dict]:
    """Sakārto bildes WP publicēšanas secībā:
       1) Featured bilde PIRMAJĀ vietā (kļūst img_001, kas publish_to_wp ņem
          kā featured_media)
       2) Citas fasāde
       3) Interjers
       4) Cits / unknown
       5) Plāns (last — publish_to_wp.py to izņem no galerijas un sūta uz
          fave_floor_plans)
    """
    def sort_key(idx_img: tuple[int, dict]) -> tuple[int, int, int]:
        idx, img = idx_img
        t = img.get("type") or "cits"
        prio = _TYPE_PRIORITY.get(t, 2)  # unknown → "cits"
        feat = 0 if img.get("featured") else 1
        # Featured kandidāts paliek savā type prio, bet feat=0 to atstāj
        # priekšā tās type grupā. Stable sort glabā oriģ. secību iekš grupas.
        return (prio, feat, idx)

    return [img for _idx, img in sorted(
        enumerate(images), key=sort_key
    )]


def _copy_images(draft_images: list, listing_id: int) -> dict[str, str]:
    """Pārkopē bildes no /storage/agent_drafts/<draft>/<target>/<file>
    uz /storage/listings/<listing_id>/{raw,ai_ready}/. Atgriež
    {filename_in_dst: type} priekš manifest.
    """
    images = _normalize_images(draft_images)
    if not images:
        return {}
    # Aģenta atzīmētā secība — featured first, fasade, interjers, cits, plans
    ordered = _sort_images_for_publish(images)

    dst_raw = STORAGE_ROOT / "listings" / str(listing_id) / "raw"
    dst_ai = STORAGE_ROOT / "listings" / str(listing_id) / "ai_ready"
    dst_raw.mkdir(parents=True, exist_ok=True)
    dst_ai.mkdir(parents=True, exist_ok=True)

    name_to_type: dict[str, str] = {}
    copied_idx = 0
    for img in ordered:
        rel_path = img.get("enhanced_path") or img["path"]
        src = STORAGE_ROOT / rel_path
        if not src.is_file():
            print(f"  ⚠ Bilde nav atrasta: {rel_path}")
            continue
        copied_idx += 1
        ext = src.suffix.lower() or ".jpg"
        name = f"img_{copied_idx:03d}{ext}"
        shutil.copy2(src, dst_raw / name)
        # ai_ready — tā pati bilde (aģenta bildes NEIET caur Seedream)
        shutil.copy2(src, dst_ai / name)
        # Manifest tips — aģenta atzīmējums vai None → vēlāk default
        agent_type = img.get("type")
        if agent_type in ("fasade", "interjers", "cits", "plans"):
            name_to_type[name] = agent_type
    return name_to_type


def _insert_listing(conn, bp_id: int, unit: dict, building: dict,
                    mode: str, wp_user_id: int) -> int:
    """INSERT listings rinda, atgriež jauno ID."""
    locked = EASY_LOCKED_FIELDS if mode == "easy" else FULL_LOCKED_FIELDS
    source = f"agent_anketa_{mode}"
    # FULL mode: Debug_status='ok' uzreiz (AI worker neaiztiks)
    # EASY mode: NULL → AI worker paķers + papildinās
    debug_status = "ok" if mode == "full" else None

    # NB: listings tabula NEsatur composite_key (mig 016/017 attiecas uz
    # building_profiles.building_key). Aģenta INSERT-iem vienkārši lietojam
    # ID kā unikālais; duplikātu kontrole notiek manuāli pa adresi.

    # Helper — paskata abus kapitalizācijas variantus (lielo un mazo).
    # UI šobrīd sūta mazos (space_group), bet vēsturiski lieli (Space_group).
    def uget(*keys):
        for k in keys:
            v = unit.get(k)
            if v is not None and v != "":
                return v
        return None

    # Galvenie lauki
    cols = {
        "building_profile_id": bp_id,
        "street": building.get("street"),
        "city": building.get("city"),
        "district": building.get("district"),
        "source": source,
        "agent_user_id": wp_user_id,
        "agent_locked_fields": locked,
        "Debug_status": debug_status,
        # Aģenta input — pieņem abus kapitalizācijas variantus
        "Space_group": uget("Space_group", "space_group"),
        "area_m2": uget("area_m2"),
        "floor": uget("floor"),
        "Cik_telpas": uget("Cik_telpas", "cik_telpas"),
        "cik_WC": uget("cik_WC", "cik_wc"),
        "price": uget("price"),
        "price_type": uget("price_type"),
        "Agent_comment": uget("Agent_comment", "agent_comment"),
    }

    # FULL režīma papildlauki — visi pieņem abus kapitalizācijas variantus
    if mode == "full":
        full_field_pairs = [
            ("Space_condition", "space_condition", "Space_condition"),
            ("Apkure", "apkure", "Apkure"),
            ("Logu_type", "logu_type", "Logu_type"),
            ("Gridas_materials", "gridas_materials", "Gridas_materials"),
            ("Mebeleta_telpa", "mebeleta_telpa", "Mebeleta_telpa"),
            ("Dalama_telpa", "dalama_telpa", "Dalama_telpa"),
            ("Griestu_augstums", "griestu_augstums", "Griestu_augstums"),
            ("electric_power_kw", "electric_power_kw", "electric_power_kw"),
            ("Gridas_izturiba_kg_m2", "gridas_izturiba_kg_m2", "Gridas_izturiba_kg_m2"),
            ("Investiciju_strategija", "investiciju_strategija", "Investiciju_strategija"),
            ("Pacelamie_varti_count", "pacelamie_varti_count", "Pacelamie_varti_count"),
            ("Rampa_logistikai_count", "rampa_logistikai_count", "Rampa_logistikai_count"),
            ("Parkings", "parkings", "Parkings"),
            ("Zemes_gabals_m2", "zemes_gabals_m2", "Zemes_gabals_m2"),
        ]
        for big_key, small_key, db_col in full_field_pairs:
            v = uget(big_key, small_key)
            if v:
                cols[db_col] = v

        # Boolean check lauki — UI tos sūta tieši kā augšējos lauks ar nosaukumiem
        # piem. unit.Pacelamie_varti_check = "checked"/"not checked"/null
        check_fields = [
            "Pacelamie_varti_check", "Rampa_logistikai_check", "Virtuve_check",
            "Sava_ieeja_check", "street_entrance",
            "Apsargajama_teritorija_check", "Nozogota_teritorija_check",
            "Auto_pacelajs_check", "Treifelis_Pacelajs",
            "Ir_izlietne_telpa_check", "Balkons_check", "Sava_eka_check",
        ]
        for cf in check_fields:
            v = uget(cf)
            if v:
                cols[cf] = v

        # Vēsturiskais "checks" dict — ja UI to sūta atsevišķi
        for k, v in (unit.get("checks") or {}).items():
            if v:
                cols[k] = v

        # WC location no UI
        wc_loc = uget("WC_location")
        if wc_loc:
            cols["WC_location"] = wc_loc

        # building_class/building_type uz listing (mig 014+ — listings = primary)
        if building.get("building_class"):
            cols["building_class"] = building["building_class"]
        if building.get("building_type"):
            cols["building_type"] = building["building_type"]

    # INSERT
    col_list = ", ".join(f'"{k}"' for k in cols)
    val_list = ", ".join(["%s"] * len(cols))
    sql = (f"INSERT INTO properties.listings ({col_list}) "
           f"VALUES ({val_list}) RETURNING id")
    cur = conn.execute(sql, tuple(cols.values()))
    listing_id = cur.fetchone()[0]
    return listing_id


def _write_image_manifest(listing_id: int, agent_types: dict[str, str]) -> None:
    """Pieraksta `_image_manifest.json` priekš publish_to_wp.py.

    Aģents atzīmēja katrai bildei tipu (fasade/interjers/plans/cits) — tas iet
    pa virsu. Neatzīmētajām bildēm uzliek default — pirmā = fasade, pārējās =
    interjers (lai publish_to_wp pareizi sakārto galeriju un featured_media).
    """
    ai_dir = STORAGE_ROOT / "listings" / str(listing_id) / "ai_ready"
    if not ai_dir.is_dir():
        return
    images = sorted(ai_dir.glob("img_*.jpg")) + sorted(ai_dir.glob("img_*.png"))
    if not images:
        return
    manifest = {}
    for i, p in enumerate(images):
        # Aģenta atzīmējums override default
        agent_t = agent_types.get(p.name)
        if agent_t:
            manifest[p.name] = {"type": agent_t, "quality": "good"}
        else:
            manifest[p.name] = {
                "type": "fasade" if i == 0 else "interjers",
                "quality": "good",
            }
    manifest_path = ai_dir.parent / "_image_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _ensure_classify_manifest(listing_id: int) -> None:
    """Backward compat — saglabāju veco nosaukumu, bet bez aģenta tipiem.
    Lieto, ja `_copy_images` neatgrieza nekādu mapping (tas neturētu notikt
    jaunajā plūsmā, kad agent_publish vienmēr pārsūta caur jauno path)."""
    _write_image_manifest(listing_id, {})


def publish_anketa(payload: dict) -> dict:
    """Galvenais entry-point — queue-based plūsma (NE sync publish_to_wp).

    Plūsma:
      1. INSERT/UPDATE building_profiles → bp_id
      2. INSERT properties.listings (pa unit) ar:
           EASY: Debug_status=NULL → trešais AI worker paķer un papildina
           FULL: Debug_status='ok' uzreiz → wp_export_queue poller paķer
      3. Pārkopē bildes uz /storage/listings/<id>/{raw,ai_ready}/
      4. INSERT wp_export_queue rindu (status='pending')
      5. UI uzreiz dabū "Pievienots rindai" — gaidīt 5-15 min nav vajadzīgs

    queue_poller (ss-to-wp-worker) paskata Debug_status='ok' un publicē
    publish_to_wp.publish() asinhroni, kad AI gatavs (EASY) vai uzreiz (FULL).
    """
    mode = payload["mode"]
    wp_user_id = int(payload["wp_user_id"])
    building = payload["building"]
    units = payload["units"]
    requested_by_email = payload.get("requested_by_email")

    log: list[str] = []
    warnings: list[str] = []
    queued: list[dict] = []

    with psycopg.connect(DATABASE_URL) as conn:
        # Step 1: BP
        bp_id = _get_or_create_bp(conn, building, wp_user_id)
        log.append(f"✓ building_profile_id = {bp_id}")

        # Step 2-3: pa katru telpu — INSERT + bildes
        listing_ids = []
        for i, unit in enumerate(units, start=1):
            listing_id = _insert_listing(conn, bp_id, unit, building, mode, wp_user_id)
            log.append(f"✓ listing #{i} → id={listing_id}")

            # Bildes: ēkas kopīgās + telpas (katra ar type un featured no aģenta)
            all_imgs = (building.get("images") or []) + (unit.get("images") or [])
            name_to_type = _copy_images(all_imgs, listing_id)
            log.append(f"  → {len(name_to_type) or len(all_imgs)} bildes pārkopētas")

            # Manifest priekš publish_to_wp — ar aģenta atzīmējumiem
            try:
                _write_image_manifest(listing_id, name_to_type)
            except Exception as e:
                warnings.append(f"manifest neizdevās listing {listing_id}: {e}")

            # Lokālos bilžu ceļus saglabā DB (Mig 019 lauki)
            ai_dir = STORAGE_ROOT / "listings" / str(listing_id) / "ai_ready"
            ai_paths = sorted(str(p) for p in ai_dir.glob("img_*.*"))
            if ai_paths:
                conn.execute(
                    """UPDATE properties.listings
                          SET local_image_paths_processed = %s
                        WHERE id = %s""",
                    (ai_paths, listing_id),
                )
            listing_ids.append(listing_id)

        # Step 4: Debug_status='ok' priekš QUEUE POLLER GATE
        # FULL: skaidrs, AI nav vajadzīgs — uzliek uzreiz.
        # EASY: atstāj NULL — agent_ai_poller paķers listings, palaiž OpenAI
        # Vision (teksts + bildes), papildina laukus respektējot
        # agent_locked_fields, un beigās pats uzliek Debug_status='ok'.
        # Tad queue_poller paķer rindu un publicē uz WP.
        if mode == "full":
            conn.execute(
                """UPDATE properties.listings SET "Debug_status" = 'ok'
                    WHERE id = ANY(%s)""",
                (listing_ids,),
            )

        # Step 5: katram listing → ievieto wp_export_queue rindu
        # queue_poller paskata Debug_status='ok' un palaiž publish_to_wp asinhroni.
        # EASY gadījumā poller atliks (rinda paliek pending), kamēr AI worker
        # uzliek Debug_status='ok'.
        for lid in listing_ids:
            cur = conn.execute(
                """INSERT INTO properties.wp_export_queue
                       (listing_id, status, requested_by)
                   VALUES (%s, 'pending', %s)
                   ON CONFLICT (listing_id) WHERE status IN ('pending','processing')
                   DO NOTHING
                   RETURNING id""",
                (lid, requested_by_email),
            )
            row = cur.fetchone()
            queue_id = row[0] if row else None
            queued.append({
                "listing_id": lid,
                "queue_id": queue_id,
                "mode": mode,
                # EASY: gaidīs uz AI; FULL: tūlīt processing
                "needs_ai": mode == "easy",
            })

        conn.commit()

    if mode == "easy":
        warnings.append(
            "EASY režīmā — AI worker (3. plūsma) papildinās laukus un uzliks "
            "Debug_status='ok'. Kad gatavs, wp_export_queue poller publicēs uz WP. "
            "Statusu var sekot /publish lapā."
        )

    return {
        "ok": True,
        "mode": mode,
        "building_profile_id": bp_id,
        "queued": queued,
        "log": log,
        "warnings": warnings,
    }
