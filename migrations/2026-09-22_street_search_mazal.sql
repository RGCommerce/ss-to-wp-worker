-- 2026-09-22 (zaļās atradums pie dublikātu apvienošanas): «M.»/«L.» ir
-- «Mazā»/«Lielā» — CITA iela (M. Nometņu ≠ Nometņu, M. Juglas ≠ Juglas), bet
-- iepriekšējā street_search izteiksme tos izmeta kā iniciāļus → dažādas ielas
-- saplūda vienā meklēšanas atslēgā. Tagad: m./maz.→'maza ', l./liel.→'liela ',
-- tad pārējos iniciāļus (Kr., G., d.) izmet kā līdz šim.
-- SPOGULIS: building_profiles_worker strip_street_type, paneļa street-search.ts
-- un building-key.ts, agent_api.py searchBuildings.

ALTER TABLE properties.listings DROP COLUMN IF EXISTS street_search;
ALTER TABLE properties.listings ADD COLUMN street_search TEXT
GENERATED ALWAYS AS (
  btrim(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
    translate(lower(coalesce(split_part(street, ',', 1), '')),
              'āčēģīķļņōŗšūž', 'acegiklnorsuz'),
    '\y(m|maz)\.\s*', 'maza ', 'g'),
    '\y(l|liel)\.\s*', 'liela ', 'g'),
    '\y[a-z]{1,2}\.', ' ', 'g'),
    '\y(iela|iel|gatve|gat|bulvaris|bulv|prospekts|prosp|pr|laukums|dambis|cels|aleja|soseja|linija|krastmala|tilts|pasaza)\y', ' ', 'g'),
    '\.', ' ', 'g'),
    '\s+', ' ', 'g'))
) STORED;
