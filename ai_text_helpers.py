"""test_runner_wordpress.py — WordPress AI worker.

Mirror no test_runner_db.py (ss.lv worker), pielāgots WP plūsmai.

Galvenās atšķirības no oriģināla:
  - Nolasa rindas no properties.wordpress_inbox (ne listings)
  - Listing teksts nāk no DB (post_title + post_content + post_excerpt +
    strukturētajiem laukiem) — NEvelk HTML caur HTTP
  - Bilžu URL-i no rindas wp_image_urls lauka — NEvelk i.ss.lv galeriju
  - check_data_conflicts() (B4 regex) — NEizpilda (Ieva pati ievada laukus)
  - handle_agent_detected() — NEizsauc (mēs publicējam paši, ne aģenti)
  - delete_listing() — NEKAD nesauc (atšķirībā no ss.lv); ja AI atgriež ne-'ok'
    (low_evidence / data_conflict / agent_detected), saglabā Debug_status DB
    un atstāj rindu manuālai pārskatīšanai

Prompt un JSON schema — IDENTISKS ar ss.lv versiju (saskaņots 2026-05-08).
"""
import os
import re
import sys
import json
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from dotenv import load_dotenv
from openai import OpenAI

# Windows console UTF-8 priekš latviešu burtu print()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL = os.getenv("MODEL", "gpt-5.4")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))
EMPTY_SLEEP_SECONDS = int(os.getenv("EMPTY_SLEEP_SECONDS", "30"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

# NB: NEKRĀTS pie ielādes — agent_ai_poller atļauj startēt bez OPENAI_API_KEY
# (paliek idle un waitē). Citi ss-to-wp-worker komponenti (queue_poller, pdf_poller,
# agent_api) ai_text_helpers neimportē — tikai agent_ai_poller, kas pats pārbauda.

PROMPT = r"""
# SS.LV COMMERCIAL LISTING ANALYSIS — STRICT MVP PROMPT

## 1. GOAL

Analyze one SS.lv commercial property listing using:
- the listing text
- ALL provided images in original order
- the last image is especially important because it may be a plan

You must inspect ALL images in sequence before filling fields.

If not all images were effectively reviewed, analysis is invalid.

---

## 2. HARD RULES

1. Use all images in order.
2. Use listing text and images together.
3. Do not invent facts.
4. If evidence is insufficient, return the allowed unknown value.
5. Output must match the JSON schema exactly.
6. For enum-like fields, use ONLY the exact allowed values from the schema.
7. Do not output markdown.
8. Do not explain fields outside JSON.
9. Do not retell the listing text unless the field explicitly allows text-based extraction.
10. Prefer PHOTO over TEXT where the field rules say so.

---

## 3. IMAGE REVIEW RULE

Before filling any field:
- review all images
- internally identify likely image types:
  - IMG_FACADE
  - IMG_INTERIOR
  - IMG_PLAN
  - IMG_TERRITORY
  - IMG_TECHNICAL
  - IMG_MISC

These tags are internal only. Do not print them.

---

## 4. OUTPUT NORMALIZATION RULE

You must output ONLY values that fit the current Excel/schema logic.

Important canonical values:

### building_type
Allowed:
- Biroju ēka
- Jaukta tipa ēka
- Industriāla ēka
- Tirdzniecības centrs
- Medicīnas centrs
- Autoserviss
- Unknown

## building_class
Primary:
- PHOTO (facade + interior)
Secondary:
- TEXT if directly useful

Rules:
- A = modern, premium-quality commercial building/space, clearly high standard
- B = maintained, functional, heated, usable commercial/industrial object; decent standard, not premium
- C = visibly outdated, rough, neglected, unheated, low-comfort, low-finish, or technically weak object

Important:
- industrial / warehouse / production use ALONE does not mean C
- simple facade ALONE does not mean C
- brick facade ALONE does not mean C
- if the object is clean, maintained, heated, visually usable, and has decent windows/finishes, prefer B over C
- if the space/object is clearly unheated or Apkure = Nav with enough evidence, it cannot be A or B -> use C
- use C only when there is clear evidence of lower standard, such as visible neglect, worn-out condition, poor comfort, missing/weak engineering, or clearly unheated stock

If insufficient evidence -> unknown

### Space_condition
Allowed:
- Jauns
- Labs
- Lietots
- Nepabeigts
- Nepieciešams remonts
- Kapitālais remonts
- unknown

### Mebeleta_telpa
Allowed:
- Jā
- Nē
- Daļēji
- unknown

### Logu_type
Allowed:
- Lielie Logi
- Standarta logi
- Maz logu
- Nav logi
- unknown

### Dalama_telpa
Allowed:
- Jā
- Nē
- Unkown

### street_entrance
Allowed:
- checked
- not checked
- unknown

### Apkure
Allowed:
- Centrālā
- Gāzes
- Elektriskā
- Nav
- unknown

### Checkbox fields
Allowed:
- checked
- not checked
- unknown

Checkbox fields:
- Auto_pacelajs_check
- Treifelis_Pacelajs
- Virtuve_check
- Balkons_check
- Apsargajama_teritorija_check
- Ventilacijas_sistema_check
- Rampa_logistikai_check
- Pacelamie_varti_check
- Sava_ieeja_check
- Ir_izlietne_telpa_check
- Nozogota_teritorija_check
- Sava_eka_check
- Vides_pieejamiba_check
- Mazgajamas_sienas_check

---

## 5. FIELD RULES

## building_type
Meaning: building type of the OBJECT / BUILDING itself, not just current room use.

Primary source:
- PHOTO (facade/exterior/building form)
Secondary:
- TEXT if explicitly stated

Rules:
- If building visually looks industrial / hangar / warehouse structure / service building -> Industriāla ēka
- If building visually looks clear office building -> Biroju ēka
- If building visually looks retail-center type with showcase facade / center logic -> Tirdzniecības centrs
- If building visually looks clinical/medical center or text clearly states such -> Medicīnas centrs
- If building itself is clearly auto service type -> Autoserviss
- If object mixes office + retail/service and building itself is mixed-use -> Jaukta tipa ēka
- If not enough evidence -> Unknown

Important:
Do not confuse building_type with Space_group.
building_type = what kind of building this is.
Space_group = what kind of space use this unit fits.

---

## building_class
Primary:
- PHOTO (facade + interior)
Secondary:
- TEXT if directly useful

Rules:
- A = modern, premium-quality commercial building/space, clearly high standard
- B = maintained, functional, good standard commercial/industrial object; clean, heated, usable, with decent windows/finishes; not premium, but clearly not low-grade
- C = visibly outdated, rough, neglected, low-comfort, low-finish, or technically weak object

Important:
- industrial / warehouse / production use ALONE does not mean class C
- simple facade ALONE does not mean class C
- brick facade ALONE does not mean class C
- if the object is clean, maintained, heated, visually usable, and has decent windows/finishes, prefer B over C
- use C only when there is clear evidence of lower standard, such as visible neglect, worn-out condition, poor comfort, missing/weak engineering, or clearly outdated rough stock

If insufficient evidence -> unknown

---

## Building_description
Primary:
- PHOTO (facade / territory / exterior)

Write exactly 1 short sentence about the exterior/building.

Allowed:
- facade material
- facade condition
- external elements like gates, windows, separate entrance
- visible exterior/building character

Forbidden:
- location
- price
- use-case claims from text
- full listing retell
- tenancy / offer details

If facade/exterior is visible, unknown is not allowed.

Good example:
"Ķieģeļu ēka ar industriāliem paceļamiem vārtiem un vienkāršu funkcionālu fasādi."

---

## electric_power_kw
Primary:
- TEXT
Secondary:
- PHOTO if exact readable number exists on electrical panel

If exact power is not visible or text does not specify -> unknown

Do not invent approximate kW.

---

## Apsaimniekosanas_maksa
Primary:
- TEXT only

If management fee is explicitly stated, return short exact text/value.
Else unknown.

Do not place rent price here.

---

## NIN
Primary:
- TEXT only

If explicitly stated -> return short exact value/text
Else unknown

---

## Komunalie
Primary:
- TEXT only

If utilities included -> return exact short text
If utilities price/value specified -> return exact short text
Else unknown

Do not put base rent here.

---

## Papildu_maksas
Primary:
- TEXT only

This field means extra fees only.

Allowed outputs:
- exact extra fee short text
- fakts bez cenas
- unknown

Forbidden:
- base rent
- total monthly price
- utilities
- full pricing retell
- VAT unless explicitly part of extra fee itself

If text mentions specific extra fee -> return short exact extra-fee text
If extra fees are mentioned without amount -> fakts bez cenas
Else -> unknown

---

## Parkings

Allowed values:
- Ir vietas bezmaksas
- Ir vietas par maksu
- Ir vietas
- Tikai ielas parking
- Nav
- unknown

Primary Source: TEXT
Secondary Source: PHOTO (IMG_TERRITORY / IMG_FACADE)

Rules:

IF
- tekstā skaidri minēts, ka stāvvietas ir bezmaksas
OR
- tekstā skaidri minēts, ka parking ir included / bez maksas
THEN Parkings = Ir vietas bezmaksas

ELSE IF
- tekstā skaidri minēts, ka parking ir par atsevišķu samaksu
THEN Parkings = Ir vietas par maksu

ELSE IF
- tekstā vai bildēs skaidri redzams, ka pie objekta ir savas stāvvietas
AND
- nav skaidrs, vai tās ir bezmaksas vai par maksu
THEN Parkings = Ir vietas

ELSE IF
- nav redzamas vai minētas privātas vietas
AND
- redzams vai secināms tikai ielas parking
THEN Parkings = Tikai ielas parking

ELSE IF
- tekstā skaidri minēts, ka parking nav
OR
- ir pietiekami daudz pierādījumu, ka parking nav
THEN Parkings = Nav

ELSE
Parkings = unknown
---

## Space_group
Meaning: what this UNIT / SPACE is best classified as based on ACTIVE use.

Primary:
- PHOTO (interior)
Secondary:
- TEXT

Rules:
- If clear office interior -> Birojs
- If clear retail/shop/showcase logic -> Tirdzniecība
- If clear warehouse logic -> Noliktava
- If clear production logic -> Ražošana
- If clear stock-office hybrid AND the unit is on the 1st floor or has clear direct ground access -> StockOfiss
- If the unit is not on the 1st floor, do not use StockOfiss

MEDICĪNA (active medical use only):
- Use Medicīna ONLY if visible or text-supported ACTIVE medical functionality:
  - visible medical cabinets with examination tables, dental chairs, medical equipment
  - registration desk with patient logs, dedicated waiting room
  - signage like "Zobārstniecība", "Klīnika", "Ārsta kabinets", "Ambulance"
  - text explicitly states active medical use / med code 1264 registered
- Renovation potential alone does NOT qualify for Medicīna primary
- For renovation potential, use Potential_space_group instead

PVD (active food / PVD-licensed use only):
- Use PVD ONLY if visible or text-supported ACTIVE food production / kitchen fit-out:
  - visible commercial kitchen equipment (industrial stoves, prep tables, cold rooms)
  - sanitary tile walls + stainless steel surfaces actively in use
  - text states active PVD registration / food production / commercial kitchen
- A real food-prep / production kitchen without customer-facing hospitality -> PVD
- Renovation potential alone does NOT qualify for PVD primary
- For renovation potential, use Potential_space_group instead

SPORTA ZĀLE (active gym/training use only):
- Use Sporta zāle ONLY if visible or text-supported ACTIVE gym/training use:
  - visible sports equipment (weight machines, dumbbells, training mats, boxing bags)
  - wall mirrors (key indicator for fitness/yoga/dance studios)
  - sports flooring (parquet, rubber, sports mats, gym-grade surface)
  - text mentions "sporta zāle", "fitness", "treniņi", "krossfit", "yoga", "pilates", "aerobika"

STUDIJA:
- Use Studija only if the space is clearly suited for studio-type use such as
  photo studio, design studio, dance studio, beauty studio, or other open
  creative/service studio use
- Do NOT use Studija for ordinary office rooms, cabinet-type plānojumu, narrow
  rooms, standard administrative premises, or typical multi-room office layouts
- If the space is clearly a normal office, prefer Birojs and do not use Studija

RESTORANS/CAFE:
- Use Restorans/Cafe only if the space is clearly suited for restaurant, cafe,
  bar, catering, or customer-facing food service use
- Do NOT use Restorans/Cafe for an office kitchenette, staff kitchen corner,
  tea point, or simple sink/cabinet area inside an office
- If the space is a real food-prep / production kitchen without customer-facing
  hospitality use, prefer PVD

AUTOSERVISS:
- If clear auto-service logic -> Autoserviss

If insufficient -> unknown

Important:
Space_group is about the space itself, not the whole building.
Space_group represents ACTIVE current use. Renovation potential goes to
Potential_space_group, not here.

---

## Potential_space_group

This field is multi-value free text indicating realistic ALTERNATIVE
use-cases for the space, based on physical infrastructure and feasibility
for renovation. This is different from Space_group (which is ACTIVE use).

Allowed canonical values:
- Birojs
- Tirdzniecība
- Noliktava
- Ražošana
- StockOfiss
- PVD
- Medicīna
- Studija
- Restorans/Cafe
- Sporta zāle
- Autoserviss
- unknown

Output format:
- one or more allowed canonical values, separated by comma and space
- example: "StockOfiss, Noliktava, Ražošana"
- do not write explanations or bullets
- do not invent new values
- if no realistic alternative fits exist -> unknown

GENERAL CLASSIFICATION RULE:
Classify on physical infrastructure alone. Marketing keywords in description
are NOT required — most ss.lv listings don't position to specific uses but
may still physically qualify. Conversely, marketing keywords alone (e.g.
"medicīnai", "kafenīcai piemērots") without matching infrastructure do NOT
qualify.

### Medicīna potential — 4-check baseline
Include "Medicīna" in Potential_space_group if ALL FOUR pass:
1. Vides_pieejamiba_check = checked
2. Ventilacijas_sistema_check = checked OR feasible
   (industrial ceiling, A-class building with ventilation infrastructure)
3. cik_WC indicates ≥1 dedicated WC in the unit (NOT "WC koplietošanā" /
   "koplietošanas WC" / "shared WC")
4. Ir_izlietne_telpa_check = checked OR water extension feasibility:
   - text mentions "var ievilkt ūdeni", "iespējams izbūvēt sanmezglus",
     "ūdens pievadi", "kanalizācija pieejama"
   - dedicated WC presence implies extendable plumbing to all rooms (MK 60 allows)

### PVD potential — 3-check baseline
Include "PVD" in Potential_space_group if ALL THREE pass:
1. Wash-friendly surfaces — any of:
   - Mazgajamas_sienas_check = checked
   - Gridas_materials in: Keramikas flīzes, Porcelāna flīzes, Epoksīda grīda,
     Poliuretāna-cementa grīda, Gumijas grīda, Betona grīda ar trapiem,
     Slīpēts betons, Mikrocements, PVC / vinils
   - A-class warehouse/production building with ventilation + water present
     (industrial baseline boost — modern sandwich-panel walls are food-grade
     washable; renovation feasible)
2. Water extension feasibility — any of:
   - Ir_izlietne_telpa_check = checked
   - cik_WC indicates ≥1 dedicated WC in unit
   - text mentions "var ievilkt ūdeni" / "iespējams izbūvēt sanmezglus" /
     "ūdens pievadi" / "kanalizācija pieejama"
3. Ventilacijas_sistema_check = checked OR feasible

PVD STRONG TEXT SIGNALS (raise confidence; not required):
- "trapi grīdā" / "floor drains"
- "slapja ražošana" / "wet production"
- "PVD reģistrēts" / "PVD aktīvs"
- "atļauts pārtikai" / "speciāli pārtikai"

### Sporta zāle potential — 4-check baseline
Include "Sporta zāle" in Potential_space_group if ALL FOUR pass:
1. Vides_pieejamiba_check = checked
2. Open space layout — any of:
   - Cik_telpas = "1" or low (≤2)
   - photos show clearly open, undivided space without fixed partition walls
   - simple gray/concrete/bare interior finish works as baseline
   - no rigid cabinet/office partitioning visible
3. Ventilacijas_sistema_check = checked OR feasible
   (CO2 removal during training is essential)
4. Water/WC feasibility — any of:
   - cik_WC indicates ≥1 dedicated WC OR Ir_izlietne_telpa_check = checked
   - text mentions extension feasibility for changing rooms / showers

Size note: area_m2 ≥ 200m² preferred (500-1500m² ideal), but smaller spaces
(100m²+) can qualify if they visually present as a gym/training space.

### Other categories (existing rules)
StockOfiss:
- Allowed only if unit is on the 1st floor or has clear direct ground access
- If the unit is above the 1st floor, do not include StockOfiss

Restorans/Cafe:
- Allowed only if the space could realistically function as a restaurant,
  cafe, bar, or customer-facing food service unit
- Do not include Restorans/Cafe for an office kitchenette, staff kitchen
  corner, or simple built-in cabinets with sink inside office space
- If the visible kitchen element is only supportive to office use, keep
  Birojs and do not add Restorans/Cafe
- If the space is more suitable for food production / catering / prep
  without customer-facing hospitality use, prefer PVD

Studija:
- Allowed only if the space could realistically function as an open studio-type space
- Do not include Studija for standard office premises with small or separate
  rooms, administrative layouts, or typical cabinet-style offices

General fallback:
- If Space_group is clearly Birojs and there is no strong evidence for any
  other realistic use-case, return unknown

---

## Space_condition
Primary:
- PHOTO (interior)
Secondary:
- TEXT

Map visually:
- Jauns = newly renovated / fresh / no visible wear
- Labs = maintained, usable, clean, no major issues
- Lietots = visibly used but usable
- Nepabeigts = unfinished / shell / grey-finish
- Nepieciešams remonts = needs repair
- Kapitālais remonts = severe repair need
- unknown = not enough evidence

---

## Agent_comment
Primary:
- PHOTO (interior)

Write 1–2 short sentences only about what is visible in the space.

Allowed:
- walls
- floor
- lighting
- spaciousness
- industrial / office / technical feeling
- visible materials
- visual atmosphere

Forbidden:
- retelling listing text
- price
- location
- hidden utilities
- claims not visible in images

If interior images exist, unknown is not allowed.

Good example:
"Vienkārša industriāla telpa ar betona un ķieģeļu virsmām, tehnisku apgaismojumu un funkcionālu, neizskaistinātu vidi."

---

## Mebeleta_telpa
Primary:
- PHOTO (interior)
Secondary:
- TEXT

If clearly furnished -> Jā
If clearly empty -> Nē
If partly furnished -> Daļēji
If too incomplete to tell -> unknown

---

## Logu_type
Primary:
- PHOTO (interior + facade)
Secondary:
- TEXT only if photos insufficient

Return only:
- Lielie Logi
- Standarta logi
- Maz logu
- Nav logi
- unknown

Rules:
- large glazing / showcase / large windows -> Lielie Logi
- normal windows -> Standarta logi
- small windows / weak natural light -> Maz logu
- enough images and no windows visible -> Nav logi
- not enough evidence -> unknown

---

### Griestu_augstums

Allowed output:
- exact number string
- approximate number string with ~
- unknown

Primary Source:
- TEXT
- PLAN
Secondary Source:
- PHOTO (conservative inference is allowed)

Rules:
- if text explicitly states ceiling height, return the exact value without ~
- if plan explicitly shows ceiling height, return the exact value without ~
- if no exact height is given, but images clearly support an estimate, return an approximate value with ~
- if evidence is weak, return unknown

Inference guide:
- standard office / standard commercial ceiling -> ~2.7 to ~3.0
- visibly above standard commercial ceiling -> ~3.5 to ~4.0
- clear warehouse / industrial unit with high open volume -> ~4.0 to ~5.0
- large warehouse / service bay / hangar type with visibly high gates, tall walls, truck-scale proportions -> ~5.0 to ~7.0
- very tall hangar / industrial hall only if strongly supported -> ~7.0+

Conservative rule:
- if choosing between two values, choose the lower believable one
- use ~ for every inferred value
- do not return exact value unless text or plan explicitly states it

---
### Gridas_materials

Allowed values:
- Betona grīda
- Betona grīda ar trapiem
- Slīpēts betons
- Betons ar hardeneri
- Epoksīda grīda
- Poliuretāna-cementa grīda
- Cementa klons
- Mikrocements
- Keramikas flīzes
- Porcelāna flīzes
- Dabīgais akmens
- PVC / vinils
- Kvarca vinils
- Linolejs
- Gumijas grīda
- Paklājflīzes
- Koka grīda
- Parkets
- Lamināts
- Asfalta grīda
- Mūra grīda
- unknown

Primary Source:
- PHOTO (IMG_INTERIOR / IMG_TECHNICAL)
Secondary Source:
- TEXT

Rules:
- if floor material is clearly stated in text, use that
- else if floor material is clearly visible in images, use the best matching canonical value
- else unknown

Important:
- do not invent exotic materials not on the list
- choose conservatively
- if there is doubt between two close materials, choose the simpler/lower-spec option
---

### Gridas_izturiba_kg_m2

Primary Source:
- TEXT
Secondary Source:
- PHOTO + inferred from Gridas_materials, visible structure, floor level, and industrial character

Rules:
- if text explicitly gives floor load capacity, return the exact value/text without ~
- else if floor material and use-class can be reasonably inferred, return an approximate value with ~
- if a range is plausible, choose the LOWER safe value
- if evidence is weak, return unknown

Approximate mapping:
- Betona grīda -> ~300
- Betona grīda ar trapiem -> ~300
- Slīpēts betons -> ~500
- Betons ar hardeneri -> ~750
- Epoksīda grīda -> ~500
- Poliuretāna-cementa grīda -> ~750
- Cementa klons -> ~250
- Mikrocements -> ~250
- Keramikas flīzes -> ~250
- Porcelāna flīzes -> ~400
- Dabīgais akmens -> ~400
- PVC / vinils -> ~250
- Kvarca vinils -> ~250
- Linolejs -> ~250
- Gumijas grīda -> ~250
- Paklājflīzes -> ~250
- Koka grīda -> ~250
- Parkets -> ~250
- Lamināts -> ~250
- Asfalta grīda -> ~250
- Mūra grīda -> ~250

Additional logic:
- if space is on an upper floor and no heavy-duty signals exist, stay conservative
- if the object is clearly industrial with heavy-duty concrete floor, open span, truck access, ramps, or warehouse logic, higher approximate values are allowed only if strongly supported by images
- never output a high estimate unless the visual evidence clearly supports heavy-duty floor logic
---

## Dalama_telpa
Allowed:
- Jā
- Nē
- Unkown

Primary:
- PLAN
- INTERIOR + TEXT

If space is clearly open/flexible/divisible -> Jā
If clearly not divisible / many fixed small rooms / rigid structure -> Nē
If insufficient -> Unkown

---
### Auto_pacelajs_check

Meaning:
Vehicle service lift used for cars in autoservice / garage / service bay context.

Allowed values:
- checked
- not checked
- unknown

Primary Source:
- PHOTO (IMG_INTERIOR / IMG_TECHNICAL)
Secondary Source:
- TEXT

Rules:
- if a car lift is visible -> checked
- if text clearly states auto lift / car lift / vehicle lift / pacēlājs automašīnām -> checked
- if text says pacēlājs in a clear autoservice / garage / service bay context, interpret it as Auto_pacelajs_check = checked, not Treifelis_Pacelajs
- if text says service pit / bedre, that supports autoservice context but does NOT by itself prove a car lift
- if enough evidence exists and no car lift is present -> not checked
- if evidence is insufficient -> unknown
---

## street_entrance

Allowed values:
- checked
- not checked
- unknown

Primary Source:
- PHOTO (IMG_FACADE)
Secondary Source:
- TEXT

Rules:
- if direct street-level entrance is visible or explicitly stated -> checked
- if enough facade evidence exists and such entrance is clearly absent -> not checked
- if evidence is insufficient -> unknown
---

## Cik_telpas

Allowed output:
- exact number string
- approximate number string with ~
- unknown

Rules:
- if exact room count is clearly stated in text or plan -> return exact value without ~
- if room count can only be roughly inferred from images -> return approximate value with ~
- if evidence is weak or layout is not clearly countable -> unknown
- do not return an exact room count from photos alone

---

## cik_WC
If count/location clearly known from text/plan/photo -> return short exact text
Example:
- 1 WC telpā
- 2 WC koplietošanā

Else unknown

---

## Apkure
Allowed:
- Centrālā
- Gāzes
- Elektriskā
- Nav
- unknown

Primary:
- PHOTO + TEXT

Rules:
- if text explicitly mentions central heating -> Centrālā
- if text explicitly mentions gas heating / gas boiler -> Gāzes
- if text explicitly mentions electric heating / heat pump / electric radiators -> Elektriskā
- if text explicitly mentions heating exists, but type is not specified -> Centrālā
- if enough interior/facade images exist, no heating signs are visible, and text does not mention heating -> Nav
- if evidence is insufficient -> unknown

Important:
If there are enough images and no heating evidence, use Nav.

---

## Treifelis_Pacelajs
Checkbox logic:
- visible or text-supported lift/hoist -> checked
- enough evidence and clearly absent -> not checked
- insufficient evidence -> unknown

---

## Virtuve_check
- visible or text-supported kitchen -> checked
- enough evidence and clearly absent -> not checked
- insufficient evidence -> unknown

---

## Balkons_check
- visible or text-supported balcony -> checked
- enough evidence and clearly absent -> not checked
- insufficient evidence -> unknown

---

## Apsargajama_teritorija_check
- visible or text-supported guarded territory -> checked
- enough evidence and clearly absent -> not checked
- insufficient evidence -> unknown

---

## Ventilacijas_sistema_check
Allowed:
- checked
- not checked
- unknown

Primary:
- PHOTO + TEXT

Rules:
- if ventilation system visible or explicitly mentioned -> checked
- if enough images exist and no ventilation signs and text does not mention it -> not checked
- if evidence too limited -> unknown

---

## Rampa_logistikai_check
- visible or text-supported loading ramp -> checked
- enough evidence and clearly absent -> not checked
- insufficient evidence -> unknown
---
### Rampa_logistikai_count

Primary Source:
- PHOTO (IMG_FACADE + IMG_TERRITORY)
Secondary Source:
- TEXT

Rules:
- if the number of loading ramps can be clearly counted from images or text, return the count as a number string
- if ramps are present but exact count is unclear, return unknown
- if no ramps are present, return 0
- if evidence is insufficient, return unknown
---
## Pacelamie_varti_check

Primary Source:
- PHOTO (IMG_FACADE + IMG_INTERIOR)
Secondary Source:
- TEXT
Rules:
- visible or text-supported overhead/sectional industrial door -> checked
- if facade/interior evidence is sufficient and not present -> not checked
- if facade/interior evidence insufficient -> unknown

---
### Pacelamie_varti_count

Primary Source:
- PHOTO (IMG_FACADE + IMG_INTERIOR)
Secondary Source:
- TEXT

Rules:
- if the number of overhead/sectional industrial doors can be clearly counted from images or text, return the count as a number string
- if overhead doors are present but exact count is unclear, return unknown
- if no overhead doors are present, return 0
- if evidence is insufficient, return unknown
---

## Sava_ieeja_check
- separate dedicated entrance visible or stated -> checked
- enough evidence and clearly absent -> not checked
- insufficient evidence -> unknown

---

## Ir_izlietne_telpa_check
This means sink in the unit/space, not generic assumption.

- visible sink in relevant space -> checked
- enough evidence and clearly absent -> not checked
- insufficient evidence -> unknown

---

## Sava_eka_check

Allowed:
- checked
- not checked
- unknown

Meaning:
This field checks whether the offered unit appears to be the whole standalone building / the tenant would occupy the building itself, not just one unit among other tenants.

Primary:
- PHOTO (facade / territory / building form)
Secondary:
- TEXT

Rules:
- checked = clear evidence that this is a separate standalone building and not a multi-tenant/shared building
- not checked = clear evidence that this is only one unit in a larger building with other tenants / shared corridors / multi-unit logic
- unknown = not enough evidence

---

## Nozogota_teritorija_check
- visible or text-supported fenced territory -> checked
- enough evidence and clearly absent -> not checked
- insufficient evidence -> unknown

---

## Vides_pieejamiba_check
Allowed:
- checked
- not checked
- unknown

Meaning:
Whether the unit/space is accessible for clients with physical limitations
(legal requirement for medical premises per MK 60, and a practical requirement
for gym/training spaces with client flow).

Primary:
- TEXT (explicit mentions of lift, ramp, accessibility, floor number)
Secondary:
- PHOTO (visible passenger lift, ramp at entrance, wide ground-level entrance)

Rules:
- checked if any of:
  - floor field indicates "1. stāvs" / "1" (ground floor — accessibility automatic)
  - text mentions "lifts" / "lifti" / "X lifti"
  - text mentions "piekļuve cilvēkiem ar kustību traucējumiem"
  - text mentions "vides pieejamība" / "vides pieejama"
  - text mentions "rampa" as passenger access (NOT loading ramp)
  - photos show passenger lift door, accessibility ramp at building entrance,
    or wide ground-level entrance for the unit
- not checked if all of:
  - floor is clearly above 1st (e.g., "2", "3", "5", "6")
  - no lift evidence in text or photos
  - no passenger ramp visible
  - clearly walk-up staircase only
- unknown if floor cannot be determined and no lift/ramp evidence

Important:
Distinguish PASSENGER lift/ramp (Vides_pieejamiba_check) from LOADING ramp
(Rampa_logistikai_check). Loading ramps for trucks/cargo do NOT count here.

---

## Mazgajamas_sienas_check
Allowed:
- checked
- not checked
- unknown

Meaning:
Whether visible wall surfaces are washable / non-porous. Key requirement for
PVD / food hygiene; a positive indicator for medical-grade hygiene as well.

Primary:
- PHOTO (interior wall close-up + general interior shots)
Secondary:
- TEXT (explicit wall material mentions)

Rules:
- checked if visible wall material is washable / non-porous:
  - ceramic or porcelain wall tiles (any color, any size)
  - PVC / plastic wall panels (sanitary panels, hospital-style)
  - epoxy or high-gloss painted surfaces
  - stainless steel or metal sheet wall panels
  - modern A-class warehouse/production sandwich panels (food-grade
    industrial baseline — these are washable by design)
- not checked if visible walls are clearly:
  - matte standard painted walls (non-industrial, not sandwich-panel)
  - wallpaper
  - exposed unfinished brick (not coated/painted)
  - wood paneling (porous, food-incompatible)
  - drywall with standard paint finish
  - unfinished / shell condition with no wall coating
- unknown if walls not clearly visible across photos or material ambiguous

---

## Has_conference_room
This is a BUILDING-LEVEL feature — does the building have a conference / meeting hall
that tenants can use (either inside this listed unit, or as a shared building amenity)?

Allowed values:
- checked
- not checked
- unknown

Primary Source:
- TEXT (descriptions explicitly mention conference/meeting facilities)
Secondary Source:
- PHOTO (large hall with projector / conference table / theater-style seating)

Rules:
- if text mentions any of: "konferenču zāle", "sapulču zāle", "meeting room",
  "konferences telpa", "prezentāciju zāle", "konferenču istaba" -> checked
- if photos clearly show a large hall with conference table + projector OR
  theater-style seating arrangement for client meetings -> checked
- if enough interior photos exist of a building amenity area and no conference
  facility visible AND text does not mention it -> not checked
- if evidence too limited (only the rented unit shown, no common areas) -> unknown

Important:
- A small office kitchenette or staff room is NOT a conference room
- A regular office room with a table for a few people is NOT a conference room
  (must be designed for client meetings / presentations / formal gatherings)
---
## Zemes_gabals_m2

Primary:
- TEXT only

Rules:
- if the listing text explicitly mentions land plot / zemes gabals / zeme and gives area in m2, return that exact value
- examples: 2638 m2, 1500m2
- if nothing about land plot is mentioned in text -> Nav minēts
---
## Investiciju_strategija

Allowed values:
- Core/Core+
- Value-Add
- Distressed
- Nav investīciju objekts
- unknown

Meaning:
This field evaluates the investment profile of the BUILDING / ASSET primarily at building level, not only the specific small unit being advertised.

Primary:
- PHOTO (facade / building form / territory / common quality)
Secondary:
- TEXT

Important:
- judge mainly the building / asset quality and investment profile
- do not judge only by the size of the advertised unit
- a small unit inside a strong office complex can still be Core/Core+
- a converted office inside a residential building is usually not Core/Core+
- use this field only as an approximate investor-fit classification from listing evidence

Rules:
- Core/Core+ = professionally positioned commercial asset in good or very good condition, good location, stable-looking, modern or maintained, little obvious CAPEX need, suitable for conservative investor profile
- Value-Add = usable commercial asset, but with visible repositioning / improvement / modernization / management upside; often B/C class or under-optimized asset with improvement potential
- Distressed = clearly problematic, heavily outdated, strongly worn, technically weak, hard-to-lease without serious work, or requiring major intervention
- Nav investīciju objekts = more suitable for owner-user than investor; too small, too specific, too narrow-use, residential-conversion type office, or lacking clear investor logic
- unknown = not enough evidence

Additional logic:
- evaluate office, retail, warehouse, production, and medical assets by the same investor logic
- for retail or medical properties, do not automatically use Core/Core+ only because the use is specialized
- if the building itself is high-quality and professionally commercial, prefer Core/Core+
- if the advertised unit is inside a premium office complex / business center, judge by the building/complex, not only by one small room
- if the space is inside a residential / mixed-use non-investment-style building and functions more like a user premises than institutional asset, prefer Nav investīciju objekts or Value-Add
## Confidence
Return a string between 0.00 and 1.00

---

Guide:
- 0.90–1.00 = strong clear evidence
- 0.70–0.85 = mostly clear, some uncertainty
- 0.40–0.65 = partial evidence
- 0.00–0.35 = very weak evidence

---

## AGENT DETECTION (CRITICAL)

The listing might be posted by a real estate brokerage agent, not the actual owner.
We want to keep only owner-posted listings. Agents will be removed.

WHITELIST PRINCIPLE:
- Only flag as agent if you see CLEAR evidence from the BROKERAGE_LIST below.
- Everything else gets a free pass (default = NOT an agent).
- A logo from BUILDING_OWNER_LIST is NOT an agent — they post their own buildings.

BROKERAGE_LIST (any of these visible as logo, watermark, URL, or signature -> agent):
- Latio
- City24
- Arco Real Estate
- Ober-Haus
- Colliers
- ASTRA
- Cushman & Wakefield (also "C&W")
- JLL (Jones Lang LaSalle)
- Savills
- Knight Frank
- Newsec
- Lemmus
- Sotheby's International Realty
- AVER
- Prime Realty
- PRG
- ELE Properties
- InSign
- RentInRiga
- KIVI
- STARLEX

BUILDING_OWNER_LIST (NOT agents — these own and rent their own properties):
- EFTEN
- Capitalica
- Eastnine
- Hanner
- Galvanizers
- Hagberg
- BPM Real Estate
- ALSO
- Domina
- Linstow
- Galleria Riga
- ELL
- Lords LB
- Maxima
- Rimi Property
- Realto
- BPF (Baltic Property Fund)
- Varianti
- Ad Verum

Signal sources to inspect:
- WATERMARK on any image (corner logo, semi-transparent overlay)
- URL in the listing text (e.g., "latio.lv", "city24.lv", "arco.lv", "ober-haus.com")
- TEXT signatures: company name + "Aģentūra" / "©" / explicit brokerage branding

Decision rule:
- 1+ STRONG signal from BROKERAGE_LIST (watermark logo OR URL on their domain) -> Debug_status = "agent_detected"
- A weak text signal alone (just "Tel.:" or "Mob.:") is NOT enough — those are common in private ads
- Logo from BUILDING_OWNER_LIST -> NOT an agent, continue normal analysis
- Unknown logo / no clear brokerage signal -> NOT an agent (free pass, default behavior)

When Debug_status = "agent_detected":
- Put detected signals into Debug_note, e.g., "watermark:Latio; url:latio.lv"
- Other fields can stay at default values; the listing will be deleted from DB

---

## DATA CONSISTENCY CROSS-CHECK

The structured fields from ss.lv (street/area/floor) come from the listing's
form fields. The owner-written description text and the photos may show a
DIFFERENT address, area, or floor — for example, the owner writes a different
street name in the description to appear in neighbouring district searches.

Inspect these signals:
- Does the listing TEXT mention an address/m²/floor that contradicts the
  structured fields you were told?
- Do the PHOTOS (e.g., facade, building number visible) suggest a different
  address than the structured street?

Decision rule for `data_consistency` field:
- "consistent" — structured fields match what's in text and photos (default)
- "conflict" — clear evidence of a mismatch (e.g., text says "Stopiņu 36m²"
  but structured = "Piedrujas 28a, 103m²"; OR photos show a different building
  number than structured street)
- "unknown" — text/photos don't give enough evidence to verify either way

When you set "conflict", briefly explain the mismatch in the Debug_note field
(e.g., "text says Stopiņu iela 36m², struct says Piedrujas 28a 103m²").

---

## Debug_status
Use:
- ok
- agent_detected
- data_conflict
- no_html
- no_gallery_urls
- no_images_analyzed
- lookup_failed
- update_failed
- low_evidence

Use ok only if text + all images were analyzed.
Use agent_detected only when AGENT DETECTION rules above match.
Use data_conflict only when DATA CONSISTENCY CROSS-CHECK above sets
data_consistency = "conflict".

---

## Debug_note
Short note.
Mention briefly:
- how many images were reviewed
- whether facade/interior/plan existed
- if evidence was limited

---

## 6. FINAL OUTPUT RULE

Return ONLY valid JSON that matches the schema exactly.

No markdown.
No extra commentary.
No extra keys.
No field explanations outside JSON.
For enum fields, use only exact schema values.
"""

JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "building_type": {
            "type": "string",
            "enum": [
                "Biroju ēka",
                "Jaukta tipa ēka",
                "Industriāla ēka",
                "Tirdzniecības centrs",
                "Medicīnas centrs",
                "Autoserviss",
                "Unknown"
            ]
        },
        "building_class": {
            "type": "string",
            "enum": ["A", "B", "C", "unknown"]
        },
        "Building_description": {"type": "string"},
        "electric_power_kw": {"type": "string"},
        "Apsaimniekosanas_maksa": {"type": "string"},
        "NIN": {"type": "string"},
        "Komunalie": {"type": "string"},
        "Papildu_maksas": {"type": "string"},
        "Parkings": {
            "type": "string",
            "enum": ["Ir vietas", "Ir vietas par maksu", "Ir vietas bezmaksas", "Tikai ielas parking", "Nav", "unknown"]
        },
        "Space_group": {
            "type": "string",
            "enum": [
                "Birojs",
                "Tirdzniecība",
                "Noliktava",
                "Ražošana",
                "StockOfiss",
                "PVD",
                "Medicīna",
                "Studija",
                "Restorans/Cafe",
                "Sporta zāle",
                "Autoserviss",
                "unknown"
            ]
        },
        "Potential_space_group": {
            "type": "string",
            "pattern": r"^(unknown|(?:Birojs|Tirdzniecība|Noliktava|Ražošana|StockOfiss|PVD|Medicīna|Studija|Restorans/Cafe|Sporta zāle|Autoserviss)(?:, (?:Birojs|Tirdzniecība|Noliktava|Ražošana|StockOfiss|PVD|Medicīna|Studija|Restorans/Cafe|Sporta zāle|Autoserviss))*)$"
        },
        "Space_condition": {
            "type": "string",
            "enum": [
                "Jauns",
                "Labs",
                "Lietots",
                "Nepabeigts",
                "Nepieciešams remonts",
                "Kapitālais remonts",
                "unknown"
            ]
        },
        "Agent_comment": {"type": "string"},
        "Mebeleta_telpa": {
            "type": "string",
            "enum": ["Jā", "Nē", "Daļēji", "unknown"]
        },
        "Logu_type": {
            "type": "string",
            "enum": ["Lielie Logi", "Standarta logi", "Maz logu", "Nav logi", "unknown"]
        },
        "Gridas_materials": {
            "type": "string",
            "enum": [
                "Betona grīda",
                "Betona grīda ar trapiem",
                "Slīpēts betons",
                "Betons ar hardeneri",
                "Epoksīda grīda",
                "Poliuretāna-cementa grīda",
                "Cementa klons",
                "Mikrocements",
                "Keramikas flīzes",
                "Porcelāna flīzes",
                "Dabīgais akmens",
                "PVC / vinils",
                "Kvarca vinils",
                "Linolejs",
                "Gumijas grīda",
                "Paklājflīzes",
                "Koka grīda",
                "Parkets",
                "Lamināts",
                "Asfalta grīda",
                "Mūra grīda",
                "unknown"
            ]
        },
        "Gridas_izturiba_kg_m2": {"type": "string"},
        "Griestu_augstums": {"type": "string"},
        "Dalama_telpa": {
            "type": "string",
            "enum": ["Jā", "Nē", "Unkown"]
        },
        "Auto_pacelajs_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "street_entrance": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Cik_telpas": {"type": "string"},
        "cik_WC": {"type": "string"},
        "Apkure": {
            "type": "string",
            "enum": ["Centrālā", "Gāzes", "Elektriskā", "Nav", "unknown"]
        },
        "Treifelis_Pacelajs": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Virtuve_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Balkons_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Apsargajama_teritorija_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Ventilacijas_sistema_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Rampa_logistikai_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Rampa_logistikai_count": {"type": "string"},
        "Pacelamie_varti_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Pacelamie_varti_count": {"type": "string"},
        "Sava_ieeja_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Ir_izlietne_telpa_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Sava_eka_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Nozogota_teritorija_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Vides_pieejamiba_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Mazgajamas_sienas_check": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Has_conference_room": {
            "type": "string",
            "enum": ["checked", "not checked", "unknown"]
        },
        "Zemes_gabals_m2": {"type": "string"},
        "Investiciju_strategija": {
            "type": "string",
            "enum": ["Core/Core+", "Value-Add", "Distressed", "Nav investīciju objekts", "unknown"]
        },
        "Confidence": {"type": "string"},
        "Debug_status": {
            "type": "string",
            "enum": [
                "ok",
                "agent_detected",
                "data_conflict",
                "no_html",
                "no_gallery_urls",
                "no_images_analyzed",
                "lookup_failed",
                "update_failed",
                "low_evidence"
            ]
        },
        "Debug_note": {"type": "string"},
        "data_consistency": {
            "type": "string",
            "enum": ["consistent", "conflict", "unknown"]
        }
    },
    "required": [
        "building_type",
        "building_class",
        "Building_description",
        "electric_power_kw",
        "Apsaimniekosanas_maksa",
        "NIN",
        "Komunalie",
        "Papildu_maksas",
        "Parkings",
        "Space_group",
        "Potential_space_group",
        "Space_condition",
        "Agent_comment",
        "Mebeleta_telpa",
        "Logu_type",
        "Griestu_augstums",
        "Gridas_materials",
        "Gridas_izturiba_kg_m2",
        "Dalama_telpa",
        "street_entrance",
        "Auto_pacelajs_check",
        "Cik_telpas",
        "cik_WC",
        "Apkure",
        "Treifelis_Pacelajs",
        "Virtuve_check",
        "Balkons_check",
        "Apsargajama_teritorija_check",
        "Ventilacijas_sistema_check",
        "Rampa_logistikai_check",
        "Rampa_logistikai_count",
        "Pacelamie_varti_check",
        "Pacelamie_varti_count",
        "Sava_ieeja_check",
        "Ir_izlietne_telpa_check",
        "Sava_eka_check",
        "Nozogota_teritorija_check",
        "Vides_pieejamiba_check",
        "Mazgajamas_sienas_check",
        "Has_conference_room",
        "Zemes_gabals_m2",
        "Investiciju_strategija",
        "Confidence",
        "Debug_status",
        "Debug_note",
        "data_consistency"
    ],
    "title": "response_schema"
}

