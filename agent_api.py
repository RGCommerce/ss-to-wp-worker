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
import image_classify  # noqa: E402  (manifesta I/O — plāns/fasāde)
import watermark_check  # noqa: E402  (ss.com pārbaude pēc in-place enhance)

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
    """Tipa-neatkarīga adreses meklēšana building_profiles. "Cēsu 31" = "Cēsu iela 31";
    "Brīvības" atrod arī gatve/bulvāris. Atgriež max 8 mini-cards anketas dropdown-am."""
    import re as _re
    q = (q or "").strip()
    if len(q) < 2:
        return []
    # Normalizē meklēšanas terminu tāpat kā DB izteiksme (tipa vārds + diakritika nost)
    _LV = str.maketrans("āčēģīķļņōŗšūž", "acegiklnorsuz")
    _TYPE = _re.compile(r"\b(iela|gatve|bulvaris|prospekts|laukums|dambis|cels|aleja|soseja|linija|krastmala|tilts|pasaza)\b")
    key = _re.sub(r"\s+", " ", _TYPE.sub(" ", q.split(",")[0].lower().translate(_LV))).strip()
    # SQL norm izteiksme (= listings.street_search ģenerētā kolona)
    norm = (r"btrim(regexp_replace(regexp_replace(translate(lower(split_part(%s,',',1)),"
            r"'āčēģīķļņōŗšūž','acegiklnorsuz'),"
            r"'\y(iela|gatve|bulvaris|prospekts|laukums|dambis|cels|aleja|soseja|linija|krastmala|tilts|pasaza)\y',' ','g'),'\s+',' ','g'))")
    cols = "id, full_address, city, district, building_type, building_class, listing_count_active"
    order = "ORDER BY listing_count_active DESC NULLS LAST, full_address LIMIT 8"
    with _db() as conn, conn.cursor() as cur:
        if key:
            sql = (f"SELECT {cols} FROM properties.building_profiles "
                   f"WHERE {norm % 'full_address'} LIKE '%%'||%s||'%%' "
                   f"   OR {norm % 'street'} LIKE '%%'||%s||'%%' "
                   f"   OR full_address ILIKE '%%'||%s||'%%' {order}")
            cur.execute(sql, (key, key, q))
        else:
            sql = (f"SELECT {cols} FROM properties.building_profiles "
                   f"WHERE full_address ILIKE '%%'||%s||'%%' OR street ILIKE '%%'||%s||'%%' {order}")
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
                   building_class::text, "Space_condition"::text, wp_post_id,
                   source
              FROM properties.listings
             WHERE building_profile_id = %s
             ORDER BY id
            """,
            (bp_id,),
        )
        listings = cur.fetchall()

    return {"building": bp, "listings": listings}


# ---------------------------------------------------------------------------
# 2.5) TEKSTA BŪVĒTĀJS — sludinājuma teksts sadalīts pa teikumiem + dzīvs preview
# ---------------------------------------------------------------------------

class BodySegmentsReq(BaseModel):
    # Draft papildinājumi (aģents vēl nav saglabājis) — preview lieto tos, DB neaiztiek.
    segments: Optional[list[dict]] = None


@router.post("/listing-body-segments/{listing_id}")
def listing_body_segments(
    listing_id: int,
    req: Optional[BodySegmentsReq] = None,
    _auth: None = Depends(require_token),
) -> dict:
    """Teksta būvētājam: atgriež ģenerētā sludinājuma pieliekamās sekcijas ar to
    teikumiem (`sections`) + dzīvo preview HTML. Ja `segments` padots — preview lieto
    tos (nesaglabā), citādi listinga saglabātos. Lokācija no building_profile (kā publish)."""
    import wp_templates  # noqa: PLC0415
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM properties.listings WHERE id = %s", (listing_id,))
        L = cur.fetchone()
        if not L:
            raise HTTPException(404, f"Listing {listing_id} nav atrasts")
        bp = None
        if L.get("building_profile_id"):
            cur.execute("SELECT * FROM properties.building_profiles WHERE id = %s",
                        (L["building_profile_id"],))
            bp = cur.fetchone()
    sg = (str(L.get("Space_group") or "")).strip()
    Lp = dict(L)
    if bp:
        for loc in ("city", "district", "street"):
            if bp.get(loc):
                Lp[loc] = bp[loc]
    sections = wp_templates.body_segments(sg, Lp, bp)
    if req is not None and req.segments is not None:
        Lp["Agent_text_segments"] = req.segments
    preview_html = wp_templates.render_body(sg, Lp, bp)
    return {"sections": sections, "preview_html": preview_html}


# ---------------------------------------------------------------------------
# 2.6) TEKSTA KOREKTORS — "Labot kļūdas" poga (LV gramatika/pareizrakstība)
# ---------------------------------------------------------------------------

class TextFixReq(BaseModel):
    text: str


_TEXTFIX_SYSTEM = (
    "Tu esi profesionāls latviešu valodas korektors nekustamā īpašuma sludinājumiem. "
    "Izlabo gramatikas, pareizrakstības un interpunkcijas kļūdas dotajā tekstā. "
    "OBLIGĀTI saglabā nozīmi, faktus, skaitļus un lietišķo toni. NEPIEVIENO un "
    "NEIZŅEM informāciju, NEMAINI stilu vairāk kā nepieciešams kļūdu labošanai. "
    "Atgriez TIKAI izlaboto tekstu — bez paskaidrojumiem, bez pēdiņām, bez markdown."
)


@router.post("/text-fix")
def text_fix(req: TextFixReq, _auth: None = Depends(require_token)) -> dict:
    """Aizsūta aģenta tekstu OpenAI korektoram un atgriež izlaboto (LV gramatika).
    Kļūmes gadījumā atgriež oriģinālu ar ok=false (UI nekad nepaliek bez teksta)."""
    text = (req.text or "").strip()
    if not text:
        return {"text": "", "ok": True}
    if len(text) > 5000:
        text = text[:5000]
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {"text": req.text, "ok": False, "error": "OPENAI_API_KEY nav konfigurēts"}
    try:
        from openai import OpenAI
        # Ātrs ne-spriešanas modelis — poga jābūt "fast & smooth" (~2s, ne 4-5s kā
        # gpt-5.4-mini). Kvalitāte gramatikai tā pati. Pārlabojams ar TEXTFIX_MODEL.
        model = os.getenv("TEXTFIX_MODEL", "gpt-4.1-mini")
        _verify = os.getenv("VERIFY_SSL", os.getenv("WP_VERIFY_SSL", "1")) \
            not in ("0", "false", "False")
        if not _verify:
            import httpx
            client = OpenAI(api_key=api_key, http_client=httpx.Client(verify=False))
        else:
            client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=model,
            input=[
                {"role": "system",
                 "content": [{"type": "input_text", "text": _TEXTFIX_SYSTEM}]},
                {"role": "user",
                 "content": [{"type": "input_text", "text": text}]},
            ],
        )
        fixed = (resp.output_text or "").strip()
        # Notīra iespējamās pēdiņas ap visu tekstu, ja modelis tās pieliek.
        if len(fixed) >= 2 and fixed[0] in "\"“„'" and fixed[-1] in "\"”“'":
            fixed = fixed[1:-1].strip()
        if not fixed:
            return {"text": req.text, "ok": False, "error": "tukša atbilde"}
        return {"text": fixed, "ok": True}
    except Exception as e:  # noqa: BLE001
        return {"text": req.text, "ok": False, "error": str(e)[:200]}


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
    quality: str = "medium"  # low | medium | high (tikai openai dzinējam)
    engine: str = "openai"  # openai (gpt-image-1) | replicate (Seedream)


@router.post("/image-enhance")
def image_enhance_one(req: EnhanceOneReq, _auth: None = Depends(require_token)) -> dict:
    """Izsauc AI dzinēju pa vienu bildi un atgriež enhanced path. Frontend aizvieto
    src ar šo + uzliek enhanced=True flag. Dzinējs: openai (gpt-image-1) vai
    replicate (Seedream). KATRA bilde = atsevišķs, izolēts izsaukums."""
    src_path = STORAGE_ROOT / req.image_path
    if not src_path.is_file():
        raise HTTPException(404, f"Bilde nav atrasta: {req.image_path}")

    # Enhance result iet blakus oriģinālam. Atšķirīgs sufikss pa dzinējam, lai
    # var salīdzināt abus blakus, neviens nepārraksta otru.
    engine = (req.engine or "openai").strip().lower()
    suffix = "_enhanced_repl.jpg" if engine == "replicate" else "_enhanced.png"
    out_path = src_path.with_name(src_path.stem + suffix)
    try:
        if engine == "replicate":
            image_enhance_openai.enhance_image_replicate(
                src_path=src_path, dst_path=out_path,
            )
        else:
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

class PublishReq(BaseModel):
    """Permissīva shēma — building un units ir dict, lai pieņem abus
    kapitalizācijas variantus (Space_group / space_group, existing_bp_id /
    existing_building_id) un images kā list[dict] (ar type+featured atzīmēm)
    vai list[str] (tikai paths, vēsturiski).

    Validācija un normalizēšana notiek agent_publish.py iekš _insert_listing()
    un _get_or_create_bp(), kas paskata abas kapitalizācijas un images formāti.
    """
    mode: str  # 'easy' | 'full'
    wp_user_id: int
    draft_id: Optional[int] = None
    requested_by_email: Optional[str] = None
    building: dict[str, Any]
    units: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# 7) LISTING IMAGES — apskatīt esoša listing-a ai_ready bildes
# ---------------------------------------------------------------------------

@router.get("/listing-images/{listing_id}")
def listing_images(listing_id: int, _auth: None = Depends(require_token)) -> dict:
    """Atgriež listing-a bildes:
      raw/     = ss.lv oriģinālās (ar ūdenszīmi) — vienmēr ir, ja download_images.py
                 worker tos jau notvēris (kas notiek automātiski).
      ai_ready/= pēc image_pipeline.py (Seedream) — eksistē tikai pēc tam, kad
                 aģents nospiedis "Ielikt WP" (publish_to_wp.publish() triggerē).

    Atgriež RAW (priekš aģenta priekšskatu pirms publicēšanas).
    """
    base = STORAGE_ROOT / "listings" / str(listing_id)
    raw_dir = base / "raw"
    ai_dir = base / "ai_ready"
    wp_dir = base / "wp_raw"

    has_raw = raw_dir.is_dir()
    has_ai = ai_dir.is_dir()
    has_wp = wp_dir.is_dir()
    if not has_raw and not has_ai and not has_wp:
        return {"images": [], "note": f"Nav bilžu /storage/listings/{listing_id}/"}

    # Priekšroka raw (oriģināls ar SS.lv ūdenszīmi); tad ai_ready; tad wp_raw
    # (mājaslapas bildes — piem. WP-source listingi, kam ss.lv raw nav vispār;
    # bez šī fallback ēkas skata bilžu modālis tiem bija tukšs).
    if has_raw:
        src_dir, src_label = raw_dir, "raw"
    elif has_ai:
        src_dir, src_label = ai_dir, "ai_ready"
    else:
        src_dir, src_label = wp_dir, "wp_raw"

    _img_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
    files = sorted(p for p in src_dir.glob("img_*.*")
                   if p.suffix.lower() in _img_exts)
    if not files:
        # Mape ir, bet img_* failu nav (cits nosaukumu formāts?) — ņem visus attēlus
        files = sorted(p for p in src_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in _img_exts)
    images = [{
        "name": f.name,
        "type": src_label,  # 'raw' / 'ai_ready' / 'wp_raw'
        "url": f"/agent/image-proxy/{listing_id}/{src_label}/{f.name}",
    } for f in files]
    return {
        "images": images,
        "source": src_label,
        "has_ai_ready": has_ai,
        "note": ("RAW bildes no SS.lv (ar ūdenszīmi). AI uzlabošana notiks pie 'Ielikt WP' klikšķa."
                 if src_label == "raw" else
                 "AI-apstrādātas bildes (ūdenszīme noņemta)." if src_label == "ai_ready" else
                 "Mājaslapas bildes (WP)."),
    }


class DuplicateReq(BaseModel):
    draft_id: int
    target: str  # "unit_<localId>" — kur draft mapē kopēt bildes


# DB price_type → UI ("monthly"=noma / "regular"=pārdošana)
_SALE_PRICE_TYPES = {"regular", "pārdošana", "pardosana", "sale", "pārdod", "pardod"}


def _copy_listing_images_to_draft(
    listing_id: int, draft_id: int, target: str,
    wp_image_urls: Optional[list[str]] = None,
) -> list[dict]:
    """Kopē listinga bildes uz draft mapi → ImageRef[] paths, lai dublētā
    telpa tās rāda kā parastas augšuplādētas bildes.

    Kopē VISAS bildes, ko panelis reāli rāda (ne 1). Agrāk skatīja tikai
    ai_ready→raw, tāpēc mājaslapā-dzimušiem (source='wp') listingiem — kam ir
    tikai wp_raw / wp_image_urls, ne ss.lv raw — dublējās 1 bilde, kaut galerijā
    ir visas (#61914 Ganību Dambis 25d). Tagad no lokālajām mapēm ņem to ar
    VISVAIRĀK bildēm (= pilnā galerija); pie vienāda skaita priekšroka ai_ready
    (TĪRAS, bez ss.com ūdenszīmes → #109581 regresija) > wp_raw > raw. Ja WP
    remote URL komplekts ir lielāks par jebkuru lokālo spoguli (wp_raw spogulis
    vēl nav pārvilcies pēc «Ievilkt mājaslapas bildes»), lejupielādē pilno
    galeriju tieši no wp_image_urls."""
    base = STORAGE_ROOT / "listings" / str(listing_id)
    exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

    def _files(folder: str) -> list[Path]:
        d = base / folder
        if not d.is_dir():
            return []
        fs = sorted(p for p in d.glob("img_*.*") if p.suffix.lower() in exts)
        if not fs:  # cits nosaukumu formāts — ņem visus attēlus
            fs = sorted(p for p in d.iterdir()
                        if p.is_file() and p.suffix.lower() in exts)
        return fs

    # Priekšroka: pie VIENĀDA skaita ai_ready > wp_raw > raw (max ņem pirmo
    # maksimālo → saraksta secība = tiebreak). Uzvar tas, kam visvairāk bilžu.
    local_best = max([_files("ai_ready"), _files("wp_raw"), _files("raw")], key=len)

    dst = STORAGE_ROOT / "agent_drafts" / str(draft_id) / target
    dst.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []

    urls = [u for u in (wp_image_urls or []) if u]
    if len(urls) > len(local_best):
        # Lokālais spogulis nepilnīgs → velc pilno galeriju no mājaslapas (WP
        # bildes jau tīras — Seedream tām gāja publicējot). download_one → None
        # ja bilde pazudusi; ja VISS neizdodas, atkāpjas uz local_best zemāk.
        import download_images
        for url in urls:
            data = download_images.download_one(url)
            if data is None:
                continue
            new = f"{uuid.uuid4().hex}.jpg"
            (dst / new).write_bytes(data)
            out.append({
                "path": f"agent_drafts/{draft_id}/{target}/{new}",
                "size": (dst / new).stat().st_size,
            })
        if out:
            return out

    for f in local_best:
        new = f"{uuid.uuid4().hex}{f.suffix.lower()}"
        shutil.copy2(f, dst / new)
        out.append({
            "path": f"agent_drafts/{draft_id}/{target}/{new}",
            "size": (dst / new).stat().st_size,
        })
    return out


@router.post("/duplicate-listing/{listing_id}")
def duplicate_listing(
    listing_id: int, req: DuplicateReq, _auth: None = Depends(require_token)
) -> dict:
    """Dublē esošu listingu jaunā anketas telpā (UnitForm shape + bilžu kopijas).
    Aģents tad to rediģē/akceptē; nevajadzīgo dzēš. Bildes kopējas draft mapē."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM properties.listings WHERE id = %s", (listing_id,))
        L = cur.fetchone()
    if not L:
        raise HTTPException(404, f"Listing {listing_id} nav atrasts")

    def s(col: str) -> str:
        v = L.get(col)
        return "" if v is None else str(v)

    def chk(col: str):
        v = str(L.get(col) or "").strip().lower()
        return "checked" if v == "checked" else ("not checked" if v == "not checked" else None)

    pt = str(L.get("price_type") or "").strip().lower()
    price_type = "regular" if pt in _SALE_PRICE_TYPES else ("monthly" if pt else None)
    wc_loc = L.get("WC_location")

    # #65: dublējot līdzi iet arī "der arī kā" (Potential_space_group bez primārā),
    # "Cenā ietilpst" un projekta ("telpas vēl nav uzceltas") atzīme — agrāk tie
    # izkrita un anketā bija jāķeksē no jauna.
    primary_sg = str(L.get("Space_group") or "").strip()
    potential = [
        x.strip() for x in str(L.get("Potential_space_group") or "").split(",")
        if x.strip() and x.strip() != primary_sg
    ]
    price_includes = [x.strip() for x in str(L.get("price_includes") or "").split(",") if x.strip()]

    unit = {
        "space_group": L.get("Space_group"),
        "potential_space_groups": potential,
        "price_includes": price_includes,
        "is_project": bool(L.get("is_project")),
        "project_completion": s("project_completion"),
        "area_m2": s("area_m2"),
        "floor": s("floor"),
        "price": s("price"),
        "price_type": price_type,
        "Apsaimniekosanas_maksa": s("Apsaimniekosanas_maksa"),
        "Papildu_maksas": s("Papildu_maksas"),
        "Space_condition": L.get("Space_condition"),
        "cik_telpas": s("Cik_telpas"),
        "cik_WC": s("cik_WC"),
        "WC_location": wc_loc if wc_loc in ("Telpā", "Koplietošanā") else None,
        "Griestu_augstums": s("Griestu_augstums"),
        "electric_power_kw": s("electric_power_kw"),
        "Gridas_izturiba_kg_m2": s("Gridas_izturiba_kg_m2"),
        "Zemes_gabals_m2": s("Zemes_gabals_m2"),
        "Parkings": L.get("Parkings"),
        "Agent_comment": s("Agent_comment"),
    }
    for c in (
        "Pacelamie_varti_check", "Rampa_logistikai_check", "Virtuve_check",
        "Sava_ieeja_check", "street_entrance", "Apsargajama_teritorija_check",
        "Nozogota_teritorija_check", "Auto_pacelajs_check", "Treifelis_Pacelajs",
        "Ir_izlietne_telpa_check", "Balkons_check", "Sava_eka_check",
    ):
        unit[c] = chk(c)

    images = _copy_listing_images_to_draft(
        listing_id, req.draft_id, req.target, L.get("wp_image_urls")
    )
    return {"unit": unit, "images": images}


@router.get("/image-proxy/{listing_id}/{folder}/{filename}")
def image_proxy(
    listing_id: int, folder: str, filename: str,
    token: Optional[str] = None,
    x_rgc_token: Annotated[Optional[str], Header(alias="X-RGC-Token")] = None,
):
    """Atgriež bildes baitus no /storage/listings/<id>/<folder>/.
    folder = 'raw' (ss.lv oriģināls) / 'ai_ready' (Seedream) / 'wp_raw' (mājaslapas).
    Auth: X-RGC-Token header VAI ?token=... query param."""
    from fastapi.responses import FileResponse
    if not RGC_MK_TOKEN:
        raise HTTPException(500, "Service nav konfigurēts")
    if x_rgc_token != RGC_MK_TOKEN and token != RGC_MK_TOKEN:
        raise HTTPException(403, "Trūkst tokena")
    if folder not in ("raw", "ai_ready", "wp_raw"):
        raise HTTPException(400, "folder ir 'raw', 'ai_ready' vai 'wp_raw'")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Nepareizs filename")
    path = STORAGE_ROOT / "listings" / str(listing_id) / folder / filename
    if not path.is_file():
        raise HTTPException(404, "Bilde nav atrasta")
    # Cache-Control: bez tā pārlūks/panelis katru reizi vilka pilnu bildi no
    # Railway (lēnā ielāde). Faila saturs tajā pašā ceļā mainās tikai crop/enhance,
    # ko UI pavada ar ?v= cache-busteri → 1h kešs ir drošs.
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------------------
# Listinga bilžu rediģēšana (Broker Panel image-edit, Plan B)
# Panelim NAV volume (Railway = 1 volume/serviss), tāpēc upload/delete iet caur
# šo worker (kam pieder volume). Šis dara TIKAI failu I/O; DB masīvu
# (local_image_paths_*) atjauno panelis (tam ir chooseImageSource + Prisma).
# ---------------------------------------------------------------------------

_EDIT_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_EDIT_FOLDERS = {"raw", "ai_ready", "wp_raw"}


@router.post("/listing-file-write/{listing_id}")
def listing_file_write(
    listing_id: int,
    folder: str = Form(...),
    file: UploadFile = File(...),
    _auth: None = Depends(require_token),
) -> dict:
    """Saglabā 1 augšupielādētu bildi uz /storage/listings/<id>/<folder>/.
    Atgriež relatīvo ceļu, ko panelis pievieno DB masīvam."""
    if folder not in _EDIT_FOLDERS:
        raise HTTPException(400, f"folder jābūt {_EDIT_FOLDERS}")
    if not file.filename:
        raise HTTPException(400, "Filename trūkst")
    ext = Path(file.filename).suffix.lower() or ".jpg"
    if ext not in _EDIT_IMG_EXTS:
        raise HTTPException(400, f"Nepieņemams paplašinājums: {ext}")
    base = STORAGE_ROOT / "listings" / str(listing_id) / folder
    base.mkdir(parents=True, exist_ok=True)
    filename = f"img_user_{uuid.uuid4().hex}{ext}"
    out_path = base / filename
    with open(out_path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    return {
        "ok": True,
        "path": f"listings/{listing_id}/{folder}/{filename}",
        "size": out_path.stat().st_size,
    }


class ListingFileDeleteReq(BaseModel):
    paths: list[str]  # relatīvi /storage ceļi: "listings/<id>/<folder>/<file>"


@router.post("/listing-file-delete/{listing_id}")
def listing_file_delete(
    listing_id: int,
    req: ListingFileDeleteReq,
    _auth: None = Depends(require_token),
) -> dict:
    """Dzēš norādītos bilžu failus no volume. Drošība: tikai šī listinga mapē.
    Panelis pēc tam atjauno DB masīvu."""
    prefix = f"listings/{listing_id}/"
    deleted = 0
    for rel in req.paths:
        if not rel.startswith(prefix) or ".." in rel:
            continue
        p = STORAGE_ROOT / rel
        try:
            if p.is_file():
                p.unlink()
                deleted += 1
        except OSError:
            pass
    return {"ok": True, "deleted": deleted}


@router.get("/draft-image-proxy/{draft_id}/{target}/{filename}")
def draft_image_proxy(
    draft_id: int, target: str, filename: str,
    token: Optional[str] = None,
    x_rgc_token: Annotated[Optional[str], Header(alias="X-RGC-Token")] = None,
):
    """Atgriež bildes baitus no /storage/agent_drafts/<draft_id>/<target>/.
    Lieto anketa-par-eku frontendam, lai parādītu draftā augšupielādētās
    bildes pirms publikācijas (kad tās vēl nav uz listings/<id>/raw/)."""
    from fastapi.responses import FileResponse
    if not RGC_MK_TOKEN:
        raise HTTPException(500, "Service nav konfigurēts")
    if x_rgc_token != RGC_MK_TOKEN and token != RGC_MK_TOKEN:
        raise HTTPException(403, "Trūkst tokena")
    safe_target = target.replace("/", "_").replace("\\", "_")[:32]
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Nepareizs filename")
    path = STORAGE_ROOT / "agent_drafts" / str(draft_id) / safe_target / filename
    if not path.is_file():
        raise HTTPException(404, "Bilde nav atrasta")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# 7b) SS.LV IMPORTS CAUR LINKU — viens sludinājums → listings DB (bez WP)
# ---------------------------------------------------------------------------

class SslvImportReq(BaseModel):
    url: str
    wp_user_id: int = 0


@router.post("/sslv-import")
def sslv_import_endpoint(req: SslvImportReq,
                         _auth: None = Depends(require_token)) -> dict:
    """«Caur linku» imports: noskrāpē VIENU ss.lv sludinājumu (kas nav DB),
    izlaiž caur AI analīzi (ss.lv teksts + galerijas URL) un izveido listings
    rindu ar Debug_status='ok'. UZ WP NEIET — kontaktus aģents ieraksta pats,
    tad «Export to WP». Bildes lejupielādē image_download_poller (~30s).
    Sinhrons (~30-90s, AI vision). Sk. sslv_import.py."""
    import sslv_import
    try:
        return sslv_import.import_from_url(req.url, req.wp_user_id)
    except sslv_import.SslvImportError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Imports neizdevās: {type(e).__name__}: {str(e)[:300]}")


# ---------------------------------------------------------------------------
# 7c) WP GALERIJAS SINHRONIZĀCIJA — mājaslapā-dzimušiem (source='wp') listingiem
#     ievelk PILNO Houzez galeriju no WP (mūsu DB importējot glabāja tikai 1).
# ---------------------------------------------------------------------------

@router.post("/wp-gallery-sync/{listing_id}")
def wp_gallery_sync(listing_id: int,
                    _auth: None = Depends(require_token)) -> dict:
    """Ievelk PILNO WP galeriju listingam, kuram wp_post_id ir (parasti
    source='wp' — izveidots tieši WordPress). Raksta wp_image_urls = visas
    galerijas bildes + reset wp_images_downloaded_at=NULL → image_download_poller
    pārvelk lokālo wp_raw spoguli (~30s) → panelis rāda visas + dublēšana kopē
    visas. Prasa rgc-mk plugin >= 5.2.0 (GET /property/{id}/gallery)."""
    with _db() as conn:
        row = conn.execute(
            'SELECT wp_post_id, source FROM properties.listings WHERE id = %s',
            (listing_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Listings nav atrasts")
    wp_post_id = row["wp_post_id"]
    if not wp_post_id:
        raise HTTPException(400, "Listingam nav wp_post_id (nav uz mājaslapas) — nav ko sinhronizēt")

    import wp_publisher
    try:
        gallery = wp_publisher.WPPublisher().get_property_gallery(int(wp_post_id))
    except Exception as e:
        raise HTTPException(502, f"WP galerijas nolasīšana neizdevās: {str(e)[:300]}")

    urls = [im["url"] for im in (gallery.get("images") or [])
            if isinstance(im, dict) and im.get("url")]
    if not urls:
        return {"ok": True, "listing_id": listing_id, "count": 0,
                "note": "WP galerija tukša — nekas netika mainīts."}

    with _db() as conn:
        conn.execute(
            """UPDATE properties.listings
               SET wp_image_urls = %s, wp_images_downloaded_at = NULL
               WHERE id = %s""",
            (urls, listing_id),
        )
        conn.commit()
    return {"ok": True, "listing_id": listing_id, "count": len(urls),
            "featured_id": gallery.get("featured_id")}


# ---------------------------------------------------------------------------
# 8) REPUBLISH — esoša listing-a (ne agent_anketa) publicēšana uz WP
# ---------------------------------------------------------------------------

class RepublishReq(BaseModel):
    force: bool = False  # True → pāraugšuplādē bildes (pēc in-place enhance)


@router.post("/republish/{listing_id}")
def republish(listing_id: int, req: Optional[RepublishReq] = None,
              _auth: None = Depends(require_token)) -> dict:
    """Izsauc publish_to_wp.publish() priekš jau eksistējoša listing-a (kas
    DB-ā ir, bet wp_post_id=NULL). Lieto, kad aģents anketā autocomplete
    ielādē esošu building_profile ar sslv-listings un grib tos arī uzlikt
    uz WP bez datu pārievades. force=True → bilžu re-upload (bilžu editors
    pēc in-place enhance, lai WP dabū jauno bildes saturu, ne veco cache)."""
    import publish_to_wp
    force = bool(req.force) if req else False
    try:
        publish_to_wp.publish(listing_id, dry_run=False, force=force, skip_ai=False)
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


# ---------------------------------------------------------------------------
# 9) LISTINGA BILŽU EDITORS (Broker Panel) — in-place AI enhance + manifests
#    (galvenā bilde / plāns). Operē ar DZĪVĀ listinga ai_ready mapi + manifestu
#    /storage/listings/<id>/_image_manifest.json. Panelis pēc tam republicē.
# ---------------------------------------------------------------------------

_EDITOR_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _ai_ready_files(listing_id: int) -> list[Path]:
    ai_dir = STORAGE_ROOT / "listings" / str(listing_id) / "ai_ready"
    if not ai_dir.is_dir():
        return []
    return sorted(p for p in ai_dir.glob("img_*.*")
                  if p.suffix.lower() in _EDITOR_IMG_EXTS)


def _manifest_map(listing_id: int) -> dict[str, str]:
    """Atgriež {filename: type} VISĀM ai_ready bildēm. Manifestā trūkstošajām
    liek default (pirmā = fasade, pārējās = interjers) — spoguļo agent_publish
    _write_image_manifest, lai galvenā bilde ir noteikta arī bez manifesta."""
    files = _ai_ready_files(listing_id)
    existing = image_classify.load_manifest(STORAGE_ROOT, listing_id)
    out: dict[str, str] = {}
    for i, p in enumerate(files):
        info = existing.get(p.name) if isinstance(existing, dict) else None
        t = (info or {}).get("type") if isinstance(info, dict) else None
        if t not in ("fasade", "interjers", "plans", "cits"):
            t = "fasade" if i == 0 else "interjers"
        out[p.name] = t
    return out


def _write_manifest_map(listing_id: int, types: dict[str, str]) -> None:
    """Saglabā {filename: type} manifestā (wrapped {"images": {...}} formātā,
    ko lasa image_classify.load_manifest un publish_to_wp._split_by_manifest).
    Saglabā esošo quality/reason, ja bija."""
    existing = image_classify.load_manifest(STORAGE_ROOT, listing_id)
    images: dict[str, dict] = {}
    for name, t in types.items():
        prev = existing.get(name) if isinstance(existing, dict) else None
        prev = prev if isinstance(prev, dict) else {}
        images[name] = {**prev, "type": t,
                        "quality": prev.get("quality", "good_for_website"),
                        "filename": name}
    # __manual_order__ (paneļa manuālā secība) un __wp_att__ (per-faila WP
    # attachment karte ātrajam re-publish) NEDRĪKST pazust pie featured/plāna
    # maiņas — pārnes no esošā manifesta.
    if isinstance(existing, dict) and existing.get("__manual_order__"):
        images["__manual_order__"] = True
    if isinstance(existing, dict) and isinstance(existing.get("__wp_att__"), dict):
        images["__wp_att__"] = existing["__wp_att__"]
    image_classify.save_manifest(STORAGE_ROOT, listing_id, images)


def _featured_of(types: dict[str, str]) -> Optional[str]:
    """Galvenā bilde = pirmā fasade (kā publish_to_wp._split_by_manifest sorto);
    ja fasade nav — pirmā ne-plāna bilde."""
    for name, t in types.items():
        if t == "fasade":
            return name
    for name, t in types.items():
        if t != "plans":
            return name
    return None


@router.get("/listing-manifest/{listing_id}")
def listing_manifest(listing_id: int, _auth: None = Depends(require_token)) -> dict:
    """Bilžu editora sākumstāvoklis: katras ai_ready bildes tips + galvenā."""
    types = _manifest_map(listing_id)
    return {
        "images": types,
        "featured": _featured_of(types),
        "plans": [n for n, t in types.items() if t == "plans"],
    }


class ClassifyReq(BaseModel):
    op: str            # "featured" | "plan"
    filename: str      # ai_ready bildes fails (img_...)
    on: bool = True    # plan: True=atzīmē, False=noņem


@router.post("/listing-image-classify/{listing_id}")
def listing_image_classify(listing_id: int, req: ClassifyReq,
                           _auth: None = Depends(require_token)) -> dict:
    """Atzīmē galveno bildi (featured→fasade, pārējās ne-plāni→interjers) vai
    plānu (type=plans / atpakaļ interjers). Raksta manifestu; publicēšanu
    (republish) izsauc panelis, lai izmaiņa uzreiz aiziet mājaslapā."""
    types = _manifest_map(listing_id)
    if req.filename not in types:
        raise HTTPException(404, f"Bilde nav ai_ready: {req.filename}")

    if req.op == "featured":
        # Tieši viena fasade → tā kļūst galvenā (sorto pirmā). Pārējās, kas nav
        # plāni, → interjers. Plānus neaiztiek.
        for name in types:
            if name == req.filename:
                types[name] = "fasade"
            elif types[name] != "plans":
                types[name] = "interjers"
    elif req.op == "plan":
        types[req.filename] = "plans" if req.on else "interjers"
    else:
        raise HTTPException(400, f"Nezināma op: {req.op}")

    _write_manifest_map(listing_id, types)
    return {
        "ok": True,
        "images": types,
        "featured": _featured_of(types),
        "plans": [n for n, t in types.items() if t == "plans"],
    }


class ImageBatchReq(BaseModel):
    featured: Optional[str] = None       # ai_ready fails, kam būt galvenajai
    plans: Optional[list[str]] = None    # PILNS plānu saraksts (None = neaiztikt)
    manual_order: bool = False           # uzliek __manual_order__ karogu


@router.post("/listing-image-batch/{listing_id}")
def listing_image_batch(listing_id: int, req: ImageBatchReq,
                        _auth: None = Depends(require_token)) -> dict:
    """VIENĀ izsaukumā: galvenā + pilns plānu komplekts + manual_order karogs
    (bilžu editora «Saglabāt» — agrāk katrs bija atsevišķs HTTP izsaukums →
    lēni). Viens manifest write. plans = PILNS vēlamais saraksts (diff šeit)."""
    types = _manifest_map(listing_id)
    if req.plans is not None:
        want = {p for p in req.plans if p in types}
        for name in types:
            if name in want:
                types[name] = "plans"
            elif types[name] == "plans":
                types[name] = "interjers"
    if req.featured and req.featured in types:
        for name in types:
            if name == req.featured:
                types[name] = "fasade"
            elif types[name] != "plans":
                types[name] = "interjers"
    _write_manifest_map(listing_id, types)
    if req.manual_order:
        manifest = image_classify.load_manifest(STORAGE_ROOT, listing_id)
        if isinstance(manifest, dict) and not manifest.get("__manual_order__"):
            manifest["__manual_order__"] = True
            image_classify.save_manifest(STORAGE_ROOT, listing_id, manifest)
    return {
        "ok": True,
        "featured": _featured_of(types),
        "plans": [n for n, t in types.items() if t == "plans"],
    }


@router.post("/listing-manual-order/{listing_id}")
def listing_manual_order(listing_id: int,
                         _auth: None = Depends(require_token)) -> dict:
    """Panelis pēc manuālas bilžu labošanas (secība/upload/dzēšana) atzīmē
    manifestā `__manual_order__` → publish_to_wp galeriju NEPĀRKĀRTO pēc
    type (fasade/interjers/cits), bet saglabā paneļa DB masīva secību —
    aģents mājaslapā redz TIEŠI to, ko sakārtoja editorā (Raimonds
    2026-08-02). Featured (★) paliek pirmā fasade. Idempotents."""
    manifest = image_classify.load_manifest(STORAGE_ROOT, listing_id)
    if not isinstance(manifest, dict):
        manifest = {}
    if not manifest.get("__manual_order__"):
        manifest["__manual_order__"] = True
        image_classify.save_manifest(STORAGE_ROOT, listing_id, manifest)
    return {"ok": True}


class EditorEnhanceReq(BaseModel):
    filename: str                 # bildes fails folderī
    folder: str = "ai_ready"      # raw | ai_ready | wp_raw (kur bilde reāli ir)
    engine: str = "replicate"     # replicate (lētais) | openai (dārgais)
    quality: str = "medium"       # tikai openai
    # Custom prompt (tikai replicate/Seedream ceļam) — aģents pats apraksta, kas
    # bildē jāizdara (piem. konkrētas ūdenszīmes noņemšana). Tukšs → standarta
    # Seedream PROMPT.
    prompt: Optional[str] = None


@router.post("/listing-image-enhance/{listing_id}")
def listing_image_enhance(listing_id: int, req: EditorEnhanceReq,
                          _auth: None = Depends(require_token)) -> dict:
    """AI uzlabo VIENU dzīvā listinga bildi UZ VIETAS (pārraksta to pašu failu,
    lai DB ceļš/secība/manifests nemainās). Strādā jebkurā lokālā folderī
    (raw/ai_ready/wp_raw) — tā AI (t.sk. custom prompt ūdenszīmju noņemšanai)
    pieejama arī vēl nepublicētiem / tikko importētiem listingiem, kur apstrādātu
    bilžu vēl nav (Raimonds 2026-08-23). Pēc tam ss.com ūdenszīmes pārbaude.
    Panelis pēc tam republicē (force) → WP dabū jauno."""
    if req.folder not in _EDIT_FOLDERS:
        raise HTTPException(400, f"folder jābūt {_EDIT_FOLDERS}")
    img_dir = STORAGE_ROOT / "listings" / str(listing_id) / req.folder
    src = img_dir / req.filename
    if req.filename.startswith(".") or "/" in req.filename \
            or "\\" in req.filename or ".." in req.filename:
        raise HTTPException(400, "Nederīgs filename")
    if not src.is_file():
        raise HTTPException(404, f"Bilde nav {req.folder}: {req.filename}")

    engine = (req.engine or "replicate").strip().lower()
    tmp = src.with_name(src.stem + "__enh_tmp" + src.suffix)
    try:
        if engine == "openai":
            image_enhance_openai.enhance_image(
                src_path=src, dst_path=tmp, quality=req.quality)
        else:
            image_enhance_openai.enhance_image_replicate(
                src_path=src, dst_path=tmp,
                prompt=(req.prompt or "").strip() or None)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(500, f"AI uzlabošana neizdevās: {str(e)[:200]}")

    # Drošība: ja AI dzinējs atstāja ss.com ūdenszīmi — neapstiprinām.
    try:
        wm = watermark_check.has_watermark_bytes(tmp.read_bytes())
    except Exception:
        wm = None
    if wm is True:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            422, "Pēc AI ūdenszīme joprojām redzama — mēģini otru dzinēju.")

    # Pārraksta oriģinālo failu ar uzlaboto saturu (tas pats filename → DB nav
    # jāmaina). Manifestā uzlabotā bilde vairs nav plāns pēc noklusējuma? Nē —
    # tipu neaiztiekam, tikai saturu.
    tmp.replace(src)
    return {"ok": True, "filename": req.filename,
            "size": src.stat().st_size, "engine": engine}


class ListingImageCropReq(BaseModel):
    filename: str                 # bildes fails folderī
    folder: str = "ai_ready"      # raw | ai_ready | wp_raw
    action: str = "crop"          # crop | restore | status
    # Crop kaste [left, top, width, height] px — koordinātas PĒC exif-transpose
    # + rotate (t.i., tādā orientācijā, kādā lietotājs bildi redz pārlūkā).
    box: Optional[list[int]] = None
    rotate: int = 0               # 0/90/180/270 grādi pulksteņrādītāja virzienā


def _crop_backup_path(listing_id: int, folder: str, filename: str) -> Path:
    """Oriģināla rezerves kopija pirms pirmā crop — lai «Atjaunot oriģinālu»
    strādā arī pēc vairākiem secīgiem crop (backup taisa tikai vienreiz)."""
    return (STORAGE_ROOT / "listings" / str(listing_id) / "edit_backup"
            / f"{folder}__{filename}")


@router.post("/listing-image-crop/{listing_id}")
def listing_image_crop(listing_id: int, req: ListingImageCropReq,
                       _auth: None = Depends(require_token)) -> dict:
    """Apgriež (crop) / pagriež VIENU listinga bildi UZ VIETAS — jaunā bilde
    aizvieto veco (tas pats filename → DB ceļš/secība/manifests nemainās, kā
    enhance). Pirmajā reizē oriģinālu noliek edit_backup/ → action=restore to
    atliek atpakaļ. action=status tikai pasaka, vai backup ir."""
    if req.folder not in _EDIT_FOLDERS:
        raise HTTPException(400, f"folder jābūt {_EDIT_FOLDERS}")
    if req.filename.startswith(".") or "/" in req.filename \
            or "\\" in req.filename or ".." in req.filename:
        raise HTTPException(400, "Nederīgs filename")
    src = STORAGE_ROOT / "listings" / str(listing_id) / req.folder / req.filename
    backup = _crop_backup_path(listing_id, req.folder, req.filename)

    if req.action == "status":
        return {"ok": True, "has_backup": backup.is_file()}

    if req.action == "restore":
        if not backup.is_file():
            raise HTTPException(404, "Nav saglabāta oriģināla, ko atjaunot")
        shutil.copyfile(backup, src)  # backup paliek — var atjaunot atkārtoti
        return {"ok": True, "restored": True, "filename": req.filename}

    if req.action != "crop":
        raise HTTPException(400, f"Nezināma action: {req.action}")
    if not src.is_file():
        raise HTTPException(404, f"Bilde nav atrasta: {req.folder}/{req.filename}")
    rotate = req.rotate % 360
    if rotate not in {0, 90, 180, 270}:
        raise HTTPException(400, "rotate jābūt 0/90/180/270")
    if req.box is None and rotate == 0:
        raise HTTPException(400, "Nav ne crop kastes, ne rotācijas")

    from PIL import Image, ImageOps  # lokāli — neaiztur worker startu

    try:
        img = Image.open(src)
        img = ImageOps.exif_transpose(img)  # pārlūks rāda transposed — sakrītam
        if rotate:
            img = img.rotate(-rotate, expand=True)  # PIL + = CCW, mums CW
        if req.box is not None:
            if len(req.box) != 4:
                raise HTTPException(400, "box jābūt [left, top, width, height]")
            left, top, w, h = (int(v) for v in req.box)
            if w < 20 or h < 20:
                raise HTTPException(400, "Crop kaste par mazu (min 20px)")
            if left < 0 or top < 0 or left + w > img.width or top + h > img.height:
                raise HTTPException(
                    400, f"Crop kaste ārpus bildes ({img.width}x{img.height})")
            img = img.crop((left, top, left + w, top + h))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Bildes apstrāde neizdevās: {str(e)[:200]}")

    # Backup TIKAI pirmajā reizē — tas vienmēr ir īstais oriģināls.
    if not backup.is_file():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, backup)

    ext = src.suffix.lower()
    tmp = src.with_name(src.stem + "__crop_tmp" + src.suffix)
    try:
        if ext in {".jpg", ".jpeg"}:
            img.convert("RGB").save(tmp, "JPEG", quality=92)
        else:
            img.save(tmp)  # png/webp — formātu nosaka paplašinājums
        tmp.replace(src)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(500, f"Saglabāšana neizdevās: {str(e)[:200]}")

    return {"ok": True, "filename": req.filename, "width": img.width,
            "height": img.height, "has_backup": True,
            "size": src.stat().st_size}


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
