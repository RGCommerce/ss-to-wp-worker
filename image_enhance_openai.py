"""OpenAI gpt-image-1 — SELEKTĪVA bilžu uzlabošana sliktajām bildēm.

PAPILD-skripts pie `image_pipeline.py` (Seedream = primārais, ekonomiskais).
Lieto tikai bildēm, kuras Seedream izvade ir par sudīgu un vajag "WOW"
pārbūvi: pilna regen ar OpenAI gpt-image-1 (~$0.05 medium / ~$0.19 high).

Plūsma:
  1. Identificē listing + (opcionāli) konkrētu raw bilžu sarakstu, kas
     jāapstrādā ar OpenAI vietā Seedream izvades.
  2. Katrai izvēlētajai raw bildei: `images/edits` ar gpt-image-1 (pilna
     pārbūve, noņem ss.com ūdenszīmi pats, "izmazgā" telpu tīru, BET
     nepārvieto/nemaina lietas).
  3. Pārraksta `ai_ready/img_NNN.jpg` (publish_to_wp tagad augšuplādēs šo
     jauno versiju). Faila vārds tas pats → `local_image_paths_processed`
     DB ierakstā nemainās.

Lietošana:
  python crm/image_enhance_openai.py --listing 13
  python crm/image_enhance_openai.py --listing 13 --images img_002.jpg,img_003.jpg
  python crm/image_enhance_openai.py --listing 13 --quality high
  python crm/image_enhance_openai.py --listing 13 --dry-run

Vide (crm/.env):
  DATABASE_URL              — Railway DB
  OPENAI_API_KEY            — sk-...
  STORAGE_ROOT              — default ./storage lokāli, /storage uz Railway
  OPENAI_IMAGE_MODEL        — default gpt-image-1
  OPENAI_IMAGE_QUALITY      — low|medium|high (default medium)
  OPENAI_IMAGE_SIZE         — default 1536x1024 (landscape NĪ)
  IMAGE_PROMPT              — opcionāli pārraksta default prompt

Nākotnē (Etap-2 plāns, Raimonds 2026-05-20): AI parse atzīmēs katru raw
bildi kā `good_for_website` vai `not_good_for_website`. Slikti atzīmētās
automātiski iet caur ŠO skriptu (selektīvi); pārējās paliek Seedream
izvadē. Bet šobrīd palaišana = manuāla.
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import time
from pathlib import Path

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.rows import dict_row

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Lietojam image_pipeline helperus, lai neduplicētu DB/storage loģiku
sys.path.insert(0, str(Path(__file__).parent))
import image_pipeline as ip  # noqa: E402

load_dotenv(Path(__file__).parent / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
OPENAI_IMAGE_QUALITY = os.getenv("OPENAI_IMAGE_QUALITY", "medium")
OPENAI_IMAGE_SIZE = os.getenv("OPENAI_IMAGE_SIZE", "1536x1024")

# Aptuvenās cenas (gpt-image-1, 2026-05): low ~$0.015, medium ~$0.05,
# high ~$0.19 (1536x1024). Lieto kopsavilkuma izdrukai.
_COST_BY_QUALITY = {"low": 0.015, "medium": 0.05, "high": 0.19, "auto": 0.05}

# PROMPTU EVOLŪCIJA (Raimonda iterācijas 2026-05-20):
# v1-v2: "transform into magazine-quality" — par WOW, nereāli idealizēts
# v3: "pro fotosesija ar studio strobes + platleņķi" — labāks, bet
#     izskatās it kā telpa relit, ne īsts skats. Patur prātā kā fallback
#     ja vēlāk vajag spilgtākas bildes ar acīmredzami studio gaismu.
# v4 (PAŠREIZ): "tā pati bilde, šauta ar dārgu DSLR no statīva" —
#     uzvarētājs (Raimonds 2026-05-20 nakts). 3/4 Duntes bildēm perfekti
#     faithful, asas, augstas izšķirtspējas, dabīga gaisma. Vienīgais
#     trūkums: dimmīgi raw paliek dimmīgi (img_004). Risināms vēlāk ar
#     mērenu shadow-lift pielāgojumu vai selektīvi v3 dimmīgajām.

_PROMPT_V3_PHOTO_SESSION = (  # fallback ja vajag spilgtu studio-stila
    "Imagine this real-estate listing photo is being taken by a top "
    "professional real-estate photographer during a proper photo session. "
    "The photographer briefly tidied the space (dust wiped off, surface "
    "grime cleaned, small mess cleared) but did not restore, refurbish, "
    "repaint or polish anything. Set up professional lighting (strobes/"
    "fill flashes complement existing ceiling lights) — evenly lit, soft "
    "shadows, balanced exposure, natural daylight white balance. Lens: "
    "moderate wide-angle (18-22mm equivalent, no fisheye distortion). "
    "Remove the small \"SS.com\" overlay text in the corners. "
    "ABSOLUTE CONSTRAINTS: same room, same architecture, same objects in "
    "their exact positions — no restage, no refurbish."
)

DEFAULT_PROMPT = (  # v4 — "tā pati bilde, dārga kamera"
    "This is the SAME real-estate photo of THIS exact room — do not "
    "regenerate it as a new photo. Imagine the same person was standing "
    "in the exact same spot, at the exact same time of day, and instead "
    "of taking this photo with a low-resolution phone, they took it with "
    "a top professional full-frame DSLR camera on a tripod. Same camera "
    "position, same lens angle, same field of view, same time of day, "
    "same natural lighting and shadows, same overall colours and "
    "atmosphere — just MUCH higher quality.\n\n"
    "The result is: razor-sharp pixel-level detail throughout, very high "
    "resolution, no JPEG compression artifacts, no digital noise, deeply "
    "detailed textures (wall paint, floor grain, fabric, metal, wood, "
    "etc.). Highlights, shadows and exposure are essentially the same as "
    "the original — do not brighten the scene dramatically, do not "
    "relight it, do not change the mood, do not stage new lamps, strobes "
    "or flashes. Keep the original lighting almost exactly as it was, "
    "only with the dynamic range a proper sensor would capture (a touch "
    "less blown-out highlights, a touch more shadow detail — natural, "
    "not HDR, not over-processed).\n\n"
    "Light cleaning is allowed: surface dust on the floor and shelves can "
    "be gone, scuff marks and grime on the walls can be wiped, any tiny "
    "rubbish on the floor can disappear. But nothing is restored, "
    "refurbished, repainted, sanded, polished or buffed. Walls keep "
    "their original colour, hue and texture; floor keeps its original "
    "colour and texture; no mirror shine, no glossing up, no idealising, "
    "no whitening of off-white walls.\n\n"
    "Also remove the small \"SS.com\" overlay text in the corners "
    "completely and seamlessly.\n\n"
    "ABSOLUTE CONSTRAINTS: same room, same architecture, same room "
    "shape, same perspective, same camera angle and framing. Same number "
    "and position of windows, doors, columns, ceiling beams and wall "
    "partitions. Every existing piece of furniture, every shelving unit, "
    "every chair, cabinet, radiator, plant, box and item stays in its "
    "exact same position, with the same shape, size, colour, material "
    "and identity. Do not add, remove, move, replace, restage, restyle, "
    "refurbish, repaint or reupholster anything. Do not change the floor "
    "material or the wall colour. Do not invent new rooms or alter the "
    "building. This is not a new photo — it is the SAME photo, taken "
    "with a far better camera."
)
PROMPT = os.getenv("IMAGE_PROMPT", DEFAULT_PROMPT)

# SSL verify slēdzis (tāpat kā image_pipeline)
_VERIFY = os.getenv("VERIFY_SSL", os.getenv("WP_VERIFY_SSL", "1")) \
    not in ("0", "false", "False")


def openai_edit(image_bytes: bytes, filename: str, quality: str) -> bytes | None:
    """OpenAI gpt-image-1 image edit — pilna pārbūve. Atgriež JPEG/PNG
    baitus vai None ja kļūda. gpt-image-1 vienmēr atgriež b64_json."""
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                "https://api.openai.com/v1/images/edits",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                data={
                    "model": OPENAI_IMAGE_MODEL,
                    "prompt": PROMPT,
                    "size": OPENAI_IMAGE_SIZE,
                    "quality": quality,
                    "n": 1,
                },
                files={"image": (filename, io.BytesIO(image_bytes),
                                 "image/jpeg")},
                timeout=300,
                verify=_VERIFY,
            )
        except requests.RequestException as e:
            print(f"      ! OpenAI tīkla kļūda: {str(e)[:160]}")
            if attempt < 3:
                time.sleep(5 * attempt)
                continue
            return None
        if resp.status_code == 429:
            print("      … OpenAI 429 (rate limit), gaidu 20s")
            time.sleep(20)
            continue
        if resp.status_code != 200:
            print(f"      ! OpenAI HTTP {resp.status_code}: "
                  f"{resp.text[:300]}")
            return None
        try:
            b64 = resp.json()["data"][0]["b64_json"]
            return base64.b64decode(b64)
        except Exception as e:
            print(f"      ! OpenAI atbildes parse: {str(e)[:160]}")
            return None
    return None


def _select_raw_files(raw_files: list[Path], filter_names: list[str] | None
                      ) -> list[Path]:
    """Atstāj tikai tās raw bildes, kuru fails (img_NNN.jpg) ir filter_names."""
    if not filter_names:
        return raw_files
    wanted = {n.strip().lower() for n in filter_names if n.strip()}
    return [p for p in raw_files if p.name.lower() in wanted]


def enhance_listing(conn, listing_id: int, image_filter: list[str] | None,
                    quality: str, force: bool, dry_run: bool) -> dict:
    listing = ip.fetch_listing(conn, listing_id)
    if not listing:
        return {"listing_id": listing_id, "status": "not_found"}

    print(f"\n>>> id={listing_id} | {listing['street']} ({listing['city']})")

    if not listing["jpg_field"]:
        print("  nav JPG bildes URLs")
        return {"listing_id": listing_id, "status": "no_images"}

    # Nodrošina raw bildes lokāli (tāpat kā Seedream pipeline)
    raw_files = ip.ensure_raw_local(conn, listing)
    if not raw_files:
        return {"listing_id": listing_id, "status": "no_raw_images"}

    targets = _select_raw_files(raw_files, image_filter)
    if not targets:
        print(f"  --images filtrs neatbilst nevienai raw bildei "
              f"(pieejamas: {[p.name for p in raw_files]})")
        return {"listing_id": listing_id, "status": "no_target_images"}

    est = len(targets) * _COST_BY_QUALITY.get(quality, 0.05)
    print(f"  apstrādājam {len(targets)}/{len(raw_files)} bildes "
          f"({quality}, ~${est:.2f})")
    if dry_run:
        for p in targets:
            print(f"    [dry-run] {p.name}")
        return {"listing_id": listing_id, "status": "dry_run",
                "image_count": len(targets), "estimated_cost_usd": est}

    out_dir = ip.ai_dir(listing_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    processed: list[str] = []
    failed: list[str] = []
    t_all = time.time()

    for i, raw_path in enumerate(targets, start=1):
        filename = raw_path.name
        out_path = out_dir / filename
        if out_path.exists() and not force:
            print(f"  [{i}/{len(targets)}] {filename} — jau eksistē "
                  f"ai_ready, skip (--force lai pārrakstītu)")
            processed.append(ip.relative_ai_path(listing_id, filename))
            continue
        print(f"  [{i}/{len(targets)}] {filename}...")
        t0 = time.time()
        try:
            raw_bytes = raw_path.read_bytes()
            final = openai_edit(raw_bytes, filename, quality)
            if not final:
                failed.append(filename)
                continue
            out_path.write_bytes(final)
            processed.append(ip.relative_ai_path(listing_id, filename))
            print(f"      OK {time.time() - t0:.1f}s | {len(final) // 1024} KB")
        except Exception as e:
            print(f"      ! ERROR: {str(e)[:200]}")
            failed.append(filename)

    elapsed = time.time() - t_all
    cost = len(processed) * _COST_BY_QUALITY.get(quality, 0.05)

    # Atjauno DB local_image_paths_processed, ja iepriekš tukšs vai jauni
    # faili pievienojas. Ja visi targets jau bija sarakstā — bez izmaiņām.
    already = listing.get("local_image_paths_processed") or []
    if processed and set(processed) - set(already):
        merged = list(dict.fromkeys(already + processed))
        ip.update_processed_paths(conn, listing_id, merged)
        print(f"  DB local_image_paths_processed atjaunots ({len(merged)} ceļi)")

    print(f"  KOPĀ: {len(processed)} apstrādāts, {len(failed)} fail | "
          f"{elapsed:.1f}s | ~${cost:.2f}")
    return {
        "listing_id": listing_id, "status": "ok",
        "processed_count": len(processed), "failed_count": len(failed),
        "elapsed_seconds": round(elapsed, 1), "cost_usd": round(cost, 3),
    }


def main():
    if not ip.DATABASE_URL:
        print("DATABASE_URL trūkst crm/.env failā")
        sys.exit(1)
    if not OPENAI_API_KEY:
        print("OPENAI_API_KEY trūkst crm/.env failā")
        sys.exit(1)

    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--listing", type=int, required=True, help="listing_id")
    ap.add_argument("--images", help="Komatu atdalīts raw bilžu saraksts "
                    "(piem. img_002.jpg,img_003.jpg). Default: visas raw.")
    ap.add_argument("--quality", choices=["low", "medium", "high", "auto"],
                    default=OPENAI_IMAGE_QUALITY,
                    help=f"OpenAI quality (default {OPENAI_IMAGE_QUALITY})")
    ap.add_argument("--force", action="store_true",
                    help="Pārraksta ai_ready ja jau eksistē")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    img_filter = ([s.strip() for s in args.images.split(",") if s.strip()]
                  if args.images else None)

    print(f"Model: {OPENAI_IMAGE_MODEL} (q={args.quality}, {OPENAI_IMAGE_SIZE})")
    print(f"STORAGE_ROOT: {ip.STORAGE_ROOT}")
    if args.force:
        print("Mode: FORCE (pārraksta esošos ai_ready)")
    if args.dry_run:
        print("Mode: DRY RUN")

    with psycopg.connect(ip.DATABASE_URL, row_factory=dict_row) as conn:
        r = enhance_listing(conn, args.listing, img_filter, args.quality,
                            args.force, args.dry_run)
    print("\n=== KOPSAVILKUMS ===")
    print(f"  {r}")


if __name__ == "__main__":
    main()