DEFAULT_OUTPUT = {
    "building_type": "Unknown",
    "building_class": "unknown",
    "Building_description": "unknown",
    "electric_power_kw": "unknown",
    "Apsaimniekosanas_maksa": "unknown",
    "NIN": "unknown",
    "Komunalie": "unknown",
    "Papildu_maksas": "unknown",
    "Parkings": "unknown",
    "Space_group": "unknown",
    "Potential_space_group": "unknown",
    "Space_condition": "unknown",
    "Agent_comment": "unknown",
    "Mebeleta_telpa": "unknown",
    "Logu_type": "unknown",
    "Griestu_augstums": "unknown",
    "Gridas_materials": "unknown",
    "Gridas_izturiba_kg_m2": "unknown",
    "Dalama_telpa": "Unkown",
    "Auto_pacelajs_check": "unknown",
    "street_entrance": "unknown",
    "Cik_telpas": "unknown",
    "cik_WC": "unknown",
    "Apkure": "unknown",
    "Treifelis_Pacelajs": "unknown",
    "Virtuve_check": "unknown",
    "Balkons_check": "unknown",
    "Apsargajama_teritorija_check": "unknown",
    "Ventilacijas_sistema_check": "unknown",
    "Rampa_logistikai_check": "unknown",
    "Rampa_logistikai_count": "unknown",
    "Pacelamie_varti_check": "unknown",
    "Pacelamie_varti_count": "unknown",
    "Sava_ieeja_check": "unknown",
    "Ir_izlietne_telpa_check": "unknown",
    "Sava_eka_check": "unknown",
    "Nozogota_teritorija_check": "unknown",
    "Vides_pieejamiba_check": "unknown",
    "Mazgajamas_sienas_check": "unknown",
    "Has_conference_room": "unknown",
    "Zemes_gabals_m2": "Nav minēts",
    "Investiciju_strategija": "unknown",
    "Confidence": "0.00",
    "Debug_status": "lookup_failed",
    "Debug_note": "fallback",
    "data_consistency": "unknown"
}

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "lv-LV,lv;q=0.9,en;q=0.8",
}

