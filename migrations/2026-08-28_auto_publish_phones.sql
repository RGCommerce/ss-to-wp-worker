-- 2026-08-28 (Raimonds, #110350 Cēsu 31): auto-publish "verificēja" numuru pret
-- ēkas primary/secondary_phone, BET building_profiles_worker tos AUTOMĀTISKI
-- savāc no visiem ēkas sludinājumiem → aplis: jebkurš svešs ss.lv numurs
-- auto-publish ēkā pats kļūst "verificēts" un publicējas.
-- Fikss: atsevišķa kolonna auto_publish_phones — TIKAI manuāli apstiprināti
-- numuri (paneļa Ēkas lapā); worker to nekad nepārraksta; auto_publish_poller
-- verificē TIKAI pret šo (+ VIP tabulu).

ALTER TABLE properties.building_profiles
    ADD COLUMN IF NOT EXISTS auto_publish_phones TEXT;

COMMENT ON COLUMN properties.building_profiles.auto_publish_phones IS
    'Manuāli apstiprinātie īpašnieka numuri auto-publicēšanai (komatu atdalīti). '
    'TIKAI pret šiem verificē auto_publish_poller. Aģents labo paneļa Ēkas lapā; '
    'building_profiles_worker šo kolonnu NEAIZTIEK.';

-- Backfill: esošajām auto-publish ēkām par apstiprināto ņem primary_phone
-- (Partner ēkām tas ir manuāli kurēts un netiek pārrakstīts). Secondary NE —
-- tur krājas automātiski savāktie sludinājumu numuri (tieši caurums).
UPDATE properties.building_profiles
SET auto_publish_phones = primary_phone
WHERE auto_publish IS TRUE
  AND auto_publish_phones IS NULL
  AND primary_phone IS NOT NULL
  AND btrim(primary_phone) <> '';
