# HANDOFF → 4. kaste / Zilā: ss-to-wp-worker deploy (Melnā kaste, 2026-06-07 #2)

## ⛔ VIENS SOLIS: merge `melna` → `main` + Railway redeploy

Repo `RGCommerce/ss-to-wp-worker`. Zars `melna` ir **priekšā main par 1 commitu** (tīrs, `py_compile` OK):

```
git checkout main && git pull && git merge melna && git push origin main
```

Commits (`origin/main..origin/melna`):
- `a06c7b1` — kartes ģeokods caur pilno adresi (`geocode_address`) + nosacījumu teksts pa rindām.

## Kas mainās

**1. Karte ("nav Google img" fix).** Worker tagad sūta jaunu top-level lauku
`geocode_address` = PILNA adrese (iela+pilsēta+Latvija, paplašināti saīsinājumi).
Plugins **v5.1.3** to ģeokodē ar Houzez Google atslēgu (prioritāte #1), bet
`fave_property_map_address` paliek tukšs → karte rādās no koordinātēm, garais
adreses teksts NErādās. Aizstāj neuzticamo Python Nominatim ceļu (worker env nav
Google atslēgas → bieži krita → karte nerādījās).

**2. Teksts.** Nosacījumu sadaļa: katrs teikums savā rindā (`<br>`). "Citi
maksājumi" (Papildu_maksas) atsevišķā rindā ar pēdiņām; komatatdalītie katrs savās
pēdiņās.

## ⚠ Atkarība: plugins v5.1.3

Plugins `rgc-melna-kaste-endpoints-v5` **v5.1.3 jau augšupielādēts WP** (Raimonds,
2026-06-07). Bez tā worker `geocode_address` netiek izmantots (vecais plugins to
ignorē). Zip: `../rgc-melna-kaste-endpoints-v5__UPLOAD_5.1.3.zip`.

## Pārbaude pēc deploya

1. Re-publicē vienu listingu (piem. Gustava Zemgala gatve 76) → WP single lapā
   karte parādās ar pareizu pin, BET "Adrese" lauks tukšs (nav garā "..., Rīga,
   Latvija" teksta).
2. Apraksta "Nomas nosacījumi" — katrs teikums savā rindā; ja ir Papildu_maksas →
   "Citi maksājumi kā: „...”." atsevišķā rindā.

ENV: nekas jauns. Plugins lieto Houzez Google atslēgu (tā pati, ko admin karte).