UUID_ALIASES = ["uuid", "UUID", "id", "ID"]
LINK_ALIASES = ["ss_link", "ss.lv link", "sslv_link", "link", "url", "sludinajuma_links", "listing_url"]
RAW_JSON_ALIASES = ["raw_json", "json", "result_json"]
DEBUG_STATUS_HEADER = "Debug_status"
DEBUG_NOTE_HEADER = "Debug_note"
FLOOR_ALIASES = ["floor", "stāvs", "stavs", "floors", "stavi"]
JPG_URLS_HEADER = "JPG bildes"


def normalize_header(h: str) -> str:
    return h.strip().lower()


def first_matching_header(headers: List[str], aliases: List[str]) -> Optional[str]:
    normalized = {normalize_header(h): h for h in headers}
    for alias in aliases:
        if normalize_header(alias) in normalized:
            return normalized[normalize_header(alias)]
    return None


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def get_headers(conn) -> List[str]:
    """Atgriež wordpress_inbox tabulas kolonnu nosaukumus."""
    with conn.cursor() as cur:
        cur.execute("""
            select column_name
            from information_schema.columns
            where table_schema = 'properties'
              and table_name = 'wordpress_inbox'
            order by ordinal_position
        """)
        return [row["column_name"] for row in cur.fetchall()]


