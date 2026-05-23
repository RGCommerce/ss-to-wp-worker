# ss-to-wp-worker

Railway serviss, kas publicē ss.lv listings uz rgcommerce.lv (Houzez WP)
ar AI bilžu apstrādi un klasifikāciju. Tiek izsaukts no Broker Panel
"Convert to WP" pogas (vai manuāli ar curl) — viens HTTP POST = pilns
sludinājums uz mājaslapu.

**Atsevišķs repo no `inbox-to-listings`** (Raimonda lēmums 2026-05-21:
katram skriptam savs push cikls, nesabojā citus servisus).

## Arhitektūra

```
Broker Panel / curl
        │ POST /publish/{listing_id}  (ar X-RGC-Token header)
        ▼
Railway service: ss-to-wp-worker (FastAPI + uvicorn)
        │
        ├─► image_pipeline.py     (Seedream-5-lite, ~$0.04/bilde)
        │       ↓ raw → ai_ready
        ├─► image_classify.py     (gpt-4o-mini vision, ~$0.001/bilde)
        │       ↓ manifests storage/listings/<id>/_image_manifest.json
        └─► publish_to_wp.py      (WP REST via plugin rgc-mk/v5)
                ↓ teksts + bildes (fasāde pirmā, plāns uz floor sec.)
                ↓
        rgcommerce.lv property post

Volume: inbox-to-listings-volume mounted → /storage
        (shared ar inbox-to-listings + image-downloader)
        → raw bildes JAU pieejamas (`image-downloader` worker piepilda
          no ss.lv) → NEVIENU REIZI no šī servisa neiet uz ss.lv
```

## Endpoints

| Method | Path | Auth | Apraksts |
|---|---|---|---|
| GET  | `/health` | nav | Service + storage + poller status |
| GET  | `/poller/status` | nav | Detalizēts queue poller statuss |
| POST | `/publish/{listing_id}` | `X-RGC-Token` | Pilns pipeline (image_pipeline + classify + publish) |
| POST | `/classify/{listing_id}` | `X-RGC-Token` | Tikai klasificē (lēts pretests) |
| POST | `/enhance-openai/{listing_id}` | `X-RGC-Token` | Selektīvi OpenAI gpt-image-1 sliktajām bildēm |
| POST | `/pdf/{listing_id}` | `X-RGC-Token` | RGC sludinājuma PDF brošūra (atgriež `application/pdf`) |

### Queue poller (v0.2.0+)

Servisa fona task lasa `properties.wp_export_queue` un apstrādā pa vienam
ierakstam. Statusa pārejas: `pending → processing → done | error`.

Broker Panel klikšķis "Export to WP" pievieno rindu → poller ņem un palaiž
to pašu `publish_to_wp.publish()`, kas `/publish/{id}` endpointam.

Konfigurējams ar env:
- `POLLER_ENABLED` (default `"1"`) — uzliek `"0"`, lai izslēgtu
- `POLLER_INTERVAL` (default `"10"`) — sekundes starp tukšiem polliem

Poll lietoja `FOR UPDATE SKIP LOCKED`, tāpēc droši paralēli vairākiem
servisa instances (ja Railway scales).

Body params (visi POST, visi opcionāli):
- `force: bool` — pārpublicē/pārapstrādā
- `dry_run: bool` (tikai `/publish`) — payload bez WP raksta
- `skip_ai: bool` (tikai `/publish`) — izlaiž image_pipeline (riskanti — aizliegts raw uz WP)
- `images: list[str]` (`/classify`, `/enhance-openai`) — konkrētu bilžu apakškopa
- `quality: str` (`/enhance-openai`) — low|medium|high

## Piemēri

```bash
# Health check
curl https://ss-to-wp-worker.up.railway.app/health

# Pilns publish (Broker Panel poga gala stāvoklī)
curl -X POST https://ss-to-wp-worker.up.railway.app/publish/525 \
  -H "X-RGC-Token: $RGC_MK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'

# Dry-run (parāda payload bez WP raksta)
curl -X POST https://ss-to-wp-worker.up.railway.app/publish/525 \
  -H "X-RGC-Token: $RGC_MK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'

# Tikai klasificē
curl -X POST https://ss-to-wp-worker.up.railway.app/classify/525 \
  -H "X-RGC-Token: $RGC_MK_TOKEN" -d '{}'

# OpenAI uzlabot konkrētu bildi (img_002.jpg) ar high quality
curl -X POST https://ss-to-wp-worker.up.railway.app/enhance-openai/525 \
  -H "X-RGC-Token: $RGC_MK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"images": ["img_002.jpg"], "quality": "high"}'
```

## Deploy (vienreizēja iestatīšana)

1. **GitHub:** izveidot jaunu repo `RGCommerce/ss-to-wp-worker`,
   push šo mapi:
   ```
   cd ss-to-wp-worker
   git init && git add . && git commit -m "init ss-to-wp-worker"
   git remote add origin https://github.com/RGCommerce/ss-to-wp-worker.git
   git push -u origin main
   ```

2. **Railway dashboard** → projekts `brave-harmony`:
   - **New Service** → **GitHub Repo** → izvēlies `ss-to-wp-worker`
   - Service name: `ss-to-wp-worker`
   - **Variables**: kopēt no `sslv-ai-runner-railway/crm/.env`
     (sk. `.env.example` šajā repo) — DATABASE_URL, OPENAI_API_KEY,
     REPLICATE_API_TOKEN, RGC_MK_TOKEN, WP_URL, STORAGE_ROOT=/storage
   - **Settings → Volumes**: **Attach existing volume**
     `inbox-to-listings-volume` → Mount path `/storage`
     (Railway ļauj 1 volume → vairāki servisi, sk. README līnija 1865)
   - **Settings → Networking**: **Generate Domain**
     (sanāks `ss-to-wp-worker-production.up.railway.app`)

3. **Verificēt:**
   ```bash
   curl https://ss-to-wp-worker-production.up.railway.app/health
   ```
   Atbildei jābūt `storage_exists: true` un `has_token: true`.

4. **Pirmais reālais test** — listing 525:
   ```bash
   curl -X POST https://ss-to-wp-worker-production.up.railway.app/publish/525 \
     -H "X-RGC-Token: $RGC_MK_TOKEN" -d '{}'
   ```

## Vai šis NEgāj uz ss.lv?

Nē. `image_pipeline.py` ar `ensure_raw_local` pārbauda lokālos raw failus
PIRMS ss.lv fallback-a. Uz Railway `/storage/listings/<id>/raw/` jau ir
piepildīts no `image-downloader` worker-a. Tāpēc raw bildes pieejamas
lokāli (no šī servisa skatpunkta) → ss.lv koda zars nav vajadzīgs.

(Vienīgais gadījums, kad notiktu ss.lv lejupielāde: ja `image-downloader`
worker vēl nav apstrādājis šo listing — race condition. Atrisinājums:
nogaidīt vai pārstartēt manuāli.)

## Code source

Visi 10 Python faili kopēti no `sslv-ai-runner-railway/crm/` 2026-05-21.
Turpmākajām izmaiņām: rediģē ŠEIT un push → Railway auto-redeploy. Ja
arī crm/ versiju gribi atjaunot lokālajai dev-iterācijai, manuāli kopēt
atpakaļ. (Nākotnē — apvienot caur shared package vai git submodule.)
