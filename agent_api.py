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
import re
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
    # Normalizē meklēšanas terminu tāpat kā DB izteiksme (tipa vārds — arī
    # SAĪSINĀTS (bulv./gat./pr.) + iniciāļi (Kr.) + diakritika nost; 2026-09-22
    # «Aspazijas bulvāris 20» ↔ «Aspazijas bulv. 20» spogulis street-search.ts +
    # mig. 2026-09-22_street_search_abbrev).
    # ⚠ «M.»/«L.» = «Mazā»/«Lielā» — cita iela → izvērš, NEizmet (2026-09-22).
    _LV = str.maketrans("āčēģīķļņōŗšūž", "acegiklnorsuz")
    _MAZA = _re.compile(r"\b(?:m|maz)\.\s*")
    _LIELA = _re.compile(r"\b(?:l|liel)\.\s*")
    _INI = _re.compile(r"\b(?!m\.|l\.)[a-z]{1,2}\.")
    _TYPE = _re.compile(r"\b(iela|iel|gatve|gat|bulvaris|bulv|prospekts|prosp|pr|laukums|dambis|cels|aleja|soseja|linija|krastmala|tilts|pasaza)\.?\b")
    key = q.split(",")[0].lower().translate(_LV)
    key = _MAZA.sub("maza ", key)
    key = _LIELA.sub("liela ", key)
    key = _INI.sub(" ", key)
    key = _TYPE.sub(" ", key).replace(".", " ")
    key = _re.sub(r"\s+", " ", key).strip()
    # SQL norm izteiksme (= listings.street_search ģenerētā kolona pēc mig.
    # 2026-09-22_street_search_mazal)
    norm = (r"btrim(regexp_replace(regexp_replace(regexp_replace(regexp_replace("
            r"regexp_replace(regexp_replace("
            r"translate(lower(split_part(%s,',',1)),"
            r"'āčēģīķļņōŗšūž','acegiklnorsuz'),"
            r"'\y(m|maz)\.\s*','maza ','g'),"
            r"'\y(l|liel)\.\s*','liela ','g'),"
            r"'\y[a-z]{1,2}\.',' ','g'),"
            r"'\y(iela|iel|gatve|gat|bulvaris|bulv|prospekts|prosp|pr|laukums|dambis|cels|aleja|soseja|linija|krastmala|tilts|pasaza)\y',' ','g'),"
            r"'\.',' ','g'),'\s+',' ','g'))")
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
    # IMPORTS (2026-10-15): intent='import' → tikai DB, bez WP rindas.
    # source: 'agent_pdf' | 'agent_manual'; import_token — no /agent/import/analyze.
    intent: Optional[str] = None
    source: Optional[str] = None
    import_token: Optional[str] = None
    import_file_name: Optional[str] = None


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
    w: Optional[int] = None,
    x_rgc_token: Annotated[Optional[str], Header(alias="X-RGC-Token")] = None,
):
    """Atgriež bildes baitus no /storage/listings/<id>/<folder>/.
    folder = 'raw' (ss.lv oriģināls) / 'ai_ready' (Seedream) / 'wp_raw' (mājaslapas).
    Auth: X-RGC-Token header VAI ?token=... query param.

    ?w=<px> (Raimonds 2026-08-27 «bildes lēni lādējas»): atdod GATAVU sīktēlu
    (WebP), kešotu uz volume (.thumbs/). Agrāk panelis vilka PILNO failu
    (vairāki MB × 16 bildes) un mazināja pats — tagad pa tīklu iet ~30-60KB
    un paneļa CPU neko nedara. Kešs invalidējas pēc avota mtime (crop/AI
    pārraksta failu → sīktēls pārģenerējas)."""
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

    if w and 50 <= w <= 1600:
        try:
            src_mtime = path.stat().st_mtime
            tdir = path.parent / ".thumbs"
            tpath = tdir / f"{filename}.w{int(w)}.webp"
            if not (tpath.is_file() and tpath.stat().st_mtime >= src_mtime):
                from PIL import Image, ImageOps
                tdir.mkdir(exist_ok=True)
                with Image.open(path) as im:
                    im = ImageOps.exif_transpose(im)
                    if im.width > w:
                        im = im.resize(
                            (w, max(1, round(im.height * w / im.width))),
                            Image.LANCZOS)
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    im.save(tpath, "WEBP", quality=72)
            return FileResponse(
                tpath, media_type="image/webp",
                headers={"Cache-Control": "private, max-age=86400"})
        except Exception:
            pass  # sīktēls neizdevās → atdod pilno failu kā līdz šim

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


