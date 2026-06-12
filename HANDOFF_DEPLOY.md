# HANDOFF → 4. kaste: ss-to-wp-worker deploy (Melnā kaste, 2026-06-08)

## 🆕 2026-06-12 — Cenas auto-sync (ss.lv → listings → WP) ⛔ GAIDA DEPLOY

Jauns 5. fona pollers `price_sync_poller.py` + iekablis `main.py` (lifespan +
/health). **Bez DB migrācijas** (lieto esošos `listings.price/price_per_m2` +
`wp_export_queue.action`). Deploy = tikai merge:
```
git checkout main && git pull && git merge melna && git push origin main
```

**Problēma:** ss.lv īpašnieks pārpublicē ar citu cenu → `scrape_inbox` atjaunojas,
bet `listings` paliek iesaldēts uz veco cenu (`inbox_to_listings` = `ON CONFLICT
DO NOTHING`). Panelis/mājaslapa rāda novecojušu cenu. Diagnoze 2026-06-12: 292
listingi ar novecojušu cenu, 7 no tiem dzīvi mājaslapā (rādīja klientiem nepareizu,
biežāk par augstu cenu, piem. #888 Brīvības gatve 410: 1125 → 350).

**Risinājums:** pollers (ik 30 min, env `PRICE_SYNC_INTERVAL`) salīdzina
`listings.price` ar jaunāko `scrape_inbox.price` pa `link`; kur atšķiras →
`UPDATE listings.price + price_per_m2`; ja `on_website` + `wp_post_id` →
`wp_export_queue` (action=publish, requested_by='price_sync') → queue_poller
pārpublicē = `update_property` (atjauno ESOŠO WP postu, pārizmanto galeriju →
**bez AI izmaksām**). Re-publish cap/cikls = env `PRICE_SYNC_MAX_REPUB` (def 25).

**🛡 Drošības guard-i** (lai NEKAD neuzliktu nepareizu cenu uz prod/mājaslapas):
1. **Eksakts periods** — sync tikai ja periods atpazīts ABĀS pusēs un IDENTISKS
   (monthly→monthly, daily→daily, sale→sale). NE noma↔pārdošana, NE monthly↔daily
   (€/mēn ≠ €/dienā), NE None/weekly fallthrough.
2. **Lēciena robeža** `PRICE_SYNC_MAX_RATIO` (def 3.0×) — ja cena mainās >3× →
   NESINHRONIZĒ, loģē `WARNING ... IZLAISTS, manuāla pārbaude` (scraper kļūda /
   vienību glitch netiek klusi uzlikta klientiem).

Validēts pret prod (read-only): 292 atšķirības → pēc guard-iem **255 droši sync**
(37 aizdomīgie >3× aizturēti, t.sk. 19×/34× glitch + monthly↔daily); mājaslapā
**6** drošās korekcijas (#888 1125→350 = 3.2× → pareizi aizturēts pārbaudei).

**Pārbaude pēc deploya:** `/health` → `price_sync_poller.running=true`; pēc ~1 cikla
6 dzīvie mājaslapā rāda jauno cenu; `updated_total` aug. WARNING log = aizturētie
(manuāli jāpārbauda, vai cena reāla). Env: `PRICE_SYNC_ENABLED=0` izslēdz,
`PRICE_SYNC_MAX_RATIO` regulē jutību.

---

## 🆕 2026-06-08 (vakars) — StockOfiss publish fix ⛔ GAIDA DEPLOY

Commit `872216a` uz `melna`. **Bez DB migrācijas.** Deploy = tikai merge:
```
git checkout main && git pull && git merge melna && git push origin main
```
→ Railway auto-deploy → Tvaika 64 (listing 17943) "ss to wp export" strādās.

**Ko labo:** `StockOfiss` (kanoniska AI Space_group — stock-office hibrīds 1. stāvā;
`test_runner_db` enum + anketas pills + Houzez label map) trūka publish šablonos →
`_VEIDS` dict (no kura nāk `SUPPORTED_GROUPS`) bija tikai 10 grupas, `publish_to_wp.py`
guard meta `SystemExit: Space_group 'StockOfiss' nav atbalstīts`.
- `wp_templates.py`: `_VEIDS["StockOfiss"]="noliktavas-biroja telpas"` + `_PIELIET["StockOfiss"]="noliktavas-biroja"`.
- `houzez_reverse_map.py`: property_type → `"Noliktavas / ražošana"` (forward-only override) + property_label → `"Stock Ofiss"`.
- `crm/` dev-kopija sinhronizēta. Render/excerpt/SEO + houzez_type/label verificēts.

**Pārbaude pēc deploya:** re-publicē StockOfiss listingu (Tvaika 64) → izdodas; kartiņā
tips "Noliktavas / ražošana" + label "Stock Ofiss"; virsraksts "...noliktavas-biroja telpas...".

---

## ✅ 2026-06-08 (diena) — BREEAM + pelēkā apdare + karte (JAU DEPLOYOTS)

> Šis bloks jau deployots 06-08 integratora sesijā (`origin/main`=`5677189`). Atstāts vēsturei.

### ⚠ SECĪBA: DB migrācija PIRMS koda, tad merge melna→main

### 1. DB migrācija PIRMS visa (citādi publicēšana lūst)
BREEAM kolonna. Fails: `../rgc-broker-panel-melna/migrations/2026-06-08_building_profiles_breeam.sql`
```sql
ALTER TABLE properties.building_profiles ADD COLUMN IF NOT EXISTS has_breeam boolean;
```
Bez tās `agent_publish.py` raksta kolonnā, kas neeksistē → INSERT/UPDATE building_profiles lūst.

### 2. Worker merge melna→main
```
git checkout main && git pull && git merge melna && git push origin main
```

Commits (`origin/main..origin/melna`), visi `py_compile` OK + render testēti:
- `832456b` — **BREEAM** ilgtspējas sertifikāts ēkas info tekstā (`wp_templates.py`) + persist (`agent_publish.py` `_BP_FIELDS`). Atkarīgs no DB kolonnas (solis 1).
- `3a7dbdd` — **pelēkās apdares teksts** (Space_condition=Nepabeigts): apdares teikums paplašināts + 1 telpa→"atvērta plānojuma (open space)" + fit-out→"iespēja aprīkot". Citur teksts kā agrāk. Bez DB izmaiņām.
- `933480c` — **kartes fix**: `fave_property_map_address` = ĪSĀ adrese (iela+nr), `fave_property_map`="1" vienmēr. Ar tukšu map_address Houzez frontend karti NEzīmēja → "nav Google img". Tagad rādās + tīrs īss teksts; precīzo pin dod plugin geokods (pilnā `geocode_address`). Bez DB izmaiņām.

## Atkarības
- Plugins `rgc-melna-kaste-endpoints-v5` **v5.1.3** jau augšupielādēts WP (geocode_address support).
- DB kolonna `properties.building_profiles.has_breeam` (solis 1).

## Pārbaude pēc deploya (re-publicē 1 listingu)
1. **Karte** rādās ar pin; "Adrese" lauks = tikai iela+nr (NE garais "..., Rīga, Latvija").
2. **BREEAM** (ja ēkai atzīmēts): aprakstā "Ēkai ir BREEAM ilgtspējas sertifikāts."
3. **Pelēkā apdare** (ja Nepabeigts): "...To varat darīt gan Jūs kopā ar profesionāliem meistariem..." + open space (1 telpa) + "iespēja aprīkot...".