def choose_row(conn) -> Optional[Tuple[int, Dict[str, Any], List[str]]]:
    """Paņem 1 wordpress_inbox rindu ar Debug_status IS NULL.

    FOR UPDATE SKIP LOCKED — droši, ja vairāki workeri darbojas paralēli.
    """
    headers = get_headers(conn)

    query = sql.SQL("""
        select *
        from properties.wordpress_inbox
        where coalesce("Debug_status"::text, '') = ''
        order by id
        limit 1
        for update skip locked
    """)

    with conn.cursor() as cur:
        cur.execute(query)
        row = cur.fetchone()

    if not row:
        return None

    return int(row["id"]), dict(row), headers


def fetch_html(url: str) -> str:
    if url.startswith("ss.lv/"):
        url = "https://" + url
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def extract_listing_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text_parts = []

    for sel in [
        "#msg_div_msg",
        ".ads_opt",
        "#tdo_8",
        "#tdo_20",
        "body",
    ]:
        nodes = soup.select(sel)
        if nodes:
            for n in nodes:
                t = n.get_text(" ", strip=True)
                if t and len(t) > 40:
                    text_parts.append(t)
            if text_parts:
                break

    text = "\n".join(text_parts).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def infer_floor_from_result(full_page_text: str, result: Dict[str, Any]) -> Optional[str]:
    low = full_page_text.lower()

    if re.search(r"\b1\.\s*stāvs\b|\b1\s*stāvs\b|\bpirmaj[āa]\s*stāvā\b", low):
        return "1. stāvs"

    if re.search(r"\botraj[āa]\s*stāvā\b|\btrešaj[āa]\s*stāvā\b|\bceturtaj[āa]\s*stāvā\b|\bvirs 1\. stāva\b", low):
        return "2+"

    first_floor_signals = any(result.get(k) == "checked" for k in [
        "street_entrance",
        "Sava_ieeja_check",
        "Rampa_logistikai_check",
        "Pacelamie_varti_check",
        "Auto_pacelajs_check",
    ])

    if first_floor_signals:
        return "1. stāvs"

    return None