class ImageCopyItem(BaseModel):
    src_listing_id: int
    folder: str              # avota mape: ai_ready | raw | wp_raw
    filename: str
    type: Optional[str] = None  # fasade | interjers | plans | None (= AI/noklusējums)


class ImageCopyReq(BaseModel):
    target_folder: str       # raw | ai_ready | wp_raw (panelis izvēlas pēc listinga avota)
    items: list[ImageCopyItem]


@router.post("/listing-image-copy/{listing_id}")
def listing_image_copy(listing_id: int, req: ImageCopyReq,
                       _auth: None = Depends(require_token)) -> dict:
    """«Pievienot no citiem listingiem» (Raimonds 2026-10-20): pārkopē izvēlētās bildes
    no citu listingu volume mapēm uz ŠĪ listinga mapi (jauns vārds img_user_<uuid>.ext)
    + ieraksta manifestā aģenta norādīto tipu (fasade/interjers/plans). Manifests ir pēc
    faila vārda; image_pipeline saglabā vārdu (raw→ai_ready), ensure_classified izlaiž jau
    atzīmētos → aģenta izvēle paliek arī pēc Seedream/publicēšanas.
    Tikai faila I/O + manifests; DB masīvu atjauno panelis (tāpat kā listing-file-write)."""
    if req.target_folder not in _EDIT_FOLDERS:
        raise HTTPException(400, f"target_folder jābūt {_EDIT_FOLDERS}")
    if not req.items:
        raise HTTPException(400, "Nav izvēlētu bilžu")
    if len(req.items) > 60:
        raise HTTPException(400, "Max 60 bildes vienā reizē")
    dest_dir = STORAGE_ROOT / "listings" / str(listing_id) / req.target_folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[dict] = []
    skipped: list[dict] = []
    types: dict[str, str] = {}
    for it in req.items:
        fn = it.filename
        if (it.folder not in _EDIT_FOLDERS or not fn or fn.startswith(".")
                or "/" in fn or "\\" in fn or ".." in fn):
            skipped.append({"filename": fn, "reason": "nederīgs ceļš"})
            continue
        src = STORAGE_ROOT / "listings" / str(it.src_listing_id) / it.folder / fn
        ext = src.suffix.lower()
        if ext not in _EDIT_IMG_EXTS:
            skipped.append({"filename": fn, "reason": f"neatbalstīts formāts {ext}"})
            continue
        if not src.is_file():
            skipped.append({"filename": fn, "reason": "fails nav uz volume"})
            continue
        new_name = f"img_user_{uuid.uuid4().hex}{ext}"
        shutil.copy2(src, dest_dir / new_name)
        rel = f"listings/{listing_id}/{req.target_folder}/{new_name}"
        copied.append({"path": rel, "filename": new_name,
                       "from": f"#{it.src_listing_id}/{it.folder}/{fn}"})
        if it.type in ("fasade", "interjers", "plans"):
            types[new_name] = it.type
    if types:
        manifest = image_classify.load_manifest(STORAGE_ROOT, listing_id)
        if not isinstance(manifest, dict):
            manifest = {}
        if any(t == "fasade" for t in types.values()):
            # Viena galvenā: aģenta jaunā fasāde kļūst galvenā → vecās fasādes → interjers
            for k, v in list(manifest.items()):
                if isinstance(v, dict) and v.get("type") == "fasade":
                    manifest[k] = {**v, "type": "interjers"}
            first = True
            for name, t in types.items():
                if t == "fasade":
                    if not first:
                        types[name] = "interjers"
                    first = False
        for name, t in types.items():
            manifest[name] = {"type": t, "quality": "good_for_website",
                              "reason": "aģents (pievienots no cita listinga)",
                              "filename": name}
        image_classify.save_manifest(STORAGE_ROOT, listing_id, manifest)
    return {"ok": True, "copied": copied, "skipped": skipped}


class DraftImageCopyReq(BaseModel):
    draft_id: int
    target: str                       # 'building' | 'unit_<localId>' (kā /agent/image-upload)
    items: list[ImageCopyItem]        # src_listing_id + folder (ai_ready|raw|wp_raw) + filename


