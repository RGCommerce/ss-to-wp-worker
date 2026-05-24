"""agent_api.py — FastAPI router /anketa-par-eku plūsmai (Ceļš B).

Pieslēgts main.py-am caur include_router. Visi POST aiz `X-RGC-Token` headera
(tā pati shared-secret, kas /publish endpoint-am).

Endpoints:
  GET  /agent/autocomplete?q=...        — building_profiles ILIKE meklē (max 8)
  GET  /agent/autoload/{bp_id}          — pilnais BP + esošie listings
  POST /agent/draft/save                — anketas state autosave (DB)
  GET  /agent/draft/{user_id}/{name}    — load draft
  DELETE /agent/draft/{id}              — dzēš draft (pēc publish vai manuāli)
  POST /agent/image-upload              — bilžu multipart augšuplāde uz /storage
  POST /agent/image-enhance             — selektīvi gpt-image-1 vienai bildei
  POST /agent/publish                   — galvenais: BP + N listings + WP

Skrīpts izsauc esošos worker moduļus:
  agent_publish.publish_anketa(...) — orchestration
  publish_to_wp.py — WP property create
  image_enhance_openai.py — selektīva bilžu uzlabošana

DB: properties.building_profiles, properties.listings, properties.agent_drafts
(mig 025).
"""
from __future__ import annotations

import json
import os
import sys
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Any, Optional

import psycopg
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent))
import agent_publish  # noqa: E402
import image_enhance_openai  # noqa: E402

DATABASE_URL = os.getenv("DATABASE_URL")
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(Path(__file__).parent / "storage")))
RGC_MK_TOKEN = os.getenv("RGC_MK_TOKEN")

router = APIRouter(prefix="/agent", tags=["agent-anketa"])


# ---------------------------------------------------------------------------
# Auth (sama X-RGC-Token kā main.py)
# ---------------------------------------------------------------------------

def require_token(
    x_rgc_token: Annotated[Optional[str], Header(alias="X-RGC-Token")] = None,
) -> None:
    if not RGC_MK_TOKEN:
        raise HTTPException(500, "Service nav konfigurēts (RGC_MK_TOKEN)")
    if not x_rgc_token or x_rgc_token != RGC_MK_TOKEN:
        raise HTTPException(403, "Trūkst derīga X-RGC-Token header")


def _db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


# ---------------------------------------------------------------------------
# 1) AUTOCOMPLETE — building_profiles meklēšana pa adresei
# ---------------------------------------------------------------------------

@router.get("/autocomplete")
def autocomplete(
    q: str,
    _auth: None = Depends(require_token),
) -> list[dict]:
    """ILIKE meklē building_profiles.full_address. Atgriež max 8 rezultātus
    kā mini-cards anketas dropdown-am."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    sql = """
        SELECT id, full_address, city, district, building_type, building_class,
               listing_count_active
          FROM properties.building_profiles
         WHERE full_address ILIKE '%%' || %s || '%%'
            OR street ILIKE '%%' || %s || '%%'
         ORDER BY listing_count_active DESC NULLS LAST, full_address
         LIMIT 8
    """
    with _db() as conn, conn.cursor() as cur:
        cur.execute(sql, (q, q))
        return cur.fetchall()


# ---------------------------------------------------------------------------
# 2) AUTOLOAD — pilna BP info + esošie listings
# ---------------------------------------------------------------------------

@router.get("/autoload/{bp_id}")
def autoload(bp_id: int, _auth: None = Depends(require_token)) -> dict:
    """Atgriež pilnu building_profile + esošo listings sarakstu, lai anketa
    var aizpildīt laukus + parādīt 'šajā ēkā jau ir N sludinājumi'."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM properties.building_profiles WHERE id = %s",
            (bp_id,),
        )
        bp = cur.fetchone()
        if not bp:
            raise HTTPException(404, f"Building profile {bp_id} nav atrasts")

        cur.execute(
            """
            SELECT id, "Space_group"::text, area_m2, floor, price, price_type,
                   building_class::text, "Space_condition"::text, wp_post_id
              FROM properties.listings
             WHERE building_profile_id = %s
             ORDER BY id
            """,
            (bp_id,),
        )
        listings = cur.fetchall()

    return {"building": bp, "listings": listings}


