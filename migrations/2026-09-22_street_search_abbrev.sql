-- 2026-09-22 (Raimonds/Ieva): «Aspazijas bulvāris 20» meklētājā neatrada
-- «Aspazijas bulv. 20» — street_search ģenerētā kolonna nogriež tikai PILNOS
-- ielas tipa vārdus, ne saīsinājumus (bulv., gat., pr.) un ne iniciāļus (Kr.).
-- Pārbūvē kolonu ar paplašināto izteiksmi — SPOGULIS building_profiles_worker
-- strip_street_type (#14) un paneļa src/lib/street-search.ts.

ALTER TABLE properties.listings DROP COLUMN IF EXISTS street_search;
ALTER TABLE properties.listings ADD COLUMN street_search TEXT
GENERATED ALWAYS AS (
  btrim(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
    translate(lower(coalesce(split_part(street, ',', 1), '')),
              'āčēģīķļņōŗšūž', 'acegiklnorsuz'),
    '\y[a-z]{1,2}\.', ' ', 'g'),
    '\y(iela|iel|gatve|gat|bulvaris|bulv|prospekts|prosp|pr|laukums|dambis|cels|aleja|soseja|linija|krastmala|tilts|pasaza)\y', ' ', 'g'),
    '\.', ' ', 'g'),
    '\s+', ' ', 'g'))
) STORED;