@router.post("/draft-image-copy")
def draft_image_copy(req: DraftImageCopyReq,
                     _auth: None = Depends(require_token)) -> dict:
    """ANKETAI: bildes no esošiem listingiem → anketas melnraksta mapē
    `agent_drafts/<draft_id>/<target>/` (tieši tāpat kā /agent/image-upload), lai anketa
    tās rāda, kārto un publicējot pārkopē kā savējās. Atgriež [{path, size, from}]."""
    if not req.items:
        raise HTTPException(400, "Nav izvēlētu bilžu")
    if len(req.items) > 60:
        raise HTTPException(400, "Max 60 bildes vienā reizē")
    safe_target = req.target.replace("/", "_").replace("\\", "_")[:32]
    base = STORAGE_ROOT / "agent_drafts" / str(req.draft_id) / safe_target
    base.mkdir(parents=True, exist_ok=True)
    copied: list[dict] = []
    skipped: list[dict] = []
    for it in req.items:
        fn = it.filename
        if (it.folder not in _EDIT_FOLDERS or not fn or fn.startswith(".")
                or "/" in fn or "\\" in fn or ".." in fn):
            skipped.append({"filename": fn, "reason": "nederīgs ceļš"})
            continue
        src = STORAGE_ROOT / "listings" / str(it.src_listing_id) / it.folder / fn
        ext = src.suffix.lower()
        if ext not in _EDIT_IMG_EXTS or not src.is_file():
            skipped.append({"filename": fn, "reason": "fails nav atrasts"})
            continue
        out = base / f"{uuid.uuid4().hex}{ext}"
        shutil.copy2(src, out)
        copied.append({"path": str(out.relative_to(STORAGE_ROOT)).replace("\\", "/"),
                       "size": out.stat().st_size,
                       "from": f"#{it.src_listing_id}/{it.folder}/{fn}"})
    return {"ok": True, "copied": copied, "skipped": skipped}


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
            # Arī kešotos sīktēlus (.thumbs/<fails>.wNNN.webp) — lai orphan
            # sīktēli nekrājas un dzēstais nekur vairs neparādās.
            tdir = p.parent / ".thumbs"
            if tdir.is_dir():
                for t in tdir.glob(f"{p.name}.w*.webp"):
                    try:
                        t.unlink()
                    except OSError:
                        pass
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
    if req.intent is not None and req.intent != "import":
        raise HTTPException(400, f"Nezināms intent: {req.intent}")
    try:
        return agent_publish.publish_anketa(req.dict())
    except agent_publish.DuplicateListingError as e:
        # 409 ar cilvēkam lasāmu tekstu — panelis to rāda toast-ā.
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except psycopg.errors.IntegrityError as e:
        # DB noraidīja kādu lauku (CHECK/enum/unique) — rādām, KURU, nevis «500».
        diag = getattr(e, "diag", None)
        cname = getattr(diag, "constraint_name", None) or ""
        raise HTTPException(
            400,
            f"Datubāze noraidīja ierakstu ({type(e).__name__}"
            f"{': ' + cname if cname else ''}). Pārbaudi laukus anketā. "
            f"{str(e).splitlines()[0][:200]}",
        )
    except psycopg.errors.DataError as e:
        raise HTTPException(400, f"Nederīga lauka vērtība: {str(e).splitlines()[0][:250]}")


# ---------------------------------------------------------------------------
# 7d) IMPORTS NO PDF — analīze (bez DB) + avota PDF atdošana (sk. pdf_import.py)
# ---------------------------------------------------------------------------

@router.post("/import/analyze")
def import_analyze(
    file: UploadFile = File(...),
    wp_user_id: int = Form(0),
    _auth: None = Depends(require_token),
) -> dict:
    """PDF → AI → anketas FormState (building + units). DB NETIEK rakstīta.
    Fails paliek `agent_imports/<token>/` līdz /agent/publish (intent='import')."""
    import pdf_import
    if not file.filename:
        raise HTTPException(400, "Filename trūkst")
    data = file.file.read()
    try:
        return pdf_import.analyze_pdf(data, file.filename, wp_user_id)
    except pdf_import.PdfImportError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"PDF imports neizdevās: {type(e).__name__}: {str(e)[:300]}")


# ---------------------------------------------------------------------------
# 7e) LISTINGA PDF DOKUMENTI (Raimonds 2026-10-20): pie listinga var glabāt VAIRĀKUS
#     PDF — īpašnieka atsūtīto (avots) + savus (savs dizains/vizualizācijas).
#     Glabā `listings/<id>/docs/<uuid>__<nosaukums>.pdf` + pdf_jobs «done» rindu
#     (tā pati infrastruktūra, ko lieto panelis «Lejupielādēt» un LP «PDF faili» →
#     bez jaunas tabulas/migrācijas). Nosaukums = faila vārds pēc "__".
# ---------------------------------------------------------------------------

_DOC_MAX_BYTES = 25 * 1024 * 1024