# ---------------------------------------------------------------------------
# 3) DRAFT SAVE / LOAD / DELETE — autosave priekš anketas state-a
# ---------------------------------------------------------------------------

class DraftSaveReq(BaseModel):
    wp_user_id: int
    draft_name: str = Field(min_length=1, max_length=120)
    data: dict[str, Any]


@router.post("/draft/save")
def draft_save(req: DraftSaveReq, _auth: None = Depends(require_token)) -> dict:
    """UPSERT (wp_user_id, draft_name) pa pāri. Klients autosave každu 10s."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO properties.agent_drafts (wp_user_id, draft_name, data)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (wp_user_id, draft_name) DO UPDATE
              SET data = EXCLUDED.data, updated_at = now()
            RETURNING id, updated_at
            """,
            (req.wp_user_id, req.draft_name, json.dumps(req.data, ensure_ascii=False)),
        )
        row = cur.fetchone()
        conn.commit()
    return {"id": row["id"], "updated_at": row["updated_at"].isoformat()}


@router.get("/draft/{wp_user_id}/{draft_name}")
def draft_load(
    wp_user_id: int, draft_name: str,
    _auth: None = Depends(require_token),
) -> dict:
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, data, updated_at
              FROM properties.agent_drafts
             WHERE wp_user_id = %s AND draft_name = %s
            """,
            (wp_user_id, draft_name),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Draft nav atrasts")
    return {
        "id": row["id"],
        "data": row["data"],
        "updated_at": row["updated_at"].isoformat(),
    }


@router.get("/drafts/{wp_user_id}")
def drafts_list(wp_user_id: int, _auth: None = Depends(require_token)) -> list[dict]:
    """Aģenta visi drafts (visnesenākie augšā)."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, draft_name, updated_at
              FROM properties.agent_drafts
             WHERE wp_user_id = %s
             ORDER BY updated_at DESC
             LIMIT 50
            """,
            (wp_user_id,),
        )
        rows = cur.fetchall()
    return [
        {"id": r["id"], "name": r["draft_name"], "updated_at": r["updated_at"].isoformat()}
        for r in rows
    ]


@router.delete("/draft/{draft_id}")
def draft_delete(draft_id: int, _auth: None = Depends(require_token)) -> dict:
    with _db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM properties.agent_drafts WHERE id = %s", (draft_id,))
        conn.commit()
    return {"deleted": cur.rowcount > 0}


# ---------------------------------------------------------------------------
# 4) IMAGE UPLOAD — multipart files uz /storage staging area
# ---------------------------------------------------------------------------

@router.post("/image-upload")
def image_upload(
    file: UploadFile = File(...),
    draft_id: int = Form(...),
    target: str = Form(...),  # 'building' | 'unit_X' (X = unit index)
    _auth: None = Depends(require_token),
) -> dict:
    """Pieņem 1 bildi un saglabā uz /storage/agent_drafts/<draft_id>/<target>/.
    Atgriež path, ko frontend saglabā draft state-ā. Pārkopēšana uz pareizo
    listings/<id>/raw/ notiek POST /agent/publish laikā."""
    if not file.filename:
        raise HTTPException(400, "Filename trūkst")

    safe_target = target.replace("/", "_").replace("\\", "_")[:32]
    base = STORAGE_ROOT / "agent_drafts" / str(draft_id) / safe_target
    base.mkdir(parents=True, exist_ok=True)

    # Stabils faila vārds — UUID + paplašinājums
    ext = Path(file.filename).suffix.lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, f"Nepieņemams paplašinājums: {ext}")
    out_path = base / f"{uuid.uuid4().hex}{ext}"

    with open(out_path, "wb") as out:
        shutil.copyfileobj(file.file, out)

    return {
        "path": str(out_path.relative_to(STORAGE_ROOT)),
        "size": out_path.stat().st_size,
    }


# ---------------------------------------------------------------------------
# 5) IMAGE ENHANCE — selektīva gpt-image-1 vienai bildei
# ---------------------------------------------------------------------------

class EnhanceOneReq(BaseModel):
    image_path: str  # /storage relatīvais ceļš no /agent/image-upload
    quality: str = "medium"  # low | medium | high


@router.post("/image-enhance")
def image_enhance_one(req: EnhanceOneReq, _auth: None = Depends(require_token)) -> dict:
    """Izsauc image_enhance_openai pa vienu bildi un atgriež enhanced path.
    Frontend aizvieto src ar šo + uzliek enhanced=True flag."""
    src_path = STORAGE_ROOT / req.image_path
    if not src_path.is_file():
        raise HTTPException(404, f"Bilde nav atrasta: {req.image_path}")

    # Enhance result iet blakus oriģinālam ar _enhanced.png sufiksu
    out_path = src_path.with_name(src_path.stem + "_enhanced.png")
    try:
        image_enhance_openai.enhance_image(
            src_path=src_path, dst_path=out_path, quality=req.quality,
        )
    except Exception as e:
        raise HTTPException(500, f"AI uzlabošana neizdevās: {e}")

    return {
        "enhanced_path": str(out_path.relative_to(STORAGE_ROOT)),
        "size": out_path.stat().st_size,
    }


# ---------------------------------------------------------------------------
# 6) PUBLISH — galvenais endpoint, dabū anketas JSON un publicē uz WP
# ---------------------------------------------------------------------------

class PublishUnitReq(BaseModel):
    Space_group: str
    area_m2: str
    floor: Optional[str] = None
    Cik_telpas: Optional[str] = None
    cik_WC: Optional[str] = None
    price: str
    price_type: str  # 'monthly' | 'regular'
    Agent_comment: Optional[str] = None
    images: list[str] = Field(default_factory=list)  # /storage relatīvie ceļi
    plans: list[str] = Field(default_factory=list)
    # Pilnā režīmā ievadītie lauki
    Space_condition: Optional[str] = None
    Apkure: Optional[str] = None
    Logu_type: Optional[str] = None
    Gridas_materials: Optional[str] = None
    Mebeleta_telpa: Optional[str] = None
    Dalama_telpa: Optional[str] = None
    Griestu_augstums: Optional[str] = None
    electric_power_kw: Optional[str] = None
    Gridas_izturiba_kg_m2: Optional[str] = None
    Investiciju_strategija: Optional[str] = None
    # Check fields (pilnā režīmā)
    checks: dict[str, str] = Field(default_factory=dict)
    # Skaitiķi
    Pacelamie_varti_count: Optional[str] = None
    Rampa_logistikai_count: Optional[str] = None


class PublishBuildingReq(BaseModel):
    existing_building_id: Optional[int] = None  # ja autocomplete izvēlējās esošu
    street: str
    city: str
    district: Optional[str] = None
    building_type: Optional[str] = None
    building_class: Optional[str] = None
    has_conference_room: Optional[str] = None  # 'checked' | 'not checked'
    images: list[str] = Field(default_factory=list)
    # Pilnā režīmā
    Building_description: Optional[str] = None
    Apkure: Optional[str] = None
    Parkings: Optional[str] = None
    Apsaimniekosanas_maksa: Optional[str] = None
    NIN: Optional[str] = None
    Komunalie: Optional[str] = None
    Zemes_gabals_m2: Optional[str] = None


class PublishReq(BaseModel):
    mode: str  # 'easy' | 'full'
    wp_user_id: int
    draft_id: Optional[int] = None  # ja autosaved
    building: PublishBuildingReq
    units: list[PublishUnitReq]


# ---------------------------------------------------------------------------
# 7) LISTING IMAGES — apskatīt esoša listing-a ai_ready bildes
# ---------------------------------------------------------------------------

@router.get("/listing-images/{listing_id}")
def listing_images(listing_id: int, _auth: None = Depends(require_token)) -> dict:
    """Atgriež listing-a /storage/listings/<id>/ai_ready/ bildes + manifest tipus."""
    ai_dir = STORAGE_ROOT / "listings" / str(listing_id) / "ai_ready"
    if not ai_dir.is_dir():
        return {"images": [], "note": f"Nav mapes /storage/listings/{listing_id}/ai_ready/"}

    manifest_path = ai_dir.parent / "_image_manifest.json"
    manifest = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    files = sorted(list(ai_dir.glob("img_*.jpg"))
                   + list(ai_dir.glob("img_*.png"))
                   + list(ai_dir.glob("img_*.webp")))
    images = []
    for f in files:
        meta = manifest.get(f.name) or {}
        images.append({
            "name": f.name,
            "type": meta.get("type", "interjers"),
            "quality": meta.get("quality", "?"),
            "url": f"/agent/image-proxy/{listing_id}/{f.name}",
        })
    return {"images": images}


@router.get("/image-proxy/{listing_id}/{filename}")
def image_proxy(
    listing_id: int, filename: str,
    token: Optional[str] = None,
    x_rgc_token: Annotated[Optional[str], Header(alias="X-RGC-Token")] = None,
):
    """Atgriež bildes baitus no /storage/listings/<id>/ai_ready/.
    Auth caur X-RGC-Token header VAI ?token=... query param (lai
    <img src=...> bez JS headerinjekcijas strādātu)."""
    from fastapi.responses import FileResponse
    if not RGC_MK_TOKEN:
        raise HTTPException(500, "Service nav konfigurēts")
    if x_rgc_token != RGC_MK_TOKEN and token != RGC_MK_TOKEN:
        raise HTTPException(403, "Trūkst tokena")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Nepareizs filename")
    path = STORAGE_ROOT / "listings" / str(listing_id) / "ai_ready" / filename
    if not path.is_file():
        raise HTTPException(404, "Bilde nav atrasta")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# 8) REPUBLISH — esoša listing-a (ne agent_anketa) publicēšana uz WP
# ---------------------------------------------------------------------------

@router.post("/republish/{listing_id}")
def republish(listing_id: int, _auth: None = Depends(require_token)) -> dict:
    """Izsauc publish_to_wp.publish() priekš jau eksistējoša listing-a (kas
    DB-ā ir, bet wp_post_id=NULL). Lieto, kad aģents anketā autocomplete
    ielādē esošu building_profile ar sslv-listings un grib tos arī uzlikt
    uz WP bez datu pārievades."""
    import publish_to_wp
    try:
        publish_to_wp.publish(listing_id, dry_run=False, force=False, skip_ai=False)
    except SystemExit as e:
        return {"wp_post_id": None, "warning": str(e)[:300]}
    except Exception as e:
        return {"wp_post_id": None, "error": f"{type(e).__name__}: {str(e)[:300]}"}

    # Izlasām wp_post_id atpakaļ
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT wp_post_id FROM properties.listings WHERE id = %s", (listing_id,))
        row = cur.fetchone()
        wp_post_id = row["wp_post_id"] if row else None
    return {
        "wp_post_id": wp_post_id,
        "url": (f"https://rgcommerce.lv/?p={wp_post_id}" if wp_post_id else None),
    }


@router.post("/publish")
def publish_anketa(req: PublishReq, _auth: None = Depends(require_token)) -> dict:
    """Galvenais endpoint:
      1. INSERT/SELECT building_profile
      2. Pārkopē bildes no /agent_drafts/ uz /listings/<id>/raw/+ai_ready/
      3. INSERT N listings ar source='agent_anketa_easy'|'_full'
      4. EASY: gaida AI worker (vai sinhronoi izsauc test_runner_db); FULL: Debug_status='ok'
      5. Pa katru listing → publish_to_wp.publish_listing()
      6. Multi-units savienošana
      7. Atgriež { wp_post_ids, urls, warnings }
    """
    if req.mode not in {"easy", "full"}:
        raise HTTPException(400, f"Nezināms mode: {req.mode}")
    if not req.units:
        raise HTTPException(400, "Nav neviena telpas ieraksta")
    return agent_publish.publish_anketa(req.dict())
