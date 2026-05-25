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


def _get_or_create_bp(conn, building: dict, wp_user_id: int) -> int:
    """Atgriež building_profile_id. Ja existing_building_id ir norādīts,
    UPDATE-o tukšos laukus; pretējā gadījumā INSERT jaunu."""
    if building.get("existing_building_id"):
        bp_id = int(building["existing_building_id"])
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
        # Aģenta input
        "Space_group": unit.get("Space_group"),
        "area_m2": unit.get("area_m2"),
        "floor": unit.get("floor"),
        "Cik_telpas": unit.get("Cik_telpas"),
        "cik_WC": unit.get("cik_WC"),
        "price": unit.get("price"),
        "price_type": unit.get("price_type"),
        "Agent_comment": unit.get("Agent_comment"),
    }

    # FULL režīma papildlauki
    if mode == "full":
        for k in ("Space_condition", "Apkure", "Logu_type", "Gridas_materials",
                  "Mebeleta_telpa", "Dalama_telpa", "Griestu_augstums",
                  "electric_power_kw", "Gridas_izturiba_kg_m2",
                  "Investiciju_strategija", "Pacelamie_varti_count",
                  "Rampa_logistikai_count"):
            v = unit.get(k)
            if v:
                cols[k] = v
        # Check fields
        for k, v in (unit.get("checks") or {}).items():
            if v:
                cols[k] = v
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
    """Galvenais entry-point — pa soļiem izpilda visu plūsmu."""
    mode = payload["mode"]
    wp_user_id = int(payload["wp_user_id"])
    building = payload["building"]
    units = payload["units"]

    log: list[str] = []
    warnings: list[str] = []
    results: list[dict] = []

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
            log.append(f"  → {len(name_to_type) or len(all_imgs)} bildes pārkopētas uz /storage")

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

        conn.commit()

    # Step 4: EASY režīmā — agent_locked_fields aizpildīts, AI worker jāpaķer.
    # Šobrīd test_runner_db modifikācija (--respect-locked-fields) vēl nav
    # gatava, tāpēc EASY režīmā Debug_status uzliek 'ok' manuāli un publicē
    # bez AI papildinājuma. (TODO: AI mod nākamajā iterācijā.)
    if mode == "easy":
        warnings.append(
            "EASY režīmā AI papildinājums (Building_description, Agent_comment, "
            "Potential_space_group) vēl nav implementēts — sludinājums tiek "
            "publicēts ar to, kas anketā ievadīts. AI worker modifikācija "
            "nāks nākamajā iterācijā."
        )
        # Provizoriski uzlieku Debug_status='ok' lai publish_to_wp nestrādātu
        # ar NULL listings
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                """UPDATE properties.listings SET "Debug_status" = 'ok'
                    WHERE id = ANY(%s)""",
                (listing_ids,),
            )
            conn.commit()

    # Step 5: pa katru listing → publish_to_wp.publish()
    for lid in listing_ids:
        # Pārbauda vai šim listingam ir bildes (bez tām publish_to_wp crash)
        ai_dir = STORAGE_ROOT / "listings" / str(lid) / "ai_ready"
        n_imgs = (len(list(ai_dir.glob("img_*.jpg")))
                  + len(list(ai_dir.glob("img_*.png")))
                  + len(list(ai_dir.glob("img_*.webp"))))
        if n_imgs == 0:
            warnings.append(
                f"Listing {lid}: nav bilžu /storage/listings/{lid}/ai_ready/ — "
                f"WP publicēšana izlaista (raw ss.lv bildes uz WP aizliegtas)"
            )
            continue
        try:
            publish_to_wp.publish(lid, dry_run=False, force=False, skip_ai=True)
            log.append(f"✓ publish_to_wp({lid}) — {n_imgs} bildes augšuplādētas")
        except SystemExit as e:
            warnings.append(f"publish_to_wp({lid}) atcelts: {str(e)[:200]}")
        except Exception as e:
            warnings.append(f"publish_to_wp({lid}) neizdevās: {type(e).__name__}: {str(e)[:200]}")

    # Step 6: izlasām wp_post_id atpakaļ
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        cur = conn.execute(
            "SELECT id, wp_post_id FROM properties.listings WHERE id = ANY(%s)",
            (listing_ids,),
        )
        for row in cur.fetchall():
            results.append({
                "listing_id": row["id"],
                "wp_post_id": row["wp_post_id"],
                "url": (f"https://rgcommerce.lv/?p={row['wp_post_id']}"
                        if row["wp_post_id"] else None),
            })

    return {
        "ok": True,
        "mode": mode,
        "building_profile_id": bp_id,
        "results": results,
        "log": log,
        "warnings": warnings,
    }