def _safe_doc_name(name: str) -> str:
    import re as _re
    base = Path(name or "dokuments.pdf").name
    base = _re.sub(r"[^\w.\-() ]+", "_", base, flags=_re.UNICODE).strip() or "dokuments.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base[:100]


@router.post("/listing-doc-upload/{listing_id}")
def listing_doc_upload(
    listing_id: int,
    file: UploadFile = File(...),
    requested_by: str = Form(""),
    _auth: None = Depends(require_token),
) -> dict:
    """Augšupielādē PDF pie listinga → volume + pdf_jobs 'done' rinda. Atgriež job_id."""
    if not file.filename:
        raise HTTPException(400, "Filename trūkst")
    data = file.file.read()
    if not data or not data[:5].startswith(b"%PDF"):
        raise HTTPException(400, "Fails nav PDF")
    if len(data) > _DOC_MAX_BYTES:
        raise HTTPException(400, "PDF pārāk liels (max 25 MB)")
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM properties.listings WHERE id = %s", (listing_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"Listings {listing_id} nav atrasts")
        safe = _safe_doc_name(file.filename)
        rel_dir = Path("listings") / str(listing_id) / "docs"
        (STORAGE_ROOT / rel_dir).mkdir(parents=True, exist_ok=True)
        rel = (rel_dir / f"{uuid.uuid4().hex}__{safe}").as_posix()
        (STORAGE_ROOT / rel).write_bytes(data)
        cur.execute(
            """INSERT INTO properties.pdf_jobs
                   (listing_ids, status, requested_by, requested_at, started_at,
                    finished_at, file_path, attempts)
               VALUES (%s, 'done', %s, now(), now(), now(), %s, 0)
               RETURNING id""",
            ([listing_id], requested_by or None, rel),
        )
        job_id = int(cur.fetchone()["id"])
        conn.commit()
    return {"ok": True, "job_id": job_id, "file_path": rel, "name": safe,
            "size": len(data)}


