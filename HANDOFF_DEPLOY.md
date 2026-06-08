# HANDOFF → 4. kaste: ss-to-wp-worker deploy (Melnā kaste, 2026-06-08)

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