def extract_full_page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text


def extract_floor_value(listing_text: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", listing_text).strip()
    low = text.lower()

    patterns = [
        r"(?:Stāvs|Stavs|floor)\s*[:\-]?\s*(\d+\s*-\s*\d+\s*/\s*\d+)",
        r"(?:Stāvs|Stavs|floor)\s*[:\-]?\s*(\d+\s*/\s*\d+)",
        r"(?:Stāvs|Stavs|floor)\s*[:\-]?\s*(\d+\s*-\s*\d+)",
        r"(?:Stāvs|Stavs|floor)\s*[:\-]?\s*(\d+)",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return re.sub(r"\s+", "", m.group(1))

    floors = set()

    word_map = {
        "pirmaj": 1,
        "otraj": 2,
        "trešaj": 3,
        "ceturtaj": 4,
        "piektaj": 5,
    }

    for word, num in word_map.items():
        if re.search(rf"\b{word}[āa]?\s+stāv", low):
            floors.add(num)

    # Bug fix WP versijā 2026-05-09: ss.lv apraksti parasti saka "1. stāvs",
    # bet WP/Houzez aprakstos parasti "1. stāvā" (locīta forma). Atļaujam
    # jebkuru locījuma galotni — stāvs/stāvā/stāvi/stāvu/stāvam.
    for m in re.finditer(r"(\d+)\.\s*stāv\w*", low):
        floors.add(int(m.group(1)))

    ground_signals = any(x in low for x in [
        "garāžu telp",
        "garāžu",
        "boksi",
        "auto pacēlāj",
        "pacēlāju",
        "vārti",
        "darbnīc",
        "servisu",
        "autoserviss",
    ])

    if re.search(r"otraj[āa].+trešaj[āa].+stāv", low) and ground_signals:
        return "1-3"

    if floors:
        mn, mx = min(floors), max(floors)

        if mn == mx == 1:
            return "1. stāvs"

        if mn == mx and mn > 1:
            return "2+"

        return f"{mn}-{mx}"

    # Bug fix WP versijā: atbalsta arī "stāvā/stāvi/stāvu" galotnes
    if re.search(r"\b1\.\s*stāv\w*\b|\b1\s*stāv\w*\b|\bpirmaj[āa]\s*stāvā\b", low):
        return "1. stāvs"

    if re.search(r"\botraj[āa]\s*stāvā\b|\btrešaj[āa]\s*stāvā\b|\bceturtaj[āa]\s*stāvā\b|\bvirs 1\. stāva\b", low):
        return "2+"

    return None


def extract_gallery_urls(html: str) -> List[str]:
    pattern = r'https://i\.ss\.lv/gallery/[^"\']+?\.jpg'
    found = re.findall(pattern, html, flags=re.IGNORECASE)

    if not found:
        pattern2 = r'//i\.ss\.lv/gallery/[^"\']+?\.jpg'
        found2 = re.findall(pattern2, html, flags=re.IGNORECASE)
        found.extend(["https:" + x for x in found2])

    cleaned = []
    seen = set()

    for url in found:
        url = url.strip()
        if "thumb" in url.lower() or ".t.jpg" in url.lower():
            continue
        if url not in seen:
            seen.add(url)
            cleaned.append(url)

    return cleaned


def format_image_urls(image_urls: List[str]) -> str:
    if not image_urls:
        return ""
    return " | ".join(f"{i}. {url}" for i, url in enumerate(image_urls, start=1))


def build_messages(listing_url: str, listing_text: str, image_urls: List[str]) -> List[Dict[str, Any]]:
    content = []

    content.append({
        "type": "input_text",
        "text": (
            "Tu analizē SS.lv komerctelpu sludinājumu. "
            "Obligāti izskati VISAS bildes secībā līdz pēdējai bildei. "
            "Pēdējā bilde bieži ir plāns un ir ļoti svarīga. "
            "Drīksti atbildēt tikai ar JSON pēc schema."
        )
    })

    content.append({
        "type": "input_text",
        "text": f"Sludinājuma URL: {listing_url}"
    })

    content.append({
        "type": "input_text",
        "text": f"Sludinājuma teksts:\n{listing_text}"
    })

    content.append({
        "type": "input_text",
        "text": f"Atrasto galerijas attēlu skaits: {len(image_urls)}. Attēli ir sakārtoti oriģinālajā secībā."
    })

    for i, url in enumerate(image_urls, start=1):
        content.append({
            "type": "input_text",
            "text": f"#photo-{i}"
        })
        content.append({
            "type": "input_image",
            "image_url": url,
            "detail": "high"
        })

    content.append({
        "type": "input_text",
        "text": PROMPT
    })

    return [{"role": "user", "content": content}]


def analyze_with_openai(listing_url: str, listing_text: str, image_urls: List[str]) -> Dict[str, Any]:
    if not image_urls:
        out = DEFAULT_OUTPUT.copy()
        out["Debug_status"] = "no_gallery_urls"
        out["Debug_note"] = "HTML atrasts, bet galerijas URL netika atrasti"
        return out

    response = client.responses.create(
        model=MODEL,
        input=build_messages(listing_url, listing_text, image_urls),
        text={
            "format": {
                "type": "json_schema",
                "name": "response_schema",
                "schema": JSON_SCHEMA,
                "strict": True,
            }
        },
    )

    text = response.output_text
    data = json.loads(text)

    if not data.get("Debug_status"):
        data["Debug_status"] = "ok"

    if not data.get("Debug_note"):
        data["Debug_note"] = f"Analizēts teksts un {len(image_urls)} attēli"

    return data


STREET_SUFFIX_RE = re.compile(
    r"\b([A-ZĀČĒĢĪĶĻŅŌŖŠŪŽ][a-zāčēģīķļņōŗšūž]+(?:\s+[A-ZĀČĒĢĪĶĻŅŌŖŠŪŽ][a-zāčēģīķļņōŗšūž]+)?)\s+"
    r"(iela|gatve|bulvāris|prospekts|šoseja|ceļš|laukums|aleja|krastmala)",
    re.UNICODE,
)


def _normalize_street(s: str) -> str:
    """'Piedrujas 28a' -> 'piedrujas'. Noņem numuru un 'iela/gatve/...' piedēkli."""
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"\s*\d+[a-zā-ž]?(?:[/\-]\d+)?\s*$", "", s)
    s = re.sub(r"\s+(iela|gatve|bulvāris|prospekts|šoseja|ceļš|laukums|aleja|krastmala)\b", "", s)
    return s.strip()


def _floor_signals_in_text(text: str) -> set[int]:
    """Ievelk no apraksta visus stāvu skaitļus (vārdiski + cipari)."""
    low = text.lower()
    floors: set[int] = set()
    word_map = {"pirmaj": 1, "otraj": 2, "trešaj": 3, "ceturtaj": 4, "piektaj": 5, "sestaj": 6, "septītaj": 7}
    for word, num in word_map.items():
        if re.search(rf"\b{word}[āa]?\s+stāv", low):
            floors.add(num)
    for m in re.finditer(r"\b(\d+)\.\s*stāv", low):
        try:
            floors.add(int(m.group(1)))
        except ValueError:
            pass
    return floors


def _structured_floor_int(floor_value: str) -> Optional[int]:
    """'1. stāvs' -> 1; '2' -> 2; '1-2' -> 1 (zemākais); 'None' -> None."""
    if not floor_value or floor_value.strip().lower() in {"none", ""}:
        return None
    m = re.search(r"(\d+)", str(floor_value))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def check_data_conflicts(row_data: Dict[str, Any], listing_text: str) -> Optional[str]:
    """
    B4 regex pre-check: salīdzina ss.lv strukturētos laukus pret apraksta tekstu.
    Atgriež None ja viss kārtībā, vai īsu konflikta aprakstu, ja ≥2 lauki nesakrīt.

    Pārbaudi 3 lauki:
      1. area_m2: tekstā minētie m² skaitļi vs strukt. area_m2
      2. floor: tekstā minētie stāvu skaitļi vs strukt. floor
      3. street: vai strukt. ielas vārds parādās tekstā; vai tekstā ir CITA "X iela" pieminēta

    Konflikts = ≥2 mismatch (lai izvairītos no false positives, kad apraksta neprecizitāte
    par 1 lauku ir normāla — piem. min vēl citu telpu m²).
    """
    if not listing_text:
        return None

    mismatches: list[str] = []

    # 1. AREA
    structured_area = row_data.get("area_m2")
    try:
        structured_area_n = float(structured_area) if structured_area is not None else None
    except (ValueError, TypeError):
        structured_area_n = None

    if structured_area_n and structured_area_n > 5:
        # Tekstā meklē m² skaitļus
        text_areas = []
        for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*m\s*[²2]", listing_text):
            try:
                v = float(m.group(1).replace(",", "."))
                if v > 5:  # ignorē nelielus skaitļus (piem. WC platība)
                    text_areas.append(v)
            except ValueError:
                continue
        if text_areas:
            # OK ja kāds no tekstā minētajiem ir 5% robežās ap strukt. vērtību
            tolerance = max(structured_area_n * 0.05, 2.0)
            if not any(abs(t - structured_area_n) <= tolerance for t in text_areas):
                # Apsvērt arī ka apraksts var pieminēt SUMMU vairāku telpu — ja sakrīt ar SUMMU, OK
                if not any(abs(sum(text_areas[:i]) - structured_area_n) <= tolerance for i in range(2, len(text_areas) + 1)):
                    mismatches.append(f"area:{int(structured_area_n)}↔{','.join(str(int(t)) for t in text_areas[:5])}")

    # 2. FLOOR
    structured_floor = _structured_floor_int(str(row_data.get("floor") or ""))
    if structured_floor:
        text_floors = _floor_signals_in_text(listing_text)
        if text_floors and structured_floor not in text_floors:
            mismatches.append(f"floor:{structured_floor}↔{sorted(text_floors)}")

    # 3. STREET
    structured_street = _normalize_street(str(row_data.get("street") or ""))
    if structured_street and len(structured_street) >= 4:
        # Strukt. ielas vārds tekstā
        struct_in_text = structured_street in listing_text.lower()
        # Atrod tekstā citas "X iela" minētās (kandidāti uz konfliktējošu adresi)
        text_streets = set()
        for m in STREET_SUFFIX_RE.finditer(listing_text):
            name = _normalize_street(m.group(0))
            if name and len(name) >= 4 and name != structured_street:
                text_streets.add(name)
        if text_streets and not struct_in_text:
            mismatches.append(f"street:{structured_street}↔{','.join(sorted(text_streets)[:3])}")

    if len(mismatches) >= 2:
        return "; ".join(mismatches)
    return None


def normalize_phone_for_blacklist(raw: str) -> Optional[str]:
    """
    Pārveido telefona numuru kanoniskā formātā priekš properties.phone_blacklist.
    LV: 8 cipari bez prefiksa. Ārvalstu: pilns ar valsts kodu, bez '+'.
    Sk. crm/import_blacklist.py par detalizētu loģiku — šeit kopēts, lai šis fails
    varētu darboties bez crm/ atkarības.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) < 8:
        return None
    if digits.startswith("371") and len(digits) == 11:
        return digits[3:]
    if len(digits) == 8 and digits[0] == "2":
        return digits
    for cc in ("47", "32", "33", "44", "49", "1"):
        if digits.startswith(cc) and 9 <= len(digits) <= 15:
            return digits
    if digits.startswith("7") and 10 <= len(digits) <= 12:
        return digits
    return None


def handle_agent_detected(conn, listing_id: int, row_data: Dict[str, Any], result: Dict[str, Any]) -> int:
    """
    Apstrādā AI atklātu aģentu:
    1. No row_data izvelk visus telefonus, normalizē
    2. INSERT katru phone_blacklist (ON CONFLICT DO NOTHING)
    3. DELETE no listings VISUS ierakstus ar šo telefonu (fan-out)
    Atgriež dzēsto listings rindu skaitu.
    """
    raw_phones = str(row_data.get("phone_numbers", "") or "")
    if not raw_phones.strip():
        print(f"  ⚠ agent_detected, bet nav phone_numbers — neko nevar blacklist")
        return 0

    # Atrast visus normalizētos numurus tekstā (var būt vairāki, atdalīti ar komatu/atstarpi)
    candidates = re.findall(r"\+?\d[\d\s\-\.\(\)]{6,}\d", raw_phones)
    normalized_phones = []
    for c in candidates:
        n = normalize_phone_for_blacklist(c)
        if n and n not in normalized_phones:
            normalized_phones.append(n)

    if not normalized_phones:
        print(f"  ⚠ agent_detected, bet phone_numbers='{raw_phones}' nedeva normalizētus numurus")
        return 0

    note = result.get("Debug_note", "") or ""
    inserted = 0
    deleted_total = 0

    with conn.cursor() as cur:
        for phone in normalized_phones:
            cur.execute(
                """
                INSERT INTO properties.phone_blacklist (phone_number, reason, notes, source_listing_id)
                VALUES (%s, 'ai_detected', %s, %s)
                ON CONFLICT (phone_number) DO NOTHING
                """,
                (phone, f"AI detection: {note[:200]}", listing_id),
            )
            if cur.rowcount > 0:
                inserted += 1

            # Fan-out: dzēš visus listings, kuru phone_numbers satur šo numuru
            cur.execute(
                """
                DELETE FROM properties.listings
                WHERE regexp_replace(coalesce(phone_numbers, ''), '[^0-9]', '', 'g')
                      LIKE '%%' || %s || '%%'
                """,
                (phone,),
            )
            deleted_total += cur.rowcount

    conn.commit()
    print(f"  ✓ agent_detected. Phones={normalized_phones}. Blacklist +{inserted}. Listings dzēsti: {deleted_total}.")
    return deleted_total


def delete_listing(conn, listing_id: int, reason: str) -> None:
    """
    Dzēš listings rindu, kad AI klasifikācija nav 'ok'.
    Filozofija (2026-05-01): listings tabula = unikālas, AI apstiprinātas
    telpas. Jebkas cits — data_conflict, low_evidence, no_gallery_urls,
    no_html, etc. — netiek glabāts. Scraper inbox cikls atradīs jaunu
    pareizu sludinājumu nākamajā kārtā, ja tāds vēl ss.lv pieejams.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM properties.listings WHERE id = %s",
            (listing_id,),
        )
    conn.commit()
    print(f"  🗑 DELETE listing_id={listing_id} | iemesls: {reason}")


def update_row(conn, inbox_id: int, row_data: Dict[str, Any], headers: List[str], result: Dict[str, Any]):
    """UPDATE wordpress_inbox rindu ar AI rezultātiem.

    NEPĀRRAKSTA "JPG bildes" lauku (wordpress_load.py to jau aizpildīja ar
    Houzez galerijas URL-iem) — atšķirībā no ss.lv versijas.
    """
    updates: Dict[str, Any] = {}
    raw_json_header = first_matching_header(headers, RAW_JSON_ALIASES)

    for key, value in result.items():
        if key.startswith("_"):
            continue

        if key in headers:
            cell_value = value

            if key == "Potential_space_group" and str(cell_value) == "unknown":
                cell_value = None

            updates[key] = cell_value

    # NB: NEPĀRRAKSTA raw_json — wordpress_load.py to aizpilda ar WP REST API
    # response (ieskaitot _embedded.wp:term taksonomijām, kas vajadzīgas
    # houzez_type_map backfill). AI rezultātus saglabājam individuālos laukos
    # (Space_group, building_class, Debug_note utt.), ne raw_json.

    # NB: NEPĀRRAKSTA "JPG bildes" — WP versijā to aizpilda wordpress_load.py
    # ar Houzez galerijas URL-iem, AI worker neatzīst tās rakstīt pa virsu.

    if not updates:
        return

    assignments = []
    params = []

    for col, val in updates.items():
        assignments.append(sql.SQL("{} = %s").format(sql.Identifier(col)))
        params.append(val)

    if "updated_at" in headers:
        assignments.append(sql.SQL("updated_at = now()"))

    query = sql.SQL("""
        update properties.wordpress_inbox
        set {}
        where id = %s
    """).format(sql.SQL(", ").join(assignments))

    params.append(inbox_id)

    with conn.cursor() as cur:
        cur.execute(query, params)

    conn.commit()


def update_debug_status_only(conn, inbox_id: int, status: str, note: str = ""):
    """Saglabā TIKAI Debug_status (un Debug_note), kad AI atgriež ne-'ok'.
    WP versijā NEKAD nedzēš rindu — paliek manuālai pārskatīšanai."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE properties.wordpress_inbox
            SET "Debug_status" = %s,
                "Debug_note"   = %s,
                updated_at     = now()
            WHERE id = %s
            """,
            (status, note[:500] if note else None, inbox_id),
        )
    conn.commit()


def build_listing_text_from_db(row: Dict[str, Any]) -> str:
    """Salikt sludinājuma tekstu AI vajadzībām no DB laukiem.

    Aizvieto ss.lv puses HTML scrape — WP rindām viss teksts jau ir DB.
    """
    parts: List[str] = []
    if row.get("post_title"):
        parts.append(str(row["post_title"]))
    if row.get("street"):
        parts.append(f"Adrese: {row['street']}")
    bits = []
    if row.get("city"):
        bits.append(f"pilsēta: {row['city']}")
    if row.get("district"):
        bits.append(f"rajons: {row['district']}")
    if row.get("listing_type"):
        bits.append(f"tips: {row['listing_type']}")
    if row.get("area_m2"):
        bits.append(f"platība: {row['area_m2']} m²")
    if row.get("price"):
        price_type = row.get("price_type") or ""
        bits.append(f"cena: {row['price']} EUR ({price_type})")
    if row.get("Cik_telpas"):
        bits.append(f"telpu skaits: {row['Cik_telpas']}")
    if row.get("cik_WC"):
        bits.append(f"WC skaits: {row['cik_WC']}")
    if bits:
        parts.append(" | ".join(bits))
    if row.get("post_excerpt"):
        excerpt = re.sub(r"<[^>]+>", " ", str(row["post_excerpt"]))
        excerpt = re.sub(r"\s+", " ", excerpt).strip()
        if excerpt:
            parts.append(f"Kopsavilkums: {excerpt}")
    if row.get("post_content"):
        content = re.sub(r"<[^>]+>", " ", str(row["post_content"]))
        content = re.sub(r"\s+", " ", content).strip()
        if content:
            parts.append(f"Apraksts: {content}")
    return "\n\n".join(parts)


def extract_image_urls_from_db(row: Dict[str, Any]) -> List[str]:
    """Iegūst image URL-us no wp_image_urls lauka (TEXT[]).

    Aizvieto ss.lv puses extract_gallery_urls — WP rindām bilžu URL-i jau ir DB.
    """
    urls = row.get("wp_image_urls")
    if not urls:
        return []
    if isinstance(urls, list):
        return [u for u in urls if u]
    return []


def run_once(conn) -> Optional[str]:
    """Paņem 1 wordpress_inbox rindu (Debug_status IS NULL), apstrādā ar AI, saglabā.

    KEY ATŠĶIRĪBAS no ss.lv versijas:
      - Listing teksts no DB (ne HTTP fetch)
      - Bilžu URL-i no wp_image_urls (ne regex no HTML)
      - NEKAD nedzēš rindu (B4, agent, data_conflict, ne-'ok' — tikai UPDATE Debug_status)
      - check_data_conflicts() (B4) un handle_agent_detected() — NEizsauc

    Atgriež Debug_status string vai None ja nav rindas.
    """
    row_result = choose_row(conn)

    if row_result is None:
        return None

    inbox_id, row_data, headers = row_result

    listing_url = str(row_data.get("link", "") or "").strip()
    wp_post_id = row_data.get("wp_post_id")

    print(f"  Inbox ID    : {inbox_id}")
    print(f"  WP post_id  : {wp_post_id}")
    print(f"  Link        : {listing_url}")

    # Teksts un bildes nāk no DB (ne HTTP fetch)
    listing_text = build_listing_text_from_db(row_data)
    image_urls = extract_image_urls_from_db(row_data)
    jpg_urls_value = format_image_urls(image_urls)

    print(f"  Teksta garums : {len(listing_text)}")
    print(f"  Attēlu skaits : {len(image_urls)}")

    if not image_urls:
        # WP rinda bez bildēm — AI nevar analizēt vizuāli, atstājam manuālai
        update_debug_status_only(conn, inbox_id, "no_gallery_urls",
                                 "wp_image_urls tukšs — Ieva nav augšupielādējusi bildes vai sync nesinhronēja")
        print(f"  ⚠ Nav bilžu — Debug_status='no_gallery_urls', atstāj manuālai pārskatīšanai")
        return "no_gallery_urls"

    # NB: B4 (check_data_conflicts) NEIZPILDA — Ieva pati ievada laukus,
    # konflikta kļūda nav scraper bug. Pilna AI prompt iekļauj data_consistency
    # check, kas šo apstrādā (skat zemāk).

    try:
        result = analyze_with_openai(listing_url, listing_text, image_urls)
    except Exception as e:
        err_str = str(e)
        low = err_str.lower()
        is_quota = (
            "insufficient_quota" in low
            or "exceeded your current quota" in low
            or "you exceeded your current quota" in low
            or ("429" in err_str and "quota" in low)
        )
        if is_quota:
            print("=" * 60)
            print("  STOP — OPENAI QUOTA PĀRSNIEGTS / BEIGUSIES NAUDA")
            print(f"  Kļūda: {err_str[:300]}")
            print(f"  Inbox ID kas palika bez apstrādes: {inbox_id}")
            print("  Rinda NETIEK atzīmēta kā failed — paliek retry-able.")
            print("  Papildini OpenAI billing un pārstartē workeri.")
            print("=" * 60)
            raise SystemExit(1)

        # OpenAI cita kļūda — atzīmē failed, BET NEDZĒŠ. Var retry vēlāk.
        update_debug_status_only(conn, inbox_id, "no_images_analyzed",
                                 f"OpenAI kļūda: {err_str[:300]}")
        print(f"  ✗ AI kļūda: {err_str[:200]}")
        return "no_images_analyzed"

    result["_jpg_urls_value"] = jpg_urls_value

    # Floor extraction no apraksta — tikai ja pre-match nav aizpildījis floor.
    # NEIZMANTOJAM infer_floor_from_result (checkbox heuristikas), jo WP rindām
    # tās ir nepatikamas (piem. street_entrance=checked nenozīmē 1.stāvs;
    # apraksts ir uzticamāks avots, regex extract_floor_value).
    if not str(row_data.get("floor", "") or "").strip():
        floor_value = extract_floor_value(listing_text)
        if floor_value:
            result["floor"] = floor_value

    # B2 cross-check — ja AI atklāj data_consistency=conflict, ATSTĀJAM rindu ar
    # Debug_status='data_conflict' priekš manuālas pārskatīšanas (NEDZĒŠAM!).
    if result.get("data_consistency") == "conflict" and result.get("Debug_status") != "agent_detected":
        # AI rezultātu joprojām saglabājam (lai redzētu, ko AI sacīja)
        result["Debug_status"] = "data_conflict"
        try:
            update_row(conn, inbox_id, row_data, headers, result)
        except Exception as e:
            conn.rollback()
            print(f"  ✗ DB update kļūda: {str(e)[:120]}")
            return "update_failed"
        print(f"  ⚠ data_conflict — saglabāts AI rezultāts, atstāj manuālai pārskatīšanai")
        return "data_conflict"

    # Agent detection — uz WP rindām nedrīkst trigger-oties (mēs publicējam paši),
    # bet ja gadījumā AI tā saka, NEKĀS nedaram (nedzēš telefonus, neblacklist),
    # vienkārši ierakstām statusu Debug_status=agent_detected priekš debug.
    if result.get("Debug_status") == "agent_detected":
        try:
            update_row(conn, inbox_id, row_data, headers, result)
        except Exception as e:
            conn.rollback()
            print(f"  ✗ DB update kļūda: {str(e)[:120]}")
            return "update_failed"
        print(f"  ⚠ agent_detected (negaidīti uz WP rindas) — saglabāts AI rezultāts, manuāli pārskata")
        return "agent_detected"

    # Jebkurš ne-'ok' statuss — saglabājam visu AI rezultātu, NEDZĒŠAM
    ai_status = result.get("Debug_status", "")
    if ai_status and ai_status != "ok":
        try:
            update_row(conn, inbox_id, row_data, headers, result)
        except Exception as e:
            conn.rollback()
            print(f"  ✗ DB update kļūda: {str(e)[:120]}")
            return "update_failed"
        print(f"  ⚠ AI status: {ai_status} — saglabāts, atstāj manuālai pārskatīšanai")
        return ai_status

    # OK — UPDATE rindu ar AI rezultātiem
    try:
        update_row(conn, inbox_id, row_data, headers, result)
    except Exception as e:
        conn.rollback()
        print(f"  ✗ DB update kļūda: {str(e)[:120]}")
        return "update_failed"

    print(f"  ✓ Saglabāts. Status: ok")
    return "ok"


def main():
    import argparse
    import time

    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="Apstrādā tikai 1 rindu un beidz (priekš testa)")
    ap.add_argument("--limit", type=int,
                    help="Apstrādā ne vairāk kā N rindas un beidz")
    args = ap.parse_args()

    print("=" * 55)
    print("  WORDPRESS AI WORKER — wordpress_inbox apstrāde")
    print(f"  Modelis       : {MODEL}")
    print(f"  Poll interval : {POLL_SECONDS}s")
    print(f"  Empty sleep   : {EMPTY_SLEEP_SECONDS}s")
    if args.once:
        print(f"  Mode          : --once (1 rinda, tad beidz)")
    elif args.limit:
        print(f"  Mode          : --limit {args.limit}")
    print("=" * 55)
    print("  Worker startēts. Gaida wordpress_inbox rindas...\n")

    cycle = 0
    processed = 0

    while True:
        cycle += 1
        conn = None
        try:
            conn = get_db()
            print(f"[cikls {cycle}] ──────────────────────────────────────")
            status = run_once(conn)

            if status is None:
                if args.once or args.limit:
                    print(f"[cikls {cycle}] Vairs nav rindu apstrādei. Beidzu.")
                    return
                print(f"[cikls {cycle}] Nav jaunu rindu. Sleep {EMPTY_SLEEP_SECONDS}s...")
                time.sleep(EMPTY_SLEEP_SECONDS)
            else:
                processed += 1
                print(f"[cikls {cycle}] Pabeigts → {status}.")
                if args.once:
                    return
                if args.limit and processed >= args.limit:
                    print(f"  Sasniegts --limit {args.limit}, beidzu.")
                    return
                time.sleep(POLL_SECONDS)

        except Exception as e:
            print(f"[cikls {cycle}] ✗ Neparedzēta kļūda: {e}")
            print(f"[cikls {cycle}] Sleep {EMPTY_SLEEP_SECONDS}s pirms retry...")
            time.sleep(EMPTY_SLEEP_SECONDS)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()