@router.post("/listing-doc-delete/{job_id}")
def listing_doc_delete(job_id: int, _auth: None = Depends(require_token)) -> dict:
    """Dzēš pievienoto PDF (tikai `listings/<id>/docs/` vai `listings/<id>/source/`
    failus — brošūras no rindas `pdf_jobs/<id>.pdf` šeit NEdzēš). Noņem arī pdf_jobs rindu;
    LP `pdf_on_lp` markeris paliek (bez faila LP to vienkārši vairs nerāda)."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, file_path, listing_ids FROM properties.pdf_jobs WHERE id = %s",
                    (job_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "PDF nav atrasts")
        rel = str(row.get("file_path") or "").replace("\\", "/").lstrip("/")
        parts = rel.split("/")
        if not (len(parts) == 4 and parts[0] == "listings" and parts[2] in ("docs", "source")
                and ".." not in parts):
            raise HTTPException(400, "Šo PDF šeit dzēst nevar (nav pievienots dokuments)")
        path = STORAGE_ROOT / rel
        try:
            if path.is_file():
                path.unlink()
        except Exception as e:
            raise HTTPException(500, f"Faila dzēšana neizdevās: {e}")
        if parts[2] == "source":
            # Avota PDF — notīra arī listinga atsauci, lai poga/sadaļa to vairs nerāda.
            try:
                cur.execute(
                    """UPDATE properties.listings
                          SET source_file_path = NULL, source_file_name = NULL
                        WHERE source_file_path = %s""",
                    (rel,),
                )
            except Exception:
                conn.rollback()
        cur.execute("DELETE FROM properties.pdf_jobs WHERE id = %s", (job_id,))
        conn.commit()
    return {"ok": True, "job_id": job_id}


# ---------------------------------------------------------------------------
# 7f) PARTNERU PIEDĀVĀJUMU IEPLŪDE (partner_intake.py): saraksts, reports, apstiprināšana
# ---------------------------------------------------------------------------

@router.get("/partner-intake")
def partner_intake_list(limit: int = 50, _auth: None = Depends(require_token)) -> dict:
    import partner_intake
    return {"ok": True, "items": partner_intake.list_intakes(limit), "status": partner_intake.get_status()}


@router.get("/partner-intake/{intake_id}")
def partner_intake_get(intake_id: str, _auth: None = Depends(require_token)) -> dict:
    import partner_intake
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", intake_id):
        raise HTTPException(400, "Nederīgs intake_id")
    d = partner_intake.INTAKE_DIR / intake_id
    if not (d / "meta.json").is_file():
        raise HTTPException(404, "Nav tāda piedāvājuma")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    report = json.loads((d / "report.json").read_text(encoding="utf-8")) if (d / "report.json").is_file() else None
    body = (d / "body.txt").read_text(encoding="utf-8")[:20000] if (d / "body.txt").is_file() else ""
    return {"ok": True, "meta": meta, "report": report, "body": body}


@router.get("/partner-intake/{intake_id}/file/{rel:path}")
def partner_intake_file(intake_id: str, rel: str, w: int = 0, _auth: None = Depends(require_token)):
    """Pielikums (att/<fails>) vai izvilkta bilde (img/<idx>/<fails>) priekšskatam panelī.
    ?w=320 → sīktēls (kešots _thumbs/) — pilnas Drive bildes (300 KB+) × 14 telpai lika panelim karāties."""
    import partner_intake
    from fastapi.responses import FileResponse
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", intake_id) or ".." in rel.split("/"):
        raise HTTPException(400, "Nederīgs ceļš")
    if Path(rel).is_absolute() or rel.startswith(("/", "\\")) or "\\" in rel or ":" in rel:
        raise HTTPException(400, "Nederīgs ceļš")
    base = (partner_intake.INTAKE_DIR / intake_id).resolve()
    p = (base / rel).resolve()
    # tikai šīs ieplūdes att/ un img/ (zaļās audits 2026-10-21: absolūts rel izlēca no mapes)
    if base not in p.parents or p.relative_to(base).parts[0] not in ("att", "img", "uploads", "links"):
        raise HTTPException(400, "Nederīgs ceļš")
    if not p.is_file():
        raise HTTPException(404, "Fails nav")
    if w and 64 <= w <= 1024 and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        tp = partner_intake.make_thumb(base, rel, w)
        if tp:
            return FileResponse(str(tp), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400, immutable"})
    return FileResponse(str(p))


class PartnerApplyReq(BaseModel):
    decisions: dict[str, Any]
    requested_by: Optional[str] = None
    wp_user_id: int = 0
    dry_run: bool = False   # tikai parāda, ko rakstītu (agrāk lauks tika ignorēts → «dry» apply rakstīja DB, 2026-10-26)


@router.post("/partner-intake/{intake_id}/apply")
def partner_intake_apply(intake_id: str, req: PartnerApplyReq, _auth: None = Depends(require_token)) -> dict:
    import partner_intake
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", intake_id):
        raise HTTPException(400, "Nederīgs intake_id")
    try:
        return {"ok": True, **partner_intake.apply(intake_id, req.decisions, req.requested_by, req.wp_user_id, dry_run=req.dry_run)}
    except partner_intake.ApplyError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError:
        raise HTTPException(404, "Reports nav gatavs")
    except Exception as e:
        raise HTTPException(500, f"Apstiprināšana neizdevās: {type(e).__name__}: {str(e)[:300]}")


class PartnerRollbackReq(BaseModel):
    requested_by: Optional[str] = None


@router.post("/partner-intake/{intake_id}/rollback")
def partner_intake_rollback(intake_id: str, req: PartnerRollbackReq, _auth: None = Depends(require_token)) -> dict:
    """Atsaukt apstiprināšanu: dzēš izveidotos listingus (WP+bildes+DB caur queue), meta → ready."""
    import partner_intake
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", intake_id):
        raise HTTPException(400, "Nederīgs intake_id")
    try:
        return {"ok": True, **partner_intake.rollback(intake_id, req.requested_by)}
    except FileNotFoundError:
        raise HTTPException(404, "Ieplūde nav atrasta")
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}")


@router.post("/partner-intake/{intake_id}/reprocess")
def partner_intake_reprocess(intake_id: str, _auth: None = Depends(require_token)) -> dict:
    """Pārlaist AI + salīdzināšanu (piem. pēc partnera profila/ēkas labojuma)."""
    import partner_intake
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", intake_id):
        raise HTTPException(400, "Nederīgs intake_id")
    try:
        rep = partner_intake.process_intake(intake_id, do_notify=False)
        return {"ok": True, "summary": rep["summary"]}
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}")


class AddressOverrideReq(BaseModel):
    addr_idx: int
    bp_id: Optional[int] = None   # None = noņemt sasaisti


@router.post("/partner-intake/{intake_id}/address")
def partner_intake_address(intake_id: str, req: AddressOverrideReq, _auth: None = Depends(require_token)) -> dict:
    """Aģents sasaista piedāvājuma adresi ar DB ēku → salīdzināšana no jauna (bez AI)."""
    import partner_intake
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", intake_id):
        raise HTTPException(400, "Nederīgs intake_id")
    try:
        return {"ok": True, **partner_intake.address_override(intake_id, req.addr_idx, req.bp_id)}
    except partner_intake.ApplyError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}")


@router.get("/buildings/search")
def buildings_search(q: str = "", limit: int = 12, _auth: None = Depends(require_token)) -> dict:
    """Ēku meklēšana (adrese/nosaukums/pilsēta, bez diakritikas) — panelim ēkas izvēlei."""
    import partner_intake
    with _db() as conn:
        conn.read_only = True
        return {"ok": True, "items": partner_intake.search_buildings(conn, q, max(1, min(30, limit)))}


@router.post("/partner-intake/{intake_id}/images/{listing_id}")
def partner_intake_images(intake_id: str, listing_id: int, _auth: None = Depends(require_token)) -> dict:
    """Pārlaist bilžu soli (ss.lv + PDF + fasāde → AI) vienam jau izveidotam listingam."""
    import partner_intake
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", intake_id):
        raise HTTPException(400, "Nederīgs intake_id")
    try:
        return {"ok": True, **partner_intake.reprocess_listing_images(intake_id, listing_id)}
    except partner_intake.ApplyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}")


_INTAKE_ID_RE = r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}"


@router.get("/partner-intake/{intake_id}/unit-images/{idx}")
def partner_intake_unit_images(intake_id: str, idx: str, pick: Optional[str] = None, _auth: None = Depends(require_token)) -> dict:
    """Importa stadija: bilžu kandidāti telpai (ss.lv galerija, PDF, augšupielādes, ēkas bildes).
    ?pick=<ss.lv links> — aģents izvēlas citu ss.lv sludinājumu (galeriju ielādē uzreiz)."""
    import partner_intake
    if not re.fullmatch(_INTAKE_ID_RE, intake_id) or not re.fullmatch(r"\d{1,4}", idx):
        raise HTTPException(400, "Nederīgs ceļš")
    if pick and not re.fullmatch(r"(https?://(www\.)?)?ss\.(lv|com)/msg/[A-Za-z0-9_./-]+\.html", pick):
        raise HTTPException(400, "Nederīgs ss.lv links")
    try:
        return partner_intake.unit_image_candidates(intake_id, idx, pick=pick)
    except FileNotFoundError:
        raise HTTPException(404, "Reports nav gatavs")
    except partner_intake.ApplyError as e:
        raise HTTPException(404, str(e))


@router.post("/partner-intake/{intake_id}/unit-upload/{idx}")
def partner_intake_unit_upload(intake_id: str, idx: str, files: list[UploadFile] = File(default=[]),
                               _auth: None = Depends(require_token)) -> dict:
    import partner_intake
    if not re.fullmatch(_INTAKE_ID_RE, intake_id) or not re.fullmatch(r"\d{1,4}", idx):
        raise HTTPException(400, "Nederīgs ceļš")
    if not (partner_intake.INTAKE_DIR / intake_id / "report.json").is_file():
        raise HTTPException(404, "Ieplūde nav atrasta")
    saved, errors = [], []
    for f in files[:30]:
        try:
            saved.append(partner_intake.save_unit_upload(intake_id, idx, f.filename or "", f.file.read()))
        except partner_intake.ApplyError as e:
            errors.append(f"{f.filename}: {e}")
    return {"ok": True, "saved": saved, "errors": errors}


@router.delete("/partner-intake/{intake_id}/unit-upload/{idx}/{fn}")
def partner_intake_unit_upload_delete(intake_id: str, idx: str, fn: str, _auth: None = Depends(require_token)) -> dict:
    import partner_intake
    if (not re.fullmatch(_INTAKE_ID_RE, intake_id) or not re.fullmatch(r"\d{1,4}", idx)
            or not re.fullmatch(r"up_[0-9a-f]{32}\.(jpg|jpeg|png|webp)", fn)):
        raise HTTPException(400, "Nederīgs ceļš")
    p = partner_intake.INTAKE_DIR / intake_id / "uploads" / idx / fn
    p.unlink(missing_ok=True)
    return {"ok": True}


# ── Ēkas noklusējuma bildes (fasāde/pagalms) ──

@router.get("/building-photos/{bp_id}")
def building_photos_list(bp_id: int, _auth: None = Depends(require_token)) -> dict:
    import building_photos
    return {"bp_id": bp_id, "photos": building_photos.list_photos(bp_id)}


@router.get("/building-photos/{bp_id}/file/{fn}")
def building_photos_file(bp_id: int, fn: str, _auth: None = Depends(require_token)):
    import building_photos
    from fastapi.responses import FileResponse
    p = building_photos.file_path(bp_id, fn)
    if not p:
        raise HTTPException(404, "Nav")
    return FileResponse(str(p))


@router.post("/building-photos/{bp_id}")
def building_photos_upload(bp_id: int, files: list[UploadFile] = File(default=[]), ptype: str = Form("fasade"),
                           by: str = Form(""), _auth: None = Depends(require_token)) -> dict:
    import building_photos
    with psycopg.connect(DATABASE_URL) as conn:
        if not conn.execute("SELECT 1 FROM properties.building_profiles WHERE id=%s", (bp_id,)).fetchone():
            raise HTTPException(404, "Ēka nav atrasta")
    added, errors = [], []
    for f in files[:30]:
        try:
            added.append(building_photos.add_photo(bp_id, f.file.read(), f.filename or "", ptype, by or None))
        except ValueError as e:
            errors.append(f"{f.filename}: {e}")
    return {"ok": True, "added": added, "errors": errors}


class BuildingPhotoTypeReq(BaseModel):
    ptype: str


@router.post("/building-photos/{bp_id}/type/{fn}")
def building_photos_set_type(bp_id: int, fn: str, req: BuildingPhotoTypeReq, _auth: None = Depends(require_token)) -> dict:
    import building_photos
    try:
        return {"ok": building_photos.set_type(bp_id, fn, req.ptype)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/building-photos/{bp_id}/{fn}")
def building_photos_delete(bp_id: int, fn: str, _auth: None = Depends(require_token)) -> dict:
    import building_photos
    return {"ok": building_photos.delete_photo(bp_id, fn)}


class BuildingPhotosApplyReq(BaseModel):
    requested_by: Optional[str] = None


@router.post("/building-photos/{bp_id}/apply")
def building_photos_apply(bp_id: int, req: BuildingPhotosApplyReq, _auth: None = Depends(require_token)) -> dict:
    """Ēkas bildes visiem listingiem ēkā, kuriem NAV fasādes (mājaslapā esošos pārpublicē)."""
    import building_photos
    from psycopg.rows import dict_row
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        h = building_photos.harvest_from_listings(conn, bp_id)
        return {"ok": True, "harvested": h.get("added", 0), **building_photos.apply_to_building(conn, bp_id, req.requested_by)}


class BuildingPhotosApplyListingReq(BaseModel):
    fns: list[str]


@router.post("/building-photos/{bp_id}/apply-listing/{listing_id}")
def building_photos_apply_listing(bp_id: int, listing_id: int, req: BuildingPhotosApplyListingReq,
                                  _auth: None = Depends(require_token)) -> dict:
    """Listinga redaktors «No ēkas bildēm»: izvēlētās ēkas bildes → listinga beigās (tīras, bez Seedream)."""
    import building_photos
    from psycopg.rows import dict_row
    if not req.fns:
        raise HTTPException(400, "Nav izvēlētu bilžu")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        r = conn.execute("SELECT building_profile_id FROM properties.listings WHERE id=%s", (listing_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Listings nav")
        return {"ok": True, **building_photos.apply_to_listing(conn, listing_id, bp_id, "always", req.fns, position="end")}


@router.post("/building-photos/{bp_id}/harvest")
def building_photos_harvest(bp_id: int, _auth: None = Depends(require_token)) -> dict:
    """Savāc ēkas listingu jau atpazītās fasādes ēkas bibliotēkā (bez AI izmaksām)."""
    import building_photos
    from psycopg.rows import dict_row
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        return {"ok": True, **building_photos.harvest_from_listings(conn, bp_id)}


class HarvestAllReq(BaseModel):
    dry_run: bool = True
    limit: Optional[int] = None


_HARVEST_STATE: dict = {"running": False}


@router.post("/building-photos-harvest-all")
def building_photos_harvest_all(req: HarvestAllReq, _auth: None = Depends(require_token)) -> dict:
    """Masveidā visām ēkām (fonā). dry_run=True — tikai saskaita. Statuss: GET tas pats ceļš."""
    import threading
    import building_photos
    from psycopg.rows import dict_row
    if _HARVEST_STATE.get("running"):
        raise HTTPException(409, "Jau notiek")

    def _run():
        _HARVEST_STATE.update({"running": True, "started": __import__("datetime").datetime.now().isoformat(), "result": None, "error": None})
        try:
            with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
                _HARVEST_STATE["result"] = building_photos.harvest_all(conn, req.dry_run, req.limit)
        except Exception as e:
            _HARVEST_STATE["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        finally:
            _HARVEST_STATE["running"] = False

    threading.Thread(target=_run, daemon=True, name="bp-harvest-all").start()
    return {"ok": True, "started": True, "dry_run": req.dry_run}


@router.get("/building-photos-harvest-all")
def building_photos_harvest_all_status(_auth: None = Depends(require_token)) -> dict:
    return dict(_HARVEST_STATE)


class PartnerUploadReq(BaseModel):
    pass


@router.post("/partner-intake/upload")
def partner_intake_upload(
    files: list[UploadFile] = File(default=[]),
    body_text: str = Form(""),
    subject: str = Form("Panelī ielikts piedāvājums"),
    forwarder: str = Form(""),
    _auth: None = Depends(require_token),
) -> dict:
    """Rezerves ceļš bez e-pasta: aģents panelī iemet failus + ielīmē tekstu."""
    import partner_intake
    atts = []
    for f in files:
        data = f.file.read()
        if data and f.filename:
            atts.append({"name": partner_intake._safe_name(f.filename), "type": f.content_type or "", "data": data})
    if not atts and len(body_text.strip()) < 20:
        raise HTTPException(400, "Nav ne failu, ne teksta")
    parsed = {"message_id": f"panel:{uuid.uuid4().hex}", "forwarder_name": "", "forwarder_email": forwarder,
              "subject": subject, "body": body_text, "orig_sender": partner_intake._orig_sender_from_text(body_text),
              "attachments_raw": atts, "date": ""}
    meta = partner_intake._store_intake(parsed, "panel")
    try:
        rep = partner_intake.process_intake(meta["intake_id"], do_notify=False)
        return {"ok": True, "intake_id": meta["intake_id"], "summary": rep["summary"]}
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}")


# ---------------------------------------------------------------------------
# 7g) PARTNERU DRIVE MAPJU AUTOMĀTISKA SKENĒŠANA (partner_drive.py): avoti + «Skenēt tagad»
# ---------------------------------------------------------------------------

class DriveSourceReq(BaseModel):
    id: Optional[str] = None
    name: str
    folder: str                      # Drive mapes saite vai ID
    price_files: list[str] = Field(default_factory=list)   # cenu failu saites/ID
    agent_email: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    notes: Optional[str] = None
    schedule: Optional[str] = None   # daily | weekly
    enabled: Optional[bool] = None


@router.get("/partner-drive/sources")
def partner_drive_sources(_auth: None = Depends(require_token)) -> dict:
    import partner_drive
    return {"ok": True, "items": partner_drive.load_sources()}


@router.post("/partner-drive/sources")
def partner_drive_upsert(req: DriveSourceReq, _auth: None = Depends(require_token)) -> dict:
    import partner_drive
    data = req.model_dump(exclude_none=True)
    try:
        return {"ok": True, "source": partner_drive.upsert_source(data)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/partner-drive/sources/{sid}")
def partner_drive_delete(sid: str, _auth: None = Depends(require_token)) -> dict:
    import partner_drive
    if not partner_drive.delete_source(sid):
        raise HTTPException(404, "Nav tāda avota")
    return {"ok": True}


@router.post("/partner-drive/scan/{sid}")
def partner_drive_scan(sid: str, force: bool = False, _auth: None = Depends(require_token)) -> dict:
    """Skenē tūlīt FONĀ (atbild uzreiz; panelis seko `scanning`/`last_result` avotu sarakstā).
    force=1 → ieplūde arī tad, ja nekas nav mainījies (pirmais tests)."""
    import partner_drive
    try:
        return {"ok": True, **partner_drive.start_scan(sid, force=force)}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}")


@router.get("/listing-source-file/{listing_id}")
def listing_source_file(listing_id: int, _auth: None = Depends(require_token)):
    """Importētā listinga AVOTA PDF (oriģinālā brošūra) no volume."""
    from fastapi.responses import Response
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source_file_path, source_file_name FROM properties.listings WHERE id = %s",
            (listing_id,),
        )
        row = cur.fetchone()
    if not row or not row.get("source_file_path"):
        raise HTTPException(404, f"Listingam {listing_id} nav avota PDF")
    rel = str(row["source_file_path"]).replace("\\", "/").lstrip("/")
    if ".." in rel.split("/") or not rel.startswith("listings/"):
        raise HTTPException(400, "Nederīgs faila ceļš")
    path = STORAGE_ROOT / rel
    if not path.is_file():
        raise HTTPException(410, "Avota PDF fails nav uz volume")
    name = (row.get("source_file_name") or f"source_{listing_id}.pdf").replace('"', "")
    return Response(
        content=path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )
