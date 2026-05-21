"""ss-to-wp-worker — FastAPI wrapper ss.lv → WP konvertēšanai.

Railway serviss (Melnās kastes funkcionalitāte), kas izsauc esošos
publish_to_wp / image_pipeline / image_classify / image_enhance_openai
skriptus caur HTTP endpoint-iem. Volume `inbox-to-listings-volume`
piemontēts uz `/storage` (kā citiem servisiem) → ss.lv raw bildes JAU
pieejamas, NEIET uz ss.lv.

Endpoints (visi prasa `X-RGC-Token` header — tā pati shared-secret, kas
WP v5 plugin-am):

  GET  /health                       — bez auth, status check
  POST /publish/{listing_id}         — pilns pipeline: image_pipeline +
                                       classify + publish_to_wp (sync,
                                       atbilde pēc pabeigšanas)
  POST /classify/{listing_id}        — tikai bilžu klasifikators (cheap)
  POST /enhance-openai/{listing_id}  — selektīvi OpenAI gpt-image-1 uz
                                       not_good_for_website bildēm

Body params (visi POST):
  force: bool (default false)        — pārpublicē/pārapstrādā
  dry_run: bool (default false, tikai publish) — neraksta uz WP

Saskaņots 2026-05-21 (Raimonds): atsevišķs repo (NE pievienots inbox-to-
listings), lai katram skriptam savs push cikls.
"""
from __future__ import annotations

import io
import os
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Annotated, Optional

import psycopg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from psycopg.rows import dict_row
from pydantic import BaseModel

# Failure-tolerant local imports — skripti atrodas tajā pašā mapē
sys.path.insert(0, str(Path(__file__).parent))
import image_classify  # noqa: E402
import image_enhance_openai  # noqa: E402
import pdf_maker  # noqa: E402
import publish_to_wp  # noqa: E402

load_dotenv(Path(__file__).parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
RGC_MK_TOKEN = os.getenv("RGC_MK_TOKEN")  # Auth header pārbaude
SERVICE_NAME = "ss-to-wp-worker"
SERVICE_VERSION = "0.1.0"

app = FastAPI(
    title=SERVICE_NAME,
    version=SERVICE_VERSION,
    description=__doc__,
)


# ---------- Auth ----------

def require_token(
    x_rgc_token: Annotated[Optional[str], Header(alias="X-RGC-Token")] = None,
) -> None:
    if not RGC_MK_TOKEN:
        raise HTTPException(500, "Service nav konfigurēts (RGC_MK_TOKEN)")
    if not x_rgc_token or x_rgc_token != RGC_MK_TOKEN:
        raise HTTPException(403, "Trūkst derīga X-RGC-Token header")


# ---------- Schemas ----------

class PublishRequest(BaseModel):
    force: bool = False
    dry_run: bool = False
    skip_ai: bool = False


class ClassifyRequest(BaseModel):
    force: bool = False
    images: Optional[list[str]] = None  # piem. ["img_002.jpg"]


class EnhanceRequest(BaseModel):
    quality: str = "medium"  # low|medium|high
    images: Optional[list[str]] = None
    force: bool = False


# ---------- Helpers ----------

def _capture(fn, *args, **kwargs) -> dict:
    """Palaiž zvanu un savāc print izvadi atpakaļ klientam. Bāzes
    pieeja, kamēr nav īstas async background queue."""
    buf = io.StringIO()
    err = None
    try:
        with redirect_stdout(buf):
            result = fn(*args, **kwargs)
    except SystemExit as e:
        err = f"SystemExit: {e}"
        result = None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        traceback.print_exc(file=buf)
        result = None
    return {
        "ok": err is None,
        "error": err,
        "result": result,
        "log": buf.getvalue(),
    }


# ---------- Endpoints ----------

@app.get("/health")
def health():
    storage = publish_to_wp.STORAGE_ROOT
    storage_ok = storage.is_dir()
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "storage_root": str(storage),
        "storage_exists": storage_ok,
        "has_database_url": bool(DATABASE_URL),
        "has_token": bool(RGC_MK_TOKEN),
    }


@app.post("/publish/{listing_id}", dependencies=[Depends(require_token)])
def publish(listing_id: int, body: PublishRequest = PublishRequest()):
    """Pilns pipeline. SYNC — Broker Panel poga waitēs atbildi.
    Tipisks ilgums: 1-10 min atkarīgi no bilžu skaita (Seedream ~30s/bilde)."""
    out = _capture(publish_to_wp.publish, listing_id,
                   dry_run=body.dry_run, force=body.force,
                   skip_ai=body.skip_ai)
    if not out["ok"]:
        raise HTTPException(500, out)
    return out


@app.post("/classify/{listing_id}", dependencies=[Depends(require_token)])
def classify(listing_id: int, body: ClassifyRequest = ClassifyRequest()):
    """Tikai bilžu klasifikators (gpt-4o-mini vision, ~$0.001/bilde).
    Manifests glabājas storage/listings/<id>/_image_manifest.json."""
    storage = publish_to_wp.STORAGE_ROOT
    out = _capture(image_classify.ensure_classified, storage, listing_id,
                   None, body.force, body.images)
    if not out["ok"]:
        raise HTTPException(500, out)
    return out


@app.post("/enhance-openai/{listing_id}",
          dependencies=[Depends(require_token)])
def enhance_openai(listing_id: int,
                   body: EnhanceRequest = EnhanceRequest()):
    """Selektīvi OpenAI gpt-image-1 uz not_good_for_website bildēm
    (vai konkrētām, ja `images` dots). Quality: low|medium|high."""
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL nav konfigurēts")
    out = _capture(_enhance_inner, listing_id, body.images, body.quality,
                   body.force)
    if not out["ok"]:
        raise HTTPException(500, out)
    return out


def _enhance_inner(listing_id: int, images: Optional[list[str]],
                   quality: str, force: bool) -> dict:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        return image_enhance_openai.enhance_listing(
            conn, listing_id, images, quality, force, dry_run=False)


@app.post("/pdf/{listing_id}", dependencies=[Depends(require_token)])
def pdf(listing_id: int):
    """Ģenerē RGC sludinājuma PDF brošūru (1 īpašums). Atgriež PDF failu
    (application/pdf). Broker Panel poga "Izveidot PDF" izsauks šo."""
    try:
        pdf_bytes = pdf_maker.render_pdf(listing_id)
    except SystemExit as e:
        raise HTTPException(500, f"PDF kļūda: {e}")
    except Exception as e:
        raise HTTPException(500, f"PDF kļūda: {type(e).__name__}: {e}")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition":
                 f'inline; filename="listing_{listing_id}.pdf"'},
    )
